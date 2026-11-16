# Project: Sims 4 Vertical Video Studio (V1)

## Architecture
Local-first desktop web application converting `16:9` *The Sims 4* gameplay recordings into narrated `9:16` (`1080x1920`, `30fps` CFR, H.264 `yuv420p`, `48kHz` stereo AAC, `+faststart`) vertical MP4s with burned-in `.ass` captions.
- **Backend**: Python `3.12` (`uv` + `uv.lock`), FastAPI + `uvicorn` bound strictly to `127.0.0.1:8420`, Pydantic v2 schemas, atomic JSON persistence (`project.json` + `.bak`, `manifest.json`), bounded async job queue (`jobs.py`), FFmpeg/ffprobe subprocess pipelines (no shell), and local CPU Kokoro ONNX TTS (`CPUExecutionProvider`).
- **Frontend**: React 19 + TypeScript + Vite + Tailwind CSS v4 dark creator studio (`#0f1115`), compiled to `frontend/dist/` and served by FastAPI in production.
- **Writable Data Root**: Outside install directory (`%LOCALAPPDATA%\SimsVerticalStudio` on Windows, `~/Library/Application Support/SimsVerticalStudio` on macOS, overridable via `SIMS_STUDIO_DATA_DIR`).

## Code Layout
```text
sims-studio/ (project root: /home/antigravity/kabuumu/kristy-cam)
├── SPEC.md                         # Authoritative V1 specification summary/pointer
├── PROJECT.md                      # Living Project Orchestrator index
├── pyproject.toml                  # Python 3.12 dependencies + [tool.uv] cross-platform environments
├── uv.lock                         # Committed cross-platform lockfile (linux-x86_64, darwin-arm64, win32-AMD64)
├── start.bat                       # Windows 10/11 x64 launcher
├── start.sh                        # macOS ARM64 launcher
├── assets/
│   ├── asset_manifest.json         # Approved runtime binaries, models, fonts & SHA-256 checksums
│   ├── THIRD_PARTY_NOTICES.md      # Third-party license notices (OFL, MIT, Apache, GPL/LGPL FFmpeg)
│   └── fonts/
│       ├── Montserrat-ExtraBold.ttf
│       └── OFL.txt
├── scripts/
│   ├── verify_env.py               # Milestone 0 smoke test (FFmpeg libass, Kokoro CPU TTS, .ass burn-in, offline)
│   └── package_release.py          # Milestone 4 release archive builder + manifest/benchmark verifier
├── backend/
│   ├── __init__.py
│   ├── main.py                     # FastAPI routing, security middleware, Range streaming, static mount
│   ├── schemas.py                  # Pydantic v2 models: ProjectInput, ArtifactManifest, JobState
│   ├── bootstrap.py                # Runtime verification, model/FFmpeg download + SHA-256 checks, font checks
│   ├── storage.py                  # Atomic JSON writes, .bak recovery, revision locking, source relinking
│   ├── jobs.py                     # Bounded worker queue, progress %, cancellation, restart recovery
│   ├── media.py                    # OS-safe native file picker + headless fallback, ffprobe validation, fingerprinting
│   ├── proxy.py                    # 540p 30fps CFR proxy transcoder & timestamp mapping
│   ├── geometry.py                 # Crop coordinate math, bounds clamping, linear interpolation, trim-shift check
│   ├── tts.py                      # Kokoro CPU singleton, pronunciation replacer, sample-accurate synthesis
│   ├── subtitles.py                # ASS text escaping, clause continuity timing, .ass file writer
│   └── renderer.py                 # Duration planner, hash-cached Stage 1 segment renderer, Stage 2 concat + burn-in
├── tests/                          # Backend unit, integration & Tier 5 hardening pytest suite
│   ├── test_m1_core.py
│   ├── test_m2_media_render.py
│   ├── test_m3_api_security.py
│   ├── test_m4_release.py
│   └── test_tier5_hardening.py
├── e2e/                            # Opaque-box E2E test suite (owned by E2E Testing Track)
└── frontend/
    ├── package.json
    ├── vite.config.ts
    ├── index.html
    ├── dist/                       # Prebuilt static assets served by FastAPI
    └── src/
        ├── main.tsx
        ├── index.css
        ├── App.tsx
        ├── api/client.ts
        ├── types/studio.ts
        ├── utils/studioMath.ts
        └── components/
            ├── CropCanvas.tsx
            ├── ScriptPanel.tsx
            ├── Timeline.tsx
            └── JobStatusBar.tsx
```

