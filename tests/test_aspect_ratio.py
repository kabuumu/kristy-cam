"""Tests for arbitrary source and output aspect ratios across geometry, media, and rendering."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.bootstrap import verify_ffmpeg
from backend.geometry import (
    GeometryValidationError,
    build_crop_filter,
    clamp_normalized_coords,
    compute_ideal_crop_size,
    evaluate_crop_at,
    validate_keyframes,
)
from backend.media import probe_and_validate_media
from backend.renderer import (
    probe_rendered_video,
    render_final_export,
    render_segment_stage1,
)
from backend.schemas import (
    ArtifactManifest,
    CropKeyframe,
    OutputSettings,
    ProjectInput,
    SegmentInput,
    SourceMediaRef,
)
from backend.storage import compute_file_fingerprint


def _make_test_video(
    ffmpeg_path: Path,
    out_path: Path,
    *,
    width: int,
    height: int,
    duration: float = 1.0,
) -> Path:
    cmd = [
        str(ffmpeg_path),
        "-y",
        "-hide_banner",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate=30:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={duration}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-ar",
        "48000",
        str(out_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return out_path


def test_output_settings_requires_even_dimensions() -> None:
    """OutputSettings rejects odd width or height."""
    with pytest.raises(ValueError, match="even integer"):
        OutputSettings(width=1081, height=1920)
    with pytest.raises(ValueError, match="even integer"):
        OutputSettings(width=1080, height=1921)
    ok = OutputSettings(width=1440, height=1080)
    assert (ok.width, ok.height) == (1440, 1080)


def test_geometry_arbitrary_source_and_output_aspects() -> None:
    """Crop size, clamping, validation, and filter strings work for any aspect pair."""
    # Wider source (16:9 = 1920x1080) -> Square output (1:1 = 1080x1080)
    w, h = compute_ideal_crop_size(1920, 1080, zoom=1.0, output_width=1080, output_height=1080)
    assert w == pytest.approx(1080.0)
    assert h == pytest.approx(1080.0)

    cx, cy = clamp_normalized_coords(0.9, 0.5, zoom=1.0, width=1920, height=1080, output_width=1080, output_height=1080)
    assert cx == pytest.approx((1920.0 - 1080.0) / 1920.0)
    assert cy == pytest.approx(0.0)

    # Taller source (9:16 = 1080x1920) -> Landscape output (16:9 = 1920x1080)
    w2, h2 = compute_ideal_crop_size(1080, 1920, zoom=1.0, output_width=1920, output_height=1080)
    assert w2 == pytest.approx(1080.0)
    assert h2 == pytest.approx(1080.0 * 9.0 / 16.0)

    cx2, cy2 = clamp_normalized_coords(0.5, 0.9, zoom=1.0, width=1080, height=1920, output_width=1920, output_height=1080)
    assert cx2 == pytest.approx(0.0)
    assert cy2 == pytest.approx((1920.0 - h2) / 1920.0)

    # Keyframe validation respects output aspect
    kfs = [
        CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0),
        CropKeyframe(t=1.0, x=0.0, y=cy2, zoom=1.5),
    ]
    validate_keyframes(kfs, duration=2.0, width=1080, height=1920, output_width=1920, output_height=1080)
    box = evaluate_crop_at(kfs, t=1.0, width=1080, height=1920, output_width=1920, output_height=1080)
    assert box.w_px == pytest.approx(1080.0 / 1.5)
    assert box.h_px == pytest.approx(h2 / 1.5)

    with pytest.raises(GeometryValidationError):
        validate_keyframes(
            [CropKeyframe(t=0.0, x=0.2, y=0.0, zoom=1.0)],
            duration=1.0,
            width=1080,
            height=1920,
            output_width=1920,
            output_height=1080,
        )

    static_f = build_crop_filter(
        [CropKeyframe(t=0.0, x=0.0, y=0.1, zoom=1.0)],
        width=1080,
        height=1920,
        duration=1.0,
        fps=30,
        output_width=1280,
        output_height=720,
    )
    assert "scale=1280:720:flags=lanczos" in static_f

    anim_f = build_crop_filter(
        kfs,
        width=1080,
        height=1920,
        duration=2.0,
        fps=30,
        output_width=1280,
        output_height=720,
    )
    assert "s=1280x720" in anim_f


@pytest.mark.parametrize(
    ("src_w", "src_h", "out_w", "out_h", "animated"),
    [
        (1280, 720, 720, 720, False),   # 16:9 -> 1:1 static
        (720, 1280, 1280, 720, True),   # 9:16 portrait -> 16:9 landscape animated zoom
        (640, 480, 720, 900, False),    # 4:3 -> 4:5 static
    ],
)
def test_render_arbitrary_aspect_ratios(
    tmp_path: Path,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    animated: bool,
) -> None:
    """Media validation, Stage 1 segment render, and final export work for any aspect ratio."""
    ffmpeg_bin, ffprobe_bin = verify_ffmpeg(offline=True)
    src_path = _make_test_video(
        ffmpeg_bin,
        tmp_path / f"src_{src_w}x{src_h}.mp4",
        width=src_w,
        height=src_h,
        duration=1.0,
    )

    meta = probe_and_validate_media(src_path, ffprobe_path=ffprobe_bin, ffmpeg_path=ffmpeg_bin)
    assert (meta.width, meta.height) == (src_w, src_h)

    kfs = (
        [
            CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0),
            CropKeyframe(t=0.8, x=0.0, y=0.1, zoom=1.5),
        ]
        if animated
        else [CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0)]
    )

    out_settings = OutputSettings(width=out_w, height=out_h, fps=30)
    seg = SegmentInput(
        id="seg_01",
        video_in=0.0,
        video_out=0.8,
        duration_mode="preserve_video",
        source_audio_volume=0.5,
        speed=1.0,
        crop_keyframes=kfs,
        clauses=[],
    )
    src_ref = SourceMediaRef(
        media_id="src_test",
        path=str(src_path),
        fingerprint=compute_file_fingerprint(src_path),
    )

    project_dir = tmp_path / "proj"
    seg_mp4 = render_segment_stage1(
        segment=seg,
        source_media=src_ref,
        source_metadata=meta,
        output_settings=out_settings,
        project_dir=project_dir,
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
    )
    info = probe_rendered_video(
        seg_mp4,
        ffprobe_bin,
        expected_width=out_w,
        expected_height=out_h,
        expected_fps=30,
        require_audio=True,
    )
    assert (info["width"], info["height"]) == (out_w, out_h)

    project = ProjectInput(
        project_id="proj_aspect",
        title="Aspect Test",
        source_media=src_ref,
        output_settings=out_settings,
        segments=[seg],
    )
    manifest = ArtifactManifest(project_id="proj_aspect", source_metadata=meta)
    export_mp4 = render_final_export(
        project=project,
        source_metadata=meta,
        manifest=manifest,
        project_dir=project_dir,
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
    )
    export_info = probe_rendered_video(
        export_mp4,
        ffprobe_bin,
        expected_width=out_w,
        expected_height=out_h,
        expected_fps=30,
        require_audio=True,
    )
    assert (export_info["width"], export_info["height"]) == (out_w, out_h)
