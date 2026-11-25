"""ASS subtitle escaping, clause-continuity timing, and UTF-8 .ass file generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from backend.schemas import (
    CaptionAlign,
    CaptionAnimation,
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
    primary_color: str | None = None
    outline_color: str | None = None
    back_color: str | None = None
    border_style: int | None = None
    karaoke_highlight: bool | None = None
    animation: CaptionAnimation | None = None


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


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds as SRT timestamp HH:MM:SS,mmm, clamped >= 0."""
    ms_total = max(0, int(round(seconds * 1000.0)))
    hours, rem = divmod(ms_total, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


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
                    primary_color=override.primary_color if override is not None else None,
                    outline_color=override.outline_color if override is not None else None,
                    back_color=override.back_color if override is not None else None,
                    border_style=override.border_style if override is not None else None,
                    karaoke_highlight=override.karaoke_highlight if override is not None else None,
                    animation=override.animation if override is not None else None,
                )
            )
    events.sort(key=lambda e: (e.start_sec, e.end_sec))
    return events


def _format_karaoke_text(raw_text: str, duration_sec: float) -> str:
    """Distribute ASS \\kf karaoke centisecond durations across words proportionally to word length."""
    words = raw_text.strip().split()
    if len(words) <= 1 or duration_sec <= 0.05:
        return escape_ass_text(raw_text)
    total_cs = max(len(words), int(round(duration_sec * 100.0)))
    weights = [max(1, len(w)) for w in words]
    total_w = sum(weights)
    parts: list[str] = []
    remaining = total_cs
    for idx, (word, w) in enumerate(zip(words, weights)):
        if idx == len(words) - 1:
            cs = max(1, remaining)
        else:
            cs = max(1, int(round((w / total_w) * total_cs)))
            remaining = max(1, remaining - cs)
        sep = " " if idx < len(words) - 1 else ""
        parts.append(f"{{\\kf{cs}}}{escape_ass_text(word)}{sep}")
    return "".join(parts)


