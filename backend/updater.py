"""GitHub Releases auto-updater for Sims 4 Vertical Video Studio."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from backend.bootstrap import REPO_ROOT, get_data_dir, is_offline_env

DEFAULT_GITHUB_REPO = "kabuumu/kristy-cam"
BUILD_TAG_RE = re.compile(r"^build-(\d+)-[0-9a-fA-F]+$")
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
}


class UpdaterError(RuntimeError):
    """Raised when checking for or installing an update fails."""


def get_build_info() -> dict[str, Any]:
    """Read embedded build metadata from build_info.json (or dev defaults)."""
    is_frozen = bool(getattr(sys, "frozen", False))
    info: dict[str, Any] = {
        "version": "0.1.0-dev",
        "tag": "",
        "commit": "",
        "build_number": 0,
        "repo": os.environ.get("SIMS_STUDIO_GITHUB_REPO", DEFAULT_GITHUB_REPO).strip() or DEFAULT_GITHUB_REPO,
        "is_frozen": is_frozen,
        "can_self_update": is_frozen and sys.platform == "win32",
    }

    candidates = [
        REPO_ROOT / "build_info.json",
        REPO_ROOT / "packaging" / "build_info.json",
    ]
    for path in candidates:
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    if raw.get("version"):
                        info["version"] = str(raw["version"])
                    if raw.get("tag"):
                        info["tag"] = str(raw["tag"])
                    if raw.get("commit"):
                        info["commit"] = str(raw["commit"])
                    if raw.get("build_number") is not None:
                        info["build_number"] = int(raw["build_number"])
                    if raw.get("repo") and not os.environ.get("SIMS_STUDIO_GITHUB_REPO"):
                        info["repo"] = str(raw["repo"])
                break
            except Exception:
                pass
    return info


def parse_build_number(tag: str) -> int | None:
    """Extract numeric build number from tags like 'build-42-a1b2c3d'."""
    m = BUILD_TAG_RE.match(tag.strip())
    if m:
        return int(m.group(1))
    return None


def parse_semver(version_str: str) -> tuple[int, int, int] | None:
    """Parse 'v0.1.42' or '0.1.42' into a 3-tuple for comparison."""
    m = SEMVER_RE.match(version_str.strip())
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def is_newer_release(
    current_info: dict[str, Any],
    latest_tag: str,
    latest_build_number: int | None,
) -> bool:
    """Determine whether the remote GitHub release is newer than the running build."""
    cur_tag = str(current_info.get("tag") or "").strip()
    cur_build = int(current_info.get("build_number") or 0)
    cur_ver = str(current_info.get("version") or "").strip()

    if latest_tag and cur_tag and latest_tag == cur_tag:
        return False

    if latest_build_number is not None and cur_build > 0:
        return latest_build_number > cur_build

    cur_sem = parse_semver(cur_ver)
    lat_sem = parse_semver(latest_tag)
    if cur_sem is not None and lat_sem is not None:
        return lat_sem > cur_sem

    # Running a local dev build (build_number == 0) against a real published release
    if cur_build == 0 and (latest_build_number is not None or bool(latest_tag)):
        return True

    return False


def _select_windows_installer_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the preferred Windows Setup .exe asset from a GitHub release's assets list."""
    exe_assets = [
        a for a in assets
        if isinstance(a, dict) and str(a.get("name", "")).lower().endswith(".exe")
    ]
    if not exe_assets:
        return None
    for a in exe_assets:
        if "setup" in str(a.get("name", "")).lower():
            return a
    return exe_assets[0]


