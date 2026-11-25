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
from backend.geometry import build_crop_filter, round_even, slice_camera_keyframes_for_segment
from backend.media import probe_and_validate_asset
from backend.schemas import (
    ArtifactManifest,
    AudioTrack,
    CaptionSettings,
    CaptionTrack,
    ClauseTiming,
    CropKeyframe,
    MediaAssetMetadata,
    MediaAssetRef,
    OutputSettings,
    ProjectInput,
    SegmentInput,
    SourceMediaRef,
    SourceMetadata,
    TrackTransition,
    VideoTrack,
    VisualClip,
)
from backend.subtitles import (
    SubtitleEvent,
    build_segment_subtitle_events,
    build_timeline_subtitle_events,
    generate_ass_file,
)
from backend.tts import compute_caption_tts_hash, mix_caption_tracks_audio

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
                "transition": kf.transition,
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
        "ass_wrap_v3": True,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def compute_project_preview_hash(
    project: ProjectInput,
    manifest: ArtifactManifest,
) -> str:
    """Compute 16-char SHA-256 content hash for a full-project captioned preview."""
    seg_entries = []
    for seg in project.segments:
        seg_art = manifest.segments.get(seg.id)
        tts_hash = (seg_art.tts_hash or "") if seg_art is not None else ""
        render_hash = compute_render_hash(
            segment=seg,
            source_media=project.source_media,
            tts_hash=tts_hash,
            output_settings=project.output_settings,
        )
        seg_entries.append(
            {
                "id": seg.id,
                "render_hash": render_hash,
                "tts_hash": tts_hash,
                "display_texts": [c.display_text for c in seg.clauses],
                "clause_timings": [
                    {"id": c.id, "start_sec": c.start_sec, "duration_sec": c.duration_sec}
                    for c in seg.clauses
                ],
            }
        )
    payload = {
        "segments": seg_entries,
        "camera_keyframes": [
            {
                "t": round(float(kf.t), 6),
                "x": round(float(kf.x), 6),
                "y": round(float(kf.y), 6),
                "zoom": round(float(kf.zoom), 4),
                "transition": kf.transition,
            }
            for kf in project.camera_keyframes
        ],
        "output_settings": project.output_settings.model_dump(mode="json"),
        "caption_settings": project.caption_settings.model_dump(mode="json"),
        "ass_wrap_v3": True,
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


def _is_multitrack_project(project: ProjectInput) -> bool:
    """Return True when project uses timeline tracks or has 0 legacy segments."""
    if any(len(t.clips) > 0 for t in project.tracks):
        return True
    return len(project.segments) == 0


def _resolve_caption_clip_dur(clip: Any, manifest: ArtifactManifest) -> float:
    if not clip.auto_fit_tts and clip.duration_sec is not None and clip.duration_sec > 0.0:
        return float(clip.duration_sec)
    if clip.tts_enabled:
        art = manifest.caption_artifacts.get(clip.id)
        if art is not None and art.audio_duration > 0.0:
            return float(art.audio_duration)
    if clip.duration_sec is not None and clip.duration_sec > 0.0:
        return float(clip.duration_sec)
    words = max(1, len(str(clip.display_text).split()))
    return max(1.2, min(8.0, float(words) * 0.42))


def _resolve_asset_and_metadata(
    project: ProjectInput,
    manifest: ArtifactManifest,
    asset_id: str,
    asset_cache: dict[str, tuple[MediaAssetRef, MediaAssetMetadata]],
    ffmpeg_bin: Path,
    ffprobe_bin: Path,
) -> tuple[MediaAssetRef, MediaAssetMetadata]:
    if asset_id in asset_cache:
        return asset_cache[asset_id]

    ref = project.media_assets.get(asset_id)
    if ref is None and asset_id == project.source_media.media_id:
        sm = project.source_media
        ref = MediaAssetRef(
            asset_id=sm.media_id,
            kind="video",
            name=Path(sm.path).name or "Primary Video",
            path=sm.path,
            fingerprint=sm.fingerprint,
            copied_to_project=sm.copied_to_project,
        )
    if ref is None:
        raise RenderError(f"Referenced media asset {asset_id!r} not found in project")

    meta = manifest.media_metadata.get(asset_id)
    if meta is None and asset_id == project.source_media.media_id and manifest.source_metadata is not None:
        sm_meta = manifest.source_metadata
        meta = MediaAssetMetadata(
            kind="video",
            width=sm_meta.width,
            height=sm_meta.height,
            fps=sm_meta.fps,
            duration=sm_meta.duration,
            has_audio=sm_meta.has_audio,
            has_alpha=False,
            codec_name=sm_meta.codec_name,
            pix_fmt=sm_meta.pix_fmt,
            audio_sample_rate=sm_meta.audio_sample_rate,
            audio_channels=sm_meta.audio_channels,
        )
    if meta is None:
        try:
            meta = probe_and_validate_asset(
                ref.path,
                expected_kind=ref.kind,
                ffmpeg_path=ffmpeg_bin,
                ffprobe_path=ffprobe_bin,
            )
        except Exception as exc:
            raise RenderError(f"Failed to probe asset {asset_id!r} at {ref.path}: {exc}") from exc

    asset_cache[asset_id] = (ref, meta)
    return ref, meta


def _group_video_track_runs(
    track: VideoTrack,
) -> list[tuple[list[VisualClip], list[TrackTransition]]]:
    """Group sorted VisualClips into contiguous runs connected by TrackTransitions."""
    clips = sorted(track.clips, key=lambda c: (c.start_sec, c.id))
    if not clips:
        return []
    tr_lookup: dict[tuple[str, str], TrackTransition] = {
        (t.from_clip_id, t.to_clip_id): t for t in track.transitions
    }

    runs: list[tuple[list[VisualClip], list[TrackTransition]]] = []
    cur_clips = [clips[0]]
    cur_trs: list[TrackTransition] = []

    for nxt in clips[1:]:
        prev = cur_clips[-1]
        prev_end = prev.start_sec + prev.duration_sec
        gap = nxt.start_sec - prev_end
        tr = tr_lookup.get((prev.id, nxt.id))
        if tr is not None and gap <= 0.12 and gap >= -min(prev.duration_sec, nxt.duration_sec) + 0.05:
            cur_clips.append(nxt)
            cur_trs.append(tr)
        else:
            runs.append((cur_clips, cur_trs))
            cur_clips = [nxt]
            cur_trs = []
    runs.append((cur_clips, cur_trs))
    return runs


def get_project_timeline_duration(
    project: ProjectInput,
    manifest: ArtifactManifest,
) -> tuple[float, int, float]:
    """Compute (raw_dur, frame_count, final_dur) across all tracks and camera keyframes."""
    if not _is_multitrack_project(project):
        # Legacy mode
        fps = max(1, project.output_settings.fps)
        src_dur = manifest.source_metadata.duration if manifest.source_metadata is not None else 0.0
        raw_total = 0.0
        total_frames = 0
        for seg in project.segments:
            seg_art = manifest.segments.get(seg.id)
            aud_dur = seg_art.audio_duration if seg_art is not None else 0.0
            plan = plan_segment_duration(
                seg,
                audio_duration=aud_dur,
                source_duration=src_dur,
                tail_padding_sec=project.output_settings.tail_padding_sec,
                fps=fps,
            )
            raw_total += plan.final_dur
            total_frames += plan.frame_count
        return raw_total, total_frames, raw_total

    raw_dur = 0.0
    for kf in project.camera_keyframes:
        raw_dur = max(raw_dur, float(kf.t))

    for track in project.tracks:
        if isinstance(track, VideoTrack):
            for run_clips, run_trs in _group_video_track_runs(track):
                if len(run_clips) == 1:
                    raw_dur = max(raw_dur, run_clips[0].start_sec + run_clips[0].duration_sec)
                else:
                    run_start = run_clips[0].start_sec
                    cur_dur = run_clips[0].duration_sec
                    for idx, nxt in enumerate(run_clips[1:]):
                        tr = run_trs[idx]
                        overlap = max(0.0, (run_start + cur_dur) - nxt.start_sec)
                        if overlap >= 0.05:
                            offset = max(0.0, nxt.start_sec - run_start)
                        else:
                            eff_d = min(tr.duration_sec, cur_dur * 0.45, nxt.duration_sec * 0.45)
                            offset = max(0.0, cur_dur - eff_d)
                        cur_dur = offset + nxt.duration_sec
                    raw_dur = max(raw_dur, run_start + cur_dur)
        elif isinstance(track, AudioTrack):
            for clip in track.clips:
                raw_dur = max(raw_dur, clip.start_sec + clip.duration_sec)
        elif isinstance(track, CaptionTrack):
            for clip in track.clips:
                raw_dur = max(raw_dur, clip.start_sec + _resolve_caption_clip_dur(clip, manifest))

    if raw_dur <= 0.0:
        return 0.0, 0, 0.0

    fps = max(1, project.output_settings.fps)
    frame_count = max(1, math.ceil(raw_dur * float(fps)))
    final_dur = float(frame_count) / float(fps)
    return raw_dur, frame_count, final_dur


def compute_timeline_render_hash(
    project: ProjectInput,
    manifest: ArtifactManifest,
) -> str:
    """Compute canonical 16-char hash for full multi-track project preview/export caching."""
    cap_hashes: dict[str, str] = {}
    cap_artifacts: dict[str, tuple[str, float]] = {}
    for tr in project.tracks:
        if isinstance(tr, CaptionTrack):
            for clip in tr.clips:
                cap_hashes[clip.id] = compute_caption_tts_hash(
                    clip,
                    project.pronunciations,
                    project.voice,
                    project.language,
                )
                art = manifest.caption_artifacts.get(clip.id)
                if art is not None:
                    cap_artifacts[clip.id] = (
                        art.tts_hash or "",
                        round(float(art.audio_duration or 0.0), 4),
                    )

    asset_fps = {aid: ref.fingerprint for aid, ref in sorted(project.media_assets.items())}
    payload = {
        "source_fp": project.source_media.fingerprint,
        "asset_fps": asset_fps,
        "tracks": [t.model_dump(mode="json") for t in project.tracks],
        "camera_keyframes": [kf.model_dump(mode="json") for kf in project.camera_keyframes],
        "output_settings": project.output_settings.model_dump(mode="json"),
        "caption_settings": project.caption_settings.model_dump(mode="json"),
        "voice": project.voice,
        "language": project.language,
        "pronunciations": sorted(project.pronunciations.items()),
        "caption_hashes": sorted(cap_hashes.items()),
        "caption_artifacts": sorted(cap_artifacts.items()),
        "multitrack_v1": True,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _build_atempo_filters(speed: float) -> list[str]:
    s = float(speed)
    parts: list[str] = []
    while s < 0.5 - 1e-6:
        parts.append("atempo=0.5")
        s /= 0.5
    while s > 2.0 + 1e-6:
        parts.append("atempo=2.0")
        s /= 2.0
    if abs(s - 1.0) > 1e-4:
        parts.append(f"atempo={s:.6f}")
    return parts


def _build_slide_offset_expr(
    edge_in_type: str,
    d_in: float,
    edge_out_type: str,
    d_out: float,
    start_sec: float,
    end_sec: float,
    axis: str,
) -> str:
    """Return additive x or y expression term (in canvas W/H units) for slide transitions."""
    dim = "W" if axis == "x" else "H"
    terms: list[str] = []
    if d_in > 1e-4:
        if axis == "x":
            if edge_in_type == "slide_left":
                terms.append(f"if(lt(t,{start_sec + d_in:.6f}),(1-(t-{start_sec:.6f})/{d_in:.6f})*{dim},0)")
            elif edge_in_type == "slide_right":
                terms.append(f"if(lt(t,{start_sec + d_in:.6f}),-(1-(t-{start_sec:.6f})/{d_in:.6f})*{dim},0)")
        else:
            if edge_in_type == "slide_up":
                terms.append(f"if(lt(t,{start_sec + d_in:.6f}),(1-(t-{start_sec:.6f})/{d_in:.6f})*{dim},0)")
            elif edge_in_type == "slide_down":
                terms.append(f"if(lt(t,{start_sec + d_in:.6f}),-(1-(t-{start_sec:.6f})/{d_in:.6f})*{dim},0)")
    if d_out > 1e-4:
        out_st = max(start_sec, end_sec - d_out)
        if axis == "x":
            if edge_out_type == "slide_left":
                terms.append(f"if(gt(t,{out_st:.6f}),-((t-{out_st:.6f})/{d_out:.6f})*{dim},0)")
            elif edge_out_type == "slide_right":
                terms.append(f"if(gt(t,{out_st:.6f}),((t-{out_st:.6f})/{d_out:.6f})*{dim},0)")
        else:
            if edge_out_type == "slide_up":
                terms.append(f"if(gt(t,{out_st:.6f}),-((t-{out_st:.6f})/{d_out:.6f})*{dim},0)")
            elif edge_out_type == "slide_down":
                terms.append(f"if(gt(t,{out_st:.6f}),((t-{out_st:.6f})/{d_out:.6f})*{dim},0)")
    return "+".join(terms) if terms else "0"


def render_timeline_composite(
    project: ProjectInput,
    source_metadata: SourceMetadata | None,
    manifest: ArtifactManifest,
    project_dir: Path,
    export_path: Path | None = None,
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    fonts_dir: Path | None = None,
    cancel_event: threading.Event | None = None,
    on_process_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
    crf: int = 19,
    fps_override: int | None = None,
) -> Path:
    """Composite multi-track video/image/camera/audio/caption timeline into an MP4."""
    raw_dur, frame_count, final_dur = get_project_timeline_duration(project, manifest)
    if raw_dur <= 0.0 or frame_count <= 0:
        raise RenderError("Project timeline has zero duration; nothing to render")

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

    out_w = round_even(float(project.output_settings.width))
    out_h = round_even(float(project.output_settings.height))
    fps = max(1, int(fps_override if fps_override is not None else project.output_settings.fps))
    if fps_override is not None and fps != int(project.output_settings.fps):
        frame_count = max(1, math.ceil(raw_dur * fps))
        final_dur = frame_count / float(fps)

    unique_tag = uuid.uuid4().hex[:8]
    master_ass_path = tmp_dir / f"{project.project_id}_timeline_{unique_tag}.ass"
    mixed_tts_wav_path = tmp_dir / f"{project.project_id}_tts_{unique_tag}.wav"
    filter_script_path = tmp_dir / f"{project.project_id}_composite_{unique_tag}.txt"
    tmp_out = tmp_dir / f"{project.project_id}_composite_{unique_tag}.mp4"

    asset_cache: dict[str, tuple[MediaAssetRef, MediaAssetMetadata]] = {}
    filter_lines: list[str] = []
    node_seq = 0

    def _next_lbl(prefix: str) -> str:
        nonlocal node_seq
        node_seq += 1
        return f"{prefix}_{node_seq}"

    try:
        # 1. Master mixed TTS track + subtitle events
        events = build_timeline_subtitle_events(project.tracks, manifest.caption_artifacts)
        generate_ass_file(
            events=events,
            caption_settings=project.caption_settings,
            output_path=master_ass_path,
            width=out_w,
            height=out_h,
        )

        tts_wav, _speaking_intervals = mix_caption_tracks_audio(
            tracks=project.tracks,
            caption_artifacts=manifest.caption_artifacts,
            project_dir=project_dir,
            total_duration_sec=final_dur,
            output_wav_path=mixed_tts_wav_path,
        )

        # 2. Base RGBA canvas across exact project duration
        canvas_lbl = _next_lbl("canvas")
        filter_lines.append(
            f"color=c=black:s={out_w}x{out_h}:r={fps}:d={final_dur:.6f},format=rgba[{canvas_lbl}]"
        )

        base_audio_labels: list[str] = []
        ducked_audio_labels: list[str] = []
        input_specs: list[list[str]] = []

        def _add_input(args: list[str]) -> int:
            idx = len(input_specs)
            input_specs.append(args)
            return idx

        def _build_prepared_clip(
            track: VideoTrack,
            clip: VisualClip,
            *,
            pad_to_full_canvas: bool,
        ) -> tuple[str, str | None, int, int, float, float, float]:
            asset_ref, asset_meta = _resolve_asset_and_metadata(
                project, manifest, clip.asset_id, asset_cache, ffmpeg_bin, ffprobe_bin
            )
            asset_path = Path(asset_ref.path)
            if not asset_path.is_file():
                raise RenderError(f"Media file missing for clip {clip.id!r}: {asset_path}")

            asset_w = int(asset_meta.width or out_w)
            asset_h = int(asset_meta.height or out_h)
            dur = float(clip.duration_sec)
            speed = float(clip.speed)

            if clip.kind == "image" or asset_meta.kind == "image":
                in_idx = _add_input(
                    ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.6f}", "-i", str(asset_path)]
                )
                src_v_expr = f"[{in_idx}:v]fps={fps},setpts=PTS-STARTPTS"
                has_clip_audio = False
                slice_dur = dur
            else:
                avail_dur = max(0.05, float(asset_meta.duration) - float(clip.media_in))
                trim_end = float(clip.media_out) if clip.media_out is not None else float(asset_meta.duration)
                slice_dur = max(0.02, min(avail_dur, trim_end - float(clip.media_in)))
                need_src_dur = dur * speed
                read_dur = slice_dur if clip.end_behavior == "loop" else min(slice_dur, need_src_dur + 0.1)

                in_idx = _add_input(
                    ["-ss", f"{clip.media_in:.6f}", "-t", f"{max(0.02, read_dur):.6f}", "-i", str(asset_path)]
                )
                parts = [f"[{in_idx}:v]setpts=(PTS-STARTPTS)/{speed:.6f}", f"fps={fps}"]
                played_dur = read_dur / speed
                if clip.end_behavior == "loop" and played_dur < dur - 1e-4:
                    loop_frames = min(18000, max(1, math.ceil(played_dur * float(fps)) + 4))
                    parts.append(f"loop=loop=-1:size={loop_frames}:start=0")
                elif played_dur < dur + 0.25:
                    parts.append(f"tpad=stop_mode=clone:stop_duration={dur - played_dur + 0.5:.6f}")
                parts.append(f"trim=duration={dur:.6f},setpts=PTS-STARTPTS")
                src_v_expr = ",".join(parts)
                has_clip_audio = (
                    not track.muted
                    and asset_meta.has_audio
                    and float(clip.volume) > 0.0
                )

            # Spatial framing / camera crop vs canvas overlay fit
            raw_v_lbl = _next_lbl("vraw")
            filter_lines.append(f"{src_v_expr}[{raw_v_lbl}]")

            if track.camera_follow:
                if project.camera_keyframes:
                    local_kfs = slice_camera_keyframes_for_segment(
                        project.camera_keyframes,
                        clip_start=clip.start_sec,
                        clip_dur=dur,
                        width=asset_w,
                        height=asset_h,
                        output_width=out_w,
                        output_height=out_h,
                    )
                else:
                    # Default centered vertical crop
                    from backend.geometry import compute_ideal_crop_size

                    iw, ih = compute_ideal_crop_size(
                        asset_w, asset_h, 1.0, output_width=out_w, output_height=out_h
                    )
                    cx = max(0.0, (float(asset_w) - iw) / (2.0 * float(asset_w)))
                    cy = max(0.0, (float(asset_h) - ih) / (2.0 * float(asset_h)))
                    local_kfs = [CropKeyframe(t=0.0, x=round(cx, 6), y=round(cy, 6), zoom=1.0)]

                crop_filter = build_crop_filter(
                    local_kfs,
                    width=asset_w,
                    height=asset_h,
                    duration=dur,
                    fps=fps,
                    output_width=out_w,
                    output_height=out_h,
                )
                clip_w = out_w
                clip_h = out_h
                framed_lbl = _next_lbl("vcrop")
                filter_lines.append(f"[{raw_v_lbl}]{crop_filter},trim=duration={dur:.6f},setpts=PTS-STARTPTS[{framed_lbl}]")

                if abs(clip.transform.scale - 1.0) > 1e-3:
                    clip_w = max(2, round_even(float(out_w) * float(clip.transform.scale)))
                    clip_h = max(2, round_even(float(out_h) * float(clip.transform.scale)))
                    scaled_lbl = _next_lbl("vscl")
                    filter_lines.append(
                        f"[{framed_lbl}]scale={clip_w}:{clip_h}:flags=lanczos,setsar=1,format=rgba[{scaled_lbl}]"
                    )
                    framed_lbl = scaled_lbl
                else:
                    rgba_lbl = _next_lbl("vrgba")
                    filter_lines.append(f"[{framed_lbl}]format=rgba[{rgba_lbl}]")
                    framed_lbl = rgba_lbl
            else:
                fit_scale = min(float(out_w) / float(asset_w), float(out_h) / float(asset_h))
                clip_w = max(2, round_even(float(asset_w) * fit_scale * float(clip.transform.scale)))
                clip_h = max(2, round_even(float(asset_h) * fit_scale * float(clip.transform.scale)))
                framed_lbl = _next_lbl("vov")
                filter_lines.append(
                    f"[{raw_v_lbl}]scale={clip_w}:{clip_h}:flags=lanczos,setsar=1,format=rgba[{framed_lbl}]"
                )

            # Flip H / V
            if clip.transform.flip_h:
                fh_lbl = _next_lbl("vfh")
                filter_lines.append(f"[{framed_lbl}]hflip[{fh_lbl}]")
                framed_lbl = fh_lbl
            if clip.transform.flip_v:
                fv_lbl = _next_lbl("vfv")
                filter_lines.append(f"[{framed_lbl}]vflip[{fv_lbl}]")
                framed_lbl = fv_lbl

            # Color Adjustments (brightness, contrast, saturation) — preserve RGBA alpha via alphamerge
            ca = clip.color_adjust
            if (
                abs(float(ca.brightness)) > 1e-4
                or abs(float(ca.contrast) - 1.0) > 1e-4
                or abs(float(ca.saturation) - 1.0) > 1e-4
            ):
                sp_c = _next_lbl("veqc")
                sp_a = _next_lbl("veqa")
                eq_c = _next_lbl("veqce")
                ex_a = _next_lbl("veqae")
                eq_lbl = _next_lbl("veq")
                filter_lines.append(f"[{framed_lbl}]split[{sp_c}][{sp_a}]")
                filter_lines.append(
                    f"[{sp_c}]eq=brightness={float(ca.brightness):.4f}:"
                    f"contrast={float(ca.contrast):.4f}:saturation={float(ca.saturation):.4f},format=rgba[{eq_c}]"
                )
                filter_lines.append(f"[{sp_a}]alphaextract[{ex_a}]")
                filter_lines.append(f"[{eq_c}][{ex_a}]alphamerge[{eq_lbl}]")
                framed_lbl = eq_lbl

            # Opacity multiplier
            if float(clip.transform.opacity) < 0.999:
                op_lbl = _next_lbl("vop")
                filter_lines.append(
                    f"[{framed_lbl}]colorchannelmixer=aa={float(clip.transform.opacity):.6f}[{op_lbl}]"
                )
                framed_lbl = op_lbl

            # Clip-edge In/Out transitions
            d_in = float(clip.transition_in.duration_sec) if clip.transition_in.type != "none" else 0.0
            d_out = float(clip.transition_out.duration_sec) if clip.transition_out.type != "none" else 0.0
            if d_in + d_out > dur and (d_in + d_out) > 0.0:
                factor = dur / (d_in + d_out)
                d_in *= factor
                d_out *= factor

            t_in = clip.transition_in.type
            t_out = clip.transition_out.type

            # Wipe In
            if d_in > 1e-4 and t_in in ("wipe_left", "wipe_right"):
                xf_w = "wipeleft" if t_in == "wipe_left" else "wiperight"
                min_lbl = _next_lbl("wmin")
                wout_lbl = _next_lbl("win")
                filter_lines.append(
                    f"color=c=black@0.0:s={clip_w}x{clip_h}:r={fps}:d={d_in:.6f},format=rgba[{min_lbl}]"
                )
                filter_lines.append(
                    f"[{min_lbl}][{framed_lbl}]xfade=transition={xf_w}:duration={d_in:.6f}:offset=0[{wout_lbl}]"
                )
                framed_lbl = wout_lbl
            elif d_in > 1e-4 and t_in in ("fade", "zoom"):
                fin_lbl = _next_lbl("fin")
                filter_lines.append(f"[{framed_lbl}]fade=t=in:st=0:d={d_in:.6f}:alpha=1[{fin_lbl}]")
                framed_lbl = fin_lbl

            # Wipe Out / Fade Out
            if d_out > 1e-4 and t_out in ("wipe_left", "wipe_right"):
                xf_w = "wipeleft" if t_out == "wipe_left" else "wiperight"
                mout_lbl = _next_lbl("wmout")
                wout_lbl = _next_lbl("wout")
                offset_out = max(0.0, dur - d_out)
                filter_lines.append(
                    f"color=c=black@0.0:s={clip_w}x{clip_h}:r={fps}:d={d_out:.6f},format=rgba[{mout_lbl}]"
                )
                filter_lines.append(
                    f"[{framed_lbl}][{mout_lbl}]xfade=transition={xf_w}:duration={d_out:.6f}:offset={offset_out:.6f}[{wout_lbl}]"
                )
                framed_lbl = wout_lbl
            elif d_out > 1e-4 and t_out in ("fade", "zoom"):
                fout_lbl = _next_lbl("fout")
                st_out = max(0.0, dur - d_out)
                filter_lines.append(f"[{framed_lbl}]fade=t=out:st={st_out:.6f}:d={d_out:.6f}:alpha=1[{fout_lbl}]")
                framed_lbl = fout_lbl

            # Rotation (applied after wipe so matte dimensions match pre-rotation size)
            if abs(float(clip.transform.rotation_deg)) > 0.01:
                rad = math.radians(float(clip.transform.rotation_deg))
                rot_lbl = _next_lbl("vrot")
                filter_lines.append(
                    f"[{framed_lbl}]rotate={rad:.6f}:ow=rotw({rad:.6f}):oh=roth({rad:.6f}):c=black@0.0[{rot_lbl}]"
                )
                framed_lbl = rot_lbl

            # Optional normalization to full canvas (needed before TrackTransition xfade)
            if pad_to_full_canvas and (
                clip_w != out_w
                or clip_h != out_h
                or abs(clip.transform.x - 0.5) > 1e-3
                or abs(clip.transform.y - 0.5) > 1e-3
                or abs(clip.transform.rotation_deg) > 0.01
            ):
                base_pad = _next_lbl("cpad")
                merged_pad = _next_lbl("cfull")
                filter_lines.append(
                    f"color=c=black@0.0:s={out_w}x{out_h}:r={fps}:d={dur:.6f},format=rgba[{base_pad}]"
                )
                filter_lines.append(
                    f"[{base_pad}][{framed_lbl}]overlay=x='{clip.transform.x:.6f}*W-w/2':y='{clip.transform.y:.6f}*H-h/2':format=rgb,format=rgba[{merged_pad}]"
                )
                framed_lbl = merged_pad
                clip_w = out_w
                clip_h = out_h

            # Clip Audio Preparation (always at 0..dur local time before placement/acrossfade)
            aud_lbl: str | None = None
            if has_clip_audio:
                aud_lbl = _next_lbl("vaud")
                a_parts = [f"[{in_idx}:a]asetpts=PTS-STARTPTS"]
                a_parts.extend(_build_atempo_filters(speed))
                a_played_dur = read_dur / speed
                if clip.end_behavior == "loop" and a_played_dur < dur - 1e-4:
                    loop_smps = max(1, int(round(a_played_dur * float(AUDIO_SAMPLE_RATE))))
                    a_parts.extend(
                        [
                            f"aresample={AUDIO_SAMPLE_RATE}",
                            f"aformat=sample_fmts=fltp:channel_layouts=stereo",
                            f"aloop=loop=-1:size={loop_smps}",
                        ]
                    )
                else:
                    a_parts.extend(
                        [
                            f"aresample={AUDIO_SAMPLE_RATE}",
                            "aformat=sample_fmts=fltp:channel_layouts=stereo",
                            f"apad=whole_dur={dur:.6f}",
                        ]
                    )
                a_parts.extend([f"atrim=duration={dur:.6f}", "asetpts=PTS-STARTPTS", f"volume={float(clip.volume):.6f}"])
                if float(clip.audio_fade_in_sec) > 1e-4:
                    a_parts.append(f"afade=t=in:st=0:d={min(dur, float(clip.audio_fade_in_sec)):.6f}")
                if float(clip.audio_fade_out_sec) > 1e-4:
                    f_out = min(dur, float(clip.audio_fade_out_sec))
                    a_parts.append(f"afade=t=out:st={max(0.0, dur - f_out):.6f}:d={f_out:.6f}")
                filter_lines.append(",".join(a_parts) + f"[{aud_lbl}]")

            return framed_lbl, aud_lbl, clip_w, clip_h, dur, d_in, d_out

        # 3. Render Visual Tracks Bottom-to-Top
        for track in project.tracks:
            if not isinstance(track, VideoTrack) or not track.visible:
                continue

            for run_clips, run_trs in _group_video_track_runs(track):
                is_run = len(run_clips) > 1
                if not is_run:
                    c0 = run_clips[0]
                    v_lbl, a_lbl, _cw, _ch, c_dur, d_in, d_out = _build_prepared_clip(
                        track, c0, pad_to_full_canvas=False
                    )
                    pts_lbl = _next_lbl("vpts")
                    start_s = float(c0.start_sec)
                    end_s = start_s + c_dur
                    filter_lines.append(f"[{v_lbl}]setpts=PTS-STARTPTS+{start_s:.6f}/TB[{pts_lbl}]")

                    dx_expr = _build_slide_offset_expr(
                        c0.transition_in.type, d_in, c0.transition_out.type, d_out, start_s, end_s, "x"
                    )
                    dy_expr = _build_slide_offset_expr(
                        c0.transition_in.type, d_in, c0.transition_out.type, d_out, start_s, end_s, "y"
                    )
                    base_x = f"{c0.transform.x:.6f}*W-w/2"
                    base_y = f"{c0.transform.y:.6f}*H-h/2"
                    x_expr = base_x if dx_expr == "0" else f"{base_x}+{dx_expr}"
                    y_expr = base_y if dy_expr == "0" else f"{base_y}+{dy_expr}"

                    next_canvas = _next_lbl("canvas")
                    filter_lines.append(
                        f"[{canvas_lbl}][{pts_lbl}]overlay=x='{x_expr}':y='{y_expr}':"
                        f"enable='between(t,{start_s:.6f},{end_s:.6f})':eof_action=pass:format=rgb[{next_canvas}]"
                    )
                    canvas_lbl = next_canvas

                    if a_lbl is not None:
                        if start_s > 1e-4:
                            delayed = _next_lbl("adel")
                            delay_ms = int(round(start_s * 1000.0))
                            filter_lines.append(f"[{a_lbl}]adelay=delays={delay_ms}:all=1[{delayed}]")
                            base_audio_labels.append(delayed)
                        else:
                            base_audio_labels.append(a_lbl)
                else:
                    # Same-track xfade / acrossfade junction chain
                    prepared = [
                        _build_prepared_clip(track, clp, pad_to_full_canvas=True) for clp in run_clips
                    ]
                    any_audio = any(item[1] is not None for item in prepared)
                    run_start = float(run_clips[0].start_sec)
                    cur_v, cur_a, _w, _h, cur_dur, _di, _do = prepared[0]
                    if any_audio and cur_a is None:
                        cur_a = _next_lbl("asil")
                        filter_lines.append(
                            f"anullsrc=r={AUDIO_SAMPLE_RATE}:cl=stereo:d={cur_dur:.6f}[{cur_a}]"
                        )

                    for idx, nxt_clip in enumerate(run_clips[1:]):
                        tr = run_trs[idx]
                        nxt_v, nxt_a, _nw, _nh, nxt_dur, _ndi, _ndo = prepared[idx + 1]
                        overlap = max(0.0, (run_start + cur_dur) - float(nxt_clip.start_sec))
                        if overlap >= 0.05:
                            eff_d = min(float(tr.duration_sec), overlap, cur_dur * 0.85, nxt_dur * 0.85)
                            offset = max(0.0, float(nxt_clip.start_sec) - run_start)
                        else:
                            eff_d = min(float(tr.duration_sec), cur_dur * 0.45, nxt_dur * 0.45)
                            offset = max(0.0, cur_dur - eff_d)

                        xf_name = "fade" if tr.type == "crossfade" else tr.type
                        merged_v = _next_lbl("vxf")
                        filter_lines.append(
                            f"[{cur_v}][{nxt_v}]xfade=transition={xf_name}:duration={eff_d:.6f}:offset={offset:.6f}[{merged_v}]"
                        )
                        cur_v = merged_v

                        if any_audio:
                            if nxt_a is None:
                                nxt_a = _next_lbl("asil")
                                filter_lines.append(
                                    f"anullsrc=r={AUDIO_SAMPLE_RATE}:cl=stereo:d={nxt_dur:.6f}[{nxt_a}]"
                                )
                            merged_a = _next_lbl("axf")
                            if overlap >= 0.05 or abs(eff_d) > 1e-4:
                                # Trim cur_a cleanly to offset + eff_d before acrossfade if overlapping beyond eff_d
                                if abs((offset + eff_d) - cur_dur) > 0.02:
                                    trimmed_a = _next_lbl("atrm")
                                    filter_lines.append(
                                        f"[{cur_a}]atrim=duration={max(eff_d + 0.02, offset + eff_d):.6f},asetpts=PTS-STARTPTS[{trimmed_a}]"
                                    )
                                    cur_a = trimmed_a
                                filter_lines.append(
                                    f"[{cur_a}][{nxt_a}]acrossfade=d={eff_d:.6f}:c1=tri:c2=tri[{merged_a}]"
                                )
                                cur_a = merged_a

                        cur_dur = offset + nxt_dur

                    pts_lbl = _next_lbl("vrpts")
                    run_end = run_start + cur_dur
                    filter_lines.append(f"[{cur_v}]setpts=PTS-STARTPTS+{run_start:.6f}/TB[{pts_lbl}]")
                    next_canvas = _next_lbl("canvas")
                    filter_lines.append(
                        f"[{canvas_lbl}][{pts_lbl}]overlay=x=0:y=0:"
                        f"enable='between(t,{run_start:.6f},{run_end:.6f})':eof_action=pass:format=rgb[{next_canvas}]"
                    )
                    canvas_lbl = next_canvas

                    if cur_a is not None:
                        if run_start > 1e-4:
                            delayed = _next_lbl("radel")
                            delay_ms = int(round(run_start * 1000.0))
                            filter_lines.append(f"[{cur_a}]adelay=delays={delay_ms}:all=1[{delayed}]")
                            base_audio_labels.append(delayed)
                        else:
                            base_audio_labels.append(cur_a)

        # 4. Subtitle Burn-In + Exact Video Output Framing
        yuv_lbl = _next_lbl("vyuv")
        ass_filter = (
            f"format=yuv420p,"
            f"ass=filename={_escape_ffmpeg_filter_path(master_ass_path)}"
            f":fontsdir={_escape_ffmpeg_filter_path(resolved_fonts_dir)},"
            f"trim=end_frame={frame_count},setpts=PTS-STARTPTS"
        )
        filter_lines.append(f"[{canvas_lbl}]{ass_filter}[{yuv_lbl}]")

        # 5. Standalone Audio Tracks (Mute / Solo / Fade / Loop / Ducking)
        audio_tracks = [t for t in project.tracks if isinstance(t, AudioTrack)]
        has_solo = any(t.solo and not t.muted for t in audio_tracks)
        for atr in audio_tracks:
            if atr.muted or (has_solo and not atr.solo) or float(atr.volume) <= 0.0:
                continue
            for aclip in atr.clips:
                asset_ref, asset_meta = _resolve_asset_and_metadata(
                    project, manifest, aclip.asset_id, asset_cache, ffmpeg_bin, ffprobe_bin
                )
                asset_path = Path(asset_ref.path)
                if not asset_path.is_file():
                    raise RenderError(f"Audio file missing for clip {aclip.id!r}: {asset_path}")
                if not asset_meta.has_audio:
                    raise RenderError(f"Asset {aclip.asset_id!r} has no audio stream")

                dur = float(aclip.duration_sec)
                spd = float(aclip.speed)
                avail_dur = max(0.02, float(asset_meta.duration) - float(aclip.media_in))
                need_src = dur * spd
                read_dur = avail_dur if aclip.loop else min(avail_dur, need_src + 0.1)

                in_idx = _add_input(
                    ["-ss", f"{aclip.media_in:.6f}", "-t", f"{max(0.02, read_dur):.6f}", "-i", str(asset_path)]
                )
                aparts = [f"[{in_idx}:a]asetpts=PTS-STARTPTS"]
                aparts.extend(_build_atempo_filters(spd))
                played_dur = read_dur / spd
                if aclip.loop and played_dur < dur - 1e-4:
                    loop_smps = max(1, int(round(played_dur * float(AUDIO_SAMPLE_RATE))))
                    aparts.extend(
                        [
                            f"aresample={AUDIO_SAMPLE_RATE}",
                            "aformat=sample_fmts=fltp:channel_layouts=stereo",
                            f"aloop=loop=-1:size={loop_smps}",
                        ]
                    )
                else:
                    aparts.extend(
                        [
                            f"aresample={AUDIO_SAMPLE_RATE}",
                            "aformat=sample_fmts=fltp:channel_layouts=stereo",
                            f"apad=whole_dur={dur:.6f}",
                        ]
                    )
                eff_vol = float(atr.volume) * float(aclip.volume)
                aparts.extend([f"atrim=duration={dur:.6f}", "asetpts=PTS-STARTPTS", f"volume={eff_vol:.6f}"])
                if float(aclip.fade_in_sec) > 1e-4:
                    aparts.append(f"afade=t=in:st=0:d={min(dur, float(aclip.fade_in_sec)):.6f}")
                if float(aclip.fade_out_sec) > 1e-4:
                    f_out = min(dur, float(aclip.fade_out_sec))
                    aparts.append(f"afade=t=out:st={max(0.0, dur - f_out):.6f}:d={f_out:.6f}")
                if float(aclip.start_sec) > 1e-4:
                    delay_ms = int(round(float(aclip.start_sec) * 1000.0))
                    aparts.append(f"adelay=delays={delay_ms}:all=1")

                albl = _next_lbl("atrk")
                filter_lines.append(",".join(aparts) + f"[{albl}]")
                if atr.duck_under_narration:
                    ducked_audio_labels.append(albl)
                else:
                    base_audio_labels.append(albl)

        # 6. Mixed Kokoro TTS Audio Bus + Optional Sidechain Ducking
        tts_mix_label: str | None = None
        if tts_wav is not None and tts_wav.is_file():
            tts_idx = _add_input(["-i", str(tts_wav)])
            tts_raw_lbl = _next_lbl("ttsin")
            filter_lines.append(
                f"[{tts_idx}:a]aresample={AUDIO_SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"apad=whole_dur={final_dur:.6f},atrim=duration={final_dur:.6f},asetpts=PTS-STARTPTS[{tts_raw_lbl}]"
            )
            tts_mix_label = tts_raw_lbl

        if ducked_audio_labels:
            if len(ducked_audio_labels) == 1:
                duck_bus = _next_lbl("dbus")
                filter_lines.append(
                    f"[{ducked_audio_labels[0]}]apad=whole_dur={final_dur:.6f},atrim=duration={final_dur:.6f},asetpts=PTS-STARTPTS[{duck_bus}]"
                )
            else:
                duck_bus = _next_lbl("dbus")
                refs = "".join(f"[{l}]" for l in ducked_audio_labels)
                filter_lines.append(
                    f"{refs}amix=inputs={len(ducked_audio_labels)}:duration=longest:normalize=0,"
                    f"apad=whole_dur={final_dur:.6f},atrim=duration={final_dur:.6f},asetpts=PTS-STARTPTS[{duck_bus}]"
                )

            if tts_mix_label is not None:
                tts_sc = _next_lbl("ttssc")
                tts_main = _next_lbl("ttsmain")
                ducked_out = _next_lbl("duckout")
                filter_lines.append(f"[{tts_mix_label}]asplit=2[{tts_sc}][{tts_main}]")
                filter_lines.append(
                    f"[{duck_bus}][{tts_sc}]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=250:level_sc=1[{ducked_out}]"
                )
                base_audio_labels.append(ducked_out)
                base_audio_labels.append(tts_main)
            else:
                base_audio_labels.append(duck_bus)
        elif tts_mix_label is not None:
            base_audio_labels.append(tts_mix_label)

        aout_lbl = "aout"
        audio_end_pad = final_dur + (1024.0 / float(AUDIO_SAMPLE_RATE))
        if not base_audio_labels:
            filter_lines.append(
                f"anullsrc=r={AUDIO_SAMPLE_RATE}:cl=stereo:d={final_dur:.6f}[{aout_lbl}]"
            )
        elif len(base_audio_labels) == 1:
            filter_lines.append(
                f"[{base_audio_labels[0]}]alimiter=limit=0.95,apad=whole_dur={audio_end_pad:.6f},"
                f"atrim=duration={audio_end_pad:.6f},asetpts=PTS-STARTPTS[{aout_lbl}]"
            )
        else:
            refs = "".join(f"[{l}]" for l in base_audio_labels)
            filter_lines.append(
                f"{refs}amix=inputs={len(base_audio_labels)}:duration=longest:normalize=0,"
                f"alimiter=limit=0.95,apad=whole_dur={audio_end_pad:.6f},"
                f"atrim=duration={audio_end_pad:.6f},asetpts=PTS-STARTPTS[{aout_lbl}]"
            )

        filter_script_path.write_text(";\n".join(filter_lines), encoding="utf-8")

        cmd = [str(ffmpeg_bin), "-y", "-hide_banner"]
        for spec in input_specs:
            cmd.extend(spec)
        cmd.extend(
            [
                "-filter_complex_script",
                str(filter_script_path),
                "-map",
                f"[{yuv_lbl}]",
                "-map",
                f"[{aout_lbl}]",
                "-c:v",
                "libx264",
                "-crf",
                str(crf),
                "-preset",
                "medium",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                "-fps_mode",
                "cfr",
                "-frames:v",
                str(frame_count),
                "-movflags",
                "+faststart",
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
            expected_width=out_w,
            expected_height=out_h,
            expected_fps=fps,
            require_audio=True,
        )
        os.replace(tmp_out, final_export_path)
        return final_export_path
    except Exception:
        tmp_out.unlink(missing_ok=True)
        raise
    finally:
        master_ass_path.unlink(missing_ok=True)
        mixed_tts_wav_path.unlink(missing_ok=True)
        filter_script_path.unlink(missing_ok=True)


def render_final_export(
    project: ProjectInput,
    source_metadata: SourceMetadata | None,
    manifest: ArtifactManifest,
    project_dir: Path,
    export_path: Path | None = None,
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    fonts_dir: Path | None = None,
    cancel_event: threading.Event | None = None,
    on_process_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
    crf: int = 19,
    fps_override: int | None = None,
) -> Path:
    """Render full project to final MP4 (multi-track compositor or legacy two-stage concat)."""
    if _is_multitrack_project(project):
        return render_timeline_composite(
            project=project,
            source_metadata=source_metadata,
            manifest=manifest,
            project_dir=project_dir,
            export_path=export_path,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            fonts_dir=fonts_dir,
            cancel_event=cancel_event,
            on_process_start=on_process_start,
            crf=crf,
            fps_override=fps_override,
        )

    if not project.segments:
        raise RenderError("Project has no segments to export")
    if source_metadata is None:
        raise RenderError("Source metadata is required for legacy segment export")

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

        render_segment = segment
        if project.camera_keyframes:
            sliced_kfs = slice_camera_keyframes_for_segment(
                project.camera_keyframes,
                clip_start=offset_sec,
                clip_dur=plan.final_dur,
                width=source_metadata.width,
                height=source_metadata.height,
                output_width=project.output_settings.width,
                output_height=project.output_settings.height,
            )
            render_segment = segment.model_copy(update={"crop_keyframes": sliced_kfs})

        seg_mp4 = render_segment_stage1(
            segment=render_segment,
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


def render_project_preview(
    project: ProjectInput,
    source_metadata: SourceMetadata | None,
    manifest: ArtifactManifest,
    project_dir: Path,
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    fonts_dir: Path | None = None,
    cancel_event: threading.Event | None = None,
    on_process_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> Path:
    """Render a full-project captioned MP4 preview (content-addressed preview cache)."""
    ffmpeg_bin, ffprobe_bin = _resolve_ffmpeg_tools(ffmpeg_path, ffprobe_path)

    if _is_multitrack_project(project):
        preview_hash = compute_timeline_render_hash(project, manifest)
    else:
        if not project.segments:
            raise RenderError("Project has no segments to preview")
        preview_hash = compute_project_preview_hash(project, manifest)

    artifacts_dir = project_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    preview_path = artifacts_dir / f"preview_project_{preview_hash}.mp4"

    if preview_path.is_file():
        try:
            probe_rendered_video(
                preview_path,
                ffprobe_bin,
                expected_width=project.output_settings.width,
                expected_height=project.output_settings.height,
                expected_fps=project.output_settings.fps,
                require_audio=True,
            )
            return preview_path
        except RenderError:
            preview_path.unlink(missing_ok=True)

    return render_final_export(
        project=project,
        source_metadata=source_metadata,
        manifest=manifest,
        project_dir=project_dir,
        export_path=preview_path,
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
        fonts_dir=fonts_dir,
        cancel_event=cancel_event,
        on_process_start=on_process_start,
    )

