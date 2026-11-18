export type DurationMode = "preserve_video" | "fit_narration";
export type ProxyStatus = "pending" | "running" | "ready" | "failed";
export type JobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";
export type JobType = "proxy" | "tts" | "preview" | "export";
export type MediaAssetKind = "video" | "image" | "audio";
export type EdgeTransitionType =
  | "none"
  | "fade"
  | "slide_left"
  | "slide_right"
  | "slide_up"
  | "slide_down"
  | "wipe_left"
  | "wipe_right"
  | "zoom";
export type JunctionTransitionType =
  | "crossfade"
  | "dissolve"
  | "wipeleft"
  | "wiperight"
  | "slideleft"
  | "slideright"
  | "fadeblack"
  | "zoomin";
export type CaptionAlign = "bottom" | "center" | "top";

export interface SourceMediaRef {
  media_id: string;
  path: string;
  fingerprint: string;
  copied_to_project: boolean;
}

export interface MediaAssetRef {
  asset_id: string;
  kind: MediaAssetKind;
  name: string;
  path: string;
  fingerprint: string;
  copied_to_project: boolean;
}

export interface OutputSettings {
  width: number;
  height: number;
  fps: number;
  default_source_volume: number;
  tail_padding_sec: number;
  inter_clause_pause_ms: number;
}

export interface CaptionSettings {
  font_file: string;
  font_name: string;
  font_size: number;
  primary_color: string;
  outline_color: string;
  outline_width: number;
  margin_v: number;
}

export type KeyframeTransition = "tween" | "snap";

export interface CropKeyframe {
  t: number;
  x: number;
  y: number;
  zoom: number;
  transition?: KeyframeTransition;
}

export interface ClauseInput {
  id: string;
  display_text: string;
  spoken_text_override: string | null;
  start_sec?: number | null;
  duration_sec?: number | null;
}

export interface VisualClipTransform {
  x: number;
  y: number;
  scale: number;
  rotation_deg: number;
  opacity: number;
}

export interface ClipEdgeTransition {
  type: EdgeTransitionType;
  duration_sec: number;
}

export interface TrackTransition {
  id: string;
  from_clip_id: string;
  to_clip_id: string;
  type: JunctionTransitionType;
  duration_sec: number;
}

export interface VisualClip {
  id: string;
  asset_id: string;
  kind: "video" | "image";
  start_sec: number;
  duration_sec: number;
  media_in: number;
  media_out: number | null;
  speed: number;
  end_behavior: "freeze" | "loop";
  volume: number;
  audio_fade_in_sec: number;
  audio_fade_out_sec: number;
  transform: VisualClipTransform;
  transition_in: ClipEdgeTransition;
  transition_out: ClipEdgeTransition;
}

export interface AudioClip {
  id: string;
  asset_id: string;
  start_sec: number;
  duration_sec: number;
  media_in: number;
  volume: number;
  speed: number;
  fade_in_sec: number;
  fade_out_sec: number;
  loop: boolean;
}

export interface CaptionStyleOverride {
  margin_v?: number | null;
  font_size?: number | null;
  align?: CaptionAlign | null;
}

export interface CaptionClip {
  id: string;
  start_sec: number;
  duration_sec: number | null;
  auto_fit_tts: boolean;
  display_text: string;
  spoken_text_override: string | null;
  tts_enabled: boolean;
  speed: number;
  voice_override?: string | null;
  style_override?: CaptionStyleOverride | null;
}

export interface VideoTrack {
  id: string;
  name: string;
  kind: "video";
  visible: boolean;
  muted: boolean;
  locked: boolean;
  camera_follow: boolean;
  clips: VisualClip[];
  transitions: TrackTransition[];
}

export interface AudioTrack {
  id: string;
  name: string;
  kind: "audio";
  volume: number;
  muted: boolean;
  solo: boolean;
  duck_under_narration: boolean;
  locked: boolean;
  clips: AudioClip[];
}

export interface CaptionTrack {
  id: string;
  name: string;
  kind: "caption";
  visible: boolean;
  muted: boolean;
  locked: boolean;
  clips: CaptionClip[];
}

export type TimelineTrack = VideoTrack | AudioTrack | CaptionTrack;

export type TimelineSelection =
  | { type: "video-clip"; trackId: string; clipId: string }
  | { type: "audio-clip"; trackId: string; clipId: string }
  | { type: "caption-clip"; trackId: string; clipId: string }
  | { type: "transition"; trackId: string; transitionId: string }
  | { type: "track"; trackId: string }
  | { type: "keyframe"; index: number; segId?: string }
  | { type: "video"; segId: string }
  | { type: "clause"; segId: string; index: number }
  | null;