## Feature Inventory
Every Survey feature (`F1`–`F42`) is assigned to a milestone below:

| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F1 | Cross-platform `uv` dependency lock | `pyproject.toml` (`requires-python == "3.12.*"`, `[tool.uv] environments` for linux-x86_64, darwin-arm64, win32-AMD64) + `uv.lock` | M0 | Survey |
| F2 | Runtime bootstrap & verification | `bootstrap.py` verifies FFmpeg (`libx264`, `aac`, `libass`), bundled font, and downloads/verifies Kokoro ONNX + voices via SHA-256 | M0 | Survey |
| F3 | M0 smoke test script | `scripts/verify_env.py` tests FFmpeg `libass`, Kokoro CPU TTS + pronunciation replacement, `.ass` burn-in onto `1080x1920` `30fps` clip, and offline mode | M0 | Survey |
| F4 | Windows launcher (`start.bat`) | Installs `uv` if missing, `uv sync --locked`, `backend.bootstrap`, `/api/health` single-instance guard, launches `uvicorn` + browser | M0 | Survey |
| F5 | macOS launcher (`start.sh`) | macOS ARM64 launcher with `uv` check, `uv sync --locked`, `backend.bootstrap`, `/api/health` guard, `uvicorn` + browser | M0 | Survey |
| F6 | Distributable release archive | Packaged release archive with `backend/`, `scripts/`, `frontend/dist/`, `pyproject.toml`, `uv.lock`, launchers, `assets/`, SHA-256 asset manifest, third-party notices | M4 | Survey |
| F7 | Pydantic v2 data models | `schemas.py` separating `ProjectInput`, `ArtifactManifest`, `JobState`; `segments` array position = sole order | M1 | Survey |
| F8 | Per-user data directory layout | `SIMS_STUDIO_DATA_DIR` / OS default with `models/`, `pronunciations.json`, `logs/`, `projects/<id>/` (`artifacts/`, `exports/`, `tmp/`) | M0 | Survey |
| F9 | Atomic JSON saves & `.bak` recovery | Temp file in same dir -> flush + `fsync` -> copy to `.bak` -> `os.replace` | M1 | Survey |
| F10 | Optimistic revision locking | `PUT /api/projects/{id}` checks `expected_revision`; returns `HTTP 409 Conflict` on mismatch | M1 | Survey |
| F11 | Schema version guard | Reject `project.json` with `schema_version` > supported version without overwriting | M1 | Survey |
| F12 | Global & project-owned pronunciations | Global `pronunciations.json` seeds new projects; future global edits never mutate existing projects | M1 | Survey |
| F13 | Source portability & fingerprinting | Fast fingerprint (`size` + `mtime` + partial SHA-256 header/footer), optional `copied_to_project`, missing-source detection & relink | M1 | Survey |
| F14 | Normalized top-left crop coordinates | `{t, x, y, zoom}` (`zoom` `[1.0, 3.0]`), `base_crop_h = H`, `base_crop_w = H * 9/16`, `ideal_crop = base / zoom` | M1 | Survey |
| F15 | Bounds clamping & validation | Enforce `[0, W - ideal_crop_w]` and `[0, H - ideal_crop_h]`, keyframe at `t = 0.0`, finite/unique/strictly increasing `t` | M1 | Survey |
| F16 | Linear interpolation & hold | Linear interpolation of `x`, `y`, `zoom` between keyframes; hold final keyframe after its `t`; freeze final frame on extension | M1 | Survey |
| F17 | Trim-shift keyframe detection | Detect keyframes past shortened segment duration; flag for UI clamp/scale/delete prompt (never silent discard) | M1 | Survey |
| F18 | Even pixel rounding & FFmpeg transform | `round_even(ideal_crop_w/h)`, static `crop` + `scale=1080:1920:flags=lanczos,setsar=1,fps=30`, animated `zoompan` | M1 | Survey |
| F19 | Dynamic Kokoro voice discovery | Discover voices dynamically from validated `voices-v1.0.bin` via `kokoro.get_voices()` | M1 | Survey |
| F20 | Paired clauses & speed control | `speed` `[0.8, 1.25]`, ordered `clauses` `{id, display_text, spoken_text_override}`, no whole-segment unmapped overrides | M1 | Survey |
| F21 | Non-recursive pronunciation replacer | Literal-escaped, case-insensitive, Unicode word boundaries `(?<!\w)`/`(?!\w)`, single non-recursive pass, longest key first | M1 | Survey |
| F22 | CPU Kokoro singleton & sample-accurate WAVs | Single `Kokoro` CPU instance, serialized synthesis, `24000 Hz` integer PCM, `120ms` inter-clause pause (omitted after final clause), `clause_timings` | M1 | Survey |
| F23 | Empty narration & `tts_hash` staleness | Empty clauses valid (`audio_duration = 0.0`); `tts_hash` covers `display_text`, `spoken_text_override`, `pronunciations`, `voice`, `language`, `speed` | M1 | Survey |
| F24 | Clause-level ASS timing & continuity | Captions stay visible across `120ms` inter-clause pause until next clause starts; final clause ends strictly at audio end | M1 | Survey |
| F25 | ASS styling, bundled font & escaping | `PlayResX: 1080`, `PlayResY: 1920`, `WrapStyle: 2`, `\an2`, `fontsdir`, escape `\`, `{`, `}`, newlines -> `\N` | M1 | Survey |
| F26 | OS-safe native file picker | `osascript` (macOS), PowerShell `OpenFileDialog` (Windows), manual path / multipart upload fallback for headless/Linux | M2 | Survey |
| F27 | `ffprobe` input media validation | Reject HDR PQ/HLG, rotation != 0, `SAR != 1:1`, non-`16:9` (`1.77–1.78`), undecodable codecs; allow missing audio | M2 | Survey |
| F28 | 540p 30fps CFR proxy transcoder | `artifacts/proxy.mp4` (`scale=-2:540`, `fps=30`, `-g 15`, `yuv420p`, `libx264 -preset ultrafast -crf 26`, `+faststart`) + `ffprobe` check | M2 | Survey |
| F29 | Bounded async job queue & recovery | Input snapshots, `202` + `job_id`, `queued/running/succeeded/failed/cancelled/interrupted`, subprocess cancel, startup `interrupted` + `tmp/` purge | M2 | Survey |
| F30 | Duration reconciliation modes | `preserve_video` and `fit_narration` with `apad` / `tpad=stop_mode=clone` on source EOF | M2 | Survey |
| F31 | Upward frame quantization & audio mix | `frame_count = ceil(raw_target * 30)`, `final_dur = frame_count / 30.0`, `48000 Hz` stereo `fltp`, `amix` + `alimiter` (`limit=0.95`) | M2 | Survey |
| F32 | Stage 1 caption-free segment renderer | `-filter_complex_script` (no shell) -> `tmp/` -> `ffprobe` -> atomic move to `artifacts/seg_{id}_{render_hash}.mp4` | M2 | Survey |
| F33 | Stage 2 concat & ASS burn-in | `ffprobe` stream check -> `concat` demuxer -> cumulative offsets -> master `.ass` -> single re-encode (`crf 19`, `+faststart`) + `-c:a copy` | M2 | Survey |
| F34 | Single-segment captioned preview | Render `1080x1920` captioned preview for a single segment | M2 | Survey |
| F35 | Content-addressed cache invalidation | `render_hash` vs `tts_hash` vs Stage 2 invalidation matrix | M2 | Survey |
| F36 | Loopback server & security middleware | `127.0.0.1:8420`, `X-Session-Token` in `index.html` meta/bootstrap, `Host`/`Origin` validation, no wildcard CORS, no path traversal | M3 | Survey |
| F37 | REST endpoints (14 route entries) | `/api/health`, `/api/capabilities`, `/api/media/select`, `/api/projects`, `/api/projects/{id}`, `relink`, `tts`, `preview`, `export`, `jobs/{id}`, `cancel`, `media/{id}`, `pronunciations` | M3 | Survey |
| F38 | HTTP Range media streaming & static UI | `HTTP 206` Partial Content, `416` Range Not Satisfiable, MIME headers, serve `frontend/dist/` | M3 | Survey |
| F39 | Studio shell, API client & types | Three-zone dark studio (`#0f1115`), header, `JobStatusBar.tsx`, `client.ts`, `types/studio.ts` | M3 | Survey |
| F40 | Crop & Preview Stage (`CropCanvas.tsx`) | `proxy.mp4`, clamped `9:16` box, zoom handles (`1.0x–3.0x`), mask toggle, safe-area guide, keyframe scrubber, in-place preview, trim-shift prompt | M3 | Survey |
| F41 | Narration & Pronunciation Inspector (`ScriptPanel.tsx`) | Dynamic voice, speed, duration mode, source volume, clause split/merge/reorder, `spoken_text_override`, live preview, dict editor, audition, stale/error badges | M3 | Survey |
| F42 | Storyboard Timeline (`Timeline.tsx`) | Scrub bar with `I`/`O`/`+ Add Segment` (disabled in inputs), segment cards, freeze badges, reorder/duplicate/delete confirm, continuous playback, relink banner | M3 | Survey |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M0 | Clean Environment, Lockfile, Bootstrap & Launchers | F1, F2, F3, F4, F5, F8 | none | DONE (uv.lock, bootstrap.py, verify_env.py, start.sh/bat verified) |
| M1 | Deterministic Core & Geometry Engine | F7, F9, F10, F11, F12, F13, F14, F15, F16, F17, F18, F19, F20, F21, F22, F23, F24, F25 | M0 | DONE (schemas, storage, geometry, tts, subtitles; 12/12 pytest passed) |
| M2 | Media Pipeline, Proxy & Two-Stage Renderer | F26, F27, F28, F29, F30, F31, F32, F33, F34, F35 | M1 | DONE (media, proxy, jobs, renderer; 27/27 pytest passed) |
| M3 | FastAPI Security Layer & React Studio Frontend | F36, F37, F38, F39, F40, F41, F42 | M2 | DONE (main.py, frontend/dist, 38/38 pytest, 13/13 Chromium E2E) |
| M4 | Release Packaging & Cross-Platform Validation | F6 + full export criteria & benchmarks | M3 | DONE (package_release.py, asset_manifest.json, 46/46 pytest, benchmarks verified) |
| E2E | E2E Testing Track (Opaque-Box Tiers 1–4) | `TEST_INFRA.md`, `e2e/` test suite, `TEST_READY.md` | none (parallel) | DONE (TEST_READY.md published; 78 tests collected, 10 fixture self-checks passed) |
| Final | Final Milestone: E2E Pass (Tiers 1–4) + Tier 5 Hardening | Pass 100% E2E Tiers 1–4 + adversarial Tier 5 hardening | M4, E2E | DONE (Phase 1: 78/78 E2E & 46/46 tests PASSED; Phase 2: 12 Tier 5 tests integrated, 58/58 `tests/`, 78/78 `e2e/`, `--verify --benchmark` PASSED; Final Verification Wave: 2 Reviewers APPROVE, 2 Challengers APPROVE, Whole-Project Auditor CLEAN) |

