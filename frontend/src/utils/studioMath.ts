import type {
  ArtifactManifest,
  AudioClip,
  AudioTrack,
  CaptionClip,
  CaptionTrack,
  ClauseInput,
  CropKeyframe,
  ProjectInput,
  SegmentInput,
  TimelineTrack,
  TrackTransition,
  VideoTrack,
  VisualClip,
} from "../types/studio";

export interface IdealCropDimensions {
  baseCropW: number;
  baseCropH: number;
  idealCropW: number;
  idealCropH: number;
  maxNormX: number;
  maxNormY: number;
}

export function formatAspectRatio(width: number, height: number): string {
  const w = Math.max(1, Math.round(width));
  const h = Math.max(1, Math.round(height));
  const gcd = (a: number, b: number): number => (b === 0 ? a : gcd(b, a % b));
  const d = gcd(w, h);
  const rw = w / d;
  const rh = h / d;
  if (rw <= 32 && rh <= 32) {
    return `${rw}:${rh}`;
  }
  return `${(w / h).toFixed(2)}:1`;
}

export function computeIdealCrop(
  sourceWidth: number,
  sourceHeight: number,
  zoom: number,
  outputWidth = 1080,
  outputHeight = 1920
): IdealCropDimensions {
  const safeW = Math.max(1, sourceWidth);
  const safeH = Math.max(1, sourceHeight);
  const safeOutW = Math.max(2, outputWidth);
  const safeOutH = Math.max(2, outputHeight);
  const clampedZoom = Math.min(3.0, Math.max(1.0, zoom));
  const targetAspect = safeOutW / safeOutH;
  const sourceAspect = safeW / safeH;

  let baseCropW: number;
  let baseCropH: number;
  if (sourceAspect >= targetAspect) {
    baseCropH = safeH;
    baseCropW = safeH * targetAspect;
  } else {
    baseCropW = safeW;
    baseCropH = safeW / targetAspect;
  }

  const idealCropH = baseCropH / clampedZoom;
  const idealCropW = baseCropW / clampedZoom;
  const maxNormX = Math.max(0, (safeW - idealCropW) / safeW);
  const maxNormY = Math.max(0, (safeH - idealCropH) / safeH);
  return {
    baseCropW,
    baseCropH,
    idealCropW,
    idealCropH,
    maxNormX,
    maxNormY,
  };
}

export function clampCropPosition(
  x: number,
  y: number,
  zoom: number,
  sourceWidth: number,
  sourceHeight: number,
  outputWidth = 1080,
  outputHeight = 1920
): { x: number; y: number; zoom: number } {
  const clampedZoom = Math.min(3.0, Math.max(1.0, zoom));
  const { maxNormX, maxNormY } = computeIdealCrop(
    sourceWidth,
    sourceHeight,
    clampedZoom,
    outputWidth,
    outputHeight
  );
  return {
    x: Math.min(maxNormX, Math.max(0, Number.isFinite(x) ? x : 0)),
    y: Math.min(maxNormY, Math.max(0, Number.isFinite(y) ? y : 0)),
    zoom: clampedZoom,
  };
}

export function applyKeyframeEasing(
  u: number,
  transition?: CropKeyframe["transition"]
): number {
  const clamped = Math.max(0, Math.min(1, u));
  if (transition === "ease_in") return clamped * clamped;
  if (transition === "ease_out") {
    const inv = 1 - clamped;
    return 1 - inv * inv;
  }
  if (transition === "ease_in_out") {
    return clamped * clamped * (3 - 2 * clamped);
  }
  return clamped;
}

export function evaluateCropAt(
  keyframes: CropKeyframe[],
  t: number,
  sourceWidth: number,
  sourceHeight: number,
  outputWidth = 1080,
  outputHeight = 1920
): { x: number; y: number; zoom: number } {
  if (!keyframes.length) {
    return clampCropPosition(
      0.28,
      0,
      1.0,
      sourceWidth,
      sourceHeight,
      outputWidth,
      outputHeight
    );
  }
  const sorted = [...keyframes].sort((a, b) => a.t - b.t);
  if (t <= sorted[0].t) {
    return clampCropPosition(
      sorted[0].x,
      sorted[0].y,
      sorted[0].zoom,
      sourceWidth,
      sourceHeight,
      outputWidth,
      outputHeight
    );
  }
  const last = sorted[sorted.length - 1];
  if (t >= last.t) {
    return clampCropPosition(
      last.x,
      last.y,
      last.zoom,
      sourceWidth,
      sourceHeight,
      outputWidth,
      outputHeight
    );
  }
  for (let i = 0; i < sorted.length - 1; i++) {
    const a = sorted[i];
    const b = sorted[i + 1];
    if ((t >= a.t && t < b.t) || (i === sorted.length - 2 && t >= a.t && t <= b.t)) {
      const span = b.t - a.t;
      const rawU = span > 0 ? (t - a.t) / span : 0;
      const alpha =
        a.transition === "snap" && t < b.t
          ? 0
          : applyKeyframeEasing(rawU, a.transition);
      const x = a.x + (b.x - a.x) * alpha;
      const y = a.y + (b.y - a.y) * alpha;
      const zoom = a.zoom + (b.zoom - a.zoom) * alpha;
      return clampCropPosition(
        x,
        y,
        zoom,
        sourceWidth,
        sourceHeight,
        outputWidth,
        outputHeight
      );
    }
  }
  return clampCropPosition(
    last.x,
    last.y,
    last.zoom,
    sourceWidth,
    sourceHeight,
    outputWidth,
    outputHeight
  );
}

export interface DurationPlan {
  rawVideoDur: number;
  narrationDur: number;
  rawTarget: number;
  frameCount: number;
  finalDur: number;
  movingSourceDur: number;
  isFreezeFrame: boolean;
  isSourceExtended: boolean;
}

