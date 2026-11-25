"""Tests for CapCut-inspired QOL features: easing, ASS styling/karaoke, SRT export, project CRUD, thumbnails, waveforms, and render filters."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.bootstrap import verify_ffmpeg
from backend.geometry import build_crop_filter, evaluate_crop_at
from backend import main as main_module
from backend.renderer import render_timeline_composite
from backend.schemas import (
    ArtifactManifest,
    CaptionClip,
    CaptionSettings,
    CaptionStyleOverride,
    CaptionTrack,
    CropKeyframe,
    MediaAssetMetadata,
    MediaAssetRef,
    ProjectInput,
    SourceMediaRef,
    SourceMetadata,
    TimelineMarker,
    VideoTrack,
    VisualClip,
    VisualClipColorAdjust,
    VisualClipTransform,
)
from backend.storage import (
    create_project,
    delete_project,
    duplicate_project,
    rename_project,
    save_manifest,
)
from backend.subtitles import SubtitleEvent, render_ass_content, render_srt_content


@pytest.fixture(scope="session")
def ffmpeg_tools() -> tuple[Path, Path]:
    return verify_ffmpeg(offline=True)


def _gen_test_mp4(ffmpeg_bin: Path, out_path: Path, dur: float = 1.5) -> Path:
    cmd = [
        str(ffmpeg_bin),
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=640x360:rate=30:duration={dur}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={dur}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


def test_keyframe_easing_curves_and_ffmpeg_expr():
    kfs = [
        CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0, transition="ease_in"),
        CropKeyframe(t=2.0, x=0.2, y=0.0, zoom=2.0, transition="ease_out"),
        CropKeyframe(t=4.0, x=0.0, y=0.0, zoom=1.0, transition="ease_in_out"),
    ]
    mid_in = evaluate_crop_at(kfs, 1.0, 1920, 1080)
    assert mid_in.zoom == pytest.approx(1.25, abs=1e-4)

    mid_out = evaluate_crop_at(kfs, 3.0, 1920, 1080)
    assert mid_out.zoom == pytest.approx(1.25, abs=1e-4)

    flt = build_crop_filter(kfs, width=1920, height=1080, duration=4.0, fps=30)
    assert "pow(" in flt
    assert "zoompan=" in flt


def test_ass_extended_styling_karaoke_animations_and_srt():
    settings = CaptionSettings(
        font_size=72,
        primary_color="&H00FFFFFF",
        highlight_color="&H0000FFFF",
        outline_color="&H00000000",
        back_color="&H40000000",
        outline_width=6,
        shadow=2,
        border_style=3,
        karaoke_highlight=True,
        animation="pop",
    )
    events = [
        SubtitleEvent(start_sec=0.5, end_sec=2.5, text="Hello Sims world"),
        SubtitleEvent(start_sec=2.6, end_sec=2.9, text="   "),
        SubtitleEvent(
            start_sec=3.0,
            end_sec=4.2,
            text="Fade out now",
            animation="fade",
            karaoke_highlight=False,
            border_style=1,
            primary_color="&H00112233",
        ),
    ]
    ass = render_ass_content(events, settings)
    assert "{\\kf" in ass
    assert "\\fscx85\\fscy85\\t(0,110,\\fscx100\\fscy100)" in ass
    assert "\\fad(120,120)" in ass
    # Per-clip border_style=1 when global is 3 emits DefaultOutline style
    assert "Style: DefaultOutline," in ass
    assert ",DefaultOutline,," in ass
    # Inline color tag strips 2-digit alpha (6 hex digits, not 8)
    assert "\\1c&H112233&" in ass
    assert "\\1c&H00112233&" not in ass

    # Per-clip karaoke_highlight=True when global is False sets both \1c (highlight) and \2c (base)
    non_karaoke_settings = CaptionSettings(
        primary_color="&H00FFFFFF",
        highlight_color="&H0000FFFF",
        karaoke_highlight=False,
    )
    ass_override = render_ass_content(
        [SubtitleEvent(start_sec=0.0, end_sec=2.0, text="Two words", karaoke_highlight=True)],
        non_karaoke_settings,
    )
    assert "\\1c&H00FFFF&\\2c&HFFFFFF&" in ass_override

    srt = render_srt_content(events)
    assert "00:00:00,500 --> 00:00:02,500" in srt
    assert "Hello Sims world" in srt
    assert "00:00:03,000 --> 00:00:04,200" in srt
    # Blank middle event skipped without leaving a gap in cue numbers
    assert "\n2\n00:00:03,000 --> 00:00:04,200\n" in srt
    assert "\n3\n" not in srt


def test_project_rename_duplicate_delete_srt_thumb_waveform_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ffmpeg_tools: tuple[Path, Path],
):
    ffmpeg_bin, _ = ffmpeg_tools
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    src_mp4 = _gen_test_mp4(ffmpeg_bin, tmp_path / "sample.mp4", dur=1.2)
    monkeypatch.setenv("SIMS_STUDIO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIMS_STUDIO_OFFLINE", "1")

    with TestClient(main_module.app) as client:
        token = main_module.app.state.session_token
        headers = {"X-Session-Token": token}

        # Create project via storage helper directly
        proj = create_project(
            project_id="proj_qol_api",
            title="Original Title",
            source_media=SourceMediaRef(
                media_id="src_main",
                path=str(src_mp4),
                fingerprint="fp_test",
                copied_to_project=False,
            ),
            data_dir=data_dir,
        )
        proj.tracks = [
            CaptionTrack(
                id="t1",
                name="Captions 1",
                clips=[
                    CaptionClip(
                        id="cap_01",
                        start_sec=0.0,
                        duration_sec=1.0,
                        auto_fit_tts=False,
                        display_text="Test SRT caption",
                        tts_enabled=False,
                    )
                ],
            )
        ]
        proj.markers = [TimelineMarker(id="m1", t=0.5, label="Hook", color="#f59e0b")]
        r_put = client.put(
            f"/api/projects/{proj.project_id}",
            headers=headers,
            json={"project": proj.model_dump(mode="json"), "expected_revision": proj.revision},
        )
        assert r_put.status_code == 200, r_put.text

        # Rename
        r_ren = client.patch(
            f"/api/projects/{proj.project_id}",
            headers=headers,
            json={"title": "Renamed Title"},
        )
        assert r_ren.status_code == 200, r_ren.text
        assert r_ren.json()["project"]["title"] == "Renamed Title"

        # SRT Download
        r_srt = client.get(f"/api/projects/{proj.project_id}/subtitles.srt")
        assert r_srt.status_code == 200
        assert "Test SRT caption" in r_srt.text
        assert "00:00:00,500" not in r_srt.text
        assert "00:00:00,000 --> 00:00:01,000" in r_srt.text

        # Thumbnail
        r_thumb = client.get(f"/api/projects/{proj.project_id}/assets/src_main/thumbnail")
        assert r_thumb.status_code == 200
        assert r_thumb.headers["content-type"].startswith("image/jpeg")
        assert len(r_thumb.content) > 200

        # Waveform
        r_wf = client.get(f"/api/projects/{proj.project_id}/assets/src_main/waveform?buckets=32")
        assert r_wf.status_code == 200
        peaks = r_wf.json()["peaks"]
        assert len(peaks) == 32
        assert max(peaks) > 0.1

        # Duplicate
        r_dup = client.post(
            f"/api/projects/{proj.project_id}/duplicate",
            headers=headers,
            json={"title": "Duplicated Copy"},
        )
        assert r_dup.status_code == 201, r_dup.text
        dup_id = r_dup.json()["project"]["project_id"]
        assert dup_id != proj.project_id
        assert r_dup.json()["project"]["title"] == "Duplicated Copy"

        # Delete original
        r_del = client.delete(f"/api/projects/{proj.project_id}", headers=headers)
        assert r_del.status_code == 200
        assert r_del.json()["deleted"] is True
        assert client.get(f"/api/projects/{proj.project_id}").status_code == 404


def test_render_with_flip_color_adjust_easing_and_crf(
    tmp_path: Path, ffmpeg_tools: tuple[Path, Path]
):
    ffmpeg_bin, ffprobe_bin = ffmpeg_tools
    src_mp4 = _gen_test_mp4(ffmpeg_bin, tmp_path / "src_render.mp4", dur=1.2)
    project_dir = tmp_path / "proj_render"
    project_dir.mkdir(parents=True, exist_ok=True)

    proj = ProjectInput(
        project_id="proj_qol_render",
        title="Render QOL",
        source_media=SourceMediaRef(
            media_id="src_1",
            path=str(src_mp4),
            fingerprint="fp",
        ),
        media_assets={
            "src_1": MediaAssetRef(
                asset_id="src_1",
                kind="video",
                name="src_render.mp4",
                path=str(src_mp4),
                fingerprint="fp",
            )
        },
        caption_settings=CaptionSettings(
            karaoke_highlight=True,
            animation="pop",
            border_style=3,
        ),
        camera_keyframes=[
            CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0, transition="ease_in_out"),
            CropKeyframe(t=1.0, x=0.2, y=0.0, zoom=1.4, transition="tween"),
        ],
        tracks=[
            VideoTrack(
                id="v1",
                name="Video 1",
                camera_follow=True,
                clips=[
                    VisualClip(
                        id="vclip_01",
                        asset_id="src_1",
                        kind="video",
                        start_sec=0.0,
                        duration_sec=1.0,
                        media_in=0.0,
                        media_out=1.0,
                        transform=VisualClipTransform(flip_h=True, flip_v=False),
                        color_adjust=VisualClipColorAdjust(
                            brightness=0.05, contrast=1.1, saturation=1.2
                        ),
                    )
                ],
            ),
            CaptionTrack(
                id="t1",
                name="Captions 1",
                clips=[
                    CaptionClip(
                        id="cap_01",
                        start_sec=0.0,
                        duration_sec=0.9,
                        auto_fit_tts=False,
                        display_text="Karaoke pop test",
                        tts_enabled=False,
                    )
                ],
            ),
        ],
    )
    manifest = ArtifactManifest(
        project_id=proj.project_id,
        source_metadata=SourceMetadata(
            width=640, height=360, fps=30.0, duration=1.2, has_audio=True
        ),
        media_metadata={
            "src_1": MediaAssetMetadata(
                kind="video", width=640, height=360, fps=30.0, duration=1.2, has_audio=True
            )
        },
    )

    out_mp4 = render_timeline_composite(
        project=proj,
        source_metadata=manifest.source_metadata,
        manifest=manifest,
        project_dir=project_dir,
        export_path=project_dir / "exports" / "custom_draft.mp4",
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
        crf=24,
        fps_override=24,
    )
    assert out_mp4.is_file()
    assert out_mp4.stat().st_size > 1000
