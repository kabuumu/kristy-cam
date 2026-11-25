"""FastAPI loopback server, security middleware, REST endpoints, Range streaming, and static UI."""

from __future__ import annotations

import array
import hmac
import html
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from email.parser import BytesParser
from email.policy import default as default_email_policy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from backend.usage_log import UsageLogger, get_usage_logger

from backend.bootstrap import REPO_ROOT, get_data_dir, is_offline_env, verify_ffmpeg, verify_font
from backend.geometry import (
    COORD_EPSILON,
    GeometryValidationError,
    compute_ideal_crop_size,
    slice_camera_keyframes_for_segment,
)
from backend.jobs import JobCancelledError, JobContext, JobManager
from backend.media import (
    MediaSelectionCancelled,
    MediaValidationError,
    import_uploaded_media,
    probe_and_validate_media,
    register_project_asset,
    select_media_native,
)
from backend.proxy import generate_proxy, probe_nvenc_support
from backend.renderer import (
    RenderCancelledError,
    RenderError,
    _is_multitrack_project,
    compute_render_hash,
    get_project_timeline_duration,
    plan_segment_duration,
    render_final_export,
    render_project_preview,
    render_segment_preview,
    render_segment_stage1,
)
from backend.schemas import (
    ArtifactManifest,
    AudioTrack,
    CaptionArtifactState,
    CaptionTrack,
    ExportOptions,
    MediaAssetMetadata,
    MediaProxyState,
    ProjectInput,
    ProxyState,
    SegmentArtifactState,
    SourceMediaRef,
    SourceMetadata,
    VideoTrack,
)
from backend.storage import (
    InsufficientDiskSpaceError,
    ProjectNotFoundError,
    RevisionConflictError,
    StorageError,
    UnsupportedSchemaVersionError,
    check_source_media,
    compute_file_fingerprint,
    create_project,
    delete_project,
    duplicate_project,
    ensure_free_disk_space,
    ensure_primary_media_registered,
    get_project_dir,
    load_global_pronunciations,
    load_manifest,
    load_project,
    relink_source_media,
    rename_project,
    save_global_pronunciations,
    save_manifest,
    save_project,
)
from backend.subtitles import (
    build_segment_subtitle_events,
    build_timeline_subtitle_events,
    render_srt_content,
)
from backend.tts import (
    compute_caption_tts_hash,
    compute_tts_hash,
    list_voices,
    resolve_caption_spoken_text,
    resolve_clause_spoken_text,
    synthesize_caption_tts,
    synthesize_segment_tts,
)
from backend.updater import (
    UpdaterError,
    check_for_update,
    download_installer,
    get_build_info,
    launch_windows_installer_and_exit,
)

APP_VERSION = get_build_info()["version"]
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "testserver"})
ALLOWED_ORIGINS = frozenset({"127.0.0.1", "localhost"})
MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
FRONTEND_DIST_DIR = REPO_ROOT / "frontend" / "dist"
STREAM_CHUNK_SIZE = 64 * 1024

_STATE_INIT_LOCK = threading.Lock()
_UPDATE_LOCK = threading.Lock()
_UPDATE_CHECK_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_UPDATE_INSTALL_STATE: dict[str, Any] = {
    "status": "idle",  # idle | downloading | installing | error
    "progress": 0,
    "stage": "",
    "error": None,
}


def _extract_hostname(host_header: str) -> str:
    val = host_header.strip().lower()
    if not val:
        return ""
    if val.startswith("["):
        end = val.find("]")
        if end == -1:
            return ""
        return val[1:end]
    if ":" in val:
        return val.split(":", 1)[0]
    return val


def _ensure_state_initialized(target_app: FastAPI) -> None:
    with _STATE_INIT_LOCK:
        if not hasattr(target_app.state, "data_dir") or target_app.state.data_dir is None:
            target_app.state.data_dir = get_data_dir()
        if not hasattr(target_app.state, "session_token") or not target_app.state.session_token:
            target_app.state.session_token = secrets.token_urlsafe(32)
        if not hasattr(target_app.state, "media_registry") or target_app.state.media_registry is None:
            target_app.state.media_registry = {}
        if not hasattr(target_app.state, "manifest_locks_guard") or target_app.state.manifest_locks_guard is None:
            target_app.state.manifest_locks_guard = threading.Lock()
        if not hasattr(target_app.state, "manifest_locks") or target_app.state.manifest_locks is None:
            target_app.state.manifest_locks = {}
        if not hasattr(target_app.state, "job_manager") or target_app.state.job_manager is None:
            jm = JobManager(data_dir=target_app.state.data_dir)
            jm.recover_on_startup()
            target_app.state.job_manager = jm
        if not hasattr(target_app.state, "usage_logger") or target_app.state.usage_logger is None:
            target_app.state.usage_logger = get_usage_logger(target_app.state.data_dir)


def _get_data_dir(request: Request) -> Path:
    _ensure_state_initialized(request.app)
    return Path(request.app.state.data_dir)


def _get_job_manager(request: Request) -> JobManager:
    _ensure_state_initialized(request.app)
    return request.app.state.job_manager


def _get_usage_logger(request: Request) -> UsageLogger:
    _ensure_state_initialized(request.app)
    return request.app.state.usage_logger


def _get_manifest_lock(target_app: FastAPI, project_id: str) -> threading.Lock:
    _ensure_state_initialized(target_app)
    with target_app.state.manifest_locks_guard:
        lock = target_app.state.manifest_locks.get(project_id)
        if lock is None:
            lock = threading.Lock()
            target_app.state.manifest_locks[project_id] = lock
        return lock


@asynccontextmanager
async def lifespan(target_app: FastAPI) -> AsyncIterator[None]:
    """Initialize data dir, recover interrupted jobs, generate session token, and shut down cleanly."""
    data_dir = get_data_dir()
    usage_logger = get_usage_logger(data_dir)
    job_manager = JobManager(data_dir=data_dir)
    job_manager.recover_on_startup()

    target_app.state.data_dir = data_dir
    target_app.state.usage_logger = usage_logger
    target_app.state.job_manager = job_manager
    target_app.state.session_token = secrets.token_urlsafe(32)
    target_app.state.media_registry = {}
    target_app.state.manifest_locks_guard = threading.Lock()
    target_app.state.manifest_locks = {}
    usage_logger.log_event(
        source="backend",
        category="intention",
        action="server.started",
        intention="Start Sims 4 Vertical Video Studio backend server",
        output_data={"data_dir": str(data_dir), "app_version": APP_VERSION},
    )
    try:
        yield
    finally:
        job_manager.shutdown(wait=False)


app = FastAPI(title="Sims 4 Vertical Video Studio", lifespan=lifespan)
app.state.session_token = secrets.token_urlsafe(32)
app.state.media_registry = {}


@app.exception_handler(HTTPException)
async def http_exception_logging_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Log HTTPException details to usage/debug logs before returning standard JSON error."""
    try:
        usage_logger = _get_usage_logger(request)
        usage_logger.log_error(
            source="backend",
            action="api.http_error",
            intention=f"{request.method} {request.url.path}",
            target=f"{request.method} {request.url.path}",
            status_code=exc.status_code,
            message=str(exc.detail),
            error_type="HTTPException",
            detail=exc.detail,
            context={
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query) if request.url.query else None,
            },
        )
    except Exception:
        pass
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.exception_handler(Exception)
async def unhandled_exception_logging_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log unhandled exceptions with full traceback to usage/debug logs."""
    try:
        usage_logger = _get_usage_logger(request)
        usage_logger.log_error(
            source="backend",
            action="api.unhandled_error",
            intention=f"{request.method} {request.url.path}",
            target=f"{request.method} {request.url.path}",
            status_code=500,
            context={
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query) if request.url.query else None,
            },
            exc=exc,
        )
    except Exception:
        pass
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.middleware("http")
async def loopback_security_middleware(request: Request, call_next: Any) -> Response:
    """Enforce loopback Host/Origin checks and X-Session-Token on mutating endpoints."""
    _ensure_state_initialized(request.app)
    usage_logger: UsageLogger = request.app.state.usage_logger

    host_header = request.headers.get("host", "")
    hostname = _extract_hostname(host_header)
    if hostname not in ALLOWED_HOSTS:
        usage_logger.log_error(
            source="backend",
            action="security.forbidden_host",
            intention="Validate loopback Host header",
            target=f"{request.method} {request.url.path}",
            status_code=403,
            message="Forbidden Host header",
            error_type="SecurityRejection",
            context={"host": host_header},
        )
        return JSONResponse(status_code=403, content={"detail": "Forbidden Host header"})

    origin_header = request.headers.get("origin")
    if origin_header is not None and origin_header.strip():
        parsed = urlparse(origin_header.strip())
        origin_host = (parsed.hostname or "").lower()
        origin_scheme = (parsed.scheme or "").lower()
        if origin_host not in ALLOWED_ORIGINS or (origin_scheme and origin_scheme not in {"http", "https"}):
            usage_logger.log_error(
                source="backend",
                action="security.forbidden_origin",
                intention="Validate loopback Origin header",
                target=f"{request.method} {request.url.path}",
                status_code=403,
                message="Forbidden Origin header",
                error_type="SecurityRejection",
                context={"origin": origin_header},
            )
            return JSONResponse(status_code=403, content={"detail": "Forbidden Origin header"})

    if request.method.upper() in MUTATING_METHODS:
        provided_token = request.headers.get("x-session-token")
        expected_token: str = request.app.state.session_token
        if not provided_token:
            usage_logger.log_error(
                source="backend",
                action="security.missing_token",
                intention="Validate X-Session-Token header",
                target=f"{request.method} {request.url.path}",
                status_code=401,
                message="Missing X-Session-Token header",
                error_type="SecurityRejection",
            )
            return JSONResponse(status_code=401, content={"detail": "Missing X-Session-Token header"})
        if not hmac.compare_digest(provided_token, expected_token):
            usage_logger.log_error(
                source="backend",
                action="security.invalid_token",
                intention="Validate X-Session-Token header",
                target=f"{request.method} {request.url.path}",
                status_code=403,
                message="Invalid X-Session-Token header",
                error_type="SecurityRejection",
            )
            return JSONResponse(status_code=403, content={"detail": "Invalid X-Session-Token header"})

    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = (time.perf_counter() - t0) * 1000.0
        usage_logger.log_error(
            source="backend",
            action="api.unhandled_error",
            intention=f"{request.method} {request.url.path}",
            target=f"{request.method} {request.url.path}",
            status_code=500,
            context={"method": request.method, "path": request.url.path},
            exc=exc,
            duration_ms=duration_ms,
        )
        raise
    return response


