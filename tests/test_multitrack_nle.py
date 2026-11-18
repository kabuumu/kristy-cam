"""Comprehensive tests for decoupled multi-track NLE schemas, APIs, TTS cache, and real FFmpeg compositing."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.main as main_module
import backend.tts as tts_module
from backend.bootstrap import verify_ffmpeg
from backend.media import probe_and_validate_asset
from backend.renderer import (
    RenderError,
    compute_timeline_render_hash,
    get_project_timeline_duration,
    probe_rendered_video,
    render_final_export,
    render_project_preview,
    render_timeline_composite,
)
from backend.schemas import (
    CURRENT_SCHEMA_VERSION,
    ArtifactManifest,
    AudioClip,
    AudioTrack,
    CaptionArtifactState,
    CaptionClip,
    CaptionStyleOverride,
    CaptionTrack,
    ClipEdgeTransition,
    CropKeyframe,
    MediaAssetMetadata,
    MediaAssetRef,
    OutputSettings,
    ProjectInput,
    SegmentArtifactState,
    SegmentInput,
    ClauseInput,
    ClauseTiming,
    SourceMediaRef,
    SourceMetadata,
    TrackTransition,
    VideoTrack,
    VisualClip,
    VisualClipTransform,
)
from backend.storage import create_project, load_manifest, migrate_legacy_segments_to_tracks, save_project
from backend.tts import compute_caption_tts_hash, synthesize_caption_tts


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------


class _FakeSess:
    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]


class FakeKokoro:
    """Deterministic CPU Kokoro fake that records synthesis calls."""

    def __init__(self) -> None:
        self.sess = _FakeSess()
        self.calls: list[dict[str, Any]] = []

    def get_voices(self) -> list[str]:
        return ["bf_emma", "af_bella", "am_adam"]

    def create(
        self, text: str, *, voice: str = "bf_emma", speed: float = 1.0, lang: str = "en-gb"
    ) -> tuple[np.ndarray, int]:
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        dur_sec = max(0.35, min(1.2, 0.25 + 0.04 * len(text))) / max(0.5, float(speed))
        n = int(round(24000 * dur_sec))
        t = np.arange(n, dtype=np.float32) / 24000.0
        samples = (0.35 * np.sin(2.0 * np.pi * 520.0 * t)).astype(np.float32)
        return samples, 24000


@pytest.fixture(scope="session")
def ffmpeg_tools() -> tuple[Path, Path]:
    return verify_ffmpeg(offline=True)


def _run_ffmpeg(ffmpeg_bin: Path, args: list[str]) -> None:
    res = subprocess.run(
        [str(ffmpeg_bin), "-y", "-hide_banner", "-loglevel", "error", *args],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"FFmpeg failed: {res.stderr}"


def _ffprobe_json(ffprobe_bin: Path, media_path: Path) -> dict[str, Any]:
    res = subprocess.run(
        [
            str(ffprobe_bin),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(media_path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout)


def _make_color_mp4(
    ffmpeg_bin: Path,
    out_path: Path,
    *,
    color: str = "blue",
    width: int = 1280,
    height: int = 720,
    duration: float = 3.0,
    fps: int = 30,
    with_audio: bool = True,
    freq: int = 440,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:size={width}x{height}:rate={fps}:duration={duration}",
    ]
    if with_audio:
        cmd.extend(["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={duration}"])
    cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    if with_audio:
        cmd.extend(["-c:a", "aac", "-ar", "48000", "-ac", "2"])
    cmd.extend(["-movflags", "+faststart", str(out_path)])
    _run_ffmpeg(ffmpeg_bin, cmd)
    return out_path


def _make_rgba_png(ffmpeg_bin: Path, out_path: Path, *, width: int = 400, height: int = 400) -> Path:
    """Create a real RGBA PNG (red box with transparent background border)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Build a semi-transparent colored square using geq in RGBA
    vf = (
        f"format=rgba,"
        f"geq=r='if(between(X,50,{width-50})*between(Y,50,{height-50}),255,0)':"
        f"g='0':b='0':"
        f"a='if(between(X,50,{width-50})*between(Y,50,{height-50}),255,0)'"
    )
    _run_ffmpeg(
        ffmpeg_bin,
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=black@0.0:size={width}x{height}:duration=0.1",
            "-vf",
            vf,
            "-frames:v",
            "1",
            str(out_path),
        ],
    )
    return out_path


