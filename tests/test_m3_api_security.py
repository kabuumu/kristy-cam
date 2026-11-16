"""Milestone 3 tests for FastAPI security middleware, all 14 REST endpoints, Range streaming, and lifespan recovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.main as main_module
from backend.bootstrap import verify_ffmpeg
from backend.schemas import JobState, utc_now_iso
from backend.storage import atomic_write_json, create_project, get_project_dir, load_manifest


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------


def _default_models_dir() -> Path:
    """Locate pre-downloaded Kokoro models in the host's default data dir without mutating it."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local_app_data) if local_app_data else (Path.home() / "AppData" / "Local")
        return root / "SimsVerticalStudio" / "models"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SimsVerticalStudio" / "models"
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(xdg_data) if xdg_data else (Path.home() / ".local" / "share")
    return root / "SimsVerticalStudio" / "models"


def _seed_models_into_data_dir(data_dir: Path) -> None:
    """Symlink cached Kokoro model files into isolated data_dir/models if available."""
    models_dir = data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    src_models = _default_models_dir()
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        src = src_models / name
        dst = models_dir / name
        if src.is_file() and not dst.exists():
            dst.symlink_to(src)


@pytest.fixture(scope="session")
def ffmpeg_tools() -> tuple[Path, Path]:
    """Return verified (ffmpeg_path, ffprobe_path)."""
    return verify_ffmpeg(offline=True)