export function computeDurationPlan(
  segment: SegmentInput,
  audioDuration: number,
  sourceDuration: number,
  tailPaddingSec = 0.4
): DurationPlan {
  const rawVideoDur = Math.max(0, segment.video_out - segment.video_in);
  const hasNarration = segment.clauses.length > 0 && audioDuration > 0;
  const narDur = hasNarration ? audioDuration : 0;

  let rawTarget = rawVideoDur;
  if (segment.duration_mode === "preserve_video") {
    rawTarget =
      narDur > 0 ? Math.max(rawVideoDur, narDur + tailPaddingSec) : rawVideoDur;
  } else {
    rawTarget = narDur > 0 ? narDur + tailPaddingSec : rawVideoDur;
  }

  const frameCount = Math.max(1, Math.ceil(rawTarget * 30));
  const finalDur = frameCount / 30;

  const availableSourceDur =
    sourceDuration > 0
      ? Math.max(0, sourceDuration - segment.video_in)
      : Number.POSITIVE_INFINITY;
  const movingSourceDur =
    segment.duration_mode === "preserve_video"
      ? Math.min(rawVideoDur, availableSourceDur)
      : Math.min(rawTarget, availableSourceDur);

  const isFreezeFrame =
    segment.duration_mode === "preserve_video"
      ? rawTarget > rawVideoDur + 1e-4
      : sourceDuration > 0 &&
        segment.video_in + rawTarget > sourceDuration + 1e-4;

  const isSourceExtended =
    segment.duration_mode === "fit_narration" &&
    narDur > 0 &&
    rawTarget > rawVideoDur + 1e-4 &&
    !isFreezeFrame;

  return {
    rawVideoDur,
    narrationDur: narDur,
    rawTarget,
    frameCount,
    finalDur,
    movingSourceDur,
    isFreezeFrame,
    isSourceExtended,
  };
}

function escapeRegExp(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Single-pass, non-recursive, case-insensitive, Unicode word-boundary replacement (longest key first).
 * Mirrors backend/tts.py:apply_pronunciations.
 */
export function applyPronunciations(
  text: string,
  pronunciations: Record<string, string>
): string {
  if (!text || !pronunciations) return text;
  const keys = Object.keys(pronunciations)
    .filter((k) => k.length > 0)
    .sort((a, b) => b.length - a.length);
  if (!keys.length) return text;

  const lookup = new Map<string, string>();
  for (const k of keys) {
    lookup.set(k.toLowerCase(), pronunciations[k]);
  }

  const pattern = new RegExp(
    `(?<!\\p{L}|\\p{N}|_)(${keys.map(escapeRegExp).join("|")})(?!\\p{L}|\\p{N}|_)`,
    "giu"
  );
  return text.replace(pattern, (match) => {
    return lookup.get(match.toLowerCase()) ?? match;
  });
}

export function resolveClauseSpokenText(
  clause: ClauseInput,
  pronunciations: Record<string, string>
): string {
  if (clause.spoken_text_override !== null) {
    return clause.spoken_text_override;
  }
  return applyPronunciations(clause.display_text, pronunciations);
}

export function computeClientTtsSignature(
  segment: SegmentInput,
  pronunciations: Record<string, string>,
  voice: string,
  language: string,
  interClausePauseMs = 120
): string {
  const sortedPron = Object.entries(pronunciations).sort(([a], [b]) =>
    a.localeCompare(b)
  );
  return JSON.stringify({
    voice,
    language,
    speed: Number(segment.speed.toFixed(4)),
    interClausePauseMs,
    pronunciations: sortedPron,
    clauses: segment.clauses.map((c) => ({
      id: c.id,
      display_text: c.display_text,
      spoken_text_override: c.spoken_text_override,
      resolved: resolveClauseSpokenText(c, pronunciations),
      start_sec: c.start_sec ?? null,
      duration_sec: c.duration_sec ?? null,
    })),
  });
}

export function computeClientPreviewSignature(
  segment: SegmentInput,
  project: {
    voice: string;
    language: string;
    pronunciations: Record<string, string>;
    output_settings: unknown;
    caption_settings: unknown;
    source_media: unknown;
  }
): string {
  return JSON.stringify({
    tts: computeClientTtsSignature(
      segment,
      project.pronunciations,
      project.voice,
      project.language,
      (project.output_settings as { inter_clause_pause_ms?: number })
        ?.inter_clause_pause_ms ?? 120
    ),
    video_in: segment.video_in,
    video_out: segment.video_out,
    duration_mode: segment.duration_mode,
    source_audio_volume: segment.source_audio_volume,
    crop_keyframes: segment.crop_keyframes,
    output_settings: project.output_settings,
    caption_settings: project.caption_settings,
    source_media: project.source_media,
  });
}

export function computeClientProjectPreviewSignature(project: {
  voice: string;
  language: string;
  pronunciations: Record<string, string>;
  camera_keyframes?: CropKeyframe[];
  output_settings: unknown;
  caption_settings: unknown;
  source_media: unknown;
  segments: SegmentInput[];
}): string {
  return JSON.stringify({
    segments: project.segments.map((seg) =>
      computeClientPreviewSignature(seg, project)
    ),
    camera_keyframes: project.camera_keyframes ?? [],
  });
}

export function formatTimecode(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "00:00.00";
  const mins = Math.floor(sec / 60);
  const rem = sec - mins * 60;
  return `${String(mins).padStart(2, "0")}:${rem.toFixed(2).padStart(5, "0")}`;
}

export function autoSplitTextToClauses(text: string): string[] {
  const trimmed = text.trim();
  if (!trimmed) return [""];

  // First split on strong sentence/clause punctuation while keeping punctuation
  const rawChunks = trimmed
    .split(/(?<=[.!?,;:])\s+/)
    .map((s) => s.trim())
    .filter(Boolean);

  const result: string[] = [];
  for (const chunk of rawChunks) {
    const words = chunk.split(/\s+/).filter(Boolean);
    if (words.length <= 6) {
      result.push(chunk);
    } else {
      // Break into 3-5 word chunks
      for (let i = 0; i < words.length; i += 4) {
        const slice = words.slice(i, i + 4);
        if (slice.length < 3 && result.length > 0 && i > 0) {
          result[result.length - 1] = `${result[result.length - 1]} ${slice.join(" ")}`;
        } else {
          result.push(slice.join(" "));
        }
      }
    }
  }
  return result.length > 0 ? result : [trimmed];
}

export interface TimelineClipLayout {
  seg: SegmentInput;
  index: number;
  startSec: number;
  endSec: number;
  plan: DurationPlan;
}

export interface TimelineLayout {
  clips: TimelineClipLayout[];
  totalDuration: number;
}

export interface ResolvedProjectTime {
  clip: TimelineClipLayout | null;
  projectTime: number;
  localTime: number;
  sourceTime: number;
  isFrozen: boolean;
}

export function computeTimelineLayout(
  segments: SegmentInput[],
  manifest: ArtifactManifest | null,
  sourceDuration: number,
  tailPaddingSec = 0.4
): TimelineLayout {
  const clips: TimelineClipLayout[] = [];
  let cursor = 0;
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    const audioDur = manifest?.segments[seg.id]?.audio_duration ?? 0;
    const plan = computeDurationPlan(seg, audioDur, sourceDuration, tailPaddingSec);
    const startSec = cursor;
    const endSec = cursor + plan.finalDur;
    clips.push({ seg, index: i, startSec, endSec, plan });
    cursor = endSec;
  }
  return {
    clips,
    totalDuration: cursor,
  };
}