## Interface Contracts

### Storage & Data Root (`bootstrap.py`, `storage.py`)
- `get_data_dir() -> Path`: checks `SIMS_STUDIO_DATA_DIR`, else `%LOCALAPPDATA%\SimsVerticalStudio` (Windows), `~/Library/Application Support/SimsVerticalStudio` (macOS), `~/.local/share/SimsVerticalStudio` (Linux).
- Subdirs: `models/`, `bin/`, `logs/`, `projects/<project_id>/{artifacts,exports,tmp,source}/`, plus `pronunciations.json`.
- Atomic write: temp file in same dir -> `flush()` + `os.fsync()` -> copy existing to `.bak` -> `os.replace()`.

### Schemas (`backend/schemas.py`)
- `ProjectInput` (`project.json`): `schema_version=1`, `project_id`, `revision`, `title`, `created_at`, `updated_at`, `source_media`, `voice`, `language`, `output_settings`, `caption_settings`, `pronunciations`, `segments`.
- `ArtifactManifest` (`manifest.json`): `project_id`, `source_metadata`, `proxy`, `segments` (keyed by `seg_id` with `tts_hash`, `audio_artifact_id`, `audio_path`, `sample_rate`, `total_samples`, `audio_duration`, `clause_timings`, `render_hash`, `segment_artifact_id`, `rendered_segment_path`, `preview_artifact_id`, `preview_path`, `latest_export_artifact_id`, `latest_export_path`).
- `JobState`: `job_id`, `project_id`, `seg_id`, `job_type`, `status` (`queued|running|succeeded|failed|cancelled|interrupted`), `progress` (`0..100`), `stage`, `error`, `result`.

