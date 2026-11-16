import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Copy,
  Pause,
  Play,
  Plus,
  Snowflake,
  Trash2,
} from "lucide-react";
import type { ArtifactManifest, SegmentInput } from "../types/studio";
import { computeDurationPlan, formatTimecode } from "../utils/studioMath";

interface TimelineProps {
  segments: SegmentInput[];
  manifest: ArtifactManifest;
  selectedSegId: string | null;
  tailPaddingSec: number;
  sourceTime: number;
  isModalOpen: boolean;
  isProjectPlaying: boolean;
  onSelectSegment: (segId: string) => void;
  onSourceTimeChange: (t: number) => void;
  onAddSegment: (videoIn: number, videoOut: number) => void;
  onUpdateSegment: (updated: SegmentInput) => void;
  onReorderSegment: (index: number, direction: -1 | 1) => void;
  onDuplicateSegment: (index: number) => void;
  onDeleteSegment: (index: number) => void;
  onToggleProjectPlayback: () => void;
}

export function Timeline({
  segments,
  manifest,
  selectedSegId,
  tailPaddingSec,
  sourceTime,
  isModalOpen,
  isProjectPlaying,
  onSelectSegment,
  onSourceTimeChange,
  onAddSegment,
  onUpdateSegment,
  onReorderSegment,
  onDuplicateSegment,
  onDeleteSegment,
  onToggleProjectPlayback,
}: TimelineProps) {
  const sourceDur = manifest.source_metadata?.duration ?? 60;
  const [markIn, setMarkIn] = useState(0);
  const [markOut, setMarkOut] = useState(() => Math.min(3.0, sourceDur));
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  useEffect(() => {
    setMarkOut((prev) => (prev > sourceDur ? Math.max(0.5, sourceDur) : prev));
  }, [sourceDur]);

  // Global keyboard shortcuts I (Set In) and O (Set Out), disabled inside inputs/textareas/selects
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (isModalOpen) return;
      const active = document.activeElement;
      const tag = active?.tagName.toLowerCase();
      if (
        tag === "input" ||
        tag === "textarea" ||
        tag === "select" ||
        (active instanceof HTMLElement && active.isContentEditable)
      ) {
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      if (e.key === "i" || e.key === "I") {
        e.preventDefault();
        const clampedIn = Math.max(0, Math.min(sourceTime, sourceDur - 0.1));
        setMarkIn(Number(clampedIn.toFixed(2)));
        if (markOut <= clampedIn) {
          setMarkOut(Number(Math.min(sourceDur, clampedIn + 2.0).toFixed(2)));
        }
      } else if (e.key === "o" || e.key === "O") {
        e.preventDefault();
        const clampedOut = Math.max(markIn + 0.1, Math.min(sourceTime, sourceDur));
        setMarkOut(Number(clampedOut.toFixed(2)));
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isModalOpen, sourceTime, sourceDur, markIn, markOut]);

  const handleSetIn = () => {
    const clampedIn = Math.max(0, Math.min(sourceTime, sourceDur - 0.1));
    setMarkIn(Number(clampedIn.toFixed(2)));
    if (markOut <= clampedIn) {
      setMarkOut(Number(Math.min(sourceDur, clampedIn + 2.0).toFixed(2)));
    }
  };

  const handleSetOut = () => {
    const clampedOut = Math.max(markIn + 0.1, Math.min(sourceTime, sourceDur));
    setMarkOut(Number(clampedOut.toFixed(2)));
  };

  const handleAdd = () => {
    const safeIn = Math.max(0, Math.min(markIn, sourceDur - 0.1));
    const safeOut = Math.max(safeIn + 0.1, Math.min(markOut, sourceDur));
    onAddSegment(Number(safeIn.toFixed(2)), Number(safeOut.toFixed(2)));
  };

  return (
    <section className="flex shrink-0 flex-col rounded-xl border border-zinc-800 bg-[#151821]">
      {/* Source Capture Scrub Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800 px-3 py-2 text-xs">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onToggleProjectPlayback}
            disabled={segments.length === 0}
            className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-2.5 py-1 font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
            title="Continuous project playback following duration plan"
          >
            {isProjectPlaying ? (
              <>
                <Pause className="h-3.5 w-3.5" />
                Stop Playback
              </>
            ) : (
              <>
                <Play className="h-3.5 w-3.5" />
                Play Project
              </>
            )}
          </button>

          <span className="font-mono text-zinc-400">
            Src: {formatTimecode(sourceTime)} / {formatTimecode(sourceDur)}
          </span>
        </div>

        <div className="flex min-w-[200px] flex-1 items-center gap-2">
          <input
            type="range"
            aria-label="Source Capture Scrubber"
            min={0}
            max={Math.max(0.1, sourceDur)}
            step={0.033}
            value={Math.min(sourceTime, sourceDur)}
            onChange={(e) => onSourceTimeChange(parseFloat(e.target.value))}
            className="w-full"
          />
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={handleSetIn}
            className="rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-zinc-200 hover:bg-zinc-700"
            title="Shortcut: I"
          >
            I: {markIn.toFixed(2)}s
          </button>
          <button
            type="button"
            onClick={handleSetOut}
            className="rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-zinc-200 hover:bg-zinc-700"
            title="Shortcut: O"
          >
            O: {markOut.toFixed(2)}s
          </button>
          <button
            type="button"
            onClick={handleAdd}
            className="inline-flex items-center gap-1 rounded border border-emerald-500/40 bg-emerald-500/15 px-2.5 py-1 font-medium text-emerald-200 hover:bg-emerald-500/25"
          >
            <Plus className="h-3.5 w-3.5" />
            Add Segment
          </button>
        </div>
      </div>

      {/* Horizontal Ordered Segment Cards */}
      <div className="flex gap-2.5 overflow-x-auto p-3">
        {segments.length === 0 ? (
          <div className="w-full rounded-lg border border-dashed border-zinc-700 py-5 text-center text-xs text-zinc-400">
            No segments yet. Scrub the source bar, press I / O, and click &ldquo;+ Add Segment&rdquo;.
          </div>
        ) : (
          segments.map((seg, idx) => {
            const isSelected = seg.id === selectedSegId;
            const segArt = manifest.segments[seg.id];
            const audioDur = segArt?.audio_duration ?? 0;
            const plan = computeDurationPlan(
              seg,
              audioDur,
              sourceDur,
              tailPaddingSec
            );

            return (
              <div
                key={seg.id}
                onClick={() => onSelectSegment(seg.id)}
                className={`flex w-64 shrink-0 cursor-pointer flex-col gap-2 rounded-lg border p-2.5 transition ${
                  isSelected
                    ? "border-emerald-500 bg-emerald-500/10"
                    : "border-zinc-800 bg-[#11141b] hover:border-zinc-700"
                }`}
              >
                <div className="flex items-center justify-between gap-1">
                  <span className="font-mono text-xs font-semibold text-zinc-100">
                    #{idx + 1} • {seg.id}
                  </span>
                  <div
                    className="flex items-center gap-1"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      type="button"
                      disabled={idx === 0}
                      onClick={() => onReorderSegment(idx, -1)}
                      className="rounded p-1 text-zinc-400 hover:bg-zinc-800 disabled:opacity-30"
                      title="Move left"
                    >
                      <ArrowLeft className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      disabled={idx === segments.length - 1}
                      onClick={() => onReorderSegment(idx, 1)}
                      className="rounded p-1 text-zinc-400 hover:bg-zinc-800 disabled:opacity-30"
                      title="Move right"
                    >
                      <ArrowRight className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => onDuplicateSegment(idx)}
                      className="rounded p-1 text-zinc-400 hover:bg-zinc-800"
                      title="Duplicate segment"
                    >
                      <Copy className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmDeleteId(seg.id)}
                      className="rounded p-1 text-zinc-400 hover:bg-rose-500/20 hover:text-rose-300"
                      title="Delete segment"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>

                {/* Numeric In / Out Trim Inputs */}
                <div
                  className="grid grid-cols-2 gap-2 text-[11px]"
                  onClick={(e) => e.stopPropagation()}
                >
                  <label className="flex flex-col gap-0.5 text-zinc-400">
                    <span>In (s)</span>
                    <input
                      type="number"
                      name="video_in"
                      aria-label="Segment In"
                      step={0.1}
                      min={0}
                      max={Math.max(0, seg.video_out - 0.1)}
                      value={seg.video_in}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value);
                        if (Number.isFinite(val) && val >= 0 && val < seg.video_out) {
                          onUpdateSegment({ ...seg, video_in: val });
                        }
                      }}
                      className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
                    />
                  </label>
                  <label className="flex flex-col gap-0.5 text-zinc-400">
                    <span>Out (s)</span>
                    <input
                      type="number"
                      name="video_out"
                      aria-label="Segment Out"
                      step={0.1}
                      min={seg.video_in + 0.1}
                      max={sourceDur > 0 ? sourceDur : undefined}
                      value={seg.video_out}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value);
                        if (Number.isFinite(val) && val > seg.video_in) {
                          onUpdateSegment({ ...seg, video_out: val });
                        }
                      }}
                      className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
                    />
                  </label>
                </div>

                {/* Duration Summary */}
                <div className="grid grid-cols-3 gap-1 rounded bg-zinc-900/80 px-2 py-1 text-[10px] font-mono text-zinc-300">
                  <div>
                    <div className="text-zinc-500">Vid</div>
                    <div>{plan.rawVideoDur.toFixed(2)}s</div>
                  </div>
                  <div>
                    <div className="text-zinc-500">TTS</div>
                    <div>{plan.narrationDur.toFixed(2)}s</div>
                  </div>
                  <div>
                    <div className="text-zinc-500">Final</div>
                    <div className="text-emerald-300">{plan.finalDur.toFixed(2)}s</div>
                  </div>
                </div>

                {/* Non-color-only Freeze Frame / Source Extended Badges */}
                {(plan.isFreezeFrame || plan.isSourceExtended) && (
                  <div className="flex flex-wrap gap-1">
                    {plan.isFreezeFrame && (
                      <span className="inline-flex items-center gap-1 rounded border border-sky-500/40 bg-sky-500/15 px-1.5 py-0.5 text-[10px] font-medium text-sky-200">
                        <Snowflake className="h-3 w-3" />
                        Freeze Frame
                      </span>
                    )}
                    {plan.isSourceExtended && (
                      <span className="inline-flex items-center gap-1 rounded border border-amber-500/40 bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-200">
                        <AlertTriangle className="h-3 w-3" />
                        Source Extended
                      </span>
                    )}
                  </div>
                )}

                {/* Delete Confirmation Prompt */}
                {confirmDeleteId === seg.id && (
                  <div
                    className="flex items-center justify-between gap-2 rounded border border-rose-500/50 bg-rose-500/15 px-2 py-1 text-[11px] text-rose-200"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <span>Delete {seg.id}?</span>
                    <div className="flex gap-1">
                      <button
                        type="button"
                        onClick={() => {
                          setConfirmDeleteId(null);
                          onDeleteSegment(idx);
                        }}
                        className="rounded bg-rose-600 px-1.5 py-0.5 text-[10px] font-medium text-white hover:bg-rose-500"
                      >
                        Confirm
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmDeleteId(null)}
                        className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-200 hover:bg-zinc-700"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
