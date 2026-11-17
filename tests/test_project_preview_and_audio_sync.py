"""Tests for full-project preview rendering and stale TTS segment reporting."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.bootstrap import verify_ffmpeg
from backend.renderer import (
    compute_project_preview_hash,
    probe_rendered_video,
    render_project_preview,
)
from backend.schemas import (
    ArtifactManifest,
    ClauseInput,
    CropKeyframe,
    OutputSettings,
    ProjectInput,
    SegmentArtifactState,
    SegmentInput,
    SourceMediaRef,
    SourceMetadata,
)
from backend.storage import create_project, load_manifest, save_manifest, save_project
from backend.tts import compute_tts_hash


def _make_source_mp4(path: Path, duration: float = 3.0) -> None:
    ffmpeg_path, _ = verify_ffmpeg(offline=True)
    subprocess.run(
        [
            str(ffmpeg_path),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=1280x720:rate=30:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(path),
        ],
        check=True,
    )


def _poll_job(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        if data["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return data
        time.sleep(0.1)
    raise AssertionError(f"Job {job_id} timed out")


def test_render_project_preview_multi_segment_and_cache(tmp_path: Path) -> None:
    """render_project_preview concatenates all segments and reuses content-addressed cache."""
    ffmpeg_path, ffprobe_path = verify_ffmpeg(offline=True)
    src_mp4 = tmp_path / "src.mp4"
    _make_source_mp4(src_mp4, duration=3.0)

    proj_dir = tmp_path / "proj_prev"
    ref = SourceMediaRef(media_id="m1", path=str(src_mp4), fingerprint="sha256:test")
    meta = SourceMetadata(width=1280, height=720, fps=30.0, duration=3.0, has_audio=True)
    seg1 = SegmentInput(
        id="seg_01",
        video_in=0.0,
        video_out=1.0,
        crop_keyframes=[CropKeyframe(t=0.0, x=0.2, y=0.0, zoom=1.0)],
        clauses=[],
    )
    seg2 = SegmentInput(
        id="seg_02",
        video_in=1.0,
        video_out=2.2,
        crop_keyframes=[CropKeyframe(t=0.0, x=0.3, y=0.0, zoom=1.2)],
        clauses=[],
    )
    project = ProjectInput(
        project_id="proj_prev",
        title="Full Project Preview",
        source_media=ref,
        output_settings=OutputSettings(width=1080, height=1920, fps=30),
        segments=[seg1, seg2],
    )
    manifest = ArtifactManifest(project_id="proj_prev", source_metadata=meta)

    expected_hash = compute_project_preview_hash(project, manifest)
    out1 = render_project_preview(
        project=project,
        source_metadata=meta,
        manifest=manifest,
        project_dir=proj_dir,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    assert out1.is_file()
    assert out1.name == f"preview_project_{expected_hash}.mp4"

    info = probe_rendered_video(
        out1,
        ffprobe_path,
        expected_width=1080,
        expected_height=1920,
        expected_fps=30,
        require_audio=True,
    )
    # Combined duration of 1.0s + 1.2s = 2.2s
    assert 2.0 <= info["duration"] <= 2.4

    mtime_before = out1.stat().st_mtime_ns
    out2 = render_project_preview(
        project=project,
        source_metadata=meta,
        manifest=manifest,
        project_dir=proj_dir,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    assert out2 == out1
    assert out2.stat().st_mtime_ns == mtime_before


def test_api_project_preview_and_stale_tts_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/projects/{id}/preview renders full project and GET/PUT report stale_tts_segments."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SIMS_STUDIO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIMS_STUDIO_OFFLINE", "1")
    src_mp4 = tmp_path / "gameplay.mp4"
    _make_source_mp4(src_mp4, duration=3.0)

    with TestClient(main_module.app) as client:
        caps = client.get("/api/capabilities")
        assert caps.status_code == 200
        headers = {"X-Session-Token": client.app.state.session_token}

        created = create_project(
            project_id="proj_api_prev",
            title="API Preview Test",
            source_media=SourceMediaRef(
                media_id="m1",
                path=str(src_mp4),
                fingerprint="sha256:api_prev",
            ),
            data_dir=data_dir,
        )
        project = created.model_copy(
            update={
                "segments": [
                    SegmentInput(
                        id="seg_01",
                        video_in=0.0,
                        video_out=1.0,
                        crop_keyframes=[CropKeyframe(t=0.0, x=0.2, y=0.0, zoom=1.0)],
                        clauses=[],
                    ),
                    SegmentInput(
                        id="seg_02",
                        video_in=1.0,
                        video_out=2.0,
                        crop_keyframes=[CropKeyframe(t=0.0, x=0.25, y=0.0, zoom=1.0)],
                        clauses=[],
                    ),
                ]
            }
        )
        save_project(project, expected_revision=created.revision, data_dir=data_dir)
        meta = SourceMetadata(width=1280, height=720, fps=30.0, duration=3.0, has_audio=True)
        manifest = load_manifest("proj_api_prev", data_dir=data_dir)
        manifest.source_metadata = meta
        save_manifest(manifest, data_dir=data_dir)

        # Initially no spoken clauses -> no stale TTS segments
        r_get = client.get("/api/projects/proj_api_prev")
        assert r_get.status_code == 200
        assert r_get.json()["stale_tts_segments"] == []

        # Enqueue project-level preview
        r_prev = client.post("/api/projects/proj_api_prev/preview", headers=headers)
        assert r_prev.status_code == 202, r_prev.text
        job = _poll_job(client, r_prev.json()["job_id"])
        assert job["status"] == "succeeded", f"Job failed: {job.get('error')} | {job}"
        assert job["result"]["preview_artifact_id"] == "art_preview_latest"

        # Stream the project preview artifact
        r_media = client.get("/api/projects/proj_api_prev/media/art_preview_latest")
        assert r_media.status_code == 200
        assert len(r_media.content) > 1000

        # Add spoken narration to seg_01 and save -> stale_tts_segments should include seg_01
        proj_data = r_get.json()["project"]
        proj_data["segments"][0]["clauses"] = [
            {"id": "c1", "display_text": "Hello Sims!", "spoken_text_override": None}
        ]
        r_put = client.put(
            "/api/projects/proj_api_prev",
            headers=headers,
            json={"project": proj_data, "expected_revision": proj_data["revision"]},
        )
        assert r_put.status_code == 200, r_put.text
        assert r_put.json()["stale_tts_segments"] == ["seg_01"]

        # Simulate matching synthesized audio on disk -> stale_tts_segments becomes []
        saved_proj = ProjectInput.model_validate(r_put.json()["project"])
        tts_h = compute_tts_hash(
            segment=saved_proj.segments[0],
            pronunciations=saved_proj.pronunciations,
            voice=saved_proj.voice,
            language=saved_proj.language,
            inter_clause_pause_ms=saved_proj.output_settings.inter_clause_pause_ms,
        )
        proj_dir = data_dir / "projects" / "proj_api_prev"
        audio_rel = f"artifacts/tts_seg_01_{tts_h}.wav"
        (proj_dir / audio_rel).parent.mkdir(parents=True, exist_ok=True)
        (proj_dir / audio_rel).write_bytes(b"RIFF0000WAVEfmt ")
        latest_manifest = load_manifest("proj_api_prev", data_dir=data_dir)
        latest_manifest.segments["seg_01"] = SegmentArtifactState(
            tts_hash=tts_h,
            audio_artifact_id="art_tts_seg_01",
            audio_path=audio_rel,
            audio_duration=1.0,
        )
        save_manifest(latest_manifest, data_dir=data_dir)

        r_get2 = client.get("/api/projects/proj_api_prev")
        assert r_get2.status_code == 200
        assert r_get2.json()["stale_tts_segments"] == []

        # Edit clause text and save -> seg_01 becomes stale again
        proj_data2 = r_get2.json()["project"]
        proj_data2["segments"][0]["clauses"][0]["display_text"] = "Updated narration!"
        r_put2 = client.put(
            "/api/projects/proj_api_prev",
            headers=headers,
            json={"project": proj_data2, "expected_revision": proj_data2["revision"]},
        )
        assert r_put2.status_code == 200
        assert r_put2.json()["stale_tts_segments"] == ["seg_01"]


def test_slice_camera_keyframes_across_clip_boundary() -> None:
    """Project-level camera_keyframes interpolate seamlessly across clip cuts without resetting."""
    from backend.geometry import slice_camera_keyframes_for_segment

    kfs = [
        CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0, transition="tween"),
        CropKeyframe(t=2.0, x=0.5, y=0.0, zoom=2.0, transition="tween"),
    ]
    clip1 = slice_camera_keyframes_for_segment(kfs, 0.0, 1.0, 1920, 1080, 1080, 1920)
    clip2 = slice_camera_keyframes_for_segment(kfs, 1.0, 1.0, 1920, 1080, 1080, 1920)

    assert clip1[0].t == 0.0
    assert clip1[0].x == pytest.approx(0.1, abs=1e-4)
    assert clip1[0].zoom == pytest.approx(1.0, abs=1e-4)
    assert clip1[-1].t == pytest.approx(1.0, abs=1e-4)
    assert clip1[-1].x == pytest.approx(0.3, abs=1e-4)
    assert clip1[-1].zoom == pytest.approx(1.5, abs=1e-4)

    # Clip 2 must continue from the midpoint (0.3, zoom 1.5) rather than resetting
    assert clip2[0].t == 0.0
    assert clip2[0].x == pytest.approx(0.3, abs=1e-4)
    assert clip2[0].zoom == pytest.approx(1.5, abs=1e-4)
    assert clip2[-1].t == pytest.approx(1.0, abs=1e-4)
    assert clip2[-1].x == pytest.approx(0.5, abs=1e-4)
    assert clip2[-1].zoom == pytest.approx(2.0, abs=1e-4)

    # Snap transition holds prior keyframe until t=2.0
    snap_kfs = [
        CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0, transition="snap"),
        CropKeyframe(t=2.0, x=0.5, y=0.0, zoom=2.0, transition="tween"),
    ]
    snap_clip2 = slice_camera_keyframes_for_segment(snap_kfs, 1.0, 1.0, 1920, 1080, 1080, 1920)
    assert snap_clip2[0].x == pytest.approx(0.1, abs=1e-4)
    assert snap_clip2[0].zoom == pytest.approx(1.0, abs=1e-4)
    assert snap_clip2[0].transition == "snap"


def test_render_project_preview_with_project_camera_keyframes(tmp_path: Path) -> None:
    """render_project_preview uses project.camera_keyframes and invalidates hash on camera edits."""
    ffmpeg_path, ffprobe_path = verify_ffmpeg(offline=True)
    src_mp4 = tmp_path / "src_cam.mp4"
    _make_source_mp4(src_mp4, duration=3.0)

    proj_dir = tmp_path / "proj_cam"
    ref = SourceMediaRef(media_id="m1", path=str(src_mp4), fingerprint="sha256:cam")
    meta = SourceMetadata(width=1280, height=720, fps=30.0, duration=3.0, has_audio=True)
    seg1 = SegmentInput(
        id="seg_01",
        video_in=0.0,
        video_out=1.0,
        crop_keyframes=[CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0)],
        clauses=[],
    )
    seg2 = SegmentInput(
        id="seg_02",
        video_in=1.0,
        video_out=2.0,
        crop_keyframes=[CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0)],
        clauses=[],
    )
    project = ProjectInput(
        project_id="proj_cam",
        title="Independent Camera Test",
        source_media=ref,
        output_settings=OutputSettings(width=1080, height=1920, fps=30),
        camera_keyframes=[
            CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0, transition="tween"),
            CropKeyframe(t=2.0, x=0.5, y=0.0, zoom=1.5, transition="tween"),
        ],
        segments=[seg1, seg2],
    )
    manifest = ArtifactManifest(project_id="proj_cam", source_metadata=meta)

    h1 = compute_project_preview_hash(project, manifest)
    out = render_project_preview(
        project=project,
        source_metadata=meta,
        manifest=manifest,
        project_dir=proj_dir,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    assert out.is_file()
    info = probe_rendered_video(
        out,
        ffprobe_path,
        expected_width=1080,
        expected_height=1920,
        expected_fps=30,
        require_audio=True,
    )
    assert 1.8 <= info["duration"] <= 2.2

    project_moved = project.model_copy(
        update={
            "camera_keyframes": [
                CropKeyframe(t=0.0, x=0.2, y=0.0, zoom=1.2, transition="tween"),
                CropKeyframe(t=2.0, x=0.5, y=0.0, zoom=1.5, transition="tween"),
            ]
        }
    )
    h2 = compute_project_preview_hash(project_moved, manifest)
    assert h2 != h1