### Geometry (`backend/geometry.py`)
- `validate_keyframes(keyframes, duration, width, height)`
- `evaluate_crop_at(keyframes, t, width, height) -> CropBox(x_px, y_px, w_px, h_px, zoom)`
- `detect_trim_out_of_range(keyframes, new_duration) -> list[int]`
- `build_crop_filter(keyframes, width, height, duration, fps=30) -> str`

### TTS & Subtitles (`backend/tts.py`, `backend/subtitles.py`)
- `apply_pronunciations(text, pronunciations) -> str`
- `compute_tts_hash(segment, pronunciations, voice, language, inter_clause_pause_ms) -> str`
- `synthesize_segment_tts(...) -> SegmentTTSResult`
- `escape_ass_text(text) -> str`
- `generate_ass_file(events, caption_settings, output_path)`

### Media, Proxy, Jobs, Renderer (`media.py`, `proxy.py`, `jobs.py`, `renderer.py`)
- `probe_and_validate_media(path) -> SourceMetadata`
- `compute_file_fingerprint(path) -> str`
- `generate_proxy(source_path, output_path, ...)`
- `compute_render_hash(segment, source_media, tts_hash, output_settings) -> str`
- `plan_segment_duration(segment, audio_duration, source_duration, tail_padding_sec) -> DurationPlan`
- `render_segment_stage1(...)`, `render_segment_preview(...)`, `render_final_export(...)`

### REST API (`backend/main.py`)
- Loopback `127.0.0.1:8420`, `X-Session-Token` on all mutating/privileged endpoints, `Host`/`Origin` checks.
- Endpoints: `GET /api/health`, `GET /api/capabilities`, `POST /api/media/select`, `GET/POST /api/projects`, `GET/PUT /api/projects/{id}`, `POST /api/projects/{id}/source/relink`, `POST /api/projects/{id}/segments/{seg_id}/tts`, `POST /api/projects/{id}/segments/{seg_id}/preview`, `POST /api/projects/{id}/export`, `GET /api/jobs/{job_id}`, `POST /api/jobs/{job_id}/cancel`, `GET /api/projects/{id}/media/{artifact_id}`, `GET/PUT /api/pronunciations`.
