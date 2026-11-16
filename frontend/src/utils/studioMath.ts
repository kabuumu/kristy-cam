import type {
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

export function computeIdealCrop(
  sourceWidth: number,
  sourceHeight: number,
  zoom: number
): IdealCropDimensions {
  const safeW = Math.max(1, sourceWidth);
  const safeH = Math.max(1, sourceHeight);
  const clampedZoom = Math.min(3.0, Math.max(1.0, zoom));
  const baseCropH = safeH;
  const baseCropW = safeH * (9 / 16);
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
  sourceHeight: number
): { x: number; y: number; zoom: number } {
  const clampedZoom = Math.min(3.0, Math.max(1.0, zoom));
  const { maxNormX, maxNormY } = computeIdealCrop(
    sourceWidth,
    sourceHeight,
    clampedZoom
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
  sourceHeight: number
): { x: number; y: number; zoom: number } {
  if (!keyframes.length) {
    return clampCropPosition(0.28, 0, 1.0, sourceWidth, sourceHeight);
  }
  const sorted = [...keyframes].sort((a, b) => a.t - b.t);
  if (t <= sorted[0].t) {
    return clampCropPosition(
      sorted[0].x,
      sorted[0].y,
      sorted[0].zoom,
      sourceWidth,
      sourceHeight
    );
  }
  const last = sorted[sorted.length - 1];
  if (t >= last.t) {
    return clampCropPosition(
      last.x,
      last.y,
      last.zoom,
      sourceWidth,
      sourceHeight
    );
  }
  for (let i = 0; i < sorted.length - 1; i++) {
    const a = sorted[i];
    const b = sorted[i + 1];
    if (t >= a.t && t <= b.t) {
      const span = b.t - a.t;
      const alpha = span > 0 ? (t - a.t) / span : 0;
      const x = a.x + (b.x - a.x) * alpha;
      const y = a.y + (b.y - a.y) * alpha;
      const zoom = a.zoom + (b.zoom - a.zoom) * alpha;
      return clampCropPosition(x, y, zoom, sourceWidth, sourceHeight);
    }
  }
  return clampCropPosition(last.x, last.y, last.zoom, sourceWidth, sourceHeight);
}

export interface DurationPlan {
  rawVideoDur: number;
  narrationDur: number;
  rawTarget: number;
  frameCount: number;
  finalDur: number;
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
    })),
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
