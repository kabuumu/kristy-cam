"""Kokoro CPU singleton, pronunciation replacer, and sample-accurate WAV synthesis."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

from backend.bootstrap import ensure_kokoro_models, get_data_dir
from backend.schemas import (
    CaptionArtifactState,
    CaptionClip,
    CaptionTrack,
    ClauseInput,
    ClauseTiming,
    SegmentInput,
    TimelineTrack,
)

SAMPLE_RATE = 24000

_kokoro_lock = threading.Lock()
_kokoro_instance: Kokoro | None = None
_kokoro_paths: tuple[Path, Path] | None = None


@dataclass(frozen=True)
class SegmentTTSResult:
    """Result of synthesizing a segment's clauses into sample-accurate PCM_16 WAVs."""

    tts_hash: str
    sample_rate: int
    total_samples: int
    audio_duration: float
    clause_timings: list[ClauseTiming]
    audio_path: Path | None
    clause_wav_paths: list[Path]


def _verify_cpu_provider(kokoro: Any) -> None:
    providers = kokoro.sess.get_providers()
    if "CPUExecutionProvider" not in providers:
        raise RuntimeError(f"Expected CPUExecutionProvider in Kokoro session, got: {providers}")


def get_kokoro(data_dir: Path | None = None, offline: bool = False) -> Kokoro:
    """Return the process-wide CPU Kokoro ONNX singleton."""
    global _kokoro_instance, _kokoro_paths
    with _kokoro_lock:
        resolved_dir = data_dir if data_dir is not None else get_data_dir()
        model_path, voices_path = ensure_kokoro_models(resolved_dir, offline=offline)
        if _kokoro_instance is None or _kokoro_paths != (model_path, voices_path):
            instance = Kokoro(str(model_path), str(voices_path))
            _verify_cpu_provider(instance)
            _kokoro_instance = instance
            _kokoro_paths = (model_path, voices_path)
        return _kokoro_instance


def list_voices(
    data_dir: Path | None = None,
    offline: bool = False,
    kokoro: Any | None = None,
) -> list[str]:
    """Discover available voices dynamically from the validated voices-v1.0.bin pack."""
    inst = kokoro if kokoro is not None else get_kokoro(data_dir=data_dir, offline=offline)
    with _kokoro_lock:
        voices = list(inst.get_voices())
    return sorted(voices)


def apply_pronunciations(text: str, pronunciations: Mapping[str, str]) -> str:
    """Single-pass, non-recursive, case-insensitive, Unicode word-boundary replacement (longest key first)."""
    if not text or not pronunciations:
        return text

    keys = sorted((k for k in pronunciations if k), key=len, reverse=True)
    if not keys:
        return text

    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)",
        flags=re.IGNORECASE,
    )
    lookup = {k.lower(): pronunciations[k] for k in keys}
    return pattern.sub(lambda m: lookup[m.group(0).lower()], text)


def resolve_clause_spoken_text(
    clause: ClauseInput,
    pronunciations: Mapping[str, str],
) -> str:
    """Resolve spoken text for a clause: verbatim spoken_text_override or pronunciation-applied display_text."""
    if clause.spoken_text_override is not None:
        return clause.spoken_text_override
    return apply_pronunciations(clause.display_text, pronunciations)


