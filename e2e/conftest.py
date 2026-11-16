"""Opaque-box E2E test harness for Sims 4 Vertical Video Studio (V1).

Uses only Python 3.12 stdlib + pytest so zero external HTTP/client packages are required.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

from e2e.fixtures.media_gen import generate_sdr_cfr_mp4

REPO_ROOT = Path(__file__).resolve().parent.parent


def find_free_port() -> int:
    """Allocate an ephemeral free TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


@dataclass
class HttpResponse:
    """Simple HTTP response wrapper over http.client."""

    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class StudioClient:
    """Opaque-box HTTP client for the running Studio backend."""

    def __init__(self, host: str, port: int, session_token: str | None = None) -> None:
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.origin = self.base_url
        self.session_token = session_token

    def raw_request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        """Send an HTTP request without automatic auth/origin headers unless provided."""
        req_headers: dict[str, str] = {}
        payload = body
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        if headers:
            req_headers.update(headers)

        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request(method, path, body=payload, headers=req_headers)
            resp = conn.getresponse()
            raw_body = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return HttpResponse(status=resp.status, headers=resp_headers, body=raw_body)
        finally:
            conn.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        """Send an authenticated loopback HTTP request with X-Session-Token and Origin."""
        req_headers: dict[str, str] = {
            "Host": f"{self.host}:{self.port}",
            "Origin": self.origin,
        }
        if self.session_token:
            req_headers["X-Session-Token"] = self.session_token
        if headers:
            req_headers.update(headers)
        return self.raw_request(
            method,
            path,
            body=body,
            json_body=json_body,
            headers=req_headers,
            timeout=timeout,
        )

    def get(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("PUT", path, **kwargs)

    def bootstrap_session_token(self) -> str:
        """Extract X-Session-Token from GET / HTML <meta> tag or /api/health."""
        resp = self.raw_request("GET", "/", headers={"Host": f"{self.host}:{self.port}"})
        if resp.status == 200:
            html = resp.text
            patterns = [
                r'<meta\s+[^>]*name=["\'](?:x-session-token|session-token|sims-session-token)["\'][^>]*content=["\']([^"\']+)["\']',
                r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*name=["\'](?:x-session-token|session-token|sims-session-token)["\']',
            ]
            for pat in patterns:
                m = re.search(pat, html, re.IGNORECASE)
                if m:
                    self.session_token = m.group(1)
                    return self.session_token

        health = self.raw_request(
            "GET", "/api/health", headers={"Host": f"{self.host}:{self.port}"}
        )
        if health.status == 200:
            data = health.json()
            token = data.get("session_token") or data.get("token")
            if token:
                self.session_token = str(token)
                return self.session_token

        raise RuntimeError(
            f"Could not bootstrap X-Session-Token from / (status={resp.status}) "
            f"or /api/health (status={health.status})"
        )

    def select_media(
        self,
        media_path: Path,
        *,
        copy_to_project: bool = False,
    ) -> HttpResponse:
        """Select media via headless JSON path payload, falling back to multipart upload."""
        resp = self.post(
            "/api/media/select",
            json_body={"path": str(media_path), "copy_to_project": copy_to_project},
        )
        if resp.status in (415, 422):
            return self.upload_media_multipart("/api/media/select", media_path)
        return resp

    def upload_media_multipart(self, endpoint: str, media_path: Path) -> HttpResponse:
        """Upload a media file using stdlib multipart/form-data."""
        boundary = f"----StudioBoundary{uuid.uuid4().hex}"
        file_bytes = Path(media_path).read_bytes()
        parts = [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="file"; '
                f'filename="{media_path.name}"\r\n'
            ).encode("utf-8"),
            b"Content-Type: video/mp4\r\n\r\n",
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(parts)
        return self.post(
            endpoint,
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=60.0,
        )

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 120.0,
        poll_interval: float = 0.25,
    ) -> dict[str, Any]:
        """Poll GET /api/jobs/{job_id} until a terminal state is reached."""
        deadline = time.monotonic() + timeout
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            resp = self.get(f"/api/jobs/{job_id}")
            assert resp.status == 200, f"Unexpected job poll status {resp.status}: {resp.text}"
            last_state = resp.json()
            status = last_state.get("status")
            if status in ("succeeded", "failed", "cancelled", "interrupted"):
                return last_state
            time.sleep(poll_interval)
        raise TimeoutError(
            f"Job {job_id} did not finish within {timeout}s. Last state: {last_state}"
        )


class StudioServer:
    """Manages an isolated uvicorn backend subprocess for opaque-box E2E tests."""

    def __init__(self, data_dir: Path, port: int | None = None) -> None:
        self.host = "127.0.0.1"
        self.port = port or find_free_port()
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.process: subprocess.Popen[str] | None = None
        self.client = StudioClient(self.host, self.port)

    def start(self, timeout: float = 30.0) -> StudioClient:
        models_dir = self.data_dir / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        data_root = Path(xdg_data) if xdg_data else (Path.home() / ".local" / "share")
        cached_models_dir = data_root / "SimsVerticalStudio" / "models"
        for filename in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            src = cached_models_dir / filename
            dst = models_dir / filename
            if src.is_file() and not dst.exists():
                dst.symlink_to(src)

        env = os.environ.copy()
        env["SIMS_STUDIO_DATA_DIR"] = str(self.data_dir)
        env["PYTHONPATH"] = str(REPO_ROOT)

        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--log-level",
            "warning",
        ]
        self.process = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate(timeout=5)
                raise RuntimeError(
                    f"StudioServer exited prematurely with code {self.process.returncode}.\n"
                    f"stdout: {stdout}\nstderr: {stderr}"
                )
            try:
                resp = self.client.raw_request(
                    "GET", "/api/health", headers={"Host": f"{self.host}:{self.port}"}
                )
                if resp.status == 200:
                    self.client.bootstrap_session_token()
                    return self.client
            except OSError:
                pass
            time.sleep(0.15)

        self.stop()
        raise TimeoutError(f"StudioServer did not become healthy on port {self.port} within {timeout}s")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def restart(self, timeout: float = 30.0) -> StudioClient:
        self.stop()
        return self.start(timeout=timeout)


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def studio_server(tmp_path: Path) -> Iterator[StudioServer]:
    data_dir = tmp_path / "studio_data"
    server = StudioServer(data_dir=data_dir)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def studio_client(studio_server: StudioServer) -> StudioClient:
    return studio_server.client


@pytest.fixture
def sample_1080p_mp4(tmp_path: Path) -> Path:
    return generate_sdr_cfr_mp4(
        tmp_path / "fixtures" / "gameplay_1080p.mp4",
        width=1920,
        height=1080,
        fps=30,
        duration=4.0,
        with_audio=True,
    )


@pytest.fixture
def sample_720p_mp4(tmp_path: Path) -> Path:
    return generate_sdr_cfr_mp4(
        tmp_path / "fixtures" / "gameplay_720p.mp4",
        width=1280,
        height=720,
        fps=30,
        duration=4.0,
        with_audio=True,
    )


@pytest.fixture
def browser_page() -> Iterator[Any]:
    """Lazy Playwright browser page fixture; skips cleanly if playwright is unavailable."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
        return

    executable_path: str | None = None
    for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"):
        if Path(candidate).exists():
            executable_path = candidate
            break

    with sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {"headless": True}
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()