export function resolveProjectTime(
  layout: TimelineLayout,
  rawProjectTime: number
): ResolvedProjectTime {
  if (layout.clips.length === 0) {
    return {
      clip: null,
      projectTime: 0,
      localTime: 0,
      sourceTime: 0,
      isFrozen: false,
    };
  }
  const clamped = Math.max(0, Math.min(layout.totalDuration, rawProjectTime));
  let target = layout.clips[layout.clips.length - 1];
  for (const clip of layout.clips) {
    if (clamped < clip.endSec - 1e-6) {
      target = clip;
      break;
    }
  }
  const localTime = Math.max(
    0,
    Math.min(target.plan.finalDur, clamped - target.startSec)
  );
  const movingOffset = Math.min(target.plan.movingSourceDur, localTime);
  const sourceTime = target.seg.video_in + movingOffset;
  const isFrozen = localTime >= target.plan.movingSourceDur - 0.02;
  return {
    clip: target,
    projectTime: clamped,
    localTime,
    sourceTime,
    isFrozen,
  };
}

export function toProjectTime(
  layout: TimelineLayout,
  segId: string,
  localTime: number
): number {
  const clip = layout.clips.find((c) => c.seg.id === segId);
  if (!clip) return 0;
  return clip.startSec + Math.max(0, Math.min(clip.plan.finalDur, localTime));
}

export function splitSegmentAt(
  segment: SegmentInput,
  localTime: number,
  newSegId: string,
  sourceWidth: number,
  sourceHeight: number,
  outputWidth: number,
  outputHeight: number
): [SegmentInput, SegmentInput] | null {
  const rawDur = segment.video_out - segment.video_in;
  if (localTime <= 0.15 || localTime >= rawDur - 0.15) {
    return null;
  }
  const splitSrc = Number((segment.video_in + localTime).toFixed(2));
  if (splitSrc <= segment.video_in + 0.1 || splitSrc >= segment.video_out - 0.1) {
    return null;
  }
  const splitLocal = Number((splitSrc - segment.video_in).toFixed(2));

  const boundaryCrop = evaluateCropAt(
    segment.crop_keyframes,
    splitLocal,
    sourceWidth,
    sourceHeight,
    outputWidth,
    outputHeight
  );

  const firstKeyframes = segment.crop_keyframes
    .filter((kf) => kf.t < splitLocal - 0.02)
    .map((kf) => ({ ...kf }));

  const secondTailKeyframes = segment.crop_keyframes
    .filter((kf) => kf.t > splitLocal + 0.02)
    .map((kf) => ({
      ...kf,
      t: Number((kf.t - splitLocal).toFixed(2)),
    }));

  const secondFirstKf: CropKeyframe = {
    t: 0,
    x: Number(boundaryCrop.x.toFixed(4)),
    y: Number(boundaryCrop.y.toFixed(4)),
    zoom: Number(boundaryCrop.zoom.toFixed(3)),
    transition:
      firstKeyframes[firstKeyframes.length - 1]?.transition ?? "tween",
  };

  const firstClauses: ClauseInput[] = [];
  const secondClauses: ClauseInput[] = [];
  for (const c of segment.clauses) {
    const start = c.start_sec ?? 0;
    if (start < splitLocal) {
      firstClauses.push({ ...c });
    } else {
      secondClauses.push({
        ...c,
        start_sec: Number(Math.max(0, start - splitLocal).toFixed(2)),
      });
    }
  }

  const firstSeg: SegmentInput = {
    ...segment,
    video_out: splitSrc,
    crop_keyframes: firstKeyframes.length > 0 ? firstKeyframes : [{ ...segment.crop_keyframes[0], t: 0 }],
    clauses: firstClauses,
  };

  const secondSeg: SegmentInput = {
    ...segment,
    id: newSegId,
    video_in: splitSrc,
    crop_keyframes: [secondFirstKf, ...secondTailKeyframes],
    clauses: secondClauses,
  };

  return [firstSeg, secondSeg];
}

// ---------------------------------------------------------------------------
// Multi-Track Layered NLE Math & Migration Helpers
// ---------------------------------------------------------------------------

export function createDefaultTracks(): TimelineTrack[] {
  return [
    {
      id: "v1",
      name: "Video 1",
      kind: "video",
      visible: true,
      muted: false,
      locked: false,
      camera_follow: true,
      clips: [],
      transitions: [],
    },
    {
      id: "t1",
      name: "Captions 1",
      kind: "caption",
      visible: true,
      muted: false,
      locked: false,
      clips: [],
    },
    {
      id: "a1",
      name: "Audio 1",
      kind: "audio",
      volume: 1.0,
      muted: false,
      solo: false,
      duck_under_narration: true,
      locked: false,
      clips: [],
    },
  ];
}

export function ensureDefaultTracks(project: ProjectInput): TimelineTrack[] {
  if (project.tracks && project.tracks.length > 0) {
    return project.tracks;
  }
  return createDefaultTracks();
}

export function projectHasMultiTrackClips(project: ProjectInput): boolean {
  return (project.tracks ?? []).some((t) => t.clips.length > 0);
}

