"""Runtime FFmpeg synthetic media generators and ffprobe helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

FFMPEG_BIN = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FFPROBE_BIN = shutil.which("ffprobe") or "/usr/bin/ffprobe"


def _run_ffmpeg(args: list[str]) -> None:
    cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed ({proc.returncode}): {' '.join(cmd)}\nstderr: {proc.stderr}"
        )


def probe_media(path: str | Path) -> dict[str, Any]:
    """Run ffprobe on a media file and return parsed JSON format + streams."""
    target = Path(path)
    cmd = [
        FFPROBE_BIN,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed ({proc.returncode}) for {target}: {proc.stderr}"
        )
    return json.loads(proc.stdout)


def generate_sdr_cfr_mp4(
    path: str | Path,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    duration: float = 4.0,
    with_audio: bool = True,
) -> Path:
    """Generate an SDR CFR 16:9 MP4 with testsrc2 video and optional 48kHz stereo sine audio."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
    ]
    if with_audio:
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration}",
        ]
    args += [
        "-vf",
        "setsar=1,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-vsync",
        "cfr",
    ]
    if with_audio:
        args += ["-c:a", "aac", "-ar", "48000", "-ac", "2"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", str(out)]
    _run_ffmpeg(args)
    return out


def generate_sdr_vfr_mp4(
    path: str | Path,
    width: int = 1280,
    height: int = 720,
    duration: float = 4.0,
) -> Path:
    """Generate a true VFR 16:9 MP4 by varying frame timestamps with -vsync vfr."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Vary PTS intervals so avg_frame_rate != r_frame_rate and packet pts_time is non-uniform
    args = [
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate=30:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=523:sample_rate=48000:duration={duration}",
        "-vf",
        "setsar=1,setpts='if(eq(mod(N,2),0),PTS*0.75,PTS*1.25)',format=yuv420p",
        "-vsync",
        "vfr",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "24",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(out),
    ]
    _run_ffmpeg(args)
    return out


def generate_silent_mp4(
    path: str | Path,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    duration: float = 4.0,
) -> Path:
    """Generate an SDR CFR 16:9 MP4 with no audio stream."""
    return generate_sdr_cfr_mp4(
        path,
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        with_audio=False,
    )


def generate_hdr_pq_mp4(
    path: str | Path,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    duration: float = 2.0,
) -> Path:
    """Generate an HDR PQ (smpte2084) 16:9 MP4 for rejection testing."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
        "-vf",
        "setsar=1,format=yuv420p10le",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p10le",
        "-color_primaries",
        "bt2020",
        "-color_trc",
        "smpte2084",
        "-colorspace",
        "bt2020nc",
        "-x264-params",
        "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
        "-an",
        str(out),
    ]
    _run_ffmpeg(args)
    return out


def generate_rotated_mp4(
    path: str | Path,
    rotation: int = 90,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    duration: float = 2.0,
) -> Path:
    """Generate a 16:9 MP4 with non-zero rotation displaymatrix/metadata."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Try -display_rotation first (FFmpeg 6+), fallback to -metadata:s:v:0 rotate=
    base_args = [
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
        "-vf",
        "setsar=1,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-an",
    ]
    tmp_out = out.with_suffix(".unrotated.mp4")
    _run_ffmpeg([*base_args, str(tmp_out)])
    try:
        _run_ffmpeg(
            [
                "-i",
                str(tmp_out),
                "-c",
                "copy",
                "-metadata:s:v:0",
                f"rotate={rotation}",
                str(out),
            ]
        )
    finally:
        if tmp_out.exists():
            tmp_out.unlink()
    return out


def generate_non_16_9_mp4(
    path: str | Path,
    width: int = 1440,
    height: int = 1080,
    fps: int = 30,
    duration: float = 2.0,
) -> Path:
    """Generate a 4:3 (non-16:9) SDR CFR MP4 with square pixels."""
    return generate_sdr_cfr_mp4(
        path,
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        with_audio=True,
    )


def generate_anamorphic_sar_mp4(
    path: str | Path,
    width: int = 1440,
    height: int = 1080,
    fps: int = 30,
    duration: float = 2.0,
) -> Path:
    """Generate an MP4 with non-square pixel aspect ratio (SAR 4:3 != 1:1)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
        "-vf",
        "setsar=4/3,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(out),
    ]
    _run_ffmpeg(args)
    return out


def generate_corrupt_mp4(path: str | Path) -> Path:
    """Write invalid/corrupt bytes to a .mp4 path for error handling tests."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"CORRUPT_MP4_HEADER_BYTES\x00\xff\xfe\xfd" * 64)
    return out