def _make_wav(ffmpeg_bin: Path, out_path: Path, *, duration: float = 2.5, freq: int = 330) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        ffmpeg_bin,
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate=48000:duration={duration}",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(out_path),
        ],
    )
    return out_path


def _sample_rgb_frame(ffmpeg_bin: Path, video_path: Path, time_sec: float) -> np.ndarray:
    """Decode one RGB24 frame from video_path at time_sec as (H, W, 3) uint8 ndarray."""
    res = subprocess.run(
        [
            str(ffmpeg_bin),
            "-v",
            "error",
            "-ss",
            f"{time_sec:.4f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        shell=False,
        capture_output=True,
        check=True,
    )
    info = _ffprobe_json(verify_ffmpeg(offline=True)[1], video_path)
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    w, h = int(vs["width"]), int(vs["height"])
    arr = np.frombuffer(res.stdout, dtype=np.uint8)
    assert arr.size == w * h * 3
    return arr.reshape((h, w, 3))


def _poll_job(client: TestClient, job_id: str, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        if data["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} timed out")


@pytest.fixture()
def isolated_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    data_dir = tmp_path / "studio_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SIMS_STUDIO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIMS_STUDIO_OFFLINE", "1")
    with TestClient(main_module.app) as tc:
        yield tc


# ---------------------------------------------------------------------------
# 1. Schema, Migration & Duration Planning
# ---------------------------------------------------------------------------


def test_multitrack_schema_validation_and_migration(tmp_path: Path) -> None:
    """Legacy schema_version=1 projects cleanly migrate to independent V1/T1/A1 + global camera_keyframes."""
    assert CURRENT_SCHEMA_VERSION == 1
    src = SourceMediaRef(media_id="m1", path=str(tmp_path / "gameplay.mp4"), fingerprint="sha256:abc")
    seg1 = SegmentInput(
        id="seg_1",
        video_in=1.0,
        video_out=3.0,
        duration_mode="preserve_video",
        source_audio_volume=0.2,
        speed=1.1,
        crop_keyframes=[
            CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0),
            CropKeyframe(t=1.5, x=0.3, y=0.1, zoom=1.5, transition="snap"),
        ],
        clauses=[ClauseInput(id="c1", display_text="First line", spoken_text_override="Furst line")],
    )
    seg2 = SegmentInput(
        id="seg_2",
        video_in=4.0,
        video_out=5.5,
        duration_mode="preserve_video",
        source_audio_volume=0.4,
        speed=1.0,
        crop_keyframes=[CropKeyframe(t=0.0, x=0.25, y=0.0, zoom=1.0)],
        clauses=[ClauseInput(id="c2", display_text="Second line", spoken_text_override=None)],
    )
    proj = ProjectInput(
        project_id="proj_mig",
        title="Legacy Migrated",
        source_media=src,
        segments=[seg1, seg2],
    )
    manifest = ArtifactManifest(
        project_id="proj_mig",
        source_metadata=SourceMetadata(width=1920, height=1080, fps=30.0, duration=10.0, has_audio=True),
        segments={
            "seg_1": SegmentArtifactState(
                audio_duration=1.2,
                clause_timings=[ClauseTiming(clause_id="c1", start_sample=0, end_sample=28800, start_sec=0.0, end_sec=1.2)],
            )
        },
    )

    migrated = migrate_legacy_segments_to_tracks(proj, manifest)
    assert "m1" in migrated.media_assets
    assert len(migrated.tracks) == 3
    v1 = next(t for t in migrated.tracks if isinstance(t, VideoTrack))
    t1 = next(t for t in migrated.tracks if isinstance(t, CaptionTrack))
    assert len(v1.clips) == 2
    assert v1.clips[0].start_sec == 0.0
    assert v1.clips[0].duration_sec == 2.0
    assert v1.clips[1].start_sec == 2.0
    assert v1.clips[1].duration_sec == 1.5
    assert len(t1.clips) == 2
    assert t1.clips[0].start_sec == 0.0
    assert t1.clips[0].speed == 1.1
    assert t1.clips[1].start_sec == 2.0
    assert [kf.t for kf in migrated.camera_keyframes] == [0.0, 1.5, 2.0]

    # Duplicate clip ID across tracks must be rejected by Pydantic
    with pytest.raises(ValueError, match="Duplicate clip ID"):
        ProjectInput(
            project_id="bad_dup",
            title="Dup",
            source_media=src,
            tracks=[
                VideoTrack(
                    id="v1",
                    clips=[VisualClip(id="same_id", asset_id="m1", start_sec=0.0, duration_sec=1.0)],
                ),
                CaptionTrack(
                    id="t1",
                    clips=[CaptionClip(id="same_id", start_sec=0.0, display_text="Hi")],
                ),
            ],
        )