export interface SegmentInput {
  id: string;
  video_in: number;
  video_out: number;
  duration_mode: DurationMode;
  source_audio_volume: number;
  speed: number;
  crop_keyframes: CropKeyframe[];
  clauses: ClauseInput[];
}

export interface ProjectInput {
  schema_version: number;
  project_id: string;
  revision: number;
  title: string;
  created_at: string;
  updated_at: string;
  source_media: SourceMediaRef;
  media_assets?: Record<string, MediaAssetRef>;
  voice: string;
  language: string;
  output_settings: OutputSettings;
  caption_settings: CaptionSettings;
  pronunciations: Record<string, string>;
  camera_keyframes?: CropKeyframe[];
  tracks?: TimelineTrack[];
  segments: SegmentInput[];
}

export interface SourceMetadata {
  width: number;
  height: number;
  fps: number;
  duration: number;
  has_audio: boolean;
  codec_name?: string | null;
  pix_fmt?: string | null;
  audio_sample_rate?: number | null;
  audio_channels?: number | null;
}

export interface MediaAssetMetadata {
  kind: MediaAssetKind;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  duration: number;
  has_audio: boolean;
  has_alpha: boolean;
  codec_name?: string | null;
  pix_fmt?: string | null;
  audio_sample_rate?: number | null;
  audio_channels?: number | null;
}

export interface ProxyState {
  status: ProxyStatus;
  artifact_id: string | null;
  path: string | null;
  error: string | null;
}

export interface MediaProxyState {
  status: ProxyStatus;
  artifact_id: string | null;
  path: string | null;
  error: string | null;
}

export interface ClauseTiming {
  clause_id: string;
  start_sample: number;
  end_sample: number;
  start_sec: number;
  end_sec: number;
}

export interface CaptionArtifactState {
  tts_hash: string | null;
  audio_artifact_id: string | null;
  audio_path: string | null;
  sample_rate: number;
  total_samples: number;
  audio_duration: number;
}

export interface SegmentArtifactState {
  tts_hash: string | null;
  audio_artifact_id: string | null;
  audio_path: string | null;
  sample_rate: number;
  total_samples: number;
  audio_duration: number;
  clause_timings: ClauseTiming[];
  render_hash: string | null;
  segment_artifact_id: string | null;
  rendered_segment_path: string | null;
  preview_artifact_id: string | null;
  preview_path: string | null;
}

export interface ArtifactManifest {
  project_id: string;
  source_metadata: SourceMetadata | null;
  proxy: ProxyState;
  media_metadata?: Record<string, MediaAssetMetadata>;
  media_proxies?: Record<string, MediaProxyState>;
  caption_artifacts?: Record<string, CaptionArtifactState>;
  segments: Record<string, SegmentArtifactState>;
  latest_preview_artifact_id: string | null;
  latest_preview_path: string | null;
  latest_export_artifact_id: string | null;
  latest_export_path: string | null;
}

export interface JobState {
  job_id: string;
  project_id: string;
  seg_id: string | null;
  job_type: JobType;
  status: JobStatus;
  progress: number;
  stage: string;
  error: string | null;
  result: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface SourceStatus {
  status: "ok" | "missing" | "modified";
  ok?: boolean;
  reason?: string | null;
  path?: string | null;
  message?: string | null;
}

export interface Capabilities {
  ffmpeg_ready: boolean;
  libass_ready: boolean;
  kokoro_ready: boolean;
  font_ready: boolean;
  native_picker_supported?: boolean;
  nvenc_ready?: boolean;
  voices: string[];
  errors: string[];
}

export interface ProjectSummary {
  project_id: string;
  title: string;
  revision: number;
  created_at: string;
  updated_at: string;
  segment_count?: number;
  clip_count?: number;
}

export interface ProjectBundle {
  project: ProjectInput;
  manifest: ArtifactManifest;
  source_status?: SourceStatus;
  stale_tts_segments?: string[];
  stale_tts_captions?: string[];
}

export interface MediaSelectResult {
  media_id: string;
  path: string;
  fingerprint?: string;
  metadata: SourceMetadata;
}

export interface MediaAssetImportResult {
  asset: MediaAssetRef;
  metadata: MediaAssetMetadata;
  job_id?: string | null;
  project: ProjectInput;
  manifest: ArtifactManifest;
  source_status?: SourceStatus;
  stale_tts_segments?: string[];
  stale_tts_captions?: string[];
}

export interface JobAccepted {
  job_id: string;
  status?: JobStatus;
}