export function migrateProjectToMultiTrack(
  project: ProjectInput,
  manifest: ArtifactManifest | null
): ProjectInput {
  const existingAssets = { ...(project.media_assets ?? {}) };
  const sm = project.source_media;
  if (!existingAssets[sm.media_id]) {
    const baseName = sm.path.split(/[\\/]/).pop() || "Primary Video";
    existingAssets[sm.media_id] = {
      asset_id: sm.media_id,
      kind: "video",
      name: baseName,
      path: sm.path,
      fingerprint: sm.fingerprint,
      copied_to_project: sm.copied_to_project,
    };
  }

  const baseTracks = ensureDefaultTracks(project);
  const alreadyPopulated = baseTracks.some((t) => t.clips.length > 0);
  if (alreadyPopulated || project.segments.length === 0) {
    return {
      ...project,
      media_assets: existingAssets,
      tracks: baseTracks,
    };
  }

  const srcDur = manifest?.source_metadata?.duration ?? 0;
  const layout = computeTimelineLayout(
    project.segments,
    manifest,
    srcDur,
    project.output_settings.tail_padding_sec
  );

  const visualClips: VisualClip[] = [];
  const captionClips: CaptionClip[] = [];
  const existingKfs = [...(project.camera_keyframes ?? [])];
  const preserveCam = existingKfs.length > 0;
  const migratedKfs: CropKeyframe[] = preserveCam ? existingKfs : [];
  const usedIds = new Set<string>();

  const allocId = (preferred: string): string => {
    const base = preferred || "clip";
    if (!usedIds.has(base)) {
      usedIds.add(base);
      return base;
    }
    let idx = 2;
    while (usedIds.has(`${base}_${idx}`)) idx += 1;
    const unique = `${base}_${idx}`;
    usedIds.add(unique);
    return unique;
  };

  for (const clipLayout of layout.clips) {
    const seg = clipLayout.seg;
    visualClips.push({
      id: allocId(`vclip_${seg.id}`),
      asset_id: sm.media_id,
      kind: "video",
      start_sec: Number(clipLayout.startSec.toFixed(6)),
      duration_sec: Number(clipLayout.plan.finalDur.toFixed(6)),
      media_in: Number(seg.video_in.toFixed(6)),
      media_out: Number(seg.video_out.toFixed(6)),
      speed: 1.0,
      end_behavior: "freeze",
      volume: seg.source_audio_volume,
      audio_fade_in_sec: 0,
      audio_fade_out_sec: 0,
      transform: { x: 0.5, y: 0.5, scale: 1.0, rotation_deg: 0, opacity: 1.0 },
      transition_in: { type: "none", duration_sec: 0 },
      transition_out: { type: "none", duration_sec: 0 },
    });

    if (!preserveCam) {
      for (const kf of seg.crop_keyframes) {
        const globalT = Number((clipLayout.startSec + kf.t).toFixed(4));
        if (migratedKfs.length === 0 || globalT > migratedKfs[migratedKfs.length - 1].t + 1e-4) {
          migratedKfs.push({ ...kf, t: globalT });
        }
      }
    }

    const timings = manifest?.segments[seg.id]?.clause_timings ?? [];
    let localCursor = 0;
    seg.clauses.forEach((clause, cIdx) => {
      const timing = timings[cIdx];
      const localStart =
        clause.start_sec != null
          ? clause.start_sec
          : timing
            ? timing.start_sec
            : localCursor;
      let dur: number | null = null;
      let autoFit = true;
      if (clause.duration_sec != null) {
        dur = Number(clause.duration_sec.toFixed(6));
        autoFit = false;
        localCursor = localStart + clause.duration_sec;
      } else if (timing && timing.end_sec > timing.start_sec) {
        dur = Number((timing.end_sec - timing.start_sec).toFixed(6));
        autoFit = true;
        localCursor =
          timing.end_sec + project.output_settings.inter_clause_pause_ms / 1000;
      } else {
        localCursor = localStart + 1.5;
      }

      captionClips.push({
        id: allocId(`cap_${seg.id}_${clause.id}`),
        start_sec: Number((clipLayout.startSec + localStart).toFixed(6)),
        duration_sec: dur,
        auto_fit_tts: autoFit,
        display_text: clause.display_text,
        spoken_text_override: clause.spoken_text_override,
        tts_enabled: true,
        speed: seg.speed,
        voice_override: null,
        style_override: null,
      });
    });
  }

  const v1 = baseTracks.find((t): t is VideoTrack => t.kind === "video");
  const t1 = baseTracks.find((t): t is CaptionTrack => t.kind === "caption");
  const a1 = baseTracks.find((t): t is AudioTrack => t.kind === "audio");

  const nextV1: VideoTrack = v1
    ? { ...v1, clips: visualClips, transitions: [] }
    : {
        id: "v1",
        name: "Video 1",
        kind: "video",
        visible: true,
        muted: false,
        locked: false,
        camera_follow: true,
        clips: visualClips,
        transitions: [],
      };
  const nextT1: CaptionTrack = t1
    ? { ...t1, clips: captionClips }
    : {
        id: "t1",
        name: "Captions 1",
        kind: "caption",
        visible: true,
        muted: false,
        locked: false,
        clips: captionClips,
      };
  const nextA1: AudioTrack = a1 ?? {
    id: "a1",
    name: "Audio 1",
    kind: "audio",
    volume: 1.0,
    muted: false,
    solo: false,
    duck_under_narration: true,
    locked: false,
    clips: [],
  };
  const others = baseTracks.filter(
    (t) => t.id !== nextV1.id && t.id !== nextT1.id && t.id !== nextA1.id
  );

  return {
    ...project,
    media_assets: existingAssets,
    camera_keyframes: migratedKfs,
    tracks: [nextV1, nextT1, nextA1, ...others],
  };
}

export function getCaptionClipEffectiveDuration(
  clip: CaptionClip,
  manifest: ArtifactManifest | null
): number {
  if (!clip.auto_fit_tts && clip.duration_sec && clip.duration_sec > 0) {
    return clip.duration_sec;
  }
  const artDur = manifest?.caption_artifacts?.[clip.id]?.audio_duration ?? 0;
  if (clip.tts_enabled && artDur > 0) {
    return artDur;
  }
  if (clip.duration_sec && clip.duration_sec > 0) {
    return clip.duration_sec;
  }
  const wordCount = Math.max(1, clip.display_text.trim().split(/\s+/).filter(Boolean).length);
  return Math.max(1.2, Math.min(8.0, wordCount * 0.42));
}

export function getProjectDuration(
  project: ProjectInput,
  manifest: ArtifactManifest | null,
  minDuration = 0
): number {
  let maxEnd = minDuration;
  for (const kf of project.camera_keyframes ?? []) {
    if (kf.t > maxEnd) maxEnd = kf.t;
  }
  const tracks = project.tracks ?? [];
  for (const tr of tracks) {
    if (tr.kind === "video") {
      for (const c of tr.clips) {
        maxEnd = Math.max(maxEnd, c.start_sec + c.duration_sec);
      }
    } else if (tr.kind === "audio") {
      for (const c of tr.clips) {
        maxEnd = Math.max(maxEnd, c.start_sec + c.duration_sec);
      }
    } else if (tr.kind === "caption") {
      for (const c of tr.clips) {
        maxEnd = Math.max(
          maxEnd,
          c.start_sec + getCaptionClipEffectiveDuration(c, manifest)
        );
      }
    }
  }
  if (!projectHasMultiTrackClips(project) && project.segments.length > 0) {
    const srcDur = manifest?.source_metadata?.duration ?? 0;
    const layout = computeTimelineLayout(
      project.segments,
      manifest,
      srcDur,
      project.output_settings.tail_padding_sec
    );
    maxEnd = Math.max(maxEnd, layout.totalDuration);
  }
  return maxEnd;
}