def _validate_manual_media_path(raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise MediaValidationError("Media path must be a non-empty string.")
    candidate = Path(raw_path.strip())
    if not candidate.is_absolute():
        raise MediaValidationError(f"Media path must be absolute: {raw_path!r}")
    if ".." in candidate.parts:
        raise MediaValidationError(f"Media path must not contain '..': {raw_path!r}")
    return candidate


def _parse_multipart_file(content_type: str, body: bytes) -> tuple[str, bytes]:
    filename, payload, _ = _parse_multipart_upload(content_type, body)
    return filename, payload


def _parse_multipart_upload(content_type: str, body: bytes) -> tuple[str, bytes, dict[str, str]]:
    header_block = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
    msg = BytesParser(policy=default_email_policy).parsebytes(header_block + body)
    if not msg.is_multipart():
        raise MediaValidationError("Invalid multipart/form-data payload.")
    fields: dict[str, str] = {}
    found_file: tuple[str, bytes] | None = None
    for part in msg.iter_parts():
        disp_name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            raw = part.get_payload(decode=False)
            payload = raw.encode("utf-8") if isinstance(raw, str) else b""
        if disp_name == "file" or filename:
            if not filename:
                raise MediaValidationError("Uploaded multipart part is missing a filename.")
            if found_file is None:
                found_file = (filename, payload)
        elif disp_name:
            fields[str(disp_name)] = payload.decode("utf-8", errors="replace").strip()
    if found_file is None:
        raise MediaValidationError("No file field found in multipart/form-data request.")
    return found_file[0], found_file[1], fields


def _validate_relative_artifact_path(project_dir: Path, rel_path: str) -> Path:
    if not rel_path or "\x00" in rel_path:
        raise HTTPException(status_code=403, detail="Invalid artifact path")
    candidate_rel = Path(rel_path)
    if candidate_rel.is_absolute() or ".." in candidate_rel.parts:
        raise HTTPException(status_code=403, detail="Path traversal rejected")
    project_root = project_dir.resolve()
    resolved = (project_root / candidate_rel).resolve()
    if not resolved.is_relative_to(project_root):
        raise HTTPException(status_code=403, detail="Artifact path escapes project directory")
    return resolved


def _resolve_artifact_path(
    manifest: ArtifactManifest,
    project_dir: Path,
    artifact_id: str,
    project: ProjectInput | None = None,
) -> Path | None:
    rel_path: str | None = None
    if artifact_id == "art_proxy" or manifest.proxy.artifact_id == artifact_id:
        rel_path = manifest.proxy.path
    elif manifest.latest_preview_artifact_id == artifact_id:
        rel_path = manifest.latest_preview_path
    elif manifest.latest_export_artifact_id == artifact_id:
        rel_path = manifest.latest_export_path
    else:
        for aid, pstate in manifest.media_proxies.items():
            if pstate.artifact_id == artifact_id or artifact_id == f"art_proxy_{aid}":
                rel_path = pstate.path
                break
        if not rel_path:
            for cid, cstate in manifest.caption_artifacts.items():
                if cstate.audio_artifact_id == artifact_id or artifact_id == f"art_cap_{cid}":
                    rel_path = cstate.audio_path
                    break
        if not rel_path:
            for seg_state in manifest.segments.values():
                if seg_state.audio_artifact_id == artifact_id:
                    rel_path = seg_state.audio_path
                    break
                if seg_state.segment_artifact_id == artifact_id:
                    rel_path = seg_state.rendered_segment_path
                    break
                if seg_state.preview_artifact_id == artifact_id:
                    rel_path = seg_state.preview_path
                    break

    if rel_path:
        return _validate_relative_artifact_path(project_dir, rel_path)

    if project is not None:
        asset_ref = project.media_assets.get(artifact_id)
        if asset_ref is None and artifact_id == project.source_media.media_id:
            asset_path_str = project.source_media.path
            copied_to_project = project.source_media.copied_to_project
        elif asset_ref is not None:
            asset_path_str = asset_ref.path
            copied_to_project = asset_ref.copied_to_project
        else:
            return None

        if not asset_path_str or "\x00" in asset_path_str:
            raise HTTPException(status_code=403, detail="Invalid media asset path")
        raw_path = Path(asset_path_str)
        if ".." in raw_path.parts:
            raise HTTPException(status_code=403, detail="Path traversal rejected")
        resolved = raw_path.resolve()
        if copied_to_project:
            project_root = project_dir.resolve()
            if not resolved.is_relative_to(project_root):
                raise HTTPException(status_code=403, detail="Copied asset path escapes project directory")
        return resolved

    return None


def _parse_byte_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    """Parse HTTP Range: bytes=... header. Returns (start, end) inclusive, or None if invalid/unsatisfiable."""
    if file_size <= 0:
        return None
    raw = range_header.strip()
    if not raw.lower().startswith("bytes="):
        return None
    spec = raw[6:].strip()
    if not spec or "," in spec or "-" not in spec:
        return None

    start_str, end_str = spec.split("-", 1)
    start_str = start_str.strip()
    end_str = end_str.strip()

    if not start_str and not end_str:
        return None

    try:
        if not start_str:
            suffix_len = int(end_str)
            if suffix_len <= 0:
                return None
            start = max(0, file_size - suffix_len)
            end = file_size - 1
            return start, end

        start = int(start_str)
        if start < 0 or start >= file_size:
            return None

        if not end_str:
            end = file_size - 1
        else:
            end = int(end_str)
            if end < start:
                return None
            end = min(end, file_size - 1)
        return start, end
    except ValueError:
        return None


def _iter_file_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _media_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    mapping = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".avi": "video/x-msvideo",
        ".m4v": "video/x-m4v",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".flac": "audio/flac",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }
    return mapping.get(suffix, "application/octet-stream")


def _asset_meta_from_source(meta: SourceMetadata) -> MediaAssetMetadata:
    return MediaAssetMetadata(
        kind="video",
        width=meta.width,
        height=meta.height,
        fps=meta.fps,
        duration=meta.duration,
        has_audio=meta.has_audio,
        has_alpha=False,
        codec_name=meta.codec_name,
        pix_fmt=meta.pix_fmt,
        audio_sample_rate=meta.audio_sample_rate,
        audio_channels=meta.audio_channels,
    )


def _find_caption_clip(project: ProjectInput, clip_id: str) -> Any | None:
    for track in project.tracks:
        if isinstance(track, CaptionTrack):
            for clip in track.clips:
                if clip.id == clip_id:
                    return clip
    return None


def _ensure_caption_tts(
    target_app: FastAPI,
    project: ProjectInput,
    clip_id: str,
    project_dir: Path,
    data_dir: Path,
    manifest: ArtifactManifest,
) -> CaptionArtifactState:
    clip = _find_caption_clip(project, clip_id)
    if clip is None:
        raise ValueError(f"CaptionClip {clip_id!r} not found")

    expected_hash = compute_caption_tts_hash(
        clip=clip,
        pronunciations=project.pronunciations,
        voice=project.voice,
        language=project.language,
    )
    spoken = resolve_caption_spoken_text(clip, project.pronunciations).strip()
    existing = manifest.caption_artifacts.get(clip_id)

    if not clip.tts_enabled or not spoken:
        if (
            existing is not None
            and existing.tts_hash == expected_hash
            and existing.audio_artifact_id is None
        ):
            return existing
        state = CaptionArtifactState(
            tts_hash=expected_hash,
            audio_artifact_id=None,
            audio_path=None,
            sample_rate=24000,
            total_samples=0,
            audio_duration=0.0,
        )
        lock = _get_manifest_lock(target_app, project.project_id)
        with lock:
            latest_manifest = load_manifest(project.project_id, data_dir=data_dir)
            latest_manifest.caption_artifacts[clip_id] = state
            save_manifest(latest_manifest, data_dir=data_dir)
        manifest.caption_artifacts[clip_id] = state.model_copy(deep=True)
        return state

    if (
        existing is not None
        and existing.tts_hash == expected_hash
        and existing.audio_path
        and (project_dir / existing.audio_path).is_file()
    ):
        return existing

    res = synthesize_caption_tts(
        clip=clip,
        pronunciations=project.pronunciations,
        voice=project.voice,
        language=project.language,
        output_dir=project_dir / "artifacts",
    )
    rel_audio = res.audio_path.relative_to(project_dir).as_posix() if res.audio_path is not None else None
    audio_art_id = f"art_cap_{clip_id}" if res.audio_path is not None else None

    state = CaptionArtifactState(
        tts_hash=res.tts_hash,
        audio_artifact_id=audio_art_id,
        audio_path=rel_audio,
        sample_rate=res.sample_rate,
        total_samples=res.total_samples,
        audio_duration=res.audio_duration,
    )
    lock = _get_manifest_lock(target_app, project.project_id)
    with lock:
        latest_manifest = load_manifest(project.project_id, data_dir=data_dir)
        latest_manifest.caption_artifacts[clip_id] = state
        save_manifest(latest_manifest, data_dir=data_dir)

    manifest.caption_artifacts[clip_id] = state.model_copy(deep=True)
    return state


def _compute_stale_tts_captions(
    project: ProjectInput,
    manifest: ArtifactManifest,
    project_dir: Path,
) -> list[str]:
    """Return CaptionClip IDs whose synthesized TTS audio is missing or out of date."""
    stale: list[str] = []
    for track in project.tracks:
        if not isinstance(track, CaptionTrack):
            continue
        for clip in track.clips:
            spoken = resolve_caption_spoken_text(clip, project.pronunciations).strip()
            existing = manifest.caption_artifacts.get(clip.id)
            if not clip.tts_enabled or not spoken:
                if existing is not None and existing.audio_artifact_id:
                    stale.append(clip.id)
                continue
            expected_hash = compute_caption_tts_hash(
                clip=clip,
                pronunciations=project.pronunciations,
                voice=project.voice,
                language=project.language,
            )
            if (
                existing is None
                or existing.tts_hash != expected_hash
                or not existing.audio_path
                or not (project_dir / existing.audio_path).is_file()
            ):
                stale.append(clip.id)
    return stale


def _ensure_segment_tts(
    target_app: FastAPI,
    project: ProjectInput,
    seg_id: str,
    project_dir: Path,
    data_dir: Path,
    manifest: ArtifactManifest,
) -> SegmentArtifactState:
    segment = next((s for s in project.segments if s.id == seg_id), None)
    if segment is None:
        raise ValueError(f"Segment {seg_id!r} not found")

    expected_hash = compute_tts_hash(
        segment=segment,
        pronunciations=project.pronunciations,
        voice=project.voice,
        language=project.language,
        inter_clause_pause_ms=project.output_settings.inter_clause_pause_ms,
    )

    existing = manifest.segments.get(seg_id)
    if existing is not None and existing.tts_hash == expected_hash:
        if not segment.clauses:
            return existing
        if existing.audio_path:
            audio_abs = project_dir / existing.audio_path
            if audio_abs.is_file():
                return existing

    res = synthesize_segment_tts(
        segment=segment,
        pronunciations=project.pronunciations,
        voice=project.voice,
        language=project.language,
        inter_clause_pause_ms=project.output_settings.inter_clause_pause_ms,
        output_dir=project_dir / "artifacts",
    )

    rel_audio = res.audio_path.relative_to(project_dir).as_posix() if res.audio_path is not None else None
    audio_art_id = f"art_tts_{seg_id}" if res.audio_path is not None else None

    lock = _get_manifest_lock(target_app, project.project_id)
    with lock:
        latest_manifest = load_manifest(project.project_id, data_dir=data_dir)
        seg_state = latest_manifest.segments.get(seg_id, SegmentArtifactState())
        seg_state.tts_hash = res.tts_hash
        seg_state.audio_artifact_id = audio_art_id
        seg_state.audio_path = rel_audio
        seg_state.sample_rate = res.sample_rate
        seg_state.total_samples = res.total_samples
        seg_state.audio_duration = res.audio_duration
        seg_state.clause_timings = list(res.clause_timings)
        latest_manifest.segments[seg_id] = seg_state
        save_manifest(latest_manifest, data_dir=data_dir)

    manifest.segments[seg_id] = seg_state.model_copy(deep=True)
    return seg_state


def _compute_stale_tts_segments(
    project: ProjectInput,
    manifest: ArtifactManifest,
    project_dir: Path,
) -> list[str]:
    """Return segment IDs whose synthesized TTS audio is missing or out of date."""
    stale: list[str] = []
    for seg in project.segments:
        has_spoken = any(
            resolve_clause_spoken_text(c, project.pronunciations).strip()
            for c in seg.clauses
        )
        existing = manifest.segments.get(seg.id)
        if not has_spoken:
            if existing is not None and existing.audio_artifact_id:
                stale.append(seg.id)
            continue
        expected_hash = compute_tts_hash(
            segment=seg,
            pronunciations=project.pronunciations,
            voice=project.voice,
            language=project.language,
            inter_clause_pause_ms=project.output_settings.inter_clause_pause_ms,
        )
        if (
            existing is None
            or existing.tts_hash != expected_hash
            or not existing.audio_path
            or not (project_dir / existing.audio_path).is_file()
        ):
            stale.append(seg.id)
    return stale


