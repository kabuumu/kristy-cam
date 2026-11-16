"""Milestone 1 unit and integration tests for schemas, storage, geometry, tts, and subtitles."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.bootstrap import ensure_kokoro_models, get_data_dir, verify_ffmpeg
from backend.geometry import (
    CropBox,
    GeometryValidationError,
    build_crop_filter,
    clamp_normalized_coords,
    compute_ideal_crop_size,
    detect_trim_out_of_range,
    evaluate_crop_at,
    round_even,
    validate_keyframes,
)
from backend.schemas import (
    CURRENT_SCHEMA_VERSION,
    ArtifactManifest,
    CaptionSettings,
    ClauseInput,
    ClauseTiming,
    CropKeyframe,
    JobState,
    OutputSettings,
    ProjectInput,
    SegmentInput,
    SourceMediaRef,
)
from backend.storage import (
    CorruptProjectError,
    ProjectNotFoundError,
    RevisionConflictError,
    StorageError,
    UnsupportedSchemaVersionError,
    atomic_write_bytes,
    atomic_write_json,
    check_source_media,
    compute_file_fingerprint,
    create_project,
    get_project_dir,
    load_global_pronunciations,
    load_manifest,
    load_project,
    relink_source_media,
    save_global_pronunciations,
    save_manifest,
    save_project,
)
from backend.subtitles import (
    build_segment_subtitle_events,
    escape_ass_text,
    format_ass_timestamp,
    generate_ass_file,
    render_ass_content,
)
from backend.tts import (
    SAMPLE_RATE,
    apply_pronunciations,
    compute_tts_hash,
    list_voices,
    resolve_clause_spoken_text,
    synthesize_segment_tts,
)


def _make_source_ref(path: str = "/tmp/capture.mp4", fingerprint: str = "sha256:abc") -> SourceMediaRef:
    return SourceMediaRef(media_id="src_1", path=path, fingerprint=fingerprint, copied_to_project=False)


def _make_segment(
    seg_id: str = "seg_01",
    video_in: float = 0.0,
    video_out: float = 5.0,
    speed: float = 1.0,
    clauses: list[ClauseInput] | None = None,
    keyframes: list[CropKeyframe] | None = None,
) -> SegmentInput:
    return SegmentInput(
        id=seg_id,
        video_in=video_in,
        video_out=video_out,
        duration_mode="preserve_video",
        source_audio_volume=0.15,
        speed=speed,
        crop_keyframes=keyframes if keyframes is not None else [CropKeyframe(t=0.0, x=0.25, y=0.0, zoom=1.0)],
        clauses=clauses if clauses is not None else [ClauseInput(id="c1", display_text="Hello Sims")],
    )


# ---------------------------------------------------------------------------
# 1. Schemas
# ---------------------------------------------------------------------------


def test_schemas_strict_validation_and_ordering() -> None:
    assert CURRENT_SCHEMA_VERSION == 1

    # Extra fields forbidden on input models (no order integer field, no whole-segment override)
    with pytest.raises(ValidationError):
        SegmentInput(
            id="seg_01",
            order=0,  # type: ignore[call-arg]
            video_in=0.0,
            video_out=4.0,
            crop_keyframes=[CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0)],
        )

    with pytest.raises(ValidationError):
        SegmentInput(
            id="seg_01",
            video_in=0.0,
            video_out=4.0,
            spoken_text_override="unmapped",  # type: ignore[call-arg]
            crop_keyframes=[CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0)],
        )

    with pytest.raises(ValidationError):
        ProjectInput(
            project_id="p1",
            title="Test",
            source_media=_make_source_ref(),
            extra_field="nope",  # type: ignore[call-arg]
        )

    # Speed bounds [0.8, 1.25]
    with pytest.raises(ValidationError):
        _make_segment(speed=0.79)
    with pytest.raises(ValidationError):
        _make_segment(speed=1.26)
    assert _make_segment(speed=0.8).speed == 0.8
    assert _make_segment(speed=1.25).speed == 1.25

    # source_audio_volume bounds [0.0, 1.0]
    with pytest.raises(ValidationError):
        SegmentInput(
            id="s1",
            video_in=0.0,
            video_out=2.0,
            source_audio_volume=1.1,
            crop_keyframes=[CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0)],
        )

    # zoom bounds [1.0, 3.0]
    with pytest.raises(ValidationError):
        CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=0.9)
    with pytest.raises(ValidationError):
        CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=3.1)

    # video_out > video_in and video_in >= 0
    with pytest.raises(ValidationError):
        _make_segment(video_in=-0.1, video_out=2.0)
    with pytest.raises(ValidationError):
        _make_segment(video_in=3.0, video_out=3.0)

    # crop_keyframes non-empty, t[0] == 0.0, strictly increasing & finite
    with pytest.raises(ValidationError):
        _make_segment(keyframes=[])
    with pytest.raises(ValidationError):
        _make_segment(keyframes=[CropKeyframe(t=0.5, x=0.1, y=0.0, zoom=1.0)])
    with pytest.raises(ValidationError):
        _make_segment(
            keyframes=[
                CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0),
                CropKeyframe(t=1.0, x=0.2, y=0.0, zoom=1.0),
                CropKeyframe(t=1.0, x=0.3, y=0.0, zoom=1.0),
            ]
        )
    with pytest.raises(ValidationError):
        CropKeyframe(t=math.nan, x=0.0, y=0.0, zoom=1.0)

    # duration_mode literal
    with pytest.raises(ValidationError):
        SegmentInput(
            id="s1",
            video_in=0.0,
            video_out=2.0,
            duration_mode="stretch",  # type: ignore[arg-type]
            crop_keyframes=[CropKeyframe(t=0.0, x=0.0, y=0.0, zoom=1.0)],
        )

    # Manifest and JobState separation
    manifest = ArtifactManifest(project_id="p1")
    assert manifest.proxy.status == "pending"
    job = JobState(job_id="j1", project_id="p1", job_type="tts")
    assert job.status == "queued"


# ---------------------------------------------------------------------------
# 2. Storage & Portability
# ---------------------------------------------------------------------------


def test_storage_path_traversal_rejected(tmp_path: Path) -> None:
    for bad_id in ("", "../evil", "a/b", "a\\b", "foo..bar", " spaced "):
        with pytest.raises(StorageError):
            get_project_dir(bad_id, data_dir=tmp_path)


def test_storage_create_save_revision_conflict_and_bak_recovery(tmp_path: Path) -> None:
    save_global_pronunciations({"Vaelen": "Vay-len"}, data_dir=tmp_path)
    proj = create_project(
        project_id="sims_ep1",
        title="Episode 1",
        source_media=_make_source_ref(),
        segments=[_make_segment()],
        data_dir=tmp_path,
    )
    proj_dir = get_project_dir("sims_ep1", data_dir=tmp_path)
    for sub in ("artifacts", "exports", "tmp", "source"):
        assert (proj_dir / sub).is_dir()

    # Project seeded from global pronunciations, isolated from later global edits
    assert proj.pronunciations == {"Vaelen": "Vay-len"}
    save_global_pronunciations({"Vaelen": "Vay-len", "NewWord": "Noo"}, data_dir=tmp_path)
    reloaded = load_project("sims_ep1", data_dir=tmp_path)
    assert reloaded.pronunciations == {"Vaelen": "Vay-len"}
    assert "NewWord" in load_global_pronunciations(data_dir=tmp_path)

    # Save with matching expected_revision increments revision & creates .bak
    modified = reloaded.model_copy(update={"title": "Episode 1 Updated"})
    saved = save_project(modified, expected_revision=1, data_dir=tmp_path)
    assert saved.revision == 2
    assert (proj_dir / "project.json.bak").is_file()

    # Stale expected_revision raises RevisionConflictError (HTTP 409)
    with pytest.raises(RevisionConflictError) as exc_info:
        save_project(saved, expected_revision=1, data_dir=tmp_path)
    assert exc_info.value.expected_revision == 1
    assert exc_info.value.actual_revision == 2

    # Corrupt project.json falls back to .bak (which has revision 1)
    (proj_dir / "project.json").write_text("{corrupt json", encoding="utf-8")
    recovered = load_project("sims_ep1", data_dir=tmp_path)
    assert recovered.revision == 1
    assert recovered.title == "Episode 1"

    # Missing project.json also falls back to .bak
    (proj_dir / "project.json").unlink()
    recovered2 = load_project("sims_ep1", data_dir=tmp_path)
    assert recovered2.revision == 1

    # Both corrupt -> CorruptProjectError
    (proj_dir / "project.json.bak").write_text("{also corrupt", encoding="utf-8")
    with pytest.raises(CorruptProjectError):
        load_project("sims_ep1", data_dir=tmp_path)

    # Missing project -> ProjectNotFoundError
    with pytest.raises(ProjectNotFoundError):
        load_project("nonexistent_proj", data_dir=tmp_path)


def test_storage_unsupported_schema_version_no_overwrite(tmp_path: Path) -> None:
    proj_dir = get_project_dir("future_proj", data_dir=tmp_path)
    proj_dir.mkdir(parents=True, exist_ok=True)
    future_payload = {
        "schema_version": CURRENT_SCHEMA_VERSION + 99,
        "project_id": "future_proj",
        "revision": 1,
        "title": "Future",
        "created_at": "2026-11-16T00:00:00Z",
        "updated_at": "2026-11-16T00:00:00Z",
        "source_media": {"media_id": "m1", "path": "/tmp/x.mp4", "fingerprint": "sha256:1"},
        "future_only_field": {"nested": True},
    }
    raw_text = json.dumps(future_payload, indent=2)
    project_path = proj_dir / "project.json"
    project_path.write_text(raw_text, encoding="utf-8")

    with pytest.raises(UnsupportedSchemaVersionError) as exc_info:
        load_project("future_proj", data_dir=tmp_path)
    assert exc_info.value.found_version == CURRENT_SCHEMA_VERSION + 99
    assert exc_info.value.max_supported_version == CURRENT_SCHEMA_VERSION
    # File must remain completely untouched
    assert project_path.read_text(encoding="utf-8") == raw_text


def test_storage_atomic_write_and_source_portability(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    atomic_write_json(target, {"a": 1}, create_backup=True)
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    atomic_write_json(target, {"a": 2}, create_backup=True)
    assert json.loads(target.with_name("data.json.bak").read_text(encoding="utf-8")) == {"a": 1}
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    # No leftover .tmp files
    assert list(tmp_path.glob(".*.tmp")) == []

    # Source fingerprinting & relinking
    src_file = tmp_path / "capture_orig.mp4"
    src_file.write_bytes(b"HEADER" * 20000 + b"FOOTER" * 20000)
    fp1 = compute_file_fingerprint(src_file)
    ref = SourceMediaRef(media_id="src_1", path=str(src_file), fingerprint=fp1, copied_to_project=False)
    assert check_source_media(ref)["status"] == "ok"

    # Modify file -> modified status
    src_file.write_bytes(b"CHANGED" + b"0" * 1000)
    assert check_source_media(ref)["status"] == "modified"

    # Missing file -> missing status
    src_file.unlink()
    assert check_source_media(ref)["status"] == "missing"

    # Create project and relink with copy_to_project=True
    new_src = tmp_path / "relocated.mp4"
    new_src.write_bytes(b"NEW_VIDEO_BYTES" * 5000)
    create_project("relink_proj", "Relink Test", ref, data_dir=tmp_path)
    updated = relink_source_media(
        "relink_proj",
        new_src,
        media_id="src_2",
        copy_to_project=True,
        data_dir=tmp_path,
    )
    assert updated.source_media.copied_to_project is True
    copied_path = Path(updated.source_media.path)
    assert copied_path.is_file()
    assert copied_path.parent == get_project_dir("relink_proj", data_dir=tmp_path) / "source"
    assert check_source_media(updated.source_media)["status"] == "ok"

    # Manifest roundtrip
    m = load_manifest("relink_proj", data_dir=tmp_path)
    m.proxy.status = "ready"
    save_manifest(m, data_dir=tmp_path)
    assert load_manifest("relink_proj", data_dir=tmp_path).proxy.status == "ready"


# ---------------------------------------------------------------------------
# 3. Geometry
# ---------------------------------------------------------------------------


def test_geometry_bounds_interpolation_trim_and_even_rounding() -> None:
    assert round_even(607.5) == 608
    assert round_even(1.0) == 2
    assert round_even(1080.0) == 1080

    w, h = compute_ideal_crop_size(1920, 1080, zoom=1.0)
    assert math.isclose(w, 607.5)
    assert math.isclose(h, 1080.0)

    # At zoom=1.0, vertical travel is 0.0
    cx, cy = clamp_normalized_coords(x=0.95, y=0.5, zoom=1.0, width=1920, height=1080)
    assert math.isclose(cx * 1920, 1920 - 607.5, rel_tol=1e-6)
    assert math.isclose(cy, 0.0, abs_tol=1e-9)

    # Keyframe validation rejects out-of-bounds persisted coordinates
    bad_kfs = [CropKeyframe(t=0.0, x=0.9, y=0.0, zoom=1.0)]  # max x is (1920-607.5)/1920 = 0.68359
    with pytest.raises(GeometryValidationError):
        validate_keyframes(bad_kfs, duration=5.0, width=1920, height=1080)

    bad_y_kfs = [CropKeyframe(t=0.0, x=0.2, y=0.01, zoom=1.0)]
    with pytest.raises(GeometryValidationError):
        validate_keyframes(bad_y_kfs, duration=5.0, width=1920, height=1080)

    # Linear interpolation + hold beyond final keyframe
    kfs = [
        CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0),
        CropKeyframe(t=2.0, x=0.3, y=0.2, zoom=2.0),
    ]
    validate_keyframes(kfs, duration=5.0, width=1920, height=1080)

    box_neg = evaluate_crop_at(kfs, t=-1.0, width=1920, height=1080)
    assert math.isclose(box_neg.x_px, 0.1 * 1920)
    assert math.isclose(box_neg.zoom, 1.0)

    box_mid = evaluate_crop_at(kfs, t=1.0, width=1920, height=1080)
    assert math.isclose(box_mid.x_px, 0.2 * 1920)
    assert math.isclose(box_mid.y_px, 0.1 * 1080)
    assert math.isclose(box_mid.zoom, 1.5)
    assert box_mid.even_size() == (404, 720)

    # Freeze-frame extension beyond final keyframe holds final values
    box_hold = evaluate_crop_at(kfs, t=7.5, width=1920, height=1080)
    assert math.isclose(box_hold.x_px, 0.3 * 1920)
    assert math.isclose(box_hold.y_px, 0.2 * 1080)
    assert math.isclose(box_hold.zoom, 2.0)

    # Trim detection never discards, returns out-of-range indices
    kfs_trim = [
        CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0),
        CropKeyframe(t=2.5, x=0.2, y=0.0, zoom=1.0),
        CropKeyframe(t=4.0, x=0.3, y=0.0, zoom=1.0),
    ]
    assert detect_trim_out_of_range(kfs_trim, new_duration=3.0) == [2]
    assert detect_trim_out_of_range(kfs_trim, new_duration=2.0) == [1, 2]

    # Static and animated filter syntax
    static_filter = build_crop_filter(
        [CropKeyframe(t=0.0, x=0.2, y=0.0, zoom=1.0), CropKeyframe(t=2.0, x=0.4, y=0.0, zoom=1.0)],
        width=1920,
        height=1080,
        duration=3.0,
        fps=30,
    )
    assert "crop=w=608:h=1080" in static_filter
    assert "scale=1080:1920:flags=lanczos,setsar=1,fps=30" in static_filter

    anim_filter = build_crop_filter(kfs, width=1920, height=1080, duration=3.0, fps=30)
    assert "zoompan=" in anim_filter
    assert "d=1:fps=30:s=1080x1920" in anim_filter


@pytest.mark.parametrize("mode", ["static", "animated"])
def test_geometry_ffmpeg_filter_execution(tmp_path: Path, mode: str) -> None:
    try:
        ffmpeg_path, ffprobe_path = verify_ffmpeg(offline=True)
    except RuntimeError:
        pytest.skip("FFmpeg not available locally for integration test")

    if mode == "static":
        kfs = [
            CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0),
            CropKeyframe(t=1.0, x=0.4, y=0.0, zoom=1.0),
        ]
    else:
        kfs = [
            CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0),
            CropKeyframe(t=1.0, x=0.3, y=0.2, zoom=2.0),
        ]

    vf = build_crop_filter(kfs, width=1920, height=1080, duration=1.0, fps=30)
    out_mp4 = tmp_path / f"geom_{mode}.mp4"
    cmd = [
        str(ffmpeg_path),
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1920x1080:rate=30:duration=1.0",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-frames:v",
        "30",
        str(out_mp4),
    ]
    res = subprocess.run(cmd, shell=False, capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"FFmpeg failed: {res.stderr}"

    probe = subprocess.run(
        [str(ffprobe_path), "-v", "error", "-show_streams", "-of", "json", str(out_mp4)],
        shell=False,
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["width"] == 1080
    assert stream["height"] == 1920
    assert stream["sample_aspect_ratio"] == "1:1"
    assert stream["r_frame_rate"] == "30/1"
    assert int(stream["nb_frames"]) == 30


# ---------------------------------------------------------------------------
# 4. TTS & Pronunciations
# ---------------------------------------------------------------------------


def test_tts_pronunciations_non_recursive_unicode_longest_first() -> None:
    # Non-recursive: A -> B, B -> C must NOT turn A into C
    assert apply_pronunciations("A and B", {"A": "B", "B": "C"}) == "B and C"

    # Longest key first & case-insensitive
    prons = {"New": "N1", "New York": "Noo Yawk", "Élodie": "Ay-lo-dee", "C++": "See Plus Plus"}
    assert apply_pronunciations("new york is NEW", prons) == "Noo Yawk is N1"

    # Unicode word boundaries: should NOT match inside another word
    assert apply_pronunciations("Élodie met Élodiea", prons) == "Ay-lo-dee met Élodiea"

    # Literal escaping (C++ regex metacharacters)
    # Note: '+' is non-\w so (?!\w) matches after 'C++' when followed by space/punctuation/end
    assert apply_pronunciations("I love C++ code", prons) == "I love See Plus Plus code"

    # spoken_text_override takes verbatim precedence over pronunciations
    c_override = ClauseInput(id="c1", display_text="Lord Vaelen", spoken_text_override="Custom Phonetic")
    c_normal = ClauseInput(id="c2", display_text="Lord Vaelen", spoken_text_override=None)
    assert resolve_clause_spoken_text(c_override, {"Vaelen": "Vay-len"}) == "Custom Phonetic"
    assert resolve_clause_spoken_text(c_normal, {"Vaelen": "Vay-len"}) == "Lord Vay-len"


def test_tts_hash_staleness_and_mock_synthesis(tmp_path: Path) -> None:
    seg = _make_segment(
        clauses=[
            ClauseInput(id="c1", display_text="Lord Vaelen"),
            ClauseInput(id="c2", display_text="refused to leave."),
        ]
    )
    prons = {"Vaelen": "Vay-len"}
    h_base = compute_tts_hash(seg, prons, voice="bf_emma", language="en-gb", inter_clause_pause_ms=120)

    # Every relevant edit invalidates tts_hash
    assert h_base != compute_tts_hash(seg, {"Vaelen": "Vay-lin"}, "bf_emma", "en-gb", 120)
    assert h_base != compute_tts_hash(seg, prons, "af_bella", "en-gb", 120)
    assert h_base != compute_tts_hash(seg, prons, "bf_emma", "en-us", 120)
    assert h_base != compute_tts_hash(seg.model_copy(update={"speed": 1.1}), prons, "bf_emma", "en-gb", 120)

    # Empty clauses -> valid silent result
    empty_seg = _make_segment(clauses=[])
    silent_res = synthesize_segment_tts(empty_seg, prons, "bf_emma", "en-gb", 120, tmp_path)
    assert silent_res.total_samples == 0
    assert silent_res.audio_duration == 0.0
    assert silent_res.clause_timings == []
    assert silent_res.audio_path is None
    assert silent_res.clause_wav_paths == []

    # Mock Kokoro verifying CPUExecutionProvider, 120ms pause between clauses, omitted after final
    class MockKokoro:
        def __init__(self) -> None:
            self.sess = SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])
            self.calls: list[str] = []

        def get_voices(self) -> list[str]:
            return ["bf_emma", "af_bella"]

        def create(self, text: str, voice: str, speed: float, lang: str) -> tuple[np.ndarray, int]:
            self.calls.append(text)
            # 0.5s (12000 samples) for c1, 0.25s (6000 samples) for c2
            n = 12000 if len(self.calls) == 1 else 6000
            return np.full(n, 0.25, dtype=np.float32), SAMPLE_RATE

    mock = MockKokoro()
    assert list_voices(kokoro=mock) == ["af_bella", "bf_emma"]

    res = synthesize_segment_tts(seg, prons, "bf_emma", "en-gb", 120, tmp_path, kokoro=mock)
    assert mock.calls == ["Lord Vay-len", "refused to leave."]
    pause_samples = int(round(24000 * 0.120))  # 2880 samples
    assert res.total_samples == 12000 + pause_samples + 6000
    assert math.isclose(res.audio_duration, (12000 + 2880 + 6000) / 24000.0)
    assert len(res.clause_timings) == 2
    assert res.clause_timings[0].start_sample == 0
    assert res.clause_timings[0].end_sample == 12000
    assert res.clause_timings[1].start_sample == 12000 + pause_samples
    assert res.clause_timings[1].end_sample == 12000 + pause_samples + 6000
    assert res.audio_path is not None and res.audio_path.is_file()
    data, sr = sf.read(str(res.audio_path), dtype="int16")
    assert sr == 24000
    assert len(data) == res.total_samples
    # Pause region must be exact silence
    assert np.all(data[12000 : 12000 + pause_samples] == 0)


def test_tts_real_kokoro_cpu_integration(tmp_path: Path) -> None:
    try:
        data_dir = get_data_dir()
        ensure_kokoro_models(data_dir, offline=True)
    except RuntimeError:
        pytest.skip("Kokoro models not cached locally for offline integration test")

    voices = list_voices(data_dir=data_dir, offline=True)
    assert "bf_emma" in voices

    seg = _make_segment(clauses=[ClauseInput(id="c1", display_text="Hello Windenburg.")])
    res = synthesize_segment_tts(
        seg,
        {"Windenburg": "Vin-den-burg"},
        voice="bf_emma",
        language="en-gb",
        inter_clause_pause_ms=120,
        output_dir=tmp_path,
    )
    assert res.total_samples > 0
    assert res.audio_path is not None and res.audio_path.is_file()


# ---------------------------------------------------------------------------
# 5. Subtitles
# ---------------------------------------------------------------------------


def test_subtitles_escaping_continuity_and_ass_output(tmp_path: Path) -> None:
    # Escaping order: \ -> \\, { -> \{, } -> \}, \r\n/\r/\n -> \N
    raw = "Line 1 \\ {bold}\r\nLine 2\rLine 3"
    assert escape_ass_text(raw) == r"Line 1 \\ \{bold\}\NLine 2\NLine 3"

    assert format_ass_timestamp(-0.5) == "0:00:00.00"
    assert format_ass_timestamp(65.432) == "0:01:05.43"

    clauses = [
        ClauseInput(id="c1", display_text="Lord Vaelen", spoken_text_override="Lord Vay-len"),
        ClauseInput(id="c2", display_text="refused {to} leave."),
    ]
    timings = [
        ClauseTiming(clause_id="c1", start_sample=0, end_sample=24000, start_sec=0.0, end_sec=1.0),
        ClauseTiming(clause_id="c2", start_sample=26880, end_sample=50880, start_sec=1.12, end_sec=2.12),
    ]
    events = build_segment_subtitle_events(clauses, timings, segment_offset_sec=10.0)
    assert len(events) == 2
    # Clause 1 stays visible across the 120ms pause until clause 2 starts!
    assert math.isclose(events[0].start_sec, 10.0)
    assert math.isclose(events[0].end_sec, 11.12)
    assert events[0].text == "Lord Vaelen"  # display_text only, never spoken_text_override
    # Final clause ends strictly at final clause audio end_sec
    assert math.isclose(events[1].start_sec, 11.12)
    assert math.isclose(events[1].end_sec, 12.12)

    ass_path = tmp_path / "out.ass"
    generate_ass_file(events, CaptionSettings(), ass_path)
    content = ass_path.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in content
    assert "PlayResY: 1920" in content
    assert "WrapStyle: 0" in content
    assert ",2,60,60,340,1" in content  # Alignment=2 (\an2)
    assert "Dialogue: 0,0:00:10.00,0:00:11.12,Default,,0,0,0,,Lord Vaelen" in content
    assert r"Dialogue: 0,0:00:11.12,0:00:12.12,Default,,0,0,0,,refused \{to\} leave." in content

    from backend.subtitles import SubtitleEvent, render_ass_content

    long_ev = SubtitleEvent(
        start_sec=0.0,
        end_sec=2.0,
        text="This is a very long caption that will wrap across four or more lines on a vertical screen",
    )
    long_content = render_ass_content([long_ev], CaptionSettings(), width=1080, height=1920)
    assert r"{\an5}This is a very long caption" in long_content
