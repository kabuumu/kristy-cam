"""Milestone 4 tests for release packaging, asset manifest, notices, .gitignore, and export benchmark."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.bootstrap import KOKORO_ASSETS
from scripts.package_release import (
    assemble_release_archive,
    run_benchmark,
    verify_release_archive,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 1. assets/asset_manifest.json
# ---------------------------------------------------------------------------


def test_asset_manifest_schema_and_kokoro_models() -> None:
    manifest_path = REPO_ROOT / "assets" / "asset_manifest.json"
    assert manifest_path.is_file(), f"Missing {manifest_path}"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1

    models = manifest.get("models", {})
    for model_name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        assert model_name in models, f"Missing {model_name} in asset_manifest.json"
        entry = models[model_name]
        expected = KOKORO_ASSETS[model_name]
        assert entry["url"] == expected["url"]
        assert int(entry["size_bytes"]) == int(expected["size"])
        assert entry["sha256"] == expected["sha256"]


def test_asset_manifest_runtime_binaries_and_bundled_fonts() -> None:
    manifest_path = REPO_ROOT / "assets" / "asset_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    rt = manifest.get("runtime_binaries", {})
    assert rt.get("provider") == "static-ffmpeg"
    assert "ffmpeg" in rt.get("binaries", [])
    assert "ffprobe" in rt.get("binaries", [])
    assert "libx264" in rt.get("required_encoders", [])
    assert "aac" in rt.get("required_encoders", [])
    assert "ass" in rt.get("required_filters", [])
    assert "subtitles" in rt.get("required_filters", [])
    for platform in ("linux-x86_64", "darwin-arm64", "win32-AMD64"):
        assert platform in rt.get("platforms", [])

    fonts = manifest.get("bundled_fonts", {})
    for font_key in ("Montserrat-ExtraBold.ttf", "OFL.txt"):
        assert font_key in fonts, f"Missing {font_key} in bundled_fonts"
        meta = fonts[font_key]
        font_file = REPO_ROOT / meta["path"]
        assert font_file.is_file(), f"Bundled font file missing on disk: {font_file}"
        assert font_file.stat().st_size == int(meta["size_bytes"])
        assert _sha256_file(font_file) == meta["sha256"]


# ---------------------------------------------------------------------------
# 2. assets/THIRD_PARTY_NOTICES.md
# ---------------------------------------------------------------------------


def test_third_party_notices_covers_required_components() -> None:
    notices_path = REPO_ROOT / "assets" / "THIRD_PARTY_NOTICES.md"
    assert notices_path.is_file(), f"Missing {notices_path}"

    text = notices_path.read_text(encoding="utf-8")
    assert text.strip(), "THIRD_PARTY_NOTICES.md must not be empty"

    lowered = text.lower()
    required_terms = [
        "montserrat",
        "sil ofl 1.1",
        "kokoro-onnx",
        "onnxruntime",
        "espeakng-loader",
        "phonemizer",
        "ffmpeg",
        "fastapi",
        "react",
    ]
    for term in required_terms:
        assert term in lowered, f"THIRD_PARTY_NOTICES.md missing required notice for {term!r}"


# ---------------------------------------------------------------------------
# 3. .gitignore
# ---------------------------------------------------------------------------


def test_gitignore_rules_and_frontend_dist_not_ignored() -> None:
    gitignore_path = REPO_ROOT / ".gitignore"
    assert gitignore_path.is_file(), f"Missing {gitignore_path}"

    lines = [
        line.strip()
        for line in gitignore_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert ".venv/" in lines
    assert "frontend/node_modules/" in lines
    assert any(p in lines for p in ("/dist/", "dist/"))
    assert "__pycache__/" in lines
    assert ".pytest_cache/" in lines
    assert any(p in lines for p in ("*.pyc", "*.py[cod]"))
    assert ".agents/" in lines
    assert "frontend/dist/" not in lines

    # If git is available in the repo, verify actual ignore evaluation via git check-ignore
    git_bin = shutil.which("git")
    if git_bin and (REPO_ROOT / ".git").exists():
        ignored_samples = [
            ".venv/bin/python",
            "frontend/node_modules/react/index.js",
            "dist/sims-vertical-studio-v1.0.0.zip",
            "backend/__pycache__/main.cpython-312.pyc",
            ".pytest_cache/README.md",
            "backend/foo.pyc",
            ".agents/teamwork/ORIGINAL_REQUEST.md",
        ]
        for sample in ignored_samples:
            res = subprocess.run(
                [git_bin, "check-ignore", "-q", sample],
                cwd=str(REPO_ROOT),
                capture_output=True,
                check=False,
            )
            assert res.returncode == 0, f"Expected {sample!r} to be ignored by .gitignore"

        res_dist = subprocess.run(
            [git_bin, "check-ignore", "-q", "frontend/dist/index.html"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=False,
        )
        assert res_dist.returncode != 0, "frontend/dist/index.html must NOT be ignored by .gitignore"


# ---------------------------------------------------------------------------
# 4. scripts/package_release.py archive assembly + verification
# ---------------------------------------------------------------------------


def test_package_release_cli_assemble_and_verify(tmp_path: Path) -> None:
    archive_path = tmp_path / "release.zip"
    script_path = REPO_ROOT / "scripts" / "package_release.py"

    proc = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--output",
            str(archive_path),
            "--skip-frontend-build",
            "--verify",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, f"package_release.py failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert archive_path.is_file()

    report = json.loads(proc.stdout)
    assert Path(report["archive_path"]) == archive_path.resolve()
    assert report["verification"]["start_sh_mode"] == "0o755"

    with zipfile.ZipFile(archive_path, "r") as zf:
        names = set(zf.namelist())

        required_files = [
            "frontend/dist/index.html",
            "pyproject.toml",
            "uv.lock",
            "SPEC.md",
            "start.bat",
            "start.sh",
            "assets/fonts/Montserrat-ExtraBold.ttf",
            "assets/fonts/OFL.txt",
            "assets/asset_manifest.json",
            "assets/THIRD_PARTY_NOTICES.md",
        ]
        for req in required_files:
            assert req in names, f"Missing {req} in release zip"

        assert any(n.startswith("backend/") and n.endswith(".py") for n in names)
        assert any(n.startswith("scripts/") and n.endswith(".py") for n in names)

        for name in names:
            parts = Path(name).parts
            assert "__pycache__" not in parts, f"Unexpected __pycache__ in zip: {name}"
            assert not name.endswith((".pyc", ".pyo", ".pyd")), f"Unexpected bytecode in zip: {name}"

        start_sh_info = zf.getinfo("start.sh")
        unix_mode = (start_sh_info.external_attr >> 16) & 0o777
        assert unix_mode == 0o755, f"Expected start.sh mode 0o755, got {oct(unix_mode)}"


def test_verify_release_archive_detects_tampered_font_checksum(tmp_path: Path) -> None:
    archive_path = assemble_release_archive(
        output_path=tmp_path / "clean_release.zip",
        skip_frontend_build=True,
    )
    # Clean archive passes
    verify_release_archive(archive_path)

    # Tamper with Montserrat-ExtraBold.ttf inside a copy of the archive
    tampered_font_zip = tmp_path / "tampered_font.zip"
    with zipfile.ZipFile(archive_path, "r") as src, zipfile.ZipFile(
        tampered_font_zip, "w", compression=zipfile.ZIP_DEFLATED
    ) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "assets/fonts/Montserrat-ExtraBold.ttf":
                data = data[:-16] + b"\x00" * 16
            dst.writestr(info, data)

    with pytest.raises(RuntimeError, match="SHA-256 mismatch|Size mismatch"):
        verify_release_archive(tampered_font_zip)


def test_verify_release_archive_detects_tampered_manifest(tmp_path: Path) -> None:
    archive_path = assemble_release_archive(
        output_path=tmp_path / "clean_release.zip",
        skip_frontend_build=True,
    )

    tampered_manifest_zip = tmp_path / "tampered_manifest.zip"
    with zipfile.ZipFile(archive_path, "r") as src, zipfile.ZipFile(
        tampered_manifest_zip, "w", compression=zipfile.ZIP_DEFLATED
    ) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "assets/asset_manifest.json":
                parsed = json.loads(data.decode("utf-8"))
                parsed["bundled_fonts"]["OFL.txt"]["sha256"] = "0" * 64
                data = json.dumps(parsed).encode("utf-8")
            dst.writestr(info, data)

    with pytest.raises(RuntimeError, match="does not match repository copy"):
        verify_release_archive(tampered_manifest_zip)


# ---------------------------------------------------------------------------
# 5. End-to-end export criteria & source integrity benchmark
# ---------------------------------------------------------------------------


def test_end_to_end_export_benchmark_and_source_integrity() -> None:
    prev_data_dir = os.environ.get("SIMS_STUDIO_DATA_DIR")
    result = run_benchmark()
    assert os.environ.get("SIMS_STUDIO_DATA_DIR") == prev_data_dir

    assert result["source_sha256_unchanged"] is True
    assert len(result["source_sha256"]) == 64
    assert result["export_size_bytes"] > 0
    assert result["tts_elapsed_sec"] >= 0.0
    assert result["export_elapsed_sec"] >= 0.0

    val = result["validation"]
    assert val["width"] == 1080
    assert val["height"] == 1920
    assert val["sar"] == "1:1"
    assert val["pix_fmt"] == "yuv420p"
    assert val["video_codec"] == "h264"
    assert abs(val["avg_fps"] - 30.0) <= 0.05
    assert abs(val["r_fps"] - 30.0) <= 0.05
    assert val["audio_codec"] == "aac"
    assert val["sample_rate"] == 48000
    assert val["channels"] == 2
    assert val["faststart"] is True
    assert val["moov_offset"] < val["mdat_offset"]
    assert val["expected_frame_count"] > 0
    assert val["frame_count"] == val["expected_frame_count"]
    assert val["av_duration_delta_sec"] <= (1.0 / 30.0)
