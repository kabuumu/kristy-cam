# Test Infrastructure Specification: Sims 4 Vertical Video Studio (V1)

This document defines the **Opaque-Box, Requirement-Driven End-to-End (E2E) Testing Infrastructure** for **Sims 4 Vertical Video Studio (V1)**. It maps every requirement (`F1`–`F42`) in [`PROJECT.md`](file:///home/antigravity/kabuumu/kristy-cam/PROJECT.md) to concrete, verifiable E2E test cases in `e2e/` across four testing tiers.

---

## 1. Testing Philosophy & Architecture

### 1.1 Opaque-Box Principles
- **Zero Internal Imports:** Tests in `e2e/` MUST NOT import modules from `backend/` or `frontend/src/`. All assertions operate strictly through public interfaces:
  1. **HTTP/REST API & Security Layer** (`127.0.0.1` loopback server, `X-Session-Token`, `Host`/`Origin` validation, HTTP `200`/`202`/`206`/`401`/`403`/`409`/`416`/`422` status codes).
  2. **Process & Filesystem Isolation (`SIMS_STUDIO_DATA_DIR`)**: Every test session or isolated test case runs with a dedicated temporary `SIMS_STUDIO_DATA_DIR`, ensuring zero cross-test state leakage and verifying on-disk `project.json`, `project.json.bak`, `manifest.json`, `pronunciations.json`, `artifacts/`, `exports/`, and `tmp/`.
  3. **Media Output Inspection (`ffprobe` / `ffmpeg`)**: Rendered previews, proxies, segment intermediates, and final exports are inspected with `ffprobe` and `ffmpeg` (stream geometry `1080x1920`, `SAR 1:1`, `30fps` CFR, `yuv420p`, `48000 Hz` stereo AAC, `+faststart` moov atom, exact frame counts, A/V duration delta `<= 1/30s`, and pixel/frame-diff checks for crop/zoom/burn-in).
  4. **Headless Browser UI Verification (Playwright Chromium)**: End-to-end user interactions (`CropCanvas`, `ScriptPanel`, `Timeline`, `JobStatusBar`), keyboard-shortcut focus isolation, media playback, and real-time job polling are verified in headless Chromium against the live FastAPI server serving `frontend/dist/`.

### 1.2 Ephemeral Server & Session Harness (`e2e/conftest.py`)
- **Server Lifecycle:** Spawns `uvicorn backend.main:app --host 127.0.0.1 --port <ephemeral_port>` as a subprocess with `SIMS_STUDIO_DATA_DIR` set to a fresh `tmp_path`. Waits for `GET /api/health` to return `200 OK`.
- **Session Token Bootstrap:** Extracts the per-launch `X-Session-Token` from the served `index.html` `<meta name="session-token">` (or `/api/health` bootstrap payload) and injects it into API client fixtures.
- **Job Polling Helper:** Submits `202 Accepted` async jobs (`proxy`, `tts`, `preview`, `export`) and polls `GET /api/jobs/{job_id}` until a terminal state (`succeeded`, `failed`, `cancelled`, `interrupted`) is reached.
- **Headless Media Selection:** Exercises the headless/automated fallback on `POST /api/media/select` (local path registration or multipart upload) so OS-native GUI dialogs (`osascript` / PowerShell) do not block automated runs.

### 1.3 Runtime Synthetic Media Generation
- **No Binary Blobs in Git:** Large `.mp4` test assets are never committed to version control.
- **`e2e/fixtures/media_gen.py`:** Uses `ffmpeg` at test-session setup to deterministically synthesize all valid and invalid media fixtures into a session-scoped temp directory, caching them for the duration of the pytest run.

### 1.4 Environment Prerequisites & Graceful Handling
- **Running without full app dependencies (collection / fixture self-check):**
  - `uv run --no-project --with pytest pytest --collect-only e2e/` (or `uv run pytest --collect-only e2e/` once `pyproject.toml` is synced).
  - `ffmpeg` and `ffprobe` must be on `PATH` (or resolved via `imageio-ffmpeg` / `static-ffmpeg` / `SIMS_STUDIO_DATA_DIR/bin`).
- **Running full E2E:**
  - `uv run pytest e2e/` executes all tiers against the built backend and `frontend/dist/`.
- **Playwright / Chromium Handling:**
  - Browser UI tests check for `playwright` and a Chromium executable (`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` or default Playwright browser cache). If Playwright or Chromium is unavailable in a minimal headless environment, UI tests emit an explicit `pytest.skip("Playwright Chromium not available")` rather than crashing non-UI API/media tiers.

---

## 2. Synthetic Media Fixture Matrix (`e2e/fixtures/media_gen.py`)

All fixtures are generated on demand by `e2e/fixtures/media_gen.py` using `ffmpeg` and verified by `e2e/test_fixtures_selfcheck.py`.

### 2.1 Valid Source Fixtures

| Fixture Filename | Resolution & Aspect | Frame Rate & Pixel Format | Color Space | Audio Stream | Purpose |
|---|---|---|---|---|---|
| `sdr_1080p_cfr.mp4` | `1920x1080` (`16:9`, `SAR 1:1`) | `30fps` CFR, `yuv420p` | SDR (`bt709`) | Stereo `48kHz` AAC (`sine` tone) | Primary baseline source with color-coded quadrant/grid pattern for crop/zoom pixel verification |
| `sdr_720p_cfr.mp4` | `1280x720` (`16:9`, `SAR 1:1`) | `30fps` CFR, `yuv420p` | SDR (`bt709`) | Stereo `48kHz` AAC | Non-1080p `16:9` scaling & even-pixel rounding verification |
| `sdr_vfr.mp4` | `1920x1080` (`16:9`, `SAR 1:1`) | Variable Frame Rate (VFR), `yuv420p` | SDR (`bt709`) | Stereo `48kHz` AAC | Verifies CFR normalization (`30fps`) in proxy & Stage 1 renderer |
| `sdr_silent.mp4` | `1920x1080` (`16:9`, `SAR 1:1`) | `30fps` CFR, `yuv420p` | SDR (`bt709`) | **None** (video-only) | Verifies missing source audio is accepted and synthesized as silence in `amix` |
| `Sims 4's Café — Ep 12 (テスト).mp4` | `1920x1080` (`16:9`, `SAR 1:1`) | `30fps` CFR, `yuv420p` | SDR (`bt709`) | Stereo `48kHz` AAC | Verifies spaces, apostrophes, em-dashes, and non-ASCII Unicode paths across FFmpeg, `filter_complex_script`, and `.ass` |

### 2.2 Invalid Source Fixtures (Expected Rejection on Import / Relink)

| Fixture Filename | Defect / Violation | Expected Outcome (`F27`) |
|---|---|---|
| `invalid_hdr_pq.mp4` | HDR10 / PQ transfer characteristics (`smpte2084`, `bt2020nc`, `yuv420p10le`) | Rejected with actionable HTTP `422`/`400` error citing HDR/transfer characteristics |
| `invalid_rotated_90.mp4` | `1920x1080` with `rotate=90` display matrix / metadata tag | Rejected with actionable error citing non-zero rotation metadata |
| `invalid_aspect_4_3.mp4` | `1440x1080` (`4:3` aspect ratio, outside `1.77–1.78`) | Rejected with actionable error citing non-`16:9` aspect ratio |
| `invalid_sar_anamorphic.mp4` | `1440x1080` with `SAR 4:3` (`DAR 16:9`, non-square pixels) | Rejected with actionable error citing `SAR != 1:1` |
| `invalid_corrupt.mp4` | Truncated / random non-media bytes | Rejected with actionable error citing unreadable/corrupt media |

---

## 3. Complete Feature-to-Test Traceability Matrix (`F1`–`F42`)

Every feature `F1` through `F42` defined in [`PROJECT.md`](file:///home/antigravity/kabuumu/kristy-cam/PROJECT.md) is mapped across the four test tiers below (**78 total collected pytest items** across `e2e/`, including 10 fixture self-checks):

- **Fixture Self-Check (`e2e/test_fixtures_selfcheck.py`)**: **10 tests** verifying synthetic valid and invalid media fixtures.
- **Tier 1 (`e2e/test_tier1_features.py`)**: **35 tests** — 5 isolated happy-path tests per feature area across 7 feature areas.
- **Tier 2 (`e2e/test_tier2_boundaries.py`)**: **21 collected items** — 16 test functions + 5 parametrized `test_invalid_media_rejected_with_actionable_error[...]` cases.
- **Tier 3 (`e2e/test_tier3_combinations.py`)**: **7 tests** — pairwise and multi-feature interaction coverage.
- **Tier 4 (`e2e/test_tier4_workflows.py`)**: **5 tests** — end-to-end real-world creator workflows (including headless Chromium UI).

### 3.1 Master Traceability Table (`F1`–`F42`)

| ID | Feature (from `PROJECT.md`) | Milestone | Tier 1 (Features) | Tier 2 (Boundaries) | Tier 3 (Combinations) | Tier 4 (Workflows) |
|---|---|---|---|---|---|---|
| **F1** | Cross-platform `uv` dependency lock | M0 | `test_f1_pyproject_and_uv_lock_cross_platform` | — | — | — |
| **F2** | Runtime bootstrap & verification | M0 | `test_f2_f8_bootstrap_and_data_dir_layout` | — | — | — |
| **F3** | M0 smoke test script (`verify_env.py`) | M0 | `test_f3_verify_env_smoke_script_runs_offline` | — | — | — |
| **F4** | Windows launcher (`start.bat`) | M0 | `test_f4_f5_launchers_have_uv_sync_bootstrap_and_health_guard` | — | — | — |
| **F5** | macOS launcher (`start.sh`) | M0 | `test_f4_f5_launchers_have_uv_sync_bootstrap_and_health_guard` | — | — | — |
| **F6** | Distributable release archive | M4 | `test_f6_assets_manifest_notices_and_bundled_font` | — | — | — |
| **F7** | Pydantic v2 data models (`schemas.py`) | M1 | `test_f7_project_input_and_manifest_separation` | — | `test_combo_segment_reorder_preserves_stage1_render_hashes` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F8** | Per-user data directory layout | M0 | `test_f2_f8_bootstrap_and_data_dir_layout` | — | — | `test_workflow_3_silent_source_save_restart_and_export` |
| **F9** | Atomic JSON saves & `.bak` recovery | M1 | `test_f9_atomic_json_save_creates_bak_file` | — | — | `test_workflow_3_silent_source_save_restart_and_export` |
| **F10** | Optimistic revision locking (`HTTP 409`) | M1 | `test_f10_optimistic_revision_increments_on_put` | `test_http_409_conflict_on_stale_revision` | — | `test_workflow_4_iterative_caption_and_speed_edits_with_range_playback` |
| **F11** | Schema version guard | M1 | `test_f7_project_input_and_manifest_separation` | `test_schema_version_greater_than_supported_rejected_without_overwrite` | — | — |
| **F12** | Global & project-owned pronunciations | M1 | `test_f12_global_and_project_owned_pronunciations_isolation` | — | `test_combo_pronunciation_override_speed_and_tts_hash_invalidation` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F13** | Source portability & fingerprinting | M1 | `test_f13_source_fingerprint_copy_to_project_and_relink` | — | `test_combo_relink_and_export`, `test_combo_copied_to_project_portability_survives_source_deletion` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F14** | Normalized top-left crop coordinates | M1 | `test_f14_f15_normalized_top_left_crop_keyframes_persist` | `test_zoom_bounds_1_0_and_3_0_accepted_and_out_of_range_rejected` | `test_combo_crop_zoompan_tts_and_captions_export` | `test_workflow_5_playwright_browser_studio_end_to_end` |
| **F15** | Bounds clamping & validation | M1 | `test_f14_f15_normalized_top_left_crop_keyframes_persist` | `test_out_of_bounds_crop_coordinates_and_missing_t0_rejected` | — | — |
| **F16** | Linear interpolation & hold | M1 | `test_f16_f18_static_pan_crop_renders_1080x1920_sar_1`, `test_f16_f18_animated_zoompan_renders_exact_30fps_frames` | `test_source_eof_freeze_frame_extension` | `test_combo_fit_narration_freeze_and_captions` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F17** | Trim-shift keyframe detection | M1 | `test_f17_trim_shift_out_of_range_keyframes_flagged_or_detected` | `test_trim_shift_detection_preserves_keyframes`, `test_ui_trim_shift_prompt_displayed` | — | — |
| **F18** | Even pixel rounding & FFmpeg transform | M1 | `test_f16_f18_static_pan_crop_renders_1080x1920_sar_1`, `test_f18_constant_zoom_2x_renders_cleanly`, `test_f16_f18_animated_zoompan_renders_exact_30fps_frames` | — | `test_combo_crop_zoompan_tts_and_captions_export` | `test_workflow_2_vfr_720p_gameplay_to_cfr_vertical_short` |
| **F19** | Dynamic Kokoro voice discovery | M1 | `test_f19_capabilities_discovers_kokoro_voices` | — | — | `test_workflow_5_playwright_browser_studio_end_to_end` |
| **F20** | Paired clauses & speed control | M1 | `test_f20_f22_synthesize_paired_clauses_24khz_pcm_and_timings` | — | `test_combo_pronunciation_override_speed_and_tts_hash_invalidation` | `test_workflow_4_iterative_caption_and_speed_edits_with_range_playback` |
| **F21** | Non-recursive pronunciation replacer | M1 | `test_f21_pronunciation_replacement_and_spoken_override` | `test_pronunciation_unicode_boundary_and_longest_first` | `test_combo_pronunciation_override_speed_and_tts_hash_invalidation` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F22** | CPU Kokoro singleton & sample-accurate WAVs | M1 | `test_f20_f22_synthesize_paired_clauses_24khz_pcm_and_timings` | — | `test_combo_crop_zoompan_tts_and_captions_export` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F23** | Empty narration & `tts_hash` staleness | M1 | `test_f23_empty_narration_segment_valid_zero_duration` | `test_empty_narration_exports_valid_silent_audio_track` | `test_combo_pronunciation_override_speed_and_tts_hash_invalidation` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F24** | Clause-level ASS timing & continuity | M1 | `test_f24_f25_ass_burnin_with_bundled_font_succeeds` | — | `test_combo_fit_narration_freeze_and_captions` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F25** | ASS styling, bundled font & escaping | M1 | `test_f24_f25_ass_burnin_with_bundled_font_succeeds` | `test_ass_special_character_escaping_in_captions` | `test_combo_crop_zoompan_tts_and_captions_export` | `test_workflow_4_iterative_caption_and_speed_edits_with_range_playback` |
| **F26** | OS-safe native file picker & fallback | M2 | `test_f26_f27_headless_media_select_and_probe` | `test_unicode_space_apostrophe_source_path_end_to_end` | `test_combo_copied_to_project_portability_survives_source_deletion` | `test_workflow_5_playwright_browser_studio_end_to_end` |
| **F27** | `ffprobe` input media validation | M2 | `test_f26_f27_headless_media_select_and_probe` | `test_invalid_media_rejected_with_actionable_error[hdr_pq\|rotated_90\|non_16_9\|anamorphic_sar\|corrupt]`, `test_missing_audio_silent_source_and_vfr_accepted` | `test_combo_relink_and_export`, `test_combo_silent_source_fit_narration_freeze_and_captions` | `test_workflow_2_vfr_720p_gameplay_to_cfr_vertical_short` |
| **F28** | 540p 30fps CFR proxy transcoder | M2 | `test_f28_proxy_transcoder_540p_30fps_cfr_faststart` | — | — | `test_workflow_2_vfr_720p_gameplay_to_cfr_vertical_short` |
| **F29** | Bounded async job queue & recovery | M2 | `test_f28_proxy_transcoder_540p_30fps_cfr_faststart`, `test_f32_f33_two_stage_render_creates_stage1_and_export` | `test_job_cancellation_and_server_restart_interrupted_recovery` | — | `test_workflow_3_silent_source_save_restart_and_export` |
| **F30** | Duration reconciliation modes | M2 | `test_f30_f31_preserve_video_and_fit_narration_quantized_30fps` | `test_empty_narration_exports_valid_silent_audio_track`, `test_source_eof_freeze_frame_extension` | `test_combo_fit_narration_freeze_and_captions`, `test_combo_silent_source_fit_narration_freeze_and_captions` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F31** | Upward frame quantization & audio mix | M2 | `test_f30_f31_preserve_video_and_fit_narration_quantized_30fps` | `test_missing_audio_silent_source_and_vfr_accepted` | `test_combo_fit_narration_freeze_and_captions`, `test_combo_silent_source_fit_narration_freeze_and_captions` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F32** | Stage 1 caption-free segment renderer | M2 | `test_f32_f33_two_stage_render_creates_stage1_and_export` | `test_unicode_space_apostrophe_source_path_end_to_end` | `test_combo_crop_zoompan_tts_and_captions_export` | `test_workflow_4_iterative_caption_and_speed_edits_with_range_playback` |
| **F33** | Stage 2 concat & ASS burn-in | M2 | `test_f32_f33_two_stage_render_creates_stage1_and_export` | `test_unicode_space_apostrophe_source_path_end_to_end` | `test_combo_relink_and_export`, `test_combo_copied_to_project_portability_survives_source_deletion` | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F34** | Single-segment captioned preview | M2 | `test_f34_f35_single_segment_preview_and_hash_cache_reuse` | — | — | `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity` |
| **F35** | Content-addressed cache invalidation | M2 | `test_f34_f35_single_segment_preview_and_hash_cache_reuse` | — | `test_combo_segment_reorder_preserves_stage1_render_hashes`, `test_combo_pronunciation_override_speed_and_tts_hash_invalidation` | `test_workflow_4_iterative_caption_and_speed_edits_with_range_playback` |
| **F36** | Loopback server & security middleware | M3 | `test_f36_health_and_session_token_meta_in_index`, `test_f36_no_wildcard_cors_on_responses` | `test_http_401_403_session_token_host_origin_and_path_traversal` | — | `test_workflow_5_playwright_browser_studio_end_to_end` |
| **F37** | REST endpoints (14 route entries) | M3 | `test_f37_projects_list_and_pronunciations_rest_endpoints` | `test_http_409_conflict_on_stale_revision` | `test_combo_relink_and_export` | `test_workflow_5_playwright_browser_studio_end_to_end` |
| **F38** | HTTP Range media streaming & static UI | M3 | `test_f38_http_206_range_streaming_for_proxy_mp4`, `test_f38_static_frontend_dist_served` | `test_http_416_unsatisfiable_range` | — | `test_workflow_4_iterative_caption_and_speed_edits_with_range_playback` |
| **F39** | Studio shell, API client & types | M3 | `test_f39_studio_ui_loads_dark_three_zone_layout`, `test_f39_job_status_bar_visible_in_studio` | — | — | `test_workflow_5_playwright_browser_studio_end_to_end` |
| **F40** | Crop & Preview Stage (`CropCanvas.tsx`) | M3 | `test_f40_crop_canvas_displays_proxy_and_safe_area` | `test_ui_trim_shift_prompt_displayed` | — | `test_workflow_5_playwright_browser_studio_end_to_end` |
| **F41** | Narration & Pronunciation Inspector (`ScriptPanel.tsx`) | M3 | `test_f41_script_panel_renders_voice_and_clause_controls` | — | — | `test_workflow_5_playwright_browser_studio_end_to_end` |
| **F42** | Storyboard Timeline (`Timeline.tsx`) | M3 | `test_f42_timeline_renders_add_segment_and_io_controls` | `test_keyboard_shortcut_isolation_in_inputs` | — | `test_workflow_5_playwright_browser_studio_end_to_end` |

---

### 3.2 Tier 1: Feature Coverage (`e2e/test_tier1_features.py` — 35 tests)

Organized into 7 feature areas (5 isolated happy-path tests per area):

1. **Environment, Bootstrap, Launchers & Assets (`F1`–`F6`, `F8` — 5 tests):**
   - `test_f1_pyproject_and_uv_lock_cross_platform`
   - `test_f2_f8_bootstrap_and_data_dir_layout`
   - `test_f3_verify_env_smoke_script_runs_offline`
   - `test_f4_f5_launchers_have_uv_sync_bootstrap_and_health_guard`
   - `test_f6_assets_manifest_notices_and_bundled_font`
2. **Persistence, Schemas, Pronunciations & Portability (`F7`, `F9`–`F13` — 5 tests):**
   - `test_f7_project_input_and_manifest_separation`
   - `test_f9_atomic_json_save_creates_bak_file`
   - `test_f10_optimistic_revision_increments_on_put`
   - `test_f12_global_and_project_owned_pronunciations_isolation`
   - `test_f13_source_fingerprint_copy_to_project_and_relink`
3. **Geometry & Crop (`F14`–`F18` — 5 tests):**
   - `test_f14_f15_normalized_top_left_crop_keyframes_persist`
   - `test_f16_f18_static_pan_crop_renders_1080x1920_sar_1`
   - `test_f18_constant_zoom_2x_renders_cleanly`
   - `test_f16_f18_animated_zoompan_renders_exact_30fps_frames`
   - `test_f17_trim_shift_out_of_range_keyframes_flagged_or_detected`
4. **TTS & Subtitles (`F19`–`F25` — 5 tests):**
   - `test_f19_capabilities_discovers_kokoro_voices`
   - `test_f20_f22_synthesize_paired_clauses_24khz_pcm_and_timings`
   - `test_f21_pronunciation_replacement_and_spoken_override`
   - `test_f23_empty_narration_segment_valid_zero_duration`
   - `test_f24_f25_ass_burnin_with_bundled_font_succeeds`
5. **Media, Proxy, Duration & Two-Stage Renderer (`F26`–`F35` — 5 tests):**
   - `test_f26_f27_headless_media_select_and_probe`
   - `test_f28_proxy_transcoder_540p_30fps_cfr_faststart`
   - `test_f30_f31_preserve_video_and_fit_narration_quantized_30fps`
   - `test_f32_f33_two_stage_render_creates_stage1_and_export`
   - `test_f34_f35_single_segment_preview_and_hash_cache_reuse`
6. **Security, REST & Range Streaming (`F36`–`F38` — 5 tests):**
   - `test_f36_health_and_session_token_meta_in_index`
   - `test_f37_projects_list_and_pronunciations_rest_endpoints`
   - `test_f38_http_206_range_streaming_for_proxy_mp4`
   - `test_f38_static_frontend_dist_served`
   - `test_f36_no_wildcard_cors_on_responses`
7. **Studio UI (`F39`–`F42` — 5 tests):**
   - `test_f39_studio_ui_loads_dark_three_zone_layout`
   - `test_f40_crop_canvas_displays_proxy_and_safe_area`
   - `test_f41_script_panel_renders_voice_and_clause_controls`
   - `test_f42_timeline_renders_add_segment_and_io_controls`
   - `test_f39_job_status_bar_visible_in_studio`

---

### 3.3 Tier 2: Boundary & Corner Cases (`e2e/test_tier2_boundaries.py` — 21 collected items)

16 test functions + 5 parametrized `test_invalid_media_rejected_with_actionable_error[...]` cases:

- `test_zoom_bounds_1_0_and_3_0_accepted_and_out_of_range_rejected`: Validates `zoom` bounds `[1.0, 3.0]` clamping/acceptance and rejection of out-of-range zoom (`F14`).
- `test_out_of_bounds_crop_coordinates_and_missing_t0_rejected`: Rejects out-of-bounds persisted crop coordinates and keyframes missing `t = 0.0` (`F15`).
- `test_trim_shift_detection_preserves_keyframes`: Shortening `video_out` flags out-of-range keyframes without silently discarding them (`F17`).
- `test_empty_narration_exports_valid_silent_audio_track`: Segment with `clauses: []` produces `audio_duration = 0.0` and exports a valid `48kHz` stereo AAC silent track (`F23`, `F30`).
- `test_source_eof_freeze_frame_extension`: `fit_narration` past source EOF clones final frame via `tpad=stop_mode=clone` without looping game audio (`F16`, `F30`).
- `test_ass_special_character_escaping_in_captions`: Escapes `\`, `{`, `}`, and `\n` in `display_text` without ASS tag injection (`F25`).
- `test_http_409_conflict_on_stale_revision`: Returns `HTTP 409 Conflict` on stale `expected_revision` (`F10`, `F37`).
- `test_http_416_unsatisfiable_range`: Returns `HTTP 416 Range Not Satisfiable` on out-of-bounds `Range` header (`F38`).
- `test_http_401_403_session_token_host_origin_and_path_traversal`: Rejects missing/invalid `X-Session-Token`, invalid `Host`/`Origin`, and `..`/symlink path traversal (`F36`).
- `test_invalid_media_rejected_with_actionable_error[hdr_pq]`: Rejects HDR PQ media (`F27`).
- `test_invalid_media_rejected_with_actionable_error[rotated_90]`: Rejects `rotate=90` media (`F27`).
- `test_invalid_media_rejected_with_actionable_error[non_16_9]`: Rejects `4:3` non-`16:9` media (`F27`).
- `test_invalid_media_rejected_with_actionable_error[anamorphic_sar]`: Rejects `SAR != 1:1` anamorphic media (`F27`).
- `test_invalid_media_rejected_with_actionable_error[corrupt]`: Rejects corrupt/unreadable media (`F27`).
- `test_missing_audio_silent_source_and_vfr_accepted`: Accepts `sdr_silent.mp4` (no audio) and `sdr_vfr.mp4` (VFR) (`F27`, `F31`).
- `test_unicode_space_apostrophe_source_path_end_to_end`: `Sims 4's Café — Ep 12 (テスト).mp4` imports, proxies, renders, and exports cleanly (`F26`, `F32`, `F33`).
- `test_schema_version_greater_than_supported_rejected_without_overwrite`: Rejects `project.json` with `schema_version > 1` without overwriting (`F11`).
- `test_job_cancellation_and_server_restart_interrupted_recovery`: Cancelling an active job transitions to `cancelled`; restarting server with `running`/`queued` job marks it `interrupted` and purges `tmp/` while preserving `artifacts/` (`F29`).
- `test_keyboard_shortcut_isolation_in_inputs`: Pressing `I` or `O` inside `<input>`/`<textarea>` types into the field and does not trigger timeline slicing (`F42`).
- `test_pronunciation_unicode_boundary_and_longest_first`: Unicode word boundaries and longest-key-first single non-recursive pass (`F21`).
- `test_ui_trim_shift_prompt_displayed`: UI surfaces trim-shift prompt when out-of-range keyframes exist (`F17`, `F40`).

---

### 3.4 Tier 3 & Tier 4: Combinations & Real-World Workflows

#### Tier 3: Cross-Feature Combinations (`e2e/test_tier3_combinations.py` — 7 tests)
1. `test_combo_crop_zoompan_tts_and_captions_export`: Animated crop/zoompan + Kokoro TTS + `.ass` burn-in produces `1080x1920` `30fps` CFR MP4 with burned-in text.
2. `test_combo_segment_reorder_preserves_stage1_render_hashes`: Reordering segments invalidates Stage 2 only while Stage 1 `render_hash` and `seg_*` files remain valid and reused.
3. `test_combo_fit_narration_freeze_and_captions`: `fit_narration` with long narration past source EOF triggers freeze-frame extension while captions end strictly at final clause audio end.
4. `test_combo_relink_and_export`: Missing source triggers missing-source state -> relink valid recording -> export succeeds.
5. `test_combo_pronunciation_override_speed_and_tts_hash_invalidation`: Project pronunciation dictionary + per-clause `spoken_text_override` + `speed=1.25` + `tts_hash` invalidation + export.
6. `test_combo_silent_source_fit_narration_freeze_and_captions`: `sdr_silent.mp4` + `fit_narration` + EOF freeze + captions exports valid `48kHz` stereo AAC audio track.
7. `test_combo_copied_to_project_portability_survives_source_deletion`: Import with `copied_to_project=true`, delete original external file, project still renders and exports.

#### Tier 4: Real-World Application Scenarios (`e2e/test_tier4_workflows.py` — 5 tests)
1. `test_workflow_1_full_multi_segment_sims_story_export_and_source_integrity`: 3-segment Sims story combining `preserve_video`, `fit_narration` (with EOF freeze), and an empty-narration gameplay interlude; verifies segment preview, reorder, export (`1080x1920` `30fps` CFR, `48kHz` stereo AAC, `+faststart`), zero cumulative A/V drift, and intact original source SHA-256.
2. `test_workflow_2_vfr_720p_gameplay_to_cfr_vertical_short`: Imports `720p` and `VFR` gameplay sources, verifies `540p` `30fps` CFR proxy and `1080x1920` `30fps` CFR vertical short export.
3. `test_workflow_3_silent_source_save_restart_and_export`: Fresh `SIMS_STUDIO_DATA_DIR` with silent source -> atomic `.bak` save -> server restart & `interrupted` job recovery -> clean re-export to completion.
4. `test_workflow_4_iterative_caption_and_speed_edits_with_range_playback`: Iterative narration/caption/speed edits, `HTTP 409` stale-revision conflict handling, Stage 1/TTS hash updates, export, and `HTTP 206` Range playback.
5. `test_workflow_5_playwright_browser_studio_end_to_end`: Full Playwright Chromium studio workflow (`import -> proxy ready -> slice I/O -> add crop keyframes -> write clauses & pronunciation -> synthesize TTS -> render segment preview -> reorder -> save -> reload page -> export MP4`).

---

## 4. Verification & Execution Commands

### 4.1 Test Collection Check (78 Collected Items)
```bash
# Using uv without project install (or with project env once synced)
uv run --no-project --with pytest pytest --collect-only e2e/

# Or inside the synced project environment
uv run pytest --collect-only e2e/
```
Expected collection breakdown (**78 tests total**):
- `e2e/test_fixtures_selfcheck.py`: **10 tests**
- `e2e/test_tier1_features.py`: **35 tests**
- `e2e/test_tier2_boundaries.py`: **21 tests** (16 functions + 5 parametrized `test_invalid_media_rejected_with_actionable_error[...]` items)
- `e2e/test_tier3_combinations.py`: **7 tests**
- `e2e/test_tier4_workflows.py`: **5 tests**

### 4.2 Synthetic Fixture Self-Check (`e2e/test_fixtures_selfcheck.py` — 10 tests)
Verifies `e2e/fixtures/media_gen.py` generates all 5 valid and 5 invalid media fixtures with expected `ffprobe` properties:
- `test_sdr_cfr_1080p_with_audio`
- `test_sdr_cfr_720p`
- `test_sdr_vfr_16_9`
- `test_silent_mp4_has_no_audio`
- `test_hdr_pq_mp4`
- `test_rotated_90_mp4`
- `test_non_16_9_4_3_mp4`
- `test_anamorphic_sar_mp4`
- `test_corrupt_mp4_fails_ffprobe`
- `test_unicode_space_apostrophe_path`

```bash
uv run --no-project --with pytest pytest e2e/test_fixtures_selfcheck.py -v
```

### 4.3 Running the Full E2E Suite (After M0–M4 App Build)
```bash
# Run all E2E tiers (78 tests total)
uv run pytest e2e/ -v

# Run individual tiers
uv run pytest e2e/test_tier1_features.py -v
uv run pytest e2e/test_tier2_boundaries.py -v
uv run pytest e2e/test_tier3_combinations.py -v
uv run pytest e2e/test_tier4_workflows.py -v

# Optional: specify system Chromium for Playwright UI tests
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium uv run pytest e2e/test_tier4_workflows.py -v
```