export function computeVisualSourceTime(
  clip: VisualClip,
  projectTime: number,
  assetDuration = 0
): { localTime: number; sourceTime: number; isFrozen: boolean } {
  const localTime = Math.max(0, Math.min(clip.duration_sec, projectTime - clip.start_sec));
  if (clip.kind === "image") {
    return { localTime, sourceTime: 0, isFrozen: false };
  }
  const fallbackTrim = Math.max(0.05, clip.duration_sec * clip.speed);
  const availAfterIn =
    assetDuration > clip.media_in ? assetDuration - clip.media_in : fallbackTrim;
  const trimDur =
    clip.media_out != null
      ? Math.max(0.01, clip.media_out - clip.media_in)
      : availAfterIn;
  const scaled = localTime * clip.speed;
  if (clip.end_behavior === "loop") {
    const wrapped = trimDur > 0 ? scaled % trimDur : 0;
    return { localTime, sourceTime: clip.media_in + wrapped, isFrozen: false };
  }
  const isFrozen = scaled >= trimDur - 1e-4;
  return {
    localTime,
    sourceTime: clip.media_in + Math.min(scaled, trimDur),
    isFrozen,
  };
}

export interface ActiveVisualLayer {
  track: VideoTrack;
  trackIndex: number;
  clip: VisualClip;
  localTime: number;
}

export function findActiveVisualLayersAt(
  tracks: TimelineTrack[],
  projectTime: number
): ActiveVisualLayer[] {
  const active: ActiveVisualLayer[] = [];
  const t = Math.max(0, projectTime);
  tracks.forEach((track, trackIndex) => {
    if (track.kind !== "video" || !track.visible) return;
    const candidates = track.clips.filter(
      (c) => t >= c.start_sec - 1e-6 && t < c.start_sec + c.duration_sec - 1e-6
    );
    const chosen =
      candidates.length > 0
        ? candidates[candidates.length - 1]
        : track.clips.find((c) => Math.abs(t - (c.start_sec + c.duration_sec)) < 1e-6 && Math.abs(t - c.start_sec) > 0);
    if (chosen) {
      active.push({
        track,
        trackIndex,
        clip: chosen,
        localTime: Math.max(0, Math.min(chosen.duration_sec, t - chosen.start_sec)),
      });
    }
  });
  return active;
}

export interface ActiveAudioLayer {
  track: AudioTrack;
  clip: AudioClip;
  localTime: number;
  effectiveVolume: number;
}

export function computeAudioFadeGainAt(
  durationSec: number,
  fadeInSec: number,
  fadeOutSec: number,
  localTime: number
): number {
  let gain = 1.0;
  if (fadeInSec > 1e-4 && localTime < fadeInSec) {
    gain = Math.min(gain, Math.max(0, localTime / fadeInSec));
  }
  const rem = durationSec - localTime;
  if (fadeOutSec > 1e-4 && rem < fadeOutSec) {
    gain = Math.min(gain, Math.max(0, rem / fadeOutSec));
  }
  return Math.max(0, Math.min(1, gain));
}

export function findActiveAudioClipsAt(
  tracks: TimelineTrack[],
  projectTime: number
): ActiveAudioLayer[] {
  const audioTracks = tracks.filter((t): t is AudioTrack => t.kind === "audio");
  const hasSolo = audioTracks.some((t) => t.solo && !t.muted);
  const t = Math.max(0, projectTime);
  const out: ActiveAudioLayer[] = [];
  for (const tr of audioTracks) {
    if (tr.muted) continue;
    if (hasSolo && !tr.solo) continue;
    for (const clip of tr.clips) {
      if (t >= clip.start_sec - 1e-6 && t < clip.start_sec + clip.duration_sec - 1e-6) {
        const localTime = Math.max(0, t - clip.start_sec);
        const fadeGain = computeAudioFadeGainAt(
          clip.duration_sec,
          clip.fade_in_sec,
          clip.fade_out_sec,
          localTime
        );
        out.push({
          track: tr,
          clip,
          localTime,
          effectiveVolume: Math.max(0, Math.min(2.0, tr.volume * clip.volume * fadeGain)),
        });
      }
    }
  }
  return out;
}

export interface ActiveCaptionLayer {
  track: CaptionTrack;
  clip: CaptionClip;
  localTime: number;
  effectiveDuration: number;
}

export function findActiveCaptionsAt(
  tracks: TimelineTrack[],
  manifest: ArtifactManifest | null,
  projectTime: number
): ActiveCaptionLayer[] {
  const t = Math.max(0, projectTime);
  const out: ActiveCaptionLayer[] = [];
  for (const tr of tracks) {
    if (tr.kind !== "caption" || !tr.visible) continue;
    for (const clip of tr.clips) {
      const dur = getCaptionClipEffectiveDuration(clip, manifest);
      if (t >= clip.start_sec - 1e-6 && t < clip.start_sec + dur - 1e-6) {
        out.push({
          track: tr,
          clip,
          localTime: Math.max(0, t - clip.start_sec),
          effectiveDuration: dur,
        });
      }
    }
  }
  return out;
}

export function computeAudioDuckingFactorAt(
  tracks: TimelineTrack[],
  manifest: ArtifactManifest | null,
  projectTime: number,
  duckGain = 0.28
): number {
  const t = Math.max(0, projectTime);
  for (const tr of tracks) {
    if (tr.kind !== "caption" || tr.muted) continue;
    for (const clip of tr.clips) {
      if (!clip.tts_enabled) continue;
      const dur = getCaptionClipEffectiveDuration(clip, manifest);
      if (t >= clip.start_sec - 0.05 && t <= clip.start_sec + dur + 0.05) {
        return duckGain;
      }
    }
  }
  return 1.0;
}

export interface EdgeTransitionVisualState {
  opacityMultiplier: number;
  offsetXPct: number;
  offsetYPct: number;
  scaleMultiplier: number;
  clipPathCss: string | undefined;
}

