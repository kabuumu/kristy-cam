import { useEffect, useRef, useState } from "react";
import {
  Captions,
  Film,
  KeyRound,
  Pause,
  Play,
  Plus,
} from "lucide-react";
import type {
  ArtifactManifest,
  ClauseInput,
  SegmentInput,
  TimelineSelection,
} from "../types/studio";
import { computeDurationPlan, formatTimecode } from "../utils/studioMath";

interface TimelineProps {
  segments: SegmentInput[];
  manifest: ArtifactManifest;
  selectedSegId: string | null;
  selection: TimelineSelection;
  tailPaddingSec: number;
  sourceTime: number;
  segTime?: number | null;
  isModalOpen: boolean;
  isProjectPlaying: boolean;
  onSelectSegment: (segId: string) => void;
  onSelectItem: (sel: TimelineSelection) => void;
  onSourceTimeChange: (t: number) => void;
  onSegTimeChange?: (t: number) => void;
  onAddSegment: (videoIn: number, videoOut: number) => void;
  onUpdateSegment: (updated: SegmentInput) => void;
  onToggleProjectPlayback: () => void;
}

type DragAction =
  | { type: "scrub" }
  | {
      type: "video-move" | "video-left" | "video-right";
      startX: number;
      origIn: number;
      origOut: number;
      totalDur: number;
    }
  | {
      type: "clause-move" | "clause-left" | "clause-right";
      index: number;
      startX: number;
      origStart: number;
      origDur: number;
      totalDur: number;
    }
  | {
      type: "keyframe-move";
      index: number;
      startX: number;
      origT: number;
      totalDur: number;
    }
  | null;

