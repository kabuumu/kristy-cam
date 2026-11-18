import { useEffect, useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  BookOpen,
  Captions,
  CheckCircle2,
  Copy,
  Film,
  KeyRound,
  Layers,
  Loader2,
  Music,
  Plus,
  RotateCcw,
  Scissors,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Volume2,
  Zap,
} from "lucide-react";
import { apiClient } from "../api/client";
import type {
  ArtifactManifest,
  AudioClip,
  AudioTrack,
  CaptionAlign,
  CaptionClip,
  CaptionTrack,
  CropKeyframe,
  EdgeTransitionType,
  JunctionTransitionType,
  KeyframeTransition,
  ProjectInput,
  TimelineSelection,
  TimelineTrack,
  VideoTrack,
  VisualClip,
} from "../types/studio";
import {
  autoSplitTextToClauses,
  clampCropPosition,
  computeCaptionClipTtsSignature,
  findNextClipId,
  formatTimecode,
  getCaptionClipEffectiveDuration,
  resolveCaptionClipSpokenText,
  splitAudioClipAt,
  splitVisualClipAt,
} from "../utils/studioMath";

interface ContextualInspectorProps {
  project: ProjectInput;
  manifest: ArtifactManifest;
  selection: TimelineSelection;
  projectTime: number;
  voices: string[];
  ttsError: string | null;
  synthesizingClipId?: string | null;
  lastSynthesizedCaptionSignatures: Record<string, string>;
  onUpdateProject: (
    updater: (prev: ProjectInput) => ProjectInput,
    meta?: { label?: string; coalesceKey?: string },
    nextSel?: { selectedSegId?: string | null; selection?: TimelineSelection }
  ) => void;
  onUpdateTracks: (
    updater: (tracks: TimelineTrack[]) => TimelineTrack[],
    meta?: { label?: string; coalesceKey?: string },
    nextSelection?: TimelineSelection
  ) => void;
  onUpdateCameraKeyframes: (
    keyframes: CropKeyframe[],
    meta?: { label?: string; coalesceKey?: string }
  ) => void;
  onSynthesizeCaptionTts: (clipId?: string) => Promise<void>;
  onSelectItem: (sel: TimelineSelection) => void;
}

const EDGE_TRANSITIONS: Array<{ value: EdgeTransitionType; label: string }> = [
  { value: "none", label: "None" },
  { value: "fade", label: "Fade" },
  { value: "slide_left", label: "Slide Left" },
  { value: "slide_right", label: "Slide Right" },
  { value: "slide_up", label: "Slide Up" },
  { value: "slide_down", label: "Slide Down" },
  { value: "wipe_left", label: "Wipe Left" },
  { value: "wipe_right", label: "Wipe Right" },
  { value: "zoom", label: "Zoom" },
];

const JUNCTION_TRANSITIONS: Array<{ value: JunctionTransitionType; label: string }> = [
  { value: "crossfade", label: "Crossfade" },
  { value: "dissolve", label: "Dissolve" },
  { value: "wipeleft", label: "Wipe Left" },
  { value: "wiperight", label: "Wipe Right" },
  { value: "slideleft", label: "Slide Left" },
  { value: "slideright", label: "Slide Right" },
  { value: "fadeblack", label: "Fade to Black" },
  { value: "zoomin", label: "Zoom In" },
];

interface BufferedNumberInputProps {
  value: number;
  min?: number;
  max?: number;
  step?: number;
  precision?: number;
  strictGreaterThanMin?: boolean;
  strictLessThanMax?: boolean;
  onCommit: (val: number) => void;
  className?: string;
}

function BufferedNumberInput({
  value,
  min,
  max,
  step = 0.1,
  precision = 2,
  strictGreaterThanMin = false,
  strictLessThanMax = false,
  onCommit,
  className,
}: BufferedNumberInputProps) {
  const factor = 10 ** precision;
  const formatVal = (v: number) => String(Number(v.toFixed(precision)));
  const [focused, setFocused] = useState(false);
  const [draft, setDraft] = useState(() => formatVal(value));

  useEffect(() => {
    if (!focused) {
      setDraft(formatVal(value));
    }
  }, [value, focused, precision]);

  const isInRange = (val: number) => {
    if (min !== undefined && (strictGreaterThanMin ? val <= min : val < min)) return false;
    if (max !== undefined && (strictLessThanMax ? val >= max : val > max)) return false;
    return true;
  };

  const clampVal = (val: number) => {
    let next = val;
    if (min !== undefined) {
      const floorMin = strictGreaterThanMin ? min + 1 / factor : min;
      next = Math.max(floorMin, next);
    }
    if (max !== undefined) {
      const ceilMax = strictLessThanMax ? max - 1 / factor : max;
      next = Math.min(ceilMax, next);
    }
    let rounded = Number(next.toFixed(precision));
    if (max !== undefined && (strictLessThanMax ? rounded >= max : rounded > max)) {
      rounded = Math.floor((strictLessThanMax ? max - 1 / factor : max) * factor) / factor;
    }
    if (min !== undefined && (strictGreaterThanMin ? rounded <= min : rounded < min)) {
      rounded = Math.ceil((strictGreaterThanMin ? min + 1 / factor : min) * factor) / factor;
    }
    return rounded;
  };

  const commitDraft = () => {
    const parsed = parseFloat(draft);
    if (!Number.isFinite(parsed)) {
      setDraft(formatVal(value));
      return;
    }
    const clamped = clampVal(parsed);
    if (isInRange(clamped)) {
      onCommit(clamped);
      setDraft(formatVal(clamped));
    } else {
      setDraft(formatVal(value));
    }
  };

  const minAttr =
    min !== undefined
      ? Math.ceil((strictGreaterThanMin ? min + 0.01 : min) * factor) / factor
      : undefined;
  const maxAttr =
    max !== undefined
      ? Math.floor((strictLessThanMax ? max - 0.01 : max) * factor) / factor
      : undefined;

  return (
    <input
      type="number"
      step={step}
      min={minAttr}
      max={maxAttr !== undefined && minAttr !== undefined ? Math.max(minAttr, maxAttr) : maxAttr}
      value={focused ? draft : formatVal(value)}
      onFocus={() => {
        setFocused(true);
        setDraft(formatVal(value));
      }}
      onChange={(e) => {
        const raw = e.target.value;
        setDraft(raw);
        const parsed = parseFloat(raw);
        if (Number.isFinite(parsed) && isInRange(parsed)) {
          onCommit(Number(parsed.toFixed(precision)));
        }
      }}
      onBlur={() => {
        setFocused(false);
        commitDraft();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          commitDraft();
          e.currentTarget.blur();
        } else if (e.key === "Escape") {
          setDraft(formatVal(value));
          setFocused(false);
          e.currentTarget.blur();
        }
      }}
      className={className}
    />
  );
}

