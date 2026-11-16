"""540p 30fps CFR proxy transcoder and timestamp mapping."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.bootstrap import is_offline_env, verify_ffmpeg
from backend.media import MediaValidationError, probe_and_validate_media
from backend.schemas import ProxyState, SourceMetadata
from backend.storage import load_manifest, save_manifest


class ProxyError(RuntimeError):
    """Raised when proxy transcoding or validation fails."""


def map_source_to_proxy_time(
    source_t: float,
    source_duration: float,
    proxy_duration: float,
) -> float:
    """Map a source timestamp to the proxy timeline with bounds clamping."""
    if source_duration <= 0.0 or proxy_duration <= 0.0:
        return 0.0
    clamped_source = max(0.0, min(source_t, source_duration))
    ratio = clamped_source / source_duration
    mapped = ratio * proxy_duration
    return max(0.0, min(mapped, proxy_duration))


def _terminate_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=1.5)
    except OSError:
        pass


def generate_proxy(
    source_path: str | Path,
    output_path: str | Path,
    *,
    project_id: str | None = None,
    source_duration: float | None = None,
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    data_dir: Path | None = None,
    on_progress: Callable[[int, str], None] | None = None,
    on_process_start: Callable[[subprocess.Popen[Any]], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> SourceMetadata:
    """Transcode a 540p 30fps CFR proxy MP4, validate with ffprobe, and atomically move to output_path."""
    src_path = Path(source_path)
    out_path = Path(output_path)

    def _mark_failed(err_msg: str) -> None:
        if project_id is not None:
            try:
                manifest = load_manifest(project_id, data_dir=data_dir)
                manifest.proxy = ProxyState(
                    status="failed",
                    artifact_id=None,
                    path=None,
                    error=err_msg,
                )
                save_manifest(manifest, data_dir=data_dir)
            except Exception:
                pass

    if not src_path.is_file():
        msg = f"Source file does not exist for proxy generation: {src_path}"
        _mark_failed(msg)
        raise ProxyError(msg)

    if cancel_event is not None and cancel_event.is_set():
        raise ProxyError("Proxy generation cancelled before start.")

    if ffmpeg_path is None or ffprobe_path is None:
        v_ffmpeg, v_ffprobe = verify_ffmpeg(data_dir=data_dir, offline=is_offline_env())
        resolved_ffmpeg = Path(ffmpeg_path) if ffmpeg_path is not None else v_ffmpeg
        resolved_ffprobe = Path(ffprobe_path) if ffprobe_path is not None else v_ffprobe
    else:
        resolved_ffmpeg = Path(ffmpeg_path)
        resolved_ffprobe = Path(ffprobe_path)

    try:
        src_meta = probe_and_validate_media(
            src_path,
            ffprobe_path=resolved_ffprobe,
            ffmpeg_path=resolved_ffmpeg,
            data_dir=data_dir,
        )
    except MediaValidationError as exc:
        _mark_failed(str(exc))
        raise ProxyError(f"Source validation failed during proxy generation: {exc}") from exc

    total_duration = source_duration if (source_duration is not None and source_duration > 0.0) else src_meta.duration

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out_path.stem}.", suffix=".tmp.mp4", dir=out_path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)

    cmd = [
        str(resolved_ffmpeg),
        "-y",
        "-i",
        str(src_path),
        "-vf",
        "scale=-2:540,fps=30",
        "-g",
        "15",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "26",
        "-movflags",
        "+faststart",
    ]
    if src_meta.has_audio:
        cmd.extend(["-c:a", "aac", "-ar", "48000", "-ac", "2"])
    else:
        cmd.append("-an")

    cmd.extend(["-progress", "pipe:1", "-nostats", str(tmp_path)])

    if on_progress is not None:
        on_progress(0, "Starting proxy transcode")

    proc: subprocess.Popen[str] | None = None
    stderr_lines: list[str] = []

    def _drain_stderr(pipe: Any) -> None:
        try:
            for line in pipe:
                stderr_lines.append(line)
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            cmd,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if on_process_start is not None:
            on_process_start(proc)

        stderr_thread = threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True)
        stderr_thread.start()

        last_pct = -1
        if proc.stdout is not None:
            for raw_line in proc.stdout:
                if cancel_event is not None and cancel_event.is_set():
                    _terminate_process(proc)
                    raise ProxyError("Proxy generation cancelled.")
                line = raw_line.strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    _, val_str = line.split("=", 1)
                    try:
                        out_us = int(val_str)
                        if total_duration > 0.0 and out_us >= 0:
                            pct = min(95, max(0, int((out_us / (total_duration * 1_000_000.0)) * 95)))
                            if pct > last_pct:
                                last_pct = pct
                                if on_progress is not None:
                                    on_progress(pct, "Transcoding proxy")
                    except ValueError:
                        pass
                elif line == "progress=end" and on_progress is not None and last_pct < 95:
                    last_pct = 95
                    on_progress(95, "Validating proxy")

        returncode = proc.wait()
        stderr_thread.join(timeout=2.0)

        if cancel_event is not None and cancel_event.is_set():
            raise ProxyError("Proxy generation cancelled.")

        if returncode != 0:
            err_tail = "".join(stderr_lines[-20:]).strip()
            msg = f"FFmpeg proxy transcode failed (exit {returncode}): {err_tail}"
            _mark_failed(msg)
            raise ProxyError(msg)

        if on_progress is not None:
            on_progress(97, "Validating proxy")

        try:
            proxy_meta = probe_and_validate_media(
                tmp_path,
                ffprobe_path=resolved_ffprobe,
                ffmpeg_path=resolved_ffmpeg,
                data_dir=data_dir,
            )
        except MediaValidationError as exc:
            msg = f"Generated proxy failed ffprobe validation: {exc}"
            _mark_failed(msg)
            raise ProxyError(msg) from exc

        os.replace(tmp_path, out_path)

        if project_id is not None:
            manifest = load_manifest(project_id, data_dir=data_dir)
            if manifest.source_metadata is None:
                manifest.source_metadata = src_meta
            manifest.proxy = ProxyState(
                status="ready",
                artifact_id="art_proxy",
                path="artifacts/proxy.mp4",
                error=None,
            )
            save_manifest(manifest, data_dir=data_dir)

        if on_progress is not None:
            on_progress(100, "Proxy ready")

        return proxy_meta
    except Exception:
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
        tmp_path.unlink(missing_ok=True)
        raise
