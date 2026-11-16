"""Tier 5 white-box adversarial hardening tests for Sims 4 Vertical Video Studio (V1)."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.main as main_module
from backend.bootstrap import ensure_kokoro_models, verify_ffmpeg, verify_font
from backend.geometry import (
    COORD_EPSILON,
    GeometryValidationError,
    _piecewise_linear_expr,
    build_crop_filter,
    compute_ideal_crop_size,
    evaluate_crop_at,
    validate_keyframes,
)
from backend.jobs import JobContext, JobManager
from backend.media import (
    MediaValidationError,
    _check_rotation_zero,
    _check_square_pixels,
    _parse_fraction,
    import_uploaded_media,
)
from backend.renderer import (
    RenderError,
    compute_preview_hash,
    compute_render_hash,
    plan_segment_duration,
    probe_rendered_video,
    render_segment_stage1,
)
from backend.schemas import (
    CaptionSettings,
    ClauseInput,
    ClauseTiming,
    CropKeyframe,
    OutputSettings,
    ProjectInput,
    ProxyState,
    SegmentArtifactState,
    SegmentInput,
    SourceMediaRef,
    SourceMetadata,
)
from backend.storage import (
    FINGERPRINT_CHUNK_BYTES,
    StorageError,
    UnsupportedSchemaVersionError,
    atomic_write_json,
    compute_file_fingerprint,
    create_project,
    get_project_dir,
    load_global_pronunciations,
    load_manifest,
    load_project,
    save_manifest,
    save_project,
)
from backend.subtitles import (
    build_segment_subtitle_events,
    format_ass_timestamp,
    render_ass_content,
)
from backend.tts import (
    SAMPLE_RATE,
    SegmentTTSResult,
    _float_to_pcm16,
    apply_pronunciations,
    compute_tts_hash,
    synthesize_segment_tts,
)
from scripts.package_release import assemble_release_archive, verify_release_archive


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------


def _default_models_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local_app_data) if local_app_data else (Path.home() / "AppData" / "Local")
        return root / "SimsVerticalStudio" / "models"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SimsVerticalStudio" / "models"
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(xdg_data) if xdg_data else (Path.home() / ".local" / "share")
    return root / "SimsVerticalStudio" / "models"


def _seed_models_into_data_dir(data_dir: Path) -> None:
    models_dir = data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    src_models = _default_models_dir()
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        src = src_models / name
        dst = models_dir / name
        if src.is_file() and not dst.exists():
            dst.symlink_to(src)


@pytest.fixture()
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an isolated SIMS_STUDIO_DATA_DIR with symlinked Kokoro models."""
    data_dir = tmp_path / "studio_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _seed_models_into_data_dir(data_dir)
    monkeypatch.setenv("SIMS_STUDIO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIMS_STUDIO_OFFLINE", "1")
    return data_dir


@pytest.fixture()
def client(isolated_data_dir: Path) -> TestClient:
    """FastAPI TestClient with lifespan initialized on an isolated data dir."""
    with TestClient(main_module.app) as tc:
        yield tc


def _auth_headers(tc: TestClient) -> dict[str, str]:
    return {"X-Session-Token": tc.app.state.session_token}


def _poll_job(tc: TestClient, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = tc.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        last_data = resp.json()
        if last_data["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return last_data
        time.sleep(0.03)
    raise AssertionError(f"Timed out waiting for job {job_id}: last state={last_data}")


# ---------------------------------------------------------------------------
# Part 1: Challenger 1 White-Box Core, Geometry, TTS, Subtitles, Storage & Media Tests
# ---------------------------------------------------------------------------


def test_tier5_schemas_non_finite_and_duplicate_id_guards() -> None:
    """Verify non-finite float rejection across OutputSettings, CaptionSettings, CropKeyframe, and duplicate ID guards."""
    for bad_float in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError):
            OutputSettings(default_source_volume=bad_float)
        with pytest.raises(ValidationError):
            OutputSettings(tail_padding_sec=bad_float)
        with pytest.raises(ValidationError):
            CaptionSettings(outline_width=bad_float)
        with pytest.raises(ValidationError):
            CropKeyframe(t=0.0, x=bad_float, y=0.0, zoom=1.0)

    with pytest.raises(ValidationError, match="Clause IDs within a segment must be unique"):
        SegmentInput(
            id="seg_1",
            video_in=0.0,
            video_out=2.0,
            crop_keyframes=[CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0)],
            clauses=[
                ClauseInput(id="dup_c", display_text="One"),
                ClauseInput(id="dup_c", display_text="Two"),
            ],
        )

    seg = SegmentInput(
        id="dup_seg",
        video_in=0.0,
        video_out=2.0,
        crop_keyframes=[CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0)],
    )
    with pytest.raises(ValidationError, match="Segment IDs within a project must be unique"):
        ProjectInput(
            project_id="proj_1",
            title="Dup Test",
            source_media=SourceMediaRef(media_id="m1", path="/tmp/a.mp4", fingerprint="sha256:1"),
            segments=[seg, seg],
        )