def test_caption_tts_hash_excludes_position_duration_and_style() -> None:
    """Moving, resizing, or styling a CaptionClip must preserve its Kokoro TTS hash."""
    base = CaptionClip(
        id="cap_1",
        start_sec=0.5,
        duration_sec=1.5,
        auto_fit_tts=True,
        display_text="Lord Vaelen arrived!",
        spoken_text_override=None,
        tts_enabled=True,
        speed=1.0,
        voice_override=None,
        style_override=None,
    )
    h1 = compute_caption_tts_hash(base, {"Vaelen": "Vay-len"}, "bf_emma", "en-gb")

    moved_resized_styled = base.model_copy(
        update={
            "start_sec": 7.25,
            "duration_sec": 4.0,
            "auto_fit_tts": False,
            "style_override": CaptionStyleOverride(align="top", font_size=72, margin_v=120),
        }
    )
    h2 = compute_caption_tts_hash(moved_resized_styled, {"Vaelen": "Vay-len"}, "bf_emma", "en-gb")
    assert h1 == h2

    edited_text = base.model_copy(update={"display_text": "Lord Vaelen departed!"})
    h3 = compute_caption_tts_hash(edited_text, {"Vaelen": "Vay-len"}, "bf_emma", "en-gb")
    assert h3 != h1


# ---------------------------------------------------------------------------
# 2. Multi-Track Asset Import, Range Streaming, Delete Guards & Caption TTS API
# ---------------------------------------------------------------------------