# --- REST Endpoints (F37) ---


@app.get("/api/health")
def api_health() -> dict[str, str]:
    """Liveness and app identity check."""
    return {"status": "ok", "app": "sims-vertical-studio", "version": APP_VERSION}


@app.get("/api/app/update-check")
def api_app_update_check(force: bool = False) -> dict[str, Any]:
    """Check GitHub Releases for a newer Windows release (cached 5 min unless force=true)."""
    now = time.monotonic()
    with _UPDATE_LOCK:
        cached = _UPDATE_CHECK_CACHE.get("data")
        cached_ts = float(_UPDATE_CHECK_CACHE.get("ts") or 0.0)
        if not force and cached is not None and (now - cached_ts) < 300.0:
            return dict(cached)

    result = check_for_update()
    with _UPDATE_LOCK:
        _UPDATE_CHECK_CACHE["ts"] = time.monotonic()
        _UPDATE_CHECK_CACHE["data"] = dict(result)
    return result


@app.get("/api/app/update-status")
def api_app_update_status() -> dict[str, Any]:
    """Return current download/install status of an in-progress self-update."""
    with _UPDATE_LOCK:
        return dict(_UPDATE_INSTALL_STATE)


@app.post("/api/app/update-install", status_code=202)
def api_app_update_install(request: Request) -> dict[str, Any]:
    """Download the latest Windows Setup installer from GitHub and launch it silently."""
    data_dir = _get_data_dir(request)
    info = get_build_info()
    if not info.get("can_self_update"):
        raise HTTPException(
            status_code=400,
            detail="Automatic in-app updates are only available in the installed Windows desktop app.",
        )

    with _UPDATE_LOCK:
        if _UPDATE_INSTALL_STATE["status"] in ("downloading", "installing"):
            return dict(_UPDATE_INSTALL_STATE)

    check = check_for_update()
    if check.get("error"):
        raise HTTPException(status_code=502, detail=str(check["error"]))
    if not check.get("download_url") or not check.get("asset_name"):
        raise HTTPException(status_code=404, detail="No Windows installer asset found in the latest release.")

    download_url = str(check["download_url"])
    asset_name = str(check["asset_name"])

    with _UPDATE_LOCK:
        _UPDATE_INSTALL_STATE.update(
            {"status": "downloading", "progress": 2, "stage": "Starting download...", "error": None}
        )

    def _worker() -> None:
        def _on_prog(pct: int, stage: str) -> None:
            with _UPDATE_LOCK:
                _UPDATE_INSTALL_STATE["progress"] = pct
                _UPDATE_INSTALL_STATE["stage"] = stage

        try:
            installer_path = download_installer(
                download_url=download_url,
                asset_name=asset_name,
                data_dir=data_dir,
                on_progress=_on_prog,
            )
            with _UPDATE_LOCK:
                _UPDATE_INSTALL_STATE.update(
                    {"status": "installing", "progress": 100, "stage": "Launching installer & restarting..."}
                )
            launch_windows_installer_and_exit(installer_path)
        except (UpdaterError, Exception) as exc:
            with _UPDATE_LOCK:
                _UPDATE_INSTALL_STATE.update(
                    {"status": "error", "stage": "Update failed", "error": str(exc)}
                )

    threading.Thread(target=_worker, name="app-self-updater", daemon=True).start()
    with _UPDATE_LOCK:
        return dict(_UPDATE_INSTALL_STATE)


@app.get("/api/capabilities")
def api_capabilities(request: Request) -> dict[str, Any]:
    """Return FFmpeg, libass, font, Kokoro, native picker, and NVENC readiness."""
    data_dir = _get_data_dir(request)
    offline = is_offline_env()
    errors: list[str] = []

    ffmpeg_ready = False
    libass_ready = False
    nvenc_ready = False
    try:
        verified_ffmpeg, _ = verify_ffmpeg(data_dir=data_dir, offline=offline)
        ffmpeg_ready = True
        libass_ready = True
        nvenc_ready = probe_nvenc_support(verified_ffmpeg)
    except Exception as exc:
        errors.append(f"ffmpeg: {exc}")

    font_ready = False
    try:
        verify_font()
        font_ready = True
    except Exception as exc:
        errors.append(f"font: {exc}")

    kokoro_ready = False
    voices: list[str] = []
    try:
        voices = list_voices(data_dir=data_dir, offline=offline)
        kokoro_ready = True
    except Exception as exc:
        errors.append(f"kokoro: {exc}")

    native_picker_supported = sys.platform in {"darwin", "win32"}

    return {
        "ffmpeg_ready": ffmpeg_ready,
        "libass_ready": libass_ready,
        "kokoro_ready": kokoro_ready,
        "ffmpeg_ok": ffmpeg_ready,
        "libass_ok": libass_ready,
        "kokoro_ok": kokoro_ready,
        "font_ready": font_ready,
        "native_picker_supported": native_picker_supported,
        "nvenc_ready": nvenc_ready,
        "voices": voices,
        "errors": errors,
    }


@app.post("/api/media/select")
async def api_media_select(request: Request) -> dict[str, Any]:
    """Select and validate source media via JSON path, multipart upload, or native OS picker."""
    data_dir = _get_data_dir(request)
    content_type = request.headers.get("content-type", "")
    raw_body = await request.body()

    try:
        if "multipart/form-data" in content_type.lower():
            filename, file_bytes = _parse_multipart_file(content_type, raw_body)
            media_path = await run_in_threadpool(import_uploaded_media, filename, file_bytes, data_dir / "uploads")
        elif raw_body.strip():
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=422, detail="Request body must be a JSON object")
            if payload.get("path") is not None:
                media_path = _validate_manual_media_path(payload["path"])
            else:
                selected = await run_in_threadpool(select_media_native)
                if selected is None:
                    raise MediaSelectionCancelled("Media selection cancelled by user")
                media_path = selected
        else:
            selected = await run_in_threadpool(select_media_native)
            if selected is None:
                raise MediaSelectionCancelled("Media selection cancelled by user")
            media_path = selected

        metadata = await run_in_threadpool(probe_and_validate_media, media_path, data_dir=data_dir)
        resolved_path = media_path.resolve()
        fingerprint = await run_in_threadpool(compute_file_fingerprint, resolved_path)
    except MediaValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MediaSelectionCancelled as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    media_id = f"src_{uuid.uuid4().hex[:8]}"
    meta_dict = metadata.model_dump(mode="json")
    entry = {
        "media_id": media_id,
        "path": str(resolved_path),
        "fingerprint": fingerprint,
        "metadata": meta_dict,
    }
    request.app.state.media_registry[media_id] = entry
    _get_usage_logger(request).log_event(
        source="backend",
        category="action_output",
        action="media.select",
        intention="Select and validate source gameplay media",
        output_data={
            "media_id": media_id,
            "path": str(resolved_path),
            "width": metadata.width,
            "height": metadata.height,
            "fps": metadata.fps,
            "duration": metadata.duration,
        },
    )
    return entry


@app.get("/api/projects")
def api_list_projects(request: Request) -> dict[str, list[dict[str, Any]]]:
    """Scan data_dir/projects for valid project.json files."""
    data_dir = _get_data_dir(request)
    projects_root = data_dir / "projects"
    items: list[dict[str, Any]] = []
    if projects_root.is_dir():
        for entry in projects_root.iterdir():
            if not entry.is_dir():
                continue
            try:
                proj = load_project(entry.name, data_dir=data_dir)
            except Exception:
                continue
            items.append(
                {
                    "project_id": proj.project_id,
                    "title": proj.title,
                    "revision": proj.revision,
                    "updated_at": proj.updated_at,
                    "segment_count": len(proj.segments),
                    "clip_count": sum(len(t.clips) for t in proj.tracks),
                }
            )
    items.sort(key=lambda p: p["updated_at"], reverse=True)
    return {"projects": items}