export function ContextualInspector({
  project,
  manifest,
  selection,
  projectTime,
  voices,
  ttsError,
  synthesizingClipId = null,
  lastSynthesizedCaptionSignatures,
  onUpdateProject,
  onUpdateTracks,
  onUpdateCameraKeyframes,
  onSynthesizeCaptionTts,
  onSelectItem,
}: ContextualInspectorProps) {
  const [newPronKey, setNewPronKey] = useState("");
  const [newPronVal, setNewPronVal] = useState("");
  const [showPronEditor, setShowPronEditor] = useState(false);

  const srcW = manifest.source_metadata?.width ?? 1920;
  const srcH = manifest.source_metadata?.height ?? 1080;
  const outW = project.output_settings.width;
  const outH = project.output_settings.height;
  const tracks = project.tracks ?? [];

  const voiceOptions =
    voices.length > 0
      ? voices.includes(project.voice)
        ? voices
        : [project.voice, ...voices]
      : [project.voice];

  const handleAddPronunciation = () => {
    const k = newPronKey.trim();
    const v = newPronVal.trim();
    if (!k || !v) return;
    onUpdateProject(
      (prev) => ({
        ...prev,
        pronunciations: { ...prev.pronunciations, [k]: v },
      }),
      { label: `Add pronunciation "${k}"` }
    );
    setNewPronKey("");
    setNewPronVal("");
  };

  const handleDeletePronunciation = (key: string) => {
    onUpdateProject(
      (prev) => {
        const next = { ...prev.pronunciations };
        delete next[key];
        return { ...prev, pronunciations: next };
      },
      { label: `Delete pronunciation "${key}"` }
    );
  };

  const renderPronunciationDictionary = () => (
    <div className="rounded-lg border border-zinc-800 bg-[#11141b] p-2.5 space-y-2">
      <button
        type="button"
        onClick={() => setShowPronEditor((v) => !v)}
        className="flex w-full items-center justify-between text-xs font-semibold text-zinc-200"
      >
        <span className="inline-flex items-center gap-1.5">
          <BookOpen className="h-3.5 w-3.5 text-emerald-400" />
          Pronunciations ({Object.keys(project.pronunciations).length})
        </span>
        <span className="text-[11px] text-zinc-400">
          {showPronEditor ? "Hide" : "Edit"}
        </span>
      </button>

      {showPronEditor && (
        <div className="space-y-2 pt-1">
          <div className="flex gap-1.5">
            <input
              type="text"
              value={newPronKey}
              onChange={(e) => setNewPronKey(e.target.value)}
              placeholder="Word"
              className="w-1/2 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100"
            />
            <input
              type="text"
              value={newPronVal}
              onChange={(e) => setNewPronVal(e.target.value)}
              placeholder="Spoken"
              className="w-1/2 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100"
            />
            <button
              type="button"
              onClick={handleAddPronunciation}
              className="rounded bg-zinc-800 px-2 py-1 text-zinc-200 hover:bg-zinc-700"
            >
              <Plus className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="max-h-28 space-y-1 overflow-y-auto">
            {Object.entries(project.pronunciations).map(([k, v]) => (
              <div
                key={k}
                className="flex items-center gap-1.5 rounded bg-zinc-900/80 px-2 py-1 text-xs"
              >
                <span className="w-24 truncate text-zinc-200">{k}</span>
                <span className="text-zinc-500">→</span>
                <input
                  type="text"
                  value={v}
                  onChange={(e) =>
                    onUpdateProject(
                      (prev) => ({
                        ...prev,
                        pronunciations: {
                          ...prev.pronunciations,
                          [k]: e.target.value,
                        },
                      }),
                      {
                        label: `Edit pronunciation "${k}"`,
                        coalesceKey: `pron-${k}`,
                      }
                    )
                  }
                  className="min-w-0 flex-1 rounded border border-zinc-800 bg-zinc-950 px-1.5 py-0.5 text-emerald-200"
                />
                <button
                  type="button"
                  onClick={() => handleDeletePronunciation(k)}
                  className="p-0.5 text-zinc-500 hover:text-rose-400"
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );

  // Determine Header
  let headerTitle = "Inspector";
  let HeaderIcon = SlidersHorizontal;
  if (selection?.type === "video-clip") {
    headerTitle = "Visual Clip / Layer";
    HeaderIcon = Film;
  } else if (selection?.type === "audio-clip") {
    headerTitle = "Audio Clip";
    HeaderIcon = Music;
  } else if (selection?.type === "caption-clip") {
    headerTitle = "Caption & Voiceover";
    HeaderIcon = Captions;
  } else if (selection?.type === "transition") {
    headerTitle = "Junction Transition";
    HeaderIcon = Zap;
  } else if (selection?.type === "track") {
    headerTitle = "Track Settings";
    HeaderIcon = Layers;
  } else if (selection?.type === "keyframe") {
    headerTitle = "Camera Keyframe";
    HeaderIcon = KeyRound;
  }

  // 1. VisualClip Inspector
  const renderVideoClipInspector = (trackId: string, clipId: string) => {
    const track = tracks.find((t): t is VideoTrack => t.kind === "video" && t.id === trackId);
    const clip = track?.clips.find((c) => c.id === clipId);
    if (!track || !clip) return null;

    const asset = project.media_assets?.[clip.asset_id];
    const meta =
      manifest.media_metadata?.[clip.asset_id] ??
      (clip.asset_id === project.source_media.media_id && manifest.source_metadata
        ? { duration: manifest.source_metadata.duration }
        : undefined);
    const maxMediaOut = meta?.duration ?? 600;
    const canSplit =
      !track.locked &&
      projectTime > clip.start_sec + 0.1 &&
      projectTime < clip.start_sec + clip.duration_sec - 0.1;

    const patchClip = (patch: Partial<VisualClip>, label: string, coalesceKey?: string) => {
      onUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "video" || tr.id !== trackId) return tr;
            return {
              ...tr,
              clips: tr.clips.map((c) => (c.id === clipId ? { ...c, ...patch } : c)),
            };
          }),
        { label, coalesceKey }
      );
    };

    return (
      <div className="space-y-3 text-xs">
        <div className="flex items-center justify-between rounded-lg border border-zinc-800 bg-[#11141b] px-3 py-2 font-mono text-[11px]">
          <span className="truncate text-emerald-300" title={asset?.name || clip.asset_id}>
            {clip.id} • {asset?.name || clip.kind}
          </span>
          <span className="shrink-0 text-zinc-400">
            {track.name} ({track.camera_follow ? "CAM" : "OVR"})
          </span>
        </div>

        {/* Timing & Playback */}
        <div className="grid grid-cols-2 gap-2.5 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Timeline Start (s)</span>
            <BufferedNumberInput
              step={0.1}
              min={0}
              value={clip.start_sec}
              onCommit={(val) => patchClip({ start_sec: val }, "Edit clip start", `vc-st-${clip.id}`)}
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Duration (s)</span>
            <BufferedNumberInput
              step={0.1}
              min={0.1}
              value={clip.duration_sec}
              onCommit={(val) =>
                patchClip({ duration_sec: val }, "Edit clip duration", `vc-dur-${clip.id}`)
              }
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
            />
          </label>

          {clip.kind === "video" && (
            <>
              <label className="flex flex-col gap-1">
                <span className="text-zinc-400">Media In (s)</span>
                <BufferedNumberInput
                  step={0.1}
                  min={0}
                  max={clip.media_out ?? maxMediaOut}
                  value={clip.media_in}
                  onCommit={(val) =>
                    patchClip({ media_in: val }, "Edit media in", `vc-mi-${clip.id}`)
                  }
                  className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-zinc-400">Media Out (s)</span>
                <BufferedNumberInput
                  step={0.1}
                  min={clip.media_in + 0.05}
                  max={maxMediaOut}
                  value={clip.media_out ?? maxMediaOut}
                  onCommit={(val) =>
                    patchClip({ media_out: val }, "Edit media out", `vc-mo-${clip.id}`)
                  }
                  className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-zinc-400">Speed ({clip.speed.toFixed(2)}x)</span>
                <BufferedNumberInput
                  step={0.05}
                  min={0.25}
                  max={4.0}
                  value={clip.speed}
                  onCommit={(val) => patchClip({ speed: val }, "Change clip speed", `vc-spd-${clip.id}`)}
                  className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-zinc-400">End Behavior</span>
                <select
                  value={clip.end_behavior}
                  onChange={(e) =>
                    patchClip({ end_behavior: e.target.value as "freeze" | "loop" }, "Change end behavior")
                  }
                  className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100 focus:border-emerald-500 focus:outline-none"
                >
                  <option value="freeze">Freeze Frame</option>
                  <option value="loop">Loop Source</option>
                </select>
              </label>

              <label className="col-span-2 flex flex-col gap-1">
                <div className="flex justify-between text-zinc-400">
                  <span className="inline-flex items-center gap-1">
                    <Volume2 className="h-3.5 w-3.5" />
                    Clip Audio Volume
                  </span>
                  <span className="font-mono text-zinc-200">{Math.round(clip.volume * 100)}%</span>
                </div>
                <input
                  type="range"
                  aria-label="Clip Volume"
                  min={0}
                  max={2}
                  step={0.05}
                  value={clip.volume}
                  onChange={(e) =>
                    patchClip(
                      { volume: parseFloat(e.target.value) },
                      "Adjust clip audio volume",
                      `vc-vol-${clip.id}`
                    )
                  }
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-zinc-400">Audio Fade In (s)</span>
                <BufferedNumberInput
                  step={0.05}
                  min={0}
                  max={clip.duration_sec}
                  value={clip.audio_fade_in_sec}
                  onCommit={(val) =>
                    patchClip({ audio_fade_in_sec: val }, "Edit audio fade in", `vc-fi-${clip.id}`)
                  }
                  className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-zinc-400">Audio Fade Out (s)</span>
                <BufferedNumberInput
                  step={0.05}
                  min={0}
                  max={clip.duration_sec}
                  value={clip.audio_fade_out_sec}
                  onCommit={(val) =>
                    patchClip({ audio_fade_out_sec: val }, "Edit audio fade out", `vc-fo-${clip.id}`)
                  }
                  className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
                />
              </label>
            </>
          )}
        </div>

        {/* Spatial Layer Transform */}
        <div className="space-y-2 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-zinc-200">Layer Transform</span>
            <button
              type="button"
              onClick={() =>
                patchClip(
                  { transform: { x: 0.5, y: 0.5, scale: 1.0, rotation_deg: 0, opacity: 1.0 } },
                  "Reset layer transform"
                )
              }
              className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-300 hover:bg-zinc-700"
            >
              <RotateCcw className="h-3 w-3" />
              Reset
            </button>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-zinc-400">Pos X</span>
              <BufferedNumberInput
                step={0.02}
                min={-0.5}
                max={1.5}
                value={clip.transform.x}
                onCommit={(x) =>
                  patchClip({ transform: { ...clip.transform, x } }, "Move layer X", `vc-tx-${clip.id}`)
                }
                className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1 font-mono text-zinc-100"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-zinc-400">Pos Y</span>
              <BufferedNumberInput
                step={0.02}
                min={-0.5}
                max={1.5}
                value={clip.transform.y}
                onCommit={(y) =>
                  patchClip({ transform: { ...clip.transform, y } }, "Move layer Y", `vc-ty-${clip.id}`)
                }
                className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1 font-mono text-zinc-100"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-zinc-400">Scale</span>
              <BufferedNumberInput
                step={0.05}
                min={0.05}
                max={4.0}
                value={clip.transform.scale}
                onCommit={(scale) =>
                  patchClip(
                    { transform: { ...clip.transform, scale } },
                    "Scale layer",
                    `vc-ts-${clip.id}`
                  )
                }
                className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1 font-mono text-zinc-100"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-zinc-400">Rotation °</span>
              <BufferedNumberInput
                step={1}
                min={-360}
                max={360}
                precision={1}
                value={clip.transform.rotation_deg}
                onCommit={(rotation_deg) =>
                  patchClip(
                    { transform: { ...clip.transform, rotation_deg } },
                    "Rotate layer",
                    `vc-tr-${clip.id}`
                  )
                }
                className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1 font-mono text-zinc-100"
              />
            </label>
            <label className="col-span-2 flex flex-col gap-1">
              <div className="flex justify-between text-[11px] text-zinc-400">
                <span>Opacity</span>
                <span className="font-mono text-zinc-200">{Math.round(clip.transform.opacity * 100)}%</span>
              </div>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={clip.transform.opacity}
                onChange={(e) =>
                  patchClip(
                    { transform: { ...clip.transform, opacity: parseFloat(e.target.value) } },
                    "Adjust layer opacity",
                    `vc-op-${clip.id}`
                  )
                }
              />
            </label>
          </div>
        </div>

        {/* Clip Edge Transitions */}
        <div className="space-y-2 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
          <span className="block font-semibold text-zinc-200">Edge Transitions (In / Out)</span>
          <div className="grid grid-cols-2 gap-2">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-zinc-400">In Type</span>
              <select
                aria-label="Transition In Type"
                value={clip.transition_in.type}
                onChange={(e) => {
                  const type = e.target.value as EdgeTransitionType;
                  patchClip(
                    {
                      transition_in: {
                        type,
                        duration_sec:
                          type === "none" ? 0 : Math.max(0.25, clip.transition_in.duration_sec || 0.4),
                      },
                    },
                    "Set transition in"
                  );
                }}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
              >
                {EDGE_TRANSITIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-zinc-400">In Dur (s)</span>
              <BufferedNumberInput
                step={0.05}
                min={0}
                max={clip.duration_sec}
                value={clip.transition_in.duration_sec}
                onCommit={(duration_sec) =>
                  patchClip(
                    { transition_in: { ...clip.transition_in, duration_sec } },
                    "Edit transition in duration",
                    `vc-tid-${clip.id}`
                  )
                }
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-zinc-400">Out Type</span>
              <select
                aria-label="Transition Out Type"
                value={clip.transition_out.type}
                onChange={(e) => {
                  const type = e.target.value as EdgeTransitionType;
                  patchClip(
                    {
                      transition_out: {
                        type,
                        duration_sec:
                          type === "none" ? 0 : Math.max(0.25, clip.transition_out.duration_sec || 0.4),
                      },
                    },
                    "Set transition out"
                  );
                }}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
              >
                {EDGE_TRANSITIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-zinc-400">Out Dur (s)</span>
              <BufferedNumberInput
                step={0.05}
                min={0}
                max={clip.duration_sec}
                value={clip.transition_out.duration_sec}
                onCommit={(duration_sec) =>
                  patchClip(
                    { transition_out: { ...clip.transition_out, duration_sec } },
                    "Edit transition out duration",
                    `vc-tod-${clip.id}`
                  )
                }
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
              />
            </label>
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            disabled={!canSplit}
            onClick={() => {
              const newId = findNextClipId(project, "vclip");
              const split = splitVisualClipAt(clip, projectTime, newId);
              if (!split) return;
              const [first, second] = split;
              onUpdateTracks(
                (prevTracks) =>
                  prevTracks.map((tr) => {
                    if (tr.kind !== "video" || tr.id !== trackId) return tr;
                    return {
                      ...tr,
                      clips: tr.clips
                        .flatMap((c) => (c.id === first.id ? [first, second] : [c]))
                        .sort((a, b) => a.start_sec - b.start_sec),
                      transitions: tr.transitions.map((jt) =>
                        jt.from_clip_id === first.id ? { ...jt, from_clip_id: second.id } : jt
                      ),
                    };
                  }),
                { label: `Split clip ${clip.id}` },
                { type: "video-clip", trackId, clipId: second.id }
              );
            }}
            className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
          >
            <Scissors className="h-3.5 w-3.5" />
            Split
          </button>

          <button
            type="button"
            onClick={() => {
              const newId = findNextClipId(project, "vclip");
              const clone: VisualClip = {
                ...clip,
                id: newId,
                start_sec: Number((clip.start_sec + clip.duration_sec).toFixed(3)),
                transform: { ...clip.transform },
                transition_in: { ...clip.transition_in },
                transition_out: { ...clip.transition_out },
              };
              onUpdateTracks(
                (prevTracks) =>
                  prevTracks.map((tr) =>
                    tr.kind === "video" && tr.id === trackId
                      ? { ...tr, clips: [...tr.clips, clone].sort((a, b) => a.start_sec - b.start_sec) }
                      : tr
                  ),
                { label: `Duplicate clip ${clip.id}` },
                { type: "video-clip", trackId, clipId: newId }
              );
            }}
            className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-zinc-200 hover:bg-zinc-700"
          >
            <Copy className="h-3.5 w-3.5" />
            Duplicate
          </button>

          <button
            type="button"
            onClick={() =>
              onUpdateTracks(
                (prevTracks) =>
                  prevTracks.map((tr) =>
                    tr.kind === "video" && tr.id === trackId
                      ? {
                          ...tr,
                          clips: tr.clips.filter((c) => c.id !== clipId),
                          transitions: tr.transitions.filter(
                            (jt) => jt.from_clip_id !== clipId && jt.to_clip_id !== clipId
                          ),
                        }
                      : tr
                  ),
                { label: `Delete clip ${clipId}` },
                null
              )
            }
            className="inline-flex items-center justify-center gap-1 rounded border border-rose-500/40 bg-rose-500/15 px-2.5 py-1.5 text-rose-200 hover:bg-rose-500/25"
          >
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </button>
        </div>
      </div>
    );
  };

  // 2. AudioClip Inspector
  const renderAudioClipInspector = (trackId: string, clipId: string) => {
    const track = tracks.find((t): t is AudioTrack => t.kind === "audio" && t.id === trackId);
    const clip = track?.clips.find((c) => c.id === clipId);
    if (!track || !clip) return null;

    const asset = project.media_assets?.[clip.asset_id];
    const maxAudioDur = manifest.media_metadata?.[clip.asset_id]?.duration ?? 600;
    const canSplit =
      !track.locked &&
      projectTime > clip.start_sec + 0.1 &&
      projectTime < clip.start_sec + clip.duration_sec - 0.1;

    const patchClip = (patch: Partial<AudioClip>, label: string, coalesceKey?: string) => {
      onUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "audio" || tr.id !== trackId) return tr;
            return {
              ...tr,
              clips: tr.clips.map((c) => (c.id === clipId ? { ...c, ...patch } : c)),
            };
          }),
        { label, coalesceKey }
      );
    };

    return (
      <div className="space-y-3 text-xs">
        <div className="flex items-center justify-between rounded-lg border border-zinc-800 bg-[#11141b] px-3 py-2 font-mono text-[11px]">
          <span className="truncate text-amber-300">{clip.id} • {asset?.name || "Audio"}</span>
          <span className="shrink-0 text-zinc-400">{track.name}</span>
        </div>

        <div className="grid grid-cols-2 gap-2.5 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Start (s)</span>
            <BufferedNumberInput
              step={0.1}
              min={0}
              value={clip.start_sec}
              onCommit={(val) => patchClip({ start_sec: val }, "Edit audio start", `ac-st-${clip.id}`)}
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Duration (s)</span>
            <BufferedNumberInput
              step={0.1}
              min={0.1}
              value={clip.duration_sec}
              onCommit={(val) =>
                patchClip({ duration_sec: val }, "Edit audio duration", `ac-dur-${clip.id}`)
              }
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Media In (s)</span>
            <BufferedNumberInput
              step={0.1}
              min={0}
              max={maxAudioDur}
              value={clip.media_in}
              onCommit={(val) =>
                patchClip({ media_in: val }, "Edit audio media in", `ac-mi-${clip.id}`)
              }
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Speed</span>
            <BufferedNumberInput
              step={0.05}
              min={0.25}
              max={4.0}
              value={clip.speed}
              onCommit={(val) => patchClip({ speed: val }, "Edit audio speed", `ac-spd-${clip.id}`)}
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>

          <label className="col-span-2 flex flex-col gap-1">
            <div className="flex justify-between text-zinc-400">
              <span>Clip Volume</span>
              <span className="font-mono text-zinc-200">{Math.round(clip.volume * 100)}%</span>
            </div>
            <input
              type="range"
              min={0}
              max={2}
              step={0.05}
              value={clip.volume}
              onChange={(e) =>
                patchClip({ volume: parseFloat(e.target.value) }, "Edit audio volume", `ac-vol-${clip.id}`)
              }
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Fade In (s)</span>
            <BufferedNumberInput
              step={0.05}
              min={0}
              max={clip.duration_sec}
              value={clip.fade_in_sec}
              onCommit={(val) => patchClip({ fade_in_sec: val }, "Edit audio fade in", `ac-fi-${clip.id}`)}
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Fade Out (s)</span>
            <BufferedNumberInput
              step={0.05}
              min={0}
              max={clip.duration_sec}
              value={clip.fade_out_sec}
              onCommit={(val) =>
                patchClip({ fade_out_sec: val }, "Edit audio fade out", `ac-fo-${clip.id}`)
              }
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>

          <label className="col-span-2 flex items-center gap-2 text-zinc-300">
            <input
              type="checkbox"
              checked={clip.loop}
              onChange={(e) => patchClip({ loop: e.target.checked }, "Toggle audio loop")}
            />
            <span>Loop audio across clip duration</span>
          </label>
        </div>

        <div className="flex items-center gap-1.5">
          <button
            type="button"
            disabled={!canSplit}
            onClick={() => {
              const newId = findNextClipId(project, "aclip");
              const split = splitAudioClipAt(clip, projectTime, newId);
              if (!split) return;
              const [first, second] = split;
              onUpdateTracks(
                (prevTracks) =>
                  prevTracks.map((tr) =>
                    tr.kind === "audio" && tr.id === trackId
                      ? {
                          ...tr,
                          clips: tr.clips
                            .flatMap((c) => (c.id === first.id ? [first, second] : [c]))
                            .sort((a, b) => a.start_sec - b.start_sec),
                        }
                      : tr
                  ),
                { label: `Split audio ${clipId}` },
                { type: "audio-clip", trackId, clipId: second.id }
              );
            }}
            className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
          >
            <Scissors className="h-3.5 w-3.5" />
            Split
          </button>

          <button
            type="button"
            onClick={() => {
              const newId = findNextClipId(project, "aclip");
              const clone: AudioClip = {
                ...clip,
                id: newId,
                start_sec: Number((clip.start_sec + clip.duration_sec).toFixed(3)),
              };
              onUpdateTracks(
                (prevTracks) =>
                  prevTracks.map((tr) =>
                    tr.kind === "audio" && tr.id === trackId
                      ? { ...tr, clips: [...tr.clips, clone].sort((a, b) => a.start_sec - b.start_sec) }
                      : tr
                  ),
                { label: `Duplicate audio ${clipId}` },
                { type: "audio-clip", trackId, clipId: newId }
              );
            }}
            className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-zinc-200 hover:bg-zinc-700"
          >
            <Copy className="h-3.5 w-3.5" />
            Duplicate
          </button>

          <button
            type="button"
            onClick={() =>
              onUpdateTracks(
                (prevTracks) =>
                  prevTracks.map((tr) =>
                    tr.kind === "audio" && tr.id === trackId
                      ? { ...tr, clips: tr.clips.filter((c) => c.id !== clipId) }
                      : tr
                  ),
                { label: `Delete audio ${clipId}` },
                null
              )
            }
            className="inline-flex items-center justify-center gap-1 rounded border border-rose-500/40 bg-rose-500/15 px-2.5 py-1.5 text-rose-200 hover:bg-rose-500/25"
          >
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </button>
        </div>
      </div>
    );
  };

  // 3. CaptionClip Inspector (completely decoupled voiceover & on-screen caption)
  const renderCaptionClipInspector = (trackId: string, clipId: string) => {
    const track = tracks.find((t): t is CaptionTrack => t.kind === "caption" && t.id === trackId);
    const clip = track?.clips.find((c) => c.id === clipId);
    if (!track || !clip) return null;

    const effDur = getCaptionClipEffectiveDuration(clip, manifest);
    const capArt = manifest.caption_artifacts?.[clip.id];
    const resolvedSpoken = resolveCaptionClipSpokenText(clip, project.pronunciations);
    const currentSig = computeCaptionClipTtsSignature(
      clip,
      project.pronunciations,
      project.voice,
      project.language
    );
    const lastSig = lastSynthesizedCaptionSignatures[clip.id];
    const hasAudioArt = Boolean(capArt?.audio_artifact_id);
    const hasSpokenText = resolvedSpoken.trim().length > 0;
    const isStale =
      clip.tts_enabled &&
      hasSpokenText &&
      (!hasAudioArt || (lastSig !== undefined && lastSig !== currentSig));

    const patchClip = (patch: Partial<CaptionClip>, label: string, coalesceKey?: string) => {
      onUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "caption" || tr.id !== trackId) return tr;
            return {
              ...tr,
              clips: tr.clips.map((c) => (c.id === clipId ? { ...c, ...patch } : c)),
            };
          }),
        { label, coalesceKey }
      );
    };

    const handleSplitText = () => {
      const parts = autoSplitTextToClauses(clip.display_text);
      if (parts.length <= 1) {
        const words = clip.display_text.trim().split(/\s+/);
        if (words.length < 2) return;
        const mid = Math.ceil(words.length / 2);
        parts.splice(0, 1, words.slice(0, mid).join(" "), words.slice(mid).join(" "));
      }
      const partDur = Number((effDur / parts.length).toFixed(3));
      const newClips: CaptionClip[] = parts.map((txt, idx) => {
        if (idx === 0) {
          return {
            ...clip,
            display_text: txt,
            spoken_text_override: null,
            duration_sec: clip.auto_fit_tts ? null : partDur,
          };
        }
        return {
          ...clip,
          id: `${clip.id}_p${idx + 1}_${Math.random().toString(36).slice(2, 5)}`,
          start_sec: Number((clip.start_sec + idx * partDur).toFixed(3)),
          duration_sec: clip.auto_fit_tts ? null : partDur,
          display_text: txt,
          spoken_text_override: null,
        };
      });
      onUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "caption" || tr.id !== trackId) return tr;
            return {
              ...tr,
              clips: tr.clips
                .flatMap((c) => (c.id === clipId ? newClips : [c]))
                .sort((a, b) => a.start_sec - b.start_sec),
            };
          }),
        { label: `Split caption ${clip.id}` },
        { type: "caption-clip", trackId, clipId: newClips[0].id }
      );
    };

    return (
      <div className="space-y-3 text-xs">
        <div className="space-y-2.5 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
          <div className="flex items-center justify-between">
            <span className="font-mono text-sky-300">
              {clip.id} • {track.name}
            </span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={handleSplitText}
                className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] text-zinc-300 hover:bg-zinc-800"
              >
                <Scissors className="h-3 w-3" />
                Split
              </button>
              <button
                type="button"
                onClick={() =>
                  onUpdateTracks(
                    (prevTracks) =>
                      prevTracks.map((tr) =>
                        tr.kind === "caption" && tr.id === trackId
                          ? { ...tr, clips: tr.clips.filter((c) => c.id !== clipId) }
                          : tr
                      ),
                    { label: `Delete caption ${clipId}` },
                    null
                  )
                }
                className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] text-rose-300 hover:bg-rose-500/20"
              >
                <Trash2 className="h-3 w-3" />
                Delete
              </button>
            </div>
          </div>

          {/* Decoupled Mode: Speak TTS vs Silent On-Screen Text */}
          <label className="flex items-center justify-between rounded border border-zinc-800 bg-zinc-900/70 px-2.5 py-1.5 text-zinc-200">
            <span className="font-medium">Speak Voiceover (Kokoro TTS)</span>
            <input
              type="checkbox"
              aria-label="Speak Voiceover"
              checked={clip.tts_enabled}
              onChange={(e) =>
                patchClip({ tts_enabled: e.target.checked }, "Toggle caption TTS voiceover")
              }
            />
          </label>

          <label className="block space-y-1">
            <span className="text-zinc-400">Caption Text</span>
            <textarea
              rows={2}
              value={clip.display_text}
              onChange={(e) =>
                patchClip(
                  { display_text: e.target.value },
                  "Edit caption text",
                  `cc-txt-${clip.id}`
                )
              }
              placeholder="On-screen caption..."
              className="w-full rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-zinc-100 focus:border-emerald-500 focus:outline-none"
            />
          </label>

          {clip.tts_enabled && (
            <>
              <label className="block space-y-1">
                <span className="text-zinc-400">Alternate Narration (optional)</span>
                <input
                  type="text"
                  value={clip.spoken_text_override ?? ""}
                  onChange={(e) =>
                    patchClip(
                      {
                        spoken_text_override:
                          e.target.value.length > 0 ? e.target.value : null,
                      },
                      "Edit alternate narration",
                      `cc-spk-${clip.id}`
                    )
                  }
                  placeholder="Leave blank to speak caption text..."
                  className="w-full rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-zinc-100 focus:border-emerald-500 focus:outline-none"
                />
              </label>

              <div className="rounded bg-emerald-950/30 px-2 py-1 text-[11px] text-emerald-200">
                <span className="text-emerald-400">Spoken: </span>
                <span>{resolvedSpoken || "—"}</span>
              </div>
            </>
          )}

          <div className="grid grid-cols-2 gap-2.5">
            <label className="flex flex-col gap-1">
              <span className="text-zinc-400">Start Time (s)</span>
              <BufferedNumberInput
                step={0.1}
                min={0}
                value={clip.start_sec}
                onCommit={(val) =>
                  patchClip({ start_sec: val }, "Move caption start", `cc-st-${clip.id}`)
                }
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-zinc-400">Duration (s)</span>
              <BufferedNumberInput
                step={0.1}
                min={0.2}
                value={effDur}
                onCommit={(val) =>
                  patchClip(
                    { duration_sec: val, auto_fit_tts: false },
                    "Edit caption duration",
                    `cc-dur-${clip.id}`
                  )
                }
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
              />
            </label>
          </div>

          {clip.tts_enabled && (
            <>
              <label className="flex items-center gap-2 text-[11px] text-zinc-300">
                <input
                  type="checkbox"
                  checked={clip.auto_fit_tts}
                  onChange={(e) =>
                    patchClip({ auto_fit_tts: e.target.checked }, "Toggle auto-fit TTS duration")
                  }
                />
                <span>Auto-fit block duration to TTS audio</span>
              </label>

              <div className="grid grid-cols-2 gap-2">
                <label className="flex flex-col gap-1">
                  <span className="text-zinc-400">Speed ({clip.speed.toFixed(2)}x)</span>
                  <input
                    type="range"
                    min={0.75}
                    max={1.5}
                    step={0.05}
                    value={clip.speed}
                    onChange={(e) =>
                      patchClip(
                        { speed: parseFloat(e.target.value) },
                        "Adjust narration speed",
                        `cc-spd-${clip.id}`
                      )
                    }
                  />
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-zinc-400">Voice Override</span>
                  <select
                    aria-label="Caption Voice Override"
                    value={clip.voice_override ?? ""}
                    onChange={(e) =>
                      patchClip(
                        { voice_override: e.target.value || null },
                        "Change caption voice override"
                      )
                    }
                    className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-[11px] text-zinc-100"
                  >
                    <option value="">Project ({project.voice})</option>
                    {voiceOptions.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </>
          )}

          {/* Style Override (Alignment / Font / Vertical Margin) */}
          <div className="border-t border-zinc-800 pt-2">
            <span className="mb-1.5 block text-[11px] font-medium text-zinc-400">
              Style Override (Optional)
            </span>
            <div className="grid grid-cols-3 gap-1.5">
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-zinc-500">Align</span>
                <select
                  value={clip.style_override?.align ?? ""}
                  onChange={(e) =>
                    patchClip(
                      {
                        style_override: {
                          ...clip.style_override,
                          align: (e.target.value || null) as CaptionAlign | null,
                        },
                      },
                      "Change caption align"
                    )
                  }
                  className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1 text-[11px] text-zinc-100"
                >
                  <option value="">Default</option>
                  <option value="bottom">Bottom</option>
                  <option value="center">Center</option>
                  <option value="top">Top</option>
                </select>
              </label>

              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-zinc-500">Size (px)</span>
                <input
                  type="number"
                  min={16}
                  max={220}
                  placeholder={String(project.caption_settings.font_size)}
                  value={clip.style_override?.font_size ?? ""}
                  onChange={(e) => {
                    const parsed = parseInt(e.target.value, 10);
                    patchClip(
                      {
                        style_override: {
                          ...clip.style_override,
                          font_size: Number.isFinite(parsed) && parsed > 0 ? parsed : null,
                        },
                      },
                      "Edit caption font size",
                      `cc-fs-${clip.id}`
                    );
                  }}
                  className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1 font-mono text-[11px] text-zinc-100"
                />
              </label>

              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-zinc-500">Margin V</span>
                <input
                  type="number"
                  min={0}
                  max={1800}
                  placeholder={String(project.caption_settings.margin_v)}
                  value={clip.style_override?.margin_v ?? ""}
                  onChange={(e) => {
                    const parsed = parseInt(e.target.value, 10);
                    patchClip(
                      {
                        style_override: {
                          ...clip.style_override,
                          margin_v: Number.isFinite(parsed) && parsed >= 0 ? parsed : null,
                        },
                      },
                      "Edit caption margin V",
                      `cc-mv-${clip.id}`
                    );
                  }}
                  className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1 font-mono text-[11px] text-zinc-100"
                />
              </label>
            </div>
          </div>
        </div>

        {/* Voiceover Sync Status & Explicit Synthesize Button */}
        {clip.tts_enabled && (
          <div className="space-y-2 rounded-lg border border-zinc-800 bg-[#11141b] p-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-[11px] text-zinc-400">Kokoro Voiceover</span>
              {ttsError ? (
                <span className="inline-flex items-center gap-1 rounded border border-rose-500/50 bg-rose-500/20 px-2 py-0.5 text-[11px] text-rose-200">
                  <AlertCircle className="h-3 w-3" />
                  Error
                </span>
              ) : isStale ? (
                <span className="inline-flex items-center gap-1 rounded border border-amber-500/50 bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-200">
                  <AlertTriangle className="h-3 w-3" />
                  Out of Sync
                </span>
              ) : hasAudioArt ? (
                <span className="inline-flex items-center gap-1 rounded border border-emerald-500/40 bg-emerald-500/15 px-2 py-0.5 text-[11px] text-emerald-200">
                  <CheckCircle2 className="h-3 w-3" />
                  Synced ({capArt?.audio_duration.toFixed(2)}s)
                </span>
              ) : (
                <span className="text-[11px] text-zinc-500">Not synthesized</span>
              )}
            </div>

            {ttsError && <p className="text-[11px] text-rose-300">{ttsError}</p>}

            <button
              type="button"
              disabled={!hasSpokenText || synthesizingClipId === clip.id}
              onClick={() => void onSynthesizeCaptionTts(clip.id)}
              className="inline-flex w-full items-center justify-center gap-1.5 rounded bg-sky-600 px-2.5 py-1.5 text-[11px] font-medium text-white hover:bg-sky-500 disabled:opacity-45"
            >
              {synthesizingClipId === clip.id ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Sparkles className="h-3.5 w-3.5" />
              )}
              {synthesizingClipId === clip.id ? "Synthesizing..." : "Synthesize TTS"}
            </button>

            {capArt?.audio_artifact_id && (
              <audio
                key={`${capArt.audio_artifact_id}-${capArt.tts_hash ?? ""}`}
                controls
                src={`${apiClient.mediaUrl(project.project_id, capArt.audio_artifact_id)}?v=${encodeURIComponent(capArt.tts_hash ?? "")}`}
                className="h-8 w-full"
              />
            )}
          </div>
        )}

        {renderPronunciationDictionary()}
      </div>
    );
  };

  // 4. Junction Transition Inspector
  const renderTransitionInspector = (trackId: string, transitionId: string) => {
    const track = tracks.find((t): t is VideoTrack => t.kind === "video" && t.id === trackId);
    const tr = track?.transitions.find((jt) => jt.id === transitionId);
    if (!track || !tr) return null;

    return (
      <div className="space-y-3 text-xs">
        <div className="space-y-3 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
          <div className="flex items-center justify-between">
            <span className="font-mono text-emerald-300">
              {tr.from_clip_id} ⇄ {tr.to_clip_id}
            </span>
            <button
              type="button"
              onClick={() =>
                onUpdateTracks(
                  (prev) =>
                    prev.map((t) =>
                      t.kind === "video" && t.id === trackId
                        ? { ...t, transitions: t.transitions.filter((x) => x.id !== transitionId) }
                        : t
                    ),
                  { label: "Remove junction transition" },
                  null
                )
              }
              className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-rose-300 hover:bg-rose-500/20"
            >
              <Trash2 className="h-3 w-3" />
              Remove
            </button>
          </div>

          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Transition Effect</span>
            <select
              aria-label="Junction Transition Effect"
              value={tr.type}
              onChange={(e) => {
                const nextType = e.target.value as JunctionTransitionType;
                onUpdateTracks(
                  (prev) =>
                    prev.map((t) =>
                      t.kind === "video" && t.id === trackId
                        ? {
                            ...t,
                            transitions: t.transitions.map((x) =>
                              x.id === transitionId ? { ...x, type: nextType } : x
                            ),
                          }
                        : t
                    ),
                  { label: `Change transition (${nextType})` }
                );
              }}
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
            >
              {JUNCTION_TRANSITIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1">
            <div className="flex justify-between text-zinc-400">
              <span>Transition Duration (s)</span>
              <span className="font-mono text-zinc-200">{tr.duration_sec.toFixed(2)}s</span>
            </div>
            <BufferedNumberInput
              step={0.05}
              min={0.1}
              max={3.0}
              value={tr.duration_sec}
              onCommit={(duration_sec) =>
                onUpdateTracks(
                  (prev) =>
                    prev.map((t) =>
                      t.kind === "video" && t.id === trackId
                        ? {
                            ...t,
                            transitions: t.transitions.map((x) =>
                              x.id === transitionId ? { ...x, duration_sec } : x
                            ),
                          }
                        : t
                    ),
                  { label: "Adjust transition duration", coalesceKey: `tr-dur-${tr.id}` }
                )
              }
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>
        </div>
      </div>
    );
  };

  // 5. Track Inspector
  const renderTrackInspector = (trackId: string) => {
    const track = tracks.find((t) => t.id === trackId);
    if (!track) return null;

    const patchTrack = (updater: (t: TimelineTrack) => TimelineTrack, label: string, coalesceKey?: string) => {
      onUpdateTracks(
        (prev) => prev.map((t) => (t.id === trackId ? updater(t) : t)),
        { label, coalesceKey }
      );
    };

    return (
      <div className="space-y-3 text-xs">
        <div className="space-y-3 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
          <div className="flex items-center justify-between">
            <span className="font-mono uppercase text-zinc-300">
              {track.kind} Track ({track.id})
            </span>
            <span className="text-[11px] text-zinc-500">{track.clips.length} clip(s)</span>
          </div>

          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Track Name</span>
            <input
              type="text"
              value={track.name}
              onChange={(e) =>
                patchTrack(
                  (t) => ({ ...t, name: e.target.value }),
                  "Rename track",
                  `trk-name-${track.id}`
                )
              }
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
            />
          </label>

          <label className="flex items-center justify-between text-zinc-300">
            <span>Lock Track</span>
            <input
              type="checkbox"
              checked={track.locked}
              onChange={(e) =>
                patchTrack((t) => ({ ...t, locked: e.target.checked }), "Toggle track lock")
              }
            />
          </label>

          {track.kind === "video" && (
            <>
              <label className="flex items-center justify-between text-zinc-300">
                <span>Visible Layer</span>
                <input
                  type="checkbox"
                  checked={track.visible}
                  onChange={(e) =>
                    patchTrack(
                      (t) => (t.kind === "video" ? { ...t, visible: e.target.checked } : t),
                      "Toggle track visibility"
                    )
                  }
                />
              </label>
              <label className="flex items-center justify-between text-zinc-300">
                <span>Mute Track Audio</span>
                <input
                  type="checkbox"
                  checked={track.muted}
                  onChange={(e) =>
                    patchTrack(
                      (t) => (t.kind === "video" ? { ...t, muted: e.target.checked } : t),
                      "Toggle track mute"
                    )
                  }
                />
              </label>
              <label className="flex items-center justify-between text-zinc-300">
                <span>Follow Global Camera Crop</span>
                <input
                  type="checkbox"
                  checked={track.camera_follow}
                  onChange={(e) =>
                    patchTrack(
                      (t) =>
                        t.kind === "video" ? { ...t, camera_follow: e.target.checked } : t,
                      "Toggle camera follow"
                    )
                  }
                />
              </label>
            </>
          )}

          {track.kind === "caption" && (
            <>
              <label className="flex items-center justify-between text-zinc-300">
                <span>Visible Subtitles</span>
                <input
                  type="checkbox"
                  checked={track.visible}
                  onChange={(e) =>
                    patchTrack(
                      (t) => (t.kind === "caption" ? { ...t, visible: e.target.checked } : t),
                      "Toggle caption visibility"
                    )
                  }
                />
              </label>
              <label className="flex items-center justify-between text-zinc-300">
                <span>Mute Voiceover Track</span>
                <input
                  type="checkbox"
                  checked={track.muted}
                  onChange={(e) =>
                    patchTrack(
                      (t) => (t.kind === "caption" ? { ...t, muted: e.target.checked } : t),
                      "Toggle caption audio mute"
                    )
                  }
                />
              </label>
            </>
          )}

          {track.kind === "audio" && (
            <>
              <label className="flex flex-col gap-1">
                <div className="flex justify-between text-zinc-400">
                  <span>Track Volume</span>
                  <span className="font-mono text-zinc-200">{Math.round(track.volume * 100)}%</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={2}
                  step={0.05}
                  value={track.volume}
                  onChange={(e) =>
                    patchTrack(
                      (t) =>
                        t.kind === "audio" ? { ...t, volume: parseFloat(e.target.value) } : t,
                      "Adjust audio track volume",
                      `atr-vol-${track.id}`
                    )
                  }
                />
              </label>
              <label className="flex items-center justify-between text-zinc-300">
                <span>Mute</span>
                <input
                  type="checkbox"
                  checked={track.muted}
                  onChange={(e) =>
                    patchTrack(
                      (t) => (t.kind === "audio" ? { ...t, muted: e.target.checked } : t),
                      "Toggle audio mute"
                    )
                  }
                />
              </label>
              <label className="flex items-center justify-between text-zinc-300">
                <span>Solo</span>
                <input
                  type="checkbox"
                  checked={track.solo}
                  onChange={(e) =>
                    patchTrack(
                      (t) => (t.kind === "audio" ? { ...t, solo: e.target.checked } : t),
                      "Toggle audio solo"
                    )
                  }
                />
              </label>
              <label className="flex items-center justify-between text-zinc-300">
                <span>Duck Under Narration</span>
                <input
                  type="checkbox"
                  checked={track.duck_under_narration}
                  onChange={(e) =>
                    patchTrack(
                      (t) =>
                        t.kind === "audio"
                          ? { ...t, duck_under_narration: e.target.checked }
                          : t,
                      "Toggle audio ducking"
                    )
                  }
                />
              </label>
            </>
          )}
        </div>
      </div>
    );
  };

  // 6. Camera Keyframe Inspector (independent of video clips)
  const renderKeyframeInspector = (kfIndex: number) => {
    const cameraKeyframes = project.camera_keyframes ?? [];
    const kf = cameraKeyframes[kfIndex];
    if (!kf) return null;

    const updateKf = (patch: Partial<typeof kf>, coalesceKey?: string) => {
      const merged = { ...kf, ...patch };
      const clamped = clampCropPosition(
        merged.x,
        merged.y,
        merged.zoom,
        srcW,
        srcH,
        outW,
        outH
      );
      const updatedKf = {
        ...merged,
        t:
          kfIndex === 0 && cameraKeyframes.length === 1
            ? 0
            : Number(Math.max(0, merged.t).toFixed(2)),
        x: Number(clamped.x.toFixed(4)),
        y: Number(clamped.y.toFixed(4)),
        zoom: Number(clamped.zoom.toFixed(3)),
      };
      const next = cameraKeyframes.map((item, idx) =>
        idx === kfIndex ? updatedKf : item
      );
      next.sort((a, b) => a.t - b.t);
      const newIndex = next.indexOf(updatedKf);
      onUpdateCameraKeyframes(next, { label: "Edit camera keyframe", coalesceKey });
      if (newIndex >= 0 && newIndex !== kfIndex) {
        onSelectItem({ type: "keyframe", index: newIndex });
      }
    };

    const transition: KeyframeTransition = kf.transition ?? "tween";

    return (
      <div className="space-y-3 text-xs">
        <div className="space-y-3 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
          <div className="flex items-center justify-between">
            <span className="font-mono text-amber-300">
              Keyframe #{kfIndex + 1} @ {formatTimecode(kf.t)}
            </span>
            {(kfIndex > 0 || cameraKeyframes.length > 1) && (
              <button
                type="button"
                onClick={() => {
                  onUpdateCameraKeyframes(
                    cameraKeyframes.filter((_, i) => i !== kfIndex),
                    { label: "Delete camera keyframe" }
                  );
                  onSelectItem(null);
                }}
                className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-rose-300 hover:bg-rose-500/20"
              >
                <Trash2 className="h-3 w-3" />
                Delete
              </button>
            )}
          </div>

          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Project Time (s)</span>
            <BufferedNumberInput
              step={0.05}
              min={0}
              value={kf.t}
              onCommit={(v) => updateKf({ t: v }, `kf-t-${kfIndex}`)}
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>

          <div className="grid grid-cols-2 gap-2.5">
            <label className="flex flex-col gap-1">
              <span className="text-zinc-400">X Position</span>
              <input
                type="number"
                step={0.01}
                min={0}
                max={1}
                value={Number(kf.x.toFixed(3))}
                onChange={(e) => {
                  const v = parseFloat(e.target.value);
                  if (Number.isFinite(v)) updateKf({ x: v }, `kf-x-${kfIndex}`);
                }}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-zinc-400">Y Position</span>
              <input
                type="number"
                step={0.01}
                min={0}
                max={1}
                value={Number(kf.y.toFixed(3))}
                onChange={(e) => {
                  const v = parseFloat(e.target.value);
                  if (Number.isFinite(v)) updateKf({ y: v }, `kf-y-${kfIndex}`);
                }}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
              />
            </label>
          </div>

          <label className="flex flex-col gap-1">
            <div className="flex justify-between text-zinc-400">
              <span>Size / Zoom</span>
              <span className="font-mono text-zinc-200">{kf.zoom.toFixed(2)}x</span>
            </div>
            <input
              type="range"
              min={1}
              max={3}
              step={0.05}
              value={kf.zoom}
              onChange={(e) => updateKf({ zoom: parseFloat(e.target.value) }, `kf-z-${kfIndex}`)}
            />
          </label>

          <div className="space-y-1.5">
            <span className="block text-zinc-400">Transition to Next View</span>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => updateKf({ transition: "tween" })}
                className={`rounded border px-2.5 py-1.5 font-medium transition ${
                  transition === "tween"
                    ? "border-emerald-500 bg-emerald-500/20 text-emerald-200"
                    : "border-zinc-700 bg-zinc-900 text-zinc-400 hover:text-zinc-200"
                }`}
              >
                Tween (Smooth)
              </button>
              <button
                type="button"
                onClick={() => updateKf({ transition: "snap" })}
                className={`rounded border px-2.5 py-1.5 font-medium transition ${
                  transition === "snap"
                    ? "border-amber-500 bg-amber-500/20 text-amber-200"
                    : "border-zinc-700 bg-zinc-900 text-zinc-400 hover:text-zinc-200"
                }`}
              >
                Snap (Instant)
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  };

  const renderDefaultInspector = () => (
    <div className="space-y-3 text-xs">
      <div className="rounded-lg border border-dashed border-zinc-700 p-4 text-center text-zinc-400">
        Click any Video/Overlay clip, Audio clip, Caption/Voiceover block, Junction transition,
        Track header, or Camera keyframe to inspect its settings.
      </div>

      <div className="space-y-2.5 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
        <span className="block font-semibold text-zinc-200">Global Caption Style</span>
        <div className="grid grid-cols-3 gap-2">
          <label className="flex flex-col gap-1">
            <span className="text-[11px] text-zinc-400">Font Size</span>
            <BufferedNumberInput
              step={2}
              min={16}
              max={200}
              precision={0}
              value={project.caption_settings.font_size}
              onCommit={(font_size) =>
                onUpdateProject(
                  (prev) => ({
                    ...prev,
                    caption_settings: { ...prev.caption_settings, font_size },
                  }),
                  { label: "Change global font size", coalesceKey: "global-font-size" }
                )
              }
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] text-zinc-400">Outline</span>
            <BufferedNumberInput
              step={1}
              min={0}
              max={16}
              precision={0}
              value={project.caption_settings.outline_width}
              onCommit={(outline_width) =>
                onUpdateProject(
                  (prev) => ({
                    ...prev,
                    caption_settings: { ...prev.caption_settings, outline_width },
                  }),
                  { label: "Change outline width", coalesceKey: "global-outline" }
                )
              }
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] text-zinc-400">Margin V</span>
            <BufferedNumberInput
              step={10}
              min={0}
              max={1600}
              precision={0}
              value={project.caption_settings.margin_v}
              onCommit={(margin_v) =>
                onUpdateProject(
                  (prev) => ({
                    ...prev,
                    caption_settings: { ...prev.caption_settings, margin_v },
                  }),
                  { label: "Change caption margin V", coalesceKey: "global-margin-v" }
                )
              }
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>
        </div>
      </div>

      {renderPronunciationDictionary()}
    </div>
  );

  let body: React.ReactNode = null;
  if (selection?.type === "video-clip") {
    body = renderVideoClipInspector(selection.trackId, selection.clipId);
  } else if (selection?.type === "audio-clip") {
    body = renderAudioClipInspector(selection.trackId, selection.clipId);
  } else if (selection?.type === "caption-clip") {
    body = renderCaptionClipInspector(selection.trackId, selection.clipId);
  } else if (selection?.type === "transition") {
    body = renderTransitionInspector(selection.trackId, selection.transitionId);
  } else if (selection?.type === "track") {
    body = renderTrackInspector(selection.trackId);
  } else if (selection?.type === "keyframe") {
    body = renderKeyframeInspector(selection.index);
  }

  return (
    <aside className="flex w-full lg:w-[340px] xl:w-[360px] shrink-0 flex-col overflow-hidden rounded-xl border border-zinc-800 bg-[#151821]">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <div className="flex items-center gap-2">
          <HeaderIcon className="h-4 w-4 text-emerald-400" />
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-200">
            {headerTitle}
          </h2>
        </div>

        <label className="flex items-center gap-1.5 text-xs text-zinc-300">
          <span className="text-zinc-400">Voice</span>
          <select
            aria-label="Project Voice"
            value={project.voice}
            onChange={(e) =>
              onUpdateProject(
                (prev) => ({ ...prev, voice: e.target.value }),
                { label: `Change voice (${e.target.value})` }
              )
            }
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100 focus:border-emerald-500 focus:outline-none"
          >
            {voiceOptions.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="flex-1 overflow-y-auto p-3">
        {body ?? renderDefaultInspector()}
      </div>
    </aside>
  );
}
