# Third-Party Notices

Sims 4 Vertical Video Studio incorporates or downloads the following third-party software, fonts, and model weights. Each component is governed by its respective license terms.

---

## Bundled Fonts

### Montserrat (`Montserrat-ExtraBold.ttf`)
- **License:** SIL Open Font License 1.1 (SIL OFL 1.1)
- **Copyright:** The Montserrat Project Authors (`https://github.com/JulietaUla/Montserrat`)
- **Notice:** Full license text is bundled at `assets/fonts/OFL.txt`.

---

## Speech Synthesis & Model Weights

### `kokoro-onnx`
- **License:** MIT License
- **Repository:** `https://github.com/thewh1teagle/kokoro-onnx`

### Kokoro-82M Model Files (`kokoro-v1.0.onnx`, `voices-v1.0.bin`)
- **License:** Apache License 2.0
- **Source:** `https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0`
- **Notice:** Downloaded on first launch and verified by SHA-256 checksum against `assets/asset_manifest.json`.

### `onnxruntime`
- **License:** MIT License
- **Copyright:** Microsoft Corporation (`https://github.com/microsoft/onnxruntime`)

### `espeakng-loader` & `eSpeak NG`
- **License:** GNU General Public License v3.0 (GPL-3.0)
- **Notice:** `espeakng-loader` packages eSpeak NG phonemizer libraries licensed under GPL-3.0.

### `phonemizer`
- **License:** GNU General Public License v3.0 (GPL-3.0)
- **Repository:** `https://github.com/bootphon/phonemizer`

---

## Media Engine

### `FFmpeg`, `ffprobe`, and `static-ffmpeg`
- **License:** GNU Lesser General Public License v2.1+ (LGPL-2.1+) / GNU General Public License v2.0+ (GPL-2.0+); `libx264` is licensed under GPL-2.0+
- **Notice:** Invoked as external CLI subprocesses (`ffmpeg`, `ffprobe`) via `static-ffmpeg` (`3.0`).
- **FFmpeg Project:** `https://ffmpeg.org/`
- **static-ffmpeg:** `https://github.com/zackees/static_ffmpeg`

---

## Python Backend Dependencies

| Package | License |
| --- | --- |
| `fastapi` | MIT License |
| `uvicorn` | BSD-3-Clause License |
| `pydantic` | MIT License |
| `httpx` | BSD-3-Clause License |
| `soundfile` | BSD-3-Clause License |
| `numpy` | BSD-3-Clause License |

---

## Frontend Dependencies (Compiled into `frontend/dist/`)

| Package | License |
| --- | --- |
| `react` | MIT License |
| `react-dom` | MIT License |
| `vite` | MIT License |
| `tailwindcss` (`@tailwindcss/vite`) | MIT License |
| `lucide-react` | ISC License |
