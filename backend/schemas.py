"""Pydantic v2 data models separating authoritative inputs, derived manifests, and job states."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CURRENT_SCHEMA_VERSION = 1

DurationMode = Literal["preserve_video", "fit_narration"]
ProxyStatus = Literal["pending", "running", "ready", "failed"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
JobType = Literal["proxy", "tts", "preview", "export"]


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 Z format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _check_finite(value: float, field_name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite, got {value!r}")
    return value


class SourceMediaRef(BaseModel):
    """Authoritative reference to the project's source recording."""

    model_config = ConfigDict(extra="forbid")

    media_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    copied_to_project: bool = False


class OutputSettings(BaseModel):
    """Project-level video and narration timing defaults."""

    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=1080, gt=0)
    height: int = Field(default=1920, gt=0)
    fps: int = Field(default=30, gt=0)
    default_source_volume: float = Field(default=0.15, ge=0.0, le=1.0)
    tail_padding_sec: float = Field(default=0.4, ge=0.0)
    inter_clause_pause_ms: int = Field(default=120, ge=0)

    @field_validator("width", "height")
    @classmethod
    def _validate_even_dimension(cls, v: int, info: Any) -> int:
        if v < 2 or v % 2 != 0:
            raise ValueError(f"{info.field_name} must be an even integer >= 2, got {v}")
        return v

    @field_validator("default_source_volume", "tail_padding_sec")
    @classmethod
    def _validate_finite_floats(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)


class CaptionSettings(BaseModel):
    """Project-level ASS subtitle styling configuration."""

    model_config = ConfigDict(extra="forbid")

    font_file: str = Field(default="Montserrat-ExtraBold.ttf", min_length=1)
    font_name: str = Field(default="Montserrat ExtraBold", min_length=1)
    font_size: int = Field(default=64, gt=0)
    primary_color: str = Field(default="&H00FFFFFF", min_length=1)
    outline_color: str = Field(default="&H00000000", min_length=1)
    outline_width: float = Field(default=5.0, ge=0.0)
    margin_v: int = Field(default=340, ge=0)

    @field_validator("outline_width")
    @classmethod
    def _validate_outline_width(cls, v: float) -> float:
        return _check_finite(v, "outline_width")


class CropKeyframe(BaseModel):
    """Normalized top-left crop keyframe relative to segment output timeline."""

    model_config = ConfigDict(extra="forbid")

    t: float = Field(ge=0.0)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    zoom: float = Field(default=1.0, ge=1.0, le=3.0)

    @field_validator("t", "x", "y", "zoom")
    @classmethod
    def _validate_finite(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)


class ClauseInput(BaseModel):
    """Paired caption/narration clause; display_text is authoritative for captions."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    display_text: str
    spoken_text_override: str | None = None


class SegmentInput(BaseModel):
    """User-editable segment definition; array position in ProjectInput.segments is authoritative."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    video_in: float = Field(ge=0.0)
    video_out: float
    duration_mode: DurationMode = "preserve_video"
    source_audio_volume: float = Field(default=0.15, ge=0.0, le=1.0)
    speed: float = Field(default=1.0, ge=0.8, le=1.25)
    crop_keyframes: list[CropKeyframe] = Field(min_length=1)
    clauses: list[ClauseInput] = Field(default_factory=list)

    @field_validator("video_in", "video_out", "source_audio_volume", "speed")
    @classmethod
    def _validate_finite_floats(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)

    @model_validator(mode="after")
    def _validate_segment(self) -> SegmentInput:
        if self.video_out <= self.video_in:
            raise ValueError(
                f"video_out ({self.video_out}) must be strictly greater than video_in ({self.video_in})"
            )

        if not self.crop_keyframes:
            raise ValueError("crop_keyframes must be non-empty")

        if self.crop_keyframes[0].t != 0.0:
            raise ValueError(
                f"First crop keyframe must start at t == 0.0, got {self.crop_keyframes[0].t}"
            )

        for idx in range(1, len(self.crop_keyframes)):
            prev_t = self.crop_keyframes[idx - 1].t
            curr_t = self.crop_keyframes[idx].t
            if curr_t <= prev_t:
                raise ValueError(
                    f"crop_keyframes timestamps must be strictly increasing "
                    f"(index {idx}: {curr_t} <= {prev_t})"
                )

        clause_ids = [c.id for c in self.clauses]
        if len(clause_ids) != len(set(clause_ids)):
            raise ValueError("Clause IDs within a segment must be unique")

        return self


class ProjectInput(BaseModel):
    """Authoritative user-editable project state persisted in project.json."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=CURRENT_SCHEMA_VERSION, ge=1, le=CURRENT_SCHEMA_VERSION)
    project_id: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    title: str = Field(min_length=1)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    source_media: SourceMediaRef
    voice: str = Field(default="bf_emma", min_length=1)
    language: str = Field(default="en-gb", min_length=1)
    output_settings: OutputSettings = Field(default_factory=OutputSettings)
    caption_settings: CaptionSettings = Field(default_factory=CaptionSettings)
    pronunciations: dict[str, str] = Field(default_factory=dict)
    segments: list[SegmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_segment_ids(self) -> ProjectInput:
        seg_ids = [s.id for s in self.segments]
        if len(seg_ids) != len(set(seg_ids)):
            raise ValueError("Segment IDs within a project must be unique")
        return self


# --- Derived Manifest & Job Models (never directly overwritten by project PUT) ---


class SourceMetadata(BaseModel):
    """ffprobe-derived source media metadata."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0.0)
    duration: float = Field(gt=0.0)
    has_audio: bool
    codec_name: str | None = None
    pix_fmt: str | None = None
    audio_sample_rate: int | None = None
    audio_channels: int | None = None


class ProxyState(BaseModel):
    """Derived proxy video state in manifest.json."""

    status: ProxyStatus = "pending"
    artifact_id: str | None = None
    path: str | None = None
    error: str | None = None


class ClauseTiming(BaseModel):
    """Sample-accurate timing interval for a synthesized clause."""

    clause_id: str
    start_sample: int = Field(ge=0)
    end_sample: int = Field(ge=0)
    start_sec: float = Field(ge=0.0)
    end_sec: float = Field(ge=0.0)


class SegmentArtifactState(BaseModel):
    """Derived TTS and rendered video artifacts for a single segment."""

    tts_hash: str | None = None
    audio_artifact_id: str | None = None
    audio_path: str | None = None
    sample_rate: int = 24000
    total_samples: int = 0
    audio_duration: float = 0.0
    clause_timings: list[ClauseTiming] = Field(default_factory=list)
    render_hash: str | None = None
    segment_artifact_id: str | None = None
    rendered_segment_path: str | None = None
    preview_artifact_id: str | None = None
    preview_path: str | None = None


class ArtifactManifest(BaseModel):
    """Derived project artifacts and timings persisted in manifest.json."""

    project_id: str
    source_metadata: SourceMetadata | None = None
    proxy: ProxyState = Field(default_factory=ProxyState)
    segments: dict[str, SegmentArtifactState] = Field(default_factory=dict)
    latest_export_artifact_id: str | None = None
    latest_export_path: str | None = None


class JobState(BaseModel):
    """Background job execution state."""

    job_id: str
    project_id: str
    seg_id: str | None = None
    job_type: JobType
    status: JobStatus = "queued"
    progress: int = Field(default=0, ge=0, le=100)
    stage: str = ""
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
