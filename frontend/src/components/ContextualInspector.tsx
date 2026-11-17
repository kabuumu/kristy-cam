import { useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Captions,
  CheckCircle2,
  Copy,
  Film,
  KeyRound,
  Plus,
  Scissors,
  Trash2,
  Volume2,
} from "lucide-react";
import { apiClient } from "../api/client";
import type {
  ArtifactManifest,
  ClauseInput,
  DurationMode,
  KeyframeTransition,
  ProjectInput,
  SegmentInput,
  TimelineSelection,
} from "../types/studio";
import {
  autoSplitTextToClauses,
  clampCropPosition,
  computeClientTtsSignature,
  formatTimecode,
  resolveClauseSpokenText,
  type TimelineClipLayout,
} from "../utils/studioMath";

interface ContextualInspectorProps {
  project: ProjectInput;
  manifest: ArtifactManifest;
  selectedSegment: SegmentInput | null;
  clipLayout?: TimelineClipLayout | null;
  canSplitAtPlayhead?: boolean;
  selection: TimelineSelection;
  voices: string[];
  ttsError: string | null;
  lastSynthesizedSignatures: Record<string, string>;
  onUpdateProject: (updater: (prev: ProjectInput) => ProjectInput) => void;
  onUpdateSegment: (updated: SegmentInput) => void;
  onDuplicateSegment: (segId: string) => void;
  onDeleteSegment: (segId: string) => void;
  onSplitSegment?: () => void;
  onReorderSegment?: (fromIndex: number, toIndex: number) => void;
  onSelectItem: (sel: TimelineSelection) => void;
}