def test_asset_import_stream_delete_guards_and_caption_tts_api(
    isolated_client: TestClient, ffmpeg_tools: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg_bin, _ = ffmpeg_tools
    headers = {"X-Session-Token": isolated_client.app.state.session_token}

    src_mp4 = _make_color_mp4(ffmpeg_bin, tmp_path / "primary.mp4", color="blue", duration=2.0)
    overlay_png = _make_rgba_png(ffmpeg_bin, tmp_path / "badge.png", width=300, height=300)
    music_wav = _make_wav(ffmpeg_bin, tmp_path / "bgm.wav", duration=2.0)

    sel = isolated_client.post("/api/media/select", headers=headers, json={"path": str(src_mp4)})
    assert sel.status_code == 200, sel.text
    created = isolated_client.post(
        "/api/projects",
        headers=headers,
        json={"title": "Assets & TTS API", "media_id": sel.json()["media_id"]},
    )
    assert created.status_code == 202, created.text
    pid = created.json()["project"]["project_id"]
    assert _poll_job(isolated_client, created.json()["job_id"])["status"] == "succeeded"

    # Import RGBA PNG image
    r_img = isolated_client.post(
        f"/api/projects/{pid}/assets",
        headers=headers,
        json={"path": str(overlay_png), "kind": "image", "name": "Red Badge", "copy_to_project": True},
    )
    assert r_img.status_code == 201, r_img.text
    img_payload = r_img.json()
    img_id = img_payload["asset"]["asset_id"]
    assert img_payload["metadata"]["kind"] == "image"
    assert img_payload["metadata"]["has_alpha"] is True
    assert img_id in img_payload["project"]["media_assets"]

    # Import WAV audio
    r_aud = isolated_client.post(
        f"/api/projects/{pid}/assets",
        headers=headers,
        json={"path": str(music_wav), "kind": "audio", "name": "BGM"},
    )
    assert r_aud.status_code == 201, r_aud.text
    aud_id = r_aud.json()["asset"]["asset_id"]
    assert r_aud.json()["metadata"]["kind"] == "audio"
    assert r_aud.json()["metadata"]["has_audio"] is True

    # HTTP 200 & 206 Range stream for asset_id
    r_full = isolated_client.get(f"/api/projects/{pid}/media/{img_id}")
    assert r_full.status_code == 200
    assert r_full.headers["content-type"].startswith("image/png")
    assert len(r_full.content) > 100

    r_range = isolated_client.get(
        f"/api/projects/{pid}/media/{aud_id}", headers={"Range": "bytes=0-255"}
    )
    assert r_range.status_code == 206
    assert len(r_range.content) == 256

    # Cannot delete primary source_media (409)
    r_del_primary = isolated_client.delete(
        f"/api/projects/{pid}/assets/{sel.json()['media_id']}", headers=headers
    )
    assert r_del_primary.status_code == 409

    # Attach img_id to a track clip -> delete blocked by 409
    proj = r_aud.json()["project"]
    proj["tracks"] = [
        {
            "id": "v2",
            "name": "Overlay",
            "kind": "video",
            "visible": True,
            "muted": False,
            "locked": False,
            "camera_follow": False,
            "clips": [
                {
                    "id": "ov1",
                    "asset_id": img_id,
                    "kind": "image",
                    "start_sec": 0.5,
                    "duration_sec": 1.5,
                }
            ],
            "transitions": [],
        },
        {
            "id": "t1",
            "name": "Captions 1",
            "kind": "caption",
            "visible": True,
            "muted": False,
            "locked": False,
            "clips": [
                {"id": "cap_vo", "start_sec": 0.2, "display_text": "Hello Sims!", "tts_enabled": True},
                {"id": "cap_silent", "start_sec": 1.0, "display_text": "Visual only", "tts_enabled": False},
            ],
        },
    ]
    r_put = isolated_client.put(
        f"/api/projects/{pid}",
        headers=headers,
        json={"expected_revision": proj["revision"], "project": proj},
    )
    assert r_put.status_code == 200, r_put.text
    assert r_put.json()["stale_tts_captions"] == ["cap_vo"]

    r_del_ref = isolated_client.delete(f"/api/projects/{pid}/assets/{img_id}", headers=headers)
    assert r_del_ref.status_code == 409

    # Delete unreferenced audio asset succeeds
    r_del_aud = isolated_client.delete(f"/api/projects/{pid}/assets/{aud_id}", headers=headers)
    assert r_del_aud.status_code == 200
    assert aud_id not in r_del_aud.json()["project"]["media_assets"]

    # Synthesize TTS for independent CaptionClips with mocked CPU Kokoro
    fake_kokoro = FakeKokoro()
    monkeypatch.setattr(tts_module, "get_kokoro", lambda *a, **kw: fake_kokoro)

    r_tts = isolated_client.post(f"/api/projects/{pid}/tts", headers=headers, json={})
    assert r_tts.status_code == 202, r_tts.text
    tts_job = _poll_job(isolated_client, r_tts.json()["job_id"])
    assert tts_job["status"] == "succeeded", tts_job
    assert len(fake_kokoro.calls) == 1  # cap_silent skipped because tts_enabled=False

    b2 = isolated_client.get(f"/api/projects/{pid}").json()
    assert b2["stale_tts_captions"] == []
    cap_art = b2["manifest"]["caption_artifacts"]["cap_vo"]
    assert cap_art["audio_artifact_id"] == "art_cap_cap_vo"
    r_cap_wav = isolated_client.get(f"/api/projects/{pid}/media/art_cap_cap_vo")
    assert r_cap_wav.status_code == 200
    assert r_cap_wav.content[:4] == b"RIFF"

    # Global camera_keyframes out-of-bounds x rejected with 422
    bad_kf_proj = dict(b2["project"])
    bad_kf_proj["camera_keyframes"] = [{"t": 0.0, "x": 0.99, "y": 0.0, "zoom": 1.0}]
    r_bad_kf = isolated_client.put(
        f"/api/projects/{pid}",
        headers=headers,
        json={"expected_revision": bad_kf_proj["revision"], "project": bad_kf_proj},
    )
    assert r_bad_kf.status_code == 422

    # Add a V1 clip referencing primary source_media.media_id and relink primary source -> V1 clip asset_id updates automatically
    old_primary_id = b2["project"]["source_media"]["media_id"]
    with_v1_proj = dict(b2["project"])
    with_v1_proj["tracks"] = [
        {
            "id": "v1",
            "name": "Video 1",
            "kind": "video",
            "visible": True,
            "muted": False,
            "locked": False,
            "camera_follow": True,
            "clips": [
                {
                    "id": "v_primary",
                    "asset_id": old_primary_id,
                    "kind": "video",
                    "start_sec": 0.0,
                    "duration_sec": 1.5,
                }
            ],
            "transitions": [],
        },
        *b2["project"]["tracks"],
    ]
    r_put_v1 = isolated_client.put(
        f"/api/projects/{pid}",
        headers=headers,
        json={"expected_revision": with_v1_proj["revision"], "project": with_v1_proj},
    )
    assert r_put_v1.status_code == 200, r_put_v1.text

    replacement_mp4 = _make_color_mp4(ffmpeg_bin, tmp_path / "relinked_primary.mp4", color="green", duration=2.0)
    r_relink = isolated_client.post(
        f"/api/projects/{pid}/source/relink",
        headers=headers,
        json={"path": str(replacement_mp4), "copy_to_project": False},
    )
    assert r_relink.status_code == 200, r_relink.text
    relinked_proj = r_relink.json()["project"]
    new_primary_id = relinked_proj["source_media"]["media_id"]
    assert new_primary_id != old_primary_id
    assert new_primary_id in relinked_proj["media_assets"]
    assert old_primary_id not in relinked_proj["media_assets"]
    v1_after = next(t for t in relinked_proj["tracks"] if t["id"] == "v1")
    assert v1_after["clips"][0]["asset_id"] == new_primary_id



# ---------------------------------------------------------------------------
# 3. Real FFmpeg Multi-Track Compositor Renders
# ---------------------------------------------------------------------------


def test_render_zero_video_clips_captions_and_camera_only(
    ffmpeg_tools: tuple[Path, Path], tmp_path: Path
) -> None:
    """A 0-video-clip timeline with independent captions + camera keyframes exports cleanly."""
    ffmpeg_bin, ffprobe_bin = ffmpeg_tools
    src = _make_color_mp4(ffmpeg_bin, tmp_path / "src.mp4", color="black", duration=2.0)
    proj_dir = tmp_path / "proj_zero_video"
    src_ref = SourceMediaRef(media_id="m1", path=str(src), fingerprint="sha256:m1")
    fake_kokoro = FakeKokoro()

    cap = CaptionClip(
        id="cap_only",
        start_sec=0.5,
        duration_sec=1.5,
        auto_fit_tts=False,
        display_text="Caption without any video clip",
        tts_enabled=True,
    )
    tts_res = synthesize_caption_tts(
        cap, {}, "bf_emma", "en-gb", proj_dir / "artifacts", kokoro=fake_kokoro
    )
    assert tts_res.audio_path is not None

    proj = ProjectInput(
        project_id="proj_zero",
        title="Zero Video",
        source_media=src_ref,
        output_settings=OutputSettings(width=360, height=640, fps=30),
        camera_keyframes=[CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0), CropKeyframe(t=2.5, x=0.3, y=0.0, zoom=1.2)],
        tracks=[
            VideoTrack(id="v1", name="Video 1", camera_follow=True, clips=[]),
            CaptionTrack(id="t1", name="Captions 1", clips=[cap]),
        ],
    )
    manifest = ArtifactManifest(
        project_id="proj_zero",
        caption_artifacts={
            "cap_only": CaptionArtifactState(
                tts_hash=tts_res.tts_hash,
                audio_artifact_id="art_cap_cap_only",
                audio_path=tts_res.audio_path.relative_to(proj_dir).as_posix(),
                sample_rate=tts_res.sample_rate,
                total_samples=tts_res.total_samples,
                audio_duration=tts_res.audio_duration,
            )
        },
    )

    raw_dur, _fc, _fd = get_project_timeline_duration(proj, manifest)
    assert raw_dur == pytest.approx(2.5, abs=0.01)

    out_mp4 = render_timeline_composite(
        project=proj,
        source_metadata=None,
        manifest=manifest,
        project_dir=proj_dir,
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
    )
    info = probe_rendered_video(
        out_mp4, ffprobe_bin, expected_width=360, expected_height=640, expected_fps=30, require_audio=True
    )
    assert 2.4 <= info["duration"] <= 2.65


