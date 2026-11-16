import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Captions,
  Copy,
  Film,
  KeyRound,
  Pause,
  Play,
  Plus,
  Snowflake,
  Trash2,
  Volume2,
} from "lucide-react";
import type { ArtifactManifest, SegmentInput } from "../types/studio";
import { computeDurationPlan, formatTimecode } from "../utils/studioMath";
import { usageLogger } from "../utils/usageLogger";

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
  const [isScrubbingTracks, setIsScrubbingTracks] = useState(false);
  const trackLaneRef = useRef<HTMLDivElement | null>(null);

  const selectedSegment =
    segments.find((s) => s.id === selectedSegId) ?? null;

  useEffect(() => {
    setMarkOut((prev) => (prev > sourceDur ? Math.max(0.5, sourceDur) : prev));
  }, [sourceDur]);

  const scrubTracksAtClientX = (clientX: number) => {
    if (!selectedSegment || !trackLaneRef.current) return;
    const rect = trackLaneRef.current.getBoundingClientRect();
    if (rect.width <= 0) return;
    const segArt = manifest.segments[selectedSegment.id];
    const audioDur = segArt?.audio_duration ?? 0;
    const plan = computeDurationPlan(
      selectedSegment,
      audioDur,
      sourceDur,
      tailPaddingSec
    );
    const totalDur = Math.max(0.1, plan.finalDur);
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const relTime = ratio * totalDur;
    onSourceTimeChange(
      selectedSegment.video_in + Math.min(plan.rawVideoDur, relTime)
    );
  };

  useEffect(() => {
    if (!isScrubbingTracks) return;
    const onMove = (e: MouseEvent) => scrubTracksAtClientX(e.clientX);
    const onUp = () => setIsScrubbingTracks(false);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  });

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
        const clampedIn = Number(
          Math.max(0, Math.min(sourceTime, sourceDur - 0.1)).toFixed(2)
        );
        const nextOut =
          markOut <= clampedIn
            ? Number(Math.min(sourceDur, clampedIn + 2.0).toFixed(2))
            : markOut;
        setMarkIn(clampedIn);
        if (nextOut !== markOut) {
          setMarkOut(nextOut);
        }
        usageLogger.logActionOutput(
          "Mark segment In-point via keyboard",
          "timeline.mark_in",
          { mark_in: clampedIn, mark_out: nextOut, via: "key_i" }
        );
      } else if (e.key === "o" || e.key === "O") {
        e.preventDefault();
        const clampedOut = Number(
          Math.max(markIn + 0.1, Math.min(sourceTime, sourceDur)).toFixed(2)
        );
        setMarkOut(clampedOut);
        usageLogger.logActionOutput(
          "Mark segment Out-point via keyboard",
          "timeline.mark_out",
          { mark_in: markIn, mark_out: clampedOut, via: "key_o" }
        );
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isModalOpen, sourceTime, sourceDur, markIn, markOut]);

  const handleSetIn = () => {
    const clampedIn = Number(
      Math.max(0, Math.min(sourceTime, sourceDur - 0.1)).toFixed(2)
    );
    const nextOut =
      markOut <= clampedIn
        ? Number(Math.min(sourceDur, clampedIn + 2.0).toFixed(2))
        : markOut;
    setMarkIn(clampedIn);
    if (nextOut !== markOut) {
      setMarkOut(nextOut);
    }
    usageLogger.logActionOutput(
      "Mark segment In-point",
      "timeline.mark_in",
      { mark_in: clampedIn, mark_out: nextOut, via: "button" }
    );
  };

  const handleSetOut = () => {
    const clampedOut = Number(
      Math.max(markIn + 0.1, Math.min(sourceTime, sourceDur)).toFixed(2)
    );
    setMarkOut(clampedOut);
    usageLogger.logActionOutput(
      "Mark segment Out-point",
      "timeline.mark_out",
      { mark_in: markIn, mark_out: clampedOut, via: "button" }
    );
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

      {/* Joined-up Video / Audio / Captions / Keyframes Alignment View */}
      {selectedSegment && (() => {
        const segArt = manifest.segments[selectedSegment.id];
        const audioDur = segArt?.audio_duration ?? 0;
        const plan = computeDurationPlan(
          selectedSegment,
          audioDur,
          sourceDur,
          tailPaddingSec
        );
        const totalDur = Math.max(0.1, plan.finalDur);
        const totalFrames = plan.frameCount;
        const segRelTime = Math.max(
          0,
          Math.min(totalDur, sourceTime - selectedSegment.video_in)
        );
        const currentFrame = Math.min(totalFrames, Math.round(segRelTime * 30));
        const playheadPct = Math.max(0, Math.min(100, (segRelTime / totalDur) * 100));
        const videoEndPct = Math.max(
          0,
          Math.min(100, (plan.rawVideoDur / totalDur) * 100)
        );
        const audioEndPct = Math.max(
          0,
          Math.min(100, (audioDur / totalDur) * 100)
        );

        const timings = segArt?.clause_timings ?? [];
        const timingById = new Map(timings.map((t) => [t.clause_id, t]));
        const hasRealTimings =
          selectedSegment.clauses.length > 0 &&
          timings.length > 0 &&
          selectedSegment.clauses.some((c) => timingById.has(c.id));

        const captionBlocks = selectedSegment.clauses.map((clause, i) => {
          if (hasRealTimings) {
            const t = timingById.get(clause.id);
            if (!t) return null;
            const nextClause = selectedSegment.clauses[i + 1];
            const nextTiming = nextClause ? timingById.get(nextClause.id) : undefined;
            const endSec = nextTiming ? nextTiming.start_sec : t.end_sec;
            return {
              id: clause.id,
              text: clause.display_text,
              startSec: t.start_sec,
              endSec: Math.max(t.start_sec, endSec),
              estimated: false,
            };
          }
          const span = Math.max(0.1, plan.rawVideoDur) / Math.max(1, selectedSegment.clauses.length);
          const startSec = i * span;
          const endSec = (i + 1) * span;
          return {
            id: clause.id,
            text: clause.display_text,
            startSec,
            endSec,
            estimated: true,
          };
        }).filter((b): b is NonNullable<typeof b> => b !== null);

        const tickCount = 5;
        const ticks = Array.from({ length: tickCount + 1 }, (_, i) => {
          const frac = i / tickCount;
          const t = frac * totalDur;
          const f = Math.round(t * 30);
          return { frac, t, f };
        });

        return (
          <div className="border-t border-zinc-800 bg-[#11141b] px-3 py-2 space-y-1.5">
            <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-zinc-200">
                  Segment Alignment ({selectedSegment.id})
                </span>
                <span className="font-mono text-zinc-400">
                  {segRelTime.toFixed(2)}s / {totalDur.toFixed(2)}s •{" "}
                  <span className="text-emerald-300">
                    f{currentFrame} / {totalFrames}f
                  </span>{" "}
                  (30fps)
                </span>
              </div>
              <span className="text-[10px] text-zinc-500">
                Click or drag tracks to scrub frames, captions &amp; keyframes
              </span>
            </div>

            <div className="flex gap-2 select-none">
              {/* Track Labels */}
              <div className="w-20 shrink-0 space-y-1 pt-5 text-[10px] font-medium text-zinc-400">
                <div className="flex h-5 items-center gap-1">
                  <Film className="h-3 w-3 text-emerald-400" />
                  <span>Video</span>
                </div>
                <div className="flex h-5 items-center gap-1">
                  <Volume2 className="h-3 w-3 text-violet-400" />
                  <span>Audio</span>
                </div>
                <div className="flex h-6 items-center gap-1">
                  <Captions className="h-3 w-3 text-sky-400" />
                  <span>Captions</span>
                </div>
                <div className="flex h-5 items-center gap-1">
                  <KeyRound className="h-3 w-3 text-amber-400" />
                  <span>Keyframes</span>
                </div>
              </div>

              {/* Shared Scrubbable Multi-Track Lane */}
              <div
                ref={trackLaneRef}
                onMouseDown={(e) => {
                  setIsScrubbingTracks(true);
                  scrubTracksAtClientX(e.clientX);
                }}
                className="relative flex-1 cursor-ew-resize space-y-1 overflow-hidden rounded border border-zinc-800 bg-[#0b0d11] px-0 py-1"
              >
                {/* Time & Frame Ruler */}
                <div className="relative h-4 border-b border-zinc-800/80 text-[9px] font-mono text-zinc-500">
                  {ticks.map((tk, idx) => (
                    <div
                      key={idx}
                      className="absolute top-0 flex flex-col items-center"
                      style={{
                        left: `${tk.frac * 100}%`,
                        transform:
                          idx === 0
                            ? "translateX(2px)"
                            : idx === ticks.length - 1
                              ? "translateX(-100%)"
                              : "translateX(-50%)",
                      }}
                    >
                      <span>
                        {tk.t.toFixed(1)}s ({tk.f}f)
                      </span>
                    </div>
                  ))}
                </div>

                {/* Track 1: Video Frames */}
                <div className="relative h-5 bg-zinc-900/60">
                  <div
                    className="absolute inset-y-0 left-0 flex items-center overflow-hidden border-r border-emerald-400/50 bg-emerald-500/20 px-1.5 text-[10px] font-mono text-emerald-200"
                    style={{ width: `${videoEndPct}%` }}
                  >
                    <span className="truncate">
                      Vid 0f–{Math.round(plan.rawVideoDur * 30)}f ({plan.rawVideoDur.toFixed(2)}s)
                    </span>
                  </div>
                  {videoEndPct < 99.9 && (
                    <div
                      className="absolute inset-y-0 right-0 flex items-center overflow-hidden bg-sky-500/15 px-1.5 text-[10px] font-mono text-sky-200"
                      style={{ left: `${videoEndPct}%` }}
                    >
                      <span className="truncate">
                        {plan.isFreezeFrame ? "Freeze" : "Extended"} (+
                        {(totalDur - plan.rawVideoDur).toFixed(2)}s)
                      </span>
                    </div>
                  )}
                </div>

                {/* Track 2: Voiceover Audio */}
                <div className="relative h-5 bg-zinc-900/60">
                  {audioDur > 0 ? (
                    <div
                      className="absolute inset-y-0 left-0 flex items-center overflow-hidden rounded-sm border border-violet-400/40 bg-violet-500/25 px-1.5 text-[10px] font-mono text-violet-200"
                      style={{ width: `${audioEndPct}%` }}
                    >
                      <span className="truncate">
                        TTS 0.00s → {audioDur.toFixed(2)}s
                      </span>
                    </div>
                  ) : selectedSegment.clauses.length > 0 ? (
                    <div className="flex h-full items-center px-2 text-[10px] text-amber-300/80">
                      Pending TTS — click Preview to align audio &amp; captions
                    </div>
                  ) : (
                    <div className="flex h-full items-center px-2 text-[10px] text-zinc-600">
                      Silent (no narration)
                    </div>
                  )}
                </div>

                {/* Track 3: Captions */}
                <div className="relative h-6 bg-zinc-900/60">
                  {captionBlocks.length === 0 ? (
                    <div className="flex h-full items-center px-2 text-[10px] text-zinc-600">
                      No captions
                    </div>
                  ) : (
                    captionBlocks.map((b) => {
                      const leftPct = Math.max(0, Math.min(100, (b.startSec / totalDur) * 100));
                      const widthPct = Math.max(
                        2,
                        Math.min(100 - leftPct, ((b.endSec - b.startSec) / totalDur) * 100)
                      );
                      return (
                        <button
                          key={b.id}
                          type="button"
                          onMouseDown={(e) => e.stopPropagation()}
                          onClick={() =>
                            onSourceTimeChange(
                              selectedSegment.video_in +
                                Math.min(plan.rawVideoDur, b.startSec)
                            )
                          }
                          title={`${b.id}: "${b.text}" (${b.startSec.toFixed(2)}s – ${b.endSec.toFixed(2)}s${b.estimated ? ", estimated" : ""})`}
                          className={`absolute inset-y-0.5 flex items-center gap-1 overflow-hidden rounded px-1.5 text-left text-[10px] transition hover:brightness-125 ${
                            b.estimated
                              ? "border border-dashed border-sky-400/50 bg-sky-500/10 text-sky-200/80"
                              : "border border-sky-400/60 bg-sky-500/25 text-sky-100"
                          }`}
                          style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                        >
                          <span className="shrink-0 font-mono text-[9px] text-sky-300">
                            {b.id}
                          </span>
                          <span className="truncate">{b.text || "—"}</span>
                        </button>
                      );
                    })
                  )}
                </div>

                {/* Track 4: View Window Keyframes */}
                <div className="relative h-5 bg-zinc-900/60">
                  {selectedSegment.crop_keyframes.map((kf, idx) => {
                    const leftPct = Math.max(
                      0,
                      Math.min(100, (kf.t / totalDur) * 100)
                    );
                    const isOut = kf.t > plan.rawVideoDur + 1e-4;
                    return (
                      <button
                        key={`${kf.t}-${idx}`}
                        type="button"
                        onMouseDown={(e) => e.stopPropagation()}
                        onClick={() =>
                          onSourceTimeChange(
                            selectedSegment.video_in +
                              Math.min(plan.rawVideoDur, kf.t)
                          )
                        }
                        title={`Keyframe @${kf.t.toFixed(2)}s (f${Math.round(kf.t * 30)}) • Zoom ${kf.zoom.toFixed(2)}x`}
                        className={`absolute top-0.5 -translate-x-1/2 rounded border px-1 py-0 text-[9px] font-mono ${
                          isOut
                            ? "border-rose-400/70 bg-rose-500/30 text-rose-100"
                            : "border-amber-400/70 bg-amber-500/25 text-amber-100 hover:bg-amber-500/40"
                        }`}
                        style={{
                          left: `${leftPct}%`,
                          transform:
                            leftPct < 4
                              ? "translateX(0)"
                              : leftPct > 96
                                ? "translateX(-100%)"
                                : "translateX(-50%)",
                        }}
                      >
                        ◆ {kf.zoom.toFixed(1)}x
                      </button>
                    );
                  })}
                </div>

                {/* Shared Playhead */}
                <div
                  className="pointer-events-none absolute inset-y-0 z-10 w-0.5 bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]"
                  style={{ left: `${playheadPct}%` }}
                />
              </div>
            </div>
          </div>
        );
      })()}
    </section>
  );
}
