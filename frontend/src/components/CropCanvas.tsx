import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Eye,
  EyeOff,
  Film,
  Grid,
  Loader2,
  Pause,
  Play,
  Plus,
  Sparkles,
  X,
} from "lucide-react";
import { apiClient } from "../api/client";
import type {
  ArtifactManifest,
  CaptionSettings,
  CropKeyframe,
  SegmentInput,
} from "../types/studio";
import {
  clampCropPosition,
  computeDurationPlan,
  computeIdealCrop,
  evaluateCropAt,
  formatAspectRatio,
  formatTimecode,
} from "../utils/studioMath";
import { usageLogger } from "../utils/usageLogger";

interface CropCanvasProps {
  projectId: string;
  segment: SegmentInput | null;
  manifest: ArtifactManifest;
  captionSettings: CaptionSettings;
  outputWidth: number;
  outputHeight: number;
  tailPaddingSec: number;
  sourceTime: number;
  segTime?: number | null;
  projectTime?: number;
  cameraKeyframes?: CropKeyframe[];
  isPlaying: boolean;
  isProjectPlaying?: boolean;
  preparingPlayback?: boolean;
  onTogglePlay: () => void;
  onSourceTimeChange: (t: number) => void;
  onUpdateSegment: (updated: SegmentInput) => void;
  onUpdateCameraKeyframes?: (keyframes: CropKeyframe[]) => void;
  onBeginHistoryTransaction?: (label: string) => void;
  onEndHistoryTransaction?: () => void;
  onRenderPreview: () => void;
  canPreview?: boolean;
  previewBusy: boolean;
  previewModalOpen: boolean;
  onSetPreviewModalOpen: (open: boolean) => void;
  monitorMode?: "timeline" | "source";
}

type DragMode =
  | { type: "move"; startClientX: number; startClientY: number; origX: number; origY: number }
  | { type: "zoom"; corner: string; startClientX: number; startClientY: number; origZoom: number; origX: number; origY: number }
  | null;

