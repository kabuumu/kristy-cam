# E2E Test Suite Ready (`TEST_READY.md`)

The opaque-box, requirement-driven End-to-End (E2E) test suite in [`e2e/`](file:///home/antigravity/kabuumu/kristy-cam/e2e) is complete, verified for collection (`78 tests collected`), and ready for the **Final Milestone** once Milestones `M0`–`M4` are finished.

See [`TEST_INFRA.md`](file:///home/antigravity/kabuumu/kristy-cam/TEST_INFRA.md) for the complete specification and `F1`–`F42` traceability matrix against [`PROJECT.md`](file:///home/antigravity/kabuumu/kristy-cam/PROJECT.md).

---

## 1. Exact Test Runner Commands

### Collection Check (Pre-Build / Standalone)
```bash
uv run --no-project --with pytest pytest --collect-only e2e/
```
*Verified baseline: `78 tests collected`.*

### Fixture Generator Self-Check (Runnable Anytime)
```bash
uv run --no-project --with pytest pytest e2e/test_fixtures_selfcheck.py -v
```
*Verified baseline: `10 passed` — validates all 5 valid and 5 invalid synthetic media fixtures generated on demand by [`media_gen.py`](file:///home/antigravity/kabuumu/kristy-cam/e2e/fixtures/media_gen.py).*

### Full E2E Suite (Once M0–M4 Are Complete)
```bash
# Run full E2E suite (API, media, and Playwright UI tests if Playwright is installed)
uv run --with pytest pytest e2e/ -v

# Run full E2E suite with Playwright explicitly included
uv run --with pytest --with playwright pytest e2e/ -v

# Optional: point Playwright to a system Chromium binary
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium uv run --with pytest --with playwright pytest e2e/ -v
```

### Per-Tier Commands
```bash
# Tier 1: Isolated Feature Happy-Path Coverage (35 tests)
uv run --with pytest pytest e2e/test_tier1_features.py -v

# Tier 2: Boundary, Error, Security & Corner Cases (21 collected items)
uv run --with pytest pytest e2e/test_tier2_boundaries.py -v

# Tier 3: Cross-Feature Pairwise & Multi-Feature Combinations (7 tests)
uv run --with pytest pytest e2e/test_tier3_combinations.py -v

# Tier 4: End-to-End Real-World Creator Workflows (5 tests, incl. Playwright Chromium)
uv run --with pytest --with playwright pytest e2e/test_tier4_workflows.py -v
```

---

## 2. Coverage Summary

### Test Distribution (78 Total Collected Tests Across 5 Modules)
| Module | Collected Tests | Scope |
|---|---|---|
| [`e2e/test_fixtures_selfcheck.py`](file:///home/antigravity/kabuumu/kristy-cam/e2e/test_fixtures_selfcheck.py) | **10** | Validates runtime `ffmpeg` generation of all 5 valid and 5 invalid media fixtures via `ffprobe` |
| [`e2e/test_tier1_features.py`](file:///home/antigravity/kabuumu/kristy-cam/e2e/test_tier1_features.py) | **35** | 5 happy-path tests per feature area across 7 feature areas (`M0`–`M4`) |
| [`e2e/test_tier2_boundaries.py`](file:///home/antigravity/kabuumu/kristy-cam/e2e/test_tier2_boundaries.py) | **21** | 16 boundary/security/recovery tests + 5 parametrized invalid-media rejection cases |
| [`e2e/test_tier3_combinations.py`](file:///home/antigravity/kabuumu/kristy-cam/e2e/test_tier3_combinations.py) | **7** | Multi-feature interactions (crop/zoompan + TTS + captions, hash reuse, EOF freeze, relink, portability) |
| [`e2e/test_tier4_workflows.py`](file:///home/antigravity/kabuumu/kristy-cam/e2e/test_tier4_workflows.py) | **5** | Full real-world creator workflows including Playwright Chromium end-to-end UI studio flow |
| **Total** | **78** | **100% feature coverage (`F1`–`F42`)** |

### Feature Traceability
- **100% Feature Coverage (`F1`–`F42`)**: Every requirement `F1` through `F42` in [`PROJECT.md`](file:///home/antigravity/kabuumu/kristy-cam/PROJECT.md) is mapped to concrete test functions in [`TEST_INFRA.md`](file:///home/antigravity/kabuumu/kristy-cam/TEST_INFRA.md).

### Opaque-Box Guarantees
1. **Zero Internal Imports**: Tests in `e2e/` never import from `backend/` or `frontend/src/`; all verification occurs strictly through public HTTP/REST APIs, filesystem outputs, media inspection, and browser UI.
2. **`SIMS_STUDIO_DATA_DIR` Temp Isolation**: Each test server instance runs in a dedicated temporary `SIMS_STUDIO_DATA_DIR` (`tmp_path`), preventing cross-test state leakage and verifying `project.json`, `project.json.bak`, `manifest.json`, `pronunciations.json`, `artifacts/`, `exports/`, and `tmp/`.
3. **`X-Session-Token` Bootstrap**: The ephemeral server harness (`e2e/conftest.py`) spawns `uvicorn backend.main:app` on `127.0.0.1:<ephemeral_port>`, waits for `GET /api/health`, extracts the per-launch `X-Session-Token` from `index.html` `<meta name="session-token">` (or `/api/health` bootstrap), and exercises `Host`/`Origin` and token security checks.
4. **Runtime `ffmpeg` Media Fixture Generation**: Zero binary `.mp4` blobs are committed to Git; [`e2e/fixtures/media_gen.py`](file:///home/antigravity/kabuumu/kristy-cam/e2e/fixtures/media_gen.py) synthesizes all valid (`1080p` CFR, `720p` CFR, VFR, silent, Unicode/space/apostrophe path) and invalid (`HDR PQ`, `rotate=90`, `4:3`, `SAR 4:3`, corrupt) fixtures on demand.
5. **`ffprobe` Output Verification**: Proxies, Stage 1 segment intermediates, previews, and final exports are verified with `ffprobe`/`ffmpeg` for `1080x1920`, `SAR 1:1`, `30fps` CFR, `yuv420p`, `48000 Hz` stereo AAC, `+faststart` moov atom, exact frame counts, and A/V duration alignment.
6. **Playwright Chromium Browser Verification**: Studio UI layout (`CropCanvas`, `ScriptPanel`, `Timeline`, `JobStatusBar`), keyboard-shortcut focus isolation, media playback, and real-time job polling are verified in headless Chromium against the live server serving `frontend/dist/`.
