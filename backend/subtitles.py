"""ASS subtitle escaping, clause-continuity timing, and UTF-8 .ass file generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from backend.schemas import (
    CaptionAlign,
    CaptionArtifactState,
    CaptionSettings,
    CaptionTrack,
    ClauseInput,
    ClauseTiming,
    TimelineTrack,
)


@dataclass(frozen=True)
class SubtitleEvent:
    """A single caption dialogue interval using authoritative display_text."""

    start_sec: float
    end_sec: float
    text: str
    margin_v: int | None = None
    font_size: int | None = None
    align: CaptionAlign | None = None


def escape_ass_text(text: str) -> str:
    """Normalize newlines and escape ASS special characters before converting newlines to \\N."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    escaped = normalized.replace("\\", "\\\\")
    escaped = escaped.replace("{", "\\{")
    escaped = escaped.replace("}", "\\}")
    return escaped.replace("\n", "\\N")


def format_ass_timestamp(seconds: float) -> str:
    """Format seconds as ASS timestamp H:MM:SS.cc, clamped >= 0."""
    cs_total = max(0, int(round(seconds * 100.0)))
    hours, rem = divmod(cs_total, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def build_segment_subtitle_events(
    clauses: Sequence[ClauseInput],
    clause_timings: Sequence[ClauseTiming],
    segment_offset_sec: float = 0.0,
) -> list[SubtitleEvent]:
    """Build caption events from display_text; hold across inter-clause pauses, end strictly at final audio end."""
    if not clauses or not clause_timings:
        return []
    if len(clauses) != len(clause_timings):
        raise ValueError(
            f"Mismatch between clauses ({len(clauses)}) and clause_timings ({len(clause_timings)})"
        )

    has_custom_timings = any(
        c.start_sec is not None or c.duration_sec is not None for c in clauses
    )
    events: list[SubtitleEvent] = []
    last_idx = len(clauses) - 1
    for idx, (clause, timing) in enumerate(zip(clauses, clause_timings)):
        start_sec = segment_offset_sec + (
            clause.start_sec if clause.start_sec is not None else timing.start_sec
        )
        if clause.duration_sec is not None:
            end_sec = start_sec + clause.duration_sec
        elif has_custom_timings:
            end_sec = segment_offset_sec + timing.end_sec
        elif idx < last_idx:
            end_sec = segment_offset_sec + clause_timings[idx + 1].start_sec
        else:
            end_sec = segment_offset_sec + timing.end_sec

        events.append(
            SubtitleEvent(
                start_sec=start_sec,
                end_sec=end_sec,
                text=clause.display_text,
            )
        )

    return events


def build_timeline_subtitle_events(
    tracks: Sequence[TimelineTrack],
    caption_artifacts: Mapping[str, CaptionArtifactState],
) -> list[SubtitleEvent]:
    """Build sorted subtitle events across all visible independent CaptionTracks."""
    events: list[SubtitleEvent] = []
    for track in tracks:
        if not isinstance(track, CaptionTrack) or not track.visible:
            continue
        for clip in track.clips:
            if not clip.display_text.strip():
                continue
            dur: float | None = None
            if not clip.auto_fit_tts and clip.duration_sec is not None and clip.duration_sec > 0.0:
                dur = clip.duration_sec
            elif clip.tts_enabled:
                art = caption_artifacts.get(clip.id)
                if art is not None and art.audio_duration > 0.0:
                    dur = art.audio_duration
            if dur is None:
                if clip.duration_sec is not None and clip.duration_sec > 0.0:
                    dur = clip.duration_sec
                else:
                    words = max(1, len(clip.display_text.split()))
                    dur = max(1.2, min(8.0, words * 0.42))

            override = clip.style_override
            events.append(
                SubtitleEvent(
                    start_sec=clip.start_sec,
                    end_sec=clip.start_sec + dur,
                    text=clip.display_text,
                    margin_v=override.margin_v if override is not None else None,
                    font_size=override.font_size if override is not None else None,
                    align=override.align if override is not None else None,
                )
            )
    events.sort(key=lambda e: (e.start_sec, e.end_sec))
    return events


def render_ass_content(
    events: Sequence[SubtitleEvent],
    caption_settings: CaptionSettings,
    width: int = 1080,
    height: int = 1920,
) -> str:
    """Render complete UTF-8 ASS subtitle document with auto-wrap (WrapStyle: 0) and bottom-center alignment (\\an2)."""
    outline_w = (
        int(caption_settings.outline_width)
        if float(caption_settings.outline_width).is_integer()
        else caption_settings.outline_width
    )
    margin_lr = 60
    style_line = (
        f"Style: Default,{caption_settings.font_name},{caption_settings.font_size},"
        f"{caption_settings.primary_color},&H000000FF,{caption_settings.outline_color},&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{outline_w},0,2,{margin_lr},{margin_lr},{caption_settings.margin_v},1"
    )

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        style_line,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    align_codes: dict[CaptionAlign, int] = {"bottom": 2, "center": 5, "top": 8}

    for ev in events:
        start_ts = format_ass_timestamp(ev.start_sec)
        end_ts = format_ass_timestamp(ev.end_sec)
        text = escape_ass_text(ev.text)
        override_tags: list[str] = []
        if ev.align is not None and ev.align in align_codes:
            override_tags.append(f"\\an{align_codes[ev.align]}")
        if ev.font_size is not None and ev.font_size > 0:
            override_tags.append(f"\\fs{ev.font_size}")
        prefix = "{" + "".join(override_tags) + "}" if override_tags else ""
        margin_v_col = ev.margin_v if ev.margin_v is not None else 0
        lines.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,{margin_v_col},,{prefix}{text}")

    lines.append("")
    return "\n".join(lines)


def generate_ass_file(
    events: Sequence[SubtitleEvent],
    caption_settings: CaptionSettings,
    output_path: Path,
    width: int = 1080,
    height: int = 1920,
) -> Path:
    """Write rendered ASS subtitle content to output_path as UTF-8."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = render_ass_content(events, caption_settings, width=width, height=height)
    output_path.write_text(content, encoding="utf-8")
    return output_path