export function CropCanvas({
  projectId,
  segment,
  manifest,
  captionSettings,
  outputWidth,
  outputHeight,
  tailPaddingSec,
  sourceTime,
  segTime = null,
  projectTime = 0,
  cameraKeyframes,
  isPlaying,
  isProjectPlaying = false,
  preparingPlayback = false,
  onTogglePlay,
  onSourceTimeChange,
  onUpdateSegment,
  onUpdateCameraKeyframes,
  onBeginHistoryTransaction,
  onEndHistoryTransaction,
  onRenderPreview,
  canPreview = false,
  previewBusy,
  previewModalOpen,
  onSetPreviewModalOpen,
  monitorMode = "timeline",
}: CropCanvasProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const voiceAudioRef = useRef<HTMLAudioElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);

  // Playback diagnostic refs (rate-limited so logs stay concise)
  const playbackStartedAtRef = useRef<number>(0);
  const prevPlayingRef = useRef<boolean>(false);
  const prevFrozenRef = useRef<boolean>(false);
  const prevSegIdRef = useRef<string | null>(null);
  const prevPlayheadRef = useRef<{ segId: string | null; segRelTime: number; sourceTime: number }>({
    segId: null,
    segRelTime: 0,
    sourceTime: 0,
  });
  const lastResyncLogAtRef = useRef<{ video: number; audio: number; jump: number }>({
    video: 0,
    audio: 0,
    jump: 0,
  });
  const playbackCountersRef = useRef<{
    videoResyncs: number;
    audioResyncs: number;
    jumpBacks: number;
    freezeTransitions: number;
  }>({ videoResyncs: 0, audioResyncs: 0, jumpBacks: 0, freezeTransitions: 0 });

  const [showMask, setShowMask] = useState(true);
  const [showSafeGuide, setShowSafeGuide] = useState(true);
  const [dragMode, setDragMode] = useState<DragMode>(null);

  const srcW = manifest.source_metadata?.width ?? 1920;
  const srcH = manifest.source_metadata?.height ?? 1080;
  const srcDur = manifest.source_metadata?.duration ?? 0;
  const aspectLabel = formatAspectRatio(outputWidth, outputHeight);

  const proxyReady =
    manifest.proxy.status === "ready" && Boolean(manifest.proxy.artifact_id);
  const proxyUrl = proxyReady
    ? apiClient.mediaUrl(projectId, manifest.proxy.artifact_id!)
    : null;

  const segArtifacts = segment ? manifest.segments[segment.id] : undefined;
  const audioDur = segArtifacts?.audio_duration ?? 0;
  const voiceAudioUrl = segArtifacts?.audio_artifact_id
    ? `${apiClient.mediaUrl(projectId, segArtifacts.audio_artifact_id)}?v=${segArtifacts.tts_hash ?? ""}`
    : null;
  const durationPlan = segment
    ? computeDurationPlan(segment, audioDur, srcDur, tailPaddingSec)
    : null;

  const rawVideoDur = segment
    ? Math.max(0.01, segment.video_out - segment.video_in)
    : 1;

  const segRelTime = segment
    ? Math.max(
        0,
        Math.min(
          durationPlan?.finalDur ?? rawVideoDur,
          segTime != null ? segTime : sourceTime - segment.video_in
        )
      )
    : 0;

  const isVideoFrozen = Boolean(
    segment &&
      durationPlan &&
      isProjectPlaying &&
      segRelTime >= durationPlan.movingSourceDur - 0.02
  );

  // Out-of-range keyframes detected after trim-shift
  const outOfRangeKeyframes = segment
    ? segment.crop_keyframes.filter((kf) => kf.t > rawVideoDur + 1e-4)
    : [];

  // Log playback start/stop, freeze transitions, and unexpected backward jumps
  useEffect(() => {
    const segId = segment?.id ?? null;
    const now = performance.now();

    if (isPlaying && !prevPlayingRef.current) {
      playbackStartedAtRef.current = now;
      prevFrozenRef.current = isVideoFrozen;
      prevSegIdRef.current = segId;
      prevPlayheadRef.current = { segId, segRelTime, sourceTime };
      playbackCountersRef.current = {
        videoResyncs: 0,
        audioResyncs: 0,
        jumpBacks: 0,
        freezeTransitions: 0,
      };
      usageLogger.logIntention(
        isProjectPlaying ? "Start project playback" : "Start source playback",
        "playback.start",
        {
          projectId,
          segId,
          input: {
            mode: isProjectPlaying ? "project" : "source",
            seg_rel_time: Number(segRelTime.toFixed(3)),
            source_time: Number(sourceTime.toFixed(3)),
            moving_source_dur: durationPlan
              ? Number(durationPlan.movingSourceDur.toFixed(3))
              : null,
            final_dur: durationPlan ? Number(durationPlan.finalDur.toFixed(3)) : null,
            is_freeze_frame: durationPlan?.isFreezeFrame ?? false,
            is_video_frozen: isVideoFrozen,
          },
        }
      );
    } else if (!isPlaying && prevPlayingRef.current) {
      usageLogger.logActionOutput(
        "Stop playback",
        "playback.stop",
        {
          seg_rel_time: Number(segRelTime.toFixed(3)),
          source_time: Number(sourceTime.toFixed(3)),
          ...playbackCountersRef.current,
        },
        {
          projectId,
          segId,
          durationMs: Math.round(now - playbackStartedAtRef.current),
        }
      );
      prevFrozenRef.current = false;
    }
    prevPlayingRef.current = isPlaying;

    if (isPlaying) {
      if (segId !== prevSegIdRef.current) {
        playbackStartedAtRef.current = now;
        prevSegIdRef.current = segId;
        prevFrozenRef.current = isVideoFrozen;
        prevPlayheadRef.current = { segId, segRelTime, sourceTime };
      } else {
        if (isVideoFrozen !== prevFrozenRef.current) {
          playbackCountersRef.current.freezeTransitions += 1;
          usageLogger.logActionOutput(
            isVideoFrozen ? "Enter freeze-frame window" : "Exit freeze-frame window",
            isVideoFrozen ? "playback.freeze_enter" : "playback.freeze_exit",
            {
              seg_rel_time: Number(segRelTime.toFixed(3)),
              source_time: Number(sourceTime.toFixed(3)),
              moving_source_dur: durationPlan
                ? Number(durationPlan.movingSourceDur.toFixed(3))
                : null,
            },
            { projectId, segId }
          );
          prevFrozenRef.current = isVideoFrozen;
        }

        if (isProjectPlaying) {
          const prev = prevPlayheadRef.current;
          const segJumpedBack = segRelTime < prev.segRelTime - 0.05;
          const srcJumpedBack = sourceTime < prev.sourceTime - 0.05;
          if ((segJumpedBack || srcJumpedBack) && now - lastResyncLogAtRef.current.jump > 500) {
            lastResyncLogAtRef.current.jump = now;
            playbackCountersRef.current.jumpBacks += 1;
            usageLogger.logError(
              "Unexpected backward playhead jump during playback",
              "playback.jump_back",
              new Error("Playhead moved backward during active project playback"),
              {
                projectId,
                segId,
                context: {
                  prev_seg_rel_time: Number(prev.segRelTime.toFixed(3)),
                  next_seg_rel_time: Number(segRelTime.toFixed(3)),
                  prev_source_time: Number(prev.sourceTime.toFixed(3)),
                  next_source_time: Number(sourceTime.toFixed(3)),
                  is_video_frozen: isVideoFrozen,
                },
              }
            );
          }
          prevPlayheadRef.current = { segId, segRelTime, sourceTime };
        }
      }
    }
  }, [isPlaying, isProjectPlaying, isVideoFrozen, segRelTime, sourceTime, segment, durationPlan, projectId]);

  // Sync <video> playback, volume, & currentTime
  useEffect(() => {
    const vid = videoRef.current;
    if (!vid || !proxyUrl) return;
    const targetVol = segment ? Math.max(0, Math.min(1, segment.source_audio_volume)) : 1;
    vid.volume = targetVol;
    vid.muted = targetVol <= 0;

    if (isPlaying && !isVideoFrozen) {
      const drift = Math.abs(vid.currentTime - sourceTime);
      if (drift > 0.35) {
        const now = performance.now();
        if (
          now - playbackStartedAtRef.current > 250 &&
          now - lastResyncLogAtRef.current.video > 500
        ) {
          lastResyncLogAtRef.current.video = now;
          playbackCountersRef.current.videoResyncs += 1;
          usageLogger.logActionOutput(
            "Resync video element during playback",
            "playback.resync",
            {
              media: "video",
              actual_time: Number(vid.currentTime.toFixed(3)),
              expected_time: Number(sourceTime.toFixed(3)),
              seg_rel_time: Number(segRelTime.toFixed(3)),
              is_video_frozen: isVideoFrozen,
            },
            { projectId, segId: segment?.id ?? null }
          );
        }
        vid.currentTime = Math.max(0, sourceTime);
      }
      if (vid.paused) {
        vid.play().catch(() => {});
      }
    } else {
      if (!vid.paused) {
        vid.pause();
      }
      if (Math.abs(vid.currentTime - sourceTime) > 0.08) {
        vid.currentTime = Math.max(0, sourceTime);
      }
    }
  }, [isPlaying, isVideoFrozen, sourceTime, proxyUrl, segment, segRelTime, projectId]);

  // Sync synthesized voiceover <audio> playback with segment playhead
  useEffect(() => {
    const aud = voiceAudioRef.current;
    if (!aud || !voiceAudioUrl) return;
    const withinAudio = audioDur > 0 && segRelTime < audioDur - 0.02;

    if (isPlaying && withinAudio) {
      if (aud.readyState >= 1) {
        const drift = Math.abs(aud.currentTime - segRelTime);
        if (drift > 0.25) {
          const now = performance.now();
          if (
            now - playbackStartedAtRef.current > 250 &&
            now - lastResyncLogAtRef.current.audio > 500
          ) {
            lastResyncLogAtRef.current.audio = now;
            playbackCountersRef.current.audioResyncs += 1;
            usageLogger.logActionOutput(
              "Resync voiceover audio during playback",
              "playback.resync",
              {
                media: "audio",
                actual_time: Number(aud.currentTime.toFixed(3)),
                expected_time: Number(segRelTime.toFixed(3)),
                is_video_frozen: isVideoFrozen,
              },
              { projectId, segId: segment?.id ?? null }
            );
          }
          aud.currentTime = Math.max(0, segRelTime);
        }
      }
      if (aud.paused) {
        aud.play().catch(() => {});
      }
    } else {
      if (!aud.paused) {
        aud.pause();
      }
      if (!isPlaying && audioDur > 0 && aud.readyState >= 1) {
        const clamped = Math.max(0, Math.min(audioDur, segRelTime));
        if (Math.abs(aud.currentTime - clamped) > 0.08) {
          aud.currentTime = clamped;
        }
      }
    }
  }, [isPlaying, segRelTime, voiceAudioUrl, audioDur, isVideoFrozen, projectId, segment]);

  const useProjectCamera = Boolean(
    cameraKeyframes && cameraKeyframes.length > 0 && onUpdateCameraKeyframes
  );
  const activeKeyframes = useProjectCamera
    ? cameraKeyframes!
    : segment?.crop_keyframes ?? [];
  const activeKeyframeTime = useProjectCamera ? projectTime : segRelTime;

  // Evaluated crop at current project/segment time
  const currentCrop =
    activeKeyframes.length > 0
      ? evaluateCropAt(
          activeKeyframes,
          activeKeyframeTime,
          srcW,
          srcH,
          outputWidth,
          outputHeight
        )
      : clampCropPosition(0.28, 0, 1.0, srcW, srcH, outputWidth, outputHeight);

  const ideal = computeIdealCrop(srcW, srcH, currentCrop.zoom, outputWidth, outputHeight);
  const boxLeftPct = (currentCrop.x * 100);
  const boxTopPct = (currentCrop.y * 100);
  const boxWidthPct = (ideal.idealCropW / srcW) * 100;
  const boxHeightPct = (ideal.idealCropH / srcH) * 100;

  const upsertKeyframeAt = (
    t: number,
    x: number,
    y: number,
    zoom: number
  ) => {
    if (!useProjectCamera && !segment) return;
    const clamped = clampCropPosition(x, y, zoom, srcW, srcH, outputWidth, outputHeight);
    const roundedT = Number(Math.max(0, t).toFixed(3));
    const existingIdx = activeKeyframes.findIndex(
      (kf) => Math.abs(kf.t - roundedT) < 0.05
    );
    let nextKfs: CropKeyframe[];
    if (existingIdx >= 0) {
      nextKfs = activeKeyframes.map((kf, idx) =>
        idx === existingIdx
          ? {
              ...kf,
              t: idx === 0 ? 0 : kf.t,
              x: Number(clamped.x.toFixed(4)),
              y: Number(clamped.y.toFixed(4)),
              zoom: Number(clamped.zoom.toFixed(3)),
            }
          : kf
      );
    } else {
      nextKfs = [
        ...activeKeyframes,
        {
          t: roundedT,
          x: Number(clamped.x.toFixed(4)),
          y: Number(clamped.y.toFixed(4)),
          zoom: Number(clamped.zoom.toFixed(3)),
          transition: "tween" as const,
        },
      ].sort((a, b) => a.t - b.t);
    }
    if (useProjectCamera && onUpdateCameraKeyframes) {
      onUpdateCameraKeyframes(nextKfs);
    } else if (segment) {
      onUpdateSegment({ ...segment, crop_keyframes: nextKfs });
    }
  };

  // Dragging handlers for bounding box & corner zoom handles
  useEffect(() => {
    if (!dragMode || (!useProjectCamera && !segment)) return;

    const handleMouseMove = (e: MouseEvent) => {
      const rect = stageRef.current?.getBoundingClientRect();
      if (!rect || rect.width <= 0 || rect.height <= 0) return;

      if (dragMode.type === "move") {
        const dxNorm = (e.clientX - dragMode.startClientX) / rect.width;
        const dyNorm = (e.clientY - dragMode.startClientY) / rect.height;
        upsertKeyframeAt(
          activeKeyframeTime,
          dragMode.origX + dxNorm,
          dragMode.origY + dyNorm,
          currentCrop.zoom
        );
      } else if (dragMode.type === "zoom") {
        const dx = e.clientX - dragMode.startClientX;
        const dy = e.clientY - dragMode.startClientY;
        const delta = (dragMode.corner.includes("r") ? dx : -dx) + (dragMode.corner.includes("b") ? dy : -dy);
        const nextZoom = Math.min(3.0, Math.max(1.0, dragMode.origZoom - delta * 0.005));
        upsertKeyframeAt(
          activeKeyframeTime,
          dragMode.origX,
          dragMode.origY,
          nextZoom
        );
      }
    };

    const handleMouseUp = () => {
      setDragMode(null);
      onEndHistoryTransaction?.();
    };

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  });

  const handleClampScaleKeyframes = () => {
    if (!segment) return;
    const maxExisting = Math.max(...segment.crop_keyframes.map((k) => k.t), rawVideoDur);
    const scale = maxExisting > 0 ? rawVideoDur / maxExisting : 1;
    const scaled: CropKeyframe[] = [];
    for (let i = 0; i < segment.crop_keyframes.length; i++) {
      const kf = segment.crop_keyframes[i];
      const nextT = i === 0 ? 0 : Math.min(rawVideoDur, Number((kf.t * scale).toFixed(3)));
      if (scaled.length === 0 || nextT > scaled[scaled.length - 1].t) {
        scaled.push({ ...kf, t: nextT });
      }
    }
    onUpdateSegment({ ...segment, crop_keyframes: scaled });
  };

  const handleDeleteOutOfRangeKeyframes = () => {
    if (!segment) return;
    const kept = segment.crop_keyframes.filter((kf) => kf.t <= rawVideoDur + 1e-4);
    onUpdateSegment({
      ...segment,
      crop_keyframes: kept.length > 0 ? kept : [{ t: 0, x: 0.28, y: 0, zoom: 1.0 }],
    });
  };

  const previewArtifactId = manifest.latest_preview_artifact_id ?? null;

  return (
    <section className="flex flex-1 flex-col overflow-hidden rounded-xl border border-zinc-800 bg-[#151821]">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 px-3 py-2">
        <div className="flex items-center gap-2">
          <Film className="h-4 w-4 text-emerald-400" />
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-200">
            Crop &amp; Preview Stage
          </h2>
          {monitorMode === "source" ? (
            <span className="rounded border border-sky-500/40 bg-sky-500/15 px-2 py-0.5 font-mono text-[11px] text-sky-200">
              Source Monitor • {formatTimecode(sourceTime)}
            </span>
          ) : segment ? (
            <span className="rounded bg-zinc-800 px-2 py-0.5 font-mono text-[11px] text-zinc-300">
              {segment.id} • Zoom {currentCrop.zoom.toFixed(2)}x
            </span>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setShowMask((v) => !v)}
            className={`inline-flex items-center gap-1 rounded border px-2 py-1 text-xs ${
              showMask
                ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-200"
                : "border-zinc-700 bg-zinc-800/60 text-zinc-300"
            }`}
            title={`Toggle ${aspectLabel} crop mask`}
          >
            {showMask ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
            {aspectLabel} Mask
          </button>

          <button
            type="button"
            onClick={() => setShowSafeGuide((v) => !v)}
            className={`inline-flex items-center gap-1 rounded border px-2 py-1 text-xs ${
              showSafeGuide
                ? "border-sky-500/40 bg-sky-500/15 text-sky-200"
                : "border-zinc-700 bg-zinc-800/60 text-zinc-300"
            }`}
            title={`Toggle ${outputWidth}x${outputHeight} caption safe-area guide`}
          >
            <Grid className="h-3.5 w-3.5" />
            Safe Area
          </button>

          {(canPreview || segment) && (
            <button
              type="button"
              disabled={previewBusy || !canPreview}
              onClick={() => onRenderPreview()}
              className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {previewBusy ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Sparkles className="h-3.5 w-3.5" />
              )}
              Preview
            </button>
          )}
        </div>
      </div>

      {/* Trim-shift out-of-range keyframe warning banner */}
      {!useProjectCamera && segment && outOfRangeKeyframes.length > 0 && (
        <div
          className="flex flex-wrap items-center justify-between gap-2 border-b border-amber-500/40 bg-amber-500/15 px-3 py-2 text-xs text-amber-200"
          role="alert"
        >
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0 text-amber-400" />
            <span>
              {outOfRangeKeyframes.length} keyframe(s) exceed segment duration (
              {rawVideoDur.toFixed(2)}s). Choose how to resolve:
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleClampScaleKeyframes}
              className="rounded border border-amber-400/50 bg-amber-500/20 px-2 py-1 font-medium text-amber-100 hover:bg-amber-500/30"
            >
              Clamp/Scale Keyframes
            </button>
            <button
              type="button"
              onClick={handleDeleteOutOfRangeKeyframes}
              className="rounded border border-rose-400/50 bg-rose-500/20 px-2 py-1 font-medium text-rose-100 hover:bg-rose-500/30"
            >
              Delete Out-of-Range Keyframes
            </button>
          </div>
        </div>
      )}

      {/* Video Stage */}
      <div className="relative flex flex-1 items-center justify-center bg-[#0b0d11] p-3 overflow-hidden">
        <div
          ref={stageRef}
          className="relative overflow-hidden rounded-lg border border-zinc-800 bg-black select-none"
          style={{
            aspectRatio: `${srcW} / ${srcH}`,
            width: srcW >= srcH ? "min(100%, 780px)" : "auto",
            height: srcW < srcH ? "100%" : "auto",
            maxWidth: "100%",
            maxHeight: "100%",
          }}
        >
          {proxyUrl ? (
            <>
              <video
                ref={videoRef}
                src={proxyUrl}
                className="h-full w-full object-contain"
                playsInline
                onTimeUpdate={(e) => {
                  if (isPlaying && !isProjectPlaying) {
                    onSourceTimeChange(e.currentTarget.currentTime);
                  }
                }}
              />
              {voiceAudioUrl && (
                <audio ref={voiceAudioRef} src={voiceAudioUrl} preload="auto" />
              )}
            </>
          ) : (
            <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-zinc-400">
              {manifest.proxy.status === "failed" ? (
                <>
                  <AlertTriangle className="h-6 w-6 text-rose-400" />
                  <p className="text-xs text-rose-300">
                    Proxy failed: {manifest.proxy.error ?? "Unknown error"}
                  </p>
                </>
              ) : (
                <>
                  <Loader2 className="h-6 w-6 animate-spin text-emerald-400" />
                  <p className="text-xs">
                    Generating 540p 30fps proxy ({manifest.proxy.status})...
                  </p>
                </>
              )}
            </div>
          )}

          {/* Crop Overlay */}
          {(useProjectCamera || segment) && (
            <div
              className="absolute border-2 border-emerald-400 cursor-move"
              style={{
                left: `${boxLeftPct}%`,
                top: `${boxTopPct}%`,
                width: `${boxWidthPct}%`,
                height: `${boxHeightPct}%`,
                boxShadow: showMask ? "0 0 0 9999px rgba(0, 0, 0, 0.58)" : "none",
              }}
              onMouseDown={(e) => {
                e.preventDefault();
                onBeginHistoryTransaction?.("Move camera crop");
                setDragMode({
                  type: "move",
                  startClientX: e.clientX,
                  startClientY: e.clientY,
                  origX: currentCrop.x,
                  origY: currentCrop.y,
                });
              }}
            >
              {/* Safe-Area & Caption Guide */}
              {showSafeGuide && (
                <div className="pointer-events-none absolute inset-0">
                  <div className="absolute inset-x-[6%] top-[10%] bottom-[18%] border border-dashed border-sky-400/50" />
                  <div
                    className="absolute inset-x-[5%] flex items-center justify-center rounded border border-amber-400/60 bg-amber-500/10 text-[10px] font-medium text-amber-200"
                    style={{
                      bottom: `${Math.min(40, (captionSettings.margin_v / Math.max(1, outputHeight)) * 100)}%`,
                      height: "10%",
                    }}
                  >
                    Caption Safe Zone ({outputWidth}×{outputHeight})
                  </div>
                </div>
              )}

              {/* Corner Zoom Handles */}
              {["tl", "tr", "bl", "br"].map((corner) => (
                <div
                  key={corner}
                  onMouseDown={(e) => {
                    e.stopPropagation();
                    e.preventDefault();
                    onBeginHistoryTransaction?.("Zoom camera crop");
                    setDragMode({
                      type: "zoom",
                      corner,
                      startClientX: e.clientX,
                      startClientY: e.clientY,
                      origZoom: currentCrop.zoom,
                      origX: currentCrop.x,
                      origY: currentCrop.y,
                    });
                  }}
                  title="Drag to zoom (1.0x – 3.0x)"
                  className={`absolute h-3 w-3 rounded-sm border border-zinc-900 bg-emerald-400 ${
                    corner === "tl"
                      ? "-top-1.5 -left-1.5 cursor-nwse-resize"
                      : corner === "tr"
                        ? "-top-1.5 -right-1.5 cursor-nesw-resize"
                        : corner === "bl"
                          ? "-bottom-1.5 -left-1.5 cursor-nesw-resize"
                          : "-bottom-1.5 -right-1.5 cursor-nwse-resize"
                  }`}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Minimal Transport Bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-zinc-800 bg-[#13161c] px-3 py-2 text-xs">
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={preparingPlayback}
            onClick={onTogglePlay}
            className="inline-flex items-center gap-1 rounded bg-zinc-800 px-2.5 py-1 text-zinc-100 hover:bg-zinc-700 disabled:opacity-50"
          >
            {preparingPlayback ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Preparing Audio...
              </>
            ) : isPlaying ? (
              <>
                <Pause className="h-3.5 w-3.5" /> Pause
              </>
            ) : (
              <>
                <Play className="h-3.5 w-3.5" /> Play
              </>
            )}
          </button>
          {segment ? (
            <span className="font-mono text-zinc-300">
              {formatTimecode(segRelTime)} / {formatTimecode(durationPlan?.finalDur ?? rawVideoDur)}
            </span>
          ) : (
            <span className="font-mono text-zinc-500">
              Src: {formatTimecode(sourceTime)}
            </span>
          )}
        </div>

        {(useProjectCamera || segment) && (
          <button
            type="button"
            onClick={() =>
              upsertKeyframeAt(
                activeKeyframeTime,
                currentCrop.x,
                currentCrop.y,
                currentCrop.zoom
              )
            }
            className="inline-flex items-center gap-1 rounded border border-amber-500/40 bg-amber-500/15 px-2.5 py-1 text-amber-200 hover:bg-amber-500/25"
          >
            <Plus className="h-3.5 w-3.5" />
            Add Keyframe @{activeKeyframeTime.toFixed(2)}s
          </button>
        )}
      </div>

      {/* In-place Captioned Preview Modal */}
      {previewModalOpen && previewArtifactId && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Project Preview Player"
        >
          <div className="flex max-h-[90vh] w-full max-w-md flex-col overflow-hidden rounded-xl border border-zinc-700 bg-[#151821] shadow-2xl">
            <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-2.5">
              <span className="text-sm font-semibold text-zinc-100">
                Full-Res Project Preview ({outputWidth}×{outputHeight})
              </span>
              <button
                type="button"
                onClick={() => onSetPreviewModalOpen(false)}
                className="rounded p-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex items-center justify-center bg-black p-3">
              {(() => {
                const previewVersion = manifest.latest_preview_path ?? "";
                const previewSrc = `${apiClient.mediaUrl(projectId, previewArtifactId)}?v=${encodeURIComponent(previewVersion)}`;
                return (
                  <video
                    key={previewSrc}
                    src={previewSrc}
                    controls
                    autoPlay
                    className="max-h-[75vh] w-auto rounded"
                  />
                );
              })()}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
