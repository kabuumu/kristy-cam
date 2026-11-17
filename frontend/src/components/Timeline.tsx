import { useEffect, useRef, useState } from "react";
import {
  Captions,
  Film,
  KeyRound,
  Loader2,
  Pause,
  Play,
  Plus,
  Scissors,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import type {
  ArtifactManifest,
  ClauseInput,
  SegmentInput,
  TimelineSelection,
} from "../types/studio";
import {
  computeTimelineLayout,
  evaluateCropAt,
  formatTimecode,
  resolveProjectTime,
} from "../utils/studioMath";

interface TimelineProps {
  segments: SegmentInput[];
  manifest: ArtifactManifest;
  selectedSegId: string | null;
  selection: TimelineSelection;
  tailPaddingSec: number;
  outputWidth: number;
  outputHeight: number;
  projectTime: number;
  sourceTime: number;
  isModalOpen: boolean;
  isProjectPlaying: boolean;
  preparingPlayback?: boolean;
  onSelectSegment: (segId: string) => void;
  onSelectItem: (sel: TimelineSelection) => void;
  onProjectTimeChange: (t: number) => void;
  onSourceTimeChange: (t: number) => void;
  onAddSegment: (videoIn: number, videoOut: number) => void;
  onDeleteSegment: (segId: string) => void;
  onUpdateSegment: (updated: SegmentInput) => void;
  onReorderSegments: (fromIndex: number, toIndex: number) => void;
  onSplitSegment: (segId: string, localTime: number) => void;
  onToggleProjectPlayback: () => void;
}

type DragAction =
  | { type: "scrub" }
  | {
      type: "zoom-drag";
      startX: number;
      startY: number;
      origZoom: number;
      origScrollLeft: number;
      anchorClientX: number;
    }
  | {
      type: "clip-reorder";
      fromIndex: number;
      targetIndex: number;
      startX: number;
    }
  | {
      type: "video-slip" | "video-left" | "video-right";
      segId: string;
      startX: number;
      origIn: number;
      origOut: number;
      totalDur: number;
    }
  | {
      type: "clause-move" | "clause-left" | "clause-right";
      segId: string;
      index: number;
      startX: number;
      origStart: number;
      origDur: number;
      totalDur: number;
    }
  | {
      type: "keyframe-move";
      segId: string;
      index: number;
      startX: number;
      origT: number;
      totalDur: number;
    }
  | null;

const MIN_TIMELINE_ZOOM = 1;
const MAX_TIMELINE_ZOOM = 16;

export function Timeline({
  segments,
  manifest,
  selectedSegId,
  selection,
  tailPaddingSec,
  outputWidth,
  outputHeight,
  projectTime,
  sourceTime,
  isModalOpen,
  isProjectPlaying,
  preparingPlayback = false,
  onSelectSegment,
  onSelectItem,
  onProjectTimeChange,
  onSourceTimeChange,
  onAddSegment,
  onDeleteSegment,
  onUpdateSegment,
  onReorderSegments,
  onSplitSegment,
  onToggleProjectPlayback,
}: TimelineProps) {
  const sourceDur = manifest.source_metadata?.duration ?? 60;
  const srcW = manifest.source_metadata?.width ?? 1920;
  const srcH = manifest.source_metadata?.height ?? 1080;
  const [markIn, setMarkIn] = useState(0);
  const [markOut, setMarkOut] = useState(() => Math.min(3.0, sourceDur));
  const [timelineZoom, setTimelineZoom] = useState(1);
  const [drag, setDrag] = useState<DragAction>(null);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const trackLaneRef = useRef<HTMLDivElement | null>(null);

  const layout = computeTimelineLayout(
    segments,
    manifest,
    sourceDur,
    tailPaddingSec
  );
  const totalDur = Math.max(0.5, layout.totalDuration);
  const resolved = resolveProjectTime(layout, projectTime);

  const activeClip =
    resolved.clip ??
    layout.clips.find((c) => c.seg.id === selectedSegId) ??
    layout.clips[0] ??
    null;

  const canSplit = Boolean(
    resolved.clip &&
      resolved.localTime > 0.15 &&
      resolved.localTime < resolved.clip.plan.rawVideoDur - 0.15
  );

  useEffect(() => {
    setMarkOut((prev) => (prev > sourceDur ? Math.max(0.5, sourceDur) : prev));
  }, [sourceDur]);

  const deleteClauseOnSeg = (segId: string, idx: number) => {
    const seg = segments.find((s) => s.id === segId);
    if (!seg) return;
    const next = seg.clauses.filter((_, i) => i !== idx);
    onUpdateSegment({ ...seg, clauses: next });
    onSelectItem({ type: "video", segId });
  };

  const deleteKeyframeOnSeg = (segId: string, idx: number) => {
    if (idx <= 0) return;
    const seg = segments.find((s) => s.id === segId);
    if (!seg) return;
    const next = seg.crop_keyframes.filter((_, i) => i !== idx);
    onUpdateSegment({ ...seg, crop_keyframes: next });
    onSelectItem({ type: "video", segId });
  };

  const applyZoom = (nextZoomRaw: number, anchorClientX?: number) => {
    const nextZoom = Math.max(
      MIN_TIMELINE_ZOOM,
      Math.min(MAX_TIMELINE_ZOOM, Number(nextZoomRaw.toFixed(2)))
    );
    const scroller = scrollContainerRef.current;
    if (!scroller || scroller.clientWidth <= 0) {
      setTimelineZoom(nextZoom);
      return;
    }
    const rect = scroller.getBoundingClientRect();
    const pointerOffset =
      anchorClientX !== undefined
        ? Math.max(0, Math.min(rect.width, anchorClientX - rect.left))
        : rect.width / 2;
    const contentX = scroller.scrollLeft + pointerOffset;
    const frac = contentX / Math.max(1, rect.width * timelineZoom);

    setTimelineZoom(nextZoom);
    requestAnimationFrame(() => {
      if (!scrollContainerRef.current) return;
      const w = scrollContainerRef.current.clientWidth;
      const maxScroll = Math.max(0, w * nextZoom - w);
      const targetScroll = frac * (w * nextZoom) - pointerOffset;
      scrollContainerRef.current.scrollLeft = Math.max(
        0,
        Math.min(maxScroll, targetScroll)
      );
    });
  };

  // Global keyboard shortcuts: I (In), O (Out), S (Split at Playhead), Backspace/Delete
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
      } else if (e.key === "s" || e.key === "S") {
        if (canSplit && resolved.clip) {
          e.preventDefault();
          onSplitSegment(resolved.clip.seg.id, resolved.localTime);
        }
      } else if (e.key === "Backspace" || e.key === "Delete") {
        if (!selection && !selectedSegId) return;
        e.preventDefault();
        if (selection?.type === "clause") {
          deleteClauseOnSeg(selection.segId, selection.index);
        } else if (selection?.type === "keyframe") {
          if (selection.index > 0) {
            deleteKeyframeOnSeg(selection.segId, selection.index);
          }
        } else if (selection?.type === "video") {
          onDeleteSegment(selection.segId);
        } else if (selectedSegId) {
          onDeleteSegment(selectedSegId);
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    isModalOpen,
    sourceTime,
    sourceDur,
    markIn,
    markOut,
    selection,
    selectedSegId,
    canSplit,
    resolved,
  ]);

  const timeFromClientX = (clientX: number): number | null => {
    const rect = trackLaneRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return null;
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return ratio * totalDur;
  };

  const scrubAtClientX = (clientX: number) => {
    const t = timeFromClientX(clientX);
    if (t === null) return;
    onProjectTimeChange(Math.max(0, Math.min(layout.totalDuration, t)));
  };

  useEffect(() => {
    if (!drag) return;

    const handleMove = (e: MouseEvent) => {
      if (drag.type === "zoom-drag") {
        const dy = e.clientY - drag.startY;
        const dx = e.clientX - drag.startX;
        const nextZoom = Math.max(
          MIN_TIMELINE_ZOOM,
          Math.min(
            MAX_TIMELINE_ZOOM,
            Number((drag.origZoom * Math.exp(dy * 0.012)).toFixed(2))
          )
        );
        const scroller = scrollContainerRef.current;
        if (scroller && scroller.clientWidth > 0) {
          const rect = scroller.getBoundingClientRect();
          const pointerOffset = Math.max(
            0,
            Math.min(rect.width, drag.anchorClientX - rect.left)
          );
          const contentX = drag.origScrollLeft + pointerOffset;
          const frac = contentX / Math.max(1, rect.width * drag.origZoom);
          setTimelineZoom(nextZoom);
          requestAnimationFrame(() => {
            if (!scrollContainerRef.current) return;
            const w = scrollContainerRef.current.clientWidth;
            const maxScroll = Math.max(0, w * nextZoom - w);
            const panOffset = -dx;
            const targetScroll =
              frac * (w * nextZoom) - pointerOffset + panOffset;
            scrollContainerRef.current.scrollLeft = Math.max(
              0,
              Math.min(maxScroll, targetScroll)
            );
          });
        } else {
          setTimelineZoom(nextZoom);
        }
        return;
      }

      if (drag.type === "scrub") {
        scrubAtClientX(e.clientX);
        return;
      }

      if (drag.type === "clip-reorder") {
        const t = timeFromClientX(e.clientX);
        if (t === null || layout.clips.length === 0) return;
        let targetIndex = layout.clips.length;
        for (const c of layout.clips) {
          const mid = (c.startSec + c.endSec) / 2;
          if (t < mid) {
            targetIndex = c.index;
            break;
          }
        }
        if (targetIndex !== drag.targetIndex) {
          setDrag({ ...drag, targetIndex });
        }
        return;
      }

      const rect = trackLaneRef.current?.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;
      const seg = segments.find((s) => s.id === drag.segId);
      if (!seg) return;

      const dxSec = ((e.clientX - drag.startX) / rect.width) * drag.totalDur;

      if (drag.type === "video-slip") {
        const len = drag.origOut - drag.origIn;
        const nextIn = Math.max(0, Math.min(sourceDur - len, drag.origIn + dxSec));
        onUpdateSegment({
          ...seg,
          video_in: Number(nextIn.toFixed(2)),
          video_out: Number((nextIn + len).toFixed(2)),
        });
      } else if (drag.type === "video-left") {
        const nextIn = Math.max(
          0,
          Math.min(drag.origOut - 0.2, drag.origIn + dxSec)
        );
        onUpdateSegment({
          ...seg,
          video_in: Number(nextIn.toFixed(2)),
        });
      } else if (drag.type === "video-right") {
        const nextOut = Math.max(
          drag.origIn + 0.2,
          Math.min(sourceDur, drag.origOut + dxSec)
        );
        onUpdateSegment({
          ...seg,
          video_out: Number(nextOut.toFixed(2)),
        });
      } else if (
        drag.type === "clause-move" ||
        drag.type === "clause-left" ||
        drag.type === "clause-right"
      ) {
        const nextClauses = seg.clauses.map((c, idx) => {
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
            const nextStart = Math.max(
              0,
              Math.min(maxStart, drag.origStart + dxSec)
            );
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
        onUpdateSegment({ ...seg, clauses: nextClauses });
      } else if (drag.type === "keyframe-move" && drag.index > 0) {
        const rawDur = Math.max(0.2, seg.video_out - seg.video_in);
        const nextT = Math.max(0.05, Math.min(rawDur, drag.origT + dxSec));
        const nextKfs = seg.crop_keyframes
          .map((kf, idx) =>
            idx === drag.index ? { ...kf, t: Number(nextT.toFixed(2)) } : kf
          )
          .sort((a, b) => a.t - b.t);
        onUpdateSegment({ ...seg, crop_keyframes: nextKfs });
      }
    };

    const handleUp = () => {
      if (drag.type === "clip-reorder") {
        const { fromIndex, targetIndex } = drag;
        const adjustedTarget =
          targetIndex > fromIndex ? targetIndex - 1 : targetIndex;
        if (adjustedTarget !== fromIndex && adjustedTarget >= 0) {
          onReorderSegments(fromIndex, adjustedTarget);
        }
      }
      setDrag(null);
    };

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

  const createClauseOnClip = (seg: SegmentInput, startSec: number) => {
    const existing = new Set(seg.clauses.map((c) => c.id));
    let n = seg.clauses.length + 1;
    while (existing.has(`c${n}`)) n += 1;
    const id = `c${n}`;
    const newClause: ClauseInput = {
      id,
      display_text: "",
      spoken_text_override: null,
      start_sec: Number(Math.max(0, startSec).toFixed(2)),
      duration_sec: 1.5,
    };
    const nextIdx = seg.clauses.length;
    onUpdateSegment({
      ...seg,
      clauses: [...seg.clauses, newClause],
    });
    onSelectSegment(seg.id);
    onSelectItem({ type: "clause", segId: seg.id, index: nextIdx });
  };

  const handleAddClauseButton = () => {
    if (!activeClip) return;
    const seg = activeClip.seg;
    const segArt = manifest.segments[seg.id];
    const lastTiming = segArt?.clause_timings?.[segArt.clause_timings.length - 1];
    const lastClause = seg.clauses[seg.clauses.length - 1];
    const defaultStart = lastClause
      ? (lastClause.start_sec ?? lastTiming?.start_sec ?? 0) +
        (lastClause.duration_sec ??
          (lastTiming ? lastTiming.end_sec - lastTiming.start_sec : 1.5))
      : 0;
    createClauseOnClip(seg, defaultStart);
  };

  const findClipAtGlobalTime = (globalT: number) => {
    if (layout.clips.length === 0) return null;
    for (const clip of layout.clips) {
      if (globalT <= clip.endSec + 1e-6) return clip;
    }
    return layout.clips[layout.clips.length - 1];
  };

  const handleDoubleClickCameraTrack = (clientX: number) => {
    const clickedT = timeFromClientX(clientX);
    if (clickedT === null) return;
    const targetClip = findClipAtGlobalTime(clickedT);
    if (!targetClip) return;

    const rawVideoDur = Math.max(0.2, targetClip.plan.rawVideoDur);
    const localT = Number(
      Math.max(0.05, Math.min(rawVideoDur, clickedT - targetClip.startSec)).toFixed(2)
    );
    const existingIdx = targetClip.seg.crop_keyframes.findIndex(
      (kf) => Math.abs(kf.t - localT) < 0.05
    );
    if (existingIdx >= 0) {
      onSelectSegment(targetClip.seg.id);
      onSelectItem({
        type: "keyframe",
        segId: targetClip.seg.id,
        index: existingIdx,
      });
      onProjectTimeChange(targetClip.startSec + targetClip.seg.crop_keyframes[existingIdx].t);
      return;
    }

    const evalLocalT =
      resolved.clip?.seg.id === targetClip.seg.id ? resolved.localTime : localT;
    const lastViewedLens = evaluateCropAt(
      targetClip.seg.crop_keyframes,
      evalLocalT,
      srcW,
      srcH,
      outputWidth,
      outputHeight
    );
    const newKf = {
      t: localT,
      x: Number(lastViewedLens.x.toFixed(4)),
      y: Number(lastViewedLens.y.toFixed(4)),
      zoom: Number(lastViewedLens.zoom.toFixed(3)),
      transition: "tween" as const,
    };
    const nextKfs = [...targetClip.seg.crop_keyframes, newKf].sort(
      (a, b) => a.t - b.t
    );
    const newIndex = nextKfs.findIndex((kf) => kf === newKf);
    onUpdateSegment({ ...targetClip.seg, crop_keyframes: nextKfs });
    onSelectSegment(targetClip.seg.id);
    onSelectItem({
      type: "keyframe",
      segId: targetClip.seg.id,
      index: Math.max(0, newIndex),
    });
    onProjectTimeChange(targetClip.startSec + localT);
  };

  const handleDoubleClickClauseTrack = (clientX: number) => {
    const clickedT = timeFromClientX(clientX);
    if (clickedT === null) return;
    const targetClip = findClipAtGlobalTime(clickedT);
    if (!targetClip) return;

    const localT = Number(
      Math.max(
        0,
        Math.min(
          Math.max(0, targetClip.plan.finalDur - 0.2),
          clickedT - targetClip.startSec
        )
      ).toFixed(2)
    );
    createClauseOnClip(targetClip.seg, localT);
    onProjectTimeChange(targetClip.startSec + localT);
  };

  const totalFrames = Math.max(1, Math.round(totalDur * 30));
  const currentFrame = Math.min(totalFrames, Math.round(resolved.projectTime * 30));
  const playheadPct = Math.max(
    0,
    Math.min(100, (resolved.projectTime / totalDur) * 100)
  );
  const tickCount = Math.max(5, Math.min(40, Math.round(5 * timelineZoom)));
  const ticks = Array.from({ length: tickCount + 1 }, (_, i) => {
    const frac = i / tickCount;
    const t = frac * totalDur;
    return { frac, t, f: Math.round(t * 30) };
  });

  const dropIndicatorLeftPct =
    drag?.type === "clip-reorder"
      ? drag.targetIndex >= layout.clips.length
        ? 100
        : (layout.clips[drag.targetIndex].startSec / totalDur) * 100
      : null;

  return (
    <section className="flex shrink-0 flex-col rounded-xl border border-zinc-800 bg-[#151821]">
      {/* Top Timeline Controls */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 px-3 py-2 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onToggleProjectPlayback}
            disabled={segments.length === 0 || preparingPlayback}
            className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-2.5 py-1 font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
          >
            {preparingPlayback ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Preparing Audio...
              </>
            ) : isProjectPlaying ? (
              <>
                <Pause className="h-3.5 w-3.5" /> Stop
              </>
            ) : (
              <>
                <Play className="h-3.5 w-3.5" /> Play Project
              </>
            )}
          </button>

          {segments.length > 0 && (
            <>
              <button
                type="button"
                disabled={!canSplit}
                onClick={() => {
                  if (canSplit && resolved.clip) {
                    onSplitSegment(resolved.clip.seg.id, resolved.localTime);
                  }
                }}
                title="Split clip at playhead (S)"
                className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-[11px] font-medium text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
              >
                <Scissors className="h-3 w-3" />
                Split (S)
              </button>

              <button
                type="button"
                onClick={handleAddClauseButton}
                className="inline-flex items-center gap-1 rounded border border-sky-500/40 bg-sky-500/15 px-2 py-1 text-[11px] font-medium text-sky-200 hover:bg-sky-500/25"
              >
                <Plus className="h-3 w-3" />
                Add Narration / Caption
              </button>
            </>
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
              if (markOut <= c)
                setMarkOut(Number(Math.min(sourceDur, c + 2.0).toFixed(2)));
            }}
            className="rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 font-mono text-[11px] text-zinc-300 hover:bg-zinc-700"
            title="Set In (I)"
          >
            I:{markIn.toFixed(1)}s
          </button>
          <button
            type="button"
            onClick={() =>
              setMarkOut(
                Number(
                  Math.max(markIn + 0.1, Math.min(sourceTime, sourceDur)).toFixed(2)
                )
              )
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

      {/* Unified Multi-Track NLE Timeline */}
      {segments.length === 0 ? (
        <div className="p-5 text-center text-xs text-zinc-400">
          Scrub source video, mark I / O, and click <strong>+ Add Clip</strong> to place your first clip on the timeline.
        </div>
      ) : (
        <div className="bg-[#11141b] px-3 py-2 space-y-1.5">
          <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
            <div className="flex items-center gap-2 font-mono text-zinc-400">
              <span>
                {formatTimecode(resolved.projectTime)} / {formatTimecode(layout.totalDuration)} •{" "}
                <span className="text-emerald-300">
                  f{currentFrame} / {totalFrames}f
                </span>{" "}
                (30fps)
              </span>
              {resolved.clip && (
                <span className="rounded bg-zinc-800/80 px-1.5 py-0.5 text-[10px] text-zinc-300">
                  Clip #{resolved.clip.index + 1} ({layout.clips.length} total)
                </span>
              )}
            </div>

            <div className="flex items-center gap-3">
              <span className="hidden lg:inline text-[10px] text-zinc-500">
                Drag clip to reorder (Alt+Drag to slip) • Double-click Camera/Narration to add • S to split • Del to delete
              </span>
              <div className="flex items-center gap-1 rounded border border-zinc-800 bg-zinc-900/80 px-1 py-0.5">
                <button
                  type="button"
                  aria-label="Zoom Out Timeline"
                  title="Zoom Out Timeline"
                  disabled={timelineZoom <= MIN_TIMELINE_ZOOM}
                  onClick={() => applyZoom(timelineZoom / 1.5)}
                  className="rounded p-0.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-35"
                >
                  <ZoomOut className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => applyZoom(1)}
                  title="Reset Timeline Zoom (1.0x)"
                  className="min-w-[36px] rounded px-1 font-mono text-[10px] text-zinc-300 hover:bg-zinc-800"
                >
                  {timelineZoom.toFixed(1)}x
                </button>
                <button
                  type="button"
                  aria-label="Zoom In Timeline"
                  title="Zoom In Timeline"
                  disabled={timelineZoom >= MAX_TIMELINE_ZOOM}
                  onClick={() => applyZoom(timelineZoom * 1.5)}
                  className="rounded p-0.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-35"
                >
                  <ZoomIn className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>

          <div className="flex gap-2 select-none">
            {/* Track Labels */}
            <div className="w-24 shrink-0 space-y-1.5 pt-9 text-[10px] font-medium text-zinc-400">
              <div className="flex h-7 items-center gap-1.5">
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
            <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded border border-zinc-800 bg-[#0b0d11]">
              {/* DAW-style Top Zoom Strip */}
              <div
                role="slider"
                aria-label="Timeline Zoom Bar"
                aria-valuemin={MIN_TIMELINE_ZOOM}
                aria-valuemax={MAX_TIMELINE_ZOOM}
                aria-valuenow={timelineZoom}
                title="DAW Zoom: Drag down to zoom in, up to zoom out, left/right to pan"
                onMouseDown={(e) => {
                  e.preventDefault();
                  setDrag({
                    type: "zoom-drag",
                    startX: e.clientX,
                    startY: e.clientY,
                    origZoom: timelineZoom,
                    origScrollLeft: scrollContainerRef.current?.scrollLeft ?? 0,
                    anchorClientX: e.clientX,
                  });
                }}
                onDoubleClick={() => applyZoom(1)}
                className="relative flex h-3.5 cursor-ns-resize items-center justify-center border-b border-zinc-800/80 bg-zinc-900/80 text-[9px] font-mono text-zinc-500 hover:bg-zinc-800/80 hover:text-zinc-300"
              >
                <span>↕ Drag to Zoom ({timelineZoom.toFixed(1)}x)</span>
              </div>

              <div ref={scrollContainerRef} className="overflow-x-auto">
                <div
                  ref={trackLaneRef}
                  className="relative space-y-1.5 py-1"
                  style={{ width: `${timelineZoom * 100}%`, minWidth: "100%" }}
                >
                  {/* Scrubbable Global Ruler */}
                  <div
                    data-log-intent="Scrub timeline ruler"
                    onMouseDown={(e) => {
                      setDrag({ type: "scrub" });
                      scrubAtClientX(e.clientX);
                    }}
                    className="relative h-4 cursor-pointer border-b border-zinc-800/80 text-[9px] font-mono text-zinc-500"
                  >
                    {ticks.map((tk, idx) => (
                      <div
                        key={idx}
                        className="absolute top-0 whitespace-nowrap"
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
                        {tk.t.toFixed(timelineZoom >= 3 ? 2 : 1)}s ({tk.f}f)
                      </div>
                    ))}
                  </div>

                  {/* Clip Boundary Vertical Dividers across tracks */}
                  {layout.clips.slice(1).map((c) => (
                    <div
                      key={`div-${c.seg.id}`}
                      className="pointer-events-none absolute top-5 bottom-1 z-[5] w-px bg-zinc-700/60"
                      style={{ left: `${(c.startSec / totalDur) * 100}%` }}
                    />
                  ))}

                  {/* Track 1: Sequential Video Clips */}
                  <div className="relative h-7 bg-zinc-900/50">
                    {layout.clips.map((clip) => {
                      const leftPct = (clip.startSec / totalDur) * 100;
                      const widthPct = (clip.plan.finalDur / totalDur) * 100;
                      const movingPctOfClip =
                        clip.plan.finalDur > 0
                          ? Math.min(100, (clip.plan.rawVideoDur / clip.plan.finalDur) * 100)
                          : 100;
                      const isSelected =
                        (selection?.type === "video" && selection.segId === clip.seg.id) ||
                        (!selection && selectedSegId === clip.seg.id);
                      const isActiveClip = selectedSegId === clip.seg.id;

                      return (
                        <div
                          key={clip.seg.id}
                          data-log-intent={`Select/move clip #${clip.index + 1} (${clip.seg.video_in.toFixed(1)}s–${clip.seg.video_out.toFixed(1)}s)`}
                          onMouseDown={(e) => {
                            e.stopPropagation();
                            onSelectSegment(clip.seg.id);
                            onSelectItem({ type: "video", segId: clip.seg.id });
                            if (e.altKey) {
                              setDrag({
                                type: "video-slip",
                                segId: clip.seg.id,
                                startX: e.clientX,
                                origIn: clip.seg.video_in,
                                origOut: clip.seg.video_out,
                                totalDur,
                              });
                            } else {
                              setDrag({
                                type: "clip-reorder",
                                fromIndex: clip.index,
                                targetIndex: clip.index,
                                startX: e.clientX,
                              });
                            }
                          }}
                          title={`Clip #${clip.index + 1} (${clip.seg.video_in.toFixed(1)}s → ${clip.seg.video_out.toFixed(1)}s) • Drag to reorder • Alt+Drag to slip`}
                          className={`group absolute inset-y-0 flex cursor-grab active:cursor-grabbing items-center justify-between overflow-hidden rounded-sm border text-[10px] font-mono ${
                            isSelected
                              ? "border-emerald-400 bg-emerald-500/25 text-emerald-100 ring-1 ring-emerald-400/50"
                              : isActiveClip
                                ? "border-emerald-500/60 bg-emerald-500/20 text-emerald-100"
                                : "border-emerald-500/35 bg-emerald-500/12 text-emerald-200/90 hover:bg-emerald-500/20"
                          }`}
                          style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                        >
                          {/* Freeze/Extension tail shading inside clip */}
                          {movingPctOfClip < 99.5 && (
                            <div
                              className="pointer-events-none absolute inset-y-0 right-0 bg-sky-500/15 border-l border-sky-400/40"
                              style={{ left: `${movingPctOfClip}%` }}
                            />
                          )}

                          {/* Left Trim Handle */}
                          <div
                            data-log-intent="Trim video clip start"
                            onMouseDown={(e) => {
                              e.stopPropagation();
                              onSelectSegment(clip.seg.id);
                              onSelectItem({ type: "video", segId: clip.seg.id });
                              setDrag({
                                type: "video-left",
                                segId: clip.seg.id,
                                startX: e.clientX,
                                origIn: clip.seg.video_in,
                                origOut: clip.seg.video_out,
                                totalDur,
                              });
                            }}
                            className="relative z-10 h-full w-2 shrink-0 cursor-ew-resize hover:bg-emerald-300/40"
                            title="Drag to trim clip start"
                          />

                          <span className="relative z-10 min-w-0 flex-1 truncate px-1">
                            #{clip.index + 1} • {clip.seg.video_in.toFixed(1)}–{clip.seg.video_out.toFixed(1)}s
                            {movingPctOfClip < 99.5
                              ? ` (+${(clip.plan.finalDur - clip.plan.rawVideoDur).toFixed(1)}s)`
                              : ""}
                          </span>

                          <button
                            type="button"
                            onMouseDown={(e) => e.stopPropagation()}
                            onClick={(e) => {
                              e.stopPropagation();
                              onDeleteSegment(clip.seg.id);
                            }}
                            title={`Delete clip #${clip.index + 1} (Backspace/Delete)`}
                            aria-label={`Delete clip #${clip.index + 1}`}
                            className="relative z-10 mr-0.5 shrink-0 rounded p-0.5 text-emerald-200/70 opacity-0 transition group-hover:opacity-100 hover:text-rose-300"
                          >
                            <X className="h-3 w-3" />
                          </button>

                          {/* Right Trim Handle */}
                          <div
                            data-log-intent="Trim/extend video clip end"
                            onMouseDown={(e) => {
                              e.stopPropagation();
                              onSelectSegment(clip.seg.id);
                              onSelectItem({ type: "video", segId: clip.seg.id });
                              setDrag({
                                type: "video-right",
                                segId: clip.seg.id,
                                startX: e.clientX,
                                origIn: clip.seg.video_in,
                                origOut: clip.seg.video_out,
                                totalDur,
                              });
                            }}
                            className="relative z-10 h-full w-2 shrink-0 cursor-ew-resize hover:bg-emerald-300/40"
                            title="Drag to extend/trim clip end"
                          />
                        </div>
                      );
                    })}

                    {/* Drop indicator during clip reorder */}
                    {dropIndicatorLeftPct !== null && (
                      <div
                        className="pointer-events-none absolute inset-y-0 z-20 w-1 bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.9)]"
                        style={{
                          left: `${dropIndicatorLeftPct}%`,
                          transform: "translateX(-50%)",
                        }}
                      />
                    )}
                  </div>

                  {/* Track 2: Camera / View Window Keyframes across all clips */}
                  <div
                    data-testid="camera-track"
                    onDoubleClick={(e) => handleDoubleClickCameraTrack(e.clientX)}
                    title="Double-click to add a camera/lens keyframe here"
                    className="relative h-6 bg-zinc-900/50"
                  >
                    {layout.clips.flatMap((clip) =>
                      clip.seg.crop_keyframes.map((kf, idx) => {
                        const globalT = clip.startSec + kf.t;
                        const leftPct = Math.max(
                          0,
                          Math.min(100, (globalT / totalDur) * 100)
                        );
                        const isSelected =
                          selection?.type === "keyframe" &&
                          selection.segId === clip.seg.id &&
                          selection.index === idx;
                        const modeLabel =
                          (kf.transition ?? "tween") === "snap" ? "S" : "T";
                        return (
                          <div
                            key={`${clip.seg.id}-kf-${idx}-${kf.t}`}
                            data-log-intent={`Select keyframe #${idx + 1} on ${clip.seg.id} @${kf.t.toFixed(2)}s`}
                            onDoubleClick={(e) => e.stopPropagation()}
                            onMouseDown={(e) => {
                              e.stopPropagation();
                              onSelectSegment(clip.seg.id);
                              onSelectItem({
                                type: "keyframe",
                                segId: clip.seg.id,
                                index: idx,
                              });
                              onProjectTimeChange(globalT);
                              if (idx > 0) {
                                setDrag({
                                  type: "keyframe-move",
                                  segId: clip.seg.id,
                                  index: idx,
                                  startX: e.clientX,
                                  origT: kf.t,
                                  totalDur,
                                });
                              }
                            }}
                            title={`Clip #${clip.index + 1} Keyframe #${idx + 1} @${kf.t.toFixed(2)}s • Zoom ${kf.zoom.toFixed(2)}x • ${(kf.transition ?? "tween").toUpperCase()}`}
                            className={`group absolute top-0.5 inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-mono ${
                              idx > 0
                                ? "cursor-grab active:cursor-grabbing"
                                : "cursor-pointer"
                            } ${
                              isSelected
                                ? "z-10 border-amber-300 bg-amber-500/40 text-white ring-1 ring-amber-300"
                                : "border-amber-500/60 bg-amber-500/20 text-amber-200 hover:bg-amber-500/30"
                            }`}
                            style={{
                              left: `${leftPct}%`,
                              transform:
                                leftPct < 3
                                  ? "translateX(0)"
                                  : leftPct > 97
                                    ? "translateX(-100%)"
                                    : "translateX(-50%)",
                            }}
                          >
                            <span>
                              ◆ {kf.zoom.toFixed(1)}x [{modeLabel}]
                            </span>
                            {idx > 0 && (
                              <button
                                type="button"
                                onMouseDown={(e) => e.stopPropagation()}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  deleteKeyframeOnSeg(clip.seg.id, idx);
                                }}
                                title="Delete keyframe (Backspace/Delete)"
                                aria-label={`Delete keyframe #${idx + 1}`}
                                className="rounded text-amber-200/70 opacity-0 transition group-hover:opacity-100 hover:text-rose-300"
                              >
                                <X className="h-2.5 w-2.5" />
                              </button>
                            )}
                          </div>
                        );
                      })
                    )}
                  </div>

                  {/* Track 3: Narration & Caption Blocks across all clips */}
                  <div
                    data-testid="narration-track"
                    onDoubleClick={(e) => handleDoubleClickClauseTrack(e.clientX)}
                    title="Double-click to add an empty caption/narration block here"
                    className="relative h-7 bg-zinc-900/50"
                  >
                    {layout.clips.every((c) => c.seg.clauses.length === 0) ? (
                      <div className="pointer-events-none flex h-full items-center px-2 text-[10px] text-zinc-600">
                        Double-click track or click &ldquo;+ Add Narration / Caption&rdquo;
                      </div>
                    ) : (
                      layout.clips.flatMap((clip) => {
                        const segArt = manifest.segments[clip.seg.id];
                        const timings = segArt?.clause_timings ?? [];
                        const timingById = new Map(
                          timings.map((t) => [t.clause_id, t])
                        );
                        return clip.seg.clauses.map((clause, i) => {
                          const t = timingById.get(clause.id);
                          const localStart =
                            clause.start_sec ??
                            t?.start_sec ??
                            i *
                              (clip.plan.rawVideoDur /
                                Math.max(1, clip.seg.clauses.length));
                          const durSec =
                            clause.duration_sec ??
                            (t
                              ? Math.max(0.2, t.end_sec - t.start_sec)
                              : Math.max(
                                  0.5,
                                  clip.plan.rawVideoDur /
                                    Math.max(1, clip.seg.clauses.length)
                                ));
                          const globalStart = clip.startSec + localStart;
                          const leftPct = Math.max(
                            0,
                            Math.min(100, (globalStart / totalDur) * 100)
                          );
                          const widthPct = Math.max(
                            3,
                            Math.min(100 - leftPct, (durSec / totalDur) * 100)
                          );
                          const isSelected =
                            selection?.type === "clause" &&
                            selection.segId === clip.seg.id &&
                            selection.index === i;

                          return (
                            <div
                              key={`${clip.seg.id}-${clause.id}`}
                              data-log-intent={`Select caption block ${clause.id} on ${clip.seg.id}`}
                              onDoubleClick={(e) => e.stopPropagation()}
                              onMouseDown={(e) => {
                                e.stopPropagation();
                                onSelectSegment(clip.seg.id);
                                onSelectItem({
                                  type: "clause",
                                  segId: clip.seg.id,
                                  index: i,
                                });
                                onProjectTimeChange(globalStart);
                                setDrag({
                                  type: "clause-move",
                                  segId: clip.seg.id,
                                  index: i,
                                  startX: e.clientX,
                                  origStart: localStart,
                                  origDur: durSec,
                                  totalDur,
                                });
                              }}
                              title={`Clip #${clip.index + 1} • ${clause.id}: "${clause.display_text}" (${localStart.toFixed(2)}s, len ${durSec.toFixed(2)}s)`}
                              className={`group absolute inset-y-0.5 flex cursor-grab active:cursor-grabbing items-center gap-1 overflow-hidden rounded border px-2 text-[10px] ${
                                isSelected
                                  ? "z-10 border-sky-300 bg-sky-500/35 text-white ring-1 ring-sky-300"
                                  : "border-sky-500/50 bg-sky-500/20 text-sky-100 hover:bg-sky-500/30"
                              }`}
                              style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                            >
                              <div
                                onMouseDown={(e) => {
                                  e.stopPropagation();
                                  onSelectSegment(clip.seg.id);
                                  onSelectItem({
                                    type: "clause",
                                    segId: clip.seg.id,
                                    index: i,
                                  });
                                  setDrag({
                                    type: "clause-left",
                                    segId: clip.seg.id,
                                    index: i,
                                    startX: e.clientX,
                                    origStart: localStart,
                                    origDur: durSec,
                                    totalDur,
                                  });
                                }}
                                className="absolute inset-y-0 left-0 w-2 cursor-ew-resize hover:bg-sky-300/40"
                                title="Drag to adjust start"
                              />
                              <span className="shrink-0 font-mono text-[9px] text-sky-300">
                                {clause.id}
                              </span>
                              <span className="min-w-0 flex-1 truncate">
                                {clause.display_text || "Empty caption..."}
                              </span>
                              <button
                                type="button"
                                onMouseDown={(e) => e.stopPropagation()}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  deleteClauseOnSeg(clip.seg.id, i);
                                }}
                                title={`Delete ${clause.id} (Backspace/Delete)`}
                                aria-label={`Delete caption block ${clause.id}`}
                                className="relative z-10 shrink-0 rounded text-sky-200/70 opacity-0 transition group-hover:opacity-100 hover:text-rose-300"
                              >
                                <X className="h-3 w-3" />
                              </button>
                              <div
                                onMouseDown={(e) => {
                                  e.stopPropagation();
                                  onSelectSegment(clip.seg.id);
                                  onSelectItem({
                                    type: "clause",
                                    segId: clip.seg.id,
                                    index: i,
                                  });
                                  setDrag({
                                    type: "clause-right",
                                    segId: clip.seg.id,
                                    index: i,
                                    startX: e.clientX,
                                    origStart: localStart,
                                    origDur: durSec,
                                    totalDur,
                                  });
                                }}
                                className="absolute inset-y-0 right-0 w-2 cursor-ew-resize hover:bg-sky-300/40"
                                title="Drag to extend/shorten length"
                              />
                            </div>
                          );
                        });
                      })
                    )}
                  </div>

                  {/* Shared Global Playhead */}
                  <div
                    className="pointer-events-none absolute inset-y-0 z-20 w-0.5 bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]"
                    style={{ left: `${playheadPct}%` }}
                  />
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