def _make_test_mp4(
    ffmpeg_path: Path,
    out_path: Path,
    *,
    width: int = 1280,
    height: int = 720,
    duration: float = 2.0,
    fps: int = 30,
    with_audio: bool = True,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg_path),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
    ]
    if with_audio:
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=48000:duration={duration}",
            ]
        )
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if with_audio:
        cmd.extend(["-c:a", "aac", "-ar", "48000", "-ac", "2"])
    cmd.extend(["-movflags", "+faststart", str(out_path)])
    res = subprocess.run(cmd, shell=False, capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"FFmpeg failed: {res.stderr}"
    return out_path


def _ffprobe_json(ffprobe_path: Path, media_path: Path) -> dict[str, Any]:
    res = subprocess.run(
        [
            str(ffprobe_path),
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


@pytest.fixture()
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an isolated SIMS_STUDIO_DATA_DIR with symlinked Kokoro models."""
    data_dir = tmp_path / "studio_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _seed_models_into_data_dir(data_dir)
    monkeypatch.setenv("SIMS_STUDIO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIMS_STUDIO_OFFLINE", "1")
    return data_dir


@pytest.fixture()
def client(isolated_data_dir: Path) -> TestClient:
    """FastAPI TestClient with lifespan initialized on an isolated data dir."""
    with TestClient(main_module.app) as tc:
        yield tc


def _auth_headers(tc: TestClient) -> dict[str, str]:
    return {"X-Session-Token": tc.app.state.session_token}


def _poll_job(tc: TestClient, job_id: str, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = tc.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        last_data = resp.json()
        if last_data["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return last_data
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for job {job_id}: last state={last_data}")


# ---------------------------------------------------------------------------
# 1. Loopback Security Middleware (F36)
# ---------------------------------------------------------------------------


def test_host_header_validation(client: TestClient) -> None:
    """Reject non-loopback Host headers with 403; allow 127.0.0.1 and localhost."""
    r_bad = client.get("/api/health", headers={"Host": "evil.com"})
    assert r_bad.status_code == 403

    r_bad_port = client.get("/api/health", headers={"Host": "evil.com:8420"})
    assert r_bad_port.status_code == 403

    r_ip = client.get("/api/health", headers={"Host": "127.0.0.1:8420"})
    assert r_ip.status_code == 200

    r_lh = client.get("/api/health", headers={"Host": "localhost:8420"})
    assert r_lh.status_code == 200


def test_origin_header_validation_and_no_wildcard_cors(client: TestClient) -> None:
    """Reject untrusted Origin headers with 403, allow loopback Origins, and never emit wildcard CORS."""
    r_evil = client.get("/api/health", headers={"Origin": "https://evil.com"})
    assert r_evil.status_code == 403
    assert r_evil.headers.get("access-control-allow-origin") != "*"

    r_bad_scheme = client.get("/api/health", headers={"Origin": "ftp://localhost:8420"})
    assert r_bad_scheme.status_code == 403

    for valid_origin in ("http://127.0.0.1:8420", "http://localhost:5173"):
        resp = client.get("/api/health", headers={"Origin": valid_origin})
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_session_token_enforcement_on_mutating_endpoints(client: TestClient) -> None:
    """Missing X-Session-Token -> 401; invalid token -> 403; valid token passes middleware."""
    mutating_calls = [
        ("POST", "/api/media/select", {"json": {"path": "/nonexistent.mp4"}}),
        ("POST", "/api/projects", {"json": {"title": "T", "media_id": "missing"}}),
        ("PUT", "/api/projects/missing", {"json": {"expected_revision": 1}}),
        ("POST", "/api/projects/missing/source/relink", {"json": {"path": "/nonexistent.mp4"}}),
        ("POST", "/api/projects/missing/segments/seg_1/tts", {}),
        ("POST", "/api/projects/missing/segments/seg_1/preview", {}),
        ("POST", "/api/projects/missing/export", {}),
        ("POST", "/api/jobs/missing/cancel", {}),
        ("PUT", "/api/pronunciations", {"json": {"pronunciations": {"Sim": "Seem"}}}),
    ]

    for method, url, kwargs in mutating_calls:
        r_missing = client.request(method, url, **kwargs)
        assert r_missing.status_code == 401, f"{method} {url} without token returned {r_missing.status_code}"

        r_invalid = client.request(
            method, url, headers={"X-Session-Token": "wrong-token"}, **kwargs
        )
        assert r_invalid.status_code == 403, f"{method} {url} with bad token returned {r_invalid.status_code}"

    # Valid token reaches endpoint validation/business logic (not 401/403)
    r_valid = client.put(
        "/api/pronunciations",
        headers=_auth_headers(client),
        json={"pronunciations": {"Vaelen": "Vay-len"}},
    )
    assert r_valid.status_code == 200


# ---------------------------------------------------------------------------
# 2. Static index.html Token Injection & SPA Fallback (F36, F38)
# ---------------------------------------------------------------------------


def test_index_html_token_injection_with_temp_dist(
    isolated_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serve index.html from FRONTEND_DIST_DIR with injected session-token meta tag and SPA fallback."""
    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text(
        "<!doctype html><html><head><title>Studio</title></head><body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    (assets_dir / "app.js").write_text("console.log('ok');", encoding="utf-8")

    monkeypatch.setattr(main_module, "FRONTEND_DIST_DIR", dist_dir)

    with TestClient(main_module.app) as tc:
        token = tc.app.state.session_token
        r_root = tc.get("/")
        assert r_root.status_code == 200
        assert f'<meta name="session-token" content="{token}">' in r_root.text

        # Existing placeholder meta tag replacement
        (dist_dir / "index.html").write_text(
            '<!doctype html><html><head><meta name="session-token" content="__PLACEHOLDER__"></head><body></body></html>',
            encoding="utf-8",
        )
        r_replaced = tc.get("/")
        assert r_replaced.status_code == 200
        assert f'<meta name="session-token" content="{token}">' in r_replaced.text
        assert "__PLACEHOLDER__" not in r_replaced.text

        # SPA fallback returns index.html with token; unknown /api/* returns 404
        r_spa = tc.get("/projects/some-id")
        assert r_spa.status_code == 200
        assert f'<meta name="session-token" content="{token}">' in r_spa.text

        r_api_404 = tc.get("/api/does-not-exist")
        assert r_api_404.status_code == 404


def test_index_html_token_injection_when_dist_missing(
    isolated_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback HTML still injects the per-launch session token if frontend/dist is absent."""
    missing_dist = tmp_path / "no_such_dist"
    monkeypatch.setattr(main_module, "FRONTEND_DIST_DIR", missing_dist)

    with TestClient(main_module.app) as tc:
        token = tc.app.state.session_token
        resp = tc.get("/")
        assert resp.status_code == 200
        assert f'<meta name="session-token" content="{token}">' in resp.text


# ---------------------------------------------------------------------------
# 3. All 14 REST Endpoints + Range Streaming + Security Guards (F37, F38)
# ---------------------------------------------------------------------------


def test_health_and_capabilities(client: TestClient) -> None:
    """GET /api/health and GET /api/capabilities return verified runtime status."""
    r_health = client.get("/api/health")
    assert r_health.status_code == 200
    health = r_health.json()
    assert health["status"] == "ok"
    assert health["app"] == "sims-vertical-studio"

    r_caps = client.get("/api/capabilities")
    assert r_caps.status_code == 200
    caps = r_caps.json()
    assert caps["ffmpeg_ready"] is True
    assert caps["libass_ready"] is True
    assert caps["font_ready"] is True
    assert caps["kokoro_ready"] is True
    assert isinstance(caps["voices"], list) and len(caps["voices"]) > 0
    assert "bf_emma" in caps["voices"]
    assert caps["errors"] == []


def test_pronunciations_get_and_put(client: TestClient) -> None:
    """GET/PUT /api/pronunciations persists and retrieves global defaults."""
    r_init = client.get("/api/pronunciations")
    assert r_init.status_code == 200
    assert "pronunciations" in r_init.json()

    r_put = client.put(
        "/api/pronunciations",
        headers=_auth_headers(client),
        json={"pronunciations": {"Vaelen": "Vay-len", "Windenburg": "Vin-den-burg"}},
    )
    assert r_put.status_code == 200
    assert r_put.json()["pronunciations"] == {"Vaelen": "Vay-len", "Windenburg": "Vin-den-burg"}

    r_get = client.get("/api/pronunciations")
    assert r_get.status_code == 200
    assert r_get.json()["pronunciations"] == {"Vaelen": "Vay-len", "Windenburg": "Vin-den-burg"}


def test_media_select_json_multipart_and_invalid_rejected(
    client: TestClient, ffmpeg_tools: tuple[Path, Path], tmp_path: Path
) -> None:
    """POST /api/media/select supports JSON path, multipart upload, and rejects invalid media with 422."""
    ffmpeg_path, _ = ffmpeg_tools
    valid_mp4 = _make_test_mp4(ffmpeg_path, tmp_path / "valid_16x9.mp4", width=1280, height=720, duration=1.5)
    invalid_4x3 = _make_test_mp4(ffmpeg_path, tmp_path / "invalid_4x3.mp4", width=640, height=480, duration=1.0)

    # 1. JSON path
    r_json = client.post(
        "/api/media/select",
        headers=_auth_headers(client),
        json={"path": str(valid_mp4)},
    )
    assert r_json.status_code == 200, r_json.text
    data_json = r_json.json()
    assert data_json["media_id"].startswith("src_")
    assert data_json["metadata"]["width"] == 1280
    assert data_json["metadata"]["height"] == 720
    assert data_json["metadata"]["has_audio"] is True

    # 2. Multipart upload fallback
    r_multi = client.post(
        "/api/media/select",
        headers=_auth_headers(client),
        files={"file": ("clip_upload.mp4", valid_mp4.read_bytes(), "video/mp4")},
    )
    assert r_multi.status_code == 200, r_multi.text
    data_multi = r_multi.json()
    assert data_multi["media_id"].startswith("src_")
    assert Path(data_multi["path"]).is_file()

    # 3. Non-16:9 media (4:3) is accepted -> 200
    r_4x3 = client.post(
        "/api/media/select",
        headers=_auth_headers(client),
        json={"path": str(invalid_4x3)},
    )
    assert r_4x3.status_code == 200, r_4x3.text
    assert r_4x3.json()["metadata"]["width"] == 640
    assert r_4x3.json()["metadata"]["height"] == 480

    # 4. Relative / traversal / missing paths -> 422
    for bad_path in ("relative/clip.mp4", "/tmp/../etc/passwd", str(tmp_path / "missing.mp4")):
        r_bad = client.post(
            "/api/media/select",
            headers=_auth_headers(client),
            json={"path": bad_path},
        )
        assert r_bad.status_code == 422


def test_end_to_end_project_lifecycle_jobs_range_and_security(
    client: TestClient,
    isolated_data_dir: Path,
    ffmpeg_tools: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """Exercise projects CRUD, 409 revision conflict, relink, TTS, preview, export, and 206/416/403 media streaming."""
    ffmpeg_path, ffprobe_path = ffmpeg_tools
    headers = _auth_headers(client)

    src1 = _make_test_mp4(ffmpeg_path, tmp_path / "gameplay_1.mp4", width=1280, height=720, duration=2.0)
    src2 = _make_test_mp4(ffmpeg_path, tmp_path / "gameplay_2.mp4", width=1280, height=720, duration=2.5)

    # 1. Register media & create project (202 + proxy job)
    r_sel = client.post("/api/media/select", headers=headers, json={"path": str(src1)})
    assert r_sel.status_code == 200
    media_id = r_sel.json()["media_id"]

    r_create = client.post(
        "/api/projects",
        headers=headers,
        json={"title": "Ravenswood Manor", "media_id": media_id, "project_id": "proj_m3_e2e"},
    )
    assert r_create.status_code == 202, r_create.text
    create_body = r_create.json()
    proxy_job_id = create_body["job_id"]
    project_id = create_body["project"]["project_id"]
    assert project_id == "proj_m3_e2e"

    proxy_job = _poll_job(client, proxy_job_id)
    assert proxy_job["status"] == "succeeded", proxy_job

    # 2. GET /api/projects & GET /api/projects/{id}
    r_list = client.get("/api/projects")
    assert r_list.status_code == 200
    assert any(p["project_id"] == project_id for p in r_list.json()["projects"])

    r_get = client.get(f"/api/projects/{project_id}")
    assert r_get.status_code == 200
    proj_payload = r_get.json()
    assert proj_payload["manifest"]["proxy"]["status"] == "ready"
    assert proj_payload["source_status"]["status"] == "ok"
    assert proj_payload["source_status"]["fingerprint_match"] is True

    # 3. PUT /api/projects/{id} + HTTP 409 on stale expected_revision
    project_data = proj_payload["project"]
    project_data["segments"] = [
        {
            "id": "seg_01",
            "video_in": 0.0,
            "video_out": 1.2,
            "duration_mode": "preserve_video",
            "source_audio_volume": 0.15,
            "speed": 1.0,
            "crop_keyframes": [{"t": 0.0, "x": 0.25, "y": 0.0, "zoom": 1.0}],
            "clauses": [
                {"id": "c1", "display_text": "Hello Sims!", "spoken_text_override": None},
            ],
        }
    ]
    r_put = client.put(
        f"/api/projects/{project_id}",
        headers=headers,
        json={"project": project_data, "expected_revision": project_data["revision"]},
    )
    assert r_put.status_code == 200, r_put.text
    updated_proj = r_put.json()["project"]
    assert updated_proj["revision"] == project_data["revision"] + 1

    # Stale expected_revision -> 409 Conflict
    r_conflict = client.put(
        f"/api/projects/{project_id}",
        headers=headers,
        json={"project": updated_proj, "expected_revision": 1},
    )
    assert r_conflict.status_code == 409

    # 4. POST /api/projects/{id}/source/relink
    r_relink = client.post(
        f"/api/projects/{project_id}/source/relink",
        headers=headers,
        json={"path": str(src2), "copy_to_project": True},
    )
    assert r_relink.status_code == 200, r_relink.text
    relink_body = r_relink.json()
    assert relink_body["project"]["source_media"]["copied_to_project"] is True
    assert relink_body["source_status"]["status"] == "ok"
    assert relink_body["source_status"]["fingerprint_match"] is True
    assert relink_body["manifest"]["source_metadata"]["duration"] == pytest.approx(2.5, abs=0.2)

    # 5. POST /api/projects/{id}/segments/{seg_id}/tts (202 + job_id)
    r_tts = client.post(f"/api/projects/{project_id}/segments/seg_01/tts", headers=headers)
    assert r_tts.status_code == 202, r_tts.text
    tts_job = _poll_job(client, r_tts.json()["job_id"])
    assert tts_job["status"] == "succeeded", tts_job

    manifest_after_tts = client.get(f"/api/projects/{project_id}").json()["manifest"]
    seg_art = manifest_after_tts["segments"]["seg_01"]
    assert seg_art["audio_artifact_id"] == "art_tts_seg_01"
    assert seg_art["audio_duration"] > 0.0
    assert len(seg_art["clause_timings"]) == 1

    # 6. POST /api/projects/{id}/segments/{seg_id}/preview (202 + job_id)
    r_prev = client.post(f"/api/projects/{project_id}/segments/seg_01/preview", headers=headers)
    assert r_prev.status_code == 202, r_prev.text
    prev_job = _poll_job(client, r_prev.json()["job_id"])
    assert prev_job["status"] == "succeeded", prev_job
    assert prev_job["result"]["preview_artifact_id"] == "art_preview_seg_01"

    # 7. POST /api/projects/{id}/export (202 + job_id)
    r_exp = client.post(f"/api/projects/{project_id}/export", headers=headers)
    assert r_exp.status_code == 202, r_exp.text
    exp_job = _poll_job(client, r_exp.json()["job_id"], timeout=90.0)
    assert exp_job["status"] == "succeeded", exp_job

    manifest_final = client.get(f"/api/projects/{project_id}").json()["manifest"]
    export_rel = manifest_final["latest_export_path"]
    assert export_rel is not None
    export_abs = get_project_dir(project_id, data_dir=isolated_data_dir) / export_rel
    assert export_abs.is_file()

    probe = _ffprobe_json(ffprobe_path, export_abs)
    v_stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    a_stream = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    assert v_stream["width"] == 1080
    assert v_stream["height"] == 1920
    assert v_stream["pix_fmt"] == "yuv420p"
    assert a_stream["sample_rate"] == "48000"

    # 8. GET /api/projects/{id}/media/{artifact_id} — 200, 206 Range, 416, MIME types
    r_full = client.get(f"/api/projects/{project_id}/media/art_proxy")
    assert r_full.status_code == 200
    assert r_full.headers["content-type"].startswith("video/mp4")
    assert r_full.headers["accept-ranges"] == "bytes"
    proxy_bytes = r_full.content
    assert len(proxy_bytes) > 2048

    r_wav = client.get(f"/api/projects/{project_id}/media/art_tts_seg_01")
    assert r_wav.status_code == 200
    assert r_wav.headers["content-type"].startswith("audio/wav")

    r_206 = client.get(
        f"/api/projects/{project_id}/media/art_proxy",
        headers={"Range": "bytes=0-1023"},
    )
    assert r_206.status_code == 206
    assert r_206.headers["content-range"] == f"bytes 0-1023/{len(proxy_bytes)}"
    assert r_206.headers["content-length"] == "1024"
    assert r_206.content == proxy_bytes[:1024]

    r_suffix = client.get(
        f"/api/projects/{project_id}/media/art_proxy",
        headers={"Range": "bytes=-512"},
    )
    assert r_suffix.status_code == 206
    assert r_suffix.content == proxy_bytes[-512:]

    r_416 = client.get(
        f"/api/projects/{project_id}/media/art_proxy",
        headers={"Range": f"bytes={len(proxy_bytes) + 100}-"},
    )
    assert r_416.status_code == 416
    assert r_416.headers["content-range"] == f"bytes */{len(proxy_bytes)}"

    # 9. Path traversal & symlink escape rejected with 403
    proj_dir = get_project_dir(project_id, data_dir=isolated_data_dir)
    outside_file = tmp_path / "secret.mp4"
    outside_file.write_bytes(b"secret")
    symlink_artifact = proj_dir / "artifacts" / "escape.mp4"
    symlink_artifact.symlink_to(outside_file)

    manifest_obj = load_manifest(project_id, data_dir=isolated_data_dir)
    manifest_obj.segments["seg_01"].preview_path = "../secret.mp4"
    atomic_write_json(proj_dir / "manifest.json", manifest_obj.model_dump(mode="json"), create_backup=False)
    r_trav = client.get(f"/api/projects/{project_id}/media/art_preview_seg_01")
    assert r_trav.status_code == 403

    manifest_obj.segments["seg_01"].preview_path = "artifacts/escape.mp4"
    atomic_write_json(proj_dir / "manifest.json", manifest_obj.model_dump(mode="json"), create_backup=False)
    r_sym = client.get(f"/api/projects/{project_id}/media/art_preview_seg_01")
    assert r_sym.status_code == 403


def test_job_cancel_endpoint(client: TestClient) -> None:
    """POST /api/jobs/{job_id}/cancel terminates an active job and marks it cancelled."""
    started = threading.Event()
    release = threading.Event()

    def _slow_worker(ctx):
        started.set()
        while not release.is_set():
            ctx.check_cancelled()
            time.sleep(0.02)
        return {}

    job = client.app.state.job_manager.submit_job("proj_cancel", "proxy", _slow_worker)
    assert started.wait(timeout=5.0)

    r_cancel = client.post(f"/api/jobs/{job.job_id}/cancel", headers=_auth_headers(client))
    assert r_cancel.status_code == 200
    assert r_cancel.json()["status"] == "cancelled"

    r_poll = client.get(f"/api/jobs/{job.job_id}")
    assert r_poll.status_code == 200
    assert r_poll.json()["status"] == "cancelled"

    release.set()

    r_missing = client.post("/api/jobs/job_nonexistent/cancel", headers=_auth_headers(client))
    assert r_missing.status_code == 404


# ---------------------------------------------------------------------------
# 4. Startup Recovery via FastAPI Lifespan
# ---------------------------------------------------------------------------


def test_lifespan_startup_recovery_marks_interrupted_and_purges_tmp(
    isolated_data_dir: Path, tmp_path: Path
) -> None:
    """FastAPI lifespan marks leftover queued/running jobs as interrupted and purges projects/*/tmp/."""
    jobs_dir = isolated_data_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now_iso()

    for jid, status in (
        ("job_left_queued", "queued"),
        ("job_left_running", "running"),
        ("job_already_done", "succeeded"),
    ):
        state = JobState(
            job_id=jid,
            project_id="proj_recover",
            job_type="export",
            status=status,
            progress=50 if status == "running" else 0,
            stage="Working",
            created_at=now,
            updated_at=now,
        )
        atomic_write_json(jobs_dir / f"{jid}.json", state.model_dump(mode="json"), create_backup=False)

    # Create a project with transient files in tmp/ and a valid file in artifacts/
    from backend.schemas import SourceMediaRef

    create_project(
        project_id="proj_recover",
        title="Recovery Test",
        source_media=SourceMediaRef(media_id="src_1", path="/tmp/fake.mp4", fingerprint="sha256:abc"),
        data_dir=isolated_data_dir,
    )
    proj_dir = get_project_dir("proj_recover", data_dir=isolated_data_dir)
    (proj_dir / "tmp" / "partial.mp4").write_bytes(b"partial")
    (proj_dir / "tmp" / "nested").mkdir(parents=True, exist_ok=True)
    (proj_dir / "tmp" / "nested" / "script.txt").write_text("filter", encoding="utf-8")
    keep_artifact = proj_dir / "artifacts" / "keep.wav"
    keep_artifact.write_bytes(b"RIFF")

    with TestClient(main_module.app) as tc:
        r_q = tc.get("/api/jobs/job_left_queued")
        assert r_q.status_code == 200
        assert r_q.json()["status"] == "interrupted"

        r_r = tc.get("/api/jobs/job_left_running")
        assert r_r.status_code == 200
        assert r_r.json()["status"] == "interrupted"

        r_s = tc.get("/api/jobs/job_already_done")
        assert r_s.status_code == 200
        assert r_s.json()["status"] == "succeeded"

    assert list((proj_dir / "tmp").iterdir()) == []
    assert keep_artifact.is_file()