export function ContextualInspector({
  project,
  manifest,
  selectedSegment,
  clipLayout = null,
  canSplitAtPlayhead = false,
  selection,
  voices,
  ttsError,
  lastSynthesizedSignatures,
  onUpdateProject,
  onUpdateSegment,
  onDuplicateSegment,
  onDeleteSegment,
  onSplitSegment,
  onReorderSegment,
  onSelectItem,
}: ContextualInspectorProps) {
  const [newPronKey, setNewPronKey] = useState("");
  const [newPronVal, setNewPronVal] = useState("");
  const [showPronEditor, setShowPronEditor] = useState(false);
  const [confirmDeleteSeg, setConfirmDeleteSeg] = useState(false);

  const srcW = manifest.source_metadata?.width ?? 1920;
  const srcH = manifest.source_metadata?.height ?? 1080;
  const srcDur = manifest.source_metadata?.duration ?? 60;
  const outW = project.output_settings.width;
  const outH = project.output_settings.height;

  const voiceOptions =
    voices.length > 0
      ? voices.includes(project.voice)
        ? voices
        : [project.voice, ...voices]
      : [project.voice];

  const segArtifacts = selectedSegment
    ? manifest.segments[selectedSegment.id]
    : undefined;

  const currentSig = selectedSegment
    ? computeClientTtsSignature(
        selectedSegment,
        project.pronunciations,
        project.voice,
        project.language,
        project.output_settings.inter_clause_pause_ms
      )
    : "";

  const hasClauses = Boolean(
    selectedSegment &&
      selectedSegment.clauses.some((c) => c.display_text.trim().length > 0)
  );
  const lastSig = selectedSegment
    ? lastSynthesizedSignatures[selectedSegment.id]
    : undefined;
  const hasAudioArtifact = Boolean(segArtifacts?.audio_artifact_id);
  const isStale =
    Boolean(selectedSegment) &&
    hasClauses &&
    (!hasAudioArtifact || (lastSig !== undefined && lastSig !== currentSig));

  const headerTitle =
    selection?.type === "keyframe"
      ? "Camera / View Window"
      : selection?.type === "clause"
        ? "Narration & Caption"
        : selection?.type === "video"
          ? "Video Clip"
          : "Inspector";

  const HeaderIcon =
    selection?.type === "keyframe"
      ? KeyRound
      : selection?.type === "clause"
        ? Captions
        : Film;

  const handleAddPronunciation = () => {
    const k = newPronKey.trim();
    const v = newPronVal.trim();
    if (!k || !v) return;
    onUpdateProject((prev) => ({
      ...prev,
      pronunciations: { ...prev.pronunciations, [k]: v },
    }));
    setNewPronKey("");
    setNewPronVal("");
  };

  const handleDeletePronunciation = (key: string) => {
    onUpdateProject((prev) => {
      const next = { ...prev.pronunciations };
      delete next[key];
      return { ...prev, pronunciations: next };
    });
  };

  const renderVideoInspector = (seg: SegmentInput) => {
    const segIdx = project.segments.findIndex((s) => s.id === seg.id);
    return (
    <div className="space-y-3 text-xs">
      {clipLayout && (
        <div className="flex items-center justify-between rounded-lg border border-zinc-800 bg-[#11141b] px-3 py-2 font-mono text-[11px] text-zinc-300">
          <span>
            Clip #{clipLayout.index + 1} • Start @{" "}
            <span className="text-emerald-300">{formatTimecode(clipLayout.startSec)}</span>
          </span>
          <span className="text-zinc-400">
            Dur {clipLayout.plan.finalDur.toFixed(2)}s
          </span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-2.5 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
        <label className="flex flex-col gap-1">
          <span className="text-zinc-400">Clip Start (s)</span>
          <input
            type="number"
            step={0.1}
            min={0}
            max={Math.max(0, seg.video_out - 0.1)}
            value={Number(seg.video_in.toFixed(2))}
            onChange={(e) => {
              const val = parseFloat(e.target.value);
              if (Number.isFinite(val) && val >= 0 && val < seg.video_out) {
                onUpdateSegment({ ...seg, video_in: Number(val.toFixed(2)) });
              }
            }}
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-zinc-400">Clip End (s)</span>
          <input
            type="number"
            step={0.1}
            min={seg.video_in + 0.1}
            max={srcDur}
            value={Number(seg.video_out.toFixed(2))}
            onChange={(e) => {
              const val = parseFloat(e.target.value);
              if (Number.isFinite(val) && val > seg.video_in && val <= srcDur) {
                onUpdateSegment({ ...seg, video_out: Number(val.toFixed(2)) });
              }
            }}
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
          />
        </label>

        <label className="col-span-2 flex flex-col gap-1">
          <span className="text-zinc-400">Length Mode</span>
          <select
            aria-label="Duration Mode"
            value={seg.duration_mode}
            onChange={(e) =>
              onUpdateSegment({
                ...seg,
                duration_mode: e.target.value as DurationMode,
              })
            }
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100 focus:border-emerald-500 focus:outline-none"
          >
            <option value="preserve_video">Preserve Video Length</option>
            <option value="fit_narration">Extend / Freeze to Fit Narration</option>
          </select>
        </label>

        <label className="col-span-2 flex flex-col gap-1">
          <div className="flex justify-between text-zinc-400">
            <span className="inline-flex items-center gap-1">
              <Volume2 className="h-3.5 w-3.5" />
              Game Audio Volume
            </span>
            <span className="font-mono text-zinc-200">
              {Math.round(seg.source_audio_volume * 100)}%
            </span>
          </div>
          <input
            type="range"
            aria-label="Source Audio Volume"
            min={0}
            max={1}
            step={0.05}
            value={seg.source_audio_volume}
            onChange={(e) =>
              onUpdateSegment({
                ...seg,
                source_audio_volume: parseFloat(e.target.value),
              })
            }
          />
        </label>
      </div>

      {(onSplitSegment || onReorderSegment) && (
        <div className="flex items-center gap-2">
          {onSplitSegment && (
            <button
              type="button"
              disabled={!canSplitAtPlayhead}
              onClick={onSplitSegment}
              title="Split clip at playhead (S)"
              className="inline-flex flex-1 items-center justify-center gap-1.5 rounded border border-zinc-700 bg-zinc-800 px-2.5 py-1.5 text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
            >
              <Scissors className="h-3.5 w-3.5" />
              Split at Playhead
            </button>
          )}
          {onReorderSegment && segIdx >= 0 && (
            <>
              <button
                type="button"
                disabled={segIdx === 0}
                onClick={() => onReorderSegment(segIdx, segIdx - 1)}
                title="Move clip earlier"
                className="inline-flex items-center justify-center rounded border border-zinc-700 bg-zinc-800 p-1.5 text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
              >
                <ArrowLeft className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                disabled={segIdx >= project.segments.length - 1}
                onClick={() => onReorderSegment(segIdx, segIdx + 1)}
                title="Move clip later"
                className="inline-flex items-center justify-center rounded border border-zinc-700 bg-zinc-800 p-1.5 text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
              >
                <ArrowRight className="h-3.5 w-3.5" />
              </button>
            </>
          )}
        </div>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => onDuplicateSegment(seg.id)}
          className="inline-flex flex-1 items-center justify-center gap-1.5 rounded border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-zinc-200 hover:bg-zinc-700"
        >
          <Copy className="h-3.5 w-3.5" />
          Duplicate Clip
        </button>

        {!confirmDeleteSeg ? (
          <button
            type="button"
            onClick={() => setConfirmDeleteSeg(true)}
            className="inline-flex items-center justify-center gap-1.5 rounded border border-rose-500/40 bg-rose-500/15 px-3 py-1.5 text-rose-200 hover:bg-rose-500/25"
          >
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </button>
        ) : (
          <button
            type="button"
            onClick={() => {
              setConfirmDeleteSeg(false);
              onDeleteSegment(seg.id);
            }}
            className="rounded bg-rose-600 px-3 py-1.5 font-medium text-white hover:bg-rose-500"
          >
            Confirm Delete
          </button>
        )}
      </div>
    </div>
    );
  };

  const renderKeyframeInspector = (seg: SegmentInput, kfIndex: number) => {
    const kf = seg.crop_keyframes[kfIndex];
    if (!kf) return null;

    const updateKf = (patch: Partial<typeof kf>) => {
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
      const next = seg.crop_keyframes.map((item, idx) =>
        idx === kfIndex
          ? {
              ...merged,
              t: idx === 0 ? 0 : Number(Math.max(0.01, merged.t).toFixed(2)),
              x: Number(clamped.x.toFixed(4)),
              y: Number(clamped.y.toFixed(4)),
              zoom: Number(clamped.zoom.toFixed(3)),
            }
          : item
      );
      if (kfIndex > 0) {
        next.sort((a, b) => a.t - b.t);
      }
      onUpdateSegment({ ...seg, crop_keyframes: next });
    };

    const transition: KeyframeTransition = kf.transition ?? "tween";

    return (
      <div className="space-y-3 text-xs">
        <div className="space-y-3 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
          <div className="flex items-center justify-between">
            <span className="font-mono text-amber-300">
              Keyframe #{kfIndex + 1} @ {kf.t.toFixed(2)}s
            </span>
            {kfIndex > 0 && (
              <button
                type="button"
                onClick={() => {
                  const next = seg.crop_keyframes.filter((_, i) => i !== kfIndex);
                  onUpdateSegment({ ...seg, crop_keyframes: next });
                  onSelectItem({ type: "video", segId: seg.id });
                }}
                className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-rose-300 hover:bg-rose-500/20"
              >
                <Trash2 className="h-3 w-3" />
                Delete
              </button>
            )}
          </div>

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
                  if (Number.isFinite(v)) updateKf({ x: v });
                }}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
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
                  if (Number.isFinite(v)) updateKf({ y: v });
                }}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
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
              onChange={(e) => updateKf({ zoom: parseFloat(e.target.value) })}
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

  const renderClauseInspector = (seg: SegmentInput, idx: number) => {
    const clause = seg.clauses[idx];
    if (!clause) return null;

    const timing = segArtifacts?.clause_timings?.find(
      (t) => t.clause_id === clause.id
    );
    const effectiveStart = clause.start_sec ?? timing?.start_sec ?? 0;
    const effectiveDur =
      clause.duration_sec ??
      (timing ? Math.max(0.2, timing.end_sec - timing.start_sec) : 1.5);

    const updateClause = (patch: Partial<ClauseInput>) => {
      const next = seg.clauses.map((c, i) =>
        i === idx ? { ...c, ...patch } : c
      );
      onUpdateSegment({ ...seg, clauses: next });
    };

    const handleSplit = () => {
      const parts = autoSplitTextToClauses(clause.display_text);
      if (parts.length <= 1) {
        const words = clause.display_text.trim().split(/\s+/);
        if (words.length < 2) return;
        const mid = Math.ceil(words.length / 2);
        parts.splice(0, 1, words.slice(0, mid).join(" "), words.slice(mid).join(" "));
      }
      const existingIds = new Set(seg.clauses.map((c) => c.id));
      const newClauses: ClauseInput[] = parts.map((text, pIdx) => {
        if (pIdx === 0) {
          return {
            ...clause,
            display_text: text,
            spoken_text_override: null,
          };
        }
        let counter = seg.clauses.length + pIdx;
        while (existingIds.has(`c${counter}`)) counter += 1;
        existingIds.add(`c${counter}`);
        return {
          id: `c${counter}`,
          display_text: text,
          spoken_text_override: null,
        };
      });
      const next = [
        ...seg.clauses.slice(0, idx),
        ...newClauses,
        ...seg.clauses.slice(idx + 1),
      ];
      onUpdateSegment({ ...seg, clauses: next });
    };

    const resolvedSpoken = resolveClauseSpokenText(clause, project.pronunciations);

    return (
      <div className="space-y-3 text-xs">
        <div className="space-y-3 rounded-lg border border-zinc-800 bg-[#11141b] p-3">
          <div className="flex items-center justify-between">
            <span className="font-mono text-sky-300">Block {clause.id}</span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={handleSplit}
                className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] text-zinc-300 hover:bg-zinc-800"
              >
                <Scissors className="h-3 w-3" />
                Split
              </button>
              <button
                type="button"
                onClick={() => {
                  const next = seg.clauses.filter((_, i) => i !== idx);
                  onUpdateSegment({ ...seg, clauses: next });
                  onSelectItem({ type: "video", segId: seg.id });
                }}
                className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] text-rose-300 hover:bg-rose-500/20"
              >
                <Trash2 className="h-3 w-3" />
                Delete
              </button>
            </div>
          </div>

          <label className="block space-y-1">
            <span className="text-zinc-400">Caption Text</span>
            <textarea
              rows={2}
              value={clause.display_text}
              onChange={(e) => updateClause({ display_text: e.target.value })}
              placeholder="On-screen caption..."
              className="w-full rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-zinc-100 focus:border-emerald-500 focus:outline-none"
            />
          </label>

          <label className="block space-y-1">
            <span className="text-zinc-400">Alternate Narration (optional)</span>
            <input
              type="text"
              value={clause.spoken_text_override ?? ""}
              onChange={(e) =>
                updateClause({
                  spoken_text_override:
                    e.target.value.length > 0 ? e.target.value : null,
                })
              }
              placeholder="Leave blank to speak caption text..."
              className="w-full rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-zinc-100 focus:border-emerald-500 focus:outline-none"
            />
          </label>

          <div className="rounded bg-emerald-950/30 px-2 py-1 text-[11px] text-emerald-200">
            <span className="text-emerald-400">Spoken: </span>
            <span>{resolvedSpoken || "—"}</span>
          </div>

          <div className="grid grid-cols-2 gap-2.5">
            <label className="flex flex-col gap-1">
              <span className="text-zinc-400">Start Point (s)</span>
              <input
                type="number"
                step={0.1}
                min={0}
                value={Number(effectiveStart.toFixed(2))}
                onChange={(e) => {
                  const val = parseFloat(e.target.value);
                  if (Number.isFinite(val) && val >= 0) {
                    updateClause({ start_sec: Number(val.toFixed(2)) });
                  }
                }}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-zinc-400">Length (s)</span>
              <input
                type="number"
                step={0.1}
                min={0.2}
                value={Number(effectiveDur.toFixed(2))}
                onChange={(e) => {
                  const val = parseFloat(e.target.value);
                  if (Number.isFinite(val) && val >= 0.2) {
                    updateClause({ duration_sec: Number(val.toFixed(2)) });
                  }
                }}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
              />
            </label>
          </div>

          <label className="flex flex-col gap-1">
            <div className="flex justify-between text-zinc-400">
              <span>Narration Speed</span>
              <span className="font-mono text-zinc-200">
                {seg.speed.toFixed(2)}x
              </span>
            </div>
            <input
              type="range"
              aria-label="Narration Speed"
              min={0.8}
              max={1.25}
              step={0.05}
              value={seg.speed}
              onChange={(e) =>
                onUpdateSegment({
                  ...seg,
                  speed: parseFloat(e.target.value),
                })
              }
            />
          </label>
        </div>

        {/* Voiceover Sync Status & Audio */}
        <div className="space-y-2 rounded-lg border border-zinc-800 bg-[#11141b] p-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[11px] text-zinc-400">Audio &amp; Text Files</span>
            {ttsError && (
              <span className="inline-flex items-center gap-1 rounded border border-rose-500/50 bg-rose-500/20 px-2 py-0.5 text-[11px] text-rose-200">
                <AlertCircle className="h-3 w-3" />
                Error
              </span>
            )}
            {isStale ? (
              <span className="inline-flex items-center gap-1 rounded border border-amber-500/50 bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-200">
                <AlertTriangle className="h-3 w-3" />
                Updated — Click Preview
              </span>
            ) : hasAudioArtifact ? (
              <span className="inline-flex items-center gap-1 rounded border border-emerald-500/40 bg-emerald-500/15 px-2 py-0.5 text-[11px] text-emerald-200">
                <CheckCircle2 className="h-3 w-3" />
                Synced ({segArtifacts?.audio_duration.toFixed(2)}s)
              </span>
            ) : (
              <span className="text-[11px] text-zinc-500">Not rendered yet</span>
            )}
          </div>
          {ttsError && <p className="text-[11px] text-rose-300">{ttsError}</p>}
          {segArtifacts?.audio_artifact_id && (
            <audio
              controls
              src={`${apiClient.mediaUrl(project.project_id, segArtifacts.audio_artifact_id)}?v=${segArtifacts.tts_hash ?? ""}`}
              className="h-8 w-full"
            />
          )}
        </div>

        {/* Pronunciation Dictionary */}
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
                        onUpdateProject((prev) => ({
                          ...prev,
                          pronunciations: {
                            ...prev.pronunciations,
                            [k]: e.target.value,
                          },
                        }))
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
      </div>
    );
  };

  return (
    <aside className="flex w-full lg:w-[370px] shrink-0 flex-col overflow-hidden rounded-xl border border-zinc-800 bg-[#151821]">
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
              onUpdateProject((prev) => ({ ...prev, voice: e.target.value }))
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
        {!selectedSegment || !selection ? (
          <div className="rounded-lg border border-dashed border-zinc-700 p-6 text-center text-xs text-zinc-400">
            Select a Video clip, Camera keyframe, or Narration &amp; Caption block
            on the timeline to edit its options.
          </div>
        ) : selection.type === "video" ? (
          renderVideoInspector(selectedSegment)
        ) : selection.type === "keyframe" ? (
          renderKeyframeInspector(selectedSegment, selection.index)
        ) : (
          renderClauseInspector(selectedSegment, selection.index)
        )}
      </div>
    </aside>
  );
}