def test_render_video_gaps_and_rgba_overlay_compositing(
    ffmpeg_tools: tuple[Path, Path], tmp_path: Path
) -> None:
    """Video clips leave black gaps; RGBA PNG overlay on V2 composites with transform & alpha."""
    ffmpeg_bin, ffprobe_bin = ffmpeg_tools
    blue_mp4 = _make_color_mp4(ffmpeg_bin, tmp_path / "blue.mp4", color="blue", duration=3.0)
    green_mp4 = _make_color_mp4(ffmpeg_bin, tmp_path / "green.mp4", color="green", duration=3.0)
    badge_png = _make_rgba_png(ffmpeg_bin, tmp_path / "red_badge.png", width=200, height=200)

    proj_dir = tmp_path / "proj_gaps_overlay"
    src_ref = SourceMediaRef(media_id="m_blue", path=str(blue_mp4), fingerprint="sha256:blue")
    proj = ProjectInput(
        project_id="proj_gaps",
        title="Gaps and Overlay",
        source_media=src_ref,
        media_assets={
            "m_blue": MediaAssetRef(asset_id="m_blue", kind="video", name="blue.mp4", path=str(blue_mp4), fingerprint="sha256:blue"),
            "m_green": MediaAssetRef(asset_id="m_green", kind="video", name="green.mp4", path=str(green_mp4), fingerprint="sha256:green"),
            "m_badge": MediaAssetRef(asset_id="m_badge", kind="image", name="red_badge.png", path=str(badge_png), fingerprint="sha256:badge"),
        },
        output_settings=OutputSettings(width=360, height=640, fps=30),
        tracks=[
            VideoTrack(
                id="v1",
                name="Video 1",
                camera_follow=True,
                clips=[
                    VisualClip(
                        id="c_blue",
                        asset_id="m_blue",
                        kind="video",
                        start_sec=0.0,
                        duration_sec=1.0,
                        transition_in=ClipEdgeTransition(type="fade", duration_sec=0.2),
                    ),
                    # Explicit black gap from 1.0s to 2.0s
                    VisualClip(
                        id="c_green",
                        asset_id="m_green",
                        kind="video",
                        start_sec=2.0,
                        duration_sec=1.0,
                    ),
                ],
            ),
            VideoTrack(
                id="v2",
                name="Overlay",
                camera_follow=False,
                clips=[
                    VisualClip(
                        id="ov_red",
                        asset_id="m_badge",
                        kind="image",
                        start_sec=1.2,
                        duration_sec=1.2,  # visible over gap (1.2..2.0) AND over green (2.0..2.4)
                        transform=VisualClipTransform(x=0.5, y=0.5, scale=0.6, rotation_deg=0.0, opacity=1.0),
                        transition_in=ClipEdgeTransition(type="slide_left", duration_sec=0.2),
                        transition_out=ClipEdgeTransition(type="wipe_left", duration_sec=0.2),
                    )
                ],
            ),
        ],
    )
    manifest = ArtifactManifest(
        project_id="proj_gaps",
        media_metadata={
            "m_blue": probe_and_validate_asset(blue_mp4, expected_kind="video", ffmpeg_path=ffmpeg_bin, ffprobe_path=ffprobe_bin),
            "m_green": probe_and_validate_asset(green_mp4, expected_kind="video", ffmpeg_path=ffmpeg_bin, ffprobe_path=ffprobe_bin),
            "m_badge": probe_and_validate_asset(badge_png, expected_kind="image", ffmpeg_path=ffmpeg_bin, ffprobe_path=ffprobe_bin),
        },
    )

    out_mp4 = render_final_export(
        project=proj,
        source_metadata=None,
        manifest=manifest,
        project_dir=proj_dir,
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
    )
    info = probe_rendered_video(
        out_mp4, ffprobe_bin, expected_width=360, expected_height=640, expected_fps=30, require_audio=True
    )
    assert 2.9 <= info["duration"] <= 3.1

    # Verify visual pixels across timeline phases!
    # At t=0.5s: blue base (B dominant)
    f_blue = _sample_rgb_frame(ffmpeg_bin, out_mp4, 0.5)
    assert f_blue[320, 180, 2] > 180 and f_blue[320, 180, 0] < 40

    # At t=1.6s (gap + overlay settled): outer corner is black gap, center is red badge (alpha respected!)
    f_gap_ov = _sample_rgb_frame(ffmpeg_bin, out_mp4, 1.6)
    assert f_gap_ov[10, 10].max() < 25  # Black background outside badge
    assert f_gap_ov[320, 180, 0] > 180 and f_gap_ov[320, 180, 1] < 40  # Red center from RGBA PNG

    # At t=2.2s (green base + red overlay): outer corner is green, center is red
    f_green_ov = _sample_rgb_frame(ffmpeg_bin, out_mp4, 2.2)
    assert f_green_ov[20, 20, 1] > 100 and f_green_ov[20, 20, 0] < 40
    assert f_green_ov[320, 180, 0] > 180 and f_green_ov[320, 180, 1] < 40

    # At t=2.7s (green base only): center is green
    f_green = _sample_rgb_frame(ffmpeg_bin, out_mp4, 2.7)
    assert f_green[320, 180, 1] > 100 and f_green[320, 180, 0] < 40


