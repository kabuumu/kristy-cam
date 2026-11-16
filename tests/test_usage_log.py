"""Tests for structured usage, interaction, action-output, and error logging."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.jobs import JobManager
from backend.main import app
from backend.usage_log import close_usage_logger_handlers, get_usage_logger


@pytest.fixture()
def isolated_log_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "studio_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SIMS_STUDIO_DATA_DIR", str(data_dir))
    app.state.data_dir = data_dir
    app.state.usage_logger = get_usage_logger(data_dir)
    app.state.job_manager = None
    yield data_dir
    close_usage_logger_handlers()


def test_usage_logger_writes_jsonl_and_debug_log(isolated_log_env: Path) -> None:
    logger = get_usage_logger(isolated_log_env)
    logger.log_event(
        source="frontend",
        category="click",
        action="ui.click",
        intention="Add segment to timeline",
        target="+ Add Segment",
        project_id="proj_1",
        seg_id="seg_01",
        input_data={"tag": "button"},
        output_data={"seg_id": "seg_01", "video_in": 1.0, "video_out": 4.0},
    )

    try:
        raise RuntimeError("Simulated ffmpeg failure")
    except RuntimeError as exc:
        logger.log_error(
            source="job",
            action="job.failed",
            intention="Export final MP4",
            project_id="proj_1",
            job_id="job_abc",
            context={"stage": "Muxing", "returncode": 1},
            exc=exc,
        )

    assert logger.usage_log_path.is_file()
    assert logger.debug_log_path.is_file()

    lines = [
        json.loads(line)
        for line in logger.usage_log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) >= 2

    recent_errors = logger.read_recent(limit=10, category="error", project_id="proj_1")
    assert len(recent_errors) == 1
    err_entry = recent_errors[0]
    assert err_entry["error"]["type"] == "RuntimeError"
    assert "Simulated ffmpeg failure" in err_entry["error"]["message"]
    assert "Traceback" in err_entry["error"]["stack"]
    assert err_entry["error"]["context"]["returncode"] == 1

    debug_text = logger.debug_log_path.read_text(encoding="utf-8")
    assert "[ERROR]" in debug_text
    assert "Simulated ffmpeg failure" in debug_text


def test_api_log_ingestion_query_and_http_error_logging(isolated_log_env: Path) -> None:
    client = TestClient(app)
    token = app.state.session_token

    # Mutating log endpoint requires X-Session-Token
    r_unauth = client.post("/api/logs/events", json={"action": "ui.click"})
    assert r_unauth.status_code == 401

    batch = {
        "events": [
            {
                "category": "keypress",
                "action": "ui.keypress",
                "intention": "Shortcut: Set Mark In",
                "target": "i",
                "project_id": "proj_test",
                "input": {"key": "i"},
            },
            {
                "category": "action_output",
                "action": "timeline.mark_in",
                "intention": "Mark segment In-point via keyboard",
                "project_id": "proj_test",
                "output": {"mark_in": 2.5, "mark_out": 5.5},
            },
            {
                "category": "error",
                "action": "api.error",
                "intention": "Save project",
                "project_id": "proj_test",
                "error": {
                    "type": "ApiError",
                    "message": "Revision conflict",
                    "status_code": 409,
                    "context": {"expected_revision": 1},
                },
            },
        ]
    }
    r_post = client.post(
        "/api/logs/events",
        headers={"X-Session-Token": token},
        json=batch,
    )
    assert r_post.status_code == 200
    assert r_post.json()["accepted"] == 3

    # Trigger a backend 404 HTTPException and verify it is also logged
    r_404 = client.get("/api/projects/does_not_exist_123")
    assert r_404.status_code == 404

    r_logs = client.get("/api/logs?limit=50")
    assert r_logs.status_code == 200
    body = r_logs.json()
    assert body["count"] >= 4
    actions = {ev["action"] for ev in body["events"]}
    assert {"ui.keypress", "timeline.mark_in", "api.error", "api.http_error"}.issubset(actions)

    r_err_only = client.get("/api/logs?category=error")
    assert r_err_only.status_code == 200
    assert all(ev["category"] == "error" for ev in r_err_only.json()["events"])


def test_job_manager_logs_success_and_failure(isolated_log_env: Path) -> None:
    jm = JobManager(data_dir=isolated_log_env, max_workers=1)
    try:
        def ok_worker(ctx) -> dict[str, str]:
            ctx.update_progress(50, "Working")
            return {"artifact_id": "art_ok"}

        def fail_worker(ctx) -> dict[str, str]:
            ctx.update_progress(35, "Encoding")
            raise ValueError("Bad crop width")

        j_ok = jm.submit_job("proj_j", "preview", ok_worker, seg_id="seg_01")
        j_fail = jm.submit_job("proj_j", "export", fail_worker)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            s1 = jm.get_job(j_ok.job_id)
            s2 = jm.get_job(j_fail.job_id)
            if s1 and s2 and s1.status == "succeeded" and s2.status == "failed":
                break
            time.sleep(0.05)

        logger = get_usage_logger(isolated_log_env)
        events = logger.read_recent(limit=50, project_id="proj_j")
        actions = [e["action"] for e in events]
        assert "job.queued" in actions
        assert "job.started" in actions
        assert "job.succeeded" in actions
        assert "job.failed" in actions

        failed_events = [e for e in events if e["action"] == "job.failed"]
        assert len(failed_events) == 1
        err = failed_events[0]["error"]
        assert err["type"] == "ValueError"
        assert "Bad crop width" in err["message"]
        assert err["context"]["last_stage"] == "Encoding"
        assert err["context"]["last_progress"] == 35
        assert "Traceback" in err["stack"]
    finally:
        jm.shutdown(wait=True)
