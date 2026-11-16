"""Duration planner, content-addressed Stage 1 segment renderer, preview, and Stage 2 concat/burn-in."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from backend.bootstrap import verify_ffmpeg, verify_font
from backend.geometry import build_crop_filter
from backend.schemas import (
    ArtifactManifest,
    CaptionSettings,
    ClauseTiming,
    OutputSettings,
    ProjectInput,
    SegmentInput,
    SourceMediaRef,
    SourceMetadata,
)
from backend.subtitles import SubtitleEvent, build_segment_subtitle_events, generate_ass_file

AUDIO_SAMPLE_RATE = 48000


class RenderError(RuntimeError):
    """Raised when rendering, probing, or concat compatibility validation fails."""


class RenderCancelledError(RenderError):
    """Raised when an active render subprocess is cancelled."""


@dataclass(frozen=True)
class DurationPlan:
    """Frame-quantized duration and freeze-frame plan for a single segment."""

    raw_video_dur: float
    narration_dur: float
    raw_target: float
    frame_count: int
    final_dur: float
    audio_sample_count: int
    moving_source_dur: float
    freeze_frame: bool
    freeze_dur: float


def plan_segment_duration(
    segment: SegmentInput,
    audio_duration: float,
    source_duration: float,
    tail_padding_sec: float,
    fps: int = 30,
) -> DurationPlan:
    """Reconcile video trim vs narration duration and quantize upward to exact output frames."""
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    if tail_padding_sec < 0.0 or not math.isfinite(tail_padding_sec):
        raise ValueError(f"tail_padding_sec must be finite and >= 0, got {tail_padding_sec!r}")
    if source_duration < 0.0 or not math.isfinite(source_duration):
        raise ValueError(f"source_duration must be finite and >= 0, got {source_duration!r}")

    raw_video_dur = float(segment.video_out - segment.video_in)
    narration_dur = max(0.0, float(audio_duration))

    if segment.duration_mode == "preserve_video":
        raw_target = max(raw_video_dur, narration_dur + tail_padding_sec) if narration_dur > 0.0 else raw_video_dur
    elif segment.duration_mode == "fit_narration":
        raw_target = (narration_dur + tail_padding_sec) if narration_dur > 0.0 else raw_video_dur
    else:
        raise ValueError(f"Unsupported duration_mode: {segment.duration_mode!r}")

    frame_count = max(1, math.ceil(raw_target * float(fps)))
    final_dur = float(frame_count) / float(fps)
    audio_sample_count = int(round(final_dur * float(AUDIO_SAMPLE_RATE)))

    available_source_dur = max(0.0, float(source_duration) - float(segment.video_in))
    if segment.duration_mode == "preserve_video":
        moving_source_dur = min(raw_video_dur, available_source_dur)
    else:
        moving_source_dur = min(raw_target, available_source_dur)

    freeze_dur = max(0.0, final_dur - moving_source_dur)
    freeze_frame = freeze_dur > (1.0 / (float(fps) * 2.0))

    return DurationPlan(
        raw_video_dur=raw_video_dur,
        narration_dur=narration_dur,
        raw_target=raw_target,
        frame_count=frame_count,
        final_dur=final_dur,
        audio_sample_count=audio_sample_count,
        moving_source_dur=moving_source_dur,
        freeze_frame=freeze_frame,
        freeze_dur=freeze_dur,
    )


def compute_render_hash(
    segment: SegmentInput,
    source_media: SourceMediaRef,
    tts_hash: str,
    output_settings: OutputSettings,
) -> str:
    """Compute 16-char SHA-256 content hash for Stage 1 caption-free segment rendering.

    Intentionally excludes caption_settings and segment ordering so caption style edits
    or segment reordering never invalidate Stage 1 segment renders.
    """
    payload = {
        "source_fingerprint": source_media.fingerprint,
        "video_in": float(segment.video_in),
        "video_out": float(segment.video_out),
        "duration_mode": segment.duration_mode,
        "source_audio_volume": float(segment.source_audio_volume),
        "crop_keyframes": [
            {
                "t": float(kf.t),
                "x": float(kf.x),
                "y": float(kf.y),
                "zoom": float(kf.zoom),
            }
            for kf in segment.crop_keyframes
        ],
        "tts_hash": str(tts_hash),
        "output_settings": {
            "width": int(output_settings.width),
            "height": int(output_settings.height),
            "fps": int(output_settings.fps),
            "tail_padding_sec": float(output_settings.tail_padding_sec),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def compute_preview_hash(
    render_hash: str,
    tts_hash: str,
    segment: SegmentInput,
    caption_settings: CaptionSettings,
) -> str:
    """Compute 16-char SHA-256 content hash for a single-segment captioned preview."""
    payload = {
        "render_hash": render_hash,
        "tts_hash": tts_hash,
        "display_texts": [c.display_text for c in segment.clauses],
        "caption_settings": caption_settings.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _escape_ffmpeg_filter_path(path: Path) -> str:
    """Escape a filesystem path for two-level FFmpeg filtergraph option parsing."""
    s = str(path.resolve()).replace("\\", "/")
    # Level 1 (filter option value): escape \, :, '
    s = s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    # Level 2 (filtergraph parser): escape \, :, ', ,, ;, [, ], space
    for ch in ("\\", ":", "'", ",", ";", "[", "]", " "):
        s = s.replace(ch, "\\" + ch)
    return s


def _escape_concat_path(path: Path) -> str:
    """Escape a filesystem path for FFmpeg concat demuxer `file '...'` lines."""
    return str(path.resolve()).replace("'", r"'\''")


def _parse_fps_fraction(raw: str | None) -> float:
    if not raw or raw == "0/0":
        return 0.0
    if "/" in raw:
        num_s, den_s = raw.split("/", 1)
        num = float(num_s)
        den = float(den_s)
        return (num / den) if den != 0.0 else 0.0
    return float(raw)


def _run_ffmpeg_cancellable(
    cmd: list[str],
    cancel_event: threading.Event | None = None,
    on_process_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> None:
    """Execute an FFmpeg subprocess without a shell, supporting cooperative cancellation."""
    if cancel_event is not None and cancel_event.is_set():
        raise RenderCancelledError("Render cancelled before start")

    proc = subprocess.Popen(
        cmd,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if on_process_start is not None:
        on_process_start(proc)

    while True:
        if cancel_event is not None and cancel_event.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise RenderCancelledError("Render cancelled during execution")

        try:
            _stdout, stderr_bytes = proc.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            continue

    if proc.returncode != 0:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        tail = "\n".join(stderr_text.splitlines()[-15:]) if stderr_text else "(no stderr)"
        raise RenderError(f"FFmpeg exited with code {proc.returncode}: {tail}")


def probe_rendered_video(
    video_path: Path,
    ffprobe_path: Path,
    *,
    expected_width: int = 1080,
    expected_height: int = 1920,
    expected_fps: int = 30,
    require_audio: bool = True,
) -> dict[str, Any]:
    """Validate a rendered MP4 via ffprobe and return normalized stream metadata."""
    if not video_path.is_file():
        raise RenderError(f"Rendered video missing: {video_path}")

    cmd = [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(video_path),
    ]
    res = subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if res.returncode != 0:
        raise RenderError(f"ffprobe failed on {video_path}: {res.stderr.strip()}")

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError(f"Invalid ffprobe JSON for {video_path}: {exc}") from exc

    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise RenderError(f"No video stream found in {video_path}")

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    pix_fmt = str(video_stream.get("pix_fmt", ""))
    sar = str(video_stream.get("sample_aspect_ratio", "1:1"))
    fps_val = _parse_fps_fraction(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    nb_frames_raw = video_stream.get("nb_frames")
    nb_frames = int(nb_frames_raw) if nb_frames_raw and str(nb_frames_raw).isdigit() else None

    if width != expected_width or height != expected_height:
        raise RenderError(
            f"Resolution mismatch in {video_path}: expected {expected_width}x{expected_height}, got {width}x{height}"
        )
    if pix_fmt != "yuv420p":
        raise RenderError(f"Pixel format mismatch in {video_path}: expected yuv420p, got {pix_fmt!r}")
    if sar not in ("1:1", "N/A", ""):
        raise RenderError(f"SAR mismatch in {video_path}: expected 1:1, got {sar!r}")
    if abs(fps_val - float(expected_fps)) > 0.05:
        raise RenderError(
            f"FPS mismatch in {video_path}: expected {expected_fps}, got {fps_val:.3f}"
        )

    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    sample_rate: int | None = None
    channels: int | None = None
    if require_audio:
        if audio_stream is None:
            raise RenderError(f"No audio stream found in {video_path}")
        sample_rate = int(audio_stream.get("sample_rate", 0))
        channels = int(audio_stream.get("channels", 0))
        if sample_rate != AUDIO_SAMPLE_RATE:
            raise RenderError(
                f"Audio sample rate mismatch in {video_path}: expected {AUDIO_SAMPLE_RATE}, got {sample_rate}"
            )
        if channels != 2:
            raise RenderError(f"Audio channel count mismatch in {video_path}: expected 2, got {channels}")
    elif audio_stream is not None:
        sample_rate = int(audio_stream.get("sample_rate", 0))
        channels = int(audio_stream.get("channels", 0))

    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0.0))

    return {
        "width": width,
        "height": height,
        "fps": fps_val,
        "pix_fmt": pix_fmt,
        "sample_aspect_ratio": sar,
        "sample_rate": sample_rate,
        "channels": channels,
        "duration": duration,
        "nb_frames": nb_frames,
    }


def _resolve_ffmpeg_tools(
    ffmpeg_path: Path | None,
    ffprobe_path: Path | None,
) -> tuple[Path, Path]:
    if ffmpeg_path is not None and ffprobe_path is not None:
        return ffmpeg_path, ffprobe_path
    verified_ffmpeg, verified_ffprobe = verify_ffmpeg()
    return (
        ffmpeg_path if ffmpeg_path is not None else verified_ffmpeg,
        ffprobe_path if ffprobe_path is not None else verified_ffprobe,
    )


def render_segment_stage1(
    segment: SegmentInput,
    source_media: SourceMediaRef,
    source_metadata: SourceMetadata,
    output_settings: OutputSettings,
    project_dir: Path,
    tts_hash: str = "",
    audio_path: Path | None = None,
    audio_duration: float = 0.0,
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    cancel_event: threading.Event | None = None,
    on_process_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> Path:
    """Render a caption-free Stage 1 segment MP4 with content-addressed caching."""
    ffmpeg_bin, ffprobe_bin = _resolve_ffmpeg_tools(ffmpeg_path, ffprobe_path)

    render_hash = compute_render_hash(
        segment=segment,
        source_media=source_media,
        tts_hash=tts_hash,
        output_settings=output_settings,
    )
    artifacts_dir = project_dir / "artifacts"
    tmp_dir = project_dir / "tmp"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    output_path = artifacts_dir / f"seg_{segment.id}_{render_hash}.mp4"
    if output_path.is_file():
        try:
            probe_rendered_video(
                output_path,
                ffprobe_bin,
                expected_width=output_settings.width,
                expected_height=output_settings.height,
                expected_fps=output_settings.fps,
                require_audio=True,
            )
            return output_path
        except RenderError:
            output_path.unlink(missing_ok=True)

    plan = plan_segment_duration(
        segment=segment,
        audio_duration=audio_duration,
        source_duration=source_metadata.duration,
        tail_padding_sec=output_settings.tail_padding_sec,
        fps=output_settings.fps,
    )
    if plan.moving_source_dur <= 0.0:
        raise RenderError(
            f"Segment {segment.id!r} has no available source footage at video_in={segment.video_in}"
        )

    crop_duration = max(plan.raw_video_dur, plan.moving_source_dur, plan.final_dur)
    crop_filter = build_crop_filter(
        segment.crop_keyframes,
        width=source_metadata.width,
        height=source_metadata.height,
        duration=crop_duration,
        fps=output_settings.fps,
        output_width=output_settings.width,
        output_height=output_settings.height,
    )

    video_chain_parts = [
        "[0:v]setpts=PTS-STARTPTS",
        crop_filter,
    ]
    if plan.freeze_frame or plan.final_dur > plan.moving_source_dur:
        video_chain_parts.append(f"tpad=stop_mode=clone:stop_duration={plan.freeze_dur + 0.25:.6f}")
    else:
        video_chain_parts.append("tpad=stop_mode=clone:stop_duration=0.250000")

    video_chain_parts.append(
        f"fps={output_settings.fps},trim=duration={plan.final_dur:.6f},setpts=PTS-STARTPTS,format=yuv420p[vout]"
    )
    video_chain = ",".join(video_chain_parts)

    has_tts = audio_path is not None and audio_path.is_file() and plan.narration_dur > 0.0
    audio_chains: list[str] = []
    if source_metadata.has_audio:
        audio_chains.append(
            f"[0:a]atrim=duration={plan.moving_source_dur:.6f},asetpts=PTS-STARTPTS,"
            f"aresample={AUDIO_SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={segment.source_audio_volume:.6f},apad=whole_dur={plan.final_dur:.6f}[srca]"
        )
    else:
        audio_chains.append(
            f"anullsrc=r={AUDIO_SAMPLE_RATE}:cl=stereo:d={plan.final_dur:.6f}[srca]"
        )

    if has_tts:
        audio_chains.append(
            f"[1:a]aresample={AUDIO_SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad=whole_dur={plan.final_dur:.6f}[ttsa]"
        )
        audio_chains.append(
            f"[srca][ttsa]amix=inputs=2:duration=first:normalize=0,"
            f"alimiter=limit=0.95,atrim=duration={plan.final_dur:.6f},asetpts=PTS-STARTPTS[aout]"
        )
    else:
        audio_chains.append(
            f"[srca]alimiter=limit=0.95,atrim=duration={plan.final_dur:.6f},asetpts=PTS-STARTPTS[aout]"
        )

    filter_script_content = ";\n".join([video_chain, *audio_chains])
    unique_tag = uuid.uuid4().hex[:8]
    filter_script_path = tmp_dir / f"seg_{segment.id}_{render_hash}_{unique_tag}_filter.txt"
    tmp_out = tmp_dir / f"seg_{segment.id}_{render_hash}_{unique_tag}.mp4"

    try:
        filter_script_path.write_text(filter_script_content, encoding="utf-8")

        cmd = [
            str(ffmpeg_bin),
            "-y",
            "-hide_banner",
            "-ss",
            f"{segment.video_in:.6f}",
            "-t",
            f"{plan.moving_source_dur:.6f}",
            "-i",
            str(source_media.path),
        ]
        if has_tts:
            cmd.extend(["-i", str(audio_path)])

        cmd.extend(
            [
                "-filter_complex_script",
                str(filter_script_path),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(output_settings.fps),
                "-fps_mode",
                "cfr",
                "-video_track_timescale",
                "15360",
                "-frames:v",
                str(plan.frame_count),
                "-c:a",
                "aac",
                "-ar",
                str(AUDIO_SAMPLE_RATE),
                "-ac",
                "2",
                "-b:a",
                "192k",
                str(tmp_out),
            ]
        )

        _run_ffmpeg_cancellable(cmd, cancel_event=cancel_event, on_process_start=on_process_start)

        probe_rendered_video(
            tmp_out,
            ffprobe_bin,
            expected_width=output_settings.width,
            expected_height=output_settings.height,
            expected_fps=output_settings.fps,
            require_audio=True,
        )
        os.replace(tmp_out, output_path)
        return output_path
    except Exception:
        tmp_out.unlink(missing_ok=True)
        raise
    finally:
        filter_script_path.unlink(missing_ok=True)


def render_segment_preview(
    segment: SegmentInput,
    source_media: SourceMediaRef,
    source_metadata: SourceMetadata,
    output_settings: OutputSettings,
    caption_settings: CaptionSettings,
    project_dir: Path,
    tts_hash: str = "",
    audio_path: Path | None = None,
    audio_duration: float = 0.0,
    clause_timings: Sequence[ClauseTiming] = (),
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    fonts_dir: Path | None = None,
    cancel_event: threading.Event | None = None,
    on_process_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> Path:
    """Render a full-resolution single-segment MP4 preview with burned-in .ass captions."""
    ffmpeg_bin, ffprobe_bin = _resolve_ffmpeg_tools(ffmpeg_path, ffprobe_path)
    resolved_fonts_dir = fonts_dir if fonts_dir is not None else verify_font().parent

    stage1_path = render_segment_stage1(
        segment=segment,
        source_media=source_media,
        source_metadata=source_metadata,
        output_settings=output_settings,
        project_dir=project_dir,
        tts_hash=tts_hash,
        audio_path=audio_path,
        audio_duration=audio_duration,
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
        cancel_event=cancel_event,
        on_process_start=on_process_start,
    )

    render_hash = compute_render_hash(
        segment=segment,
        source_media=source_media,
        tts_hash=tts_hash,
        output_settings=output_settings,
    )
    preview_hash = compute_preview_hash(
        render_hash=render_hash,
        tts_hash=tts_hash,
        segment=segment,
        caption_settings=caption_settings,
    )

    artifacts_dir = project_dir / "artifacts"
    tmp_dir = project_dir / "tmp"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    preview_path = artifacts_dir / f"preview_{segment.id}_{preview_hash}.mp4"
    if preview_path.is_file():
        try:
            probe_rendered_video(
                preview_path,
                ffprobe_bin,
                expected_width=output_settings.width,
                expected_height=output_settings.height,
                expected_fps=output_settings.fps,
                require_audio=True,
            )
            return preview_path
        except RenderError:
            preview_path.unlink(missing_ok=True)

    events = build_segment_subtitle_events(
        clauses=segment.clauses,
        clause_timings=clause_timings,
        segment_offset_sec=0.0,
    )
    unique_tag = uuid.uuid4().hex[:8]
    ass_path = tmp_dir / f"preview_{segment.id}_{preview_hash}_{unique_tag}.ass"
    tmp_out = tmp_dir / f"preview_{segment.id}_{preview_hash}_{unique_tag}.mp4"

    try:
        generate_ass_file(
            events=events,
            caption_settings=caption_settings,
            output_path=ass_path,
            width=output_settings.width,
            height=output_settings.height,
        )

        ass_filter = (
            f"ass=filename={_escape_ffmpeg_filter_path(ass_path)}"
            f":fontsdir={_escape_ffmpeg_filter_path(resolved_fonts_dir)}"
        )
        cmd = [
            str(ffmpeg_bin),
            "-y",
            "-hide_banner",
            "-i",
            str(stage1_path),
            "-vf",
            ass_filter,
            "-c:v",
            "libx264",
            "-crf",
            "19",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(output_settings.fps),
            "-fps_mode",
            "cfr",
            "-movflags",
            "+faststart",
            "-c:a",
            "copy",
            str(tmp_out),
        ]

        _run_ffmpeg_cancellable(cmd, cancel_event=cancel_event, on_process_start=on_process_start)
        probe_rendered_video(
            tmp_out,
            ffprobe_bin,
            expected_width=output_settings.width,
            expected_height=output_settings.height,
            expected_fps=output_settings.fps,
            require_audio=True,
        )
        os.replace(tmp_out, preview_path)
        return preview_path
    except Exception:
        tmp_out.unlink(missing_ok=True)
        raise
    finally:
        ass_path.unlink(missing_ok=True)


def verify_concat_compatibility(
    segment_paths: Sequence[Path],
    ffprobe_path: Path,
    *,
    expected_width: int = 1080,
    expected_height: int = 1920,
    expected_fps: int = 30,
) -> None:
    """Verify all ordered Stage 1 segment MP4s share identical stream parameters before concat."""
    if not segment_paths:
        raise RenderError("No segment paths provided for concat compatibility check")

    baseline: dict[str, Any] | None = None
    baseline_path: Path | None = None

    for seg_path in segment_paths:
        info = probe_rendered_video(
            seg_path,
            ffprobe_path,
            expected_width=expected_width,
            expected_height=expected_height,
            expected_fps=expected_fps,
            require_audio=True,
        )
        if baseline is None:
            baseline = info
            baseline_path = seg_path
            continue

        for key in ("width", "height", "pix_fmt", "sample_rate", "channels"):
            if info[key] != baseline[key]:
                raise RenderError(
                    f"Concat stream mismatch on {seg_path} vs {baseline_path} for {key}: "
                    f"{info[key]!r} != {baseline[key]!r}"
                )
        if abs(float(info["fps"]) - float(baseline["fps"])) > 0.05:
            raise RenderError(
                f"Concat FPS mismatch on {seg_path} vs {baseline_path}: "
                f"{info['fps']:.3f} != {baseline['fps']:.3f}"
            )


def render_final_export(
    project: ProjectInput,
    source_metadata: SourceMetadata,
    manifest: ArtifactManifest,
    project_dir: Path,
    export_path: Path | None = None,
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    fonts_dir: Path | None = None,
    cancel_event: threading.Event | None = None,
    on_process_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> Path:
    """Render Stage 1 segments (reusing cache), verify concat compatibility, concat, and burn in master .ass."""
    if not project.segments:
        raise RenderError("Project has no segments to export")

    ffmpeg_bin, ffprobe_bin = _resolve_ffmpeg_tools(ffmpeg_path, ffprobe_path)
    resolved_fonts_dir = fonts_dir if fonts_dir is not None else verify_font().parent

    exports_dir = project_dir / "exports"
    tmp_dir = project_dir / "tmp"
    exports_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    final_export_path = (
        export_path if export_path is not None else exports_dir / f"{project.project_id}_export.mp4"
    )
    final_export_path.parent.mkdir(parents=True, exist_ok=True)

    stage1_paths: list[Path] = []
    all_events: list[SubtitleEvent] = []
    offset_sec = 0.0
    total_frames = 0

    for segment in project.segments:
        seg_art = manifest.segments.get(segment.id)
        tts_hash = (seg_art.tts_hash or "") if seg_art is not None else ""
        audio_duration = seg_art.audio_duration if seg_art is not None else 0.0
        clause_timings = seg_art.clause_timings if seg_art is not None else []

        resolved_audio_path: Path | None = None
        if seg_art is not None and seg_art.audio_path:
            raw_audio = Path(seg_art.audio_path)
            resolved_audio_path = raw_audio if raw_audio.is_absolute() else (project_dir / raw_audio)

        plan = plan_segment_duration(
            segment=segment,
            audio_duration=audio_duration,
            source_duration=source_metadata.duration,
            tail_padding_sec=project.output_settings.tail_padding_sec,
            fps=project.output_settings.fps,
        )

        seg_mp4 = render_segment_stage1(
            segment=segment,
            source_media=project.source_media,
            source_metadata=source_metadata,
            output_settings=project.output_settings,
            project_dir=project_dir,
            tts_hash=tts_hash,
            audio_path=resolved_audio_path,
            audio_duration=audio_duration,
            ffmpeg_path=ffmpeg_bin,
            ffprobe_path=ffprobe_bin,
            cancel_event=cancel_event,
            on_process_start=on_process_start,
        )
        stage1_paths.append(seg_mp4)

        if segment.clauses and clause_timings:
            all_events.extend(
                build_segment_subtitle_events(
                    clauses=segment.clauses,
                    clause_timings=clause_timings,
                    segment_offset_sec=offset_sec,
                )
            )
        offset_sec += plan.final_dur
        total_frames += plan.frame_count

    verify_concat_compatibility(
        stage1_paths,
        ffprobe_bin,
        expected_width=project.output_settings.width,
        expected_height=project.output_settings.height,
        expected_fps=project.output_settings.fps,
    )

    unique_tag = uuid.uuid4().hex[:8]
    master_ass_path = tmp_dir / f"{project.project_id}_master_{unique_tag}.ass"
    concat_list_path = tmp_dir / f"{project.project_id}_concat_{unique_tag}.txt"
    tmp_out = tmp_dir / f"{project.project_id}_export_{unique_tag}.mp4"

    try:
        generate_ass_file(
            events=all_events,
            caption_settings=project.caption_settings,
            output_path=master_ass_path,
            width=project.output_settings.width,
            height=project.output_settings.height,
        )

        concat_lines = [f"file '{_escape_concat_path(p)}'\n" for p in stage1_paths]
        concat_list_path.write_text("".join(concat_lines), encoding="utf-8")

        ass_filter = (
            f"ass=filename={_escape_ffmpeg_filter_path(master_ass_path)}"
            f":fontsdir={_escape_ffmpeg_filter_path(resolved_fonts_dir)},"
            f"trim=end_frame={total_frames},setpts=PTS-STARTPTS"
        )
        cmd = [
            str(ffmpeg_bin),
            "-y",
            "-hide_banner",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_path),
            "-vf",
            ass_filter,
            "-c:v",
            "libx264",
            "-crf",
            "19",
            "-preset",
            "medium",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(project.output_settings.fps),
            "-fps_mode",
            "cfr",
            "-movflags",
            "+faststart",
            "-af",
            (
                "apad=pad_dur=0.5,"
                f"atrim=duration={offset_sec + (1024.0 / float(AUDIO_SAMPLE_RATE)):.6f},"
                "asetpts=PTS-STARTPTS"
            ),
            "-c:a",
            "aac",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-ac",
            "2",
            "-b:a",
            "192k",
            str(tmp_out),
        ]

        _run_ffmpeg_cancellable(cmd, cancel_event=cancel_event, on_process_start=on_process_start)
        probe_rendered_video(
            tmp_out,
            ffprobe_bin,
            expected_width=project.output_settings.width,
            expected_height=project.output_settings.height,
            expected_fps=project.output_settings.fps,
            require_audio=True,
        )
        os.replace(tmp_out, final_export_path)
        return final_export_path
    except Exception:
        tmp_out.unlink(missing_ok=True)
        raise
    finally:
        master_ass_path.unlink(missing_ok=True)
        concat_list_path.unlink(missing_ok=True)