@pytest.mark.parametrize("tr_type", ["crossfade", "wipeleft", "fadeblack", "zoomin"])
def test_render_same_track_xfade_and_acrossfade(
    ffmpeg_tools: tuple[Path, Path], tmp_path: Path, tr_type: str
) -> None:
    """Same-track TrackTransition xfade/acrossfade renders cleanly for multiple transition types."""
    ffmpeg_bin, ffprobe_bin = ffmpeg_tools
    red_mp4 = _make_color_mp4(ffmpeg_bin, tmp_path / f"red_{tr_type}.mp4", color="red", duration=2.0, freq=330)
    blue_mp4 = _make_color_mp4(ffmpeg_bin, tmp_path / f"blue_{tr_type}.mp4", color="blue", duration=2.0, freq=660)

    proj_dir = tmp_path / f"proj_xf_{tr_type}"
    src_ref = SourceMediaRef(media_id="m_red", path=str(red_mp4), fingerprint="sha256:red")
    proj = ProjectInput(
        project_id=f"proj_xf_{tr_type}",
        title="Xfade Test",
        source_media=src_ref,
        media_assets={
            "m_red": MediaAssetRef(asset_id="m_red", kind="video", name="red.mp4", path=str(red_mp4), fingerprint="sha256:red"),
            "m_blue": MediaAssetRef(asset_id="m_blue", kind="video", name="blue.mp4", path=str(blue_mp4), fingerprint="sha256:blue"),
        },
        output_settings=OutputSettings(width=360, height=640, fps=30),
        tracks=[
            VideoTrack(
                id="v1",
                camera_follow=True,
                clips=[
                    VisualClip(id="c1", asset_id="m_red", kind="video", start_sec=0.0, duration_sec=1.5),
                    VisualClip(id="c2", asset_id="m_blue", kind="video", start_sec=1.5, duration_sec=1.5),
                ],
                transitions=[
                    TrackTransition(
                        id="tr_12",
                        from_clip_id="c1",
                        to_clip_id="c2",
                        type=tr_type,  # type: ignore[arg-type]
                        duration_sec=0.5,
                    )
                ],
            )
        ],
    )
    manifest = ArtifactManifest(
        project_id=proj.project_id,
        media_metadata={
            "m_red": probe_and_validate_asset(red_mp4, expected_kind="video", ffmpeg_path=ffmpeg_bin, ffprobe_path=ffprobe_bin),
            "m_blue": probe_and_validate_asset(blue_mp4, expected_kind="video", ffmpeg_path=ffmpeg_bin, ffprobe_path=ffprobe_bin),
        },
    )

    out_mp4 = render_timeline_composite(
        project=proj,
        source_metadata=None,
        manifest=manifest,
        project_dir=proj_dir,
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
    )
    info = probe_rendered_video(
        out_mp4, ffprobe_bin, expected_width=360, expected_height=640, expected_fps=30, require_audio=True
    )
    # 1.5s + 1.5s - 0.5s overlap = 2.5s
    assert 2.4 <= info["duration"] <= 2.65

    # Verify before & after transition colors
    f_start = _sample_rgb_frame(ffmpeg_bin, out_mp4, 0.3)
    assert f_start[320, 180, 0] > 180 and f_start[320, 180, 2] < 40
    f_end = _sample_rgb_frame(ffmpeg_bin, out_mp4, 2.2)
    assert f_end[320, 180, 2] > 180 and f_end[320, 180, 0] < 40