def test_tier5_geometry_multi_keyframe_interpolation_clamping_and_expr() -> None:
    """Stress-test 3+ keyframe piecewise-linear expressions, mid-interpolation bounds clamping, and epsilon boundaries."""
    w, h = 1920, 1080
    iw3, ih3 = compute_ideal_crop_size(w, h, 3.0)
    max_x3 = (w - iw3) / w
    max_y3 = (h - ih3) / h

    kfs = [
        CropKeyframe(t=0.0, x=max_x3, y=max_y3, zoom=3.0),
        CropKeyframe(t=1.0, x=max_x3 * 0.5, y=max_y3 * 0.5, zoom=2.0),
        CropKeyframe(t=2.5, x=0.0, y=0.0, zoom=1.0),
    ]
    validate_keyframes(kfs, duration=2.5, width=w, height=h)

    kf_eps_ok = [
        CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0),
        CropKeyframe(t=2.5 + COORD_EPSILON * 0.5, x=0.0, y=0.0, zoom=1.0),
    ]
    validate_keyframes(kf_eps_ok, duration=2.5, width=w, height=h)

    kf_eps_bad = [
        CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0),
        CropKeyframe(t=2.5 + COORD_EPSILON * 2.0, x=0.0, y=0.0, zoom=1.0),
    ]
    with pytest.raises(GeometryValidationError, match="exceeds segment duration"):
        validate_keyframes(kf_eps_bad, duration=2.5, width=w, height=h)

    for step in range(-5, 35):
        t = step * 0.1
        box = evaluate_crop_at(kfs, t=t, width=w, height=h)
        assert 0.0 <= box.x_px <= w - box.w_px + 1e-6
        assert 0.0 <= box.y_px <= h - box.h_px + 1e-6
        ew, eh = box.even_size()
        assert ew % 2 == 0 and ew >= 2
        assert eh % 2 == 0 and eh >= 2

    expr = _piecewise_linear_expr([0.0, 1.0, 2.0], [100.0, 100.0, 250.0], time_var="t")
    assert "if(lt(t,1.000000),100.000000," in expr

    with pytest.raises(GeometryValidationError):
        compute_ideal_crop_size(0, 1080, 1.0)
    with pytest.raises(GeometryValidationError):
        build_crop_filter(kfs, width=w, height=h, duration=2.5, fps=0)


def test_tier5_tts_pronunciations_regex_metachars_and_pcm16_clipping(tmp_path: Path) -> None:
    """Verify pronunciation replacement with regex metacharacters, empty keys, and PCM_16 clipping/pause=0."""
    prons = {
        "": "ShouldBeIgnored",
        "Sim$": "Sim-Dollar",
        "A.I.": "Artificial Intelligence",
        "[MOD]": "Modification",
        "Vaelen": "Vay-len",
        "Vaelen's": "Vay-lenz",
    }
    text = "In [MOD] mode, Vaelen's A.I. costs Sim$ in Vaelen."
    resolved = apply_pronunciations(text, prons)
    assert resolved == "In Modification mode, Vay-lenz Artificial Intelligence costs Sim-Dollar in Vay-len."

    empty_pcm = _float_to_pcm16(np.array([], dtype=np.float32))
    assert empty_pcm.dtype == np.int16 and empty_pcm.size == 0

    extreme = _float_to_pcm16(np.array([-2.5, -1.0, 0.0, 1.0, 3.5], dtype=np.float32))
    assert list(extreme) == [-32767, -32767, 0, 32767, 32767]

    seg = SegmentInput(
        id="seg_p0",
        video_in=0.0,
        video_out=2.0,
        crop_keyframes=[CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0)],
        clauses=[
            ClauseInput(id="c1", display_text="First"),
            ClauseInput(id="c2", display_text="Second"),
        ],
    )
    h_0 = compute_tts_hash(seg, prons, "bf_emma", "en-gb", inter_clause_pause_ms=0)
    h_120 = compute_tts_hash(seg, prons, "bf_emma", "en-gb", inter_clause_pause_ms=120)
    assert h_0 != h_120

    class MockKokoro:
        def __init__(self) -> None:
            self.sess = SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])

        def create(self, text: str, voice: str, speed: float, lang: str) -> tuple[np.ndarray, int]:
            return np.zeros(4800, dtype=np.float32), SAMPLE_RATE

    res = synthesize_segment_tts(seg, prons, "bf_emma", "en-gb", 0, tmp_path, kokoro=MockKokoro())
    assert res.total_samples == 9600
    assert res.clause_timings[0].end_sample == 4800
    assert res.clause_timings[1].start_sample == 4800


