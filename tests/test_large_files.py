"""Tests for large-file handling: non-blocking native picker, disk checks, async copy, and NVENC fallback."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend import main as backend_main
from backend import proxy as backend_proxy
from backend.bootstrap import verify_ffmpeg
from backend.schemas import SourceMetadata
from backend.storage import InsufficientDiskSpaceError, ensure_free_disk_space


def _make_test_video(path: Path, ffmpeg_bin: Path, duration: float = 1.0) -> None:
    res = subprocess.run(
        [
            str(ffmpeg_bin),
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=640x360:rate=30:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Failed to create test video: {res.stderr}"


def test_capabilities_exposes_picker_and_nvenc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMS_STUDIO_DATA_DIR", str(tmp_path))
    backend_main.app.state.data_dir = tmp_path
    from fastapi.testclient import TestClient

    with TestClient(backend_main.app) as client:
        r = client.get("/api/capabilities")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["native_picker_supported"], bool)
        assert isinstance(body["nvenc_ready"], bool)


@pytest.mark.anyio
async def test_native_picker_does_not_block_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMS_STUDIO_DATA_DIR", str(tmp_path))
    backend_main.app.state.data_dir = tmp_path
    ffmpeg_bin, _ = verify_ffmpeg(data_dir=tmp_path, offline=True)
    src = tmp_path / "sample.mp4"
    _make_test_video(src, ffmpeg_bin, duration=0.5)

    picker_entered = threading.Event()
    release_picker = threading.Event()

    def _blocking_picker() -> Path:
        picker_entered.set()
        release_picker.wait(timeout=5.0)
        return src

    monkeypatch.setattr(backend_main, "select_media_native", _blocking_picker)

    transport = httpx.ASGITransport(app=backend_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        token = backend_main.app.state.session_token
        headers = {"X-Session-Token": token}

        import asyncio

        select_task = asyncio.create_task(client.post("/api/media/select", headers=headers, json={}))
        for _ in range(50):
            if picker_entered.is_set():
                break
            await asyncio.sleep(0.02)
        assert picker_entered.is_set(), "Picker was never invoked"

        t0 = time.perf_counter()
        health_res = await client.get("/api/health")
        elapsed = time.perf_counter() - t0
        assert health_res.status_code == 200
        assert elapsed < 0.5, f"/api/health blocked for {elapsed:.2f}s while picker was open"

        release_picker.set()
        select_res = await select_task
        assert select_res.status_code == 200
        assert select_res.json()["path"] == str(src.resolve())


def test_ensure_free_disk_space_raises_when_low(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_usage = SimpleNamespace(total=10 * 1024**3, used=9 * 1024**3, free=100 * 1024**2)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: fake_usage)

    with pytest.raises(InsufficientDiskSpaceError):
        ensure_free_disk_space(tmp_path, 500 * 1024**2)

    ensure_free_disk_space(tmp_path, 50 * 1024**2)


def test_create_project_checks_disk_space_and_copies_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIMS_STUDIO_DATA_DIR", str(tmp_path))
    backend_main.app.state.data_dir = tmp_path
    ffmpeg_bin, _ = verify_ffmpeg(data_dir=tmp_path, offline=True)
    src = tmp_path / "source_clip.mp4"
    _make_test_video(src, ffmpeg_bin, duration=0.8)

    from fastapi.testclient import TestClient

    with TestClient(backend_main.app) as client:
        token = backend_main.app.state.session_token
        headers = {"X-Session-Token": token}

        r_sel = client.post("/api/media/select", headers=headers, json={"path": str(src)})
        assert r_sel.status_code == 200
        media_id = r_sel.json()["media_id"]

        # 1. Insufficient disk space returns HTTP 507
        fake_usage = SimpleNamespace(total=10 * 1024**3, used=10 * 1024**3, free=1024)
        monkeypatch.setattr(shutil, "disk_usage", lambda _p: fake_usage)
        r_low = client.post(
            "/api/projects",
            headers=headers,
            json={"title": "Low Disk", "media_id": media_id, "copy_to_project": True},
        )
        assert r_low.status_code == 507
        monkeypatch.undo()

        # 2. Normal create with copy_to_project=True runs copy in background job
        r_create = client.post(
            "/api/projects",
            headers=headers,
            json={
                "project_id": "proj_bg_copy",
                "title": "Background Copy",
                "media_id": media_id,
                "copy_to_project": True,
            },
        )
        assert r_create.status_code == 202
        job_id = r_create.json()["job_id"]

        deadline = time.time() + 30.0
        while time.time() < deadline:
            r_job = client.get(f"/api/jobs/{job_id}")
            assert r_job.status_code == 200
            job = r_job.json()
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.1)

        assert job["status"] == "succeeded", job
        r_proj = client.get("/api/projects/proj_bg_copy")
        assert r_proj.status_code == 200
        proj_body = r_proj.json()["project"]
        assert proj_body["source_media"]["copied_to_project"] is True
        assert "proj_bg_copy/source" in proj_body["source_media"]["path"]
        assert Path(proj_body["source_media"]["path"]).is_file()


def test_generate_proxy_nvenc_fallback_and_skip_duplicate_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffmpeg_bin, ffprobe_bin = verify_ffmpeg(data_dir=tmp_path, offline=True)
    src = tmp_path / "src.mp4"
    _make_test_video(src, ffmpeg_bin, duration=0.8)
    out = tmp_path / "proxy.mp4"

    probe_calls: list[Path] = []
    real_probe = backend_proxy.probe_and_validate_media

    def _counting_probe(path: str | Path, **kwargs: object) -> SourceMetadata:
        probe_calls.append(Path(path))
        return real_probe(path, **kwargs)

    src_meta = real_probe(src, ffprobe_path=ffprobe_bin, ffmpeg_path=ffmpeg_bin, data_dir=tmp_path)
    monkeypatch.setattr(backend_proxy, "probe_and_validate_media", _counting_probe)

    real_popen = subprocess.Popen
    seen_cmds: list[list[str]] = []

    class _FailNvencProc:
        def __init__(self) -> None:
            self.stdout = iter([])
            self.stderr = iter(["Cannot load nvcuda.dll\n"])

        def poll(self) -> int:
            return 1

        def wait(self) -> int:
            return 1

    def _fake_popen(cmd: list[str], *args: object, **kwargs: object) -> object:
        seen_cmds.append(list(cmd))
        if "h264_nvenc" in cmd:
            return _FailNvencProc()
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(backend_proxy.subprocess, "Popen", _fake_popen)

    encoders_selected: list[str] = []
    proxy_meta = backend_proxy.generate_proxy(
        src,
        out,
        source_metadata=src_meta,
        use_nvenc=True,
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
        data_dir=tmp_path,
        on_encoder_selected=encoders_selected.append,
    )

    assert out.is_file()
    assert proxy_meta.width > 0
    assert encoders_selected == ["h264_nvenc", "libx264"]
    assert any("h264_nvenc" in c for c in seen_cmds)
    assert any("libx264" in c for c in seen_cmds)
    # Source was NOT re-probed because source_metadata was supplied; only the generated proxy was probed.
    assert len(probe_calls) == 1
    assert probe_calls[0] != src
