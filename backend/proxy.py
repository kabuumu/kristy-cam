"""540p 30fps CFR proxy transcoder and timestamp mapping."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.bootstrap import is_offline_env, verify_ffmpeg
from backend.media import MediaValidationError, probe_and_validate_media
from backend.schemas import MediaProxyState, ProxyState, SourceMetadata
from backend.storage import get_project_dir, load_manifest, save_manifest


class ProxyError(RuntimeError):
    """Raised when proxy transcoding or validation fails."""


_NVENC_PROBE_CACHE: dict[str, bool] = {}
_NVENC_PROBE_LOCK = threading.Lock()


def probe_nvenc_support(ffmpeg_path: Path) -> bool:
    """Return True if this FFmpeg binary can encode with h264_nvenc on the current machine."""
    if sys.platform != "win32" and os.environ.get("SIMS_STUDIO_ENABLE_NVENC") != "1":
        return False

    key = str(Path(ffmpeg_path).resolve())
    with _NVENC_PROBE_LOCK:
        cached = _NVENC_PROBE_CACHE.get(key)
    if cached is not None:
        return cached

    supported = False
    try:
        res = subprocess.run(
            [
                str(ffmpeg_path),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=256x256:d=0.1",
                "-frames:v",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ],
            shell=False,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        supported = res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        supported = False

    with _NVENC_PROBE_LOCK:
        _NVENC_PROBE_CACHE[key] = supported
    return supported


def _build_proxy_cmd(
    ffmpeg_path: Path,
    src_path: Path,
    tmp_path: Path,
    *,
    has_audio: bool,
    encoder: str,
) -> list[str]:
    cmd = [str(ffmpeg_path), "-y"]
    if encoder == "h264_nvenc":
        cmd.extend(["-hwaccel", "cuda"])
    cmd.extend(["-i", str(src_path), "-vf", "scale=-2:540,fps=30", "-g", "15"])
    if encoder == "h264_nvenc":
        cmd.extend(["-bf", "0", "-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-preset", "p2", "-cq", "26"])
    else:
        cmd.extend(["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26"])
    cmd.extend(["-movflags", "+faststart"])
    if has_audio:
        cmd.extend(["-c:a", "aac", "-ar", "48000", "-ac", "2"])
    else:
        cmd.append("-an")
    cmd.extend(["-progress", "pipe:1", "-nostats", str(tmp_path)])
    return cmd


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
    asset_id: str | None = None,
    source_duration: float | None = None,
    source_metadata: SourceMetadata | None = None,
    use_nvenc: bool | None = None,
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    data_dir: Path | None = None,
    on_progress: Callable[[int, str], None] | None = None,
    on_process_start: Callable[[subprocess.Popen[Any]], None] | None = None,
    on_encoder_selected: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> SourceMetadata:
    """Transcode a 540p 30fps CFR proxy MP4, validate with ffprobe, and atomically move to output_path."""
    src_path = Path(source_path)
    out_path = Path(output_path)

    def _mark_failed(err_msg: str) -> None:
        if project_id is not None:
            try:
                manifest = load_manifest(project_id, data_dir=data_dir)
                if asset_id is None:
                    manifest.proxy = ProxyState(
                        status="failed",
                        artifact_id=None,
                        path=None,
                        error=err_msg,
                    )
                else:
                    manifest.media_proxies[asset_id] = MediaProxyState(
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

    if source_metadata is not None:
        src_meta = source_metadata
    else:
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

    try_nvenc = probe_nvenc_support(resolved_ffmpeg) if use_nvenc is None else use_nvenc
    encoders_to_try = ["h264_nvenc", "libx264"] if try_nvenc else ["libx264"]

    if on_progress is not None:
        on_progress(0, "Starting proxy transcode")

    proc: subprocess.Popen[str] | None = None
    stderr_lines: list[str] = []

    def _drain_stderr(pipe: Any, target_lines: list[str]) -> None:
        try:
            for line in pipe:
                target_lines.append(line)
        except Exception:
            pass

    try:
        last_err_msg = ""
        for idx, encoder in enumerate(encoders_to_try):
            stderr_lines = []
            cmd = _build_proxy_cmd(
                resolved_ffmpeg,
                src_path,
                tmp_path,
                has_audio=src_meta.has_audio,
                encoder=encoder,
            )
            if on_encoder_selected is not None:
                on_encoder_selected(encoder)
            if idx > 0 and on_progress is not None:
                on_progress(0, f"Falling back to {encoder}")

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

            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(proc.stderr, stderr_lines),
                daemon=True,
            )
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
                                        on_progress(pct, f"Transcoding proxy ({encoder})")
                        except ValueError:
                            pass
                    elif line == "progress=end" and on_progress is not None and last_pct < 95:
                        last_pct = 95
                        on_progress(95, "Validating proxy")

            returncode = proc.wait()
            stderr_thread.join(timeout=2.0)

            if cancel_event is not None and cancel_event.is_set():
                raise ProxyError("Proxy generation cancelled.")

            if returncode == 0:
                break

            err_tail = "".join(stderr_lines[-20:]).strip()
            last_err_msg = f"FFmpeg proxy transcode failed ({encoder}, exit {returncode}): {err_tail}"
            if idx + 1 < len(encoders_to_try):
                tmp_path.unlink(missing_ok=True)
                continue
            _mark_failed(last_err_msg)
            raise ProxyError(last_err_msg)

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
            if asset_id is None:
                if manifest.source_metadata is None:
                    manifest.source_metadata = src_meta
                manifest.proxy = ProxyState(
                    status="ready",
                    artifact_id="art_proxy",
                    path="artifacts/proxy.mp4",
                    error=None,
                )
            else:
                try:
                    proj_dir = get_project_dir(project_id, data_dir=data_dir).resolve()
                    rel_path = out_path.resolve().relative_to(proj_dir).as_posix()
                except Exception:
                    rel_path = f"artifacts/{out_path.name}"
                manifest.media_proxies[asset_id] = MediaProxyState(
                    status="ready",
                    artifact_id=f"art_proxy_{asset_id}",
                    path=rel_path,
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