@app.post("/api/projects")
async def api_create_project(request: Request) -> JSONResponse:
    """Create a new project from a registered media_id and enqueue proxy generation (202)."""
    data_dir = _get_data_dir(request)
    job_manager = _get_job_manager(request)

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")

    title = payload.get("title")
    media_id = payload.get("media_id")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=422, detail="'title' must be a non-empty string")
    if not isinstance(media_id, str) or not media_id.strip():
        raise HTTPException(status_code=422, detail="'media_id' must be a non-empty string")

    media_entry = request.app.state.media_registry.get(media_id)
    if media_entry is None:
        raise HTTPException(status_code=404, detail=f"Registered media_id {media_id!r} not found")

    project_id = payload.get("project_id") or f"proj_{uuid.uuid4().hex[:8]}"
    if not isinstance(project_id, str):
        raise HTTPException(status_code=422, detail="'project_id' must be a string")

    voice = payload.get("voice") or "bf_emma"
    language = payload.get("language") or "en-gb"
    copy_source = bool(payload.get("copy_source", payload.get("copy_to_project", False)))

    try:
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except StorageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    src_path = Path(media_entry["path"])
    final_src_path = src_path
    fingerprint = media_entry["fingerprint"]
    copied = False

    try:
        src_size = src_path.stat().st_size if src_path.is_file() else 0
        proxy_headroom = min(max(src_size // 4, 256 * 1024 * 1024), 1024 * 1024 * 1024)
        required_bytes = proxy_headroom + (src_size if copy_source else 0)
        ensure_free_disk_space(project_dir, required_bytes)
    except InsufficientDiskSpaceError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc

    source_ref = SourceMediaRef(
        media_id=media_id,
        path=str(final_src_path),
        fingerprint=fingerprint,
        copied_to_project=copied,
    )

    try:
        project = create_project(
            project_id=project_id,
            title=title.strip(),
            source_media=source_ref,
            voice=str(voice),
            language=str(language),
            data_dir=data_dir,
        )
    except StorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc

    source_meta = SourceMetadata.model_validate(media_entry["metadata"])
    manifest_lock = _get_manifest_lock(request.app, project_id)
    with manifest_lock:
        manifest = load_manifest(project_id, data_dir=data_dir)
        manifest.source_metadata = source_meta
        manifest.media_metadata[media_id] = _asset_meta_from_source(source_meta)
        manifest.proxy = ProxyState(status="pending")
        manifest.media_proxies[media_id] = MediaProxyState(status="pending")
        save_manifest(manifest, data_dir=data_dir)

    proxy_output = project_dir / "artifacts" / "proxy.mp4"
    target_app = request.app

    def _proxy_worker(ctx: JobContext) -> dict[str, Any]:
        with manifest_lock:
            m = load_manifest(project_id, data_dir=data_dir)
            m.proxy = ProxyState(status="running")
            m.media_proxies[media_id] = MediaProxyState(status="running")
            save_manifest(m, data_dir=data_dir)

        active_src_path = final_src_path
        if copy_source:
            ctx.update_progress(2, "Copying source recording")
            source_dir = project_dir / "source"
            source_dir.mkdir(parents=True, exist_ok=True)
            dest_path = (source_dir / src_path.name).resolve()
            if dest_path != src_path.resolve():
                shutil.copy2(src_path, dest_path)
            active_src_path = dest_path
            copied_fp = compute_file_fingerprint(active_src_path)
            cur_proj = load_project(project_id, data_dir=data_dir)
            updated_ref = SourceMediaRef(
                media_id=media_id,
                path=str(active_src_path),
                fingerprint=copied_fp,
                copied_to_project=True,
            )
            updated_proj = ensure_primary_media_registered(
                cur_proj.model_copy(update={"source_media": updated_ref})
            )
            save_project(
                updated_proj,
                expected_revision=cur_proj.revision,
                data_dir=data_dir,
            )

        selected_encoders: list[str] = []
        try:
            generate_proxy(
                source_path=active_src_path,
                output_path=proxy_output,
                project_id=None,
                source_duration=source_meta.duration,
                source_metadata=source_meta,
                data_dir=data_dir,
                on_progress=ctx.update_progress,
                on_process_start=ctx.register_process,
                on_encoder_selected=selected_encoders.append,
                cancel_event=ctx.cancel_event,
            )
        except JobCancelledError:
            raise
        except Exception as exc:
            with manifest_lock:
                m = load_manifest(project_id, data_dir=data_dir)
                m.proxy = ProxyState(status="failed", artifact_id=None, path=None, error=str(exc))
                m.media_proxies[media_id] = MediaProxyState(status="failed", artifact_id=None, path=None, error=str(exc))
                save_manifest(m, data_dir=data_dir)
            raise

        with manifest_lock:
            m = load_manifest(project_id, data_dir=data_dir)
            m.proxy = ProxyState(
                status="ready",
                artifact_id="art_proxy",
                path="artifacts/proxy.mp4",
                error=None,
            )
            m.media_proxies[media_id] = MediaProxyState(
                status="ready",
                artifact_id="art_proxy",
                path="artifacts/proxy.mp4",
                error=None,
            )
            save_manifest(m, data_dir=data_dir)

        _ = target_app
        return {
            "artifact_id": "art_proxy",
            "path": "artifacts/proxy.mp4",
            "encoder": selected_encoders[-1] if selected_encoders else "libx264",
        }

    job = job_manager.submit_job(project_id, "proxy", _proxy_worker)
    _get_usage_logger(request).log_event(
        source="backend",
        category="action_output",
        action="project.create",
        intention="Create new project",
        project_id=project_id,
        job_id=job.job_id,
        output_data={"project_id": project_id, "title": project.title, "job_id": job.job_id},
    )
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job.job_id,
            "project": project.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
        },
    )


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: str, request: Request) -> dict[str, Any]:
    """Load project.json, manifest.json, and source media status."""
    data_dir = _get_data_dir(request)
    try:
        project = load_project(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedSchemaVersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    project = ensure_primary_media_registered(project)
    project_dir = get_project_dir(project_id, data_dir=data_dir)
    manifest = load_manifest(project_id, data_dir=data_dir)
    if (
        manifest.source_metadata is not None
        and project.source_media.media_id not in manifest.media_metadata
    ):
        manifest.media_metadata[project.source_media.media_id] = _asset_meta_from_source(manifest.source_metadata)
    source_status = check_source_media(project.source_media)
    return {
        "project": project.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
        "source_status": source_status,
        "stale_tts_segments": _compute_stale_tts_segments(project, manifest, project_dir),
        "stale_tts_captions": _compute_stale_tts_captions(project, manifest, project_dir),
    }


@app.put("/api/projects/{project_id}")
async def api_save_project(project_id: str, request: Request) -> dict[str, Any]:
    """Validate and atomically persist ProjectInput with optimistic revision locking."""
    data_dir = _get_data_dir(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")

    expected_rev_param = request.query_params.get("expected_revision")
    if "project" in payload and isinstance(payload["project"], dict):
        project_dict = dict(payload["project"])
        raw_expected = payload.get("expected_revision", expected_rev_param)
    else:
        project_dict = dict(payload)
        raw_expected = project_dict.pop("expected_revision", expected_rev_param)

    if raw_expected is None or isinstance(raw_expected, bool):
        raise HTTPException(status_code=422, detail="expected_revision is required")
    try:
        expected_revision = int(raw_expected)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="expected_revision must be an integer") from exc

    try:
        project = ProjectInput.model_validate(project_dict)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc

    if project.project_id != project_id:
        raise HTTPException(status_code=422, detail="project_id in body does not match URL path")

    project = ensure_primary_media_registered(project)

    try:
        manifest = load_manifest(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if manifest.source_metadata is not None:
        src_w = manifest.source_metadata.width
        src_h = manifest.source_metadata.height
        for seg in project.segments:
            for idx, kf in enumerate(seg.crop_keyframes):
                try:
                    ideal_w, ideal_h = compute_ideal_crop_size(
                        src_w,
                        src_h,
                        kf.zoom,
                        output_width=project.output_settings.width,
                        output_height=project.output_settings.height,
                    )
                except GeometryValidationError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                max_x_norm = max(0.0, (float(src_w) - ideal_w) / float(src_w))
                max_y_norm = max(0.0, (float(src_h) - ideal_h) / float(src_h))
                if kf.x < -COORD_EPSILON or kf.x > max_x_norm + COORD_EPSILON:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Segment {seg.id!r} keyframe {idx} x={kf.x} out of bounds "
                            f"[0.0, {max_x_norm:.6f}] for zoom={kf.zoom}"
                        ),
                    )
                if kf.y < -COORD_EPSILON or kf.y > max_y_norm + COORD_EPSILON:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Segment {seg.id!r} keyframe {idx} y={kf.y} out of bounds "
                            f"[0.0, {max_y_norm:.6f}] for zoom={kf.zoom}"
                        ),
                    )
        for idx, kf in enumerate(project.camera_keyframes):
            try:
                ideal_w, ideal_h = compute_ideal_crop_size(
                    src_w,
                    src_h,
                    kf.zoom,
                    output_width=project.output_settings.width,
                    output_height=project.output_settings.height,
                )
            except GeometryValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            max_x_norm = max(0.0, (float(src_w) - ideal_w) / float(src_w))
            max_y_norm = max(0.0, (float(src_h) - ideal_h) / float(src_h))
            if kf.x < -COORD_EPSILON or kf.x > max_x_norm + COORD_EPSILON:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Camera keyframe {idx} x={kf.x} out of bounds "
                        f"[0.0, {max_x_norm:.6f}] for zoom={kf.zoom}"
                    ),
                )
            if kf.y < -COORD_EPSILON or kf.y > max_y_norm + COORD_EPSILON:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Camera keyframe {idx} y={kf.y} out of bounds "
                        f"[0.0, {max_y_norm:.6f}] for zoom={kf.zoom}"
                    ),
                )


    try:
        saved = save_project(project, expected_revision=expected_revision, data_dir=data_dir)
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedSchemaVersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    project_dir = get_project_dir(project_id, data_dir=data_dir)
    manifest = load_manifest(project_id, data_dir=data_dir)
    _get_usage_logger(request).log_event(
        source="backend",
        category="action_output",
        action="project.save",
        intention="Save project changes",
        project_id=project_id,
        output_data={
            "revision": saved.revision,
            "segment_count": len(saved.segments),
            "track_count": len(saved.tracks),
            "output_resolution": f"{saved.output_settings.width}x{saved.output_settings.height}",
        },
    )
    return {
        "project": saved.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
        "stale_tts_segments": _compute_stale_tts_segments(saved, manifest, project_dir),
        "stale_tts_captions": _compute_stale_tts_captions(saved, manifest, project_dir),
    }


@app.post("/api/projects/{project_id}/source/relink")
async def api_relink_source(project_id: str, request: Request) -> dict[str, Any]:
    """Validate and rebind a project's source recording."""
    data_dir = _get_data_dir(request)
    try:
        load_project(project_id, data_dir=data_dir)
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedSchemaVersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    content_type = request.headers.get("content-type", "")
    raw_body = await request.body()
    copy_to_project = False

    try:
        if "multipart/form-data" in content_type.lower():
            filename, file_bytes = _parse_multipart_file(content_type, raw_body)
            media_path = await run_in_threadpool(import_uploaded_media, filename, file_bytes, project_dir / "source")
        elif raw_body.strip():
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=422, detail="Request body must be a JSON object")
            copy_to_project = bool(payload.get("copy_to_project", False))
            if payload.get("path") is not None:
                media_path = _validate_manual_media_path(payload["path"])
            else:
                selected = await run_in_threadpool(select_media_native)
                if selected is None:
                    raise MediaSelectionCancelled("Media selection cancelled by user")
                media_path = selected
        else:
            selected = await run_in_threadpool(select_media_native)
            if selected is None:
                raise MediaSelectionCancelled("Media selection cancelled by user")
            media_path = selected

        source_meta = await run_in_threadpool(probe_and_validate_media, media_path, data_dir=data_dir)
    except MediaValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MediaSelectionCancelled as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    new_media_id = f"src_{uuid.uuid4().hex[:8]}"
    updated_project = await run_in_threadpool(
        relink_source_media,
        project_id=project_id,
        new_path=media_path,
        media_id=new_media_id,
        copy_to_project=copy_to_project,
        data_dir=data_dir,
    )

    lock = _get_manifest_lock(request.app, project_id)
    with lock:
        manifest = load_manifest(project_id, data_dir=data_dir)
        manifest.source_metadata = source_meta
        manifest.media_metadata[new_media_id] = _asset_meta_from_source(source_meta)
        save_manifest(manifest, data_dir=data_dir)

    source_status = check_source_media(updated_project.source_media)
    _get_usage_logger(request).log_event(
        source="backend",
        category="action_output",
        action="source.relink",
        intention="Relink source media",
        project_id=project_id,
        output_data={
            "source_status": source_status.get("status"),
            "path": updated_project.source_media.path,
            "copied_to_project": updated_project.source_media.copied_to_project,
        },
    )
    return {
        "project": updated_project.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
        "source_status": source_status,
        "stale_tts_segments": _compute_stale_tts_segments(updated_project, manifest, project_dir),
        "stale_tts_captions": _compute_stale_tts_captions(updated_project, manifest, project_dir),
    }