def test_tier5_subtitles_mismatched_lengths_and_fractional_outline() -> None:
    """Verify subtitle event builder guards, large hour timestamps, and fractional outline_width formatting."""
    assert build_segment_subtitle_events([ClauseInput(id="c1", display_text="One")], []) == []
    with pytest.raises(ValueError, match="Mismatch between clauses"):
        build_segment_subtitle_events(
            clauses=[
                ClauseInput(id="c1", display_text="One"),
                ClauseInput(id="c2", display_text="Two"),
            ],
            clause_timings=[
                ClauseTiming(clause_id="c1", start_sample=0, end_sample=24000, start_sec=0.0, end_sec=1.0),
            ],
        )

    assert format_ass_timestamp(3661.55) == "1:01:01.55"

    cap_frac = CaptionSettings(outline_width=3.75)
    ass_frac = render_ass_content([], cap_frac)
    assert ",1,3.75,0,2," in ass_frac

    cap_int = CaptionSettings(outline_width=5.0)
    ass_int = render_ass_content([], cap_int)
    assert ",1,5,0,2," in ass_int


def test_tier5_storage_fingerprint_boundaries_and_schema_version_guards(tmp_path: Path) -> None:
    """Test 64KB chunk boundary fingerprinting and invalid schema_version types."""
    f_exact = tmp_path / "exact_64k.bin"
    f_exact.write_bytes(b"A" * FINGERPRINT_CHUNK_BYTES)
    fp_exact = compute_file_fingerprint(f_exact)

    f_over = tmp_path / "over_64k.bin"
    f_over.write_bytes(b"A" * FINGERPRINT_CHUNK_BYTES + b"Z" * 10)
    fp_over = compute_file_fingerprint(f_over)
    assert fp_exact != fp_over

    for idx, bad_ver in enumerate([True, False, 0, -1, "1", None]):
        pid = f"bad_ver_{idx}"
        pdir = get_project_dir(pid, data_dir=tmp_path)
        pdir.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({"schema_version": bad_ver, "project_id": pid})
        (pdir / "project.json").write_text(raw, encoding="utf-8")
        with pytest.raises(UnsupportedSchemaVersionError):
            load_project(pid, data_dir=tmp_path)
        assert (pdir / "project.json").read_text(encoding="utf-8") == raw

    (tmp_path / "pronunciations.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(StorageError, match="must be a JSON object"):
        load_global_pronunciations(data_dir=tmp_path)


def test_tier5_media_internal_parsers_and_validators(tmp_path: Path) -> None:
    """Directly test _parse_fraction, _check_rotation_zero, _check_square_pixels, and upload filename guards."""
    assert _parse_fraction(None) is None
    assert _parse_fraction("0/0") is None
    assert _parse_fraction("30/0") is None
    assert _parse_fraction("-30/1") is None
    assert _parse_fraction("bad/fraction") is None
    assert math.isclose(_parse_fraction("60000/1001") or 0.0, 60000.0 / 1001.0)

    _check_rotation_zero({"tags": {"rotate": "0"}})
    _check_rotation_zero({"tags": {"rotate": "360"}})
    _check_rotation_zero({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -360}]})

    with pytest.raises(MediaValidationError):
        _check_rotation_zero({"tags": {"rotate": "not-a-float"}})
    with pytest.raises(MediaValidationError):
        _check_rotation_zero({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]})

    _check_square_pixels({"sample_aspect_ratio": "1:1"})
    _check_square_pixels({"sample_aspect_ratio": "0:1"})
    _check_square_pixels({"sample_aspect_ratio": "1000/1000"})
    for bad_sar in ("16:9", "invalid", "a:b", "1:0", "-1:1"):
        with pytest.raises(MediaValidationError):
            _check_square_pixels({"sample_aspect_ratio": bad_sar})

    for bad_name in ("", "   ", ".", "..", "a\x00b.mp4"):
        with pytest.raises(MediaValidationError):
            import_uploaded_media(bad_name, b"data", tmp_path / "uploads")


