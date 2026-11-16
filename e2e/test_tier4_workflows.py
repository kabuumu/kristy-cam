"""Tier 4: Real-World Application Scenario E2E Tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from e2e.conftest import StudioClient, StudioServer
from e2e.fixtures.media_gen import (
    generate_sdr_cfr_mp4,
    generate_sdr_vfr_mp4,
    generate_silent_mp4,
    probe_media,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_v1_baseline_export(export_path: Path) -> None:
    """Verify V1 baseline contract: 1080x1920 SAR 1:1 30fps yuv420p H.264, 48kHz stereo AAC, +faststart."""
    assert export_path.is_file() and export_path.stat().st_size > 10_000
    info = probe_media(export_path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")

    assert v["width"] == 1080
    assert v["height"] == 1920
    assert v.get("sample_aspect_ratio", "1:1") == "1:1"
    assert v["codec_name"] == "h264"
    assert v["pix_fmt"] == "yuv420p"
    assert v["r_frame_rate"] == "30/1"
    assert v["avg_frame_rate"] == "30/1"

    assert a["codec_name"] == "aac"
    assert a["sample_rate"] == "48000"
    assert int(a["channels"]) == 2

    # A/V duration alignment within 1 output frame (1/30s) + small container tolerance
    assert abs(float(v["duration"]) - float(a["duration"])) <= (1.0 / 30.0) + 0.02

    # +faststart check: moov atom appears before mdat atom in MP4 header
    head = export_path.read_bytes()[:65536]
    moov_idx = head.find(b"moov")
    mdat_idx = head.find(b"mdat")
    assert moov_idx != -1, "moov atom not found in first 64KB (+faststart missing)"
    if mdat_idx != -1:
        assert moov_idx < mdat_idx, "moov must precede mdat for +faststart"


def _create_project(client: StudioClient, media_path: Path, title: str) -> tuple[str, dict[str, Any]]:
    sel = client.select_media(media_path)
    assert sel.status == 200, sel.text
    created = client.post(
        "/api/projects",
        json_body={"media_id": sel.json()["media_id"], "title": title},
    )
    assert created.status in (200, 201, 202), created.text
    payload = created.json()
    pid = payload.get("project_id") or payload["project"]["project_id"]
    if "job_id" in payload:
        assert client.wait_for_job(payload["job_id"])["status"] == "succeeded"
    return pid, client.get(f"/api/projects/{pid}").json()


def test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity(
    studio_server: StudioServer, tmp_path: Path
) -> None:
    """Import -> slice 3 segments -> keyframe pan/zoom -> TTS -> preview -> reorder -> export."""
    src = generate_sdr_cfr_mp4(tmp_path / "ravenswood_gameplay.mp4", 1920, 1080, 30, 6.0, True)
    src_hash_before = _sha256(src)
    client = studio_server.client

    pid, bundle = _create_project(client, src, "The Curse of Ravenswood Manor")
    p = bundle["project"]
    p["pronunciations"] = {"Vaelen": "Vay-len", "Windenburg": "Vin-den-burg"}
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 2.0,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [
                {"t": 0.0, "x": 0.15, "y": 0.0, "zoom": 1.0},
                {"t": 1.8, "x": 0.35, "y": 0.0, "zoom": 1.0},
            ],
            "clauses": [
                {"id": "c1", "display_text": "Lord Vaelen refused", "spoken_text_override": None},
                {"id": "c2", "display_text": "to leave Windenburg.", "spoken_text_override": None},
            ],
        },
        {
            "id": "seg_02",
            "video_in": 2.0,
            "video_out": 4.0,
            "duration_mode": "fit_narration",
            "source_audio_volume": 0.1,
            "speed": 1.1,
            "crop_keyframes": [
                {"t": 0.0, "x": 0.25, "y": 0.0, "zoom": 1.0},
                {"t": 1.5, "x": 0.35, "y": 0.15, "zoom": 1.5},
            ],
            "clauses": [
                {"id": "c3", "display_text": "Sul sul!", "spoken_text_override": "Sool sool!"},
            ],
        },
        {
            "id": "seg_03",
            "video_in": 4.0,
            "video_out": 5.5,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.25,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.28, "y": 0.0, "zoom": 1.0}],
            "clauses": [],
        },
    ]
    put1 = client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    assert put1.status == 200, put1.text

    for seg_id in ("seg_01", "seg_02", "seg_03"):
        assert client.wait_for_job(
            client.post(f"/api/projects/{pid}/segments/{seg_id}/tts").json()["job_id"]
        )["status"] == "succeeded"

    # Preview seg_01
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/segments/seg_01/preview").json()["job_id"]
    )["status"] == "succeeded"

    # Reorder seg_02 before seg_01, save, and export
    p2 = put1.json()["project"]
    p2["segments"] = [p2["segments"][1], p2["segments"][0], p2["segments"][2]]
    put2 = client.put(f"/api/projects/{pid}", json_body={"expected_revision": p2["revision"], "project": p2})
    assert put2.status == 200

    exp = client.wait_for_job(client.post(f"/api/projects/{pid}/export").json()["job_id"])
    assert exp["status"] == "succeeded", exp

    m = client.get(f"/api/projects/{pid}").json()["manifest"]
    out = studio_server.data_dir / "projects" / pid / m["latest_export_path"]
    _assert_v1_baseline_export(out)
    assert _sha256(src) == src_hash_before, "Original source recording must never be mutated"


def test_workflow_2_vfr_720p_gameplay_to_cfr_vertical_short(
    studio_server: StudioServer, tmp_path: Path
) -> None:
    """VFR 720p capture -> 2 narrated segments -> full 1080x1920 30fps CFR export."""
    vfr = generate_sdr_vfr_mp4(tmp_path / "obs_vfr_720p.mp4", duration=4.0)
    client = studio_server.client
    pid, bundle = _create_project(client, vfr, "VFR 720p Short")
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 2.0,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [{"id": "c1", "display_text": "VFR source normalized.", "spoken_text_override": None}],
        },
        {
            "id": "seg_02",
            "video_in": 2.0,
            "video_out": 3.5,
            "duration_mode": "fit_narration",
            "source_audio_volume": 0.15,
            "speed": 1.15,
            "crop_keyframes": [{"t": 0.0, "x": 0.3, "y": 0.1, "zoom": 1.25}],
            "clauses": [{"id": "c2", "display_text": "Smooth thirty FPS!", "spoken_text_override": None}],
        },
    ]
    client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    for seg_id in ("seg_01", "seg_02"):
        assert client.wait_for_job(
            client.post(f"/api/projects/{pid}/segments/{seg_id}/tts").json()["job_id"]
        )["status"] == "succeeded"

    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"

    m = client.get(f"/api/projects/{pid}").json()["manifest"]
    _assert_v1_baseline_export(studio_server.data_dir / "projects" / pid / m["latest_export_path"])


def test_workflow_3_silent_source_save_restart_and_export(
    studio_server: StudioServer, tmp_path: Path
) -> None:
    """Silent gameplay recording -> save -> restart backend -> reload & export."""
    silent = generate_silent_mp4(tmp_path / "silent_build.mp4", 1920, 1080, 30, 4.0)
    client = studio_server.client
    pid, bundle = _create_project(client, silent, "Speed Build Tour")
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 2.0,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.25, "y": 0.0, "zoom": 1.0}],
            "clauses": [{"id": "c1", "display_text": "Touring the manor.", "spoken_text_override": None}],
        }
    ]
    client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"

    # Restart server and verify project + TTS cache persist cleanly
    client2 = studio_server.restart()
    assert client2.wait_for_job(
        client2.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"
    m = client2.get(f"/api/projects/{pid}").json()["manifest"]
    _assert_v1_baseline_export(studio_server.data_dir / "projects" / pid / m["latest_export_path"])


def test_workflow_4_iterative_caption_and_speed_edits_with_range_playback(
    studio_server: StudioServer, sample_1080p_mp4: Path
) -> None:
    """Edit speed & caption text, re-synthesize, export, and stream final MP4 via HTTP 206 Range."""
    client = studio_server.client
    pid, bundle = _create_project(client, sample_1080p_mp4, "Iterative Edit")
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 2.0,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.25, "y": 0.0, "zoom": 1.0}],
            "clauses": [{"id": "c1", "display_text": "Draft one.", "spoken_text_override": None}],
        }
    ]
    r1 = client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"

    p2 = r1.json()["project"]
    p2["segments"][0]["speed"] = 1.2
    p2["segments"][0]["clauses"][0]["display_text"] = "Final polished line."
    client.put(f"/api/projects/{pid}", json_body={"expected_revision": p2["revision"], "project": p2})
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"

    m = client.get(f"/api/projects/{pid}").json()["manifest"]
    _assert_v1_baseline_export(studio_server.data_dir / "projects" / pid / m["latest_export_path"])

    export_art_id = m["latest_export_artifact_id"]
    r_stream = client.get(
        f"/api/projects/{pid}/media/{export_art_id}",
        headers={"Range": "bytes=0-2047"},
    )
    assert r_stream.status == 206
    assert len(r_stream.body) == 2048


def test_workflow_5_playwright_browser_studio_end_to_end(
    studio_client: StudioClient, sample_1080p_mp4: Path, browser_page: Any
) -> None:
    """Open Studio UI in headless Chromium, load project, verify proxy video & controls."""
    pid, bundle = _create_project(studio_client, sample_1080p_mp4, "Browser E2E Project")
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 2.0,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.25, "y": 0.0, "zoom": 1.0}],
            "clauses": [{"id": "c1", "display_text": "Browser test clause.", "spoken_text_override": None}],
        }
    ]
    studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )

    browser_page.goto(f"{studio_client.base_url}/?project={pid}", wait_until="networkidle")
    assert browser_page.locator("video").count() >= 1
    assert "Browser test clause." in browser_page.content()