def test_render_audio_tracks_solo_mute_loop_and_narration_ducking(
    ffmpeg_tools: tuple[Path, Path], tmp_path: Path
) -> None:
    """AudioTrack solo/mute, looping, and sidechaincompress ducking under CaptionClip narration render cleanly."""
    ffmpeg_bin, ffprobe_bin = ffmpeg_tools
    src = _make_color_mp4(ffmpeg_bin, tmp_path / "src.mp4", color="navy", duration=2.0, with_audio=False)
    bgm_short = _make_wav(ffmpeg_bin, tmp_path / "bgm_short.wav", duration=0.8, freq=220)
    sfx_wav = _make_wav(ffmpeg_bin, tmp_path / "sfx.wav", duration=0.6, freq=880)
    proj_dir = tmp_path / "proj_audio_mix"
    fake_kokoro = FakeKokoro()

    cap = CaptionClip(
        id="cap_nar",
        start_sec=1.0,
        duration_sec=1.0,
        auto_fit_tts=False,
        display_text="Narration triggers music ducking",
        tts_enabled=True,
    )
    tts_res = synthesize_caption_tts(
        cap, {}, "bf_emma", "en-gb", proj_dir / "artifacts", kokoro=fake_kokoro
    )
    assert tts_res.audio_path is not None

    src_ref = SourceMediaRef(media_id="m_src", path=str(src), fingerprint="sha256:src")
    proj = ProjectInput(
        project_id="proj_audio_mix",
        title="Audio Mix",
        source_media=src_ref,
        media_assets={
            "m_src": MediaAssetRef(asset_id="m_src", kind="video", name="src.mp4", path=str(src), fingerprint="sha256:src"),
            "m_bgm": MediaAssetRef(asset_id="m_bgm", kind="audio", name="bgm.wav", path=str(bgm_short), fingerprint="sha256:bgm"),
            "m_sfx": MediaAssetRef(asset_id="m_sfx", kind="audio", name="sfx.wav", path=str(sfx_wav), fingerprint="sha256:sfx"),
        },
        output_settings=OutputSettings(width=360, height=640, fps=30),
        tracks=[
            VideoTrack(
                id="v1",
                camera_follow=True,
                clips=[VisualClip(id="v_01", asset_id="m_src", kind="video", start_sec=0.0, duration_sec=3.0)],
            ),
            CaptionTrack(id="t1", clips=[cap]),
            AudioTrack(
                id="a1",
                name="Music Solo Duck",
                volume=0.8,
                muted=False,
                solo=True,
                duck_under_narration=True,
                clips=[
                    AudioClip(
                        id="ac_bgm",
                        asset_id="m_bgm",
                        start_sec=0.0,
                        duration_sec=2.8,  # longer than 0.8s wav -> tests aloop!
                        fade_in_sec=0.2,
                        fade_out_sec=0.3,
                        loop=True,
                    )
                ],
            ),
            AudioTrack(
                id="a2",
                name="Muted/Non-Solo Track",
                volume=1.0,
                muted=False,
                solo=False,
                duck_under_narration=False,
                clips=[AudioClip(id="ac_sfx", asset_id="m_sfx", start_sec=0.4, duration_sec=0.6)],
            ),
        ],
    )
    manifest = ArtifactManifest(
        project_id=proj.project_id,
        media_metadata={
            "m_src": probe_and_validate_asset(src, expected_kind="video", ffmpeg_path=ffmpeg_bin, ffprobe_path=ffprobe_bin),
            "m_bgm": probe_and_validate_asset(bgm_short, expected_kind="audio", ffmpeg_path=ffmpeg_bin, ffprobe_path=ffprobe_bin),
            "m_sfx": probe_and_validate_asset(sfx_wav, expected_kind="audio", ffmpeg_path=ffmpeg_bin, ffprobe_path=ffprobe_bin),
        },
        caption_artifacts={
            "cap_nar": CaptionArtifactState(
                tts_hash=tts_res.tts_hash,
                audio_artifact_id="art_cap_cap_nar",
                audio_path=tts_res.audio_path.relative_to(proj_dir).as_posix(),
                sample_rate=tts_res.sample_rate,
                total_samples=tts_res.total_samples,
                audio_duration=tts_res.audio_duration,
            )
        },
    )

    h1 = compute_timeline_render_hash(proj, manifest)
    out1 = render_project_preview(
        project=proj,
        source_metadata=None,
        manifest=manifest,
        project_dir=proj_dir,
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
    )
    assert out1.is_file()
    assert out1.name == f"preview_project_{h1}.mp4"
    info = probe_rendered_video(
        out1, ffprobe_bin, expected_width=360, expected_height=640, expected_fps=30, require_audio=True
    )
    assert 2.9 <= info["duration"] <= 3.1

    # Render hash cache reuse
    mtime_before = out1.stat().st_mtime_ns
    out2 = render_project_preview(
        project=proj,
        source_metadata=None,
        manifest=manifest,
        project_dir=proj_dir,
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
    )
    assert out2 == out1
    assert out2.stat().st_mtime_ns == mtime_before