def test_tier5_renderer_duration_validation_and_corrupt_cache_self_healing(tmp_path: Path) -> None:
    """Verify duration planner validation guards, preview hash sensitivity, and Stage 1 corrupt cache recovery."""
    ffmpeg_path, ffprobe_path = verify_ffmpeg(offline=True)
    seg = SegmentInput(
        id="seg_heal",
        video_in=0.0,
        video_out=1.0,
        crop_keyframes=[CropKeyframe(t=0.0, x=0.2, y=0.0, zoom=1.0)],
        clauses=[ClauseInput(id="c1", display_text="Caption A")],
    )

    with pytest.raises(ValueError, match="fps must be positive"):
        plan_segment_duration(seg, 0.0, 10.0, 0.4, fps=0)
    with pytest.raises(ValueError, match="tail_padding_sec"):
        plan_segment_duration(seg, 0.0, 10.0, -0.1, fps=30)
    with pytest.raises(ValueError, match="source_duration"):
        plan_segment_duration(seg, 0.0, -1.0, 0.4, fps=30)

    cap1 = CaptionSettings(font_size=64)
    cap2 = CaptionSettings(font_size=72)
    ph1 = compute_preview_hash("r1", "t1", seg, cap1)
    ph2 = compute_preview_hash("r1", "t1", seg, cap2)
    seg_text2 = seg.model_copy(update={"clauses": [ClauseInput(id="c1", display_text="Caption B")]})
    ph3 = compute_preview_hash("r1", "t1", seg_text2, cap1)
    assert len({ph1, ph2, ph3}) == 3

    src_mp4 = tmp_path / "src.mp4"
    subprocess.run(
        [
            str(ffmpeg_path), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=2.0",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2.0",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(src_mp4),
        ],
        check=True,
    )
    ref = SourceMediaRef(media_id="m1", path=str(src_mp4), fingerprint="sha256:test")
    meta = SourceMetadata(width=1280, height=720, fps=30.0, duration=2.0, has_audio=True)
    out_settings = OutputSettings()
    proj_dir = tmp_path / "proj_heal"
    r_hash = compute_render_hash(seg, ref, "", out_settings)
    cached_path = proj_dir / "artifacts" / f"seg_{seg.id}_{r_hash}.mp4"
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(b"corrupt_zero_frame_garbage")

    healed_path = render_segment_stage1(
        segment=seg,
        source_media=ref,
        source_metadata=meta,
        output_settings=out_settings,
        project_dir=proj_dir,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    assert healed_path == cached_path
    info = probe_rendered_video(healed_path, ffprobe_path)
    assert info["width"] == 1080 and info["height"] == 1920

    seg_past_eof = seg.model_copy(update={"video_in": 2.5, "video_out": 3.5})
    with pytest.raises(RenderError, match="no available source footage"):
        render_segment_stage1(
            segment=seg_past_eof,
            source_media=ref,
            source_metadata=meta,
            output_settings=out_settings,
            project_dir=proj_dir,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )


# ---------------------------------------------------------------------------
# Part 2: Challenger 2 White-Box Security, Range, Job Snapshot/Recovery & Packaging Tests
# ---------------------------------------------------------------------------


def test_tier5_security_host_origin_and_session_token_matrix(client: TestClient) -> None:
    """Test hostile Host/Origin bypass vectors and missing/empty/wrong X-Session-Token across POST/PUT/DELETE/PATCH."""
    hostile_hosts = [
        "",
        "127.0.0.1.evil.com",
        "localhost.evil.com",
        "evil.localhost",
        "127.0.0.2",
        "0.0.0.0",
        "[::1]",
        "[::ffff:127.0.0.1]",
        "[127.0.0.1",
        "localhost@evil.com",
        "127.0.0.1#@evil.com",
    ]
    for bad_host in hostile_hosts:
        resp = client.get("/api/health", headers={"Host": bad_host})
        assert resp.status_code == 403, f"Host {bad_host!r} was not rejected with 403 (got {resp.status_code})"

    hostile_origins = [
        "null",
        "http://127.0.0.1.evil.com",
        "http://localhost.evil.com:8420",
        "https://evil.com",
        "file://localhost/path",
        "ws://127.0.0.1:8420",
        "javascript:alert(1)",
        "http://user@evil.com:8420",
    ]
    for bad_origin in hostile_origins:
        resp = client.get("/api/health", headers={"Origin": bad_origin})
        assert resp.status_code == 403, f"Origin {bad_origin!r} was not rejected with 403 (got {resp.status_code})"
        assert resp.headers.get("access-control-allow-origin") != "*"

    for method in ("POST", "PUT", "DELETE", "PATCH"):
        r_missing = client.request(method, "/api/pronunciations", json={"pronunciations": {}})
        assert r_missing.status_code == 401, f"{method} without token returned {r_missing.status_code}"

        r_empty = client.request(
            method,
            "/api/pronunciations",
            headers={"X-Session-Token": ""},
            json={"pronunciations": {}},
        )
        assert r_empty.status_code == 401, f"{method} with empty token returned {r_empty.status_code}"

        r_wrong = client.request(
            method,
            "/api/pronunciations",
            headers={"X-Session-Token": "invalid_session_token_value"},
            json={"pronunciations": {}},
        )
        assert r_wrong.status_code == 403, f"{method} with wrong token returned {r_wrong.status_code}"


def test_tier5_reject_derived_state_and_url_encoded_path_traversal(
    client: TestClient, isolated_data_dir: Path, tmp_path: Path
) -> None:
    """Reject derived-state injection in PUT /api/projects/{id} and URL-encoded/symlink path traversal."""
    headers = _auth_headers(client)
    project_id = "proj_tier5_sec"
    create_project(
        project_id=project_id,
        title="Security Probe",
        source_media=SourceMediaRef(media_id="src_1", path="/tmp/fake.mp4", fingerprint="sha256:abc"),
        data_dir=isolated_data_dir,
    )
    proj_dir = get_project_dir(project_id, data_dir=isolated_data_dir)
    manifest_before = (proj_dir / "manifest.json").read_text(encoding="utf-8")

    base_proj = client.get(f"/api/projects/{project_id}").json()["project"]
    base_seg = {
        "id": "seg_01",
        "video_in": 0.0,
        "video_out": 2.0,
        "duration_mode": "preserve_video",
        "source_audio_volume": 0.15,
        "speed": 1.0,
        "crop_keyframes": [{"t": 0.0, "x": 0.25, "y": 0.0, "zoom": 1.0}],
        "clauses": [{"id": "c1", "display_text": "Hello", "spoken_text_override": None}],
    }

    # 1. Inject derived fields at ProjectInput level -> 422
    for forbidden_proj_field, val in (
        ("manifest", {"proxy": {"status": "ready"}}),
        ("proxy", {"status": "ready", "path": "artifacts/evil.mp4"}),
        ("source_metadata", {"width": 1920, "height": 1080, "fps": 30.0, "duration": 10.0, "has_audio": True}),
        ("latest_export_path", "exports/hacked.mp4"),
        ("job_state", {"status": "succeeded"}),
    ):
        mutated = dict(base_proj)
        mutated[forbidden_proj_field] = val
        resp = client.put(
            f"/api/projects/{project_id}",
            headers=headers,
            json={"project": mutated, "expected_revision": base_proj["revision"]},
        )
        assert resp.status_code == 422, f"Expected 422 for project field {forbidden_proj_field!r}, got {resp.status_code}"

    # 2. Inject derived or unmapped fields at SegmentInput level -> 422
    for forbidden_seg_field, val in (
        ("order", 0),
        ("tts_hash", "deadbeef"),
        ("render_hash", "cafebabe"),
        ("audio_path", "artifacts/hacked.wav"),
        ("audio_duration", 99.9),
        ("spoken_text_override", "Whole segment override not mapped to clause"),
    ):
        bad_seg = dict(base_seg)
        bad_seg[forbidden_seg_field] = val
        mutated = dict(base_proj)
        mutated["segments"] = [bad_seg]
        resp = client.put(
            f"/api/projects/{project_id}",
            headers=headers,
            json={"project": mutated, "expected_revision": base_proj["revision"]},
        )
        assert resp.status_code == 422, f"Expected 422 for segment field {forbidden_seg_field!r}, got {resp.status_code}"

    # 3. Wrapper-level "manifest" key is ignored and never overwrites manifest.json on disk
    valid_update = dict(base_proj)
    valid_update["segments"] = [base_seg]
    r_ok = client.put(
        f"/api/projects/{project_id}",
        headers=headers,
        json={
            "project": valid_update,
            "expected_revision": base_proj["revision"],
            "manifest": {"proxy": {"status": "ready", "path": "../secret.mp4"}},
        },
    )
    assert r_ok.status_code == 200
    assert (proj_dir / "manifest.json").read_text(encoding="utf-8") == manifest_before

    # 4. URL-encoded path traversal and malformed project IDs across endpoints
    traversal_ids = [
        "%2e%2e",
        "%2e%2e%2fetc",
        "..%2fsecret",
        "..%5csecret",
        "proj%00evil",
        " . ",
    ]
    for bad_id in traversal_ids:
        r_g = client.get(f"/api/projects/{bad_id}")
        assert r_g.status_code in {400, 404, 422}, f"GET /api/projects/{bad_id} returned {r_g.status_code}"

        r_m = client.get(f"/api/projects/{bad_id}/media/art_proxy")
        assert r_m.status_code in {400, 403, 404}, f"GET media for {bad_id} returned {r_m.status_code}"

    # SPA fallback path traversal check: must not leak repository files outside FRONTEND_DIST_DIR
    r_spa_trav = client.get("/%2e%2e/pyproject.toml")
    assert "[project]" not in r_spa_trav.text

    # 5. Symlink escape inside project artifacts/ rejected with 403
    outside_secret = tmp_path / "outside_secret.mp4"
    outside_secret.write_bytes(b"TOP_SECRET_DATA")
    symlink_in_artifacts = proj_dir / "artifacts" / "symlink_escape.mp4"
    symlink_in_artifacts.symlink_to(outside_secret)

    manifest = load_manifest(project_id, data_dir=isolated_data_dir)
    manifest.proxy = ProxyState(status="ready", artifact_id="art_proxy", path="artifacts/symlink_escape.mp4")
    save_manifest(manifest, data_dir=isolated_data_dir)

    r_sym = client.get(f"/api/projects/{project_id}/media/art_proxy")
    assert r_sym.status_code == 403


def test_tier5_http_range_boundary_and_unsatisfiable_matrix(
    client: TestClient, isolated_data_dir: Path
) -> None:
    """Verify open-ended, suffix, oversized-end clamping, 1-byte boundaries, and 416 unsatisfiable ranges."""
    project_id = "proj_tier5_range"
    create_project(
        project_id=project_id,
        title="Range Probe",
        source_media=SourceMediaRef(media_id="src_1", path="/tmp/fake.mp4", fingerprint="sha256:abc"),
        data_dir=isolated_data_dir,
    )
    proj_dir = get_project_dir(project_id, data_dir=isolated_data_dir)
    payload = bytes(i % 256 for i in range(1024))
    proxy_file = proj_dir / "artifacts" / "proxy.mp4"
    proxy_file.write_bytes(payload)

    empty_wav = proj_dir / "artifacts" / "empty.wav"
    empty_wav.write_bytes(b"")

    manifest = load_manifest(project_id, data_dir=isolated_data_dir)
    manifest.proxy = ProxyState(status="ready", artifact_id="art_proxy", path="artifacts/proxy.mp4")
    manifest.segments["seg_01"] = SegmentArtifactState(
        audio_artifact_id="art_tts_seg_01",
        audio_path="artifacts/empty.wav",
    )
    save_manifest(manifest, data_dir=isolated_data_dir)

    url = f"/api/projects/{project_id}/media/art_proxy"

    # Valid Range requests -> 206 Partial Content
    valid_cases = [
        ("bytes=100-", 100, 1023, payload[100:]),
        ("bytes=-50", 974, 1023, payload[-50:]),
        ("bytes=-5000", 0, 1023, payload),
        ("bytes=500-9999", 500, 1023, payload[500:]),
        ("bytes=0-0", 0, 0, payload[:1]),
        ("bytes=1023-1023", 1023, 1023, payload[1023:1024]),
    ]
    for range_hdr, exp_start, exp_end, exp_bytes in valid_cases:
        resp = client.get(url, headers={"Range": range_hdr})
        assert resp.status_code == 206, f"Range {range_hdr!r} returned {resp.status_code}"
        assert resp.headers["content-range"] == f"bytes {exp_start}-{exp_end}/1024"
        assert resp.headers["content-length"] == str(len(exp_bytes))
        assert resp.headers["accept-ranges"] == "bytes"
        assert resp.content == exp_bytes

    # Unsatisfiable / malformed Range requests -> 416 with Content-Range: bytes */1024
    invalid_ranges = [
        "bytes=1024-",
        "bytes=2000-3000",
        "bytes=500-100",
        "bytes=-0",
        "bytes=-",
        "bytes=0-10, 20-30",
        "items=0-10",
        "bytes=abc-def",
        "bytes=--10",
    ]
    for bad_hdr in invalid_ranges:
        resp = client.get(url, headers={"Range": bad_hdr})
        assert resp.status_code == 416, f"Bad Range {bad_hdr!r} returned {resp.status_code}"
        assert resp.headers["content-range"] == "bytes */1024"

    # 0-byte artifact: full GET returns 200 empty, Range GET returns 416 with bytes */0
    empty_url = f"/api/projects/{project_id}/media/art_tts_seg_01"
    r_empty_200 = client.get(empty_url)
    assert r_empty_200.status_code == 200
    assert r_empty_200.content == b""

    r_empty_416 = client.get(empty_url, headers={"Range": "bytes=0-0"})
    assert r_empty_416.status_code == 416
    assert r_empty_416.headers["content-range"] == "bytes */0"


def test_tier5_job_snapshot_isolation_queued_cancel_and_symlink_safe_tmp_purge(
    client: TestClient, isolated_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify job snapshot isolation during concurrent PUT, queued job cancellation, and symlink-safe tmp/ purge."""
    headers = _auth_headers(client)
    project_id = "proj_tier5_jobs"

    proj = create_project(
        project_id=project_id,
        title="Job Snapshot Test",
        source_media=SourceMediaRef(media_id="src_1", path="/tmp/fake.mp4", fingerprint="sha256:abc"),
        voice="bf_emma",
        language="en-gb",
        data_dir=isolated_data_dir,
    )
    proj.segments = [
        SegmentInput(
            id="seg_01",
            video_in=0.0,
            video_out=2.0,
            crop_keyframes=[CropKeyframe(t=0.0, x=0.25, y=0.0, zoom=1.0)],
            clauses=[ClauseInput(id="c1", display_text="Original snapshot clause")],
        )
    ]
    proj = save_project(proj, expected_revision=proj.revision, data_dir=isolated_data_dir)

    # 1. Concurrent PUT while TTS job is mid-execution must not mutate the running job's snapshot
    tts_entered = threading.Event()
    tts_proceed = threading.Event()
    captured_call: dict[str, Any] = {}

    def _mock_synthesize_segment_tts(
        segment: SegmentInput,
        pronunciations: dict[str, str],
        voice: str,
        language: str,
        inter_clause_pause_ms: int,
        output_dir: Path,
        **kwargs: Any,
    ) -> SegmentTTSResult:
        tts_entered.set()
        assert tts_proceed.wait(timeout=10.0)
        captured_call["voice"] = voice
        captured_call["display_text"] = segment.clauses[0].display_text
        output_dir.mkdir(parents=True, exist_ok=True)
        wav_path = output_dir / f"seg_{segment.id}_mockhash.wav"
        wav_path.write_bytes(b"RIFFmockwav")
        return SegmentTTSResult(
            tts_hash="mockhash",
            audio_path=wav_path,
            sample_rate=24000,
            total_samples=24000,
            audio_duration=1.0,
            clause_timings=[
                ClauseTiming(clause_id="c1", start_sample=0, end_sample=24000, start_sec=0.0, end_sec=1.0)
            ],
            clause_wav_paths=[wav_path],
        )

    monkeypatch.setattr(main_module, "synthesize_segment_tts", _mock_synthesize_segment_tts)

    r_tts = client.post(f"/api/projects/{project_id}/segments/seg_01/tts", headers=headers)
    assert r_tts.status_code == 202
    tts_job_id = r_tts.json()["job_id"]
    assert tts_entered.wait(timeout=5.0)

    # Mutate project via PUT while TTS worker is paused
    mutated_payload = proj.model_dump(mode="json")
    mutated_payload["voice"] = "af_heart"
    mutated_payload["segments"][0]["clauses"][0]["display_text"] = "Mutated concurrent clause"
    r_put = client.put(
        f"/api/projects/{project_id}",
        headers=headers,
        json={"project": mutated_payload, "expected_revision": proj.revision},
    )
    assert r_put.status_code == 200

    tts_proceed.set()
    finished_job = _poll_job(client, tts_job_id)
    assert finished_job["status"] == "succeeded"
    assert captured_call["voice"] == "bf_emma"
    assert captured_call["display_text"] == "Original snapshot clause"

    # 2. Cancelling a queued job before execution prevents worker_fn from ever running
    jm = JobManager(data_dir=tmp_path / "jm_data", max_workers=1)
    try:
        blocker_started = threading.Event()
        blocker_release = threading.Event()
        queued_worker_called = False

        def _blocker(ctx: JobContext) -> dict[str, Any]:
            blocker_started.set()
            blocker_release.wait(timeout=10.0)
            return {"ok": True}

        def _should_not_run(ctx: JobContext) -> dict[str, Any]:
            nonlocal queued_worker_called
            queued_worker_called = True
            return {"ran": True}

        job1 = jm.submit_job("p1", "proxy", _blocker)
        assert blocker_started.wait(timeout=5.0)

        job2 = jm.submit_job("p1", "tts", _should_not_run)
        assert jm.get_job(job2.job_id).status == "queued"

        cancelled_state = jm.cancel_job(job2.job_id)
        assert cancelled_state is not None and cancelled_state.status == "cancelled"

        blocker_release.set()
        jm._futures[job1.job_id].result(timeout=5.0)
        jm._futures[job2.job_id].result(timeout=5.0)

        assert queued_worker_called is False
        final_job2 = jm.get_job(job2.job_id)
        assert final_job2 is not None and final_job2.status == "cancelled"
    finally:
        jm.shutdown(wait=True)

    # 3. recover_on_startup() unlinks directory symlinks inside projects/<id>/tmp/ without deleting external files
    external_dir = tmp_path / "external_keep_dir"
    external_dir.mkdir(parents=True, exist_ok=True)
    precious_file = external_dir / "precious.txt"
    precious_file.write_text("DO_NOT_DELETE", encoding="utf-8")

    proj_tmp = get_project_dir(project_id, data_dir=isolated_data_dir) / "tmp"
    linked_dir = proj_tmp / "linked_external"
    linked_dir.symlink_to(external_dir, target_is_directory=True)
    transient_file = proj_tmp / "scratch.txt"
    transient_file.write_text("temp", encoding="utf-8")

    recovery_jm = JobManager(data_dir=isolated_data_dir, max_workers=1)
    try:
        recovery_jm.recover_on_startup()
    finally:
        recovery_jm.shutdown(wait=True)

    assert not linked_dir.exists() and not linked_dir.is_symlink()
    assert not transient_file.exists()
    assert precious_file.is_file()
    assert precious_file.read_text(encoding="utf-8") == "DO_NOT_DELETE"


def test_tier5_release_archive_tamper_and_bootstrap_corrupt_asset_guards(tmp_path: Path) -> None:
    """Verify verify_release_archive rejects injected .pyc and 0o644 start.sh, and bootstrap rejects corrupt assets."""
    archive_path = assemble_release_archive(
        output_path=tmp_path / "release_valid.zip",
        skip_frontend_build=True,
    )
    info = verify_release_archive(archive_path)
    assert info["start_sh_mode"] == "0o755"

    # 1. Tamper archive by injecting a __pycache__/.pyc artifact -> RuntimeError
    pyc_zip = tmp_path / "release_with_pyc.zip"
    with zipfile.ZipFile(archive_path, "r") as src_zf, zipfile.ZipFile(pyc_zip, "w") as dst_zf:
        for item in src_zf.infolist():
            dst_zf.writestr(item, src_zf.read(item.filename))
        dst_zf.writestr("backend/__pycache__/evil.cpython-312.pyc", b"bad_bytecode")

    with pytest.raises(RuntimeError, match="Forbidden Python cache artifact"):
        verify_release_archive(pyc_zip)

    # 2. Tamper archive by stripping executable bit (0o644) on start.sh -> RuntimeError
    non_exec_zip = tmp_path / "release_non_exec_start_sh.zip"
    with zipfile.ZipFile(archive_path, "r") as src_zf, zipfile.ZipFile(non_exec_zip, "w") as dst_zf:
        for item in src_zf.infolist():
            data = src_zf.read(item.filename)
            if item.filename == "start.sh":
                item.external_attr = ((0o100000 | 0o644) & 0xFFFF) << 16
            dst_zf.writestr(item, data)

    with pytest.raises(RuntimeError, match="executable permissions"):
        verify_release_archive(non_exec_zip)

    # 3. Bootstrap offline font & model corruption checks -> RuntimeError
    fake_repo = tmp_path / "fake_repo"
    font_dir = fake_repo / "assets" / "fonts"
    font_dir.mkdir(parents=True, exist_ok=True)
    font_file = font_dir / "Montserrat-ExtraBold.ttf"

    font_file.write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty"):
        verify_font(fake_repo)

    font_file.write_bytes(b"BAD!" * 16)
    with pytest.raises(RuntimeError, match="invalid TTF/OTF header"):
        verify_font(fake_repo)

    corrupt_data_dir = tmp_path / "corrupt_data"
    (corrupt_data_dir / "models").mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError, match="missing and offline mode is enabled"):
        ensure_kokoro_models(corrupt_data_dir, offline=True)

    (corrupt_data_dir / "models" / "kokoro-v1.0.onnx").write_bytes(b"corrupt_onnx_bytes")
    with pytest.raises(RuntimeError, match="corrupt or incomplete"):
        ensure_kokoro_models(corrupt_data_dir, offline=True)
