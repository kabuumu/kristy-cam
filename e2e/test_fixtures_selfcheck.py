"""Self-check tests for runtime synthetic media generators (runnable standalone)."""

from __future__ import annotations

from pathlib import Path

import pytest

from e2e.fixtures.media_gen import (
    generate_anamorphic_sar_mp4,
    generate_corrupt_mp4,
    generate_hdr_pq_mp4,
    generate_non_16_9_mp4,
    generate_rotated_mp4,
    generate_sdr_cfr_mp4,
    generate_sdr_vfr_mp4,
    generate_silent_mp4,
    probe_media,
)


def _video_stream(info: dict) -> dict:
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    assert streams, f"No video stream found in probe output: {info}"
    return streams[0]


def _audio_streams(info: dict) -> list[dict]:
    return [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]


def test_sdr_cfr_1080p_with_audio(tmp_path: Path) -> None:
    out = generate_sdr_cfr_mp4(tmp_path / "sdr_1080p.mp4", 1920, 1080, 30, 2.0, True)
    info = probe_media(out)
    v = _video_stream(info)
    assert v["width"] == 1920
    assert v["height"] == 1080
    assert v.get("sample_aspect_ratio", "1:1") == "1:1"
    assert v.get("r_frame_rate") == "30/1"
    assert v.get("avg_frame_rate") == "30/1"
    assert v.get("color_transfer", "") != "smpte2084"
    a = _audio_streams(info)
    assert len(a) == 1
    assert a[0]["sample_rate"] == "48000"
    assert int(a[0]["channels"]) == 2


def test_sdr_cfr_720p(tmp_path: Path) -> None:
    out = generate_sdr_cfr_mp4(tmp_path / "sdr_720p.mp4", 1280, 720, 30, 2.0, True)
    info = probe_media(out)
    v = _video_stream(info)
    assert v["width"] == 1280
    assert v["height"] == 720
    assert v.get("r_frame_rate") == "30/1"


def test_sdr_vfr_16_9(tmp_path: Path) -> None:
    out = generate_sdr_vfr_mp4(tmp_path / "sdr_vfr.mp4", duration=2.0)
    info = probe_media(out)
    v = _video_stream(info)
    assert v["width"] == 1280
    assert v["height"] == 720
    # True VFR should have r_frame_rate != avg_frame_rate
    assert v["r_frame_rate"] != v["avg_frame_rate"], (
        f"Expected VFR r_frame_rate != avg_frame_rate, got {v['r_frame_rate']} vs {v['avg_frame_rate']}"
    )


def test_silent_mp4_has_no_audio(tmp_path: Path) -> None:
    out = generate_silent_mp4(tmp_path / "silent.mp4", 1920, 1080, 30, 2.0)
    info = probe_media(out)
    v = _video_stream(info)
    assert v["width"] == 1920
    assert v["height"] == 1080
    assert _audio_streams(info) == []


def test_hdr_pq_mp4(tmp_path: Path) -> None:
    out = generate_hdr_pq_mp4(tmp_path / "hdr_pq.mp4", duration=1.5)
    info = probe_media(out)
    v = _video_stream(info)
    assert v.get("color_transfer") == "smpte2084"


def test_rotated_90_mp4(tmp_path: Path) -> None:
    out = generate_rotated_mp4(tmp_path / "rotated.mp4", rotation=90, duration=1.5)
    info = probe_media(out)
    v = _video_stream(info)
    tags = v.get("tags", {})
    side_data = v.get("side_data_list", [])
    has_rotate_tag = str(tags.get("rotate", "0")) != "0"
    has_display_matrix = any(
        abs(int(sd.get("rotation", 0))) != 0
        for sd in side_data
        if "rotation" in sd
    )
    assert has_rotate_tag or has_display_matrix, (
        f"Expected non-zero rotation metadata, got tags={tags}, side_data={side_data}"
    )


def test_non_16_9_4_3_mp4(tmp_path: Path) -> None:
    out = generate_non_16_9_mp4(tmp_path / "four_three.mp4", 1440, 1080, 30, 1.5)
    info = probe_media(out)
    v = _video_stream(info)
    assert v["width"] == 1440
    assert v["height"] == 1080
    assert pytest.approx(v["width"] / v["height"], abs=0.01) == 4 / 3


def test_anamorphic_sar_mp4(tmp_path: Path) -> None:
    out = generate_anamorphic_sar_mp4(tmp_path / "anamorphic.mp4", duration=1.5)
    info = probe_media(out)
    v = _video_stream(info)
    assert v.get("sample_aspect_ratio") not in (None, "1:1", "0:1")


def test_corrupt_mp4_fails_ffprobe(tmp_path: Path) -> None:
    out = generate_corrupt_mp4(tmp_path / "corrupt.mp4")
    assert out.exists() and out.stat().st_size > 0
    with pytest.raises(RuntimeError):
        probe_media(out)


def test_unicode_space_apostrophe_path(tmp_path: Path) -> None:
    special_dir = tmp_path / "Sims 4 — Vaelen's Café"
    out = generate_sdr_cfr_mp4(
        special_dir / "O'Malley's Clip (1080p) 日本語.mp4",
        width=1280,
        height=720,
        fps=30,
        duration=1.5,
        with_audio=True,
    )
    assert out.exists()
    info = probe_media(out)
    v = _video_stream(info)
    assert v["width"] == 1280
    assert v["height"] == 720