export function computeClipEdgeTransitionState(
  clip: VisualClip,
  localTime: number
): EdgeTransitionVisualState {
  const state: EdgeTransitionVisualState = {
    opacityMultiplier: 1.0,
    offsetXPct: 0,
    offsetYPct: 0,
    scaleMultiplier: 1.0,
    clipPathCss: undefined,
  };

  const dur = Math.max(0.01, clip.duration_sec);
  let dIn = clip.transition_in.type !== "none" ? clip.transition_in.duration_sec : 0;
  let dOut = clip.transition_out.type !== "none" ? clip.transition_out.duration_sec : 0;
  if (dIn + dOut > dur) {
    const scaleDown = dur / (dIn + dOut);
    dIn *= scaleDown;
    dOut *= scaleDown;
  }

  if (dIn > 1e-4 && localTime < dIn) {
    const p = Math.max(0, Math.min(1, localTime / dIn));
    switch (clip.transition_in.type) {
      case "fade":
        state.opacityMultiplier *= p;
        break;
      case "slide_left":
        state.offsetXPct += (1 - p) * 100;
        break;
      case "slide_right":
        state.offsetXPct -= (1 - p) * 100;
        break;
      case "slide_up":
        state.offsetYPct += (1 - p) * 100;
        break;
      case "slide_down":
        state.offsetYPct -= (1 - p) * 100;
        break;
      case "wipe_left":
        state.clipPathCss = `inset(0 ${((1 - p) * 100).toFixed(2)}% 0 0)`;
        break;
      case "wipe_right":
        state.clipPathCss = `inset(0 0 0 ${((1 - p) * 100).toFixed(2)}%)`;
        break;
      case "zoom":
        state.scaleMultiplier *= 0.6 + 0.4 * p;
        state.opacityMultiplier *= p;
        break;
      default:
        break;
    }
  }

  const rem = dur - localTime;
  if (dOut > 1e-4 && rem < dOut) {
    const p = Math.max(0, Math.min(1, rem / dOut)); // 1 -> 0 as clip exits
    const q = 1 - p; // 0 -> 1 progress through out transition
    switch (clip.transition_out.type) {
      case "fade":
        state.opacityMultiplier *= p;
        break;
      case "slide_left":
        state.offsetXPct -= q * 100;
        break;
      case "slide_right":
        state.offsetXPct += q * 100;
        break;
      case "slide_up":
        state.offsetYPct -= q * 100;
        break;
      case "slide_down":
        state.offsetYPct += q * 100;
        break;
      case "wipe_left":
        state.clipPathCss = `inset(0 0 0 ${(q * 100).toFixed(2)}%)`;
        break;
      case "wipe_right":
        state.clipPathCss = `inset(0 ${(q * 100).toFixed(2)}% 0 0)`;
        break;
      case "zoom":
        state.scaleMultiplier *= 1.0 + 0.35 * q;
        state.opacityMultiplier *= p;
        break;
      default:
        break;
    }
  }

  return state;
}

export interface AdjacentJunctionPair {
  fromClip: VisualClip;
  toClip: VisualClip;
  existing: TrackTransition | null;
  gapSec: number;
  junctionTime: number;
}

export function computeAdjacentJunctionPairs(track: VideoTrack): AdjacentJunctionPair[] {
  const sorted = [...track.clips].sort((a, b) => a.start_sec - b.start_sec || a.id.localeCompare(b.id));
  const pairs: AdjacentJunctionPair[] = [];
  for (let i = 0; i < sorted.length - 1; i++) {
    const a = sorted[i];
    const b = sorted[i + 1];
    const aEnd = a.start_sec + a.duration_sec;
    const gapSec = b.start_sec - aEnd;
    if (gapSec <= 0.12 && gapSec >= -Math.min(a.duration_sec, b.duration_sec) + 0.05) {
      const existing =
        track.transitions.find(
          (tr) => tr.from_clip_id === a.id && tr.to_clip_id === b.id
        ) ?? null;
      pairs.push({
        fromClip: a,
        toClip: b,
        existing,
        gapSec,
        junctionTime: Math.max(a.start_sec, Math.min(b.start_sec, aEnd)),
      });
    }
  }
  return pairs;
}

export function resolveCaptionClipSpokenText(
  clip: CaptionClip,
  pronunciations: Record<string, string>
): string {
  if (clip.spoken_text_override !== null) {
    return clip.spoken_text_override;
  }
  return applyPronunciations(clip.display_text, pronunciations);
}

/**
 * Intentionally excludes start_sec & duration_sec so moving or resizing a caption
 * clip along the timeline preserves cached Kokoro TTS audio without invalidation.
 */
export function computeCaptionClipTtsSignature(
  clip: CaptionClip,
  pronunciations: Record<string, string>,
  defaultVoice: string,
  language: string
): string {
  const sortedPron = Object.entries(pronunciations).sort(([a], [b]) =>
    a.localeCompare(b)
  );
  return JSON.stringify({
    id: clip.id,
    tts_enabled: clip.tts_enabled,
    voice: clip.voice_override || defaultVoice,
    language,
    speed: Number(clip.speed.toFixed(4)),
    display_text: clip.display_text,
    spoken_text_override: clip.spoken_text_override,
    resolved: resolveCaptionClipSpokenText(clip, pronunciations),
    pronunciations: sortedPron,
  });
}

export function computeMultiTrackPreviewSignature(project: ProjectInput): string {
  return JSON.stringify({
    tracks: project.tracks ?? [],
    camera_keyframes: project.camera_keyframes ?? [],
    media_assets: project.media_assets ?? {},
    voice: project.voice,
    language: project.language,
    pronunciations: project.pronunciations,
    output_settings: project.output_settings,
    caption_settings: project.caption_settings,
    source_media: project.source_media,
  });
}

export function splitVisualClipAt(
  clip: VisualClip,
  globalTime: number,
  newClipId: string
): [VisualClip, VisualClip] | null {
  const local = Number((globalTime - clip.start_sec).toFixed(3));
  if (local <= 0.1 || local >= clip.duration_sec - 0.1) {
    return null;
  }
  const secondDur = Number((clip.duration_sec - local).toFixed(3));
  const mediaSplit =
    clip.kind === "video"
      ? Number((clip.media_in + local * clip.speed).toFixed(3))
      : 0;

  const first: VisualClip = {
    ...clip,
    duration_sec: local,
    media_out: clip.kind === "video" ? mediaSplit : clip.media_out,
    transition_out: { type: "none", duration_sec: 0 },
  };
  const second: VisualClip = {
    ...clip,
    id: newClipId,
    start_sec: Number((clip.start_sec + local).toFixed(3)),
    duration_sec: secondDur,
    media_in: clip.kind === "video" ? mediaSplit : 0,
    media_out: clip.media_out,
    transition_in: { type: "none", duration_sec: 0 },
  };
  return [first, second];
}