@app.post("/api/projects/{project_id}/assets")
async def api_import_project_asset(project_id: str, request: Request) -> JSONResponse:
    """Import an additional video, image (with alpha), or audio asset into the project Media Bin."""
    data_dir = _get_data_dir(request)
    job_manager = _get_job_manager(request)
    try:
        load_project(project_id, data_dir=data_dir)
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedSchemaVersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    content_type = request.headers.get("content-type", "")
    raw_body = await request.body()
    expected_kind: str | None = None
    asset_name: str | None = None
    copy_to_project = False
    temp_upload_path: Path | None = None

    try:
        if "multipart/form-data" in content_type.lower():
            filename, file_bytes, fields = _parse_multipart_upload(content_type, raw_body)
            raw_kind = fields.get("kind") or None
            if raw_kind in ("video", "image", "audio"):
                expected_kind = raw_kind
            elif raw_kind is not None:
                raise MediaValidationError(f"Unsupported media kind: {raw_kind!r}")
            asset_name = fields.get("name") or None
            temp_upload_path = await run_in_threadpool(
                import_uploaded_media, filename, file_bytes, data_dir / "uploads"
            )
            media_path = temp_upload_path
            copy_to_project = True
        elif raw_body.strip():
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=422, detail="Request body must be a JSON object")
            raw_kind = payload.get("kind")
            if raw_kind is not None:
                if raw_kind not in ("video", "image", "audio"):
                    raise MediaValidationError(f"Unsupported media kind: {raw_kind!r}")
                expected_kind = raw_kind
            if payload.get("name") is not None:
                asset_name = str(payload["name"]).strip() or None
            copy_to_project = bool(payload.get("copy_to_project", False))
            if payload.get("path") is not None:
                media_path = _validate_manual_media_path(payload["path"])
            else:
                selected = await run_in_threadpool(select_media_native, "Select Media Asset")
                if selected is None:
                    raise MediaSelectionCancelled("Media selection cancelled by user")
                media_path = selected
        else:
            selected = await run_in_threadpool(select_media_native, "Select Media Asset")
            if selected is None:
                raise MediaSelectionCancelled("Media selection cancelled by user")
            media_path = selected

        asset_id = f"asset_{uuid.uuid4().hex[:8]}"
        asset_ref, asset_meta = await run_in_threadpool(
            register_project_asset,
            media_path,
            expected_kind=expected_kind,  # type: ignore[arg-type]
            asset_id=asset_id,
            name=asset_name,
            copy_to_dir=(project_dir / "source") if copy_to_project else None,
            data_dir=data_dir,
        )
    except MediaValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MediaSelectionCancelled as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temp_upload_path is not None:
            temp_upload_path.unlink(missing_ok=True)

    # Persist asset_ref to project.json with retry on concurrent revision update
    saved_project: ProjectInput | None = None
    for _attempt in range(3):
        cur_proj = ensure_primary_media_registered(load_project(project_id, data_dir=data_dir))
        new_assets = dict(cur_proj.media_assets)
        new_assets[asset_ref.asset_id] = asset_ref
        try:
            saved_project = save_project(
                cur_proj.model_copy(update={"media_assets": new_assets}),
                expected_revision=cur_proj.revision,
                data_dir=data_dir,
            )
            break
        except RevisionConflictError:
            continue
    if saved_project is None:
        raise HTTPException(status_code=409, detail="Concurrent project save conflict while registering asset")

    manifest_lock = _get_manifest_lock(request.app, project_id)
    with manifest_lock:
        manifest = load_manifest(project_id, data_dir=data_dir)
        manifest.media_metadata[asset_ref.asset_id] = asset_meta
        if asset_ref.kind == "video":
            manifest.media_proxies[asset_ref.asset_id] = MediaProxyState(status="pending")
        save_manifest(manifest, data_dir=data_dir)

    job_id: str | None = None
    if asset_ref.kind == "video":
        proxy_rel = f"artifacts/proxy_{asset_ref.asset_id}.mp4"
        proxy_out = project_dir / proxy_rel
        src_for_proxy = SourceMetadata(
            width=int(asset_meta.width or 2),
            height=int(asset_meta.height or 2),
            fps=float(asset_meta.fps or 30.0),
            duration=float(asset_meta.duration),
            has_audio=bool(asset_meta.has_audio),
            codec_name=asset_meta.codec_name,
            pix_fmt=asset_meta.pix_fmt,
            audio_sample_rate=asset_meta.audio_sample_rate,
            audio_channels=asset_meta.audio_channels,
        )

        def _asset_proxy_worker(ctx: JobContext) -> dict[str, Any]:
            with manifest_lock:
                m = load_manifest(project_id, data_dir=data_dir)
                m.media_proxies[asset_ref.asset_id] = MediaProxyState(status="running")
                save_manifest(m, data_dir=data_dir)
            selected_encoders: list[str] = []
            try:
                generate_proxy(
                    source_path=Path(asset_ref.path),
                    output_path=proxy_out,
                    project_id=None,
                    asset_id=asset_ref.asset_id,
                    source_duration=asset_meta.duration,
                    source_metadata=src_for_proxy,
                    data_dir=data_dir,
                    on_progress=ctx.update_progress,
                    on_process_start=ctx.register_process,
                    on_encoder_selected=selected_encoders.append,
                    cancel_event=ctx.cancel_event,
                )
            except JobCancelledError:
                raise
            except Exception as exc:
                with manifest_lock:
                    m = load_manifest(project_id, data_dir=data_dir)
                    m.media_proxies[asset_ref.asset_id] = MediaProxyState(
                        status="failed", artifact_id=None, path=None, error=str(exc)
                    )
                    save_manifest(m, data_dir=data_dir)
                raise

            art_id = f"art_proxy_{asset_ref.asset_id}"
            with manifest_lock:
                m = load_manifest(project_id, data_dir=data_dir)
                m.media_proxies[asset_ref.asset_id] = MediaProxyState(
                    status="ready",
                    artifact_id=art_id,
                    path=proxy_rel,
                    error=None,
                )
                save_manifest(m, data_dir=data_dir)
            return {
                "asset_id": asset_ref.asset_id,
                "artifact_id": art_id,
                "path": proxy_rel,
                "encoder": selected_encoders[-1] if selected_encoders else "libx264",
            }

        job = job_manager.submit_job(project_id, "proxy", _asset_proxy_worker)
        job_id = job.job_id

    source_status = check_source_media(saved_project.source_media)
    _get_usage_logger(request).log_event(
        source="backend",
        category="action_output",
        action="asset.import",
        intention="Import project media asset",
        project_id=project_id,
        job_id=job_id,
        output_data={
            "asset_id": asset_ref.asset_id,
            "kind": asset_ref.kind,
            "name": asset_ref.name,
        },
    )
    return JSONResponse(
        status_code=202 if job_id is not None else 201,
        content={
            "asset": asset_ref.model_dump(mode="json"),
            "metadata": asset_meta.model_dump(mode="json"),
            "job_id": job_id,
            "project": saved_project.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
            "source_status": source_status,
            "stale_tts_segments": _compute_stale_tts_segments(saved_project, manifest, project_dir),
            "stale_tts_captions": _compute_stale_tts_captions(saved_project, manifest, project_dir),
        },
    )


@app.delete("/api/projects/{project_id}/assets/{asset_id}")
def api_delete_project_asset(project_id: str, asset_id: str, request: Request) -> dict[str, Any]:
    """Delete an unreferenced secondary media asset from the project Media Bin."""
    data_dir = _get_data_dir(request)
    try:
        project = ensure_primary_media_registered(load_project(project_id, data_dir=data_dir))
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedSchemaVersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if asset_id == project.source_media.media_id:
        raise HTTPException(status_code=409, detail="Cannot remove primary project source video")
    if asset_id not in project.media_assets:
        raise HTTPException(status_code=404, detail=f"Asset {asset_id!r} not found")

    for track in project.tracks:
        if isinstance(track, (VideoTrack, AudioTrack)):
            for clip in track.clips:
                if clip.asset_id == asset_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Asset {asset_id!r} is referenced by clip {clip.id!r} on track {track.name!r}",
                    )

    updated_assets = dict(project.media_assets)
    removed_ref = updated_assets.pop(asset_id)
    saved_project = save_project(
        project.model_copy(update={"media_assets": updated_assets}),
        expected_revision=project.revision,
        data_dir=data_dir,
    )

    manifest_lock = _get_manifest_lock(request.app, project_id)
    with manifest_lock:
        manifest = load_manifest(project_id, data_dir=data_dir)
        manifest.media_metadata.pop(asset_id, None)
        pstate = manifest.media_proxies.pop(asset_id, None)
        save_manifest(manifest, data_dir=data_dir)

    if pstate is not None and pstate.path:
        try:
            proxy_abs = _validate_relative_artifact_path(project_dir, pstate.path)
            proxy_abs.unlink(missing_ok=True)
        except Exception:
            pass
    if removed_ref.copied_to_project:
        try:
            src_abs = Path(removed_ref.path).resolve()
            if src_abs.is_relative_to(project_dir.resolve()):
                src_abs.unlink(missing_ok=True)
        except Exception:
            pass

    source_status = check_source_media(saved_project.source_media)
    return {
        "project": saved_project.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
        "source_status": source_status,
        "stale_tts_segments": _compute_stale_tts_segments(saved_project, manifest, project_dir),
        "stale_tts_captions": _compute_stale_tts_captions(saved_project, manifest, project_dir),
    }


@app.post("/api/projects/{project_id}/tts")
async def api_project_tts(project_id: str, request: Request) -> JSONResponse:
    """Enqueue CPU Kokoro TTS synthesis for independent CaptionClips across all CaptionTracks (202)."""
    data_dir = _get_data_dir(request)
    job_manager = _get_job_manager(request)

    try:
        project = load_project(project_id, data_dir=data_dir)
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedSchemaVersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raw_body = await request.body()
    requested_ids: list[str] | None = None
    if raw_body.strip():
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Request body must be a JSON object")
        raw_ids = payload.get("clip_ids")
        if raw_ids is not None:
            if not isinstance(raw_ids, list) or not all(isinstance(cid, str) and cid for cid in raw_ids):
                raise HTTPException(status_code=422, detail="'clip_ids' must be a list of non-empty strings")
            requested_ids = list(raw_ids)

    all_caption_ids = [
        c.id
        for tr in project.tracks
        if isinstance(tr, CaptionTrack)
        for c in tr.clips
    ]
    if requested_ids is not None:
        missing = [cid for cid in requested_ids if cid not in set(all_caption_ids)]
        if missing:
            raise HTTPException(status_code=404, detail=f"CaptionClip(s) not found: {missing!r}")
        target_ids = requested_ids
    else:
        manifest_now = load_manifest(project_id, data_dir=data_dir)
        stale_ids = _compute_stale_tts_captions(project, manifest_now, project_dir)
        target_ids = stale_ids if stale_ids else all_caption_ids

    project_snap = project.model_copy(deep=True)
    target_app = request.app

    def _project_tts_worker(ctx: JobContext) -> dict[str, Any]:
        manifest_snap = load_manifest(project_id, data_dir=data_dir)
        total = max(1, len(target_ids))
        synthesized: list[str] = []
        for idx, cid in enumerate(target_ids):
            ctx.check_cancelled()
            ctx.update_progress(int((idx / total) * 90) + 5, f"Synthesizing caption {idx + 1}/{total}")
            _ensure_caption_tts(
                target_app=target_app,
                project=project_snap,
                clip_id=cid,
                project_dir=project_dir,
                data_dir=data_dir,
                manifest=manifest_snap,
            )
            synthesized.append(cid)
        ctx.update_progress(100, "Captions narration ready")
        return {
            "synthesized_clip_ids": synthesized,
            "caption_artifacts": {
                k: v.model_dump(mode="json") for k, v in manifest_snap.caption_artifacts.items()
            },
        }

    job = job_manager.submit_job(project_id, "tts", _project_tts_worker)
    return JSONResponse(status_code=202, content={"job_id": job.job_id, "target_clip_ids": target_ids})


@app.post("/api/projects/{project_id}/segments/{seg_id}/tts")
def api_segment_tts(project_id: str, seg_id: str, request: Request) -> JSONResponse:
    """Enqueue CPU Kokoro TTS synthesis for a segment (202)."""
    data_dir = _get_data_dir(request)
    job_manager = _get_job_manager(request)

    try:
        project = load_project(project_id, data_dir=data_dir)
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedSchemaVersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    segment = next((s for s in project.segments if s.id == seg_id), None)
    if segment is None:
        raise HTTPException(status_code=404, detail=f"Segment {seg_id!r} not found")

    project_snap = project.model_copy(deep=True)
    target_app = request.app

    def _tts_worker(ctx: JobContext) -> dict[str, Any]:
        ctx.update_progress(10, "Synthesizing narration")
        manifest_snap = load_manifest(project_id, data_dir=data_dir)
        seg_state = _ensure_segment_tts(
            target_app=target_app,
            project=project_snap,
            seg_id=seg_id,
            project_dir=project_dir,
            data_dir=data_dir,
            manifest=manifest_snap,
        )
        ctx.update_progress(100, "Narration ready")
        return {
            "tts_hash": seg_state.tts_hash,
            "audio_artifact_id": seg_state.audio_artifact_id,
            "audio_duration": seg_state.audio_duration,
        }

    job = job_manager.submit_job(project_id, "tts", _tts_worker, seg_id=seg_id)
    return JSONResponse(status_code=202, content={"job_id": job.job_id})


