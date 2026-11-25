"""Atomic JSON persistence, .bak recovery, optimistic revision locking, and source portability."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.bootstrap import get_data_dir
from backend.schemas import (
    CURRENT_SCHEMA_VERSION,
    ArtifactManifest,
    AudioTrack,
    CaptionClip,
    CaptionSettings,
    CaptionTrack,
    CropKeyframe,
    MediaAssetRef,
    OutputSettings,
    ProjectInput,
    SegmentInput,
    SourceMediaRef,
    TimelineTrack,
    VideoTrack,
    VisualClip,
    utc_now_iso,
)

FINGERPRINT_CHUNK_BYTES = 64 * 1024  # 64 KB header + 64 KB footer


class StorageError(Exception):
    """Base error for persistence and storage operations."""


class ProjectNotFoundError(StorageError):
    """Raised when a project directory or project.json/.bak does not exist."""


class RevisionConflictError(StorageError):
    """Raised when expected_revision does not match persisted project revision (HTTP 409)."""

    def __init__(self, expected_revision: int, actual_revision: int, project_id: str = "") -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.project_id = project_id
        super().__init__(
            f"Revision conflict for project {project_id!r}: "
            f"expected {expected_revision}, actual {actual_revision}"
        )


class UnsupportedSchemaVersionError(StorageError):
    """Raised when project.json has a schema_version newer than supported."""

    def __init__(self, found_version: Any, max_supported_version: int = CURRENT_SCHEMA_VERSION) -> None:
        self.found_version = found_version
        self.max_supported_version = max_supported_version
        super().__init__(
            f"Unsupported schema_version {found_version!r}; "
            f"max supported version is {max_supported_version}"
        )


class CorruptProjectError(StorageError):
    """Raised when both project.json and project.json.bak are corrupt or invalid."""


class InsufficientDiskSpaceError(StorageError):
    """Raised when free disk space is insufficient for copying or proxy generation."""

    def __init__(self, target_path: Path, required_bytes: int, available_bytes: int) -> None:
        self.target_path = target_path
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        req_gb = required_bytes / (1024 ** 3)
        avail_gb = available_bytes / (1024 ** 3)
        super().__init__(
            f"Insufficient free disk space at {target_path}: "
            f"need {req_gb:.2f} GB, only {avail_gb:.2f} GB available."
        )


def ensure_free_disk_space(target_path: str | Path, required_bytes: int) -> None:
    """Verify at least required_bytes are free on the filesystem containing target_path."""
    if required_bytes <= 0:
        return
    probe_path = Path(target_path)
    while not probe_path.exists() and probe_path.parent != probe_path:
        probe_path = probe_path.parent
    usage = shutil.disk_usage(probe_path)
    if usage.free < required_bytes:
        raise InsufficientDiskSpaceError(probe_path, required_bytes, usage.free)


def _resolve_data_dir(data_dir: Path | None = None) -> Path:
    return data_dir if data_dir is not None else get_data_dir()


def get_project_dir(project_id: str, data_dir: Path | None = None) -> Path:
    """Resolve a project directory safely, rejecting empty IDs and path traversal."""
    if not project_id or not isinstance(project_id, str):
        raise StorageError(f"Invalid project_id: {project_id!r}")
    if (
        "/" in project_id
        or "\\" in project_id
        or ".." in project_id
        or "\x00" in project_id
        or project_id.strip() != project_id
    ):
        raise StorageError(f"Invalid project_id (path traversal rejected): {project_id!r}")

    base = _resolve_data_dir(data_dir)
    projects_root = (base / "projects").resolve()
    projects_root.mkdir(parents=True, exist_ok=True)
    candidate = (projects_root / project_id).resolve()

    if candidate.parent != projects_root:
        raise StorageError(f"Invalid project_id (escapes projects root): {project_id!r}")

    return candidate


def atomic_write_bytes(path: Path, data: bytes, create_backup: bool = False) -> None:
    """Atomically write bytes via temp file in same dir, flush+fsync, optional .bak, os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        if create_backup and path.is_file():
            bak_path = path.with_name(f"{path.name}.bak")
            shutil.copy2(path, bak_path)

        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Any, create_backup: bool = False) -> None:
    """Serialize payload as UTF-8 JSON and write atomically."""
    encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    atomic_write_bytes(path, encoded, create_backup=create_backup)


