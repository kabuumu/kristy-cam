"""Tests for GitHub Releases auto-updater logic and API endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import main as main_module
from backend.updater import (
    UpdaterError,
    _validate_download_url,
    is_newer_release,
    parse_build_number,
)


def test_parse_build_number_and_is_newer_release():
    assert parse_build_number("build-42-a1b2c3d") == 42
    assert parse_build_number("v0.1.5") is None

    cur = {"version": "0.1.10", "tag": "build-10-aaaaaaa", "build_number": 10}
    assert is_newer_release(cur, "build-11-bbbbbbb", 11) is True
    assert is_newer_release(cur, "build-10-aaaaaaa", 10) is False
    assert is_newer_release(cur, "build-9-ccccccc", 9) is False

    dev = {"version": "0.1.0-dev", "tag": "", "build_number": 0}
    assert is_newer_release(dev, "build-1-a1b2c3d", 1) is True


def test_validate_download_url_security():
    _validate_download_url("https://github.com/kabuumu/kristy-cam/releases/download/build-1-abc/SimsVerticalStudio-Setup.exe")
    _validate_download_url("https://objects.githubusercontent.com/github-production-release-asset/12345")

    with pytest.raises(UpdaterError):
        _validate_download_url("http://github.com/kabuumu/kristy-cam/releases/download/build-1/app.exe")

    with pytest.raises(UpdaterError):
        _validate_download_url("https://evil.example.com/malware.exe")


def test_update_check_and_install_guard_api(tmp_path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SIMS_STUDIO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIMS_STUDIO_OFFLINE", "1")
    main_module._UPDATE_CHECK_CACHE["data"] = None
    main_module._UPDATE_CHECK_CACHE["ts"] = 0.0

    with TestClient(main_module.app) as client:
        token = main_module.app.state.session_token
        headers = {"X-Session-Token": token}

        # Offline mode returns cleanly without error
        r_check = client.get("/api/app/update-check?force=true")
        assert r_check.status_code == 200
        body = r_check.json()
        assert body["offline"] is True
        assert body["update_available"] is False
        assert body["can_self_update"] is False

        # Status endpoint returns idle
        r_status = client.get("/api/app/update-status")
        assert r_status.status_code == 200
        assert r_status.json()["status"] == "idle"

        # Install endpoint rejects non-frozen / non-Windows environment
        r_inst = client.post("/api/app/update-install", headers=headers)
        assert r_inst.status_code == 400
