"""Pydantic v2 data models separating authoritative inputs, derived manifests, and job states."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CURRENT_SCHEMA_VERSION = 1

DurationMode = Literal["preserve_video", "fit_narration"]
ProxyStatus = Literal["pending", "running", "ready", "failed"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
JobType = Literal["proxy", "tts", "preview", "export"]
MediaAssetKind = Literal["video", "image", "audio"]
EdgeTransitionType = Literal[
    "none",
    "fade",
    "slide_left",
    "slide_right",
    "slide_up",
    "slide_down",
    "wipe_left",
    "wipe_right",
    "zoom",
]
JunctionTransitionType = Literal[
    "crossfade",
    "dissolve",
    "wipeleft",
    "wiperight",
    "slideleft",
    "slideright",
    "fadeblack",
    "zoomin",
]
CaptionAlign = Literal["bottom", "center", "top"]
CaptionAnimation = Literal["none", "pop", "fade"]
KeyframeTransition = Literal["tween", "snap", "ease_in", "ease_out", "ease_in_out"]
ExportQuality = Literal["draft", "standard", "high"]


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
    back_color: str = Field(default="&H64000000", min_length=1)
    outline_width: float = Field(default=5.0, ge=0.0)
    shadow: float = Field(default=0.0, ge=0.0, le=16.0)
    border_style: int = Field(default=1, ge=1, le=3)
    margin_v: int = Field(default=340, ge=0)
    karaoke_highlight: bool = False
    highlight_color: str = Field(default="&H0000FFFF", min_length=1)
    animation: CaptionAnimation = "none"

    @field_validator("outline_width", "shadow")
    @classmethod
    def _validate_outline_width(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)


class CropKeyframe(BaseModel):
    """Normalized top-left crop keyframe relative to segment output timeline."""

    model_config = ConfigDict(extra="forbid")

    t: float = Field(ge=0.0)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    zoom: float = Field(default=1.0, ge=1.0, le=3.0)
    transition: KeyframeTransition = "tween"

    @field_validator("t", "x", "y", "zoom")
    @classmethod
    def _validate_finite(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)


class ClauseInput(BaseModel):
    """Paired caption/narration block; display_text is authoritative for captions."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    display_text: str
    spoken_text_override: str | None = None
    start_sec: float | None = Field(default=None, ge=0.0)
    duration_sec: float | None = Field(default=None, gt=0.0)

    @field_validator("start_sec", "duration_sec")
    @classmethod
    def _validate_optional_finite(cls, v: float | None, info: Any) -> float | None:
        if v is None:
            return None
        return _check_finite(v, info.field_name)


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