def _ass_color_override_tags(channel: int, ass_color: str) -> str:
    """Convert &HAABBGGRR (or &HBBGGRR) to valid ASS inline override tags (\\{ch}c&HBBGGRR& + optional \\{ch}a&HAA&)."""
    raw = ass_color.strip().rstrip("&")
    if raw.upper().startswith("&H"):
        hex_part = raw[2:].upper()
    else:
        hex_part = raw.upper()
    if len(hex_part) == 8:
        alpha_hex = hex_part[:2]
        bgr_hex = hex_part[2:]
        tag = f"\\{channel}c&H{bgr_hex}&"
        if alpha_hex != "00":
            tag += f"\\{channel}a&H{alpha_hex}&"
        return tag
    if len(hex_part) == 6:
        return f"\\{channel}c&H{hex_part}&"
    return f"\\{channel}c{ass_color}&"


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
    shadow_w = (
        int(caption_settings.shadow)
        if float(caption_settings.shadow).is_integer()
        else caption_settings.shadow
    )
    border_style = caption_settings.border_style if caption_settings.border_style in (1, 3) else 1
    # In ASS karaoke (\kf), SecondaryColour is the unhighlighted color and PrimaryColour is the highlighted color.
    # When global karaoke_highlight is enabled, swap so words fill into highlight_color.
    if caption_settings.karaoke_highlight:
        primary_col = caption_settings.highlight_color
        secondary_col = caption_settings.primary_color
    else:
        primary_col = caption_settings.primary_color
        secondary_col = caption_settings.highlight_color

    margin_lr = 60

    def _make_style_line(name: str, bs: int) -> str:
        # In libass BorderStyle=3 (opaque box), OutlineColour renders the box background.
        out_col = caption_settings.back_color if bs == 3 else caption_settings.outline_color
        return (
            f"Style: {name},{caption_settings.font_name},{caption_settings.font_size},"
            f"{primary_col},{secondary_col},{out_col},{caption_settings.back_color},"
            f"0,0,0,0,100,100,0,0,{bs},{outline_w},{shadow_w},2,{margin_lr},{margin_lr},{caption_settings.margin_v},1"
        )

    style_lines = [_make_style_line("Default", border_style)]
    alt_border_style = 1 if border_style == 3 else 3
    alt_style_name = "DefaultBox" if alt_border_style == 3 else "DefaultOutline"
    needs_alt_style = any(
        ev.border_style in (1, 3) and ev.border_style != border_style for ev in events
    )
    if needs_alt_style:
        style_lines.append(_make_style_line(alt_style_name, alt_border_style))

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
        *style_lines,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    align_codes: dict[CaptionAlign, int] = {"bottom": 2, "center": 5, "top": 8}

    for ev in events:
        start_ts = format_ass_timestamp(ev.start_sec)
        end_ts = format_ass_timestamp(ev.end_sec)
        use_karaoke = (
            ev.karaoke_highlight
            if ev.karaoke_highlight is not None
            else caption_settings.karaoke_highlight
        )
        anim = ev.animation if ev.animation is not None else caption_settings.animation
        ev_bs = ev.border_style if ev.border_style in (1, 3) else border_style
        ev_style_name = alt_style_name if ev_bs != border_style else "Default"
        text = (
            _format_karaoke_text(ev.text, max(0.0, ev.end_sec - ev.start_sec))
            if use_karaoke
            else escape_ass_text(ev.text)
        )

        override_tags: list[str] = []
        if ev.align is not None and ev.align in align_codes:
            override_tags.append(f"\\an{align_codes[ev.align]}")
        if ev.font_size is not None and ev.font_size > 0:
            override_tags.append(f"\\fs{ev.font_size}")

        # Ensure Primary/Secondary colors match whether this specific event uses karaoke
        if use_karaoke != caption_settings.karaoke_highlight:
            base_col = ev.primary_color or caption_settings.primary_color
            if use_karaoke:
                override_tags.append(_ass_color_override_tags(1, caption_settings.highlight_color))
                override_tags.append(_ass_color_override_tags(2, base_col))
            else:
                override_tags.append(_ass_color_override_tags(1, base_col))
        elif ev.primary_color:
            override_tags.append(_ass_color_override_tags(2 if use_karaoke else 1, ev.primary_color))

        if ev.outline_color and ev_bs != 3:
            override_tags.append(_ass_color_override_tags(3, ev.outline_color))
        if ev.back_color:
            if ev_bs == 3:
                override_tags.append(_ass_color_override_tags(3, ev.back_color))
            override_tags.append(_ass_color_override_tags(4, ev.back_color))
        if anim == "pop":
            override_tags.append("\\fscx85\\fscy85\\t(0,110,\\fscx100\\fscy100)")
        elif anim == "fade":
            override_tags.append("\\fad(120,120)")

        prefix = "{" + "".join(override_tags) + "}" if override_tags else ""
        margin_v_col = ev.margin_v if ev.margin_v is not None else 0
        lines.append(f"Dialogue: 0,{start_ts},{end_ts},{ev_style_name},,0,0,{margin_v_col},,{prefix}{text}")

    lines.append("")
    return "\n".join(lines)


def render_srt_content(events: Sequence[SubtitleEvent]) -> str:
    """Render standard UTF-8 SubRip (.srt) subtitle text from sorted SubtitleEvents."""
    blocks: list[str] = []
    seq = 0
    for ev in events:
        start_ts = format_srt_timestamp(ev.start_sec)
        end_ts = format_srt_timestamp(max(ev.start_sec + 0.05, ev.end_sec))
        clean_text = ev.text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not clean_text:
            continue
        seq += 1
        blocks.append(f"{seq}\n{start_ts} --> {end_ts}\n{clean_text}\n")
    return "\n".join(blocks)


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
