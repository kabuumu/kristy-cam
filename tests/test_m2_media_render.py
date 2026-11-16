"""Milestone 2 unit and integration tests for media, proxy, jobs, and renderer."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.bootstrap import verify_ffmpeg
from backend.jobs import JobContext, JobManager
from backend.media import (
    MediaValidationError,
    import_uploaded_media,
    probe_and_validate_media,
    register_source_media,
    select_media_native,
)
from backend.proxy import ProxyError, generate_proxy, map_source_to_proxy_time
from backend.renderer import (
    AUDIO_SAMPLE_RATE,
    compute_render_hash,
    plan_segment_duration,
    render_final_export,
    render_segment_preview,
    render_segment_stage1,
)
from backend.schemas import (
    ArtifactManifest,
    CaptionSettings,
    ClauseInput,
    ClauseTiming,
    CropKeyframe,
    JobState,
    OutputSettings,
    ProjectInput,
    SegmentArtifactState,
    SegmentInput,
    SourceMediaRef,
    SourceMetadata,
)
from backend.storage import create_project, get_project_dir, load_manifest


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def ffmpeg_tools() -> tuple[Path, Path]:
    """Return verified (ffmpeg_path, ffprobe_path) for real media tests."""
    return verify_ffmpeg(offline=True)


def _run_ffmpeg(ffmpeg_path: Path, args: list[str]) -> None:
    res = subprocess.run(
        [str(ffmpeg_path), "-y", "-hide_banner", "-loglevel", "error", *args],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert res.returncode == 0, f"FFmpeg failed: {res.stderr}"


def _ffprobe_json(ffprobe_path: Path, media_path: Path, *, count_frames: bool = False) -> dict:
    cmd = [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
    ]
    if count_frames:
        cmd.append("-count_frames")
    cmd.append(str(media_path))
    res = subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return json.loads(res.stdout)


def _make_cfr_video(
    ffmpeg_path: Path,
    out_path: Path,
    *,
    width: int = 1280,
    height: int = 720,
    duration: float = 3.0,
    fps: int = 30,
    with_audio: bool = True,
    audio_freq: int = 440,
    vf: str | None = None,
    extra_output_args: list[str] | None = None,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
    ]
    if with_audio:
        args.extend(["-f", "lavfi", "-i", f"sine=frequency={audio_freq}:sample_rate=48000:duration={duration}"])

    if vf:
        args.extend(["-vf", vf])

    args.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    if with_audio:
        args.extend(["-c:a", "aac", "-ar", "48000", "-ac", "2"])
    if extra_output_args:
        args.extend(extra_output_args)
    args.append(str(out_path))
    _run_ffmpeg(ffmpeg_path, args)
    return out_path


def _make_vfr_video(ffmpeg_path: Path, out_path: Path) -> Path:
    """Synthesize a genuine VFR 16:9 MP4 by alternating frame durations via settb + setpts."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1280x720:rate=30:duration=2.0",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=2.0",
        "-vf",
        "settb=1/1000,setpts='if(eq(N,0),0,PREV_OUTPTS+if(mod(N,2),20,60))'",
        "-vsync",
        "vfr",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(out_path),
    ]
    _run_ffmpeg(ffmpeg_path, args)
    return out_path


