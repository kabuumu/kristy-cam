import type {
  ArtifactManifest,
  ClauseInput,
  CropKeyframe,
  SegmentInput,
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
      const alpha =
        a.transition === "snap" && t < b.t
          ? 0
          : span > 0
            ? (t - a.t) / span
            : 0;
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

