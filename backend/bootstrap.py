"""Runtime bootstrap and asset verification for Sims 4 Vertical Video Studio."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR_NAME = "SimsVerticalStudio"

KOKORO_ASSETS: dict[str, dict[str, object]] = {
    "kokoro-v1.0.onnx": {
        "url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
        "size": 325532387,
        "sha256": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    },
    "voices-v1.0.bin": {
        "url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        "size": 28214398,
        "sha256": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
    },
}

VALID_FONT_HEADERS = (
    b"\x00\x01\x00\x00",  # TrueType
    b"OTTO",              # OpenType CFF
    b"true",              # Apple TrueType
    b"ttcf",              # TrueType Collection
)


def is_offline_env() -> bool:
    """Return True if SIMS_STUDIO_OFFLINE is set to a truthy value."""
    val = os.environ.get("SIMS_STUDIO_OFFLINE", "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def get_data_dir() -> Path:
    """Resolve and create the per-user writable data directory."""
    override = os.environ.get("SIMS_STUDIO_DATA_DIR", "").strip()
    if override:
        base = Path(override).expanduser().resolve()
    elif sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local_app_data) if local_app_data else (Path.home() / "AppData" / "Local")
        base = root / APP_DIR_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        root = Path(xdg_data) if xdg_data else (Path.home() / ".local" / "share")
        base = root / APP_DIR_NAME

    for sub in ("models", "bin", "logs", "projects"):
        (base / sub).mkdir(parents=True, exist_ok=True)

    pronunciations_path = base / "pronunciations.json"
    if not pronunciations_path.exists():
        pronunciations_path.write_text("{}\n", encoding="utf-8")

    return base


def _run_subprocess(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def verify_ffmpeg(data_dir: Path | None = None, offline: bool = False) -> tuple[Path, Path]:
    """Locate and verify ffmpeg and ffprobe with libx264, aac, and libass support."""
    if data_dir is None:
        data_dir = get_data_dir()

    offline = offline or is_offline_env()
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    bin_dir = data_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    cached_ffmpeg = bin_dir / f"ffmpeg{exe_suffix}"
    cached_ffprobe = bin_dir / f"ffprobe{exe_suffix}"

    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None

    if cached_ffmpeg.is_file() and cached_ffprobe.is_file():
        ffmpeg_path = cached_ffmpeg
        ffprobe_path = cached_ffprobe
    else:
        sys_ffmpeg = shutil.which("ffmpeg")
        sys_ffprobe = shutil.which("ffprobe")
        if sys_ffmpeg and sys_ffprobe:
            ffmpeg_path = Path(sys_ffmpeg)
            ffprobe_path = Path(sys_ffprobe)
        else:
            if offline:
                raise RuntimeError(
                    "FFmpeg/ffprobe not found in data_dir/bin or system PATH while running in offline mode. "
                    "Run `python -m backend.bootstrap` online first."
                )
            try:
                from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise

                src_ffmpeg, src_ffprobe = get_or_fetch_platform_executables_else_raise()
                shutil.copy2(src_ffmpeg, cached_ffmpeg)
                shutil.copy2(src_ffprobe, cached_ffprobe)
                if sys.platform != "win32":
                    cached_ffmpeg.chmod(0o755)
                    cached_ffprobe.chmod(0o755)
                ffmpeg_path = cached_ffmpeg
                ffprobe_path = cached_ffprobe
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to fetch FFmpeg binaries via static-ffmpeg: {exc}"
                ) from exc

    # Verify native execution
    ver_ffmpeg = _run_subprocess([str(ffmpeg_path), "-version"])
    if ver_ffmpeg.returncode != 0:
        raise RuntimeError(
            f"FFmpeg binary failed to execute ({ffmpeg_path}): {ver_ffmpeg.stderr.strip()}"
        )

    ver_ffprobe = _run_subprocess([str(ffprobe_path), "-version"])
    if ver_ffprobe.returncode != 0:
        raise RuntimeError(
            f"ffprobe binary failed to execute ({ffprobe_path}): {ver_ffprobe.stderr.strip()}"
        )

    # Verify encoders: libx264 and aac
    encoders_res = _run_subprocess([str(ffmpeg_path), "-hide_banner", "-encoders"])
    if encoders_res.returncode != 0:
        raise RuntimeError(f"Failed to query FFmpeg encoders: {encoders_res.stderr.strip()}")
    encoders_out = encoders_res.stdout
    for required_enc in ("libx264", "aac"):
        if not re.search(rf"^\s*[A-Z.]+\s+{re.escape(required_enc)}\b", encoders_out, re.MULTILINE):
            raise RuntimeError(
                f"FFmpeg at {ffmpeg_path} is missing required encoder '{required_enc}'."
            )

    # Verify libass filters: ass and subtitles
    filters_res = _run_subprocess([str(ffmpeg_path), "-hide_banner", "-filters"])
    if filters_res.returncode != 0:
        raise RuntimeError(f"Failed to query FFmpeg filters: {filters_res.stderr.strip()}")
    filters_out = filters_res.stdout
    for required_filter in ("ass", "subtitles"):
        if not re.search(rf"^\s*[A-Z.]+\s+{re.escape(required_filter)}\b", filters_out, re.MULTILINE):
            raise RuntimeError(
                f"FFmpeg at {ffmpeg_path} is missing required libass filter '{required_filter}'."
            )

    return ffmpeg_path, ffprobe_path


def verify_font(repo_root: Path | None = None) -> Path:
    """Verify bundled Montserrat-ExtraBold.ttf exists, is non-empty, and has a valid TTF/OTF header."""
    root = repo_root if repo_root is not None else REPO_ROOT
    font_path = root / "assets" / "fonts" / "Montserrat-ExtraBold.ttf"
    if not font_path.is_file():
        raise RuntimeError(f"Bundled caption font not found: {font_path}")

    size = font_path.stat().st_size
    if size == 0:
        raise RuntimeError(f"Bundled caption font is empty: {font_path}")

    with font_path.open("rb") as f:
        header = f.read(4)

    if header not in VALID_FONT_HEADERS:
        raise RuntimeError(
            f"Bundled caption font {font_path} has invalid TTF/OTF header: {header!r}"
        )

    return font_path


def _compute_sha256_and_size(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


_PROXY_ENV_LOCK = threading.Lock()


def _sanitize_no_proxy_value(raw: str) -> str:
    """Unbracket IPv6 addresses in no_proxy/NO_PROXY so httpx does not fail parsing them."""
    entries: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if item.startswith("[") and item.endswith("]"):
            inner = item[1:-1]
            try:
                addr = ipaddress.ip_address(inner)
                if isinstance(addr, ipaddress.IPv6Address):
                    item = inner
            except ValueError:
                pass
        if item not in seen:
            seen.add(item)
            entries.append(item)
    return ",".join(entries)


@contextmanager
def _sanitized_proxy_env() -> Iterator[None]:
    """Temporarily sanitize no_proxy/NO_PROXY in os.environ while constructing httpx.Client."""
    with _PROXY_ENV_LOCK:
        saved: dict[str, str | None] = {}
        try:
            for key in ("no_proxy", "NO_PROXY"):
                if key in os.environ:
                    saved[key] = os.environ[key]
                    os.environ[key] = _sanitize_no_proxy_value(os.environ[key])
            yield
        finally:
            for key, old_val in saved.items():
                if old_val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_val


def _download_verified(url: str, dest_path: Path, expected_sha256: str, expected_size: int) -> None:
    tmp_path = dest_path.with_name(f".{dest_path.name}.tmp")
    h = hashlib.sha256()
    total = 0
    try:
        with _sanitized_proxy_env():
            client = httpx.Client(follow_redirects=True, timeout=120.0)
        with client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with tmp_path.open("wb") as out:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        out.write(chunk)
                        h.update(chunk)
                        total += len(chunk)
                    out.flush()
                    os.fsync(out.fileno())

        actual_sha256 = h.hexdigest()
        if total != expected_size:
            raise RuntimeError(
                f"Downloaded {dest_path.name} size mismatch: expected {expected_size} bytes, got {total}."
            )
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"Downloaded {dest_path.name} SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}."
            )
        os.replace(tmp_path, dest_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def ensure_kokoro_models(data_dir: Path, offline: bool = False) -> tuple[Path, Path]:
    """Ensure Kokoro ONNX model and voices pack exist in data_dir/models and pass SHA-256 verification.

    Makes zero network calls if both files are already cached and valid.
    """
    offline = offline or is_offline_env()
    models_dir = data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    resolved: dict[str, Path] = {}
    for filename, meta in KOKORO_ASSETS.items():
        target = models_dir / filename
        expected_size = int(meta["size"])
        expected_sha256 = str(meta["sha256"])
        url = str(meta["url"])

        if target.is_file():
            actual_sha256, actual_size = _compute_sha256_and_size(target)
            if actual_size == expected_size and actual_sha256 == expected_sha256:
                resolved[filename] = target
                continue
            if offline:
                raise RuntimeError(
                    f"Cached model file {target} is corrupt or incomplete "
                    f"(size={actual_size}/{expected_size}, sha256={actual_sha256}/{expected_sha256}) "
                    "and offline mode is enabled. Run `python -m backend.bootstrap` online to repair."
                )
        elif offline:
            raise RuntimeError(
                f"Required model file {target} is missing and offline mode is enabled. "
                "Run `python -m backend.bootstrap` online first."
            )

        print(f"Downloading {filename}...")
        _download_verified(url, target, expected_sha256=expected_sha256, expected_size=expected_size)
        resolved[filename] = target

    return resolved["kokoro-v1.0.onnx"], resolved["voices-v1.0.bin"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify FFmpeg, bundled fonts, and Kokoro ONNX model assets."
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not attempt any network downloads; fail if cached assets are missing or corrupt.",
    )
    args = parser.parse_args(argv)
    offline = bool(args.offline) or is_offline_env()

    try:
        data_dir = get_data_dir()
        font_path = verify_font()
        ffmpeg_path, ffprobe_path = verify_ffmpeg(data_dir=data_dir, offline=offline)
        model_path, voices_path = ensure_kokoro_models(data_dir=data_dir, offline=offline)
    except Exception as exc:
        print(f"[ERROR] Bootstrap verification failed: {exc}", file=sys.stderr)
        return 1

    print(f"Data directory : {data_dir}")
    print(f"Caption font   : {font_path}")
    print(f"FFmpeg         : {ffmpeg_path}")
    print(f"FFprobe        : {ffprobe_path}")
    print(f"Kokoro model   : {model_path}")
    print(f"Kokoro voices  : {voices_path}")
    print("Bootstrap verification PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