def compute_tts_hash(
    segment: SegmentInput,
    pronunciations: Mapping[str, str],
    voice: str,
    language: str,
    inter_clause_pause_ms: int = 120,
) -> str:
    """Compute canonical SHA-256 content hash for segment narration inputs."""
    payload = {
        "voice": voice,
        "language": language,
        "speed": float(segment.speed),
        "inter_clause_pause_ms": int(inter_clause_pause_ms),
        "pronunciations": sorted((str(k), str(v)) for k, v in pronunciations.items()),
        "clauses": [
            {
                "id": c.id,
                "display_text": c.display_text,
                "spoken_text_override": c.spoken_text_override,
                "resolved_spoken_text": resolve_clause_spoken_text(c, pronunciations),
                **(
                    {"start_sec": c.start_sec}
                    if c.start_sec is not None
                    else {}
                ),
                **(
                    {"duration_sec": c.duration_sec}
                    if c.duration_sec is not None
                    else {}
                ),
            }
            for c in segment.clauses
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _float_to_pcm16(samples: Any) -> np.ndarray:
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return np.zeros(0, dtype=np.int16)
    clipped = np.clip(arr, -1.0, 1.0)
    return np.round(clipped * 32767.0).astype(np.int16)


def synthesize_segment_tts(
    segment: SegmentInput,
    pronunciations: Mapping[str, str],
    voice: str,
    language: str,
    inter_clause_pause_ms: int,
    output_dir: Path,
    kokoro: Any | None = None,
) -> SegmentTTSResult:
    """Synthesize a segment's clauses into PCM_16 24kHz WAVs with sample-accurate timings."""
    tts_hash = compute_tts_hash(
        segment=segment,
        pronunciations=pronunciations,
        voice=voice,
        language=language,
        inter_clause_pause_ms=inter_clause_pause_ms,
    )

    if not segment.clauses:
        return SegmentTTSResult(
            tts_hash=tts_hash,
            sample_rate=SAMPLE_RATE,
            total_samples=0,
            audio_duration=0.0,
            clause_timings=[],
            audio_path=None,
            clause_wav_paths=[],
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    inst = kokoro if kokoro is not None else get_kokoro()

    has_custom_timings = any(
        c.start_sec is not None or c.duration_sec is not None for c in segment.clauses
    )

    chunks: list[np.ndarray] = []
    placed_blocks: list[tuple[int, np.ndarray, int]] = []
    timings: list[ClauseTiming] = []
    clause_wav_paths: list[Path] = []
    cursor = 0

    with _kokoro_lock:
        _verify_cpu_provider(inst)
        for idx, clause in enumerate(segment.clauses):
            spoken_text = resolve_clause_spoken_text(clause, pronunciations)
            samples, sr = inst.create(
                spoken_text,
                voice=voice,
                speed=segment.speed,
                lang=language,
            )
            if sr != SAMPLE_RATE:
                raise RuntimeError(f"Expected {SAMPLE_RATE} Hz sample rate from Kokoro, got {sr}")

            pcm16 = _float_to_pcm16(samples)
            clause_wav_path = output_dir / f"seg_{segment.id}_{tts_hash}_c{idx}_{clause.id}.wav"
            clause_txt_path = output_dir / f"seg_{segment.id}_{tts_hash}_c{idx}_{clause.id}.txt"
            sf.write(str(clause_wav_path), pcm16, SAMPLE_RATE, subtype="PCM_16")
            clause_txt_path.write_text(clause.display_text, encoding="utf-8")
            clause_wav_paths.append(clause_wav_path)

            if has_custom_timings:
                start_sample = (
                    int(round(clause.start_sec * SAMPLE_RATE))
                    if clause.start_sec is not None
                    else cursor
                )
                audio_len = int(pcm16.shape[0])
                block_len = (
                    int(round(clause.duration_sec * SAMPLE_RATE))
                    if clause.duration_sec is not None
                    else audio_len
                )
                clipped_pcm = pcm16[:block_len] if block_len < audio_len else pcm16
                end_sample = start_sample + max(1, block_len)
                timings.append(
                    ClauseTiming(
                        clause_id=clause.id,
                        start_sample=start_sample,
                        end_sample=end_sample,
                        start_sec=float(start_sample) / float(SAMPLE_RATE),
                        end_sec=float(end_sample) / float(SAMPLE_RATE),
                    )
                )
                placed_blocks.append((start_sample, clipped_pcm, end_sample))
                cursor = max(cursor, end_sample)
            else:
                start_sample = cursor
                end_sample = cursor + int(pcm16.shape[0])
                timings.append(
                    ClauseTiming(
                        clause_id=clause.id,
                        start_sample=start_sample,
                        end_sample=end_sample,
                        start_sec=float(start_sample) / float(SAMPLE_RATE),
                        end_sec=float(end_sample) / float(SAMPLE_RATE),
                    )
                )
                chunks.append(pcm16)
                cursor = end_sample

                # Insert inter-clause pause between clauses, omitted after the final clause
                if idx < len(segment.clauses) - 1 and inter_clause_pause_ms > 0:
                    pause_samples = int(round(SAMPLE_RATE * inter_clause_pause_ms / 1000.0))
                    if pause_samples > 0:
                        chunks.append(np.zeros(pause_samples, dtype=np.int16))
                        cursor += pause_samples

    if has_custom_timings:
        total_len = max((end for _, _, end in placed_blocks), default=0)
        mix = np.zeros(total_len, dtype=np.int32)
        for start_s, pcm, _ in placed_blocks:
            end_s = min(total_len, start_s + int(pcm.shape[0]))
            if end_s > start_s:
                mix[start_s:end_s] += pcm[: end_s - start_s].astype(np.int32)
        combined = np.clip(mix, -32768, 32767).astype(np.int16)
    else:
        combined = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)

    combined_path = output_dir / f"seg_{segment.id}_{tts_hash}.wav"
    sf.write(str(combined_path), combined, SAMPLE_RATE, subtype="PCM_16")

    total_samples = int(combined.shape[0])
    return SegmentTTSResult(
        tts_hash=tts_hash,
        sample_rate=SAMPLE_RATE,
        total_samples=total_samples,
        audio_duration=float(total_samples) / float(SAMPLE_RATE),
        clause_timings=timings,
        audio_path=combined_path,
        clause_wav_paths=clause_wav_paths,
    )


def resolve_caption_spoken_text(
    clip: CaptionClip,
    pronunciations: Mapping[str, str],
) -> str:
    """Resolve spoken text for an independent CaptionClip."""
    if clip.spoken_text_override is not None:
        return clip.spoken_text_override
    return apply_pronunciations(clip.display_text, pronunciations)


def compute_caption_tts_hash(
    clip: CaptionClip,
    pronunciations: Mapping[str, str],
    voice: str,
    language: str,
) -> str:
    """Compute canonical 16-char hash for a CaptionClip's narration.

    Intentionally excludes start_sec, duration_sec, auto_fit_tts, and style_override
    so moving or resizing a caption block never invalidates cached Kokoro audio.
    """
    resolved_voice = clip.voice_override or voice
    payload = {
        "clip_id": clip.id,
        "tts_enabled": bool(clip.tts_enabled),
        "voice": resolved_voice,
        "language": language,
        "speed": float(clip.speed),
        "display_text": clip.display_text,
        "spoken_text_override": clip.spoken_text_override,
        "resolved_spoken_text": resolve_caption_spoken_text(clip, pronunciations),
        "pronunciations": sorted((str(k), str(v)) for k, v in pronunciations.items()),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass(frozen=True)
class CaptionTTSResult:
    """Result of synthesizing an independent CaptionClip into a 24kHz PCM_16 WAV."""

    clip_id: str
    tts_hash: str
    sample_rate: int
    total_samples: int
    audio_duration: float
    audio_path: Path | None


def synthesize_caption_tts(
    clip: CaptionClip,
    pronunciations: Mapping[str, str],
    voice: str,
    language: str,
    output_dir: Path,
    kokoro: Any | None = None,
) -> CaptionTTSResult:
    """Synthesize a single independent CaptionClip into PCM_16 24kHz WAV."""
    import os

    tts_hash = compute_caption_tts_hash(
        clip=clip,
        pronunciations=pronunciations,
        voice=voice,
        language=language,
    )
    spoken_text = resolve_caption_spoken_text(clip, pronunciations)
    if not clip.tts_enabled or not spoken_text.strip():
        return CaptionTTSResult(
            clip_id=clip.id,
            tts_hash=tts_hash,
            sample_rate=SAMPLE_RATE,
            total_samples=0,
            audio_duration=0.0,
            audio_path=None,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    wav_path = output_dir / f"cap_{clip.id}_{tts_hash}.wav"
    if wav_path.is_file():
        data, sr = sf.read(str(wav_path), dtype="int16")
        if int(sr) == SAMPLE_RATE and int(data.shape[0]) > 0:
            total_samples = int(data.shape[0])
            return CaptionTTSResult(
                clip_id=clip.id,
                tts_hash=tts_hash,
                sample_rate=SAMPLE_RATE,
                total_samples=total_samples,
                audio_duration=float(total_samples) / float(SAMPLE_RATE),
                audio_path=wav_path,
            )

    inst = kokoro if kokoro is not None else get_kokoro()
    resolved_voice = clip.voice_override or voice
    with _kokoro_lock:
        _verify_cpu_provider(inst)
        samples, sr = inst.create(
            spoken_text,
            voice=resolved_voice,
            speed=clip.speed,
            lang=language,
        )
        if sr != SAMPLE_RATE:
            raise RuntimeError(f"Expected {SAMPLE_RATE} Hz sample rate from Kokoro, got {sr}")

    pcm16 = _float_to_pcm16(samples)
    tmp_wav = output_dir / f".cap_{clip.id}_{tts_hash}.tmp.wav"
    sf.write(str(tmp_wav), pcm16, SAMPLE_RATE, subtype="PCM_16")
    os.replace(tmp_wav, wav_path)

    total_samples = int(pcm16.shape[0])
    return CaptionTTSResult(
        clip_id=clip.id,
        tts_hash=tts_hash,
        sample_rate=SAMPLE_RATE,
        total_samples=total_samples,
        audio_duration=float(total_samples) / float(SAMPLE_RATE),
        audio_path=wav_path,
    )


def mix_caption_tracks_audio(
    tracks: Sequence[TimelineTrack],
    caption_artifacts: Mapping[str, CaptionArtifactState],
    project_dir: Path,
    total_duration_sec: float,
    output_wav_path: Path,
) -> tuple[Path | None, list[tuple[float, float]]]:
    """Place active synthesized CaptionClip WAVs at exact global timeline offsets into one 24kHz PCM_16 WAV."""
    placed: list[tuple[int, np.ndarray, int]] = []
    intervals: list[tuple[float, float]] = []

    for track in tracks:
        if not isinstance(track, CaptionTrack) or track.muted:
            continue
        for clip in track.clips:
            if not clip.tts_enabled:
                continue
            art = caption_artifacts.get(clip.id)
            if art is None or not art.audio_path:
                continue
            raw_path = Path(art.audio_path)
            abs_path = raw_path if raw_path.is_absolute() else (project_dir / raw_path)
            if not abs_path.is_file():
                continue

            pcm_raw, sr = sf.read(str(abs_path), dtype="int16")
            arr = np.asarray(pcm_raw, dtype=np.int16)
            if arr.ndim > 1:
                arr = arr.mean(axis=1).astype(np.int16)
            if int(sr) != SAMPLE_RATE and int(sr) > 0 and arr.size > 0:
                target_len = max(1, int(round(arr.shape[0] * (float(SAMPLE_RATE) / float(sr)))))
                x_old = np.linspace(0.0, 1.0, num=arr.shape[0], endpoint=False)
                x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
                arr = np.round(np.interp(x_new, x_old, arr.astype(np.float32))).astype(np.int16)

            if not clip.auto_fit_tts and clip.duration_sec is not None and clip.duration_sec > 0.0:
                max_len = max(1, int(round(clip.duration_sec * SAMPLE_RATE)))
                if arr.shape[0] > max_len:
                    arr = arr[:max_len]

            if arr.shape[0] <= 0:
                continue
            start_sample = max(0, int(round(clip.start_sec * SAMPLE_RATE)))
            end_sample = start_sample + int(arr.shape[0])
            placed.append((start_sample, arr, end_sample))
            intervals.append(
                (float(start_sample) / float(SAMPLE_RATE), float(end_sample) / float(SAMPLE_RATE))
            )

    if not placed:
        return None, []

    min_samples = max(1, int(round(max(0.0, total_duration_sec) * SAMPLE_RATE)))
    total_samples = max(min_samples, max(end_s for _, _, end_s in placed))
    mix = np.zeros(total_samples, dtype=np.int32)
    for start_s, pcm, _ in placed:
        end_s = min(total_samples, start_s + int(pcm.shape[0]))
        if end_s > start_s:
            mix[start_s:end_s] += pcm[: end_s - start_s].astype(np.int32)

    combined = np.clip(mix, -32768, 32767).astype(np.int16)
    output_wav_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_wav_path), combined, SAMPLE_RATE, subtype="PCM_16")
    return output_wav_path, sorted(intervals)
