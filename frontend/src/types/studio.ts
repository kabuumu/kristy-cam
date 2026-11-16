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

export interface SourceMediaRef {
  media_id: string;
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

export interface CropKeyframe {
  t: number;
  x: number;
  y: number;
  zoom: number;
}

export interface ClauseInput {
  id: string;
  display_text: string;
  spoken_text_override: string | null;
}

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
  voice: string;
  language: string;
  output_settings: OutputSettings;
  caption_settings: CaptionSettings;
  pronunciations: Record<string, string>;
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

export interface ProxyState {
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
  segments: Record<string, SegmentArtifactState>;
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
}

export interface ProjectBundle {
  project: ProjectInput;
  manifest: ArtifactManifest;
  source_status?: SourceStatus;
}

export interface MediaSelectResult {
  media_id: string;
  path: string;
  fingerprint?: string;
  metadata: SourceMetadata;
}

export interface JobAccepted {
  job_id: string;
  status?: JobStatus;
}