@app.post("/api/projects/{project_id}/segments/{seg_id}/preview")
def api_segment_preview(project_id: str, seg_id: str, request: Request) -> JSONResponse:
    """Enqueue single-segment 1080x1920 captioned preview render (202)."""
    data_dir = _get_data_dir(request)
    job_manager = _get_job_manager(request)

    try:
        project = load_project(project_id, data_dir=data_dir)
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedSchemaVersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    segment = next((s for s in project.segments if s.id == seg_id), None)
    if segment is None:
        raise HTTPException(status_code=404, detail=f"Segment {seg_id!r} not found")

    project_snap = project.model_copy(deep=True)
    segment_snap = segment.model_copy(deep=True)
    manifest_snap = load_manifest(project_id, data_dir=data_dir).model_copy(deep=True)
    target_app = request.app

    def _preview_worker(ctx: JobContext) -> dict[str, Any]:
        ctx.update_progress(5, "Preparing segment preview")
        src_meta = manifest_snap.source_metadata
        if src_meta is None:
            src_meta = probe_and_validate_media(project_snap.source_media.path, data_dir=data_dir)

        ctx.update_progress(20, "Checking narration audio")
        seg_state = _ensure_segment_tts(
            target_app=target_app,
            project=project_snap,
            seg_id=seg_id,
            project_dir=project_dir,
            data_dir=data_dir,
            manifest=manifest_snap,
        )

        audio_path: Path | None = None
        if seg_state.audio_path:
            audio_path = project_dir / seg_state.audio_path

        ctx.update_progress(45, "Rendering segment preview")
        try:
            preview_path = render_segment_preview(
                segment=segment_snap,
                source_media=project_snap.source_media,
                source_metadata=src_meta,
                output_settings=project_snap.output_settings,
                caption_settings=project_snap.caption_settings,
                project_dir=project_dir,
                tts_hash=seg_state.tts_hash or "",
                audio_path=audio_path,
                audio_duration=seg_state.audio_duration,
                clause_timings=seg_state.clause_timings,
                cancel_event=ctx.cancel_event,
                on_process_start=ctx.register_process,
            )
        except RenderCancelledError as exc:
            raise JobCancelledError(str(exc)) from exc

        ctx.check_cancelled()
        render_hash = compute_render_hash(
            segment=segment_snap,
            source_media=project_snap.source_media,
            tts_hash=seg_state.tts_hash or "",
            output_settings=project_snap.output_settings,
        )
        stage1_path = project_dir / "artifacts" / f"seg_{seg_id}_{render_hash}.mp4"
        preview_rel = preview_path.relative_to(project_dir).as_posix()
        preview_art_id = f"art_preview_{seg_id}"

        lock = _get_manifest_lock(target_app, project_id)
        with lock:
            latest_manifest = load_manifest(project_id, data_dir=data_dir)
            s_state = latest_manifest.segments.get(seg_id, SegmentArtifactState())
            s_state.render_hash = render_hash
            if stage1_path.is_file():
                s_state.segment_artifact_id = f"art_seg_{seg_id}"
                s_state.rendered_segment_path = stage1_path.relative_to(project_dir).as_posix()
            s_state.preview_artifact_id = preview_art_id
            s_state.preview_path = preview_rel
            latest_manifest.segments[seg_id] = s_state
            save_manifest(latest_manifest, data_dir=data_dir)

        ctx.update_progress(100, "Preview ready")
        return {"preview_artifact_id": preview_art_id, "preview_path": preview_rel}

    job = job_manager.submit_job(project_id, "preview", _preview_worker, seg_id=seg_id)
    return JSONResponse(status_code=202, content={"job_id": job.job_id})


@app.post("/api/projects/{project_id}/preview")
def api_project_preview(project_id: str, request: Request) -> JSONResponse:
    """Enqueue full-project captioned preview render (202)."""
    data_dir = _get_data_dir(request)
    job_manager = _get_job_manager(request)

    try:
        project = load_project(project_id, data_dir=data_dir)
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedSchemaVersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    manifest_snap = load_manifest(project_id, data_dir=data_dir).model_copy(deep=True)
    is_mt = _is_multitrack_project(project)
    if is_mt:
        raw_dur, _fc, _fd = get_project_timeline_duration(project, manifest_snap)
        if raw_dur <= 0.0:
            raise HTTPException(
                status_code=422,
                detail="Timeline has no clips, captions, or camera keyframes to preview",
            )
    elif not project.segments:
        raise HTTPException(status_code=422, detail="Project has no segments to preview")

    project_snap = project.model_copy(deep=True)
    target_app = request.app

    def _project_preview_worker(ctx: JobContext) -> dict[str, Any]:
        ctx.update_progress(5, "Preparing project preview")
        src_meta = manifest_snap.source_metadata

        if is_mt:
            cap_ids = [
                c.id
                for tr in project_snap.tracks
                if isinstance(tr, CaptionTrack)
                for c in tr.clips
            ]
            for idx, cid in enumerate(cap_ids):
                ctx.check_cancelled()
                pct = 10 + int((idx / max(1, len(cap_ids))) * 45)
                ctx.update_progress(pct, f"Checking caption audio {idx + 1}/{len(cap_ids)}")
                _ensure_caption_tts(
                    target_app=target_app,
                    project=project_snap,
                    clip_id=cid,
                    project_dir=project_dir,
                    data_dir=data_dir,
                    manifest=manifest_snap,
                )
        else:
            if src_meta is None:
                src_meta = probe_and_validate_media(project_snap.source_media.path, data_dir=data_dir)

            total_segs = len(project_snap.segments)
            offset_sec = 0.0
            for idx, seg in enumerate(project_snap.segments):
                ctx.check_cancelled()
                pct = 10 + int((idx / max(1, total_segs)) * 55)
                ctx.update_progress(pct, f"Preparing segment {idx + 1}/{total_segs}")

                seg_state = _ensure_segment_tts(
                    target_app=target_app,
                    project=project_snap,
                    seg_id=seg.id,
                    project_dir=project_dir,
                    data_dir=data_dir,
                    manifest=manifest_snap,
                )
                plan = plan_segment_duration(
                    segment=seg,
                    audio_duration=seg_state.audio_duration,
                    source_duration=src_meta.duration,
                    tail_padding_sec=project_snap.output_settings.tail_padding_sec,
                    fps=project_snap.output_settings.fps,
                )
                render_seg = seg
                if project_snap.camera_keyframes:
                    sliced_kfs = slice_camera_keyframes_for_segment(
                        project_snap.camera_keyframes,
                        clip_start=offset_sec,
                        clip_dur=plan.final_dur,
                        width=src_meta.width,
                        height=src_meta.height,
                        output_width=project_snap.output_settings.width,
                        output_height=project_snap.output_settings.height,
                    )
                    render_seg = seg.model_copy(update={"crop_keyframes": sliced_kfs})
                offset_sec += plan.final_dur

                audio_path = (project_dir / seg_state.audio_path) if seg_state.audio_path else None
                try:
                    seg_mp4 = render_segment_stage1(
                        segment=render_seg,
                        source_media=project_snap.source_media,
                        source_metadata=src_meta,
                        output_settings=project_snap.output_settings,
                        project_dir=project_dir,
                        tts_hash=seg_state.tts_hash or "",
                        audio_path=audio_path,
                        audio_duration=seg_state.audio_duration,
                        cancel_event=ctx.cancel_event,
                        on_process_start=ctx.register_process,
                    )
                except RenderCancelledError as exc:
                    raise JobCancelledError(str(exc)) from exc

                render_hash = compute_render_hash(
                    segment=render_seg,
                    source_media=project_snap.source_media,
                    tts_hash=seg_state.tts_hash or "",
                    output_settings=project_snap.output_settings,
                )
                seg_rel = seg_mp4.relative_to(project_dir).as_posix()
                lock = _get_manifest_lock(target_app, project_id)
                with lock:
                    latest_manifest = load_manifest(project_id, data_dir=data_dir)
                    s_state = latest_manifest.segments.get(seg.id, SegmentArtifactState())
                    s_state.render_hash = render_hash
                    s_state.segment_artifact_id = f"art_seg_{seg.id}"
                    s_state.rendered_segment_path = seg_rel
                    latest_manifest.segments[seg.id] = s_state
                    save_manifest(latest_manifest, data_dir=data_dir)
                manifest_snap.segments[seg.id] = s_state.model_copy(deep=True)

        ctx.update_progress(70, "Rendering project preview")
        try:
            preview_path = render_project_preview(
                project=project_snap,
                source_metadata=src_meta,
                manifest=manifest_snap,
                project_dir=project_dir,
                cancel_event=ctx.cancel_event,
                on_process_start=ctx.register_process,
            )
        except RenderCancelledError as exc:
            raise JobCancelledError(str(exc)) from exc
        except RenderError as exc:
            raise RuntimeError(str(exc)) from exc

        ctx.check_cancelled()
        preview_rel = preview_path.relative_to(project_dir).as_posix()
        preview_art_id = "art_preview_latest"
        lock = _get_manifest_lock(target_app, project_id)
        with lock:
            latest_manifest = load_manifest(project_id, data_dir=data_dir)
            latest_manifest.latest_preview_artifact_id = preview_art_id
            latest_manifest.latest_preview_path = preview_rel
            save_manifest(latest_manifest, data_dir=data_dir)

        ctx.update_progress(100, "Preview ready")
        return {"preview_artifact_id": preview_art_id, "preview_path": preview_rel}

    job = job_manager.submit_job(project_id, "preview", _project_preview_worker)
    return JSONResponse(status_code=202, content={"job_id": job.job_id})