export function snapTimelineTime(
  rawTime: number,
  candidates: number[],
  thresholdSec: number
): number {
  const clamped = Math.max(0, rawTime);
  let best = clamped;
  let bestDist = thresholdSec;
  for (const cand of candidates) {
    const dist = Math.abs(cand - clamped);
    if (dist <= bestDist) {
      bestDist = dist;
      best = Math.max(0, cand);
    }
  }
  return Number(best.toFixed(3));
}

export function collectTimelineSnapCandidates(
  project: ProjectInput,
  manifest: ArtifactManifest | null,
  excludeClipId?: string,
  extraPoints: number[] = []
): number[] {
  const pts = new Set<number>([0, ...extraPoints]);
  for (const kf of project.camera_keyframes ?? []) {
    pts.add(kf.t);
  }
  for (const m of project.markers ?? []) {
    pts.add(m.t);
  }
  for (const tr of project.tracks ?? []) {
    if (tr.kind === "video" || tr.kind === "audio") {
      for (const c of tr.clips) {
        if (c.id === excludeClipId) continue;
        pts.add(c.start_sec);
        pts.add(c.start_sec + c.duration_sec);
      }
    } else if (tr.kind === "caption") {
      for (const c of tr.clips) {
        if (c.id === excludeClipId) continue;
        pts.add(c.start_sec);
        pts.add(c.start_sec + getCaptionClipEffectiveDuration(c, manifest));
      }
    }
  }
  return Array.from(pts);
}

export function splitAudioClipAt(
  clip: AudioClip,
  globalTime: number,
  newClipId: string
): [AudioClip, AudioClip] | null {
  const local = Number((globalTime - clip.start_sec).toFixed(3));
  if (local <= 0.1 || local >= clip.duration_sec - 0.1) {
    return null;
  }
  const secondDur = Number((clip.duration_sec - local).toFixed(3));
  const mediaSplit = Number((clip.media_in + local * clip.speed).toFixed(3));
  const first: AudioClip = {
    ...clip,
    duration_sec: local,
    fade_out_sec: 0,
  };
  const second: AudioClip = {
    ...clip,
    id: newClipId,
    start_sec: Number((clip.start_sec + local).toFixed(3)),
    duration_sec: secondDur,
    media_in: mediaSplit,
    fade_in_sec: 0,
  };
  return [first, second];
}

export function findNextClipId(project: ProjectInput, prefix: string): string {
  const used = new Set<string>();
  for (const tr of project.tracks ?? []) {
    for (const c of tr.clips) used.add(c.id);
  }
  let idx = 1;
  while (used.has(`${prefix}_${String(idx).padStart(2, "0")}`)) {
    idx += 1;
  }
  return `${prefix}_${String(idx).padStart(2, "0")}`;
}

export function findNextTrackId(project: ProjectInput, prefix: string): string {
  const used = new Set((project.tracks ?? []).map((t) => t.id));
  let idx = 1;
  while (used.has(`${prefix}${idx}`)) {
    idx += 1;
  }
  return `${prefix}${idx}`;
}

// ---------------------------------------------------------------------------
// QOL Helpers: Ripple Delete, Close Gap, Cut Navigation, Bulk Script, Colors
// ---------------------------------------------------------------------------

export function rippleDeleteClip(
  tracks: TimelineTrack[],
  trackId: string,
  clipId: string,
  manifest?: ArtifactManifest | null
): TimelineTrack[] {
  return tracks.map((tr) => {
    if (tr.id !== trackId) return tr;
    if (tr.kind === "video") {
      const target = tr.clips.find((c) => c.id === clipId);
      if (!target) return tr;
      const shift = target.duration_sec;
      const cutoff = target.start_sec;
      return {
        ...tr,
        clips: tr.clips
          .filter((c) => c.id !== clipId)
          .map((c) =>
            c.start_sec >= cutoff - 1e-4
              ? { ...c, start_sec: Number(Math.max(0, c.start_sec - shift).toFixed(3)) }
              : c
          )
          .sort((a, b) => a.start_sec - b.start_sec),
        transitions: tr.transitions.filter(
          (jt) => jt.from_clip_id !== clipId && jt.to_clip_id !== clipId
        ),
      };
    }
    if (tr.kind === "audio") {
      const target = tr.clips.find((c) => c.id === clipId);
      if (!target) return tr;
      const shift = target.duration_sec;
      const cutoff = target.start_sec;
      return {
        ...tr,
        clips: tr.clips
          .filter((c) => c.id !== clipId)
          .map((c) =>
            c.start_sec >= cutoff - 1e-4
              ? { ...c, start_sec: Number(Math.max(0, c.start_sec - shift).toFixed(3)) }
              : c
          )
          .sort((a, b) => a.start_sec - b.start_sec),
      };
    }
    const target = tr.clips.find((c) => c.id === clipId);
    if (!target) return tr;
    const shift = getCaptionClipEffectiveDuration(target, manifest ?? null);
    const cutoff = target.start_sec;
    return {
      ...tr,
      clips: tr.clips
        .filter((c) => c.id !== clipId)
        .map((c) =>
          c.start_sec >= cutoff - 1e-4
            ? { ...c, start_sec: Number(Math.max(0, c.start_sec - shift).toFixed(3)) }
            : c
        )
        .sort((a, b) => a.start_sec - b.start_sec),
    };
  });
}

