import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Eye,
  EyeOff,
  Film,
  Grid,
  Layers,
  Loader2,
  Maximize2,
  Monitor,
  Pause,
  Play,
  Plus,
  Sparkles,
  X,
} from "lucide-react";
import { apiClient } from "../api/client";
import type {
  ArtifactManifest,
  CropKeyframe,
  MediaAssetMetadata,
  ProjectInput,
  TimelineSelection,
  TimelineTrack,
  VisualClip,
} from "../types/studio";
import {
  clampCropPosition,
  computeAudioDuckingFactorAt,
  computeAudioFadeGainAt,
  computeClipEdgeTransitionState,
  computeIdealCrop,
  computeVisualSourceTime,
  evaluateCropAt,
  findActiveAudioClipsAt,
  findActiveCaptionsAt,
  findActiveVisualLayersAt,
  formatAspectRatio,
  formatTimecode,
  getProjectDuration,
} from "../utils/studioMath";

interface CropCanvasProps {
  project: ProjectInput;
  manifest: ArtifactManifest;
  selection: TimelineSelection;
  projectTime: number;
  sourceTime: number;
  monitorMode: "timeline" | "source";
  previewAssetId: string | null;
  isPlaying: boolean;
  isProjectPlaying: boolean;
  preparingPlayback?: boolean;
  canPreview: boolean;
  previewBusy: boolean;
  previewModalOpen: boolean;
  onTogglePlay: () => void;
  onSourceTimeChange: (t: number) => void;
  onUpdateCameraKeyframes: (
    keyframes: CropKeyframe[],
    meta?: { label?: string; coalesceKey?: string }
  ) => void;
  onUpdateTracks: (
    updater: (tracks: TimelineTrack[]) => TimelineTrack[],
    meta?: { label?: string; coalesceKey?: string },
    nextSelection?: TimelineSelection
  ) => void;
  onBeginHistoryTransaction?: (label: string) => void;
  onEndHistoryTransaction?: () => void;
  onRenderPreview: () => void;
  onSetPreviewModalOpen: (open: boolean) => void;
  onSelectItem: (sel: TimelineSelection) => void;
  onSetMonitorMode?: (mode: "timeline" | "source") => void;
}

type StageFramingMode = "output" | "camera";

type DragMode =
  | {
      type: "crop-move";
      startClientX: number;
      startClientY: number;
      origX: number;
      origY: number;
      camW: number;
      camH: number;
    }
  | {
      type: "crop-zoom";
      corner: string;
      startClientX: number;
      startClientY: number;
      origZoom: number;
      origX: number;
      origY: number;
      camW: number;
      camH: number;
    }
  | {
      type: "overlay-move";
      trackId: string;
      clipId: string;
      startClientX: number;
      startClientY: number;
      origX: number;
      origY: number;
    }
  | {
      type: "overlay-scale";
      trackId: string;
      clipId: string;
      corner: string;
      startClientX: number;
      startClientY: number;
      origScale: number;
    }
  | null;

