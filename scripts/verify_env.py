"""Milestone 0 smoke test: verify FFmpeg libass, Kokoro CPU TTS, and 1080x1920 30fps ASS burn-in offline."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

# Ensure repo root is importable when invoked as `python scripts/verify_env.py`
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.bootstrap import (  # noqa: E402
    ensure_kokoro_models,
    get_data_dir,
    verify_ffmpeg,
    verify_font,
)


def apply_pronunciations(text: str, pronunciations: dict[str, str]) -> str:
    """Single-pass, case-insensitive, Unicode word-boundary pronunciation replacement."""
    if not pronunciations:
        return text
    keys = sorted(pronunciations.keys(), key=len, reverse=True)
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)",
        flags=re.IGNORECASE,
    )
    lookup = {k.lower(): v for k, v in pronunciations.items()}
    return pattern.sub(lambda m: lookup[m.group(0).lower()], text)


def format_ass_timestamp(seconds: float) -> str:
    cs_total = max(0, int(round(seconds * 100)))
    hours, rem = divmod(cs_total, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def escape_ffmpeg_filter_path(path: Path) -> str:
    """Escape a filesystem path for FFmpeg's filtergraph key=value syntax."""
    s = path.resolve().as_posix()
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    return s


def main() -> int:
    print("=== Sims 4 Vertical Video Studio — Milestone 0 Environment Verification ===")

    # 1. Bootstrap verification strictly in offline mode (zero network calls)
    data_dir = get_data_dir()
    font_path = verify_font(REPO_ROOT)
    ffmpeg_path, ffprobe_path = verify_ffmpeg(data_dir=data_dir, offline=True)
    model_path, voices_path = ensure_kokoro_models(data_dir=data_dir, offline=True)
    print(f"[PASS] Offline bootstrap assets verified (data_dir={data_dir})")

    # 2. Kokoro CPU TTS + custom pronunciation replacement
    kokoro = Kokoro(str(model_path), str(voices_path))
    providers = kokoro.sess.get_providers()
    if "CPUExecutionProvider" not in providers:
        raise RuntimeError(f"Expected CPUExecutionProvider in Kokoro session, got: {providers}")
    print(f"[PASS] Kokoro ONNX initialized on CPU (providers={providers})")

    display_text = 'Lord Vaelen — "Windenburg"'
    spoken_input = "Lord Vaelen refused to leave Windenburg."
    pronunciations = {"Vaelen": "Vay-len", "Windenburg": "Vin-den-burg"}
    spoken_text = apply_pronunciations(spoken_input, pronunciations)
    if "Vay-len" not in spoken_text or "Vin-den-burg" not in spoken_text:
        raise RuntimeError(f"Pronunciation replacement failed: {spoken_text!r}")

    samples, sample_rate = kokoro.create(spoken_text, voice="bf_emma", speed=1.0, lang="en-gb")
    if sample_rate != 24000:
        raise RuntimeError(f"Expected 24000 Hz sample rate from Kokoro, got {sample_rate}")
    audio_np = np.asarray(samples, dtype=np.float32)
    if audio_np.size == 0:
        raise RuntimeError("Kokoro returned empty audio buffer")

    duration_sec = max(1.0, float(audio_np.shape[0]) / float(sample_rate))
    print(
        f"[PASS] Kokoro CPU TTS synthesized {audio_np.shape[0]} samples @ {sample_rate} Hz "
        f"({duration_sec:.2f}s) for {spoken_text!r}"
    )

    with tempfile.TemporaryDirectory(prefix="sims_m0_verify_") as tmp:
        tmp_dir = Path(tmp)
        wav_path = tmp_dir / "tts.wav"
        ass_path = tmp_dir / "captions.ass"
        mp4_path = tmp_dir / "smoke_test_1080x1920.mp4"

        sf.write(str(wav_path), audio_np, sample_rate, subtype="PCM_16")

        # 3. Write UTF-8 .ass subtitle file using bundled Montserrat ExtraBold
        end_ts = format_ass_timestamp(duration_sec)
        ass_content = "\n".join(
            [
                "[Script Info]",
                "ScriptType: v4.00+",
                "PlayResX: 1080",
                "PlayResY: 1920",
                "WrapStyle: 2",
                "ScaledBorderAndShadow: yes",
                "",
                "[V4+ Styles]",
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
                "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
                "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
                "Style: Default,Montserrat ExtraBold,64,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
                "0,0,0,0,100,100,0,0,1,5,0,2,60,60,340,1",
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                f"Dialogue: 0,0:00:00.00,{end_ts},Default,,0,0,0,,{display_text}",
                "",
            ]
        )
        ass_path.write_text(ass_content, encoding="utf-8")

        # 4. Burn in .ass onto a 1080x1920 30fps test clip with 48kHz stereo AAC
        ass_escaped = escape_ffmpeg_filter_path(ass_path)
        fontsdir_escaped = escape_ffmpeg_filter_path(font_path.parent)
        vf_filter = f"ass=filename='{ass_escaped}':fontsdir='{fontsdir_escaped}',setsar=1"

        ffmpeg_cmd = [
            str(ffmpeg_path),
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=1080x1920:rate=30:duration={duration_sec:.3f}",
            "-i",
            str(wav_path),
            "-vf",
            vf_filter,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(mp4_path),
        ]
        ff_res = subprocess.run(
            ffmpeg_cmd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if ff_res.returncode != 0:
            raise RuntimeError(f"FFmpeg burn-in failed (rc={ff_res.returncode}):\n{ff_res.stderr}")
        print("[PASS] FFmpeg rendered 1080x1920 30fps MP4 with UTF-8 .ass subtitle burn-in")

        # 5. Verify output streams with ffprobe
        probe_cmd = [
            str(ffprobe_path),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(mp4_path),
        ]
        probe_res = subprocess.run(
            probe_cmd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if probe_res.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {probe_res.stderr}")

        probe_data = json.loads(probe_res.stdout)
        streams = probe_data.get("streams", [])
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

        if len(video_streams) != 1 or len(audio_streams) != 1:
            raise RuntimeError(
                f"Expected 1 video and 1 audio stream, got {len(video_streams)}V/{len(audio_streams)}A"
            )

        v = video_streams[0]
        a = audio_streams[0]

        assert v.get("codec_name") == "h264", f"Unexpected video codec: {v.get('codec_name')}"
        assert v.get("width") == 1080 and v.get("height") == 1920, (
            f"Unexpected dimensions: {v.get('width')}x{v.get('height')}"
        )
        assert v.get("pix_fmt") == "yuv420p", f"Unexpected pix_fmt: {v.get('pix_fmt')}"
        assert v.get("r_frame_rate") == "30/1", f"Unexpected r_frame_rate: {v.get('r_frame_rate')}"
        assert v.get("sample_aspect_ratio") == "1:1", (
            f"Unexpected SAR: {v.get('sample_aspect_ratio')}"
        )

        assert a.get("codec_name") == "aac", f"Unexpected audio codec: {a.get('codec_name')}"
        assert str(a.get("sample_rate")) == "48000", (
            f"Unexpected audio sample_rate: {a.get('sample_rate')}"
        )
        assert a.get("channels") == 2, f"Unexpected audio channels: {a.get('channels')}"

        print(
            "[PASS] ffprobe verified output: "
            f"video={v['codec_name']} {v['width']}x{v['height']} {v['pix_fmt']} {v['r_frame_rate']} SAR {v['sample_aspect_ratio']}, "
            f"audio={a['codec_name']} {a['sample_rate']}Hz {a['channels']}ch"
        )

    print("=== ALL MILESTONE 0 CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        raise SystemExit(1)