@app.post("/api/projects/{project_id}/export")
async def api_project_export(project_id: str, request: Request) -> JSONResponse:
    """Enqueue immutable project snapshot render + concat + .ass burn-in (202)."""
    data_dir = _get_data_dir(request)
    job_manager = _get_job_manager(request)

    export_opts = ExportOptions()
    raw_body = await request.body()
    if raw_body and raw_body.strip():
        try:
            export_opts = ExportOptions.model_validate(json.loads(raw_body.decode("utf-8")))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid export options: {exc}") from exc

    quality_crf_map = {"draft": 24, "standard": 19, "high": 16}
    export_crf = quality_crf_map.get(export_opts.quality, 19)
    export_fps_override = export_opts.fps

    try:
        project = load_project(project_id, data_dir=data_dir)
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedSchemaVersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    custom_export_path: Path | None = None
    if export_opts.filename and export_opts.filename.strip():
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", export_opts.filename.strip()).strip("._")
        if safe_stem:
            if not safe_stem.lower().endswith(".mp4"):
                safe_stem = f"{safe_stem}.mp4"
            custom_export_path = project_dir / "exports" / safe_stem

    manifest_snap = load_manifest(project_id, data_dir=data_dir).model_copy(deep=True)
    is_mt = _is_multitrack_project(project)
    if is_mt:
        raw_dur, _fc, _fd = get_project_timeline_duration(project, manifest_snap)
        if raw_dur <= 0.0:
            raise HTTPException(
                status_code=422,
                detail="Timeline has no clips, captions, or camera keyframes to export",
            )
    elif not project.segments:
        raise HTTPException(status_code=422, detail="Project has no segments to export")

    project_snap = project.model_copy(deep=True)
    target_app = request.app

    def _export_worker(ctx: JobContext) -> dict[str, Any]:
        ctx.update_progress(5, "Preparing export")
        src_meta = manifest_snap.source_metadata

        if is_mt:
            cap_ids = [
                c.id
                for tr in project_snap.tracks
                if isinstance(tr, CaptionTrack)
                for c in tr.clips
            ]
            for idx, cid in enumerate(cap_ids):
                ctx.check_cancelled()
                pct = 10 + int((idx / max(1, len(cap_ids))) * 45)
                ctx.update_progress(pct, f"Checking caption audio {idx + 1}/{len(cap_ids)}")
                _ensure_caption_tts(
                    target_app=target_app,
                    project=project_snap,
                    clip_id=cid,
                    project_dir=project_dir,
                    data_dir=data_dir,
                    manifest=manifest_snap,
                )
        else:
            if src_meta is None:
                src_meta = probe_and_validate_media(project_snap.source_media.path, data_dir=data_dir)

            total_segs = len(project_snap.segments)
            offset_sec = 0.0
            for idx, seg in enumerate(project_snap.segments):
                ctx.check_cancelled()
                pct = 10 + int((idx / max(1, total_segs)) * 55)
                ctx.update_progress(pct, f"Preparing segment {idx + 1}/{total_segs}")

                seg_state = _ensure_segment_tts(
                    target_app=target_app,
                    project=project_snap,
                    seg_id=seg.id,
                    project_dir=project_dir,
                    data_dir=data_dir,
                    manifest=manifest_snap,
                )
                plan = plan_segment_duration(
                    segment=seg,
                    audio_duration=seg_state.audio_duration,
                    source_duration=src_meta.duration,
                    tail_padding_sec=project_snap.output_settings.tail_padding_sec,
                    fps=project_snap.output_settings.fps,
                )
                render_seg = seg
                if project_snap.camera_keyframes:
                    sliced_kfs = slice_camera_keyframes_for_segment(
                        project_snap.camera_keyframes,
                        clip_start=offset_sec,
                        clip_dur=plan.final_dur,
                        width=src_meta.width,
                        height=src_meta.height,
                        output_width=project_snap.output_settings.width,
                        output_height=project_snap.output_settings.height,
                    )
                    render_seg = seg.model_copy(update={"crop_keyframes": sliced_kfs})
                offset_sec += plan.final_dur

                audio_path = (project_dir / seg_state.audio_path) if seg_state.audio_path else None
                try:
                    seg_mp4 = render_segment_stage1(
                        segment=render_seg,
                        source_media=project_snap.source_media,
                        source_metadata=src_meta,
                        output_settings=project_snap.output_settings,
                        project_dir=project_dir,
                        tts_hash=seg_state.tts_hash or "",
                        audio_path=audio_path,
                        audio_duration=seg_state.audio_duration,
                        cancel_event=ctx.cancel_event,
                        on_process_start=ctx.register_process,
                    )
                except RenderCancelledError as exc:
                    raise JobCancelledError(str(exc)) from exc

                render_hash = compute_render_hash(
                    segment=render_seg,
                    source_media=project_snap.source_media,
                    tts_hash=seg_state.tts_hash or "",
                    output_settings=project_snap.output_settings,
                )
                seg_rel = seg_mp4.relative_to(project_dir).as_posix()
                lock = _get_manifest_lock(target_app, project_id)
                with lock:
                    latest_manifest = load_manifest(project_id, data_dir=data_dir)
                    s_state = latest_manifest.segments.get(seg.id, SegmentArtifactState())
                    s_state.render_hash = render_hash
                    s_state.segment_artifact_id = f"art_seg_{seg.id}"
                    s_state.rendered_segment_path = seg_rel
                    latest_manifest.segments[seg.id] = s_state
                    save_manifest(latest_manifest, data_dir=data_dir)
                manifest_snap.segments[seg.id] = s_state.model_copy(deep=True)

        ctx.update_progress(70, "Compositing and burning captions")
        try:
            export_path = render_final_export(
                project=project_snap,
                source_metadata=src_meta,
                manifest=manifest_snap,
                project_dir=project_dir,
                export_path=custom_export_path,
                cancel_event=ctx.cancel_event,
                on_process_start=ctx.register_process,
                crf=export_crf,
                fps_override=export_fps_override,
            )
        except RenderCancelledError as exc:
            raise JobCancelledError(str(exc)) from exc
        except RenderError as exc:
            raise RuntimeError(str(exc)) from exc

        ctx.check_cancelled()
        export_rel = export_path.relative_to(project_dir).as_posix()
        lock = _get_manifest_lock(target_app, project_id)
        with lock:
            latest_manifest = load_manifest(project_id, data_dir=data_dir)
            latest_manifest.latest_export_artifact_id = "art_export_latest"
            latest_manifest.latest_export_path = export_rel
            save_manifest(latest_manifest, data_dir=data_dir)

        ctx.update_progress(100, "Export complete")
        return {"export_artifact_id": "art_export_latest", "export_path": export_rel}

    job = job_manager.submit_job(project_id, "export", _export_worker)
    return JSONResponse(status_code=202, content={"job_id": job.job_id})


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str, request: Request) -> dict[str, Any]:
    """Return JobState for a background job."""
    job_manager = _get_job_manager(request)
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job.model_dump(mode="json")


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    """Request cancellation of a background job."""
    job_manager = _get_job_manager(request)
    job = job_manager.cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job.model_dump(mode="json")


@app.get("/api/projects/{project_id}/media/{artifact_id}")
def api_stream_media(project_id: str, artifact_id: str, request: Request) -> Response:
    """Stream a registered project artifact with HTTP 206 Range and 416 handling (F38)."""
    data_dir = _get_data_dir(request)
    try:
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Project {project_id!r} not found")

    try:
        project = load_project(project_id, data_dir=data_dir)
    except Exception:
        project = None
    manifest = load_manifest(project_id, data_dir=data_dir)
    target_path = _resolve_artifact_path(manifest, project_dir, artifact_id, project=project)
    if target_path is None or not target_path.is_file():
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id!r} not found")

    file_size = target_path.stat().st_size
    media_type = _media_content_type(target_path)
    range_header = request.headers.get("range")

    if not range_header:
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        }
        return StreamingResponse(
            _iter_file_range(target_path, 0, max(0, file_size - 1)) if file_size > 0 else iter(()),
            status_code=200,
            media_type=media_type,
            headers=headers,
        )

    parsed_range = _parse_byte_range(range_header, file_size)
    if parsed_range is None:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    start, end = parsed_range
    chunk_len = end - start + 1
    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_len),
    }
    return StreamingResponse(
        _iter_file_range(target_path, start, end),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


class RenameProjectBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class DuplicateProjectBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)


def _project_bundle(
    project: ProjectInput,
    manifest: ArtifactManifest,
    project_dir: Path,
) -> dict[str, Any]:
    return {
        "project": project.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
        "source_status": check_source_media(project.source_media),
        "stale_tts_segments": _compute_stale_tts_segments(project, manifest, project_dir),
        "stale_tts_captions": _compute_stale_tts_captions(project, manifest, project_dir),
    }


