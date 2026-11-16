"""ASS subtitle escaping, clause-continuity timing, and UTF-8 .ass file generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from backend.schemas import CaptionSettings, ClauseInput, ClauseTiming


@dataclass(frozen=True)
class SubtitleEvent:
    """A single caption dialogue interval using authoritative display_text."""

    start_sec: float
    end_sec: float
    text: str


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


def _estimate_wrapped_lines(text: str, max_width_px: int, font_size: int) -> int:
    """Estimate wrapped line count for Montserrat ExtraBold at given width & font size."""
    avg_char_w = max(1.0, float(font_size) * 0.58)
    max_chars = max(8, int(max_width_px / avg_char_w))
    total_lines = 0
    for para in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        words = para.split()
        if not words:
            total_lines += 1
            continue
        lines = 1
        cur_len = 0
        for w in words:
            w_len = len(w)
            if cur_len == 0:
                cur_len = w_len
            elif cur_len + 1 + w_len <= max_chars:
                cur_len += 1 + w_len
            else:
                lines += 1
                cur_len = w_len
        total_lines += lines
    return max(1, total_lines)


def render_ass_content(
    events: Sequence[SubtitleEvent],
    caption_settings: CaptionSettings,
    width: int = 1080,
    height: int = 1920,
) -> str:
    """Render complete UTF-8 ASS subtitle document with auto-wrap (WrapStyle: 0) and bottom-center/vertical centering."""
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

    usable_width = max(100, width - (margin_lr * 2))
    for ev in events:
        start_ts = format_ass_timestamp(ev.start_sec)
        end_ts = format_ass_timestamp(ev.end_sec)
        text = escape_ass_text(ev.text)
        est_lines = _estimate_wrapped_lines(ev.text, usable_width, caption_settings.font_size)
        est_block_h = est_lines * float(caption_settings.font_size) * 1.25
        if est_lines >= 4 or (est_block_h + caption_settings.margin_v > float(height) * 0.55):
            text = f"{{\\an5}}{text}"
        lines.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{text}")

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
