"""Native desktop launcher for Sims 4 Vertical Video Studio (Windows pywebview + FastAPI)."""

from __future__ import annotations

import multiprocessing
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from backend.bootstrap import get_data_dir


def _redirect_windowless_stdio(log_path: Path) -> None:
    """Ensure sys.stdout and sys.stderr are writable when running under console=False on Windows."""
    if sys.stdout is None or sys.stderr is None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_file  # type: ignore[assignment]
        if sys.stderr is None:
            sys.stderr = log_file  # type: ignore[assignment]


def _pick_free_port(preferred: int = 8765) -> int:
    """Return preferred localhost port if available, otherwise an OS-assigned free port."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
                return int(sock.getsockname()[1])
            except OSError:
                continue
    return preferred


def _wait_for_server(url: str, timeout_sec: float = 20.0) -> bool:
    """Poll the FastAPI health endpoint until it responds 200 OK."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def main() -> None:
    multiprocessing.freeze_support()

    data_dir = get_data_dir()
    _redirect_windowless_stdio(data_dir / "logs" / "desktop.log")

    # Import FastAPI app after stdio redirection and freeze_support
    import uvicorn
    from backend.main import app

    port = _pick_free_port(8765)
    base_url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=None,
    )
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, name="studio-api", daemon=True)
    server_thread.start()

    _wait_for_server(f"{base_url}/api/health", timeout_sec=20.0)

    try:
        import webview

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window(
            title="Sims 4 Vertical Video Studio",
            url=base_url,
            width=1440,
            height=900,
            min_size=(1024, 680),
            resizable=True,
            confirm_close=False,
        )
        gui_backend = "edgechromium" if sys.platform == "win32" else None
        webview.start(gui=gui_backend, debug=False)
    except Exception as exc:
        print(f"[launcher] Native WebView failed ({exc}); falling back to default browser.", file=sys.stderr)
        webbrowser.open(base_url)
        try:
            while server_thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    finally:
        server.should_exit = True
        server_thread.join(timeout=3.0)


if __name__ == "__main__":
    main()