class MediaAssetRef(BaseModel):
    """Authoritative reference to a project media asset (video, image, or audio)."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    kind: MediaAssetKind
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    copied_to_project: bool = False


class VisualClipTransform(BaseModel):
    """2D layer transform in output canvas space (normalized 0.5,0.5 = centered)."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(default=0.5, ge=-2.0, le=3.0)
    y: float = Field(default=0.5, ge=-2.0, le=3.0)
    scale: float = Field(default=1.0, ge=0.05, le=4.0)
    rotation_deg: float = Field(default=0.0, ge=-360.0, le=360.0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    flip_h: bool = False
    flip_v: bool = False

    @field_validator("x", "y", "scale", "rotation_deg", "opacity")
    @classmethod
    def _validate_finite_floats(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)


class VisualClipColorAdjust(BaseModel):
    """Basic per-clip color adjustments (brightness, contrast, saturation)."""

    model_config = ConfigDict(extra="forbid")

    brightness: float = Field(default=0.0, ge=-1.0, le=1.0)
    contrast: float = Field(default=1.0, ge=0.0, le=3.0)
    saturation: float = Field(default=1.0, ge=0.0, le=3.0)

    @field_validator("brightness", "contrast", "saturation")
    @classmethod
    def _validate_finite_color(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)


class ClipEdgeTransition(BaseModel):
    """In/out animation transition attached to the start or end edge of a VisualClip."""

    model_config = ConfigDict(extra="forbid")

    type: EdgeTransitionType = "none"
    duration_sec: float = Field(default=0.0, ge=0.0, le=10.0)

    @field_validator("duration_sec")
    @classmethod
    def _validate_finite_dur(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)


class TrackTransition(BaseModel):
    """Junction transition between two sequential/overlapping clips on the same visual track."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    from_clip_id: str = Field(min_length=1)
    to_clip_id: str = Field(min_length=1)
    type: JunctionTransitionType = "crossfade"
    duration_sec: float = Field(default=0.5, gt=0.0, le=10.0)

    @field_validator("duration_sec")
    @classmethod
    def _validate_finite_dur(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)

    @model_validator(mode="after")
    def _validate_endpoints(self) -> TrackTransition:
        if self.from_clip_id == self.to_clip_id:
            raise ValueError("TrackTransition from_clip_id and to_clip_id must differ")
        return self


class VisualClip(BaseModel):
    """Independent video or image layer clip placed at an arbitrary timeline timestamp."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    kind: Literal["video", "image"] = "video"
    start_sec: float = Field(ge=0.0)
    duration_sec: float = Field(gt=0.0)
    media_in: float = Field(default=0.0, ge=0.0)
    media_out: float | None = Field(default=None, gt=0.0)
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    end_behavior: Literal["freeze", "loop"] = "freeze"
    volume: float = Field(default=0.15, ge=0.0, le=2.0)
    audio_fade_in_sec: float = Field(default=0.0, ge=0.0)
    audio_fade_out_sec: float = Field(default=0.0, ge=0.0)
    transform: VisualClipTransform = Field(default_factory=VisualClipTransform)
    color_adjust: VisualClipColorAdjust = Field(default_factory=VisualClipColorAdjust)
    transition_in: ClipEdgeTransition = Field(default_factory=ClipEdgeTransition)
    transition_out: ClipEdgeTransition = Field(default_factory=ClipEdgeTransition)

    @field_validator(
        "start_sec",
        "duration_sec",
        "media_in",
        "speed",
        "volume",
        "audio_fade_in_sec",
        "audio_fade_out_sec",
    )
    @classmethod
    def _validate_finite_floats(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)

    @field_validator("media_out")
    @classmethod
    def _validate_optional_finite(cls, v: float | None, info: Any) -> float | None:
        if v is None:
            return None
        return _check_finite(v, info.field_name)

    @model_validator(mode="after")
    def _validate_trim(self) -> VisualClip:
        if self.media_out is not None and self.media_out <= self.media_in:
            raise ValueError(
                f"media_out ({self.media_out}) must be greater than media_in ({self.media_in})"
            )
        return self


class AudioClip(BaseModel):
    """Independent music, SFX, or voiceover clip placed on an AudioTrack."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    start_sec: float = Field(ge=0.0)
    duration_sec: float = Field(gt=0.0)
    media_in: float = Field(default=0.0, ge=0.0)
    volume: float = Field(default=1.0, ge=0.0, le=2.0)
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    fade_in_sec: float = Field(default=0.0, ge=0.0)
    fade_out_sec: float = Field(default=0.0, ge=0.0)
    loop: bool = False

    @field_validator(
        "start_sec",
        "duration_sec",
        "media_in",
        "volume",
        "speed",
        "fade_in_sec",
        "fade_out_sec",
    )
    @classmethod
    def _validate_finite_floats(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)


class CaptionStyleOverride(BaseModel):
    """Optional per-caption positioning, color, animation, and font size overrides."""

    model_config = ConfigDict(extra="forbid")

    margin_v: int | None = Field(default=None, ge=0)
    font_size: int | None = Field(default=None, gt=0)
    align: CaptionAlign | None = None
    primary_color: str | None = None
    outline_color: str | None = None
    back_color: str | None = None
    border_style: int | None = Field(default=None, ge=1, le=3)
    karaoke_highlight: bool | None = None
    animation: CaptionAnimation | None = None


class CaptionClip(BaseModel):
    """Independent caption and/or Kokoro narration clip placed on a CaptionTrack."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    start_sec: float = Field(ge=0.0)
    duration_sec: float | None = Field(default=None, gt=0.0)
    auto_fit_tts: bool = True
    display_text: str = ""
    spoken_text_override: str | None = None
    tts_enabled: bool = True
    speed: float = Field(default=1.0, ge=0.8, le=1.25)
    voice_override: str | None = None
    style_override: CaptionStyleOverride | None = None

    @field_validator("start_sec", "speed")
    @classmethod
    def _validate_finite_floats(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)

    @field_validator("duration_sec")
    @classmethod
    def _validate_optional_finite(cls, v: float | None, info: Any) -> float | None:
        if v is None:
            return None
        return _check_finite(v, info.field_name)


class VideoTrack(BaseModel):
    """Visual layer track (V1 base gameplay or V2+ image/video overlays)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(default="Video 1", min_length=1)
    kind: Literal["video"] = "video"
    visible: bool = True
    muted: bool = False
    locked: bool = False
    camera_follow: bool = True
    clips: list[VisualClip] = Field(default_factory=list)
    transitions: list[TrackTransition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_track_refs(self) -> VideoTrack:
        clip_ids = [c.id for c in self.clips]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError(f"Clip IDs within VideoTrack {self.id!r} must be unique")
        clip_set = set(clip_ids)
        tr_ids = [t.id for t in self.transitions]
        if len(tr_ids) != len(set(tr_ids)):
            raise ValueError(f"Transition IDs within VideoTrack {self.id!r} must be unique")
        for tr in self.transitions:
            if tr.from_clip_id not in clip_set or tr.to_clip_id not in clip_set:
                raise ValueError(
                    f"TrackTransition {tr.id!r} references missing clip(s) on track {self.id!r}"
                )
        return self


class AudioTrack(BaseModel):
    """Independent audio track (music, SFX, or standalone voiceover)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(default="Audio 1", min_length=1)
    kind: Literal["audio"] = "audio"
    volume: float = Field(default=1.0, ge=0.0, le=2.0)
    muted: bool = False
    solo: bool = False
    duck_under_narration: bool = True
    locked: bool = False
    clips: list[AudioClip] = Field(default_factory=list)

    @field_validator("volume")
    @classmethod
    def _validate_finite_vol(cls, v: float, info: Any) -> float:
        return _check_finite(v, info.field_name)

    @model_validator(mode="after")
    def _validate_clip_ids(self) -> AudioTrack:
        clip_ids = [c.id for c in self.clips]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError(f"Clip IDs within AudioTrack {self.id!r} must be unique")
        return self


class CaptionTrack(BaseModel):
    """Independent caption and narration track."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(default="Captions 1", min_length=1)
    kind: Literal["caption"] = "caption"
    visible: bool = True
    muted: bool = False
    locked: bool = False
    clips: list[CaptionClip] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_clip_ids(self) -> CaptionTrack:
        clip_ids = [c.id for c in self.clips]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError(f"Clip IDs within CaptionTrack {self.id!r} must be unique")
        return self


TimelineTrack = Annotated[Union[VideoTrack, AudioTrack, CaptionTrack], Field(discriminator="kind")]


class TimelineMarker(BaseModel):
    """Named/colored timeline marker placed on the project ruler."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    t: float = Field(ge=0.0)
    label: str = ""
    color: str = Field(default="#f59e0b", min_length=1)

    @field_validator("t")
    @classmethod
    def _validate_finite_t(cls, v: float) -> float:
        return _check_finite(v, "t")


class ExportOptions(BaseModel):
    """Optional export configuration overrides (filename, quality preset, fps)."""

    model_config = ConfigDict(extra="forbid")

    filename: str | None = None
    quality: ExportQuality = "standard"
    fps: int | None = Field(default=None, ge=15, le=60)


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
    media_assets: dict[str, MediaAssetRef] = Field(default_factory=dict)
    voice: str = Field(default="bf_emma", min_length=1)
    language: str = Field(default="en-gb", min_length=1)
    output_settings: OutputSettings = Field(default_factory=OutputSettings)
    caption_settings: CaptionSettings = Field(default_factory=CaptionSettings)
    pronunciations: dict[str, str] = Field(default_factory=dict)
    camera_keyframes: list[CropKeyframe] = Field(default_factory=list)
    markers: list[TimelineMarker] = Field(default_factory=list)
    tracks: list[TimelineTrack] = Field(default_factory=list)
    segments: list[SegmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_segment_ids(self) -> ProjectInput:
        seg_ids = [s.id for s in self.segments]
        if len(seg_ids) != len(set(seg_ids)):
            raise ValueError("Segment IDs within a project must be unique")

        track_ids = [t.id for t in self.tracks]
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("Track IDs within a project must be unique")

        seen_clip_ids: set[str] = set()
        for track in self.tracks:
            for clip in track.clips:
                if clip.id in seen_clip_ids:
                    raise ValueError(f"Duplicate clip ID {clip.id!r} across timeline tracks")
                seen_clip_ids.add(clip.id)

        if self.camera_keyframes:
            for idx in range(1, len(self.camera_keyframes)):
                prev_t = self.camera_keyframes[idx - 1].t
                curr_t = self.camera_keyframes[idx].t
                if curr_t <= prev_t:
                    raise ValueError(
                        f"camera_keyframes timestamps must be strictly increasing "
                        f"(index {idx}: {curr_t} <= {prev_t})"
                    )
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


class MediaAssetMetadata(BaseModel):
    """ffprobe-derived metadata for any project MediaAsset (video, image, or audio)."""

    kind: MediaAssetKind
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fps: float | None = Field(default=None, gt=0.0)
    duration: float = Field(gt=0.0)
    has_audio: bool = False
    has_alpha: bool = False
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


class MediaProxyState(BaseModel):
    """Derived proxy state for an imported video asset in manifest.json."""

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


class CaptionArtifactState(BaseModel):
    """Derived synthesized TTS WAV state for an independent CaptionClip."""

    tts_hash: str | None = None
    audio_artifact_id: str | None = None
    audio_path: str | None = None
    sample_rate: int = 24000
    total_samples: int = 0
    audio_duration: float = 0.0


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
    media_metadata: dict[str, MediaAssetMetadata] = Field(default_factory=dict)
    media_proxies: dict[str, MediaProxyState] = Field(default_factory=dict)
    caption_artifacts: dict[str, CaptionArtifactState] = Field(default_factory=dict)
    segments: dict[str, SegmentArtifactState] = Field(default_factory=dict)
    latest_preview_artifact_id: str | None = None
    latest_preview_path: str | None = None
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
