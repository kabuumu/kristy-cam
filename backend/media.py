"""OS-safe native file selection, ffprobe media validation, and source registration."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from backend.bootstrap import is_offline_env, verify_ffmpeg
from backend.schemas import MediaAssetKind, MediaAssetMetadata, MediaAssetRef, SourceMediaRef, SourceMetadata
from backend.storage import compute_file_fingerprint

HDR_TRANSFER_CHARACTERISTICS = frozenset({"smpte2084", "arib-std-b67"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"})
AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".aac", ".m4a", ".ogg", ".flac", ".opus"})
ALPHA_PIX_FMT_TOKENS = ("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya8", "ya16", "pal8")


class MediaValidationError(ValueError):
    """Raised when selected or probed media violates V1 input requirements."""


class MediaSelectionCancelled(RuntimeError):
    """Raised when the user cancels native media selection."""


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_powershell_single_quoted(value: str) -> str:
    return value.replace("'", "''")


def _default_sims_recordings_dir() -> Path | None:
    candidate = Path.home() / "Documents" / "Electronic Arts" / "The Sims 4" / "Recorded Videos"
    if candidate.is_dir():
        return candidate
    return None


def select_media_native(dialog_title: str = "Select Sims 4 Gameplay Recording") -> Path | None:
    """Open an OS-native file picker via subprocess without a shell.

    Returns the selected Path, or None if the user cancelled the dialog.
    Raises NotImplementedError on headless/Linux environments where no native
    dialog is supported (use manual_path or import_uploaded_media instead).
    """
    if sys.platform == "darwin":
        escaped_title = _escape_applescript_string(dialog_title)
        default_dir = _default_sims_recordings_dir()
        location_clause = ""
        if default_dir is not None:
            escaped_dir = _escape_applescript_string(str(default_dir))
            location_clause = f' default location (POSIX file "{escaped_dir}")'
        script = (
            "activate\n"
            f'POSIX path of (choose file of type {{"public.movie"}} with prompt "{escaped_title}"{location_clause})'
        )
        res = subprocess.run(
            ["osascript", "-e", script],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if res.returncode != 0:
            stderr_lower = res.stderr.lower()
            if "-128" in res.stderr or "user canceled" in stderr_lower or "user cancelled" in stderr_lower:
                return None
            raise RuntimeError(f"macOS file selection failed (exit {res.returncode}): {res.stderr.strip()}")
        selected = res.stdout.strip()
        return Path(selected) if selected else None

    if sys.platform == "win32":
        escaped_title = _escape_powershell_single_quoted(dialog_title)
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$owner = New-Object System.Windows.Forms.Form; "
            "$owner.TopMost = $true; "
            "$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual; "
            "$owner.Location = New-Object System.Drawing.Point(-32000, -32000); "
            "$owner.Size = New-Object System.Drawing.Size(1, 1); "
            "$owner.ShowInTaskbar = $false; "
            "$dlg = New-Object System.Windows.Forms.OpenFileDialog; "
            f"$dlg.Title = '{escaped_title}'; "
            "$dlg.Filter = 'Video Files|*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v|All Files|*.*'; "
            "$dlg.Multiselect = $false; "
            "$simsDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Electronic Arts\\The Sims 4\\Recorded Videos'; "
            "if (Test-Path -LiteralPath $simsDir) { $dlg.InitialDirectory = $simsDir }; "
            "try { "
            "$owner.Show(); $owner.Activate(); "
            "if ($dlg.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Write-Output $dlg.FileName } "
            "} finally { $dlg.Dispose(); $owner.Dispose() }"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"Windows file selection failed (exit {res.returncode}): {res.stderr.strip()}")
        selected = res.stdout.strip()
        return Path(selected) if selected else None

    raise NotImplementedError(
        "Native OS file dialog is only supported on macOS and Windows; "
        "use manual path entry or import_uploaded_media() on Linux/headless systems."
    )


def import_uploaded_media(filename: str, data: bytes, dest_dir: str | Path) -> Path:
    """Persist uploaded media bytes safely into dest_dir (e.g. projects/<id>/source/)."""
    raw_name = Path(filename).name.strip()
    if (
        not raw_name
        or raw_name in {".", ".."}
        or "/" in raw_name
        or "\\" in raw_name
        or "\x00" in raw_name
    ):
        raise MediaValidationError(f"Invalid uploaded media filename: {filename!r}")

    target_dir = Path(dest_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    dest_path = (target_dir / raw_name).resolve()
    if dest_path.parent != target_dir.resolve():
        raise MediaValidationError(f"Uploaded filename escapes destination directory: {filename!r}")

    tmp_path = dest_path.with_name(f".{dest_path.name}.upload.tmp")
    try:
        with tmp_path.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, dest_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return dest_path


def _parse_fraction(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == "0/0":
        return None
    if "/" in text:
        parts = text.split("/", 1)
        try:
            num = float(parts[0])
            den = float(parts[1])
        except ValueError:
            return None
        if den <= 0.0 or not math.isfinite(num) or not math.isfinite(den):
            return None
        val = num / den
        return val if val > 0.0 and math.isfinite(val) else None
    try:
        val = float(text)
    except ValueError:
        return None
    return val if val > 0.0 and math.isfinite(val) else None


def _check_rotation_zero(vstream: dict[str, Any]) -> None:
    tags = vstream.get("tags") or {}
    if isinstance(tags, dict) and "rotate" in tags and tags["rotate"] is not None:
        try:
            rot = float(tags["rotate"])
        except (TypeError, ValueError) as exc:
            raise MediaValidationError(f"Invalid rotation metadata tag: {tags['rotate']!r}") from exc
        if abs(rot % 360.0) > 1e-6:
            raise MediaValidationError(
                f"Rotated video (tags.rotate={tags['rotate']!r}) is not supported in V1; rotation must be 0."
            )

    side_data_list = vstream.get("side_data_list") or []
    if isinstance(side_data_list, list):
        for sd in side_data_list:
            if not isinstance(sd, dict):
                continue
            sd_type = str(sd.get("side_data_type") or "").lower()
            if "rotation" in sd and (sd_type == "display matrix" or "display matrix" in sd_type or sd_type == ""):
                raw_rot = sd.get("rotation")
                if raw_rot is not None:
                    try:
                        rot = float(raw_rot)
                    except (TypeError, ValueError) as exc:
                        raise MediaValidationError(f"Invalid Display Matrix rotation: {raw_rot!r}") from exc
                    if abs(rot % 360.0) > 1e-6:
                        raise MediaValidationError(
                            f"Rotated video (Display Matrix rotation={raw_rot!r}) is not supported in V1; rotation must be 0."
                        )


def _check_square_pixels(vstream: dict[str, Any]) -> None:
    sar_raw = str(vstream.get("sample_aspect_ratio") or "").strip()
    if not sar_raw or sar_raw in {"1:1", "0:1", "1/1", "0/1"}:
        return

    sep = ":" if ":" in sar_raw else ("/" if "/" in sar_raw else None)
    if sep is None:
        raise MediaValidationError(f"Unsupported sample aspect ratio (SAR={sar_raw!r}); square pixels (1:1) required.")
    parts = sar_raw.split(sep, 1)
    try:
        num = float(parts[0])
        den = float(parts[1])
    except ValueError as exc:
        raise MediaValidationError(f"Invalid sample aspect ratio (SAR={sar_raw!r}).") from exc

    if num == 0.0 and den == 1.0:
        return
    if den <= 0.0 or num <= 0.0 or abs((num / den) - 1.0) > 1e-3:
        raise MediaValidationError(
            f"Non-square pixels (SAR={sar_raw!r}) are not supported in V1; square pixels (1:1) are required."
        )


def _resolve_ffmpeg_tools(
    ffmpeg_path: Path | None,
    ffprobe_path: Path | None,
    data_dir: Path | None,
) -> tuple[Path, Path]:
    if ffmpeg_path is not None and ffprobe_path is not None:
        return Path(ffmpeg_path), Path(ffprobe_path)
    verified_ffmpeg, verified_ffprobe = verify_ffmpeg(data_dir=data_dir, offline=is_offline_env())
    return (
        Path(ffmpeg_path) if ffmpeg_path is not None else verified_ffmpeg,
        Path(ffprobe_path) if ffprobe_path is not None else verified_ffprobe,
    )


def probe_and_validate_media(
    path: str | Path,
    *,
    ffprobe_path: Path | None = None,
    ffmpeg_path: Path | None = None,
    data_dir: Path | None = None,
) -> SourceMetadata:
    """Inspect media with ffprobe and ffmpeg, rejecting unsupported V1 inputs."""
    media_path = Path(path)
    if not media_path.exists() or not media_path.is_file():
        raise MediaValidationError(f"Media file does not exist: {media_path}")
    if not os.access(media_path, os.R_OK):
        raise MediaValidationError(f"Media file is not readable: {media_path}")

    resolved_ffmpeg, resolved_ffprobe = _resolve_ffmpeg_tools(ffmpeg_path, ffprobe_path, data_dir)

    probe_res = subprocess.run(
        [
            str(resolved_ffprobe),
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(media_path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if probe_res.returncode != 0:
        raise MediaValidationError(
            f"ffprobe failed for {media_path} (exit {probe_res.returncode}): {probe_res.stderr.strip()}"
        )

    try:
        payload = json.loads(probe_res.stdout)
    except json.JSONDecodeError as exc:
        raise MediaValidationError(f"ffprobe returned invalid JSON for {media_path}: {exc}") from exc

    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}

    video_streams = [
        s
        for s in streams
        if isinstance(s, dict)
        and s.get("codec_type") == "video"
        and not (isinstance(s.get("disposition"), dict) and s["disposition"].get("attached_pic") == 1)
    ]
    if not video_streams:
        raise MediaValidationError(f"No valid video stream found in {media_path}.")

    vstream = video_streams[0]

    color_transfer = str(vstream.get("color_transfer") or "").strip().lower()
    if color_transfer in HDR_TRANSFER_CHARACTERISTICS:
        raise MediaValidationError(
            f"HDR video (color_transfer={color_transfer!r}) is not supported in V1; SDR is required."
        )

    _check_rotation_zero(vstream)
    _check_square_pixels(vstream)

    try:
        width = int(vstream.get("width") or 0)
        height = int(vstream.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise MediaValidationError(f"Invalid video dimensions in {media_path}.") from exc

    if width <= 0 or height <= 0:
        raise MediaValidationError(f"Invalid video dimensions ({width}x{height}) in {media_path}.")

    fps = _parse_fraction(vstream.get("avg_frame_rate")) or _parse_fraction(vstream.get("r_frame_rate"))
    if fps is None:
        raise MediaValidationError(f"Could not determine valid video frame rate for {media_path}.")

    duration: float | None = None
    for candidate in (vstream.get("duration"), fmt.get("duration")):
        if candidate is not None:
            try:
                val = float(candidate)
                if math.isfinite(val) and val > 0.0:
                    duration = val
                    break
            except (TypeError, ValueError):
                continue

    if duration is None:
        raise MediaValidationError(f"Could not determine valid positive duration for {media_path}.")

    # Verify the video stream is actually decodable by the verified FFmpeg build.
    decode_res = subprocess.run(
        [
            str(resolved_ffmpeg),
            "-v",
            "error",
            "-i",
            str(media_path),
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if decode_res.returncode != 0:
        raise MediaValidationError(
            f"Video stream in {media_path} is not decodable by FFmpeg: {decode_res.stderr.strip()}"
        )

    audio_streams = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    has_audio = len(audio_streams) > 0
    audio_sample_rate: int | None = None
    audio_channels: int | None = None
    if has_audio:
        astream = audio_streams[0]
        if astream.get("sample_rate") is not None:
            try:
                sr = int(astream["sample_rate"])
                if sr > 0:
                    audio_sample_rate = sr
            except (TypeError, ValueError):
                audio_sample_rate = None
        if astream.get("channels") is not None:
            try:
                ch = int(astream["channels"])
                if ch > 0:
                    audio_channels = ch
            except (TypeError, ValueError):
                audio_channels = None

    codec_name = str(vstream["codec_name"]) if vstream.get("codec_name") else None
    pix_fmt = str(vstream["pix_fmt"]) if vstream.get("pix_fmt") else None

    return SourceMetadata(
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        has_audio=has_audio,
        codec_name=codec_name,
        pix_fmt=pix_fmt,
        audio_sample_rate=audio_sample_rate,
        audio_channels=audio_channels,
    )


def register_source_media(
    path: str | Path,
    *,
    media_id: str | None = None,
    copy_to_dir: Path | None = None,
    ffprobe_path: Path | None = None,
    ffmpeg_path: Path | None = None,
    data_dir: Path | None = None,
) -> tuple[SourceMediaRef, SourceMetadata]:
    """Validate source media, optionally copy into project source/, and compute fingerprint."""
    src_path = Path(path).resolve()
    metadata = probe_and_validate_media(
        src_path,
        ffprobe_path=ffprobe_path,
        ffmpeg_path=ffmpeg_path,
        data_dir=data_dir,
    )

    copied = False
    final_path = src_path
    if copy_to_dir is not None:
        dest_dir = Path(copy_to_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = (dest_dir / src_path.name).resolve()
        if dest_path != src_path:
            shutil.copy2(src_path, dest_path)
        final_path = dest_path
        copied = True

    resolved_id = media_id if media_id else f"src_{uuid.uuid4().hex[:8]}"
    fingerprint = compute_file_fingerprint(final_path)

    ref = SourceMediaRef(
        media_id=resolved_id,
        path=str(final_path),
        fingerprint=fingerprint,
        copied_to_project=copied,
    )
    return ref, metadata


def _has_alpha_pix_fmt(pix_fmt: str | None) -> bool:
    if not pix_fmt:
        return False
    lower = pix_fmt.lower()
    return any(tok in lower for tok in ALPHA_PIX_FMT_TOKENS)


def probe_and_validate_asset(
    path: str | Path,
    *,
    expected_kind: MediaAssetKind | None = None,
    default_image_duration: float = 5.0,
    ffprobe_path: Path | None = None,
    ffmpeg_path: Path | None = None,
    data_dir: Path | None = None,
) -> MediaAssetMetadata:
    """Validate and extract metadata for video, image (including RGBA PNG/WebP), or audio assets."""
    media_path = Path(path)
    if not media_path.exists() or not media_path.is_file():
        raise MediaValidationError(f"Asset file does not exist: {media_path}")
    if not os.access(media_path, os.R_OK):
        raise MediaValidationError(f"Asset file is not readable: {media_path}")

    resolved_ffmpeg, resolved_ffprobe = _resolve_ffmpeg_tools(ffmpeg_path, ffprobe_path, data_dir)

    probe_res = subprocess.run(
        [
            str(resolved_ffprobe),
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(media_path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if probe_res.returncode != 0:
        raise MediaValidationError(
            f"ffprobe failed for {media_path} (exit {probe_res.returncode}): {probe_res.stderr.strip()}"
        )

    try:
        payload = json.loads(probe_res.stdout)
    except json.JSONDecodeError as exc:
        raise MediaValidationError(f"ffprobe returned invalid JSON for {media_path}: {exc}") from exc

    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    suffix = media_path.suffix.lower()

    video_streams = [
        s
        for s in streams
        if isinstance(s, dict)
        and s.get("codec_type") == "video"
        and not (isinstance(s.get("disposition"), dict) and s["disposition"].get("attached_pic") == 1)
    ]
    audio_streams = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]

    inferred_kind: MediaAssetKind
    if expected_kind is not None:
        inferred_kind = expected_kind
    elif suffix in IMAGE_EXTENSIONS:
        inferred_kind = "image"
    elif suffix in AUDIO_EXTENSIONS and not video_streams:
        inferred_kind = "audio"
    elif video_streams:
        vs0 = video_streams[0]
        codec0 = str(vs0.get("codec_name") or "").lower()
        nb_frames0 = str(vs0.get("nb_frames") or "").strip()
        avg_fps0 = str(vs0.get("avg_frame_rate") or "").strip()
        if codec0 in {"png", "mjpeg", "webp", "bmp", "tiff"} and (
            avg_fps0 in {"", "0/0"} or nb_frames0 == "1"
        ):
            inferred_kind = "image"
        else:
            inferred_kind = "video"
    elif audio_streams:
        inferred_kind = "audio"
    else:
        raise MediaValidationError(f"Could not detect supported media type in {media_path}.")

    if inferred_kind == "audio":
        if not audio_streams:
            raise MediaValidationError(f"No audio stream found in {media_path}.")
        astream = audio_streams[0]
        duration: float | None = None
        for candidate in (astream.get("duration"), fmt.get("duration")):
            if candidate is not None:
                try:
                    val = float(candidate)
                    if math.isfinite(val) and val > 0.0:
                        duration = val
                        break
                except (TypeError, ValueError):
                    continue
        if duration is None:
            raise MediaValidationError(f"Could not determine positive audio duration for {media_path}.")

        decode_res = subprocess.run(
            [
                str(resolved_ffmpeg),
                "-v",
                "error",
                "-i",
                str(media_path),
                "-t",
                "0.5",
                "-vn",
                "-f",
                "null",
                "-",
            ],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if decode_res.returncode != 0:
            raise MediaValidationError(
                f"Audio stream in {media_path} is not decodable by FFmpeg: {decode_res.stderr.strip()}"
            )

        sr = None
        if astream.get("sample_rate") is not None:
            try:
                sr = int(astream["sample_rate"])
                if sr <= 0:
                    sr = None
            except (TypeError, ValueError):
                sr = None
        ch = None
        if astream.get("channels") is not None:
            try:
                ch = int(astream["channels"])
                if ch <= 0:
                    ch = None
            except (TypeError, ValueError):
                ch = None

        return MediaAssetMetadata(
            kind="audio",
            width=None,
            height=None,
            fps=None,
            duration=duration,
            has_audio=True,
            has_alpha=False,
            codec_name=str(astream["codec_name"]) if astream.get("codec_name") else None,
            pix_fmt=None,
            audio_sample_rate=sr,
            audio_channels=ch,
        )

    if inferred_kind == "image":
        if not video_streams:
            raise MediaValidationError(f"No image stream found in {media_path}.")
        vstream = video_streams[0]
        try:
            width = int(vstream.get("width") or 0)
            height = int(vstream.get("height") or 0)
        except (TypeError, ValueError) as exc:
            raise MediaValidationError(f"Invalid image dimensions in {media_path}.") from exc
        if width <= 0 or height <= 0:
            raise MediaValidationError(f"Invalid image dimensions ({width}x{height}) in {media_path}.")

        decode_res = subprocess.run(
            [
                str(resolved_ffmpeg),
                "-v",
                "error",
                "-i",
                str(media_path),
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if decode_res.returncode != 0:
            raise MediaValidationError(
                f"Image file {media_path} is not decodable by FFmpeg: {decode_res.stderr.strip()}"
            )

        pix_fmt = str(vstream["pix_fmt"]) if vstream.get("pix_fmt") else None
        codec_name = str(vstream["codec_name"]) if vstream.get("codec_name") else None
        return MediaAssetMetadata(
            kind="image",
            width=width,
            height=height,
            fps=None,
            duration=default_image_duration,
            has_audio=False,
            has_alpha=_has_alpha_pix_fmt(pix_fmt),
            codec_name=codec_name,
            pix_fmt=pix_fmt,
            audio_sample_rate=None,
            audio_channels=None,
        )

    # Video asset
    src_meta = probe_and_validate_media(
        media_path,
        ffprobe_path=resolved_ffprobe,
        ffmpeg_path=resolved_ffmpeg,
        data_dir=data_dir,
    )
    return MediaAssetMetadata(
        kind="video",
        width=src_meta.width,
        height=src_meta.height,
        fps=src_meta.fps,
        duration=src_meta.duration,
        has_audio=src_meta.has_audio,
        has_alpha=_has_alpha_pix_fmt(src_meta.pix_fmt),
        codec_name=src_meta.codec_name,
        pix_fmt=src_meta.pix_fmt,
        audio_sample_rate=src_meta.audio_sample_rate,
        audio_channels=src_meta.audio_channels,
    )


def register_project_asset(
    path: str | Path,
    *,
    expected_kind: MediaAssetKind | None = None,
    asset_id: str | None = None,
    name: str | None = None,
    copy_to_dir: Path | None = None,
    ffprobe_path: Path | None = None,
    ffmpeg_path: Path | None = None,
    data_dir: Path | None = None,
) -> tuple[MediaAssetRef, MediaAssetMetadata]:
    """Validate any project media asset (video/image/audio), optionally copy to dir, and fingerprint."""
    src_path = Path(path).resolve()
    metadata = probe_and_validate_asset(
        src_path,
        expected_kind=expected_kind,
        ffprobe_path=ffprobe_path,
        ffmpeg_path=ffmpeg_path,
        data_dir=data_dir,
    )

    resolved_id = asset_id if asset_id else f"asset_{uuid.uuid4().hex[:8]}"
    copied = False
    final_path = src_path
    if copy_to_dir is not None:
        dest_dir = Path(copy_to_dir).resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = (dest_dir / src_path.name).resolve()
        if dest_path.parent != dest_dir:
            raise MediaValidationError(f"Asset filename escapes destination directory: {src_path.name!r}")
        if dest_path != src_path:
            if dest_path.exists():
                stem = dest_path.stem
                suf = dest_path.suffix
                dest_path = (dest_dir / f"{stem}_{resolved_id}{suf}").resolve()
            shutil.copy2(src_path, dest_path)
        final_path = dest_path
        copied = True

    fingerprint = compute_file_fingerprint(final_path)
    display_name = (name or src_path.name).strip() or resolved_id
    ref = MediaAssetRef(
        asset_id=resolved_id,
        kind=metadata.kind,
        name=display_name,
        path=str(final_path),
        fingerprint=fingerprint,
        copied_to_project=copied,
    )
    return ref, metadata