export function closeGapOnTrack(
  tracks: TimelineTrack[],
  trackId: string,
  gapTime: number,
  manifest?: ArtifactManifest | null
): TimelineTrack[] {
  return tracks.map((tr) => {
    if (tr.id !== trackId) return tr;
    const intervals = tr.clips
      .map((c) => {
        const dur =
          tr.kind === "caption"
            ? getCaptionClipEffectiveDuration(c as CaptionClip, manifest ?? null)
            : (c as VisualClip | AudioClip).duration_sec;
        return { start: c.start_sec, end: c.start_sec + dur };
      })
      .sort((a, b) => a.start - b.start);

    // Find the gap containing gapTime
    let prevEnd = 0;
    let nextStart: number | null = null;
    for (const iv of intervals) {
      if (gapTime >= prevEnd - 1e-3 && gapTime <= iv.start + 1e-3 && iv.start > prevEnd + 0.02) {
        nextStart = iv.start;
        break;
      }
      prevEnd = Math.max(prevEnd, iv.end);
    }
    if (nextStart === null) return tr;
    const gapLen = nextStart - prevEnd;
    if (gapLen <= 0.01) return tr;

    if (tr.kind === "video") {
      return {
        ...tr,
        clips: tr.clips
          .map((c) =>
            c.start_sec >= nextStart! - 1e-4
              ? { ...c, start_sec: Number(Math.max(0, c.start_sec - gapLen).toFixed(3)) }
              : c
          )
          .sort((a, b) => a.start_sec - b.start_sec),
      };
    }
    if (tr.kind === "audio") {
      return {
        ...tr,
        clips: tr.clips
          .map((c) =>
            c.start_sec >= nextStart! - 1e-4
              ? { ...c, start_sec: Number(Math.max(0, c.start_sec - gapLen).toFixed(3)) }
              : c
          )
          .sort((a, b) => a.start_sec - b.start_sec),
      };
    }
    return {
      ...tr,
      clips: tr.clips
        .map((c) =>
          c.start_sec >= nextStart! - 1e-4
            ? { ...c, start_sec: Number(Math.max(0, c.start_sec - gapLen).toFixed(3)) }
            : c
        )
        .sort((a, b) => a.start_sec - b.start_sec),
    };
  });
}

export function findCutPoints(
  project: ProjectInput,
  manifest: ArtifactManifest | null
): number[] {
  const pts = collectTimelineSnapCandidates(project, manifest);
  const unique = Array.from(new Set(pts.map((p) => Number(p.toFixed(3)))));
  unique.sort((a, b) => a - b);
  return unique;
}

export function parseBulkScriptToCaptionClips(
  scriptText: string,
  startSec: number,
  project: ProjectInput,
  options?: { ttsEnabled?: boolean; speed?: number; pauseSec?: number }
): CaptionClip[] {
  const rawLines = scriptText
    .split(/\r?\n+/)
    .map((l) => l.trim())
    .filter(Boolean);
  const chunks: string[] = [];
  for (const line of rawLines) {
    const parts = autoSplitTextToClauses(line).filter((p) => p.trim().length > 0);
    chunks.push(...parts);
  }
  if (chunks.length === 0) return [];

  const used = new Set<string>();
  for (const tr of project.tracks ?? []) {
    for (const c of tr.clips) used.add(c.id);
  }
  let idx = 1;
  const allocCapId = () => {
    while (used.has(`cap_${String(idx).padStart(2, "0")}`)) idx += 1;
    const id = `cap_${String(idx).padStart(2, "0")}`;
    used.add(id);
    return id;
  };

  const ttsEnabled = options?.ttsEnabled ?? true;
  const speed = options?.speed ?? 1.0;
  const pauseSec = options?.pauseSec ?? 0.15;
  let cursor = Math.max(0, Number(startSec.toFixed(3)));

  return chunks.map((text) => {
    const words = Math.max(1, text.split(/\s+/).length);
    const estDur = Number(Math.max(1.0, Math.min(6.0, (words * 0.42) / speed)).toFixed(3));
    const clip: CaptionClip = {
      id: allocCapId(),
      start_sec: cursor,
      duration_sec: estDur,
      auto_fit_tts: ttsEnabled,
      display_text: text,
      spoken_text_override: null,
      tts_enabled: ttsEnabled,
      speed,
      voice_override: null,
      style_override: null,
    };
    cursor = Number((cursor + estDur + pauseSec).toFixed(3));
    return clip;
  });
}

/** Convert ASS &HAABBGGRR (or &HBBGGRR) color string to CSS #RRGGBB hex. */
export function assColorToHex(assColor?: string | null, fallback = "#ffffff"): string {
  if (!assColor) return fallback;
  const m = assColor.trim().match(/^&H([0-9A-Fa-f]{6,8})&?$/);
  if (!m) {
    return assColor.startsWith("#") && assColor.length === 7 ? assColor : fallback;
  }
  const hex = m[1];
  const bgr = hex.length === 8 ? hex.slice(2) : hex;
  const bb = bgr.slice(0, 2);
  const gg = bgr.slice(2, 4);
  const rr = bgr.slice(4, 6);
  return `#${rr}${gg}${bb}`.toLowerCase();
}

/** Convert CSS #RRGGBB hex + optional alphaByte (00=opaque, 64=semi) to ASS &HAABBGGRR. */
export function hexToAssColor(hexColor: string, alphaHex = "00"): string {
  const clean = hexColor.replace(/^#/, "").trim();
  if (clean.length !== 6 || !/^[0-9A-Fa-f]{6}$/.test(clean)) {
    return `&H${alphaHex}FFFFFF`;
  }
  const rr = clean.slice(0, 2).toUpperCase();
  const gg = clean.slice(2, 4).toUpperCase();
  const bb = clean.slice(4, 6).toUpperCase();
  return `&H${alphaHex.toUpperCase()}${bb}${gg}${rr}`;
}

export interface CaptionStylePreset {
  id: string;
  label: string;
  patch: Partial<ProjectInput["caption_settings"]>;
}

export const CAPTION_STYLE_PRESETS: CaptionStylePreset[] = [
  {
    id: "classic-bold",
    label: "Classic Bold",
    patch: {
      font_size: 64,
      primary_color: "&H00FFFFFF",
      outline_color: "&H00000000",
      back_color: "&H64000000",
      outline_width: 5,
      shadow: 0,
      border_style: 1,
      karaoke_highlight: false,
      animation: "none",
    },
  },
  {
    id: "yellow-pop",
    label: "Yellow Pop",
    patch: {
      font_size: 72,
      primary_color: "&H0000E5FF",
      outline_color: "&H00000000",
      back_color: "&H64000000",
      outline_width: 6,
      shadow: 2,
      border_style: 1,
      karaoke_highlight: false,
      animation: "pop",
    },
  },
  {
    id: "subtitle-pill",
    label: "Subtitle Box",
    patch: {
      font_size: 56,
      primary_color: "&H00FFFFFF",
      outline_color: "&H00000000",
      back_color: "&H40000000",
      outline_width: 4,
      shadow: 0,
      border_style: 3,
      karaoke_highlight: false,
      animation: "fade",
    },
  },
  {
    id: "karaoke-neon",
    label: "Karaoke Highlight",
    patch: {
      font_size: 68,
      primary_color: "&H00FFFFFF",
      highlight_color: "&H0000FFFF",
      outline_color: "&H00000000",
      back_color: "&H64000000",
      outline_width: 6,
      shadow: 2,
      border_style: 1,
      karaoke_highlight: true,
      animation: "pop",
    },
  },
];

