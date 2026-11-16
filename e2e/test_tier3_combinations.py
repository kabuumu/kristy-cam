"""Tier 3: Cross-Feature Combination E2E Tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from e2e.conftest import StudioClient, StudioServer
from e2e.fixtures.media_gen import generate_sdr_cfr_mp4, generate_silent_mp4, probe_media


def _create_project(
    client: StudioClient, media_path: Path, *, copy_to_project: bool = False
) -> tuple[str, dict[str, Any]]:
    sel = client.select_media(media_path, copy_to_project=copy_to_project)
    assert sel.status == 200, sel.text
    created = client.post(
        "/api/projects",
        json_body={
            "media_id": sel.json()["media_id"],
            "title": "Combo Test",
            "copy_to_project": copy_to_project,
        },
    )
    assert created.status in (200, 201, 202), created.text
    payload = created.json()
    pid = payload.get("project_id") or payload["project"]["project_id"]
    if "job_id" in payload:
        assert client.wait_for_job(payload["job_id"])["status"] == "succeeded"
    return pid, client.get(f"/api/projects/{pid}").json()


def test_combo_crop_zoompan_tts_and_captions_export(
    studio_server: StudioServer, sample_1080p_mp4: Path
) -> None:
    client = studio_server.client
    pid, bundle = _create_project(client, sample_1080p_mp4)
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 2.5,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [
                {"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0},
                {"t": 2.0, "x": 0.35, "y": 0.15, "zoom": 1.5},
            ],
            "clauses": [
                {"id": "c1", "display_text": "Zooming in on", "spoken_text_override": None},
                {"id": "c2", "display_text": "Ravenswood Manor.", "spoken_text_override": None},
            ],
        }
    ]
    client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"

    m = client.get(f"/api/projects/{pid}").json()["manifest"]
    out = studio_server.data_dir / "projects" / pid / m["latest_export_path"]
    info = probe_media(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert v["width"] == 1080 and v["height"] == 1920
    assert v["r_frame_rate"] == "30/1"
    assert a["sample_rate"] == "48000"


def test_combo_segment_reorder_preserves_stage1_render_hashes(
    studio_client: StudioClient, sample_1080p_mp4: Path
) -> None:
    pid, bundle = _create_project(studio_client, sample_1080p_mp4)
    p = bundle["project"]
    seg1 = {
        "id": "seg_01",
        "video_in": 0.0,
        "video_out": 1.5,
        "duration_mode": "preserve_video",
        "source_audio_volume": 0.15,
        "speed": 1.0,
        "crop_keyframes": [{"t": 0.0, "x": 0.1, "y": 0.0, "zoom": 1.0}],
        "clauses": [],
    }
    seg2 = {
        "id": "seg_02",
        "video_in": 1.5,
        "video_out": 3.0,
        "duration_mode": "preserve_video",
        "source_audio_volume": 0.15,
        "speed": 1.0,
        "crop_keyframes": [{"t": 0.0, "x": 0.4, "y": 0.0, "zoom": 1.0}],
        "clauses": [],
    }
    p["segments"] = [seg1, seg2]
    r1 = studio_client.put(
        f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p}
    )
    assert r1.status == 200
    assert studio_client.wait_for_job(
        studio_client.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"
    m1 = studio_client.get(f"/api/projects/{pid}").json()["manifest"]["segments"]
    h1_a, h2_a = m1["seg_01"]["render_hash"], m1["seg_02"]["render_hash"]

    # Reorder segments: Stage 1 render_hash values must remain valid & unchanged
    p2 = r1.json()["project"]
    p2["segments"] = [seg2, seg1]
    r2 = studio_client.put(
        f"/api/projects/{pid}", json_body={"expected_revision": p2["revision"], "project": p2}
    )
    assert r2.status == 200
    assert studio_client.wait_for_job(
        studio_client.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"
    m2 = studio_client.get(f"/api/projects/{pid}").json()["manifest"]["segments"]
    assert m2["seg_01"]["render_hash"] == h1_a
    assert m2["seg_02"]["render_hash"] == h2_a


def test_combo_fit_narration_freeze_and_captions(
    studio_server: StudioServer, tmp_path: Path
) -> None:
    short_src = generate_sdr_cfr_mp4(tmp_path / "short_fit.mp4", 1280, 720, 30, 1.5, True)
    client = studio_server.client
    pid, bundle = _create_project(client, short_src)
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.5,
            "video_out": 1.5,
            "duration_mode": "fit_narration",
            "source_audio_volume": 0.15,
            "speed": 0.9,
            "crop_keyframes": [{"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [
                {
                    "id": "c1",
                    "display_text": "Extending past source end with freeze frame.",
                    "spoken_text_override": None,
                }
            ],
        }
    ]
    client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"


def test_combo_relink_and_export(
    studio_client: StudioClient, tmp_path: Path
) -> None:
    src1 = generate_sdr_cfr_mp4(tmp_path / "src1.mp4", 1280, 720, 30, 2.0, True)
    src2 = generate_sdr_cfr_mp4(tmp_path / "src2.mp4", 1920, 1080, 30, 3.0, True)
    pid, bundle = _create_project(studio_client, src1)
    fp1 = bundle["project"]["source_media"]["fingerprint"]

    rel = studio_client.post(f"/api/projects/{pid}/source/relink", json_body={"path": str(src2)})
    assert rel.status == 200, rel.text
    after = studio_client.get(f"/api/projects/{pid}").json()
    p = after["project"]
    assert p["source_media"]["fingerprint"] != fp1

    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 1.5,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.25, "y": 0.0, "zoom": 1.0}],
            "clauses": [],
        }
    ]
    studio_client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    assert studio_client.wait_for_job(
        studio_client.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"


def test_combo_pronunciation_override_speed_and_tts_hash_invalidation(
    studio_client: StudioClient, sample_1080p_mp4: Path
) -> None:
    pid, bundle = _create_project(studio_client, sample_1080p_mp4)
    p = bundle["project"]
    p["pronunciations"] = {"Windenburg": "Vin-den-burg"}
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 2.5,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [
                {"id": "c1", "display_text": "Welcome to Windenburg!", "spoken_text_override": None},
                {"id": "c2", "display_text": "Dag dag!", "spoken_text_override": "Dahg dahg!"},
            ],
        }
    ]
    r1 = studio_client.put(
        f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p}
    )
    assert studio_client.wait_for_job(
        studio_client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"
    h1 = studio_client.get(f"/api/projects/{pid}").json()["manifest"]["segments"]["seg_01"]["tts_hash"]

    # Change speed -> tts_hash must change
    p2 = r1.json()["project"]
    p2["segments"][0]["speed"] = 1.2
    studio_client.put(
        f"/api/projects/{pid}", json_body={"expected_revision": p2["revision"], "project": p2}
    )
    assert studio_client.wait_for_job(
        studio_client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"
    h2 = studio_client.get(f"/api/projects/{pid}").json()["manifest"]["segments"]["seg_01"]["tts_hash"]
    assert h1 != h2
    assert studio_client.wait_for_job(
        studio_client.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"


def test_combo_silent_source_fit_narration_freeze_and_captions(
    studio_server: StudioServer, tmp_path: Path
) -> None:
    silent = generate_silent_mp4(tmp_path / "silent_short.mp4", 1280, 720, 30, 1.5)
    client = studio_server.client
    pid, bundle = _create_project(client, silent)
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.5,
            "video_out": 1.5,
            "duration_mode": "fit_narration",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [
                {"id": "c1", "display_text": "Silent gameplay with voiceover.", "spoken_text_override": None}
            ],
        }
    ]
    client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"

    m = client.get(f"/api/projects/{pid}").json()["manifest"]
    out = studio_server.data_dir / "projects" / pid / m["latest_export_path"]
    info = probe_media(out)
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert a["sample_rate"] == "48000" and int(a["channels"]) == 2


def test_combo_copied_to_project_portability_survives_source_deletion(
    studio_client: StudioClient, tmp_path: Path
) -> None:
    ext_src = generate_sdr_cfr_mp4(tmp_path / "external_src.mp4", 1280, 720, 30, 2.0, True)
    pid, bundle = _create_project(studio_client, ext_src, copy_to_project=True)
    ext_src.unlink()
    assert not ext_src.exists()

    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 1.5,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [{"id": "c1", "display_text": "Self contained!", "spoken_text_override": None}],
        }
    ]
    studio_client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    assert studio_client.wait_for_job(
        studio_client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"
    assert studio_client.wait_for_job(
        studio_client.post(f"/api/projects/{pid}/export").json()["job_id"]
    )["status"] == "succeeded"
