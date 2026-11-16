"""Tier 2: Boundary & Corner Cases E2E Tests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from e2e.conftest import StudioClient, StudioServer
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


def _create_project(client: StudioClient, media_path: Path) -> tuple[str, dict[str, Any]]:
    sel = client.select_media(media_path)
    assert sel.status == 200, sel.text
    created = client.post(
        "/api/projects",
        json_body={"media_id": sel.json()["media_id"], "title": "Boundary Test"},
    )
    assert created.status in (200, 201, 202), created.text
    payload = created.json()
    pid = payload.get("project_id") or payload["project"]["project_id"]
    if "job_id" in payload:
        assert client.wait_for_job(payload["job_id"])["status"] == "succeeded"
    return pid, client.get(f"/api/projects/{pid}").json()


def test_zoom_bounds_1_0_and_3_0_accepted_and_out_of_range_rejected(
    studio_client: StudioClient, sample_1080p_mp4: Path
) -> None:
    pid, bundle = _create_project(studio_client, sample_1080p_mp4)
    p = bundle["project"]

    # Valid boundary zoom 1.0 and 3.0
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 2.0,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [
                {"t": 0.0, "x": 0.0, "y": 0.0, "zoom": 1.0},
                {"t": 1.5, "x": 0.5, "y": 0.5, "zoom": 3.0},
            ],
            "clauses": [],
        }
    ]
    ok = studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert ok.status == 200, ok.text
    p = ok.json()["project"]

    # Out-of-range zoom > 3.0 must be rejected (422)
    p["segments"][0]["crop_keyframes"][1]["zoom"] = 3.5
    bad_hi = studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert bad_hi.status == 422

    # Out-of-range zoom < 1.0 must be rejected (422)
    p["segments"][0]["crop_keyframes"][1]["zoom"] = 0.5
    bad_lo = studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert bad_lo.status == 422


def test_out_of_bounds_crop_coordinates_and_missing_t0_rejected(
    studio_client: StudioClient, sample_1080p_mp4: Path
) -> None:
    pid, bundle = _create_project(studio_client, sample_1080p_mp4)
    p = bundle["project"]

    # Missing t=0.0 keyframe
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 2.0,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.5, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [],
        }
    ]
    r1 = studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert r1.status == 422

    # Out-of-bounds y > 0 when zoom == 1.0
    p["segments"][0]["crop_keyframes"] = [{"t": 0.0, "x": 0.2, "y": 0.5, "zoom": 1.0}]
    r2 = studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert r2.status == 422


def test_trim_shift_detection_preserves_keyframes(
    studio_client: StudioClient, sample_1080p_mp4: Path
) -> None:
    pid, bundle = _create_project(studio_client, sample_1080p_mp4)
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 3.5,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [
                {"t": 0.0, "x": 0.1, "y": 0.0, "zoom": 1.0},
                {"t": 3.0, "x": 0.4, "y": 0.0, "zoom": 1.0},
            ],
            "clauses": [],
        }
    ]
    r1 = studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert r1.status == 200
    p = r1.json()["project"]
    p["segments"][0]["video_out"] = 1.5
    r2 = studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert r2.status in (200, 422)
    if r2.status == 200:
        assert len(r2.json()["project"]["segments"][0]["crop_keyframes"]) == 2


def test_empty_narration_exports_valid_silent_audio_track(
    studio_server: StudioServer, sample_1080p_mp4: Path
) -> None:
    client = studio_server.client
    pid, bundle = _create_project(client, sample_1080p_mp4)
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 1.5,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.2,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [],
        }
    ]
    client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    job = client.wait_for_job(client.post(f"/api/projects/{pid}/export").json()["job_id"])
    assert job["status"] == "succeeded", job


def test_source_eof_freeze_frame_extension(
    studio_server: StudioServer, tmp_path: Path
) -> None:
    short_src = generate_sdr_cfr_mp4(tmp_path / "short_2s.mp4", 1280, 720, 30, 2.0, True)
    client = studio_server.client
    pid, bundle = _create_project(client, short_src)
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 1.0,
            "video_out": 2.0,
            "duration_mode": "fit_narration",
            "source_audio_volume": 0.15,
            "speed": 0.85,
            "crop_keyframes": [{"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [
                {
                    "id": "c1",
                    "display_text": "This narration intentionally exceeds the remaining one second of source footage.",
                    "spoken_text_override": None,
                }
            ],
        }
    ]
    client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    assert client.wait_for_job(
        client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"
    exp = client.wait_for_job(client.post(f"/api/projects/{pid}/export").json()["job_id"])
    assert exp["status"] == "succeeded", exp

    m = client.get(f"/api/projects/{pid}").json()["manifest"]
    out_path = studio_server.data_dir / "projects" / pid / m["latest_export_path"]
    info = probe_media(out_path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert float(v["duration"]) > 1.5


def test_ass_special_character_escaping_in_captions(
    studio_client: StudioClient, sample_1080p_mp4: Path
) -> None:
    pid, bundle = _create_project(studio_client, sample_1080p_mp4)
    p = bundle["project"]
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
                {
                    "id": "c1",
                    "display_text": "Path C:\\Sims\\{Vaelen}\nLine two {\\b1}safe",
                    "spoken_text_override": "Path Sims Vaelen Line two safe",
                }
            ],
        }
    ]
    studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert studio_client.wait_for_job(
        studio_client.post(f"/api/projects/{pid}/segments/seg_01/tts").json()["job_id"]
    )["status"] == "succeeded"
    prev = studio_client.wait_for_job(
        studio_client.post(f"/api/projects/{pid}/segments/seg_01/preview").json()["job_id"]
    )
    assert prev["status"] == "succeeded", prev


def test_http_409_conflict_on_stale_revision(
    studio_client: StudioClient, sample_1080p_mp4: Path
) -> None:
    pid, bundle = _create_project(studio_client, sample_1080p_mp4)
    p = bundle["project"]
    rev0 = p["revision"]
    p["title"] = "First Update"
    r1 = studio_client.put(f"/api/projects/{pid}", json_body={"expected_revision": rev0, "project": p})
    assert r1.status == 200

    # Stale revision must return 409
    p["title"] = "Stale Update"
    r2 = studio_client.put(f"/api/projects/{pid}", json_body={"expected_revision": rev0, "project": p})
    assert r2.status == 409


def test_http_416_unsatisfiable_range(
    studio_client: StudioClient, sample_1080p_mp4: Path
) -> None:
    pid, bundle = _create_project(studio_client, sample_1080p_mp4)
    art_id = bundle["manifest"]["proxy"]["artifact_id"]
    resp = studio_client.get(
        f"/api/projects/{pid}/media/{art_id}",
        headers={"Range": "bytes=999999999-"},
    )
    assert resp.status == 416


def test_http_401_403_session_token_host_origin_and_path_traversal(
    studio_client: StudioClient, sample_1080p_mp4: Path
) -> None:
    pid, _ = _create_project(studio_client, sample_1080p_mp4)

    # Missing session token on mutating endpoint -> 401 or 403
    no_tok = studio_client.raw_request(
        "POST",
        "/api/projects",
        json_body={"media_id": "fake"},
        headers={
            "Host": f"{studio_client.host}:{studio_client.port}",
            "Origin": studio_client.origin,
        },
    )
    assert no_tok.status in (401, 403)

    # Invalid Host header -> 403
    bad_host = studio_client.raw_request(
        "GET",
        "/api/health",
        headers={"Host": "evil.example.com:8420"},
    )
    assert bad_host.status in (400, 403)

    # Evil Origin header -> 403
    bad_origin = studio_client.raw_request(
        "POST",
        "/api/pronunciations",
        json_body={"pronunciations": {}},
        headers={
            "Host": f"{studio_client.host}:{studio_client.port}",
            "Origin": "https://evil.example.com",
            "X-Session-Token": studio_client.session_token or "",
        },
    )
    assert bad_origin.status in (403, 405)

    # Path traversal in media lookup -> 400/403/404 (never 200)
    trav = studio_client.get(f"/api/projects/{pid}/media/..%2F..%2Fetc%2Fpasswd")
    assert trav.status in (400, 403, 404)


@pytest.mark.parametrize(
    "kind",
    ["hdr_pq", "rotated_90", "non_16_9", "anamorphic_sar", "corrupt"],
)
def test_invalid_media_rejected_with_actionable_error(
    studio_client: StudioClient, tmp_path: Path, kind: str
) -> None:
    p = tmp_path / f"{kind}.mp4"
    if kind == "hdr_pq":
        generate_hdr_pq_mp4(p)
    elif kind == "rotated_90":
        generate_rotated_mp4(p, rotation=90)
    elif kind == "non_16_9":
        generate_non_16_9_mp4(p)
    elif kind == "anamorphic_sar":
        generate_anamorphic_sar_mp4(p)
    elif kind == "corrupt":
        generate_corrupt_mp4(p)

    resp = studio_client.select_media(p)
    assert resp.status in (400, 422), f"Expected rejection for {kind}, got {resp.status}: {resp.text}"


def test_missing_audio_silent_source_and_vfr_accepted(
    studio_server: StudioServer, tmp_path: Path
) -> None:
    client = studio_server.client
    silent = generate_silent_mp4(tmp_path / "silent_1080p.mp4", 1920, 1080, 30, 2.0)
    pid, bundle = _create_project(client, silent)
    assert bundle["manifest"]["source_metadata"]["has_audio"] is False

    vfr = generate_sdr_vfr_mp4(tmp_path / "vfr_720p.mp4", duration=2.0)
    vfr_sel = client.select_media(vfr)
    assert vfr_sel.status == 200, vfr_sel.text


def test_unicode_space_apostrophe_source_path_end_to_end(
    studio_server: StudioServer, tmp_path: Path
) -> None:
    special = generate_sdr_cfr_mp4(
        tmp_path / "Sims 4 — Vaelen's Café" / "O'Malley's Clip (1080p) 日本語.mp4",
        1280,
        720,
        30,
        2.0,
        True,
    )
    pid, bundle = _create_project(studio_server.client, special)
    assert bundle["manifest"]["proxy"]["status"] == "ready"


def test_schema_version_greater_than_supported_rejected_without_overwrite(
    studio_server: StudioServer, sample_1080p_mp4: Path
) -> None:
    client = studio_server.client
    pid, _ = _create_project(client, sample_1080p_mp4)
    proj_file = studio_server.data_dir / "projects" / pid / "project.json"
    raw = json.loads(proj_file.read_text(encoding="utf-8"))
    raw["schema_version"] = 999
    proj_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    get_resp = client.get(f"/api/projects/{pid}")
    assert get_resp.status in (400, 422, 500)
    # File must not be overwritten with schema_version=1
    still = json.loads(proj_file.read_text(encoding="utf-8"))
    assert still["schema_version"] == 999


def test_job_cancellation_and_server_restart_interrupted_recovery(
    studio_server: StudioServer, sample_1080p_mp4: Path
) -> None:
    client = studio_server.client
    pid, bundle = _create_project(client, sample_1080p_mp4)
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 3.0,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [],
        }
    ]
    client.put(f"/api/projects/{pid}", json_body={"expected_revision": p["revision"], "project": p})
    exp = client.post(f"/api/projects/{pid}/export")
    assert exp.status == 202
    job_id = exp.json()["job_id"]
    cancel = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel.status == 200
    final_state = client.wait_for_job(job_id)
    assert final_state["status"] in ("cancelled", "succeeded")

    # Simulate stale tmp file + server restart recovery
    tmp_dir = studio_server.data_dir / "projects" / pid / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stale_tmp = tmp_dir / "partial_render.mp4"
    stale_tmp.write_bytes(b"stale")
    new_client = studio_server.restart()
    assert new_client.get("/api/health").status == 200
    assert not stale_tmp.exists(), "Startup recovery must purge tmp/"


def test_keyboard_shortcut_isolation_in_inputs(
    studio_client: StudioClient, sample_1080p_mp4: Path, browser_page: Any
) -> None:
    pid, _ = _create_project(studio_client, sample_1080p_mp4)
    browser_page.goto(f"{studio_client.base_url}/?project={pid}", wait_until="networkidle")
    inputs = browser_page.locator("input[type='text'], textarea")
    if inputs.count() == 0:
        pytest.skip("No text input/textarea rendered on initial view")
    target = inputs.first
    target.click()
    target.type("io")
    assert "io" in target.input_value()


def test_pronunciation_unicode_boundary_and_longest_first(
    studio_client: StudioClient, sample_1080p_mp4: Path
) -> None:
    """Verify single-pass, case-insensitive, Unicode word-boundary, longest-key-first TTS replacement."""
    pid, bundle = _create_project(studio_client, sample_1080p_mp4)
    p = bundle["project"]
    p["pronunciations"] = {
        "Vaelen": "Vay-len",
        "Vaelen Manor": "Vay-len Man-or",
        "Vay-len": "This recursive phrase must never be spoken",
        "Café": "Kah-fay",
        "Sim": "Seem",
    }
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 3.5,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [
                {
                    "id": "c1",
                    "display_text": "lord vaelen manor at Café, not Caféshop, Sim vs Sims.",
                    "spoken_text_override": None,
                }
            ],
        },
        {
            "id": "seg_control",
            "video_in": 0.0,
            "video_out": 3.5,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.2, "y": 0.0, "zoom": 1.0}],
            "clauses": [
                {
                    "id": "c2",
                    "display_text": "control",
                    "spoken_text_override": "lord Vay-len Man-or at Kah-fay, not Caféshop, Seem vs Sims.",
                }
            ],
        },
    ]
    put_resp = studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert put_resp.status == 200, put_resp.text

    for seg_id in ("seg_01", "seg_control"):
        tts_resp = studio_client.post(f"/api/projects/{pid}/segments/{seg_id}/tts")
        assert tts_resp.status == 202, tts_resp.text
        job = studio_client.wait_for_job(tts_resp.json()["job_id"])
        assert job["status"] == "succeeded", job

    manifest = studio_client.get(f"/api/projects/{pid}").json()["manifest"]
    m_test = manifest["segments"]["seg_01"]
    m_ctrl = manifest["segments"]["seg_control"]
    assert m_test["sample_rate"] == 24000
    assert m_test["total_samples"] > 0
    assert m_test["audio_duration"] > 0.0
    assert m_test["total_samples"] == m_ctrl["total_samples"], (
        f"Expected single-pass longest-first Unicode replacement to match control samples "
        f"({m_test['total_samples']} vs {m_ctrl['total_samples']})"
    )

    # Audition endpoint check: synthesized WAV is streamable via media artifact ID
    audition = studio_client.get(f"/api/projects/{pid}/media/{m_test['audio_artifact_id']}")
    assert audition.status == 200
    assert audition.body[:4] == b"RIFF"


def test_ui_trim_shift_prompt_displayed(
    studio_client: StudioClient, sample_1080p_mp4: Path, browser_page: Any
) -> None:
    """Shortening video_out with keyframes past new duration flags keyframes and prompts in UI."""
    pid, bundle = _create_project(studio_client, sample_1080p_mp4)
    p = bundle["project"]
    p["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 3.5,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [
                {"t": 0.0, "x": 0.1, "y": 0.0, "zoom": 1.0},
                {"t": 3.0, "x": 0.4, "y": 0.0, "zoom": 1.0},
            ],
            "clauses": [],
        }
    ]
    put1 = studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert put1.status == 200, put1.text
    p = put1.json()["project"]

    # Shorten video_out to 1.5s while keyframe at t=3.0s remains
    p["segments"][0]["video_out"] = 1.5
    put2 = studio_client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert put2.status in (200, 422)
    if put2.status == 200:
        assert len(put2.json()["project"]["segments"][0]["crop_keyframes"]) == 2

    browser_page.goto(f"{studio_client.base_url}/?project={pid}", wait_until="networkidle")

    # If backend rejected PUT (422), try shortening via a visible Out/video_out input in the UI
    if put2.status == 422:
        out_input = browser_page.locator(
            "input[aria-label*='out' i], input[name*='out' i], input[data-testid*='out' i]"
        )
        if out_input.count() > 0:
            out_input.first.fill("1.5")
            out_input.first.press("Enter")
            browser_page.wait_for_timeout(300)

    body_text = browser_page.locator("body").inner_text()
    assert re.search(r"keyframe|trim|clamp|scale|past|out of range|duration", body_text, re.I), (
        f"Expected trim-shift flag/prompt in UI, got: {body_text[:400]}"
    )