export function CropCanvas({
  project,
  manifest,
  selection,
  projectTime,
  sourceTime,
  monitorMode,
  previewAssetId,
  isPlaying,
  isProjectPlaying,
  preparingPlayback = false,
  canPreview,
  previewBusy,
  previewModalOpen,
  onTogglePlay,
  onSourceTimeChange,
  onUpdateCameraKeyframes,
  onUpdateTracks,
  onBeginHistoryTransaction,
  onEndHistoryTransaction,
  onRenderPreview,
  onSetPreviewModalOpen,
  onSelectItem,
  onSetMonitorMode,
}: CropCanvasProps) {
  const stageRef = useRef<HTMLDivElement | null>(null);
  const cameraStageRef = useRef<HTMLDivElement | null>(null);
  const sourceVideoRef = useRef<HTMLVideoElement | null>(null);
  const cameraVideoRef = useRef<HTMLVideoElement | null>(null);
  const videoElMapRef = useRef<Record<string, HTMLVideoElement | null>>({});
  const audioElMapRef = useRef<Record<string, HTMLAudioElement | null>>({});
  const captionAudioMapRef = useRef<Record<string, HTMLAudioElement | null>>({});

  const [framingMode, setFramingMode] = useState<StageFramingMode>("output");
  const [showMask, setShowMask] = useState(true);
  const [showSafeGuide, setShowSafeGuide] = useState(true);
  const [dragMode, setDragMode] = useState<DragMode>(null);
  const [stageHeightPx, setStageHeightPx] = useState(420);

  const projectId = project.project_id;
  const outW = Math.max(2, project.output_settings.width);
  const outH = Math.max(2, project.output_settings.height);
  const aspectLabel = formatAspectRatio(outW, outH);
  const captionSettings = project.caption_settings;
  const tracks = project.tracks ?? [];
  const cameraKeyframes = project.camera_keyframes ?? [];

  const getAssetMeta = (assetId: string): MediaAssetMetadata | undefined => {
    const direct = manifest.media_metadata?.[assetId];
    if (direct) return direct;
    if (assetId === project.source_media.media_id && manifest.source_metadata) {
      const sm = manifest.source_metadata;
      return {
        kind: "video",
        width: sm.width,
        height: sm.height,
        fps: sm.fps,
        duration: sm.duration,
        has_audio: sm.has_audio,
        has_alpha: false,
        codec_name: sm.codec_name,
        pix_fmt: sm.pix_fmt,
      };
    }
    return undefined;
  };

  const resolveMediaUrl = (assetId: string, kindHint?: "video" | "image" | "audio"): string | null => {
    if (kindHint !== "image" && kindHint !== "audio") {
      if (assetId === project.source_media.media_id) {
        if (manifest.proxy.status === "ready" && manifest.proxy.artifact_id) {
          return apiClient.mediaUrl(projectId, manifest.proxy.artifact_id);
        }
        return null;
      }
      const p = manifest.media_proxies?.[assetId];
      if (p?.status === "ready" && p.artifact_id) {
        return apiClient.mediaUrl(projectId, p.artifact_id);
      }
    }
    if (project.media_assets?.[assetId] || manifest.media_metadata?.[assetId]) {
      return apiClient.mediaUrl(projectId, assetId);
    }
    if (assetId === project.source_media.media_id) {
      return apiClient.mediaUrl(projectId, assetId);
    }
    return null;
  };

  const activeSourceId = previewAssetId || project.source_media.media_id;
  const activeSourceMeta = getAssetMeta(activeSourceId);
  const primarySrcW = manifest.source_metadata?.width ?? 1920;
  const primarySrcH = manifest.source_metadata?.height ?? 1080;

  const activeSourceProxyStatus =
    activeSourceId === project.source_media.media_id
      ? manifest.proxy.status
      : manifest.media_proxies?.[activeSourceId]?.status ?? "ready";
  const activeSourceUrl = resolveMediaUrl(activeSourceId, "video");

  const activeVisualLayers = useMemo(
    () => findActiveVisualLayersAt(tracks, projectTime),
    [tracks, projectTime]
  );
  const activeAudioLayers = useMemo(
    () => findActiveAudioClipsAt(tracks, projectTime),
    [tracks, projectTime]
  );
  const activeCaptions = useMemo(
    () => findActiveCaptionsAt(tracks, manifest, projectTime),
    [tracks, manifest, projectTime]
  );
  const duckFactor = useMemo(
    () => computeAudioDuckingFactorAt(tracks, manifest, projectTime),
    [tracks, manifest, projectTime]
  );

  // Choose camera follow reference dimensions (active camera_follow layer -> primary source dims)
  const activeCameraLayer =
    activeVisualLayers.find((l) => l.track.camera_follow) ?? null;
  const activeCameraAssetMeta = activeCameraLayer
    ? getAssetMeta(activeCameraLayer.clip.asset_id)
    : undefined;
  const camRefW = activeCameraAssetMeta?.width ?? primarySrcW;
  const camRefH = activeCameraAssetMeta?.height ?? primarySrcH;

  const evaluatedCameraCrop =
    cameraKeyframes.length > 0
      ? evaluateCropAt(cameraKeyframes, projectTime, camRefW, camRefH, outW, outH)
      : clampCropPosition(0.28, 0, 1.0, camRefW, camRefH, outW, outH);

  const idealCameraCrop = computeIdealCrop(
    camRefW,
    camRefH,
    evaluatedCameraCrop.zoom,
    outW,
    outH
  );

  const totalProjectDur = useMemo(
    () => getProjectDuration(project, manifest, 0),
    [project, manifest]
  );

  // Track Output canvas pixel height so caption font preview scales accurately
  useEffect(() => {
    const el = stageRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.contentRect.height > 0) {
          setStageHeightPx(entry.contentRect.height);
        }
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [framingMode, monitorMode]);

  const upsertKeyframeAt = (
    t: number,
    x: number,
    y: number,
    zoom: number,
    refW: number,
    refH: number
  ) => {
    const clamped = clampCropPosition(x, y, zoom, refW, refH, outW, outH);
    const roundedT = Number(Math.max(0, t).toFixed(3));
    const existingIdx = cameraKeyframes.findIndex(
      (kf) => Math.abs(kf.t - roundedT) < 0.05
    );
    let nextKfs: CropKeyframe[];
    if (existingIdx >= 0) {
      nextKfs = cameraKeyframes.map((kf, idx) =>
        idx === existingIdx
          ? {
              ...kf,
              x: Number(clamped.x.toFixed(4)),
              y: Number(clamped.y.toFixed(4)),
              zoom: Number(clamped.zoom.toFixed(3)),
            }
          : kf
      );
    } else {
      nextKfs = [
        ...cameraKeyframes,
        {
          t: roundedT,
          x: Number(clamped.x.toFixed(4)),
          y: Number(clamped.y.toFixed(4)),
          zoom: Number(clamped.zoom.toFixed(3)),
          transition: "tween" as const,
        },
      ].sort((a, b) => a.t - b.t);
    }
    onUpdateCameraKeyframes(nextKfs, {
      label: "Adjust camera keyframe",
      coalesceKey: `cam-kf-${roundedT}`,
    });
  };

  // Dragging handlers for camera crop & selected overlay transform
  useEffect(() => {
    if (!dragMode) return;

    const handleMouseMove = (e: MouseEvent) => {
      if (dragMode.type === "crop-move") {
        const refEl =
          framingMode === "camera" ? cameraStageRef.current : stageRef.current;
        const rect = refEl?.getBoundingClientRect();
        if (!rect || rect.width <= 0 || rect.height <= 0) return;

        let dxNorm: number;
        let dyNorm: number;
        if (framingMode === "camera") {
          dxNorm = (e.clientX - dragMode.startClientX) / rect.width;
          dyNorm = (e.clientY - dragMode.startClientY) / rect.height;
        } else {
          // Dragging scene under viewport in Output mode moves crop window in opposite direction
          const ideal = computeIdealCrop(
            dragMode.camW,
            dragMode.camH,
            evaluatedCameraCrop.zoom,
            outW,
            outH
          );
          const cropWNorm = ideal.idealCropW / dragMode.camW;
          const cropHNorm = ideal.idealCropH / dragMode.camH;
          dxNorm = -((e.clientX - dragMode.startClientX) / rect.width) * cropWNorm;
          dyNorm = -((e.clientY - dragMode.startClientY) / rect.height) * cropHNorm;
        }
        upsertKeyframeAt(
          projectTime,
          dragMode.origX + dxNorm,
          dragMode.origY + dyNorm,
          evaluatedCameraCrop.zoom,
          dragMode.camW,
          dragMode.camH
        );
      } else if (dragMode.type === "crop-zoom") {
        const dx = e.clientX - dragMode.startClientX;
        const dy = e.clientY - dragMode.startClientY;
        const delta =
          (dragMode.corner.includes("r") ? dx : -dx) +
          (dragMode.corner.includes("b") ? dy : -dy);
        const nextZoom = Math.min(
          3.0,
          Math.max(1.0, dragMode.origZoom - delta * 0.005)
        );
        upsertKeyframeAt(
          projectTime,
          dragMode.origX,
          dragMode.origY,
          nextZoom,
          dragMode.camW,
          dragMode.camH
        );
      } else if (dragMode.type === "overlay-move") {
        const rect = stageRef.current?.getBoundingClientRect();
        if (!rect || rect.width <= 0 || rect.height <= 0) return;
        const dxNorm = (e.clientX - dragMode.startClientX) / rect.width;
        const dyNorm = (e.clientY - dragMode.startClientY) / rect.height;
        const nextX = Number(Math.max(-0.5, Math.min(1.5, dragMode.origX + dxNorm)).toFixed(4));
        const nextY = Number(Math.max(-0.5, Math.min(1.5, dragMode.origY + dyNorm)).toFixed(4));
        onUpdateTracks(
          (prevTracks) =>
            prevTracks.map((tr) => {
              if (tr.kind !== "video" || tr.id !== dragMode.trackId) return tr;
              return {
                ...tr,
                clips: tr.clips.map((c) =>
                  c.id === dragMode.clipId
                    ? { ...c, transform: { ...c.transform, x: nextX, y: nextY } }
                    : c
                ),
              };
            }),
          {
            label: "Move overlay layer",
            coalesceKey: `ov-move-${dragMode.clipId}`,
          }
        );
      } else if (dragMode.type === "overlay-scale") {
        const dx = e.clientX - dragMode.startClientX;
        const dy = e.clientY - dragMode.startClientY;
        const delta =
          (dragMode.corner.includes("r") ? dx : -dx) +
          (dragMode.corner.includes("b") ? dy : -dy);
        const nextScale = Number(
          Math.max(0.05, Math.min(4.0, dragMode.origScale + delta * 0.004)).toFixed(3)
        );
        onUpdateTracks(
          (prevTracks) =>
            prevTracks.map((tr) => {
              if (tr.kind !== "video" || tr.id !== dragMode.trackId) return tr;
              return {
                ...tr,
                clips: tr.clips.map((c) =>
                  c.id === dragMode.clipId
                    ? { ...c, transform: { ...c.transform, scale: nextScale } }
                    : c
                ),
              };
            }),
          {
            label: "Scale overlay layer",
            coalesceKey: `ov-scale-${dragMode.clipId}`,
          }
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

  // 1. Sync Source Monitor <video>
  useEffect(() => {
    const vid = sourceVideoRef.current;
    if (!vid || !activeSourceUrl || monitorMode !== "source") return;
    vid.volume = 1;
    vid.muted = false;
    if (isPlaying && !isProjectPlaying) {
      if (Math.abs(vid.currentTime - sourceTime) > 0.35) {
        vid.currentTime = Math.max(0, sourceTime);
      }
      if (vid.paused) {
        vid.play().catch(() => {});
      }
    } else {
      if (!vid.paused) vid.pause();
      if (Math.abs(vid.currentTime - sourceTime) > 0.08) {
        vid.currentTime = Math.max(0, sourceTime);
      }
    }
  }, [monitorMode, activeSourceUrl, isPlaying, isProjectPlaying, sourceTime]);

  // 2. Sync Camera Frame reference <video>
  useEffect(() => {
    const vid = cameraVideoRef.current;
    if (!vid || monitorMode !== "timeline" || framingMode !== "camera") return;
    if (!activeCameraLayer || activeCameraLayer.clip.kind !== "video") {
      if (!vid.paused) vid.pause();
      return;
    }
    const meta = getAssetMeta(activeCameraLayer.clip.asset_id);
    const srcState = computeVisualSourceTime(
      activeCameraLayer.clip,
      projectTime,
      meta?.duration ?? 0
    );
    const camGain = activeCameraLayer.track.muted
      ? 0
      : activeCameraLayer.clip.volume *
        computeAudioFadeGainAt(
          activeCameraLayer.clip.duration_sec,
          activeCameraLayer.clip.audio_fade_in_sec,
          activeCameraLayer.clip.audio_fade_out_sec,
          srcState.localTime
        );
    vid.volume = Math.max(0, Math.min(1, camGain));
    vid.muted = camGain <= 0;
    vid.playbackRate = Math.max(0.25, Math.min(4, activeCameraLayer.clip.speed));
    if (isPlaying && !srcState.isFrozen) {
      if (Math.abs(vid.currentTime - srcState.sourceTime) > 0.35) {
        vid.currentTime = Math.max(0, srcState.sourceTime);
      }
      if (vid.paused) vid.play().catch(() => {});
    } else {
      if (!vid.paused) vid.pause();
      if (Math.abs(vid.currentTime - srcState.sourceTime) > 0.08) {
        vid.currentTime = Math.max(0, srcState.sourceTime);
      }
    }
  }, [monitorMode, framingMode, activeCameraLayer, projectTime, isPlaying]);

  // 3. Sync Active Visual Layer <video> elements
  useEffect(() => {
    if (monitorMode !== "timeline") {
      Object.values(videoElMapRef.current).forEach((v) => {
        if (v && !v.paused) v.pause();
      });
      return;
    }

    const activeIds = new Set<string>();
    for (const layer of activeVisualLayers) {
      if (layer.clip.kind !== "video") continue;
      activeIds.add(layer.clip.id);
      const vid = videoElMapRef.current[layer.clip.id];
      if (!vid) continue;
      const meta = getAssetMeta(layer.clip.asset_id);
      const srcState = computeVisualSourceTime(
        layer.clip,
        projectTime,
        meta?.duration ?? 0
      );

      const audioGain = layer.track.muted
        ? 0
        : layer.clip.volume *
          computeAudioFadeGainAt(
            layer.clip.duration_sec,
            layer.clip.audio_fade_in_sec,
            layer.clip.audio_fade_out_sec,
            srcState.localTime
          );
      vid.volume = Math.max(0, Math.min(1, audioGain));
      vid.muted = audioGain <= 0;
      vid.playbackRate = Math.max(0.25, Math.min(4, layer.clip.speed));

      if (isPlaying && !srcState.isFrozen) {
        if (Math.abs(vid.currentTime - srcState.sourceTime) > 0.35) {
          vid.currentTime = Math.max(0, srcState.sourceTime);
        }
        if (vid.paused) {
          vid.play().catch(() => {});
        }
      } else {
        if (!vid.paused) vid.pause();
        if (Math.abs(vid.currentTime - srcState.sourceTime) > 0.08) {
          vid.currentTime = Math.max(0, srcState.sourceTime);
        }
      }
    }

    for (const [cid, vid] of Object.entries(videoElMapRef.current)) {
      if (vid && !activeIds.has(cid) && !vid.paused) {
        vid.pause();
      }
    }
  }, [monitorMode, framingMode, activeVisualLayers, projectTime, isPlaying]);

  // 4. Sync Active Audio Track <audio> pool
  useEffect(() => {
    const activeIds = new Set<string>();
    if (monitorMode === "timeline") {
      for (const layer of activeAudioLayers) {
        activeIds.add(layer.clip.id);
        const aud = audioElMapRef.current[layer.clip.id];
        if (!aud) continue;
        const meta = getAssetMeta(layer.clip.asset_id);
        const availDur = Math.max(0.05, (meta?.duration ?? 60) - layer.clip.media_in);
        const scaled = layer.localTime * layer.clip.speed;
        const targetTime = layer.clip.loop
          ? layer.clip.media_in + (scaled % availDur)
          : Math.min(meta?.duration ?? 60, layer.clip.media_in + scaled);

        const vol =
          layer.effectiveVolume *
          (layer.track.duck_under_narration ? duckFactor : 1.0);
        aud.volume = Math.max(0, Math.min(1, vol));
        aud.playbackRate = Math.max(0.25, Math.min(4, layer.clip.speed));

        if (isPlaying && (layer.clip.loop || scaled < availDur)) {
          if (aud.readyState >= 1 && Math.abs(aud.currentTime - targetTime) > 0.25) {
            aud.currentTime = Math.max(0, targetTime);
          }
          if (aud.paused) aud.play().catch(() => {});
        } else {
          if (!aud.paused) aud.pause();
          if (aud.readyState >= 1 && Math.abs(aud.currentTime - targetTime) > 0.1) {
            aud.currentTime = Math.max(0, targetTime);
          }
        }
      }
    }
    for (const [cid, aud] of Object.entries(audioElMapRef.current)) {
      if (aud && !activeIds.has(cid) && !aud.paused) {
        aud.pause();
      }
    }
  }, [monitorMode, activeAudioLayers, duckFactor, isPlaying, projectTime]);

  // 5. Sync Active Kokoro TTS Voiceover <audio> pool
  useEffect(() => {
    const activeIds = new Set<string>();
    if (monitorMode === "timeline") {
      for (const layer of activeCaptions) {
        if (!layer.clip.tts_enabled || layer.track.muted) continue;
        const art = manifest.caption_artifacts?.[layer.clip.id];
        if (!art?.audio_artifact_id) continue;
        activeIds.add(layer.clip.id);
        const aud = captionAudioMapRef.current[layer.clip.id];
        if (!aud) continue;
        const within =
          art.audio_duration > 0 && layer.localTime < art.audio_duration - 0.02;
        if (isPlaying && within) {
          if (aud.readyState >= 1 && Math.abs(aud.currentTime - layer.localTime) > 0.25) {
            aud.currentTime = Math.max(0, layer.localTime);
          }
          if (aud.paused) aud.play().catch(() => {});
        } else {
          if (!aud.paused) aud.pause();
          if (!isPlaying && aud.readyState >= 1 && art.audio_duration > 0) {
            const clamped = Math.max(0, Math.min(art.audio_duration, layer.localTime));
            if (Math.abs(aud.currentTime - clamped) > 0.08) {
              aud.currentTime = clamped;
            }
          }
        }
      }
    }
    for (const [cid, aud] of Object.entries(captionAudioMapRef.current)) {
      if (aud && !activeIds.has(cid) && !aud.paused) {
        aud.pause();
      }
    }
  }, [monitorMode, activeCaptions, manifest.caption_artifacts, isPlaying, projectTime]);

  // Determine whether any visible <video> is mounted right now so we can mount a hidden
  // fallback primary <video> during gaps / 0-video-clip timelines
  const hasVisibleVideoEl =
    (monitorMode === "source" && Boolean(activeSourceUrl)) ||
    (monitorMode === "timeline" &&
      framingMode === "output" &&
      activeVisualLayers.some(
        (l) => l.clip.kind === "video" && Boolean(resolveMediaUrl(l.clip.asset_id, "video"))
      )) ||
    (monitorMode === "timeline" &&
      framingMode === "camera" &&
      Boolean(
        activeCameraLayer
          ? resolveMediaUrl(activeCameraLayer.clip.asset_id, activeCameraLayer.clip.kind)
          : activeSourceUrl
      ));

  const primaryProxyUrl = resolveMediaUrl(project.source_media.media_id, "video");
  const previewArtifactId = manifest.latest_preview_artifact_id ?? null;

  const renderStageBody = () => {
    // Source Monitor Mode (for scrubbing source assets & marking I/O)
    if (monitorMode === "source") {
      const sW = activeSourceMeta?.width ?? primarySrcW;
      const sH = activeSourceMeta?.height ?? primarySrcH;
      const srcCrop = evaluateCropAt(cameraKeyframes, projectTime, sW, sH, outW, outH);
      const srcIdeal = computeIdealCrop(sW, sH, srcCrop.zoom, outW, outH);

      return (
        <div
          className="relative overflow-hidden rounded-lg border border-sky-700/60 bg-black select-none"
          style={{
            aspectRatio: `${sW} / ${sH}`,
            width: sW >= sH ? "min(100%, 760px)" : "auto",
            height: sW < sH ? "100%" : "auto",
            maxWidth: "100%",
            maxHeight: "100%",
          }}
        >
          {activeSourceUrl ? (
            <video
              ref={sourceVideoRef}
              src={activeSourceUrl}
              className="h-full w-full object-contain"
              playsInline
              onTimeUpdate={(e) => {
                if (isPlaying && !isProjectPlaying) {
                  onSourceTimeChange(e.currentTarget.currentTime);
                }
              }}
            />
          ) : (
            <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-zinc-400">
              {activeSourceProxyStatus === "failed" ? (
                <>
                  <AlertTriangle className="h-6 w-6 text-rose-400" />
                  <p className="text-xs text-rose-300">Proxy generation failed</p>
                </>
              ) : (
                <>
                  <Loader2 className="h-6 w-6 animate-spin text-emerald-400" />
                  <p className="text-xs">
                    Generating proxy ({activeSourceProxyStatus})...
                  </p>
                </>
              )}
            </div>
          )}

          {/* Read-only reference framing guide */}
          {showMask && (
            <div
              className="pointer-events-none absolute border border-dashed border-sky-400/70"
              style={{
                left: `${srcCrop.x * 100}%`,
                top: `${srcCrop.y * 100}%`,
                width: `${(srcIdeal.idealCropW / sW) * 100}%`,
                height: `${(srcIdeal.idealCropH / sH) * 100}%`,
                boxShadow: "0 0 0 9999px rgba(0, 0, 0, 0.45)",
              }}
            />
          )}
        </div>
      );
    }

    // Camera Frame Mode (full source frame with interactive 9:16 crop window + corner zoom)
    if (framingMode === "camera") {
      const boxLeftPct = evaluatedCameraCrop.x * 100;
      const boxTopPct = evaluatedCameraCrop.y * 100;
      const boxWidthPct = (idealCameraCrop.idealCropW / camRefW) * 100;
      const boxHeightPct = (idealCameraCrop.idealCropH / camRefH) * 100;
      const camMediaUrl = activeCameraLayer
        ? resolveMediaUrl(
            activeCameraLayer.clip.asset_id,
            activeCameraLayer.clip.kind
          )
        : activeSourceUrl;

      return (
        <div
          ref={cameraStageRef}
          className="relative overflow-hidden rounded-lg border border-zinc-800 bg-black select-none"
          style={{
            aspectRatio: `${camRefW} / ${camRefH}`,
            width: camRefW >= camRefH ? "min(100%, 760px)" : "auto",
            height: camRefW < camRefH ? "100%" : "auto",
            maxWidth: "100%",
            maxHeight: "100%",
          }}
        >
          {activeCameraLayer ? (
            activeCameraLayer.clip.kind === "image" && camMediaUrl ? (
              <img
                src={camMediaUrl}
                alt="Camera reference"
                className="h-full w-full object-contain pointer-events-none"
              />
            ) : camMediaUrl ? (
              <video
                ref={cameraVideoRef}
                src={camMediaUrl}
                className="h-full w-full object-contain pointer-events-none"
                playsInline
              />
            ) : (
              <div className="flex h-full w-full items-center justify-center text-xs text-zinc-500">
                Loading video proxy...
              </div>
            )
          ) : (
            <div className="flex h-full w-full flex-col items-center justify-center gap-1.5 bg-[radial-gradient(#232836_1px,transparent_1px)] [background-size:16px_16px] text-center text-zinc-400">
              <span className="text-xs font-medium text-zinc-300">
                {totalProjectDur > 0
                  ? "Gap — No camera-follow clip at playhead"
                  : "0-Clip Empty Canvas"}
              </span>
              <span className="text-[11px] text-zinc-500">
                Drag crop box or click + Keyframe @{projectTime.toFixed(2)}s independently
              </span>
            </div>
          )}

          {/* Interactive Camera Crop Window */}
          <div
            data-testid="camera-crop-box"
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
                type: "crop-move",
                startClientX: e.clientX,
                startClientY: e.clientY,
                origX: evaluatedCameraCrop.x,
                origY: evaluatedCameraCrop.y,
                camW: camRefW,
                camH: camRefH,
              });
            }}
          >
            {showSafeGuide && (
              <div className="pointer-events-none absolute inset-0">
                <div className="absolute inset-x-[6%] top-[10%] bottom-[18%] border border-dashed border-sky-400/50" />
                <div
                  className="absolute inset-x-[5%] flex items-center justify-center rounded border border-amber-400/60 bg-amber-500/10 text-[10px] font-medium text-amber-200"
                  style={{
                    bottom: `${Math.min(40, (captionSettings.margin_v / Math.max(1, outH)) * 100)}%`,
                    height: "10%",
                  }}
                >
                  Caption Safe Zone ({outW}×{outH})
                </div>
              </div>
            )}

            {["tl", "tr", "bl", "br"].map((corner) => (
              <div
                key={corner}
                onMouseDown={(e) => {
                  e.stopPropagation();
                  e.preventDefault();
                  onBeginHistoryTransaction?.("Zoom camera crop");
                  setDragMode({
                    type: "crop-zoom",
                    corner,
                    startClientX: e.clientX,
                    startClientY: e.clientY,
                    origZoom: evaluatedCameraCrop.zoom,
                    origX: evaluatedCameraCrop.x,
                    origY: evaluatedCameraCrop.y,
                    camW: camRefW,
                    camH: camRefH,
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
        </div>
      );
    }

    // Program Output WYSIWYG Compositor Mode (renders layered tracks bottom-to-top + transitions + captions)
    return (
      <div
        ref={stageRef}
        data-testid="program-canvas"
        className="relative overflow-hidden rounded-lg border border-zinc-800 bg-black select-none shadow-lg"
        style={{
          aspectRatio: `${outW} / ${outH}`,
          height: outH >= outW ? "100%" : "auto",
          width: outW > outH ? "min(100%, 760px)" : "auto",
          maxWidth: "100%",
          maxHeight: "100%",
        }}
        onMouseDown={(e) => {
          // Allow direct camera panning by dragging canvas background when a camera-follow layer is active
          if (e.target === e.currentTarget || (e.target as HTMLElement).dataset.camDrag === "true") {
            e.preventDefault();
            onBeginHistoryTransaction?.("Pan camera crop");
            setDragMode({
              type: "crop-move",
              startClientX: e.clientX,
              startClientY: e.clientY,
              origX: evaluatedCameraCrop.x,
              origY: evaluatedCameraCrop.y,
              camW: camRefW,
              camH: camRefH,
            });
          }
        }}
      >
        {activeVisualLayers.length === 0 ? (
          <div
            data-cam-drag="true"
            className="flex h-full w-full flex-col items-center justify-center gap-1 bg-black text-center text-zinc-500"
          >
            {manifest.proxy.status === "pending" || manifest.proxy.status === "running" ? (
              <>
                <Loader2 className="h-5 w-5 animate-spin text-emerald-400" />
                <span className="text-[11px] text-zinc-400">
                  Preparing proxy ({manifest.proxy.status})...
                </span>
              </>
            ) : (
              <>
                <span className="text-[11px] font-mono text-zinc-500">
                  {tracks.some((t) => t.kind === "video" && t.clips.length > 0)
                    ? "Gap (Black Canvas)"
                    : "0 Video Clips"}
                </span>
                <span className="text-[10px] text-zinc-600">
                  Captions &amp; camera keyframes remain independent
                </span>
              </>
            )}
          </div>
        ) : (
          activeVisualLayers.map((layer) => {
            const clip: VisualClip = layer.clip;
            const meta = getAssetMeta(clip.asset_id);
            const aW = meta?.width ?? primarySrcW;
            const aH = meta?.height ?? primarySrcH;
            const mediaUrl = resolveMediaUrl(clip.asset_id, clip.kind);
            const edge = computeClipEdgeTransitionState(clip, layer.localTime);
            const effOpacity = Math.max(
              0,
              Math.min(1, clip.transform.opacity * edge.opacityMultiplier)
            );
            const isSelectedOverlay =
              !layer.track.camera_follow &&
              selection?.type === "video-clip" &&
              selection.clipId === clip.id &&
              selection.trackId === layer.track.id;

            if (layer.track.camera_follow) {
              const crop = evaluateCropAt(cameraKeyframes, projectTime, aW, aH, outW, outH);
              const ideal = computeIdealCrop(aW, aH, crop.zoom, outW, outH);
              const cropWNorm = ideal.idealCropW / aW;
              const cropHNorm = ideal.idealCropH / aH;

              return (
                <div
                  key={`${layer.track.id}-${clip.id}`}
                  data-cam-drag="true"
                  className="absolute overflow-hidden cursor-move"
                  style={{
                    left: `${clip.transform.x * 100 + edge.offsetXPct}%`,
                    top: `${clip.transform.y * 100 + edge.offsetYPct}%`,
                    width: `${clip.transform.scale * 100}%`,
                    height: `${clip.transform.scale * 100}%`,
                    transform: `translate(-50%, -50%) rotate(${clip.transform.rotation_deg}deg) scale(${edge.scaleMultiplier})`,
                    opacity: effOpacity,
                    clipPath: edge.clipPathCss,
                  }}
                >
                  {clip.kind === "image" && mediaUrl ? (
                    <img
                      data-cam-drag="true"
                      src={mediaUrl}
                      alt={clip.id}
                      style={{
                        position: "absolute",
                        maxWidth: "none",
                        maxHeight: "none",
                        width: `${(1 / cropWNorm) * 100}%`,
                        height: `${(1 / cropHNorm) * 100}%`,
                        left: `${-(crop.x / cropWNorm) * 100}%`,
                        top: `${-(crop.y / cropHNorm) * 100}%`,
                      }}
                    />
                  ) : mediaUrl ? (
                    <video
                      ref={(el) => {
                        videoElMapRef.current[clip.id] = el;
                      }}
                      data-clip-id={clip.id}
                      data-cam-drag="true"
                      src={mediaUrl}
                      playsInline
                      style={{
                        position: "absolute",
                        maxWidth: "none",
                        maxHeight: "none",
                        width: `${(1 / cropWNorm) * 100}%`,
                        height: `${(1 / cropHNorm) * 100}%`,
                        left: `${-(crop.x / cropWNorm) * 100}%`,
                        top: `${-(crop.y / cropHNorm) * 100}%`,
                      }}
                    />
                  ) : (
                    <div className="flex h-full w-full items-center justify-center bg-zinc-950 text-[11px] text-zinc-400">
                      {manifest.proxy.status === "failed"
                        ? `Proxy failed: ${manifest.proxy.error ?? "error"}`
                        : `Generating proxy (${manifest.proxy.status})...`}
                    </div>
                  )}
                </div>
              );
            }

            // Overlay / PiP Layer (non-camera_follow, fit-to-canvas with transform)
            const fitScale = Math.min(outW / Math.max(1, aW), outH / Math.max(1, aH));
            const baseWFrac = (aW * fitScale) / outW;
            const baseHFrac = (aH * fitScale) / outH;

            return (
              <div
                key={`${layer.track.id}-${clip.id}`}
                data-testid={`stage-overlay-${clip.id}`}
                onMouseDown={(e) => {
                  e.stopPropagation();
                  onSelectItem({
                    type: "video-clip",
                    trackId: layer.track.id,
                    clipId: clip.id,
                  });
                  if (layer.track.locked) return;
                  e.preventDefault();
                  onBeginHistoryTransaction?.("Move overlay layer");
                  setDragMode({
                    type: "overlay-move",
                    trackId: layer.track.id,
                    clipId: clip.id,
                    startClientX: e.clientX,
                    startClientY: e.clientY,
                    origX: clip.transform.x,
                    origY: clip.transform.y,
                  });
                }}
                className={`absolute cursor-move ${
                  isSelectedOverlay ? "ring-2 ring-purple-400" : ""
                }`}
                style={{
                  left: `${clip.transform.x * 100 + edge.offsetXPct}%`,
                  top: `${clip.transform.y * 100 + edge.offsetYPct}%`,
                  width: `${baseWFrac * clip.transform.scale * 100}%`,
                  height: `${baseHFrac * clip.transform.scale * 100}%`,
                  transform: `translate(-50%, -50%) rotate(${clip.transform.rotation_deg}deg) scale(${edge.scaleMultiplier})`,
                  opacity: effOpacity,
                  clipPath: edge.clipPathCss,
                }}
              >
                {clip.kind === "image" && mediaUrl ? (
                  <img
                    src={mediaUrl}
                    alt={clip.id}
                    className="h-full w-full object-contain pointer-events-none"
                  />
                ) : mediaUrl ? (
                  <video
                    ref={(el) => {
                      videoElMapRef.current[clip.id] = el;
                    }}
                    data-clip-id={clip.id}
                    src={mediaUrl}
                    playsInline
                    className="h-full w-full object-contain pointer-events-none"
                  />
                ) : null}

                {isSelectedOverlay &&
                  !layer.track.locked &&
                  ["tl", "tr", "bl", "br"].map((corner) => (
                    <div
                      key={corner}
                      onMouseDown={(e) => {
                        e.stopPropagation();
                        e.preventDefault();
                        onBeginHistoryTransaction?.("Scale overlay layer");
                        setDragMode({
                          type: "overlay-scale",
                          trackId: layer.track.id,
                          clipId: clip.id,
                          corner,
                          startClientX: e.clientX,
                          startClientY: e.clientY,
                          origScale: clip.transform.scale,
                        });
                      }}
                      title="Drag corner to resize overlay"
                      className={`absolute h-2.5 w-2.5 rounded-sm border border-zinc-950 bg-purple-400 ${
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
            );
          })
        )}

        {/* Safe Area Overlay */}
        {showSafeGuide && (
          <div className="pointer-events-none absolute inset-0">
            <div className="absolute inset-x-[6%] top-[10%] bottom-[18%] border border-dashed border-sky-400/35" />
          </div>
        )}

        {/* Live Synchronized Caption Overlay Preview */}
        {activeCaptions.map((capLayer) => {
          const text = capLayer.clip.display_text.trim();
          if (!text) return null;
          const align = capLayer.clip.style_override?.align ?? "bottom";
          const fontSizePx =
            capLayer.clip.style_override?.font_size ?? captionSettings.font_size;
          const marginV =
            capLayer.clip.style_override?.margin_v ?? captionSettings.margin_v;
          const scaledFont = Math.max(
            11,
            Math.round((fontSizePx / Math.max(1, outH)) * stageHeightPx)
          );
          const marginPct = Math.min(44, (marginV / Math.max(1, outH)) * 100);
          const posStyle =
            align === "top"
              ? { top: `${Math.max(4, marginPct * 0.5)}%` }
              : align === "center"
                ? { top: "50%", transform: "translateY(-50%)" }
                : { bottom: `${marginPct}%` };

          return (
            <div
              key={`${capLayer.track.id}-${capLayer.clip.id}`}
              onClick={(e) => {
                e.stopPropagation();
                onSelectItem({
                  type: "caption-clip",
                  trackId: capLayer.track.id,
                  clipId: capLayer.clip.id,
                });
              }}
              className="absolute inset-x-[6%] text-center font-extrabold uppercase tracking-wide text-white cursor-pointer"
              style={{
                ...posStyle,
                fontSize: `${scaledFont}px`,
                lineHeight: 1.15,
                textShadow:
                  "-1.5px -1.5px 0 #000, 1.5px -1.5px 0 #000, -1.5px 1.5px 0 #000, 1.5px 1.5px 0 #000, 0 2px 6px rgba(0,0,0,0.85)",
              }}
            >
              {text}
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <section className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-zinc-800 bg-[#151821]">
      {/* Stage Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <Film className="h-4 w-4 text-emerald-400" />
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-200">
            Compositor &amp; Camera Stage
          </h2>

          {monitorMode === "source" ? (
            <div className="flex items-center gap-1.5">
              <span className="rounded border border-sky-500/40 bg-sky-500/15 px-2 py-0.5 font-mono text-[11px] text-sky-200">
                Source Monitor • {formatTimecode(sourceTime)}
              </span>
              {onSetMonitorMode && (
                <button
                  type="button"
                  onClick={() => onSetMonitorMode("timeline")}
                  className="rounded border border-zinc-700 bg-zinc-800 px-2 py-0.5 text-[11px] text-zinc-200 hover:bg-zinc-700"
                >
                  Program View
                </button>
              )}
            </div>
          ) : (
            <div className="flex items-center gap-1 rounded border border-zinc-700 bg-zinc-900 p-0.5 text-[11px]">
              <button
                type="button"
                onClick={() => setFramingMode("output")}
                className={`inline-flex items-center gap-1 rounded px-2 py-0.5 font-medium transition ${
                  framingMode === "output"
                    ? "bg-emerald-600 text-white"
                    : "text-zinc-400 hover:text-zinc-200"
                }`}
                title="Preview layered Output Canvas with overlays & live captions"
              >
                <Monitor className="h-3 w-3" />
                Output {aspectLabel}
              </button>
              <button
                type="button"
                onClick={() => setFramingMode("camera")}
                className={`inline-flex items-center gap-1 rounded px-2 py-0.5 font-medium transition ${
                  framingMode === "camera"
                    ? "bg-amber-600 text-white"
                    : "text-zinc-400 hover:text-zinc-200"
                }`}
                title="Edit full-frame Camera crop box & corner zoom"
              >
                <Maximize2 className="h-3 w-3" />
                Camera Frame
              </button>
            </div>
          )}

          <span className="hidden xl:inline rounded bg-zinc-800/80 px-2 py-0.5 font-mono text-[10px] text-zinc-400">
            Zoom {evaluatedCameraCrop.zoom.toFixed(2)}x • {activeVisualLayers.length} layer(s)
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {(monitorMode === "source" || framingMode === "camera") && (
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
              Mask
            </button>
          )}

          <button
            type="button"
            onClick={() => setShowSafeGuide((v) => !v)}
            className={`inline-flex items-center gap-1 rounded border px-2 py-1 text-xs ${
              showSafeGuide
                ? "border-sky-500/40 bg-sky-500/15 text-sky-200"
                : "border-zinc-700 bg-zinc-800/60 text-zinc-300"
            }`}
            title={`Toggle ${outW}x${outH} caption safe-area guide`}
          >
            <Grid className="h-3.5 w-3.5" />
            Safe Area
          </button>

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
        </div>
      </div>

      {/* Stage Area */}
      <div className="relative flex flex-1 items-center justify-center bg-[#0b0d11] p-3 overflow-hidden">
        {renderStageBody()}

        {/* Hidden fallback video element so primary proxy stays mounted during gaps / 0-clip timelines */}
        {!hasVisibleVideoEl && (
          <video
            src={primaryProxyUrl ?? undefined}
            className="hidden"
            playsInline
            muted
          />
        )}

        {/* Hidden Audio Track Pool */}
        {activeAudioLayers.map((layer) => {
          const url = resolveMediaUrl(layer.clip.asset_id, "audio");
          if (!url) return null;
          return (
            <audio
              key={`aud-${layer.clip.id}`}
              ref={(el) => {
                audioElMapRef.current[layer.clip.id] = el;
              }}
              src={url}
              preload="auto"
            />
          );
        })}

        {/* Hidden Caption TTS Voiceover Pool */}
        {activeCaptions.map((layer) => {
          const art = manifest.caption_artifacts?.[layer.clip.id];
          if (!layer.clip.tts_enabled || !art?.audio_artifact_id) return null;
          const url = `${apiClient.mediaUrl(projectId, art.audio_artifact_id)}?v=${encodeURIComponent(art.tts_hash ?? "")}`;
          return (
            <audio
              key={`cap-aud-${layer.clip.id}`}
              ref={(el) => {
                captionAudioMapRef.current[layer.clip.id] = el;
              }}
              src={url}
              preload="auto"
            />
          );
        })}
      </div>

      {/* Transport Bar */}
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
          {monitorMode === "source" ? (
            <span className="font-mono text-sky-300">
              Src: {formatTimecode(sourceTime)}
            </span>
          ) : (
            <span className="font-mono text-zinc-300">
              {formatTimecode(projectTime)} / {formatTimecode(totalProjectDur)}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {monitorMode === "timeline" && (
            <span className="hidden md:inline-flex items-center gap-1 text-[11px] text-zinc-400">
              <Layers className="h-3 w-3 text-emerald-400" />
              {activeVisualLayers.length}V • {activeAudioLayers.length}A • {activeCaptions.length}T
            </span>
          )}
          <button
            type="button"
            onClick={() =>
              upsertKeyframeAt(
                projectTime,
                evaluatedCameraCrop.x,
                evaluatedCameraCrop.y,
                evaluatedCameraCrop.zoom,
                camRefW,
                camRefH
              )
            }
            className="inline-flex items-center gap-1 rounded border border-amber-500/40 bg-amber-500/15 px-2.5 py-1 text-amber-200 hover:bg-amber-500/25"
          >
            <Plus className="h-3.5 w-3.5" />
            Add Keyframe @{projectTime.toFixed(2)}s
          </button>
        </div>
      </div>

      {/* Full-Res Captioned Preview Modal */}
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
                Full-Res Project Preview ({outW}×{outH})
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