export function Timeline({
  segments,
  manifest,
  selectedSegId,
  selection,
  tailPaddingSec,
  sourceTime,
  segTime = null,
  isModalOpen,
  isProjectPlaying,
  onSelectSegment,
  onSelectItem,
  onSourceTimeChange,
  onSegTimeChange,
  onAddSegment,
  onUpdateSegment,
  onToggleProjectPlayback,
}: TimelineProps) {
  const sourceDur = manifest.source_metadata?.duration ?? 60;
  const [markIn, setMarkIn] = useState(0);
  const [markOut, setMarkOut] = useState(() => Math.min(3.0, sourceDur));
  const [drag, setDrag] = useState<DragAction>(null);
  const trackLaneRef = useRef<HTMLDivElement | null>(null);

  const selectedSegment =
    segments.find((s) => s.id === selectedSegId) ?? null;

  useEffect(() => {
    setMarkOut((prev) => (prev > sourceDur ? Math.max(0.5, sourceDur) : prev));
  }, [sourceDur]);

  // Global keyboard shortcuts I (Set In) and O (Set Out)
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

  const scrubAtClientX = (clientX: number) => {
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
    const nextSegTime = Math.max(0, Math.min(totalDur, ratio * totalDur));
    if (onSegTimeChange) {
      onSegTimeChange(nextSegTime);
    } else {
      onSourceTimeChange(
        selectedSegment.video_in + Math.min(plan.movingSourceDur, nextSegTime)
      );
    }
  };

  useEffect(() => {
    if (!drag || !selectedSegment) return;

    const handleMove = (e: MouseEvent) => {
      const rect = trackLaneRef.current?.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;

      if (drag.type === "scrub") {
        scrubAtClientX(e.clientX);
        return;
      }

      const dxSec = ((e.clientX - drag.startX) / rect.width) * drag.totalDur;

      if (drag.type === "video-move") {
        const len = drag.origOut - drag.origIn;
        const nextIn = Math.max(0, Math.min(sourceDur - len, drag.origIn + dxSec));
        onUpdateSegment({
          ...selectedSegment,
          video_in: Number(nextIn.toFixed(2)),
          video_out: Number((nextIn + len).toFixed(2)),
        });
      } else if (drag.type === "video-left") {
        const nextIn = Math.max(0, Math.min(drag.origOut - 0.2, drag.origIn + dxSec));
        onUpdateSegment({
          ...selectedSegment,
          video_in: Number(nextIn.toFixed(2)),
        });
      } else if (drag.type === "video-right") {
        const nextOut = Math.max(
          drag.origIn + 0.2,
          Math.min(sourceDur, drag.origOut + dxSec)
        );
        onUpdateSegment({
          ...selectedSegment,
          video_out: Number(nextOut.toFixed(2)),
        });
      } else if (
        drag.type === "clause-move" ||
        drag.type === "clause-left" ||
        drag.type === "clause-right"
      ) {
        const nextClauses = selectedSegment.clauses.map((c, idx) => {
          if (idx !== drag.index) return c;
          if (drag.type === "clause-move") {
            const nextStart = Math.max(0, drag.origStart + dxSec);
            return {
              ...c,
              start_sec: Number(nextStart.toFixed(2)),
              duration_sec: Number(drag.origDur.toFixed(2)),
            };
          }
          if (drag.type === "clause-left") {
            const maxStart = drag.origStart + drag.origDur - 0.2;
            const nextStart = Math.max(0, Math.min(maxStart, drag.origStart + dxSec));
            const nextDur = drag.origStart + drag.origDur - nextStart;
            return {
              ...c,
              start_sec: Number(nextStart.toFixed(2)),
              duration_sec: Number(nextDur.toFixed(2)),
            };
          }
          const nextDur = Math.max(0.2, drag.origDur + dxSec);
          return {
            ...c,
            start_sec: Number(drag.origStart.toFixed(2)),
            duration_sec: Number(nextDur.toFixed(2)),
          };
        });
        onUpdateSegment({ ...selectedSegment, clauses: nextClauses });
      } else if (drag.type === "keyframe-move" && drag.index > 0) {
        const rawDur = Math.max(0.2, selectedSegment.video_out - selectedSegment.video_in);
        const nextT = Math.max(0.05, Math.min(rawDur, drag.origT + dxSec));
        const nextKfs = selectedSegment.crop_keyframes
          .map((kf, idx) =>
            idx === drag.index ? { ...kf, t: Number(nextT.toFixed(2)) } : kf
          )
          .sort((a, b) => a.t - b.t);
        onUpdateSegment({ ...selectedSegment, crop_keyframes: nextKfs });
      }
    };

    const handleUp = () => setDrag(null);
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
    };
  });

  const handleAddClip = () => {
    const safeIn = Math.max(0, Math.min(markIn, sourceDur - 0.1));
    const safeOut = Math.max(safeIn + 0.1, Math.min(markOut, sourceDur));
    onAddSegment(Number(safeIn.toFixed(2)), Number(safeOut.toFixed(2)));
  };

  const handleAddClauseBlock = () => {
    if (!selectedSegment) return;
    const existing = new Set(selectedSegment.clauses.map((c) => c.id));
    let n = selectedSegment.clauses.length + 1;
    while (existing.has(`c${n}`)) n += 1;
    const id = `c${n}`;
    const segArt = manifest.segments[selectedSegment.id];
    const lastTiming = segArt?.clause_timings?.[segArt.clause_timings.length - 1];
    const lastClause = selectedSegment.clauses[selectedSegment.clauses.length - 1];
    const defaultStart = lastClause
      ? (lastClause.start_sec ?? lastTiming?.start_sec ?? 0) +
        (lastClause.duration_sec ??
          (lastTiming ? lastTiming.end_sec - lastTiming.start_sec : 1.5))
      : 0;
    const newClause: ClauseInput = {
      id,
      display_text: "",
      spoken_text_override: null,
      start_sec: Number(defaultStart.toFixed(2)),
      duration_sec: 1.5,
    };
    const nextIdx = selectedSegment.clauses.length;
    onUpdateSegment({
      ...selectedSegment,
      clauses: [...selectedSegment.clauses, newClause],
    });
    onSelectItem({ type: "clause", segId: selectedSegment.id, index: nextIdx });
  };

  return (
    <section className="flex shrink-0 flex-col rounded-xl border border-zinc-800 bg-[#151821]">
      {/* Top Timeline Controls */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 px-3 py-2 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onToggleProjectPlayback}
            disabled={segments.length === 0}
            className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-2.5 py-1 font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
          >
            {isProjectPlaying ? (
              <>
                <Pause className="h-3.5 w-3.5" /> Stop
              </>
            ) : (
              <>
                <Play className="h-3.5 w-3.5" /> Play Project
              </>
            )}
          </button>

          {/* Compact Clip Tabs */}
          <div className="flex items-center gap-1">
            {segments.map((seg, idx) => (
              <button
                key={seg.id}
                type="button"
                onClick={() => {
                  onSelectSegment(seg.id);
                  onSelectItem({ type: "video", segId: seg.id });
                }}
                className={`rounded border px-2 py-0.5 font-mono text-[11px] transition ${
                  seg.id === selectedSegId
                    ? "border-emerald-500 bg-emerald-500/20 text-emerald-200"
                    : "border-zinc-700 bg-zinc-900 text-zinc-400 hover:text-zinc-200"
                }`}
              >
                #{idx + 1} ({(seg.video_out - seg.video_in).toFixed(1)}s)
              </button>
            ))}
          </div>

          {selectedSegment && (
            <button
              type="button"
              onClick={handleAddClauseBlock}
              className="inline-flex items-center gap-1 rounded border border-sky-500/40 bg-sky-500/15 px-2 py-1 text-[11px] font-medium text-sky-200 hover:bg-sky-500/25"
            >
              <Plus className="h-3 w-3" />
              Add Narration / Caption
            </button>
          )}
        </div>

        {/* Source Scrubber + Add Clip */}
        <div className="flex items-center gap-2">
          <span className="font-mono text-[11px] text-zinc-400">
            Src {formatTimecode(sourceTime)} / {formatTimecode(sourceDur)}
          </span>
          <input
            type="range"
            aria-label="Source Capture Scrubber"
            min={0}
            max={Math.max(0.1, sourceDur)}
            step={0.033}
            value={Math.min(sourceTime, sourceDur)}
            onChange={(e) => onSourceTimeChange(parseFloat(e.target.value))}
            className="w-28 sm:w-40"
          />
          <button
            type="button"
            onClick={() => {
              const c = Math.max(0, Math.min(sourceTime, sourceDur - 0.1));
              setMarkIn(Number(c.toFixed(2)));
              if (markOut <= c) setMarkOut(Number(Math.min(sourceDur, c + 2.0).toFixed(2)));
            }}
            className="rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 font-mono text-[11px] text-zinc-300 hover:bg-zinc-700"
            title="Set In (I)"
          >
            I:{markIn.toFixed(1)}s
          </button>
          <button
            type="button"
            onClick={() =>
              setMarkOut(Number(Math.max(markIn + 0.1, Math.min(sourceTime, sourceDur)).toFixed(2)))
            }
            className="rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 font-mono text-[11px] text-zinc-300 hover:bg-zinc-700"
            title="Set Out (O)"
          >
            O:{markOut.toFixed(1)}s
          </button>
          <button
            type="button"
            onClick={handleAddClip}
            className="inline-flex items-center gap-1 rounded border border-emerald-500/40 bg-emerald-500/15 px-2 py-1 text-[11px] font-medium text-emerald-200 hover:bg-emerald-500/25"
          >
            <Plus className="h-3 w-3" />
            Add Clip
          </button>
        </div>
      </div>

      {/* Unified Multi-Track Editor */}
      {!selectedSegment ? (
        <div className="p-5 text-center text-xs text-zinc-400">
          Scrub source video, mark I / O, and click <strong>+ Add Clip</strong> to start editing.
        </div>
      ) : (() => {
        const segArt = manifest.segments[selectedSegment.id];
        const audioDur = segArt?.audio_duration ?? 0;
        const plan = computeDurationPlan(
          selectedSegment,
          audioDur,
          sourceDur,
          tailPaddingSec
        );
        const totalDur = Math.max(0.5, plan.finalDur);
        const totalFrames = plan.frameCount;
        const segRelTime = Math.max(
          0,
          Math.min(
            totalDur,
            segTime != null ? segTime : sourceTime - selectedSegment.video_in
          )
        );
        const currentFrame = Math.min(totalFrames, Math.round(segRelTime * 30));
        const playheadPct = Math.max(0, Math.min(100, (segRelTime / totalDur) * 100));
        const videoEndPct = Math.max(0, Math.min(100, (plan.rawVideoDur / totalDur) * 100));

        const timings = segArt?.clause_timings ?? [];
        const timingById = new Map(timings.map((t) => [t.clause_id, t]));

        const captionBlocks = selectedSegment.clauses.map((clause, i) => {
          const t = timingById.get(clause.id);
          const startSec = clause.start_sec ?? t?.start_sec ?? i * (plan.rawVideoDur / Math.max(1, selectedSegment.clauses.length));
          const durSec =
            clause.duration_sec ??
            (t
              ? Math.max(0.2, t.end_sec - t.start_sec)
              : Math.max(0.5, plan.rawVideoDur / Math.max(1, selectedSegment.clauses.length)));
          return {
            index: i,
            id: clause.id,
            text: clause.display_text,
            startSec,
            durSec,
            synced: Boolean(t) && clause.start_sec == null,
          };
        });

        const ticks = Array.from({ length: 6 }, (_, i) => {
          const frac = i / 5;
          const t = frac * totalDur;
          return { frac, t, f: Math.round(t * 30) };
        });

        return (
          <div className="bg-[#11141b] px-3 py-2 space-y-1.5">
            <div className="flex items-center justify-between text-[11px]">
              <span className="font-mono text-zinc-400">
                {segRelTime.toFixed(2)}s / {totalDur.toFixed(2)}s •{" "}
                <span className="text-emerald-300">
                  f{currentFrame} / {totalFrames}f
                </span>{" "}
                (30fps)
              </span>
              <span className="text-[10px] text-zinc-500">
                Drag center to move • Drag edges to extend/trim • Click item to inspect
              </span>
            </div>

            <div className="flex gap-2 select-none">
              {/* Track Labels */}
              <div className="w-24 shrink-0 space-y-1.5 pt-5 text-[10px] font-medium text-zinc-400">
                <div className="flex h-6 items-center gap-1.5">
                  <Film className="h-3.5 w-3.5 text-emerald-400" />
                  <span>Video</span>
                </div>
                <div className="flex h-6 items-center gap-1.5">
                  <KeyRound className="h-3.5 w-3.5 text-amber-400" />
                  <span>Camera</span>
                </div>
                <div className="flex h-7 items-center gap-1.5">
                  <Captions className="h-3.5 w-3.5 text-sky-400" />
                  <span>Narration</span>
                </div>
              </div>

              {/* Shared Interactive Track Area */}
              <div
                ref={trackLaneRef}
                className="relative flex-1 space-y-1.5 overflow-hidden rounded border border-zinc-800 bg-[#0b0d11] py-1"
              >
                {/* Scrubbable Ruler */}
                <div
                  onMouseDown={(e) => {
                    setDrag({ type: "scrub" });
                    scrubAtClientX(e.clientX);
                  }}
                  className="relative h-4 cursor-pointer border-b border-zinc-800/80 text-[9px] font-mono text-zinc-500"
                >
                  {ticks.map((tk, idx) => (
                    <div
                      key={idx}
                      className="absolute top-0"
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
                      {tk.t.toFixed(1)}s ({tk.f}f)
                    </div>
                  ))}
                </div>

                {/* Track 1: Video Clip (center = move, edges = resize) */}
                <div className="relative h-6 bg-zinc-900/50">
                  <div
                    onMouseDown={(e) => {
                      e.stopPropagation();
                      onSelectItem({ type: "video", segId: selectedSegment.id });
                      setDrag({
                        type: "video-move",
                        startX: e.clientX,
                        origIn: selectedSegment.video_in,
                        origOut: selectedSegment.video_out,
                        totalDur,
                      });
                    }}
                    className={`absolute inset-y-0 left-0 flex cursor-grab active:cursor-grabbing items-center justify-between overflow-hidden rounded-sm border px-2 text-[10px] font-mono ${
                      selection?.type === "video"
                        ? "border-emerald-400 bg-emerald-500/25 text-emerald-100"
                        : "border-emerald-500/40 bg-emerald-500/15 text-emerald-200"
                    }`}
                    style={{ width: `${videoEndPct}%` }}
                  >
                    <div
                      onMouseDown={(e) => {
                        e.stopPropagation();
                        onSelectItem({ type: "video", segId: selectedSegment.id });
                        setDrag({
                          type: "video-left",
                          startX: e.clientX,
                          origIn: selectedSegment.video_in,
                          origOut: selectedSegment.video_out,
                          totalDur,
                        });
                      }}
                      className="absolute inset-y-0 left-0 w-2 cursor-ew-resize hover:bg-emerald-300/40"
                      title="Drag to trim clip start"
                    />
                    <span className="truncate pl-1">
                      {selectedSegment.video_in.toFixed(1)}s → {selectedSegment.video_out.toFixed(1)}s (
                      {Math.round(plan.rawVideoDur * 30)}f)
                    </span>
                    <div
                      onMouseDown={(e) => {
                        e.stopPropagation();
                        onSelectItem({ type: "video", segId: selectedSegment.id });
                        setDrag({
                          type: "video-right",
                          startX: e.clientX,
                          origIn: selectedSegment.video_in,
                          origOut: selectedSegment.video_out,
                          totalDur,
                        });
                      }}
                      className="absolute inset-y-0 right-0 w-2 cursor-ew-resize hover:bg-emerald-300/40"
                      title="Drag to extend/trim clip end"
                    />
                  </div>
                  {videoEndPct < 99.9 && (
                    <div
                      className="absolute inset-y-0 right-0 flex items-center px-2 text-[10px] font-mono text-sky-300/80 bg-sky-500/10"
                      style={{ left: `${videoEndPct}%` }}
                    >
                      {plan.isFreezeFrame ? "Freeze" : "Ext"} +{(totalDur - plan.rawVideoDur).toFixed(1)}s
                    </div>
                  )}
                </div>

                {/* Track 2: Camera / View Window Keyframes */}
                <div className="relative h-6 bg-zinc-900/50">
                  {selectedSegment.crop_keyframes.map((kf, idx) => {
                    const leftPct = Math.max(0, Math.min(100, (kf.t / totalDur) * 100));
                    const isSelected =
                      selection?.type === "keyframe" && selection.index === idx;
                    const modeLabel = (kf.transition ?? "tween") === "snap" ? "S" : "T";
                    return (
                      <button
                        key={`${kf.t}-${idx}`}
                        type="button"
                        onMouseDown={(e) => {
                          e.stopPropagation();
                          onSelectItem({
                            type: "keyframe",
                            segId: selectedSegment.id,
                            index: idx,
                          });
                          onSourceTimeChange(
                            selectedSegment.video_in + Math.min(plan.rawVideoDur, kf.t)
                          );
                          if (idx > 0) {
                            setDrag({
                              type: "keyframe-move",
                              index: idx,
                              startX: e.clientX,
                              origT: kf.t,
                              totalDur,
                            });
                          }
                        }}
                        title={`Keyframe #${idx + 1} @${kf.t.toFixed(2)}s • Zoom ${kf.zoom.toFixed(2)}x • ${(kf.transition ?? "tween").toUpperCase()}`}
                        className={`absolute top-0.5 rounded border px-1.5 py-0.5 text-[10px] font-mono ${
                          idx > 0 ? "cursor-grab active:cursor-grabbing" : "cursor-pointer"
                        } ${
                          isSelected
                            ? "border-amber-300 bg-amber-500/40 text-white ring-1 ring-amber-300"
                            : "border-amber-500/60 bg-amber-500/20 text-amber-200 hover:bg-amber-500/30"
                        }`}
                        style={{
                          left: `${leftPct}%`,
                          transform:
                            leftPct < 5
                              ? "translateX(0)"
                              : leftPct > 95
                                ? "translateX(-100%)"
                                : "translateX(-50%)",
                        }}
                      >
                        ◆ {kf.zoom.toFixed(1)}x [{modeLabel}]
                      </button>
                    );
                  })}
                </div>

                {/* Track 3: Grouped Narration & Caption Blocks */}
                <div className="relative h-7 bg-zinc-900/50">
                  {captionBlocks.length === 0 ? (
                    <div className="flex h-full items-center px-2 text-[10px] text-zinc-600">
                      No narration/caption blocks — click &ldquo;+ Add Narration / Caption&rdquo;
                    </div>
                  ) : (
                    captionBlocks.map((b) => {
                      const leftPct = Math.max(0, Math.min(100, (b.startSec / totalDur) * 100));
                      const widthPct = Math.max(
                        4,
                        Math.min(100 - leftPct, (b.durSec / totalDur) * 100)
                      );
                      const isSelected =
                        selection?.type === "clause" && selection.index === b.index;
                      return (
                        <div
                          key={b.id}
                          onMouseDown={(e) => {
                            e.stopPropagation();
                            onSelectItem({
                              type: "clause",
                              segId: selectedSegment.id,
                              index: b.index,
                            });
                            onSourceTimeChange(
                              selectedSegment.video_in +
                                Math.min(plan.rawVideoDur, b.startSec)
                            );
                            setDrag({
                              type: "clause-move",
                              index: b.index,
                              startX: e.clientX,
                              origStart: b.startSec,
                              origDur: b.durSec,
                              totalDur,
                            });
                          }}
                          title={`${b.id}: "${b.text}" (${b.startSec.toFixed(2)}s, len ${b.durSec.toFixed(2)}s)`}
                          className={`absolute inset-y-0.5 flex cursor-grab active:cursor-grabbing items-center gap-1 overflow-hidden rounded border px-2 text-[10px] ${
                            isSelected
                              ? "border-sky-300 bg-sky-500/35 text-white ring-1 ring-sky-300"
                              : "border-sky-500/50 bg-sky-500/20 text-sky-100 hover:bg-sky-500/30"
                          }`}
                          style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                        >
                          <div
                            onMouseDown={(e) => {
                              e.stopPropagation();
                              onSelectItem({
                                type: "clause",
                                segId: selectedSegment.id,
                                index: b.index,
                              });
                              setDrag({
                                type: "clause-left",
                                index: b.index,
                                startX: e.clientX,
                                origStart: b.startSec,
                                origDur: b.durSec,
                                totalDur,
                              });
                            }}
                            className="absolute inset-y-0 left-0 w-2 cursor-ew-resize hover:bg-sky-300/40"
                            title="Drag to adjust start"
                          />
                          <span className="shrink-0 font-mono text-[9px] text-sky-300">
                            {b.id}
                          </span>
                          <span className="truncate">{b.text || "Empty caption..."}</span>
                          <div
                            onMouseDown={(e) => {
                              e.stopPropagation();
                              onSelectItem({
                                type: "clause",
                                segId: selectedSegment.id,
                                index: b.index,
                              });
                              setDrag({
                                type: "clause-right",
                                index: b.index,
                                startX: e.clientX,
                                origStart: b.startSec,
                                origDur: b.durSec,
                                totalDur,
                              });
                            }}
                            className="absolute inset-y-0 right-0 w-2 cursor-ew-resize hover:bg-sky-300/40"
                            title="Drag to extend/shorten length"
                          />
                        </div>
                      );
                    })
                  )}
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
