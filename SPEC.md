# Sims 4 Vertical Video Studio (V1) — Authoritative Specification

## Overview
**Sims 4 Vertical Video Studio** is a local-first desktop web application for converting `16:9` *The Sims 4* gameplay recordings into narrated `9:16` vertical videos (`1080x1920`, `30fps` CFR, H.264 `yuv420p`, `48kHz` stereo AAC, `+faststart` MP4) with burned-in `.ass` captions.

For the full detailed technical specification, see [ORIGINAL_REQUEST.md](.agents/teamwork/ORIGINAL_REQUEST.md) and the project architecture index in [PROJECT.md](PROJECT.md).

## V1 Baseline Contract
- **Output Format**: `1080x1920` (`9:16`), square pixels (`SAR 1:1`), `30fps` CFR, H.264 (`yuv420p`), `48kHz` stereo AAC, `+faststart` MP4.
- **Inference & Rendering**: Local CPU execution (`CPUExecutionProvider` for ONNX Runtime), FFmpeg/ffprobe via `subprocess` (`shell=False`).
- **Scope**: One source recording per project, one narrator voice and language per project, per-segment narration speed (`0.8x`–`1.25x`), hard cuts between segments, local atomic JSON persistence (`project.json` + `.bak`, `manifest.json`), and zero cloud APIs or accounts.
- **Runtime & Environments**:
  - Python `3.12` managed via `uv` and locked in `uv.lock` (`linux-x86_64`, `darwin-arm64`, `win32-AMD64`).
  - Development: macOS ARM64 (and Linux x86_64).
  - End-user runtime: Windows 10/11 (x64) without pre-installed Python, Node.js, or FFmpeg.
  - Network policy: Internet permitted only on first launch to bootstrap `uv`, Python `3.12`, locked wheels, FFmpeg binaries, and Kokoro ONNX models (`kokoro-v1.0.onnx`, `voices-v1.0.bin`). All editing, synthesis, previewing, and exporting run 100% offline thereafter.
- **Writable Data Root**: Outside install directory (`SIMS_STUDIO_DATA_DIR`, `%LOCALAPPDATA%\SimsVerticalStudio` on Windows, `~/Library/Application Support/SimsVerticalStudio` on macOS, `~/.local/share/SimsVerticalStudio` on Linux), containing `models/`, `bin/`, `logs/`, `projects/`, and `pronunciations.json`.
- **Loopback Security**: Strictly `127.0.0.1:8420`, per-launch `X-Session-Token`, `Host`/`Origin` validation, no wildcard CORS, no raw filesystem paths or FFmpeg args from frontend.