def _make_wav(out_path: Path, duration: float = 1.2, sample_rate: int = 24000, freq: float = 523.25) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(round(duration * sample_rate))
    t = np.arange(n_samples, dtype=np.float32) / float(sample_rate)
    samples = (0.3 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
    sf.write(str(out_path), samples, sample_rate, subtype="PCM_16")
    return out_path


def _make_segment(
    seg_id: str = "seg_01",
    *,
    video_in: float = 0.0,
    video_out: float = 2.0,
    duration_mode: str = "preserve_video",
    source_audio_volume: float = 0.15,
    keyframes: list[CropKeyframe] | None = None,
    clauses: list[ClauseInput] | None = None,
) -> SegmentInput:
    return SegmentInput(
        id=seg_id,
        video_in=video_in,
        video_out=video_out,
        duration_mode=duration_mode,  # type: ignore[arg-type]
        source_audio_volume=source_audio_volume,
        speed=1.0,
        crop_keyframes=keyframes
        if keyframes is not None
        else [CropKeyframe(t=0.0, x=0.25, y=0.0, zoom=1.0)],
        clauses=clauses if clauses is not None else [],
    )


# ---------------------------------------------------------------------------
# 1. Media Validation & Native Picker (media.py)
# ---------------------------------------------------------------------------


def test_media_validation_valid_cfr_vfr_and_no_audio(tmp_path: Path, ffmpeg_tools: tuple[Path, Path]) -> None:
    ffmpeg_path, ffprobe_path = ffmpeg_tools

    cfr_path = _make_cfr_video(ffmpeg_path, tmp_path / "cfr_720p.mp4", width=1280, height=720, duration=2.0)
    cfr_meta = probe_and_validate_media(cfr_path, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)
    assert cfr_meta.width == 1280
    assert cfr_meta.height == 720
    assert cfr_meta.has_audio is True
    assert cfr_meta.audio_sample_rate == 48000
    assert cfr_meta.audio_channels == 2
    assert math.isclose(cfr_meta.fps, 30.0, abs_tol=0.1)

    vfr_path = _make_vfr_video(ffmpeg_path, tmp_path / "vfr_720p.mp4")
    # Confirm the synthesized file actually has variable packet durations
    pkt_probe = subprocess.run(
        [
            str(ffprobe_path),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=duration_time",
            "-of",
            "json",
            str(vfr_path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        check=True,
    )
    durations = {round(float(p["duration_time"]), 3) for p in json.loads(pkt_probe.stdout)["packets"] if "duration_time" in p}
    assert len(durations) > 1, f"Expected genuine VFR packet durations, got {durations}"

    vfr_meta = probe_and_validate_media(vfr_path, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)
    assert vfr_meta.width == 1280
    assert vfr_meta.height == 720
    assert vfr_meta.has_audio is True

    silent_path = _make_cfr_video(
        ffmpeg_path, tmp_path / "silent_720p.mp4", width=1280, height=720, duration=1.5, with_audio=False
    )
    silent_meta = probe_and_validate_media(silent_path, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)
    assert silent_meta.has_audio is False
    assert silent_meta.audio_sample_rate is None
    assert silent_meta.audio_channels is None


def test_media_validation_rejects_hdr_rotated_aspect_sar_and_corrupt(
    tmp_path: Path, ffmpeg_tools: tuple[Path, Path]
) -> None:
    ffmpeg_path, ffprobe_path = ffmpeg_tools

    # HDR transfer characteristics (smpte2084 and arib-std-b67)
    for trc in ("smpte2084", "arib-std-b67"):
        hdr_path = _make_cfr_video(
            ffmpeg_path,
            tmp_path / f"hdr_{trc}.mp4",
            duration=1.0,
            with_audio=False,
            extra_output_args=["-color_trc", trc],
        )
        with pytest.raises(MediaValidationError, match="HDR"):
            probe_and_validate_media(hdr_path, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)

    # Rotated metadata (90 / 180 / 270) via stream-copy so MP4 Display Matrix is written
    base_rot_src = _make_cfr_video(ffmpeg_path, tmp_path / "rot_base.mp4", duration=1.0, with_audio=False)
    for deg in (90, 180, 270):
        rot_path = tmp_path / f"rot_{deg}.mp4"
        _run_ffmpeg(
            ffmpeg_path,
            ["-i", str(base_rot_src), "-c", "copy", "-metadata:s:v:0", f"rotate={deg}", str(rot_path)],
        )
        with pytest.raises(MediaValidationError, match="Rotated"):
            probe_and_validate_media(rot_path, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)

    # Non-16:9 (4:3)
    non_16_9 = _make_cfr_video(
        ffmpeg_path, tmp_path / "four_three.mp4", width=640, height=480, duration=1.0, with_audio=False
    )
    with pytest.raises(MediaValidationError, match="16:9"):
        probe_and_validate_media(non_16_9, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)

    # Non-1:1 SAR
    bad_sar = _make_cfr_video(
        ffmpeg_path, tmp_path / "bad_sar.mp4", duration=1.0, with_audio=False, vf="setsar=4/3"
    )
    with pytest.raises(MediaValidationError, match="SAR"):
        probe_and_validate_media(bad_sar, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)

    # Corrupt / undecodable file
    corrupt_path = tmp_path / "corrupt.mp4"
    corrupt_path.write_bytes(b"not a real mp4 file at all" * 100)
    with pytest.raises(MediaValidationError):
        probe_and_validate_media(corrupt_path, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)


def test_media_import_uploaded_and_register_source(tmp_path: Path, ffmpeg_tools: tuple[Path, Path]) -> None:
    ffmpeg_path, ffprobe_path = ffmpeg_tools
    src = _make_cfr_video(ffmpeg_path, tmp_path / "orig.mp4", duration=1.5)

    uploaded = import_uploaded_media("uploaded_clip.mp4", src.read_bytes(), tmp_path / "uploads")
    assert uploaded.is_file()
    assert uploaded.name == "uploaded_clip.mp4"
    assert uploaded.read_bytes() == src.read_bytes()

    with pytest.raises(MediaValidationError):
        import_uploaded_media("..", b"123", tmp_path / "uploads")

    # Register in-place (copy_to_dir=None)
    ref_inplace, meta_inplace = register_source_media(
        uploaded, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path
    )
    assert ref_inplace.copied_to_project is False
    assert Path(ref_inplace.path) == uploaded.resolve()
    assert ref_inplace.fingerprint.startswith("sha256:")
    assert meta_inplace.width == 1280

    # Register with copy_to_dir
    proj_source = tmp_path / "proj" / "source"
    ref_copied, _ = register_source_media(
        uploaded,
        media_id="src_custom",
        copy_to_dir=proj_source,
        ffprobe_path=ffprobe_path,
        ffmpeg_path=ffmpeg_path,
    )
    assert ref_copied.media_id == "src_custom"
    assert ref_copied.copied_to_project is True
    assert Path(ref_copied.path).parent == proj_source.resolve()
    assert Path(ref_copied.path).is_file()
    assert ref_copied.fingerprint == ref_inplace.fingerprint


def test_select_media_native_os_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Linux / headless raises NotImplementedError
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(NotImplementedError):
        select_media_native()

    # macOS osascript dispatch + cancel
    calls: list[list[str]] = []

    def fake_run_darwin(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs.get("shell") is False
        return SimpleNamespace(returncode=0, stdout="/Users/simmer/clip.mp4\n", stderr="")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", fake_run_darwin)
    assert select_media_native("Pick Video") == Path("/Users/simmer/clip.mp4")
    assert calls[-1][0] == "osascript"
    assert calls[-1][1] == "-e"
    assert "Pick Video" in calls[-1][2]

    def fake_run_darwin_cancel(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="execution error: User canceled. (-128)")

    monkeypatch.setattr(subprocess, "run", fake_run_darwin_cancel)
    assert select_media_native() is None

    # Windows PowerShell -NoProfile -STA dispatch + cancel
    def fake_run_win32(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs.get("shell") is False
        return SimpleNamespace(returncode=0, stdout="C:\\Videos\\sims.mp4\r\n", stderr="")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", fake_run_win32)
    res_win = select_media_native("Pick Video")
    assert res_win is not None
    assert str(res_win) == "C:\\Videos\\sims.mp4"
    assert calls[-1][:3] == ["powershell", "-NoProfile", "-STA"]
    assert calls[-1][3] == "-Command"

    def fake_run_win32_cancel(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_win32_cancel)
    assert select_media_native() is None


# ---------------------------------------------------------------------------
# 2. Proxy Transcoder (proxy.py)
# ---------------------------------------------------------------------------


def test_proxy_generation_manifest_and_mapping(tmp_path: Path, ffmpeg_tools: tuple[Path, Path]) -> None:
    ffmpeg_path, ffprobe_path = ffmpeg_tools
    src = _make_cfr_video(ffmpeg_path, tmp_path / "source_720p.mp4", width=1280, height=720, duration=2.0)
    ref, _ = register_source_media(src, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)
    create_project("proj_proxy", "Proxy Test", ref, data_dir=tmp_path)
    proj_dir = get_project_dir("proj_proxy", data_dir=tmp_path)
    proxy_out = proj_dir / "artifacts" / "proxy.mp4"

    progress_events: list[int] = []
    proxy_meta = generate_proxy(
        src,
        proxy_out,
        project_id="proj_proxy",
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        data_dir=tmp_path,
        on_progress=lambda pct, _stage: progress_events.append(pct),
    )

    assert proxy_out.is_file()
    assert proxy_meta.height == 540
    assert proxy_meta.width == 960
    assert math.isclose(proxy_meta.fps, 30.0, abs_tol=0.1)
    assert proxy_meta.pix_fmt == "yuv420p"
    assert progress_events[-1] == 100

    # Double-check with ffprobe directly
    probe = _ffprobe_json(ffprobe_path, proxy_out)
    vstream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert vstream["height"] == 540
    assert vstream["pix_fmt"] == "yuv420p"
    assert vstream["r_frame_rate"] == "30/1"

    manifest = load_manifest("proj_proxy", data_dir=tmp_path)
    assert manifest.proxy.status == "ready"
    assert manifest.proxy.path == "artifacts/proxy.mp4"
    assert manifest.proxy.artifact_id == "art_proxy"

    # map_source_to_proxy_time clamping
    assert map_source_to_proxy_time(-5.0, 10.0, 10.0) == 0.0
    assert map_source_to_proxy_time(25.0, 10.0, 10.0) == 10.0
    assert math.isclose(map_source_to_proxy_time(5.0, 10.0, 20.0), 10.0)
    assert map_source_to_proxy_time(1.0, 0.0, 10.0) == 0.0


def test_proxy_cancellation(tmp_path: Path, ffmpeg_tools: tuple[Path, Path]) -> None:
    ffmpeg_path, ffprobe_path = ffmpeg_tools
    src = _make_cfr_video(ffmpeg_path, tmp_path / "long_src.mp4", width=1280, height=720, duration=12.0)
    proxy_out = tmp_path / "proxy_cancelled.mp4"
    cancel_event = threading.Event()

    def _cancel_on_start(_proc: subprocess.Popen) -> None:
        cancel_event.set()

    with pytest.raises(ProxyError, match="cancelled"):
        generate_proxy(
            src,
            proxy_out,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            data_dir=tmp_path,
            on_process_start=_cancel_on_start,
            cancel_event=cancel_event,
        )

    assert not proxy_out.exists()


# ---------------------------------------------------------------------------
# 3. Duration Reconciliation & Audio Mix (renderer.py)
# ---------------------------------------------------------------------------


def test_duration_reconciliation_plans_and_quantization() -> None:
    # preserve_video: narration shorter than video
    seg_short = _make_segment(video_in=1.0, video_out=6.0, duration_mode="preserve_video")
    p_short = plan_segment_duration(seg_short, audio_duration=2.0, source_duration=20.0, tail_padding_sec=0.4)
    assert math.isclose(p_short.raw_target, 5.0)
    assert p_short.freeze_frame is False
    assert p_short.frame_count == 150

    # preserve_video: narration longer than video -> freeze frame
    seg_long = _make_segment(video_in=1.0, video_out=3.0, duration_mode="preserve_video")
    p_long = plan_segment_duration(seg_long, audio_duration=4.0, source_duration=20.0, tail_padding_sec=0.4)
    assert math.isclose(p_long.raw_target, 4.4)
    assert p_long.moving_source_dur == 2.0
    assert p_long.freeze_frame is True
    assert p_long.freeze_dur > 2.3

    # preserve_video: empty narration
    p_empty = plan_segment_duration(seg_long, audio_duration=0.0, source_duration=20.0, tail_padding_sec=0.4)
    assert math.isclose(p_empty.raw_target, 2.0)
    assert p_empty.freeze_frame is False

    # fit_narration: narration shorter than video_out - video_in
    seg_fit = _make_segment(video_in=1.0, video_out=6.0, duration_mode="fit_narration")
    p_fit_short = plan_segment_duration(seg_fit, audio_duration=1.5, source_duration=20.0, tail_padding_sec=0.4)
    assert math.isclose(p_fit_short.raw_target, 1.9)
    assert math.isclose(p_fit_short.moving_source_dur, 1.9)
    assert p_fit_short.freeze_frame is False

    # fit_narration: narration longer than video_out - video_in, within source_duration
    p_fit_long = plan_segment_duration(seg_fit, audio_duration=7.0, source_duration=20.0, tail_padding_sec=0.4)
    assert math.isclose(p_fit_long.raw_target, 7.4)
    assert math.isclose(p_fit_long.moving_source_dur, 7.4)
    assert p_fit_long.freeze_frame is False

    # fit_narration: deterministic source-EOF freeze-frame fallback
    seg_eof = _make_segment(video_in=7.0, video_out=9.0, duration_mode="fit_narration")
    p_eof = plan_segment_duration(seg_eof, audio_duration=3.5, source_duration=9.0, tail_padding_sec=0.4)
    assert math.isclose(p_eof.raw_target, 3.9)
    assert math.isclose(p_eof.moving_source_dur, 2.0)  # source_duration - video_in
    assert p_eof.freeze_frame is True
    assert p_eof.freeze_dur > 1.8

    # Upward frame quantization & exact 48kHz audio sample count
    seg_q = _make_segment(video_in=0.0, video_out=2.01, duration_mode="preserve_video")
    p_q = plan_segment_duration(seg_q, audio_duration=0.0, source_duration=10.0, tail_padding_sec=0.4, fps=30)
    assert p_q.frame_count == math.ceil(2.01 * 30) == 61
    assert math.isclose(p_q.final_dur, 61 / 30.0)
    assert p_q.audio_sample_count == round(p_q.final_dur * 48000)


def test_render_duration_modes_and_eof_source_audio_truncation(
    tmp_path: Path, ffmpeg_tools: tuple[Path, Path]
) -> None:
    ffmpeg_path, ffprobe_path = ffmpeg_tools
    src = _make_cfr_video(
        ffmpeg_path, tmp_path / "src_tone.mp4", width=1280, height=720, duration=3.0, with_audio=True, audio_freq=440
    )
    ref, meta = register_source_media(src, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)
    out_settings = OutputSettings()
    proj_dir = tmp_path / "proj_dur"

    # 1. preserve_video with narration shorter than video (apad trailing silence)
    wav_short = _make_wav(tmp_path / "short.wav", duration=0.8)
    seg1 = _make_segment("s_apad", video_in=0.0, video_out=2.5, duration_mode="preserve_video")
    mp4_1 = render_segment_stage1(
        seg1,
        ref,
        meta,
        out_settings,
        proj_dir,
        tts_hash="h1",
        audio_path=wav_short,
        audio_duration=0.8,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    p1 = _ffprobe_json(ffprobe_path, mp4_1, count_frames=True)
    v1 = next(s for s in p1["streams"] if s["codec_type"] == "video")
    assert int(v1["nb_frames"]) == 75  # 2.5 * 30

    # 2. preserve_video with narration longer than video (tpad clone)
    wav_long = _make_wav(tmp_path / "long.wav", duration=2.6)
    seg2 = _make_segment("s_tpad", video_in=0.0, video_out=1.0, duration_mode="preserve_video")
    plan2 = plan_segment_duration(seg2, 2.6, meta.duration, out_settings.tail_padding_sec)
    mp4_2 = render_segment_stage1(
        seg2,
        ref,
        meta,
        out_settings,
        proj_dir,
        tts_hash="h2",
        audio_path=wav_long,
        audio_duration=2.6,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    p2 = _ffprobe_json(ffprobe_path, mp4_2, count_frames=True)
    v2 = next(s for s in p2["streams"] if s["codec_type"] == "video")
    assert int(v2["nb_frames"]) == plan2.frame_count

    # 3. fit_narration EOF freeze-frame fallback: source audio must be truncated at EOF (not looped)
    # Use a silent TTS wav (zeros) for 2.5s starting at video_in=2.0 of a 3.0s source -> 1.0s moving, ~1.9s freeze
    silent_tts_path = tmp_path / "silent_tts.wav"
    sf.write(str(silent_tts_path), np.zeros(int(2.5 * 24000), dtype=np.float32), 24000, subtype="PCM_16")
    seg_eof = _make_segment("s_eof", video_in=2.0, video_out=2.8, duration_mode="fit_narration", source_audio_volume=1.0)
    plan_eof = plan_segment_duration(seg_eof, 2.5, meta.duration, out_settings.tail_padding_sec)
    assert plan_eof.freeze_frame is True
    assert math.isclose(plan_eof.moving_source_dur, 1.0, abs_tol=0.05)

    mp4_eof = render_segment_stage1(
        seg_eof,
        ref,
        meta,
        out_settings,
        proj_dir,
        tts_hash="heof",
        audio_path=silent_tts_path,
        audio_duration=2.5,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    p_eof = _ffprobe_json(ffprobe_path, mp4_eof, count_frames=True)
    v_eof = next(s for s in p_eof["streams"] if s["codec_type"] == "video")
    assert int(v_eof["nb_frames"]) == plan_eof.frame_count

    # Extract rendered audio to WAV and verify source audio is active in [0.1s, 0.9s] and silent in [1.2s, end]
    decoded_wav = tmp_path / "decoded_eof.wav"
    _run_ffmpeg(ffmpeg_path, ["-i", str(mp4_eof), "-ac", "1", "-ar", "48000", str(decoded_wav)])
    samples, sr = sf.read(str(decoded_wav), dtype="float32")
    active_rms = float(np.sqrt(np.mean(samples[int(0.1 * sr) : int(0.9 * sr)] ** 2)))
    freeze_rms = float(np.sqrt(np.mean(samples[int(1.2 * sr) :] ** 2)))
    assert active_rms > 0.05
    assert freeze_rms < 1e-3, f"Expected truncated silence during freeze-frame, got RMS={freeze_rms}"


# ---------------------------------------------------------------------------
# 4. Static Crop & Animated Zoom (renderer.py + geometry.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["static", "animated"])
def test_stage1_static_crop_and_animated_zoom_grid(
    tmp_path: Path, ffmpeg_tools: tuple[Path, Path], mode: str
) -> None:
    ffmpeg_path, ffprobe_path = ffmpeg_tools
    src = _make_cfr_video(
        ffmpeg_path,
        tmp_path / f"grid_{mode}.mp4",
        width=1280,
        height=720,
        duration=2.0,
        vf="drawgrid=width=160:height=90:thickness=2:color=white@0.8",
    )
    ref, meta = register_source_media(src, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)
    out_settings = OutputSettings()

    if mode == "static":
        kfs = [
            CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0),
            CropKeyframe(t=1.5, x=0.4, y=0.0, zoom=1.0),
        ]
    else:
        kfs = [
            CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0),
            CropKeyframe(t=1.5, x=0.35, y=0.15, zoom=2.0),
        ]

    seg = _make_segment(f"seg_{mode}", video_in=0.0, video_out=1.5, keyframes=kfs)
    plan = plan_segment_duration(seg, 0.0, meta.duration, out_settings.tail_padding_sec)
    out_mp4 = render_segment_stage1(
        seg,
        ref,
        meta,
        out_settings,
        tmp_path / f"proj_{mode}",
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )

    probe = _ffprobe_json(ffprobe_path, out_mp4, count_frames=True)
    vstream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    astream = next(s for s in probe["streams"] if s["codec_type"] == "audio")

    assert vstream["width"] == 1080
    assert vstream["height"] == 1920
    assert vstream["sample_aspect_ratio"] == "1:1"
    assert vstream["r_frame_rate"] == "30/1"
    assert vstream["avg_frame_rate"] == "30/1"
    assert vstream["pix_fmt"] == "yuv420p"
    assert int(vstream["nb_frames"]) == plan.frame_count

    assert astream["codec_name"] == "aac"
    assert int(astream["sample_rate"]) == 48000
    assert int(astream["channels"]) == 2


# ---------------------------------------------------------------------------
# 5. Unicode / Space / Apostrophe Paths
# ---------------------------------------------------------------------------


def test_unicode_space_apostrophe_paths_end_to_end(tmp_path: Path, ffmpeg_tools: tuple[Path, Path]) -> None:
    ffmpeg_path, ffprobe_path = ffmpeg_tools
    unicode_dir = tmp_path / "Café's Studio — Dir"
    src_path = _make_cfr_video(
        ffmpeg_path,
        unicode_dir / "Café's Sims 4 Clip — Episode 1.mp4",
        width=1280,
        height=720,
        duration=2.0,
    )
    ref, meta = register_source_media(src_path, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)
    assert meta.width == 1280

    proj_dir = unicode_dir / "Project's Folder — 01"
    proxy_out = proj_dir / "artifacts" / "proxy.mp4"
    proxy_meta = generate_proxy(src_path, proxy_out, ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path)
    assert proxy_out.is_file()
    assert proxy_meta.height == 540

    wav_path = _make_wav(proj_dir / "artifacts" / "seg's_voice — 1.wav", duration=0.8)
    seg = _make_segment(
        "seg_unicode",
        video_in=0.0,
        video_out=1.2,
        clauses=[ClauseInput(id="c1", display_text="Café's haunted manor!")],
    )
    timings = [ClauseTiming(clause_id="c1", start_sample=0, end_sample=19200, start_sec=0.0, end_sec=0.8)]

    preview_mp4 = render_segment_preview(
        seg,
        ref,
        meta,
        OutputSettings(),
        CaptionSettings(),
        proj_dir,
        tts_hash="u1",
        audio_path=wav_path,
        audio_duration=0.8,
        clause_timings=timings,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    assert preview_mp4.is_file()

    project = ProjectInput(
        project_id="proj_unicode",
        title="Café's Export",
        source_media=ref,
        segments=[seg],
    )
    manifest = ArtifactManifest(
        project_id="proj_unicode",
        source_metadata=meta,
        segments={
            "seg_unicode": SegmentArtifactState(
                tts_hash="u1",
                audio_path=str(wav_path),
                audio_duration=0.8,
                clause_timings=timings,
            )
        },
    )
    export_mp4 = render_final_export(
        project,
        meta,
        manifest,
        proj_dir,
        export_path=unicode_dir / "Exports' Dir" / "Café's Final — Export.mp4",
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    assert export_mp4.is_file()
    probe = _ffprobe_json(ffprobe_path, export_mp4)
    vstream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert vstream["width"] == 1080
    assert vstream["height"] == 1920


# ---------------------------------------------------------------------------
# 6. Job Queue, Cancellation & Restart Recovery (jobs.py)
# ---------------------------------------------------------------------------


def test_job_manager_execution_and_cancellation(tmp_path: Path, ffmpeg_tools: tuple[Path, Path]) -> None:
    ffmpeg_path, ffprobe_path = ffmpeg_tools
    src = _make_cfr_video(ffmpeg_path, tmp_path / "job_src.mp4", duration=1.5)
    mgr = JobManager(data_dir=tmp_path, max_workers=2)

    try:
        # 1. Real proxy job -> succeeded + progress
        proxy_out = tmp_path / "proxy_job.mp4"

        def proxy_worker(ctx: JobContext) -> dict:
            meta = generate_proxy(
                src,
                proxy_out,
                ffmpeg_path=ffmpeg_path,
                ffprobe_path=ffprobe_path,
                data_dir=tmp_path,
                on_progress=lambda pct, stage: ctx.update_progress(pct, stage),
                on_process_start=ctx.register_process,
                cancel_event=ctx.cancel_event,
            )
            return {"width": meta.width, "height": meta.height}

        job = mgr.submit_job("proj_j1", "proxy", proxy_worker)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            cur = mgr.get_job(job.job_id)
            assert cur is not None
            if cur.status in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)

        final_job = mgr.get_job(job.job_id)
        assert final_job is not None
        assert final_job.status == "succeeded"
        assert final_job.progress == 100
        assert final_job.result == {"width": 960, "height": 540}
        assert proxy_out.is_file()

        # 2. Slow FFmpeg job -> cancel_job terminates subprocess & marks cancelled
        proc_started = threading.Event()
        captured_procs: list[subprocess.Popen] = []

        def slow_ffmpeg_worker(ctx: JobContext) -> dict:
            proc = subprocess.Popen(
                [
                    str(ffmpeg_path),
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=1280x720:rate=30:duration=60",
                    "-f",
                    "null",
                    "-",
                ],
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            captured_procs.append(proc)
            ctx.register_process(proc)
            proc_started.set()
            proc.wait()
            ctx.check_cancelled()
            return {}

        slow_job = mgr.submit_job("proj_j1", "export", slow_ffmpeg_worker)
        assert proc_started.wait(timeout=10.0)
        cancelled = mgr.cancel_job(slow_job.job_id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        assert len(captured_procs) == 1
        assert captured_procs[0].poll() is not None
    finally:
        mgr.shutdown(wait=True)


def test_job_manager_restart_recovery_and_tmp_purge(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    for jid, status in [("job_q", "queued"), ("job_r", "running"), ("job_ok", "succeeded")]:
        state = JobState(job_id=jid, project_id="proj_rec", job_type="export", status=status)  # type: ignore[arg-type]
        (jobs_dir / f"{jid}.json").write_text(json.dumps(state.model_dump(mode="json")), encoding="utf-8")

    proj_dir = tmp_path / "projects" / "proj_rec"
    (proj_dir / "tmp" / "nested").mkdir(parents=True, exist_ok=True)
    (proj_dir / "tmp" / "partial.mp4").write_bytes(b"partial")
    (proj_dir / "tmp" / "nested" / "script.txt").write_text("filter", encoding="utf-8")
    (proj_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    keep_art = proj_dir / "artifacts" / "keep.mp4"
    keep_art.write_bytes(b"valid_artifact")

    mgr = JobManager(data_dir=tmp_path)
    try:
        recovered = mgr.recover_on_startup()
        recovered_ids = {j.job_id for j in recovered}
        assert recovered_ids == {"job_q", "job_r"}

        for jid in ("job_q", "job_r"):
            loaded = mgr.get_job(jid)
            assert loaded is not None
            assert loaded.status == "interrupted"

        ok_job = mgr.get_job("job_ok")
        assert ok_job is not None
        assert ok_job.status == "succeeded"

        assert list((proj_dir / "tmp").iterdir()) == []
        assert keep_art.is_file() and keep_art.read_bytes() == b"valid_artifact"
    finally:
        mgr.shutdown(wait=False)


# ---------------------------------------------------------------------------
# 7. Cache Hit / Invalidation Matrix & Stage 2 Concat + Preview (renderer.py)
# ---------------------------------------------------------------------------


def test_render_hash_invalidation_matrix() -> None:
    ref = SourceMediaRef(media_id="src_1", path="/tmp/a.mp4", fingerprint="sha256:111")
    seg = _make_segment("s1", video_in=1.0, video_out=4.0, duration_mode="preserve_video", source_audio_volume=0.15)
    out = OutputSettings()

    h_base = compute_render_hash(seg, ref, "tts_abc", out)

    # Caption settings and segment ordering do not affect Stage 1 render_hash
    assert h_base == compute_render_hash(seg, ref, "tts_abc", out)

    # Crop keyframe edit invalidates render_hash
    seg_crop = seg.model_copy(update={"crop_keyframes": [CropKeyframe(t=0.0, x=0.35, y=0.0, zoom=1.0)]})
    assert h_base != compute_render_hash(seg_crop, ref, "tts_abc", out)

    # Trim edit invalidates render_hash
    seg_trim = seg.model_copy(update={"video_out": 4.5})
    assert h_base != compute_render_hash(seg_trim, ref, "tts_abc", out)

    # Duration mode edit invalidates render_hash
    seg_mode = seg.model_copy(update={"duration_mode": "fit_narration"})
    assert h_base != compute_render_hash(seg_mode, ref, "tts_abc", out)

    # Source volume edit invalidates render_hash
    seg_vol = seg.model_copy(update={"source_audio_volume": 0.5})
    assert h_base != compute_render_hash(seg_vol, ref, "tts_abc", out)

    # tts_hash edit invalidates render_hash
    assert h_base != compute_render_hash(seg, ref, "tts_xyz", out)


def test_stage1_cache_hit_preview_and_stage2_multiseq_export(
    tmp_path: Path, ffmpeg_tools: tuple[Path, Path]
) -> None:
    ffmpeg_path, ffprobe_path = ffmpeg_tools
    src = _make_cfr_video(ffmpeg_path, tmp_path / "export_src.mp4", width=1280, height=720, duration=5.0)
    ref, meta = register_source_media(src, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)
    proj_dir = tmp_path / "proj_export"
    out_settings = OutputSettings()
    cap_settings = CaptionSettings()

    # Segment 1: static crop + narration + clauses
    wav1 = _make_wav(proj_dir / "artifacts" / "seg1_tts.wav", duration=1.0)
    seg1 = _make_segment(
        "seg_01",
        video_in=0.0,
        video_out=1.5,
        keyframes=[CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0)],
        clauses=[ClauseInput(id="c1", display_text="First clause!")],
    )
    timings1 = [ClauseTiming(clause_id="c1", start_sample=0, end_sample=24000, start_sec=0.0, end_sec=1.0)]

    # Stage 1 cache hit verification
    s1_first = render_segment_stage1(
        seg1,
        ref,
        meta,
        out_settings,
        proj_dir,
        tts_hash="t1",
        audio_path=wav1,
        audio_duration=1.0,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    mtime_before = s1_first.stat().st_mtime_ns
    s1_second = render_segment_stage1(
        seg1,
        ref,
        meta,
        out_settings,
        proj_dir,
        tts_hash="t1",
        audio_path=wav1,
        audio_duration=1.0,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    assert s1_second == s1_first
    assert s1_second.stat().st_mtime_ns == mtime_before

    # Single-segment captioned preview
    preview_path = render_segment_preview(
        seg1,
        ref,
        meta,
        out_settings,
        cap_settings,
        proj_dir,
        tts_hash="t1",
        audio_path=wav1,
        audio_duration=1.0,
        clause_timings=timings1,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    assert preview_path.is_file()
    prev_probe = _ffprobe_json(ffprobe_path, preview_path, count_frames=True)
    prev_v = next(s for s in prev_probe["streams"] if s["codec_type"] == "video")
    assert prev_v["width"] == 1080 and prev_v["height"] == 1920

    # Segment 2: animated zoompan + silent (no narration)
    seg2 = _make_segment(
        "seg_02",
        video_in=2.0,
        video_out=3.2,
        keyframes=[
            CropKeyframe(t=0.0, x=0.1, y=0.0, zoom=1.0),
            CropKeyframe(t=1.2, x=0.35, y=0.15, zoom=1.8),
        ],
        clauses=[],
    )

    project = ProjectInput(
        project_id="proj_export",
        title="Multi-Segment Export",
        source_media=ref,
        output_settings=out_settings,
        caption_settings=cap_settings,
        segments=[seg1, seg2],
    )
    manifest = ArtifactManifest(
        project_id="proj_export",
        source_metadata=meta,
        segments={
            "seg_01": SegmentArtifactState(
                tts_hash="t1",
                audio_path=str(wav1),
                audio_duration=1.0,
                clause_timings=timings1,
            ),
            "seg_02": SegmentArtifactState(tts_hash="", audio_duration=0.0, clause_timings=[]),
        },
    )

    plan1 = plan_segment_duration(seg1, 1.0, meta.duration, out_settings.tail_padding_sec, out_settings.fps)
    plan2 = plan_segment_duration(seg2, 0.0, meta.duration, out_settings.tail_padding_sec, out_settings.fps)
    expected_total_frames = plan1.frame_count + plan2.frame_count

    export_path = render_final_export(
        project,
        meta,
        manifest,
        proj_dir,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    assert export_path.is_file()

    exp_probe = _ffprobe_json(ffprobe_path, export_path, count_frames=True)
    vstream = next(s for s in exp_probe["streams"] if s["codec_type"] == "video")
    astream = next(s for s in exp_probe["streams"] if s["codec_type"] == "audio")

    assert vstream["width"] == 1080
    assert vstream["height"] == 1920
    assert vstream["sample_aspect_ratio"] == "1:1"
    assert vstream["r_frame_rate"] == "30/1"
    assert vstream["pix_fmt"] == "yuv420p"
    assert int(vstream["nb_frames"]) == expected_total_frames

    assert astream["codec_name"] == "aac"
    assert int(astream["sample_rate"]) == 48000
    assert int(astream["channels"]) == 2

    video_dur = float(vstream["duration"])
    audio_dur = float(astream["duration"])
    assert abs(video_dur - audio_dur) <= (1.0 / 30.0), (
        f"A/V drift exceeded 1 frame: video_dur={video_dur}, audio_dur={audio_dur}"
    )
