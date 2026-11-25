# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller --onedir spec for Sims 4 Vertical Video Studio (Windows native desktop app)."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "frontend" / "dist"), "frontend/dist"),
    (str(ROOT / "assets"), "assets"),
]

# Include pre-fetched FFmpeg/ffprobe binaries if present in packaging/bin
bundled_bin = ROOT / "packaging" / "bin"
if bundled_bin.is_dir():
    for exe in bundled_bin.glob("*.exe"):
        datas.append((str(exe), "bin"))

# Include embedded build metadata for GitHub auto-updater
build_info_file = ROOT / "packaging" / "build_info.json"
if build_info_file.is_file():
    datas.append((str(build_info_file), "."))

binaries = []
hiddenimports = [
    "backend.main",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "multipart",
    "python_multipart",
]

for pkg in (
    "espeakng_loader",
    "onnxruntime",
    "kokoro_onnx",
    "phonemizer",
    "soundfile",
    "static_ffmpeg",
    "webview",
    "pythonnet",
    "clr_loader",
):
    try:
        pkg_datas, pkg_bins, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_bins
        hiddenimports += pkg_hidden
    except Exception:
        pass

hiddenimports += collect_submodules("backend")

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "playwright"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SimsVerticalStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="SimsVerticalStudio",
)