def check_for_update(
    repo: str | None = None,
    timeout_sec: float = 8.0,
) -> dict[str, Any]:
    """Query GitHub Releases API for the latest release and compare with current build."""
    info = get_build_info()
    target_repo = (repo or info["repo"]).strip()
    base_result: dict[str, Any] = {
        "current_version": info["version"],
        "current_tag": info["tag"],
        "current_build_number": info["build_number"],
        "repo": target_repo,
        "is_frozen": info["is_frozen"],
        "can_self_update": info["can_self_update"],
        "update_available": False,
        "offline": False,
        "error": None,
        "latest_version": None,
        "latest_tag": None,
        "latest_build_number": None,
        "release_name": None,
        "release_notes": None,
        "release_url": f"https://github.com/{target_repo}/releases",
        "published_at": None,
        "asset_name": None,
        "asset_size": None,
        "download_url": None,
    }

    if is_offline_env():
        base_result["offline"] = True
        return base_result

    api_url = f"https://api.github.com/repos/{target_repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"SimsVerticalStudio/{info['version']}",
    }
    try:
        with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
            resp = client.get(api_url, headers=headers)
            if resp.status_code == 404:
                return base_result
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        base_result["error"] = f"Could not check GitHub releases: {exc}"
        return base_result

    latest_tag = str(data.get("tag_name") or "").strip()
    latest_build = parse_build_number(latest_tag)
    latest_version = f"0.1.{latest_build}" if latest_build is not None else latest_tag
    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    installer = _select_windows_installer_asset(assets)

    update_available = is_newer_release(info, latest_tag, latest_build)

    base_result.update(
        {
            "update_available": update_available,
            "latest_version": latest_version,
            "latest_tag": latest_tag,
            "latest_build_number": latest_build,
            "release_name": str(data.get("name") or latest_tag),
            "release_notes": str(data.get("body") or ""),
            "release_url": str(data.get("html_url") or base_result["release_url"]),
            "published_at": data.get("published_at"),
            "asset_name": str(installer.get("name")) if installer else None,
            "asset_size": int(installer.get("size") or 0) if installer else None,
            "download_url": str(installer.get("browser_download_url")) if installer else None,
        }
    )
    return base_result


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise UpdaterError("Update download URL must use HTTPS.")
    if host not in ALLOWED_DOWNLOAD_HOSTS and not host.endswith(".githubusercontent.com"):
        raise UpdaterError(f"Refusing to download update from untrusted host: {host!r}")


def download_installer(
    download_url: str,
    asset_name: str,
    data_dir: Path | None = None,
    on_progress: Callable[[int, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Download the Windows installer .exe atomically into data_dir/updates/."""
    _validate_download_url(download_url)
    resolved_dir = data_dir if data_dir is not None else get_data_dir()
    updates_dir = resolved_dir / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(asset_name).name).strip("._") or "SimsVerticalStudio-Setup.exe"
    if not safe_name.lower().endswith(".exe"):
        safe_name += ".exe"

    final_path = updates_dir / safe_name
    part_path = updates_dir / f"{safe_name}.part"

    if on_progress:
        on_progress(5, "Connecting to GitHub...")

    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True) as client:
            with client.stream("GET", download_url, headers={"User-Agent": "SimsVerticalStudio-Updater"}) as resp:
                resp.raise_for_status()
                total_bytes = int(resp.headers.get("content-length") or 0)
                downloaded = 0
                last_pct = 5
                with open(part_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=131072):
                        if cancel_event is not None and cancel_event.is_set():
                            raise UpdaterError("Update download cancelled")
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_bytes > 0 and on_progress:
                            pct = min(95, 5 + int((downloaded / total_bytes) * 90))
                            if pct != last_pct:
                                last_pct = pct
                                mb_done = downloaded / (1024 * 1024)
                                mb_tot = total_bytes / (1024 * 1024)
                                on_progress(pct, f"Downloading update ({mb_done:.1f} / {mb_tot:.1f} MB)...")
        part_path.replace(final_path)
    except Exception:
        part_path.unlink(missing_ok=True)
        raise

    if on_progress:
        on_progress(98, "Launching installer...")
    return final_path


def launch_windows_installer_and_exit(installer_path: Path, exit_delay_sec: float = 0.8) -> None:
    """Spawn the Inno Setup installer silently and schedule current process shutdown."""
    if sys.platform != "win32":
        raise UpdaterError("Self-update installer launch is only supported on Windows.")
    if not installer_path.is_file():
        raise UpdaterError(f"Installer file not found: {installer_path}")

    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    subprocess.Popen(
        [
            str(installer_path),
            "/SP-",
            "/SILENT",
            "/CLOSEAPPLICATIONS",
            "/NORESTART",
        ],
        creationflags=creation_flags,
        close_fds=True,
    )

    def _delayed_exit() -> None:
        time.sleep(exit_delay_sec)
        os._exit(0)

    threading.Thread(target=_delayed_exit, name="updater-exit", daemon=True).start()