@app.patch("/api/projects/{project_id}")
def api_rename_project(project_id: str, body: RenameProjectBody, request: Request) -> dict[str, Any]:
    """Rename a project's title atomically and return updated bundle."""
    data_dir = _get_data_dir(request)
    try:
        updated = rename_project(project_id, body.title, data_dir=data_dir)
        manifest = load_manifest(project_id, data_dir=data_dir)
        project_dir = get_project_dir(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _project_bundle(updated, manifest, project_dir)


@app.post("/api/projects/{project_id}/duplicate", status_code=201)
def api_duplicate_project(
    project_id: str,
    request: Request,
    body: DuplicateProjectBody | None = None,
) -> dict[str, Any]:
    """Duplicate an existing project into a new project ID and return its bundle."""
    data_dir = _get_data_dir(request)
    new_id = f"proj_{uuid.uuid4().hex[:10]}"
    try:
        new_proj, new_manifest = duplicate_project(
            project_id,
            new_project_id=new_id,
            new_title=body.title if body else None,
            data_dir=data_dir,
        )
        new_dir = get_project_dir(new_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _project_bundle(new_proj, new_manifest, new_dir)


@app.delete("/api/projects/{project_id}")
def api_delete_project(project_id: str, request: Request) -> dict[str, Any]:
    """Delete a project directory and all its local artifacts."""
    data_dir = _get_data_dir(request)
    job_manager = _get_job_manager(request)
    active_jobs = [
        j for j in job_manager.list_jobs(project_id=project_id)
        if j.status in ("queued", "running")
    ]
    if active_jobs:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete project while a background job is running; cancel it first.",
        )
    try:
        delete_project(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": True, "project_id": project_id}


@app.get("/api/projects/{project_id}/subtitles.srt")
def api_download_srt(project_id: str, request: Request) -> Response:
    """Export project captions as a downloadable UTF-8 SubRip (.srt) file."""
    data_dir = _get_data_dir(request)
    try:
        project = load_project(project_id, data_dir=data_dir)
        manifest = load_manifest(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if _is_multitrack_project(project):
        events = build_timeline_subtitle_events(project.tracks, manifest.caption_artifacts)
    else:
        events = []
        offset_sec = 0.0
        src_dur = manifest.source_metadata.duration if manifest.source_metadata else 0.0
        for seg in project.segments:
            seg_art = manifest.segments.get(seg.id)
            audio_dur = seg_art.audio_duration if seg_art else 0.0
            timings = seg_art.clause_timings if seg_art else []
            if timings and seg.clauses:
                events.extend(build_segment_subtitle_events(seg.clauses, timings, segment_offset_sec=offset_sec))
            plan = plan_segment_duration(
                segment=seg,
                audio_duration=audio_dur,
                source_duration=src_dur,
                tail_padding_sec=project.output_settings.tail_padding_sec,
                fps=project.output_settings.fps,
            )
            offset_sec += plan.final_dur

    srt_text = render_srt_content(events)
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", project.title.strip() or project_id).strip("._") or project_id
    return Response(
        content=srt_text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.srt"'},
    )


def _compute_waveform_peaks(media_path: Path, buckets: int, data_dir: Path) -> list[float]:
    """Extract normalized [0..1] audio peak amplitudes using ffmpeg mono s16le PCM."""
    buckets = max(16, min(400, int(buckets)))
    try:
        ffmpeg_bin, _ = verify_ffmpeg(data_dir=data_dir, offline=is_offline_env())
    except Exception:
        return [0.0] * buckets

    cmd = [
        str(ffmpeg_bin),
        "-v",
        "error",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "8000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20, check=False)
    except Exception:
        return [0.0] * buckets

    raw = proc.stdout
    if not raw or len(raw) < 2:
        return [0.0] * buckets

    samples = array.array("h")
    usable_len = len(raw) - (len(raw) % 2)
    samples.frombytes(raw[:usable_len])
    total = len(samples)
    if total == 0:
        return [0.0] * buckets

    peaks: list[float] = []
    for i in range(buckets):
        start = int((i * total) / buckets)
        end = max(start + 1, int(((i + 1) * total) / buckets))
        end = min(total, end)
        max_abs = 0
        # Subsample large windows for speed
        step = max(1, (end - start) // 256)
        for j in range(start, end, step):
            v = abs(samples[j])
            if v > max_abs:
                max_abs = v
        peaks.append(round(min(1.0, max_abs / 32768.0), 3))
    return peaks


@app.get("/api/projects/{project_id}/assets/{asset_id}/thumbnail")
def api_asset_thumbnail(project_id: str, asset_id: str, request: Request) -> Response:
    """Return a cached JPEG thumbnail for a video/image project asset."""
    data_dir = _get_data_dir(request)
    try:
        project_dir = get_project_dir(project_id, data_dir=data_dir)
        project = load_project(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    asset = project.media_assets.get(asset_id)
    if asset is None and asset_id == project.source_media.media_id:
        media_path = Path(project.source_media.path)
        kind = "video"
    elif asset is not None:
        media_path = Path(asset.path)
        kind = asset.kind
    else:
        raise HTTPException(status_code=404, detail=f"Asset {asset_id!r} not found")

    if not media_path.is_file():
        raise HTTPException(status_code=404, detail="Asset file missing on disk")

    if kind == "image":
        return FileResponse(media_path, media_type=_media_content_type(media_path))
    if kind != "video":
        raise HTTPException(status_code=400, detail="Thumbnails are only available for video/image assets")

    thumbs_dir = project_dir / "artifacts" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    file_sig = media_path.stat().st_mtime_ns
    safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{asset_id}_{file_sig}")
    thumb_path = thumbs_dir / f"{safe_id}.jpg"
    if not thumb_path.is_file():
        tmp_thumb = thumbs_dir / f"{safe_id}.{uuid.uuid4().hex[:6]}.tmp.jpg"
        try:
            ffmpeg_bin, _ = verify_ffmpeg(data_dir=data_dir, offline=is_offline_env())
            subprocess.run(
                [
                    str(ffmpeg_bin),
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    "0.2",
                    "-i",
                    str(media_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=320:-2",
                    "-q:v",
                    "6",
                    str(tmp_thumb),
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
            if not tmp_thumb.is_file() or tmp_thumb.stat().st_size == 0:
                # Retry at t=0 for very short clips
                subprocess.run(
                    [
                        str(ffmpeg_bin),
                        "-y",
                        "-v",
                        "error",
                        "-i",
                        str(media_path),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=320:-2",
                        "-q:v",
                        "6",
                        str(tmp_thumb),
                    ],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            if tmp_thumb.is_file() and tmp_thumb.stat().st_size > 0:
                tmp_thumb.replace(thumb_path)
        except Exception as exc:
            tmp_thumb.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"Thumbnail generation failed: {exc}") from exc
        finally:
            tmp_thumb.unlink(missing_ok=True)

    if not thumb_path.is_file():
        raise HTTPException(status_code=404, detail="Could not generate thumbnail")
    return FileResponse(thumb_path, media_type="image/jpeg")


@app.get("/api/projects/{project_id}/assets/{asset_id}/waveform")
def api_asset_waveform(
    project_id: str,
    asset_id: str,
    request: Request,
    buckets: int = 120,
) -> dict[str, Any]:
    """Return cached normalized audio peak waveform array for a project media asset."""
    data_dir = _get_data_dir(request)
    buckets = max(16, min(400, int(buckets)))
    try:
        project_dir = get_project_dir(project_id, data_dir=data_dir)
        project = load_project(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    asset = project.media_assets.get(asset_id)
    if asset is None and asset_id == project.source_media.media_id:
        media_path = Path(project.source_media.path)
    elif asset is not None:
        media_path = Path(asset.path)
    else:
        raise HTTPException(status_code=404, detail=f"Asset {asset_id!r} not found")

    if not media_path.is_file():
        return {"asset_id": asset_id, "peaks": [0.0] * buckets}

    wf_dir = project_dir / "artifacts" / "waveforms"
    wf_dir.mkdir(parents=True, exist_ok=True)
    file_sig = media_path.stat().st_mtime_ns
    safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{asset_id}_{file_sig}")
    cache_path = wf_dir / f"{safe_id}_{buckets}.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return {"asset_id": asset_id, "peaks": cached}
        except Exception:
            pass

    peaks = _compute_waveform_peaks(media_path, buckets=buckets, data_dir=data_dir)
    tmp_cache = wf_dir / f"{safe_id}_{buckets}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        tmp_cache.write_text(json.dumps(peaks), encoding="utf-8")
        tmp_cache.replace(cache_path)
    except Exception:
        tmp_cache.unlink(missing_ok=True)
    return {"asset_id": asset_id, "peaks": peaks}


@app.get("/api/projects/{project_id}/media/{artifact_id}/waveform")
def api_media_artifact_waveform(
    project_id: str,
    artifact_id: str,
    request: Request,
    buckets: int = 80,
) -> dict[str, Any]:
    """Return cached normalized audio peak waveform array for a synthesized TTS/media artifact."""
    data_dir = _get_data_dir(request)
    buckets = max(16, min(400, int(buckets)))
    try:
        project_dir = get_project_dir(project_id, data_dir=data_dir)
        project = load_project(project_id, data_dir=data_dir)
        manifest = load_manifest(project_id, data_dir=data_dir)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    target_path = _resolve_artifact_path(manifest, project_dir, artifact_id, project=project)
    if target_path is None or not target_path.is_file():
        return {"artifact_id": artifact_id, "peaks": [0.0] * buckets}

    wf_dir = project_dir / "artifacts" / "waveforms"
    wf_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{artifact_id}_{target_path.stat().st_mtime_ns}")
    cache_path = wf_dir / f"art_{safe_id}_{buckets}.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return {"artifact_id": artifact_id, "peaks": cached}
        except Exception:
            pass

    peaks = _compute_waveform_peaks(target_path, buckets=buckets, data_dir=data_dir)
    tmp_cache = wf_dir / f"art_{safe_id}_{buckets}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        tmp_cache.write_text(json.dumps(peaks), encoding="utf-8")
        tmp_cache.replace(cache_path)
    except Exception:
        tmp_cache.unlink(missing_ok=True)
    return {"artifact_id": artifact_id, "peaks": peaks}


@app.get("/api/pronunciations")
def api_get_pronunciations(request: Request) -> dict[str, dict[str, str]]:
    """Read global pronunciation dictionary."""
    data_dir = _get_data_dir(request)
    return {"pronunciations": load_global_pronunciations(data_dir=data_dir)}


@app.put("/api/pronunciations")
async def api_put_pronunciations(request: Request) -> dict[str, dict[str, str]]:
    """Update global pronunciation dictionary."""
    data_dir = _get_data_dir(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")

    raw_prons = payload.get("pronunciations") if "pronunciations" in payload and isinstance(payload["pronunciations"], dict) else payload
    if not isinstance(raw_prons, dict):
        raise HTTPException(status_code=422, detail="'pronunciations' must be a JSON object")

    cleaned = {str(k): str(v) for k, v in raw_prons.items() if str(k).strip()}
    saved = save_global_pronunciations(cleaned, data_dir=data_dir)
    _get_usage_logger(request).log_event(
        source="backend",
        category="action_output",
        action="pronunciations.save",
        intention="Save global pronunciation overrides",
        output_data={"count": len(saved)},
    )
    return {"pronunciations": saved}


# --- Usage & Debug Logging Endpoints ---


class ClientLogEvent(BaseModel):
    ts: str | None = None
    category: str = Field(default="intention", max_length=64)
    action: str = Field(min_length=1, max_length=128)
    intention: str | None = Field(default=None, max_length=500)
    target: str | None = Field(default=None, max_length=500)
    project_id: str | None = Field(default=None, max_length=128)
    seg_id: str | None = Field(default=None, max_length=128)
    job_id: str | None = Field(default=None, max_length=128)
    input: Any = None
    output: Any = None
    error: dict[str, Any] | None = None
    duration_ms: float | None = None


class ClientLogBatch(BaseModel):
    events: list[ClientLogEvent] = Field(default_factory=list, max_length=100)


@app.post("/api/logs/events")
async def api_ingest_log_events(request: Request) -> dict[str, int]:
    """Ingest structured frontend usage events (intentions, clicks, keypresses, outputs, errors)."""
    usage_logger = _get_usage_logger(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc

    if isinstance(payload, dict) and "events" in payload:
        try:
            batch = ClientLogBatch.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        events = batch.events
    elif isinstance(payload, dict):
        try:
            events = [ClientLogEvent.model_validate(payload)]
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
    else:
        raise HTTPException(status_code=422, detail="Log payload must be a JSON object")

    for ev in events:
        if ev.category == "error" or ev.error is not None:
            err = ev.error or {}
            usage_logger.log_error(
                source="frontend",
                action=ev.action,
                intention=ev.intention,
                target=ev.target,
                project_id=ev.project_id,
                seg_id=ev.seg_id,
                job_id=ev.job_id,
                input_data=ev.input,
                message=str(err.get("message") or "Frontend error"),
                error_type=str(err.get("type") or "FrontendError"),
                status_code=err.get("status_code") if isinstance(err.get("status_code"), int) else None,
                detail=err.get("detail"),
                context=err.get("context") if isinstance(err.get("context"), dict) else None,
                duration_ms=ev.duration_ms,
            )
        else:
            usage_logger.log_event(
                source="frontend",
                category=ev.category,
                action=ev.action,
                intention=ev.intention,
                target=ev.target,
                project_id=ev.project_id,
                seg_id=ev.seg_id,
                job_id=ev.job_id,
                input_data=ev.input,
                output_data=ev.output,
                duration_ms=ev.duration_ms,
                ts=ev.ts,
            )
    return {"accepted": len(events)}


@app.get("/api/logs")
def api_get_logs(
    request: Request,
    limit: int = 200,
    category: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Return recent structured usage & error log entries from usage.jsonl."""
    usage_logger = _get_usage_logger(request)
    events = usage_logger.read_recent(limit=limit, category=category or None, project_id=project_id or None)
    return {
        "events": events,
        "count": len(events),
        "usage_log_path": str(usage_logger.usage_log_path),
        "debug_log_path": str(usage_logger.debug_log_path),
    }


# --- Static Frontend Serving (F38) ---


def _render_index_html(session_token: str) -> HTMLResponse:
    index_path = FRONTEND_DIST_DIR / "index.html"
    if not index_path.is_file():
        return HTMLResponse(
            content=(
                "<!doctype html><html><head><meta charset='utf-8'>"
                f"<meta name=\"session-token\" content=\"{html.escape(session_token, quote=True)}\">"
                "<title>Sims 4 Vertical Video Studio</title></head>"
                "<body><div id='root'>Frontend build not found (frontend/dist/index.html).</div></body></html>"
            ),
            status_code=200,
        )

    raw_html = index_path.read_text(encoding="utf-8")
    meta_tag = f'<meta name="session-token" content="{html.escape(session_token, quote=True)}">'
    placeholder_re = re.compile(r'<meta\s+name=["\']session-token["\'][^>]*>', flags=re.IGNORECASE)
    if placeholder_re.search(raw_html):
        injected = placeholder_re.sub(meta_tag, raw_html, count=1)
    elif re.search(r"</head>", raw_html, flags=re.IGNORECASE):
        injected = re.sub(r"</head>", f"  {meta_tag}\n</head>", raw_html, count=1, flags=re.IGNORECASE)
    else:
        injected = meta_tag + "\n" + raw_html
    return HTMLResponse(content=injected)


if (FRONTEND_DIST_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST_DIR / "assets")), name="assets")


@app.get("/")
def serve_root(request: Request) -> HTMLResponse:
    """Serve index.html with injected per-launch session-token meta tag."""
    _ensure_state_initialized(request.app)
    return _render_index_html(request.app.state.session_token)


@app.get("/{full_path:path}")
def serve_spa_fallback(full_path: str, request: Request) -> Response:
    """Serve static frontend files or SPA index.html fallback for non-/api/ routes."""
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")

    _ensure_state_initialized(request.app)
    if FRONTEND_DIST_DIR.is_dir() and full_path and "\x00" not in full_path:
        dist_root = FRONTEND_DIST_DIR.resolve()
        candidate = (dist_root / full_path).resolve()
        if candidate.is_relative_to(dist_root) and candidate.is_file():
            return FileResponse(path=candidate)

    return _render_index_html(request.app.state.session_token)


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8420)