def _parse_and_validate_project_bytes(raw_text: str) -> ProjectInput:
    """Parse JSON, check schema_version BEFORE Pydantic validation, and return ProjectInput."""
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("project.json root must be a JSON object")

    raw_version = data.get("schema_version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise UnsupportedSchemaVersionError(raw_version, CURRENT_SCHEMA_VERSION)
    if raw_version > CURRENT_SCHEMA_VERSION or raw_version < 1:
        raise UnsupportedSchemaVersionError(raw_version, CURRENT_SCHEMA_VERSION)

    return ProjectInput.model_validate(data)


def load_project(project_id: str, data_dir: Path | None = None) -> ProjectInput:
    """Load project.json, falling back to project.json.bak if missing or corrupt JSON.

    Never overwrites or modifies files on UnsupportedSchemaVersionError.
    """
    project_dir = get_project_dir(project_id, data_dir)
    project_path = project_dir / "project.json"
    bak_path = project_dir / "project.json.bak"

    if not project_path.is_file() and not bak_path.is_file():
        raise ProjectNotFoundError(f"Project {project_id!r} not found at {project_dir}")

    primary_error: Exception | None = None
    if project_path.is_file():
        try:
            raw_text = project_path.read_text(encoding="utf-8")
            return _parse_and_validate_project_bytes(raw_text)
        except UnsupportedSchemaVersionError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
            primary_error = exc

    if bak_path.is_file():
        try:
            raw_bak = bak_path.read_text(encoding="utf-8")
            return _parse_and_validate_project_bytes(raw_bak)
        except UnsupportedSchemaVersionError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as bak_exc:
            raise CorruptProjectError(
                f"Both project.json and project.json.bak are unreadable/invalid for {project_id!r}: "
                f"primary={primary_error}; backup={bak_exc}"
            ) from bak_exc

    raise CorruptProjectError(
        f"project.json is corrupt for {project_id!r} and no .bak exists: {primary_error}"
    )


def save_project(
    project: ProjectInput,
    expected_revision: int,
    data_dir: Path | None = None,
) -> ProjectInput:
    """Persist ProjectInput with optimistic revision locking and .bak backup."""
    project_dir = get_project_dir(project.project_id, data_dir)
    existing = load_project(project.project_id, data_dir)

    if existing.revision != expected_revision:
        raise RevisionConflictError(
            expected_revision=expected_revision,
            actual_revision=existing.revision,
            project_id=project.project_id,
        )

    updated = project.model_copy(
        update={
            "revision": existing.revision + 1,
            "updated_at": utc_now_iso(),
        }
    )
    project_path = project_dir / "project.json"
    atomic_write_json(project_path, updated.model_dump(mode="json"), create_backup=True)
    return updated


def load_global_pronunciations(data_dir: Path | None = None) -> dict[str, str]:
    """Load global default pronunciation dictionary from data_dir/pronunciations.json."""
    base = _resolve_data_dir(data_dir)
    pron_path = base / "pronunciations.json"
    if not pron_path.is_file():
        return {}
    raw = json.loads(pron_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise StorageError("Global pronunciations.json must be a JSON object")
    return {str(k): str(v) for k, v in raw.items()}


def save_global_pronunciations(
    pronunciations: dict[str, str],
    data_dir: Path | None = None,
) -> dict[str, str]:
    """Atomically save global default pronunciation dictionary."""
    base = _resolve_data_dir(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    cleaned = {str(k): str(v) for k, v in pronunciations.items()}
    atomic_write_json(base / "pronunciations.json", cleaned, create_backup=True)
    return dict(cleaned)


def create_default_tracks() -> list[TimelineTrack]:
    """Return the initial independent V1, T1, A1 tracks for a multi-track NLE timeline."""
    return [
        VideoTrack(id="v1", name="Video 1", camera_follow=True),
        CaptionTrack(id="t1", name="Captions 1"),
        AudioTrack(id="a1", name="Audio 1", duck_under_narration=True),
    ]


def ensure_primary_media_registered(project: ProjectInput) -> ProjectInput:
    """Ensure project.source_media is registered in project.media_assets."""
    sm = project.source_media
    existing = project.media_assets.get(sm.media_id)
    if (
        existing is not None
        and existing.path == sm.path
        and existing.fingerprint == sm.fingerprint
        and existing.copied_to_project == sm.copied_to_project
    ):
        return project

    updated_assets = dict(project.media_assets)
    updated_assets[sm.media_id] = MediaAssetRef(
        asset_id=sm.media_id,
        kind="video",
        name=Path(sm.path).name or existing.name if existing is not None else (Path(sm.path).name or "Primary Video"),
        path=sm.path,
        fingerprint=sm.fingerprint,
        copied_to_project=sm.copied_to_project,
    )
    return project.model_copy(update={"media_assets": updated_assets})


def _compute_legacy_segment_duration(
    seg: SegmentInput,
    audio_duration: float,
    tail_padding_sec: float,
    fps: int,
) -> float:
    import math

    raw_video_dur = max(0.01, float(seg.video_out - seg.video_in))
    nar_dur = max(0.0, float(audio_duration))
    if seg.duration_mode == "preserve_video":
        raw_target = max(raw_video_dur, nar_dur + tail_padding_sec) if nar_dur > 0.0 else raw_video_dur
    else:
        raw_target = (nar_dur + tail_padding_sec) if nar_dur > 0.0 else raw_video_dur
    frame_count = max(1, math.ceil(raw_target * float(max(1, fps))))
    return float(frame_count) / float(max(1, fps))


def migrate_legacy_segments_to_tracks(
    project: ProjectInput,
    manifest: ArtifactManifest | None = None,
    *,
    clear_segments: bool = False,
) -> ProjectInput:
    """Deterministically convert legacy sequential segments into decoupled V1/T1/A1 tracks."""
    registered = ensure_primary_media_registered(project)
    has_track_clips = any(len(t.clips) > 0 for t in registered.tracks)
    if has_track_clips or not registered.segments:
        if not registered.tracks:
            return registered.model_copy(update={"tracks": create_default_tracks()})
        return registered

    fps = max(1, registered.output_settings.fps)
    tail_pad = registered.output_settings.tail_padding_sec
    existing_v1 = next((t for t in registered.tracks if t.kind == "video"), None)
    existing_t1 = next((t for t in registered.tracks if t.kind == "caption"), None)
    existing_a1 = next((t for t in registered.tracks if t.kind == "audio"), None)

    visual_clips: list[VisualClip] = []
    caption_clips: list[CaptionClip] = []
    migrated_kfs: list[CropKeyframe] = list(registered.camera_keyframes)
    preserve_existing_camera = len(migrated_kfs) > 0

    used_clip_ids: set[str] = set()

    def _alloc_clip_id(preferred: str) -> str:
        candidate = preferred or "clip"
        if candidate not in used_clip_ids:
            used_clip_ids.add(candidate)
            return candidate
        idx = 2
        while f"{candidate}_{idx}" in used_clip_ids:
            idx += 1
        unique = f"{candidate}_{idx}"
        used_clip_ids.add(unique)
        return unique

    cursor = 0.0
    for seg in registered.segments:
        seg_art = manifest.segments.get(seg.id) if manifest is not None else None
        audio_dur = seg_art.audio_duration if seg_art is not None else 0.0
        final_dur = _compute_legacy_segment_duration(seg, audio_dur, tail_pad, fps)

        vclip_id = _alloc_clip_id(f"vclip_{seg.id}")
        visual_clips.append(
            VisualClip(
                id=vclip_id,
                asset_id=registered.source_media.media_id,
                kind="video",
                start_sec=round(cursor, 6),
                duration_sec=round(final_dur, 6),
                media_in=round(seg.video_in, 6),
                media_out=round(seg.video_out, 6),
                speed=1.0,  # Legacy SegmentInput.speed governed TTS speed, while video played at 1.0x
                end_behavior="freeze",
                volume=seg.source_audio_volume,
            )
        )

        if not preserve_existing_camera:
            for kf in seg.crop_keyframes:
                global_t = round(cursor + kf.t, 4)
                if not migrated_kfs or global_t > migrated_kfs[-1].t + 1e-4:
                    migrated_kfs.append(
                        CropKeyframe(
                            t=global_t,
                            x=kf.x,
                            y=kf.y,
                            zoom=kf.zoom,
                            transition=kf.transition,
                        )
                    )

        clause_timings = seg_art.clause_timings if seg_art is not None else []
        local_cursor = 0.0
        for c_idx, clause in enumerate(seg.clauses):
            timing = clause_timings[c_idx] if c_idx < len(clause_timings) else None
            if clause.start_sec is not None:
                local_start = clause.start_sec
            elif timing is not None:
                local_start = timing.start_sec
            else:
                local_start = local_cursor

            if clause.duration_sec is not None:
                c_dur: float | None = round(clause.duration_sec, 6)
                auto_fit = False
                local_cursor = local_start + clause.duration_sec
            elif timing is not None and timing.end_sec > timing.start_sec:
                c_dur = round(timing.end_sec - timing.start_sec, 6)
                auto_fit = True
                local_cursor = timing.end_sec + (registered.output_settings.inter_clause_pause_ms / 1000.0)
            else:
                c_dur = None
                auto_fit = True
                local_cursor = local_start + 1.5

            cclip_id = _alloc_clip_id(f"cap_{seg.id}_{clause.id}")
            caption_clips.append(
                CaptionClip(
                    id=cclip_id,
                    start_sec=round(cursor + local_start, 6),
                    duration_sec=c_dur,
                    auto_fit_tts=auto_fit,
                    display_text=clause.display_text,
                    spoken_text_override=clause.spoken_text_override,
                    tts_enabled=True,
                    speed=seg.speed,
                )
            )

        cursor += final_dur

    v_track = VideoTrack(
        id=existing_v1.id if existing_v1 is not None else "v1",
        name=existing_v1.name if existing_v1 is not None else "Video 1",
        visible=existing_v1.visible if existing_v1 is not None else True,
        muted=existing_v1.muted if existing_v1 is not None else False,
        locked=existing_v1.locked if existing_v1 is not None else False,
        camera_follow=existing_v1.camera_follow if existing_v1 is not None else True,
        clips=visual_clips,
        transitions=[],
    )
    t_track = CaptionTrack(
        id=existing_t1.id if existing_t1 is not None else "t1",
        name=existing_t1.name if existing_t1 is not None else "Captions 1",
        visible=existing_t1.visible if existing_t1 is not None else True,
        muted=existing_t1.muted if existing_t1 is not None else False,
        locked=existing_t1.locked if existing_t1 is not None else False,
        clips=caption_clips,
    )
    a_track = existing_a1 if existing_a1 is not None else AudioTrack(id="a1", name="Audio 1", duck_under_narration=True)
    other_tracks = [
        t
        for t in registered.tracks
        if t.id not in {v_track.id, t_track.id, a_track.id}
    ]
    tracks: list[TimelineTrack] = [v_track, t_track, a_track, *other_tracks]

    return registered.model_copy(
        update={
            "camera_keyframes": migrated_kfs,
            "tracks": tracks,
            "segments": [] if clear_segments else registered.segments,
        }
    )


def create_project(
    project_id: str,
    title: str,
    source_media: SourceMediaRef,
    *,
    voice: str = "bf_emma",
    language: str = "en-gb",
    output_settings: OutputSettings | None = None,
    caption_settings: CaptionSettings | None = None,
    media_assets: dict[str, MediaAssetRef] | None = None,
    camera_keyframes: list[CropKeyframe] | None = None,
    tracks: list[TimelineTrack] | None = None,
    segments: list[SegmentInput] | None = None,
    data_dir: Path | None = None,
) -> ProjectInput:
    """Create a new project directory layout, seed pronunciations from global defaults, and persist."""
    project_dir = get_project_dir(project_id, data_dir)
    project_path = project_dir / "project.json"
    if project_path.is_file():
        raise StorageError(f"Project {project_id!r} already exists")

    for sub in ("artifacts", "exports", "tmp", "source"):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    seeded_pronunciations = dict(load_global_pronunciations(data_dir))
    resolved_assets = (
        dict(media_assets)
        if media_assets is not None
        else {
            source_media.media_id: MediaAssetRef(
                asset_id=source_media.media_id,
                kind="video",
                name=Path(source_media.path).name or "Primary Video",
                path=source_media.path,
                fingerprint=source_media.fingerprint,
                copied_to_project=source_media.copied_to_project,
            )
        }
    )
    resolved_tracks = list(tracks) if tracks is not None else create_default_tracks()
    now = utc_now_iso()
    project = ProjectInput(
        schema_version=CURRENT_SCHEMA_VERSION,
        project_id=project_id,
        revision=1,
        title=title,
        created_at=now,
        updated_at=now,
        source_media=source_media,
        media_assets=resolved_assets,
        voice=voice,
        language=language,
        output_settings=output_settings if output_settings is not None else OutputSettings(),
        caption_settings=caption_settings if caption_settings is not None else CaptionSettings(),
        pronunciations=seeded_pronunciations,
        camera_keyframes=camera_keyframes if camera_keyframes is not None else [],
        tracks=resolved_tracks,
        segments=segments if segments is not None else [],
    )
    atomic_write_json(project_path, project.model_dump(mode="json"), create_backup=False)
    manifest = ArtifactManifest(project_id=project_id)
    atomic_write_json(project_dir / "manifest.json", manifest.model_dump(mode="json"), create_backup=False)
    return project


def load_manifest(project_id: str, data_dir: Path | None = None) -> ArtifactManifest:
    """Load manifest.json for a project, returning an empty ArtifactManifest if absent."""
    project_dir = get_project_dir(project_id, data_dir)
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.is_file():
        return ArtifactManifest(project_id=project_id)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return ArtifactManifest.model_validate(data)


def save_manifest(manifest: ArtifactManifest, data_dir: Path | None = None) -> None:
    """Atomically persist manifest.json."""
    project_dir = get_project_dir(manifest.project_id, data_dir)
    atomic_write_json(project_dir / "manifest.json", manifest.model_dump(mode="json"), create_backup=True)


def compute_file_fingerprint(path: str | Path) -> str:
    """Compute fast portable fingerprint from size, mtime_ns, and 64KB header/footer SHA-256."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Cannot fingerprint missing file: {file_path}")

    st = file_path.stat()
    h = hashlib.sha256()
    h.update(f"{st.st_size}:{st.st_mtime_ns}:".encode("utf-8"))

    with file_path.open("rb") as f:
        header = f.read(FINGERPRINT_CHUNK_BYTES)
        h.update(header)
        if st.st_size > FINGERPRINT_CHUNK_BYTES:
            offset = max(FINGERPRINT_CHUNK_BYTES, st.st_size - FINGERPRINT_CHUNK_BYTES)
            f.seek(offset)
            footer = f.read(FINGERPRINT_CHUNK_BYTES)
            h.update(footer)

    return f"sha256:{h.hexdigest()}"


def check_source_media(source_media: SourceMediaRef) -> dict[str, Any]:
    """Check whether source_media exists on disk and matches its stored fingerprint."""
    path = Path(source_media.path)
    if not path.is_file():
        return {
            "exists": False,
            "fingerprint_match": False,
            "actual_fingerprint": None,
            "status": "missing",
        }

    actual = compute_file_fingerprint(path)
    match = actual == source_media.fingerprint
    return {
        "exists": True,
        "fingerprint_match": match,
        "actual_fingerprint": actual,
        "status": "ok" if match else "modified",
    }


def relink_source_media(
    project_id: str,
    new_path: str | Path,
    media_id: str,
    copy_to_project: bool = False,
    data_dir: Path | None = None,
) -> ProjectInput:
    """Relink a project's source_media to new_path (optionally copying into projects/<id>/source/)."""
    src_path = Path(new_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"Relink target file does not exist: {src_path}")

    project = load_project(project_id, data_dir)
    project_dir = get_project_dir(project_id, data_dir)

    target_path = src_path.resolve()
    if copy_to_project:
        source_dir = project_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        dest_path = source_dir / src_path.name
        if dest_path.resolve() != target_path:
            shutil.copy2(target_path, dest_path)
        target_path = dest_path.resolve()

    old_media_id = project.source_media.media_id
    fingerprint = compute_file_fingerprint(target_path)
    updated_ref = SourceMediaRef(
        media_id=media_id,
        path=str(target_path),
        fingerprint=fingerprint,
        copied_to_project=copy_to_project,
    )
    updated_assets = dict(project.media_assets)
    if old_media_id != media_id:
        updated_assets.pop(old_media_id, None)
    updated_assets[media_id] = MediaAssetRef(
        asset_id=media_id,
        kind="video",
        name=target_path.name or "Primary Video",
        path=str(target_path),
        fingerprint=fingerprint,
        copied_to_project=copy_to_project,
    )
    updated_tracks: list[TimelineTrack] = []
    for tr in project.tracks:
        if isinstance(tr, (VideoTrack, AudioTrack)) and old_media_id != media_id:
            new_clips = [
                c.model_copy(update={"asset_id": media_id}) if c.asset_id == old_media_id else c
                for c in tr.clips
            ]
            updated_tracks.append(tr.model_copy(update={"clips": new_clips}))
        else:
            updated_tracks.append(tr)

    updated_project = project.model_copy(
        update={
            "source_media": updated_ref,
            "media_assets": updated_assets,
            "tracks": updated_tracks,
        }
    )
    return save_project(updated_project, expected_revision=project.revision, data_dir=data_dir)


def rename_project(
    project_id: str,
    new_title: str,
    data_dir: Path | None = None,
) -> ProjectInput:
    """Rename a project's title atomically, bumping its revision."""
    clean_title = new_title.strip()
    if not clean_title:
        raise StorageError("Project title must not be empty")
    project = load_project(project_id, data_dir)
    updated = project.model_copy(update={"title": clean_title})
    return save_project(updated, expected_revision=project.revision, data_dir=data_dir)


def duplicate_project(
    project_id: str,
    new_project_id: str,
    new_title: str | None = None,
    data_dir: Path | None = None,
) -> tuple[ProjectInput, ArtifactManifest]:
    """Deep-copy an existing project directory (project.json, manifest.json, artifacts, source) to new_project_id."""
    src_dir = get_project_dir(project_id, data_dir)
    src_project = load_project(project_id, data_dir)
    src_manifest = load_manifest(project_id, data_dir)

    dst_dir = get_project_dir(new_project_id, data_dir)
    if dst_dir.exists():
        raise StorageError(f"Target project directory already exists: {new_project_id}")

    dst_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("artifacts", "exports", "tmp", "source"):
        src_sub = src_dir / sub
        dst_sub = dst_dir / sub
        if sub in ("artifacts", "source") and src_sub.is_dir():
            shutil.copytree(src_sub, dst_sub, dirs_exist_ok=True)
        else:
            dst_sub.mkdir(parents=True, exist_ok=True)

    # Rewrite any paths that pointed inside src_dir so the duplicate is self-contained
    src_resolved = src_dir.resolve()
    dst_resolved = dst_dir.resolve()

    def _remap_path(p: str | None) -> str | None:
        if not p:
            return p
        try:
            rel = Path(p).resolve().relative_to(src_resolved)
            return str(dst_resolved / rel)
        except Exception:
            return p

    now_iso = utc_now_iso()
    title = (new_title or "").strip() or f"{src_project.title} (Copy)"

    remapped_source = src_project.source_media.model_copy(
        update={"path": _remap_path(src_project.source_media.path) or src_project.source_media.path}
    )
    remapped_assets = {
        aid: ref.model_copy(update={"path": _remap_path(ref.path) or ref.path})
        for aid, ref in src_project.media_assets.items()
    }
    new_project = src_project.model_copy(
        update={
            "project_id": new_project_id,
            "revision": 1,
            "title": title,
            "created_at": now_iso,
            "updated_at": now_iso,
            "source_media": remapped_source,
            "media_assets": remapped_assets,
        }
    )

    remapped_proxy = src_manifest.proxy.model_copy(
        update={"path": _remap_path(src_manifest.proxy.path)}
    )
    remapped_media_proxies = {
        aid: mp.model_copy(update={"path": _remap_path(mp.path)})
        for aid, mp in src_manifest.media_proxies.items()
    }
    remapped_cap_arts = {
        cid: ca.model_copy(update={"audio_path": _remap_path(ca.audio_path)})
        for cid, ca in src_manifest.caption_artifacts.items()
    }
    remapped_segments = {
        sid: seg_art.model_copy(
            update={
                "audio_path": _remap_path(seg_art.audio_path),
                "rendered_segment_path": _remap_path(seg_art.rendered_segment_path),
                "preview_path": _remap_path(seg_art.preview_path),
            }
        )
        for sid, seg_art in src_manifest.segments.items()
    }
    new_manifest = src_manifest.model_copy(
        update={
            "project_id": new_project_id,
            "proxy": remapped_proxy,
            "media_proxies": remapped_media_proxies,
            "caption_artifacts": remapped_cap_arts,
            "segments": remapped_segments,
            "latest_preview_artifact_id": None,
            "latest_preview_path": None,
            "latest_export_artifact_id": None,
            "latest_export_path": None,
        }
    )

    atomic_write_json(dst_dir / "project.json", new_project.model_dump(mode="json"), create_backup=False)
    save_manifest(new_manifest, data_dir=data_dir)
    return new_project, new_manifest


def delete_project(project_id: str, data_dir: Path | None = None) -> None:
    """Permanently delete a project directory and all its local artifacts."""
    project_dir = get_project_dir(project_id, data_dir)
    if not project_dir.exists():
        raise ProjectNotFoundError(f"Project {project_id!r} not found")
    shutil.rmtree(project_dir)


