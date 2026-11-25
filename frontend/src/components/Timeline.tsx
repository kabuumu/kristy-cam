import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Bookmark,
  Camera,
  Captions,
  Copy,
  Eye,
  EyeOff,
  FileText,
  Film,
  GripHorizontal,
  Layers,
  Loader2,
  Lock,
  Magnet,
  Music,
  Pause,
  Play,
  Plus,
  Scissors,
  Snowflake,
  Trash2,
  Unlock,
  Volume2,
  VolumeX,
  X,
  Zap,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { apiClient } from "../api/client";
import type {
  ArtifactManifest,
  AudioClip,
  AudioTrack,
  CaptionClip,
  CaptionTrack,
  CropKeyframe,
  MediaAssetKind,
  ProjectInput,
  TimelineMarker,
  TimelineSelection,
  TimelineTrack,
  TrackTransition,
  VideoTrack,
  VisualClip,
} from "../types/studio";
import {
  closeGapOnTrack,
  collectTimelineSnapCandidates,
  computeAdjacentJunctionPairs,
  evaluateCropAt,
  findActiveVisualLayersAt,
  findCutPoints,
  findNextClipId,
  findNextTrackId,
  formatTimecode,
  getCaptionClipEffectiveDuration,
  getProjectDuration,
  rippleDeleteClip,
  snapTimelineTime,
  splitAudioClipAt,
  splitVisualClipAt,
} from "../utils/studioMath";
import { MEDIA_BIN_DND_MIME } from "./MediaBin";

export interface TimelineMultiItem {
  kind: "video-clip" | "audio-clip" | "caption-clip";
  trackId: string;
  clipId: string;
}

interface TimelineProps {
  project: ProjectInput;
  manifest: ArtifactManifest;
  selection: TimelineSelection;
  projectTime: number;
  sourceTime: number;
  previewAssetId: string | null;
  isModalOpen: boolean;
  isProjectPlaying: boolean;
  preparingPlayback?: boolean;
  canPasteClip?: boolean;
  canPasteAttributes?: boolean;
  onSelectItem: (sel: TimelineSelection) => void;
  onProjectTimeChange: (t: number) => void;
  onSourceTimeChange: (t: number) => void;
  onUpdateTracks: (
    updater: (tracks: TimelineTrack[]) => TimelineTrack[],
    meta?: { label?: string; coalesceKey?: string },
    nextSelection?: TimelineSelection
  ) => void;
  onUpdateCameraKeyframes: (
    keyframes: CropKeyframe[],
    meta?: { label?: string; coalesceKey?: string }
  ) => void;
  onUpdateMarkers?: (
    markers: TimelineMarker[],
    meta?: { label?: string; coalesceKey?: string }
  ) => void;
  onBeginHistoryTransaction?: (label: string) => void;
  onEndHistoryTransaction?: () => void;
  onAddSourceClip: (
    assetId: string,
    videoIn: number,
    videoOut: number,
    startSec?: number,
    trackId?: string
  ) => void;
  onDropMediaAsset: (
    assetId: string,
    kind: MediaAssetKind,
    trackId: string,
    startSec: number
  ) => void;
  onToggleProjectPlayback: () => void;
  onSaveProject?: () => void;
  onCopySelectedClip?: () => void;
  onPasteClipAtPlayhead?: () => void;
  onDuplicateSelectedClip?: () => void;
  onCopyAttributes?: () => void;
  onPasteAttributes?: () => void;
  onDetachAudio?: (trackId: string, clipId: string) => void;
  onFreezeFrame?: (trackId: string, clipId: string) => void;
  onOpenShortcuts?: () => void;
  onOpenBulkScript?: () => void;
  onToast?: (msg: string, kind?: "info" | "success" | "warning") => void;
}

type DragAction =
  | { type: "scrub" }
  | { type: "height-resize"; startY: number; origHeight: number }
  | {
      type: "zoom-drag";
      startX: number;
      startY: number;
      origZoom: number;
      origScrollLeft: number;
      anchorClientX: number;
    }
  | {
      type: "keyframe-move";
      index: number;
      startX: number;
      origT: number;
      spanSec: number;
    }
  | {
      type: "vclip-move" | "vclip-left" | "vclip-right" | "vclip-slip";
      trackId: string;
      clipId: string;
      startX: number;
      origStart: number;
      origDur: number;
      origMediaIn: number;
      origMediaOut: number | null;
      speed: number;
      kind: "video" | "image";
      assetDur: number;
      spanSec: number;
    }
  | {
      type: "aclip-move" | "aclip-left" | "aclip-right" | "aclip-fade-in" | "aclip-fade-out";
      trackId: string;
      clipId: string;
      startX: number;
      origStart: number;
      origDur: number;
      origMediaIn: number;
      origFade?: number;
      speed: number;
      assetDur: number;
      spanSec: number;
    }
  | {
      type: "cclip-move" | "cclip-left" | "cclip-right";
      trackId: string;
      clipId: string;
      startX: number;
      origStart: number;
      origDur: number;
      spanSec: number;
    }
  | null;

type ContextMenuState =
  | {
      x: number;
      y: number;
      target:
        | { kind: "clip"; sel: NonNullable<TimelineSelection>; trackId: string; clipId: string; clipKind: "video" | "image" | "audio" | "caption" }
        | { kind: "gap"; trackId: string; timeSec: number };
    }
  | null;

const MIN_TIMELINE_ZOOM = 1;
const MAX_TIMELINE_ZOOM = 16;

function junctionShortCode(type: TrackTransition["type"]): string {
  const map: Record<TrackTransition["type"], string> = {
    crossfade: "CF",
    dissolve: "DIS",
    wipeleft: "WL",
    wiperight: "WR",
    slideleft: "SL",
    slideright: "SR",
    fadeblack: "FB",
    zoomin: "ZM",
  };
  return map[type] ?? "TR";
}

function reorderVideoTrackZ(
  tracks: TimelineTrack[],
  trackId: string,
  direction: "higher" | "lower"
): TimelineTrack[] {
  const videoIndices = tracks
    .map((t, i) => (t.kind === "video" ? i : -1))
    .filter((i) => i >= 0);
  const pos = videoIndices.findIndex((idx) => tracks[idx].id === trackId);
  if (pos < 0) return tracks;
  const swapPos = direction === "higher" ? pos + 1 : pos - 1;
  if (swapPos < 0 || swapPos >= videoIndices.length) return tracks;
  const next = [...tracks];
  const iA = videoIndices[pos];
  const iB = videoIndices[swapPos];
  [next[iA], next[iB]] = [next[iB], next[iA]];
  return next;
}

function WaveformBars({
  peaks,
  colorClass,
}: {
  peaks: number[] | undefined;
  colorClass: string;
}) {
  if (!peaks || peaks.length === 0) return null;
  const n = peaks.length;
  return (
    <svg
      viewBox={`0 0 ${n} 24`}
      preserveAspectRatio="none"
      className={`pointer-events-none absolute inset-0 h-full w-full opacity-35 ${colorClass}`}
    >
      {peaks.map((p, idx) => {
        const h = Math.max(2, p * 22);
        const y = (24 - h) / 2;
        return (
          <rect
            key={idx}
            x={idx}
            y={y}
            width={0.75}
            height={h}
            fill="currentColor"
          />
        );
      })}
    </svg>
  );
}

export function Timeline({
  project,
  manifest,
  selection,
  projectTime,
  sourceTime,
  previewAssetId,
  isModalOpen,
  isProjectPlaying,
  preparingPlayback = false,
  canPasteClip = false,
  canPasteAttributes = false,
  onSelectItem,
  onProjectTimeChange,
  onSourceTimeChange,
  onUpdateTracks,
  onUpdateCameraKeyframes,
  onUpdateMarkers,
  onBeginHistoryTransaction,
  onEndHistoryTransaction,
  onAddSourceClip,
  onDropMediaAsset,
  onToggleProjectPlayback,
  onSaveProject,
  onCopySelectedClip,
  onPasteClipAtPlayhead,
  onDuplicateSelectedClip,
  onCopyAttributes,
  onPasteAttributes,
  onDetachAudio,
  onFreezeFrame,
  onOpenShortcuts,
  onOpenBulkScript,
  onToast,
}: TimelineProps) {
  const activeSrcId = previewAssetId || project.source_media.media_id;
  const activeSrcMeta =
    manifest.media_metadata?.[activeSrcId] ??
    (activeSrcId === project.source_media.media_id && manifest.source_metadata
      ? {
          kind: "video" as const,
          width: manifest.source_metadata.width,
          height: manifest.source_metadata.height,
          fps: manifest.source_metadata.fps,
          duration: manifest.source_metadata.duration,
          has_audio: manifest.source_metadata.has_audio,
          has_alpha: false,
        }
      : undefined);

  const sourceDur = Math.max(0.5, activeSrcMeta?.duration ?? 60);
  const srcW = activeSrcMeta?.width ?? manifest.source_metadata?.width ?? 1920;
  const srcH = activeSrcMeta?.height ?? manifest.source_metadata?.height ?? 1080;

  const [markIn, setMarkIn] = useState(0);
  const [markOut, setMarkOut] = useState(() => Math.min(3.0, sourceDur));
  const [timelineZoom, setTimelineZoom] = useState(1);
  const [snapEnabled, setSnapEnabled] = useState(true);
  const [snapGuideTime, setSnapGuideTime] = useState<number | null>(null);
  const [timelineHeightPx, setTimelineHeightPx] = useState(250);
  const [contextMenu, setContextMenu] = useState<ContextMenuState>(null);
  const [multiSelected, setMultiSelected] = useState<TimelineMultiItem[]>([]);
  const [waveforms, setWaveforms] = useState<Record<string, number[]>>({});
  const fetchedWfKeysRef = useRef<Set<string>>(new Set());
  const [dragOverTrackId, setDragOverTrackId] = useState<string | null>(null);
  const [drag, setDrag] = useState<DragAction>(null);

  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const trackLaneRef = useRef<HTMLDivElement | null>(null);
  const markers = project.markers ?? [];

  const tracks = project.tracks ?? [];
  const videoTracks = useMemo(
    () => tracks.filter((t): t is VideoTrack => t.kind === "video"),
    [tracks]
  );
  // Display highest visual overlay layer on top of lower base layer (Premiere/CapCut convention)
  const displayVideoTracks = useMemo(
    () => [...videoTracks].reverse(),
    [videoTracks]
  );
  const captionTracks = useMemo(
    () => tracks.filter((t): t is CaptionTrack => t.kind === "caption"),
    [tracks]
  );
  const audioTracks = useMemo(
    () => tracks.filter((t): t is AudioTrack => t.kind === "audio"),
    [tracks]
  );
  const cameraKeyframes = project.camera_keyframes ?? [];
  const staleCaptionSet = useMemo(() => {
    const out = new Set<string>();
    for (const tr of captionTracks) {
      for (const c of tr.clips) {
        if (c.tts_enabled && !manifest.caption_artifacts?.[c.id]?.audio_artifact_id) {
          out.add(c.id);
        }
      }
    }
    return out;
  }, [captionTracks, manifest.caption_artifacts]);

  const rawDuration = getProjectDuration(project, manifest, 0);
  const timelineSpan = Math.max(10, Math.ceil(rawDuration + 2));
  const clampedPlayhead = Math.max(0, Math.min(timelineSpan, projectTime));

  useEffect(() => {
    setMarkIn((prev) => Math.min(prev, Math.max(0, sourceDur - 0.1)));
    setMarkOut((prev) => (prev > sourceDur ? Math.max(0.2, sourceDur) : prev));
  }, [sourceDur, activeSrcId]);

  useEffect(() => {
    setMultiSelected([]);
    setWaveforms({});
    fetchedWfKeysRef.current = new Set();
  }, [project.project_id]);

  const startDrag = (action: NonNullable<DragAction>) => {
    const txLabels: Partial<Record<NonNullable<DragAction>["type"], string>> = {
      "keyframe-move": "Move camera keyframe",
      "vclip-move": "Move visual clip",
      "vclip-left": "Trim visual clip start",
      "vclip-right": "Trim visual clip end",
      "vclip-slip": "Slip visual clip media",
      "aclip-move": "Move audio clip",
      "aclip-left": "Trim audio clip start",
      "aclip-right": "Trim audio clip end",
      "aclip-fade-in": "Adjust audio fade-in",
      "aclip-fade-out": "Adjust audio fade-out",
      "cclip-move": "Move caption block",
      "cclip-left": "Trim caption start",
      "cclip-right": "Resize caption duration",
    };
    const label = txLabels[action.type];
    if (label) {
      onBeginHistoryTransaction?.(label);
    }
    setDrag(action);
  };

  // Can we split the currently selected or playhead-active clip?
  const splitTarget = useMemo(() => {
    if (selection?.type === "video-clip") {
      const tr = tracks.find(
        (t): t is VideoTrack => t.kind === "video" && t.id === selection.trackId
      );
      const clp = tr?.clips.find((c) => c.id === selection.clipId);
      if (
        tr &&
        !tr.locked &&
        clp &&
        projectTime > clp.start_sec + 0.1 &&
        projectTime < clp.start_sec + clp.duration_sec - 0.1
      ) {
        return { kind: "video" as const, trackId: tr.id, clip: clp };
      }
    }
    if (selection?.type === "audio-clip") {
      const tr = tracks.find(
        (t): t is AudioTrack => t.kind === "audio" && t.id === selection.trackId
      );
      const clp = tr?.clips.find((c) => c.id === selection.clipId);
      if (
        tr &&
        !tr.locked &&
        clp &&
        projectTime > clp.start_sec + 0.1 &&
        projectTime < clp.start_sec + clp.duration_sec - 0.1
      ) {
        return { kind: "audio" as const, trackId: tr.id, clip: clp };
      }
    }
    const activeLayers = findActiveVisualLayersAt(tracks, projectTime);
    for (let i = activeLayers.length - 1; i >= 0; i--) {
      const layer = activeLayers[i];
      if (
        !layer.track.locked &&
        projectTime > layer.clip.start_sec + 0.1 &&
        projectTime < layer.clip.start_sec + layer.clip.duration_sec - 0.1
      ) {
        return { kind: "video" as const, trackId: layer.track.id, clip: layer.clip };
      }
    }
    return null;
  }, [selection, tracks, projectTime]);

  const handleSplitAtPlayhead = () => {
    if (!splitTarget) return;
    if (splitTarget.kind === "video") {
      const newId = findNextClipId(project, "vclip");
      const split = splitVisualClipAt(splitTarget.clip, projectTime, newId);
      if (!split) return;
      const [first, second] = split;
      onUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "video" || tr.id !== splitTarget.trackId) return tr;
            const nextClips = tr.clips
              .flatMap((c) => (c.id === first.id ? [first, second] : [c]))
              .sort((a, b) => a.start_sec - b.start_sec);
            // Re-point outgoing TrackTransitions that were attached to first.id -> second.id
            const nextTrs = tr.transitions.map((jt) =>
              jt.from_clip_id === first.id ? { ...jt, from_clip_id: second.id } : jt
            );
            return { ...tr, clips: nextClips, transitions: nextTrs };
          }),
        { label: `Split clip ${splitTarget.clip.id}` },
        { type: "video-clip", trackId: splitTarget.trackId, clipId: second.id }
      );
    } else {
      const newId = findNextClipId(project, "aclip");
      const split = splitAudioClipAt(splitTarget.clip, projectTime, newId);
      if (!split) return;
      const [first, second] = split;
      onUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "audio" || tr.id !== splitTarget.trackId) return tr;
            const nextClips = tr.clips
              .flatMap((c) => (c.id === first.id ? [first, second] : [c]))
              .sort((a, b) => a.start_sec - b.start_sec);
            return { ...tr, clips: nextClips };
          }),
        { label: `Split audio ${splitTarget.clip.id}` },
        { type: "audio-clip", trackId: splitTarget.trackId, clipId: second.id }
      );
    }
  };

  const deleteSelectedOrSpecified = (target: TimelineSelection) => {
    if (!target) return;
    if (target.type === "video-clip") {
      onUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "video" || tr.id !== target.trackId) return tr;
            return {
              ...tr,
              clips: tr.clips.filter((c) => c.id !== target.clipId),
              transitions: tr.transitions.filter(
                (jt) => jt.from_clip_id !== target.clipId && jt.to_clip_id !== target.clipId
              ),
            };
          }),
        { label: `Delete clip ${target.clipId}` },
        null
      );
    } else if (target.type === "audio-clip") {
      onUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "audio" || tr.id !== target.trackId) return tr;
            return { ...tr, clips: tr.clips.filter((c) => c.id !== target.clipId) };
          }),
        { label: `Delete audio clip ${target.clipId}` },
        null
      );
    } else if (target.type === "caption-clip") {
      onUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "caption" || tr.id !== target.trackId) return tr;
            return { ...tr, clips: tr.clips.filter((c) => c.id !== target.clipId) };
          }),
        { label: `Delete caption ${target.clipId}` },
        null
      );
    } else if (target.type === "transition") {
      onUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "video" || tr.id !== target.trackId) return tr;
            return {
              ...tr,
              transitions: tr.transitions.filter((jt) => jt.id !== target.transitionId),
            };
          }),
        { label: "Remove transition" },
        null
      );
    } else if (target.type === "keyframe") {
      if (target.index > 0 || cameraKeyframes.length > 1) {
        onUpdateCameraKeyframes(
          cameraKeyframes.filter((_, i) => i !== target.index),
          { label: "Delete camera keyframe" }
        );
        onSelectItem(null);
      }
    }
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

  // Close context menu on outside click or Escape
  useEffect(() => {
    if (!contextMenu) return;
    const dismiss = () => setContextMenu(null);
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setContextMenu(null);
    };
    window.addEventListener("mousedown", dismiss);
    window.addEventListener("keydown", onEsc);
    return () => {
      window.removeEventListener("mousedown", dismiss);
      window.removeEventListener("keydown", onEsc);
    };
  }, [contextMenu]);

  // Fetch cached audio waveforms for audio clips & synthesized TTS caption clips
  useEffect(() => {
    let cancelled = false;
    const audioAssetIds = new Set<string>();
    for (const tr of audioTracks) {
      for (const c of tr.clips) audioAssetIds.add(c.asset_id);
    }
    for (const assetId of audioAssetIds) {
      const key = `asset:${project.project_id}:${assetId}`;
      if (fetchedWfKeysRef.current.has(key)) continue;
      fetchedWfKeysRef.current.add(key);
      apiClient
        .getAssetWaveform(project.project_id, assetId, 96)
        .then((res) => {
          if (!cancelled && res.peaks?.length) {
            setWaveforms((prev) => ({ ...prev, [key]: res.peaks }));
          }
        })
        .catch(() => {});
    }

    for (const tr of captionTracks) {
      for (const c of tr.clips) {
        const art = manifest.caption_artifacts?.[c.id];
        if (!c.tts_enabled || !art?.audio_artifact_id) continue;
        const key = `art:${project.project_id}:${art.audio_artifact_id}:${art.tts_hash ?? ""}`;
        if (fetchedWfKeysRef.current.has(key)) continue;
        fetchedWfKeysRef.current.add(key);
        apiClient
          .getMediaWaveform(project.project_id, art.audio_artifact_id, 64)
          .then((res) => {
            if (!cancelled && res.peaks?.length) {
              setWaveforms((prev) => ({ ...prev, [key]: res.peaks }));
            }
          })
          .catch(() => {});
      }
    }
    return () => {
      cancelled = true;
    };
  }, [project.project_id, audioTracks, captionTracks, manifest.caption_artifacts]);

  // Auto-scroll timeline viewport to keep playhead visible during playback
  useEffect(() => {
    if (!isProjectPlaying || timelineZoom <= 1) return;
    const scroller = scrollContainerRef.current;
    if (!scroller || scroller.clientWidth <= 0) return;
    const totalW = scroller.clientWidth * timelineZoom;
    const playheadX = (clampedPlayhead / timelineSpan) * totalW;
    const viewLeft = scroller.scrollLeft;
    const viewRight = viewLeft + scroller.clientWidth;
    if (playheadX > viewRight - 40 || playheadX < viewLeft + 20) {
      scroller.scrollLeft = Math.max(0, playheadX - scroller.clientWidth * 0.25);
    }
  }, [isProjectPlaying, clampedPlayhead, timelineSpan, timelineZoom]);

  const handleAddMarkerAt = (tSec?: number) => {
    if (!onUpdateMarkers) return;
    const t = Number(Math.max(0, tSec ?? projectTime).toFixed(3));
    const existing = markers.find((m) => Math.abs(m.t - t) < 0.05);
    if (existing) {
      onProjectTimeChange(existing.t);
      return;
    }
    const newMarker: TimelineMarker = {
      id: `m_${Date.now().toString(36)}`,
      t,
      label: `M${markers.length + 1}`,
      color: "#f59e0b",
    };
    const next = [...markers, newMarker].sort((a, b) => a.t - b.t);
    onUpdateMarkers(next, { label: `Add marker @${t.toFixed(2)}s` });
    onToast?.(`Marker added at ${formatTimecode(t)}`, "info");
  };

  const handleDeleteMarker = (markerId: string) => {
    if (!onUpdateMarkers) return;
    onUpdateMarkers(
      markers.filter((m) => m.id !== markerId),
      { label: "Delete timeline marker" }
    );
  };

  const handleRippleDelete = (trackId: string, clipId: string) => {
    onUpdateTracks(
      (prev) => rippleDeleteClip(prev, trackId, clipId, manifest),
      { label: `Ripple delete ${clipId}` },
      null
    );
    setMultiSelected([]);
    onToast?.("Ripple deleted clip", "info");
  };

  const handleCloseGap = (trackId: string, gapTime: number) => {
    onUpdateTracks(
      (prev) => closeGapOnTrack(prev, trackId, gapTime, manifest),
      { label: "Close timeline gap" }
    );
    onToast?.("Closed gap", "info");
  };

  const handleBatchDeleteMulti = () => {
    if (multiSelected.length === 0) return;
    const idSet = new Set(multiSelected.map((m) => m.clipId));
    onUpdateTracks(
      (prev) =>
        prev.map((tr) => {
          if (tr.locked) return tr;
          if (tr.kind === "video") {
            return {
              ...tr,
              clips: tr.clips.filter((c) => !idSet.has(c.id)),
              transitions: tr.transitions.filter(
                (jt) => !idSet.has(jt.from_clip_id) && !idSet.has(jt.to_clip_id)
              ),
            };
          }
          if (tr.kind === "audio") {
            return { ...tr, clips: tr.clips.filter((c) => !idSet.has(c.id)) };
          }
          return { ...tr, clips: tr.clips.filter((c) => !idSet.has(c.id)) };
        }),
      { label: `Delete ${multiSelected.length} clips` },
      null
    );
    setMultiSelected([]);
  };

  const isMultiSelected = (clipId: string) =>
    multiSelected.some((m) => m.clipId === clipId);

  const toggleMultiSelect = (item: TimelineMultiItem) => {
    setMultiSelected((prev) => {
      const exists = prev.some((m) => m.clipId === item.clipId);
      if (exists) return prev.filter((m) => m.clipId !== item.clipId);
      return [...prev, item];
    });
  };

  // Global NLE keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (isModalOpen) return;
      const active = document.activeElement;
      const tag = active?.tagName.toLowerCase();
      const isTextInput =
        tag === "input" ||
        tag === "textarea" ||
        tag === "select" ||
        (active instanceof HTMLElement && active.isContentEditable);

      const isMod = e.metaKey || e.ctrlKey;

      // Ctrl/Cmd + S works even when focused inside inputs
      if (isMod && !e.altKey && e.key.toLowerCase() === "s") {
        e.preventDefault();
        onSaveProject?.();
        return;
      }

      if (isTextInput) return;

      // Ctrl/Cmd + B (CapCut Split), C (Copy), V (Paste), D (Duplicate)
      if (isMod && !e.altKey) {
        const k = e.key.toLowerCase();
        if (k === "b") {
          if (splitTarget) {
            e.preventDefault();
            handleSplitAtPlayhead();
          }
          return;
        }
        if (k === "c") {
          const hasTextSelection = Boolean(window.getSelection()?.toString());
          const hasClipSelection =
            selection?.type === "video-clip" ||
            selection?.type === "audio-clip" ||
            selection?.type === "caption-clip";
          if (!hasTextSelection && hasClipSelection) {
            e.preventDefault();
            onCopySelectedClip?.();
          }
          return;
        }
        if (k === "v") {
          e.preventDefault();
          onPasteClipAtPlayhead?.();
          return;
        }
        if (k === "d") {
          e.preventDefault();
          onDuplicateSelectedClip?.();
          return;
        }
        return;
      }

      if (e.altKey) return;

      const fpsVal = Math.max(1, project.output_settings.fps || 30);
      const frameStep = 1 / fpsVal;

      if (e.code === "Space" || e.key === " ") {
        e.preventDefault();
        if (!e.repeat) {
          onToggleProjectPlayback();
        }
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        const step = e.shiftKey ? frameStep * 10 : frameStep;
        onProjectTimeChange(Number(Math.max(0, projectTime - step).toFixed(3)));
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        const step = e.shiftKey ? frameStep * 10 : frameStep;
        onProjectTimeChange(
          Number(Math.min(Math.max(rawDuration, timelineSpan), projectTime + step).toFixed(3))
        );
      } else if (e.key === "Home") {
        e.preventDefault();
        onProjectTimeChange(0);
      } else if (e.key === "End") {
        e.preventDefault();
        onProjectTimeChange(Number(rawDuration.toFixed(3)));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        const cuts = findCutPoints(project, manifest);
        const prevCut = [...cuts].reverse().find((c) => c < projectTime - 0.02);
        onProjectTimeChange(prevCut !== undefined ? prevCut : 0);
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        const cuts = findCutPoints(project, manifest);
        const nextCut = cuts.find((c) => c > projectTime + 0.02);
        if (nextCut !== undefined) onProjectTimeChange(nextCut);
      } else if (e.key === "i" || e.key === "I") {
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
        if (splitTarget) {
          e.preventDefault();
          handleSplitAtPlayhead();
        }
      } else if (e.key === "n" || e.key === "N") {
        e.preventDefault();
        setSnapEnabled((v) => {
          const next = !v;
          onToast?.(`Magnetic snapping ${next ? "ON" : "OFF"}`, "info");
          return next;
        });
      } else if (e.key === "m" || e.key === "M") {
        e.preventDefault();
        handleAddMarkerAt(projectTime);
      } else if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        applyZoom(timelineZoom * 1.4);
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        applyZoom(timelineZoom / 1.4);
      } else if (e.key === "?") {
        e.preventDefault();
        onOpenShortcuts?.();
      } else if (e.key === "Backspace" || e.key === "Delete") {
        if (multiSelected.length > 1) {
          e.preventDefault();
          handleBatchDeleteMulti();
          return;
        }
        if (
          e.shiftKey &&
          selection &&
          (selection.type === "video-clip" ||
            selection.type === "audio-clip" ||
            selection.type === "caption-clip")
        ) {
          e.preventDefault();
          handleRippleDelete(selection.trackId, selection.clipId);
          return;
        }
        if (
          selection &&
          (selection.type === "video-clip" ||
            selection.type === "audio-clip" ||
            selection.type === "caption-clip" ||
            selection.type === "transition" ||
            selection.type === "keyframe")
        ) {
          e.preventDefault();
          deleteSelectedOrSpecified(selection);
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  });

  const timeFromClientX = (clientX: number): number | null => {
    const rect = trackLaneRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return null;
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return ratio * timelineSpan;
  };

  const findHoveredTrackId = (clientY: number, kind: TimelineTrack["kind"]): string | null => {
    const container = trackLaneRef.current;
    if (!container) return null;
    const rows = container.querySelectorAll<HTMLElement>(`[data-track-kind="${kind}"]`);
    for (const row of rows) {
      const r = row.getBoundingClientRect();
      if (clientY >= r.top && clientY <= r.bottom) {
        return row.dataset.trackId ?? null;
      }
    }
    return null;
  };

  const scrubAtClientX = (clientX: number) => {
    const t = timeFromClientX(clientX);
    if (t === null) return;
    onProjectTimeChange(Number(Math.max(0, Math.min(timelineSpan, t)).toFixed(3)));
  };

  // Dragging interaction loop
  useEffect(() => {
    if (!drag) return;

    const snapThreshold = Math.max(0.08, (timelineSpan * 0.015) / timelineZoom);

    const handleMove = (e: MouseEvent) => {
      if (drag.type === "height-resize") {
        const dy = drag.startY - e.clientY;
        setTimelineHeightPx(Math.max(150, Math.min(540, Math.round(drag.origHeight + dy))));
        return;
      }

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
            const targetScroll = frac * (w * nextZoom) - pointerOffset - dx;
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

      const rect = trackLaneRef.current?.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;
      const dxSec = ((e.clientX - drag.startX) / rect.width) * drag.spanSec;
      const useSnap = snapEnabled && !e.shiftKey;

      if (drag.type === "keyframe-move") {
        const rawT = Math.max(0, drag.origT + dxSec);
        const minAllowed = drag.index === 0 ? 0 : 0.02;
        const nextT = useSnap
          ? snapTimelineTime(
              Math.max(minAllowed, rawT),
              collectTimelineSnapCandidates(project, manifest, undefined, [projectTime]),
              snapThreshold
            )
          : Number(Math.max(minAllowed, rawT).toFixed(2));
        const movingKf = cameraKeyframes[drag.index];
        if (!movingKf) return;
        const nextKfs = cameraKeyframes
          .map((kf, idx) => (idx === drag.index ? { ...kf, t: nextT } : kf))
          .sort((a, b) => a.t - b.t);
        const newIdx = nextKfs.indexOf(
          nextKfs.find((k) => Math.abs(k.t - nextT) < 1e-4 && k.x === movingKf.x && k.y === movingKf.y) ?? nextKfs[0]
        );
        onUpdateCameraKeyframes(nextKfs);
        if (newIdx >= 0) {
          onSelectItem({ type: "keyframe", index: newIdx });
        }
        return;
      }

      if (
        drag.type === "vclip-move" ||
        drag.type === "vclip-left" ||
        drag.type === "vclip-right" ||
        drag.type === "vclip-slip"
      ) {
        const hoveredTrackId =
          drag.type === "vclip-move" ? findHoveredTrackId(e.clientY, "video") : null;
        const targetTrack =
          hoveredTrackId &&
          !tracks.find((t) => t.id === hoveredTrackId)?.locked
            ? hoveredTrackId
            : drag.trackId;
        const candidates = useSnap
          ? collectTimelineSnapCandidates(project, manifest, drag.clipId, [projectTime])
          : [];

        onUpdateTracks(
          (prevTracks) => {
            let movedClip: VisualClip | null = null;
            for (const tr of prevTracks) {
              if (tr.kind === "video" && tr.id === drag.trackId) {
                movedClip = tr.clips.find((c) => c.id === drag.clipId) ?? null;
                break;
              }
            }
            if (!movedClip) return prevTracks;

            let updatedClip: VisualClip = { ...movedClip };
            if (drag.type === "vclip-move") {
              const rawStart = Math.max(0, drag.origStart + dxSec);
              let nextStart = useSnap
                ? snapTimelineTime(rawStart, candidates, snapThreshold)
                : Number(rawStart.toFixed(3));
              let snappedAt: number | null =
                useSnap && Math.abs(nextStart - rawStart) > 1e-4 ? nextStart : null;
              // Also allow snapping the tail edge when dragging right edge near a candidate
              if (useSnap && snappedAt === null) {
                const rawEnd = rawStart + drag.origDur;
                const snappedEnd = snapTimelineTime(rawEnd, candidates, snapThreshold);
                if (Math.abs(snappedEnd - rawEnd) > 1e-4) {
                  nextStart = Math.max(0, Number((snappedEnd - drag.origDur).toFixed(3)));
                  snappedAt = snappedEnd;
                }
              }
              setSnapGuideTime(snappedAt);
              updatedClip = { ...movedClip, start_sec: nextStart };
            } else if (drag.type === "vclip-left") {
              const origEnd = drag.origStart + drag.origDur;
              const minStart =
                drag.kind === "video"
                  ? Math.max(0, drag.origStart - drag.origMediaIn / drag.speed)
                  : 0;
              const maxStart = origEnd - 0.1;
              const rawStart = Math.max(minStart, Math.min(maxStart, drag.origStart + dxSec));
              const nextStart = useSnap
                ? Math.max(minStart, Math.min(maxStart, snapTimelineTime(rawStart, candidates, snapThreshold)))
                : Number(rawStart.toFixed(3));
              const deltaTimeline = nextStart - drag.origStart;
              const nextDur = Number(Math.max(0.1, origEnd - nextStart).toFixed(3));
              const nextMediaIn =
                drag.kind === "video"
                  ? Number(Math.max(0, drag.origMediaIn + deltaTimeline * drag.speed).toFixed(3))
                  : 0;
              updatedClip = {
                ...movedClip,
                start_sec: nextStart,
                duration_sec: nextDur,
                media_in: nextMediaIn,
              };
            } else if (drag.type === "vclip-right") {
              const rawEnd = Math.max(drag.origStart + 0.1, drag.origStart + drag.origDur + dxSec);
              const snappedEnd = useSnap
                ? Math.max(drag.origStart + 0.1, snapTimelineTime(rawEnd, candidates, snapThreshold))
                : Number(rawEnd.toFixed(3));
              const nextDur = Number((snappedEnd - drag.origStart).toFixed(3));
              let nextMediaOut = drag.origMediaOut;
              if (drag.kind === "video" && drag.assetDur > 0 && drag.origMediaOut !== null) {
                nextMediaOut = Number(
                  Math.min(drag.assetDur, Math.max(drag.origMediaIn + 0.1, drag.origMediaIn + nextDur * drag.speed)).toFixed(3)
                );
              }
              updatedClip = {
                ...movedClip,
                duration_sec: nextDur,
                media_out: nextMediaOut,
              };
            } else if (drag.type === "vclip-slip" && drag.kind === "video") {
              const srcLen =
                drag.origMediaOut !== null
                  ? drag.origMediaOut - drag.origMediaIn
                  : drag.origDur * drag.speed;
              const maxIn = Math.max(0, drag.assetDur - srcLen);
              const nextIn = Number(
                Math.max(0, Math.min(maxIn, drag.origMediaIn + dxSec)).toFixed(3)
              );
              updatedClip = {
                ...movedClip,
                media_in: nextIn,
                media_out:
                  drag.origMediaOut !== null
                    ? Number((nextIn + srcLen).toFixed(3))
                    : null,
              };
            }

            if (targetTrack === drag.trackId) {
              return prevTracks.map((tr) => {
                if (tr.kind !== "video" || tr.id !== drag.trackId) return tr;
                return {
                  ...tr,
                  clips: tr.clips
                    .map((c) => (c.id === drag.clipId ? updatedClip : c))
                    .sort((a, b) => a.start_sec - b.start_sec),
                };
              });
            }

            // Cross-track move
            drag.trackId = targetTrack;
            return prevTracks.map((tr) => {
              if (tr.kind !== "video") return tr;
              if (tr.id === targetTrack) {
                return {
                  ...tr,
                  clips: [...tr.clips.filter((c) => c.id !== drag.clipId), updatedClip].sort(
                    (a, b) => a.start_sec - b.start_sec
                  ),
                };
              }
              return {
                ...tr,
                clips: tr.clips.filter((c) => c.id !== drag.clipId),
                transitions: tr.transitions.filter(
                  (jt) => jt.from_clip_id !== drag.clipId && jt.to_clip_id !== drag.clipId
                ),
              };
            });
          },
          undefined,
          { type: "video-clip", trackId: targetTrack, clipId: drag.clipId }
        );
        return;
      }

      if (
        drag.type === "aclip-move" ||
        drag.type === "aclip-left" ||
        drag.type === "aclip-right" ||
        drag.type === "aclip-fade-in" ||
        drag.type === "aclip-fade-out"
      ) {
        const hoveredTrackId =
          drag.type === "aclip-move" ? findHoveredTrackId(e.clientY, "audio") : null;
        const targetTrack =
          hoveredTrackId &&
          !tracks.find((t) => t.id === hoveredTrackId)?.locked
            ? hoveredTrackId
            : drag.trackId;
        const candidates = useSnap
          ? collectTimelineSnapCandidates(project, manifest, drag.clipId, [projectTime])
          : [];

        onUpdateTracks(
          (prevTracks) => {
            let movedClip: AudioClip | null = null;
            for (const tr of prevTracks) {
              if (tr.kind === "audio" && tr.id === drag.trackId) {
                movedClip = tr.clips.find((c) => c.id === drag.clipId) ?? null;
                break;
              }
            }
            if (!movedClip) return prevTracks;

            let updated: AudioClip = { ...movedClip };
            if (drag.type === "aclip-move") {
              const rawStart = Math.max(0, drag.origStart + dxSec);
              const nextStart = useSnap
                ? snapTimelineTime(rawStart, candidates, snapThreshold)
                : Number(rawStart.toFixed(3));
              setSnapGuideTime(
                useSnap && Math.abs(nextStart - rawStart) > 1e-4 ? nextStart : null
              );
              updated = { ...movedClip, start_sec: nextStart };
            } else if (drag.type === "aclip-left") {
              const origEnd = drag.origStart + drag.origDur;
              const minStart = Math.max(0, drag.origStart - drag.origMediaIn / drag.speed);
              const maxStart = origEnd - 0.1;
              const rawStart = Math.max(minStart, Math.min(maxStart, drag.origStart + dxSec));
              const nextStart = useSnap
                ? Math.max(minStart, Math.min(maxStart, snapTimelineTime(rawStart, candidates, snapThreshold)))
                : Number(rawStart.toFixed(3));
              const delta = nextStart - drag.origStart;
              updated = {
                ...movedClip,
                start_sec: nextStart,
                duration_sec: Number(Math.max(0.1, origEnd - nextStart).toFixed(3)),
                media_in: Number(Math.max(0, drag.origMediaIn + delta * drag.speed).toFixed(3)),
              };
            } else if (drag.type === "aclip-fade-in") {
              const nextFade = Number(
                Math.max(0, Math.min(drag.origDur * 0.5, (drag.origFade ?? 0) + dxSec)).toFixed(2)
              );
              updated = { ...movedClip, fade_in_sec: nextFade };
            } else if (drag.type === "aclip-fade-out") {
              const nextFade = Number(
                Math.max(0, Math.min(drag.origDur * 0.5, (drag.origFade ?? 0) - dxSec)).toFixed(2)
              );
              updated = { ...movedClip, fade_out_sec: nextFade };
            } else {
              const rawEnd = Math.max(drag.origStart + 0.1, drag.origStart + drag.origDur + dxSec);
              const snappedEnd = useSnap
                ? Math.max(drag.origStart + 0.1, snapTimelineTime(rawEnd, candidates, snapThreshold))
                : Number(rawEnd.toFixed(3));
              updated = {
                ...movedClip,
                duration_sec: Number((snappedEnd - drag.origStart).toFixed(3)),
              };
            }

            if (targetTrack === drag.trackId) {
              return prevTracks.map((tr) =>
                tr.kind === "audio" && tr.id === drag.trackId
                  ? {
                      ...tr,
                      clips: tr.clips
                        .map((c) => (c.id === drag.clipId ? updated : c))
                        .sort((a, b) => a.start_sec - b.start_sec),
                    }
                  : tr
              );
            }
            drag.trackId = targetTrack;
            return prevTracks.map((tr) => {
              if (tr.kind !== "audio") return tr;
              if (tr.id === targetTrack) {
                return {
                  ...tr,
                  clips: [...tr.clips.filter((c) => c.id !== drag.clipId), updated].sort(
                    (a, b) => a.start_sec - b.start_sec
                  ),
                };
              }
              return { ...tr, clips: tr.clips.filter((c) => c.id !== drag.clipId) };
            });
          },
          undefined,
          { type: "audio-clip", trackId: targetTrack, clipId: drag.clipId }
        );
        return;
      }

      if (drag.type === "cclip-move" || drag.type === "cclip-left" || drag.type === "cclip-right") {
        const hoveredTrackId =
          drag.type === "cclip-move" ? findHoveredTrackId(e.clientY, "caption") : null;
        const targetTrack =
          hoveredTrackId &&
          !tracks.find((t) => t.id === hoveredTrackId)?.locked
            ? hoveredTrackId
            : drag.trackId;
        const candidates = useSnap
          ? collectTimelineSnapCandidates(project, manifest, drag.clipId, [projectTime])
          : [];

        onUpdateTracks(
          (prevTracks) => {
            let movedClip: CaptionClip | null = null;
            for (const tr of prevTracks) {
              if (tr.kind === "caption" && tr.id === drag.trackId) {
                movedClip = tr.clips.find((c) => c.id === drag.clipId) ?? null;
                break;
              }
            }
            if (!movedClip) return prevTracks;

            let updated: CaptionClip = { ...movedClip };
            if (drag.type === "cclip-move") {
              const rawStart = Math.max(0, drag.origStart + dxSec);
              const nextStart = useSnap
                ? snapTimelineTime(rawStart, candidates, snapThreshold)
                : Number(rawStart.toFixed(3));
              setSnapGuideTime(
                useSnap && Math.abs(nextStart - rawStart) > 1e-4 ? nextStart : null
              );
              updated = { ...movedClip, start_sec: nextStart };
            } else if (drag.type === "cclip-left") {
              const origEnd = drag.origStart + drag.origDur;
              const maxStart = origEnd - 0.2;
              const rawStart = Math.max(0, Math.min(maxStart, drag.origStart + dxSec));
              const nextStart = useSnap
                ? Math.max(0, Math.min(maxStart, snapTimelineTime(rawStart, candidates, snapThreshold)))
                : Number(rawStart.toFixed(3));
              updated = {
                ...movedClip,
                start_sec: nextStart,
                auto_fit_tts: false,
                duration_sec: Number(Math.max(0.2, origEnd - nextStart).toFixed(3)),
              };
            } else {
              const rawEnd = Math.max(drag.origStart + 0.2, drag.origStart + drag.origDur + dxSec);
              const snappedEnd = useSnap
                ? Math.max(drag.origStart + 0.2, snapTimelineTime(rawEnd, candidates, snapThreshold))
                : Number(rawEnd.toFixed(3));
              updated = {
                ...movedClip,
                auto_fit_tts: false,
                duration_sec: Number((snappedEnd - drag.origStart).toFixed(3)),
              };
            }

            if (targetTrack === drag.trackId) {
              return prevTracks.map((tr) =>
                tr.kind === "caption" && tr.id === drag.trackId
                  ? {
                      ...tr,
                      clips: tr.clips
                        .map((c) => (c.id === drag.clipId ? updated : c))
                        .sort((a, b) => a.start_sec - b.start_sec),
                    }
                  : tr
              );
            }
            drag.trackId = targetTrack;
            return prevTracks.map((tr) => {
              if (tr.kind !== "caption") return tr;
              if (tr.id === targetTrack) {
                return {
                  ...tr,
                  clips: [...tr.clips.filter((c) => c.id !== drag.clipId), updated].sort(
                    (a, b) => a.start_sec - b.start_sec
                  ),
                };
              }
              return { ...tr, clips: tr.clips.filter((c) => c.id !== drag.clipId) };
            });
          },
          undefined,
          { type: "caption-clip", trackId: targetTrack, clipId: drag.clipId }
        );
      }
    };

    const handleUp = () => {
      if (
        drag.type !== "scrub" &&
        drag.type !== "zoom-drag" &&
        drag.type !== "height-resize"
      ) {
        onEndHistoryTransaction?.();
      }
      setSnapGuideTime(null);
      setDrag(null);
    };

    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
    };
  });

  const handleAddClipFromSource = () => {
    const safeIn = Number(Math.max(0, Math.min(markIn, sourceDur - 0.1)).toFixed(2));
    const safeOut = Number(Math.max(safeIn + 0.1, Math.min(markOut, sourceDur)).toFixed(2));
    onAddSourceClip(activeSrcId, safeIn, safeOut);
  };

  const handleAddCaptionAt = (targetTrackId?: string, startSec?: number) => {
    let destTrack = captionTracks.find((t) => t.id === targetTrackId);
    if (!destTrack && selection && "trackId" in selection) {
      destTrack = captionTracks.find((t) => t.id === selection.trackId);
    }
    if (!destTrack) {
      destTrack = captionTracks[0];
    }

    const newId = findNextClipId(project, "cap");
    const targetStart = Number(Math.max(0, startSec ?? projectTime).toFixed(3));
    const newClip: CaptionClip = {
      id: newId,
      start_sec: targetStart,
      duration_sec: 2.0,
      auto_fit_tts: true,
      display_text: "",
      spoken_text_override: null,
      tts_enabled: true,
      speed: 1.0,
      voice_override: null,
      style_override: null,
    };

    if (!destTrack) {
      const trackId = findNextTrackId(project, "t");
      const createdTrack: CaptionTrack = {
        id: trackId,
        name: "Captions 1",
        kind: "caption",
        visible: true,
        muted: false,
        locked: false,
        clips: [newClip],
      };
      onUpdateTracks(
        (prev) => [...prev, createdTrack],
        { label: `Add caption ${newId}` },
        { type: "caption-clip", trackId, clipId: newId }
      );
      onProjectTimeChange(targetStart);
      return;
    }

    const chosenTrackId = destTrack.id;
    onUpdateTracks(
      (prev) =>
        prev.map((tr) =>
          tr.kind === "caption" && tr.id === chosenTrackId
            ? {
                ...tr,
                clips: [...tr.clips, newClip].sort((a, b) => a.start_sec - b.start_sec),
              }
            : tr
        ),
      { label: `Add caption ${newId}` },
      { type: "caption-clip", trackId: chosenTrackId, clipId: newId }
    );
    onProjectTimeChange(targetStart);
  };

  const handleAddCameraKeyframeAt = (tRaw?: number) => {
    const clickedT = tRaw ?? projectTime;
    const globalT = Number(Math.max(0, clickedT).toFixed(2));
    const existingIdx = cameraKeyframes.findIndex((kf) => Math.abs(kf.t - globalT) < 0.05);
    if (existingIdx >= 0) {
      onSelectItem({ type: "keyframe", index: existingIdx });
      onProjectTimeChange(cameraKeyframes[existingIdx].t);
      return;
    }
    const lens = evaluateCropAt(
      cameraKeyframes,
      globalT,
      srcW,
      srcH,
      project.output_settings.width,
      project.output_settings.height
    );
    const newKf: CropKeyframe = {
      t: globalT,
      x: Number(lens.x.toFixed(4)),
      y: Number(lens.y.toFixed(4)),
      zoom: Number(lens.zoom.toFixed(3)),
      transition: "tween",
    };
    const nextKfs = [...cameraKeyframes, newKf].sort((a, b) => a.t - b.t);
    const newIdx = nextKfs.indexOf(newKf);
    onUpdateCameraKeyframes(nextKfs, { label: `Add camera keyframe @${globalT.toFixed(2)}s` });
    onSelectItem({ type: "keyframe", index: Math.max(0, newIdx) });
    onProjectTimeChange(globalT);
  };

  const handleAddTrack = (kind: TimelineTrack["kind"]) => {
    if (kind === "video") {
      const id = findNextTrackId(project, "v");
      const count = videoTracks.length + 1;
      const newTrack: VideoTrack = {
        id,
        name: count > 1 ? `Overlay ${count - 1}` : "Video 1",
        kind: "video",
        visible: true,
        muted: false,
        locked: false,
        camera_follow: count === 1,
        clips: [],
        transitions: [],
      };
      onUpdateTracks(
        (prev) => [...prev, newTrack],
        { label: `Add track ${newTrack.name}` },
        { type: "track", trackId: id }
      );
    } else if (kind === "caption") {
      const id = findNextTrackId(project, "t");
      const newTrack: CaptionTrack = {
        id,
        name: `Captions ${captionTracks.length + 1}`,
        kind: "caption",
        visible: true,
        muted: false,
        locked: false,
        clips: [],
      };
      onUpdateTracks(
        (prev) => [...prev, newTrack],
        { label: `Add track ${newTrack.name}` },
        { type: "track", trackId: id }
      );
    } else {
      const id = findNextTrackId(project, "a");
      const newTrack: AudioTrack = {
        id,
        name: `Audio ${audioTracks.length + 1}`,
        kind: "audio",
        volume: 1.0,
        muted: false,
        solo: false,
        duck_under_narration: true,
        locked: false,
        clips: [],
      };
      onUpdateTracks(
        (prev) => [...prev, newTrack],
        { label: `Add track ${newTrack.name}` },
        { type: "track", trackId: id }
      );
    }
  };

  const handleDeleteTrack = (track: TimelineTrack) => {
    const sameKindCount = tracks.filter((t) => t.kind === track.kind).length;
    if (sameKindCount <= 1) return;
    onUpdateTracks(
      (prev) => prev.filter((t) => t.id !== track.id),
      { label: `Delete track ${track.name}` },
      selection && "trackId" in selection && selection.trackId === track.id ? null : selection
    );
  };

  const handleJunctionClick = (
    track: VideoTrack,
    fromClip: VisualClip,
    toClip: VisualClip,
    existing: TrackTransition | null
  ) => {
    if (existing) {
      onSelectItem({ type: "transition", trackId: track.id, transitionId: existing.id });
      return;
    }
    const maxAllowed = Math.max(0.05, Math.min(0.5, fromClip.duration_sec * 0.4, toClip.duration_sec * 0.4));
    const newTr: TrackTransition = {
      id: `tr_${fromClip.id}_${toClip.id}_${Date.now().toString(36)}`,
      from_clip_id: fromClip.id,
      to_clip_id: toClip.id,
      type: "crossfade",
      duration_sec: Number(maxAllowed.toFixed(2)),
    };
    onUpdateTracks(
      (prev) =>
        prev.map((tr) =>
          tr.kind === "video" && tr.id === track.id
            ? { ...tr, transitions: [...tr.transitions, newTr] }
            : tr
        ),
      { label: `Add ${newTr.type} transition` },
      { type: "transition", trackId: track.id, transitionId: newTr.id }
    );
  };

  const fps = Math.max(1, project.output_settings.fps || 30);
  const totalFrames = Math.max(1, Math.round(rawDuration * fps));
  const currentFrame = Math.round(projectTime * fps);
  const playheadPct = Math.max(0, Math.min(100, (clampedPlayhead / timelineSpan) * 100));

  const tickCount = Math.max(5, Math.min(40, Math.round(5 * timelineZoom)));
  const ticks = Array.from({ length: tickCount + 1 }, (_, i) => {
    const frac = i / tickCount;
    const t = frac * timelineSpan;
    return { frac, t, f: Math.round(t * fps) };
  });

  const handleAssetDragOver = (e: React.DragEvent, track: TimelineTrack) => {
    if (track.locked) return;
    if (e.dataTransfer.types.includes(MEDIA_BIN_DND_MIME)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      if (dragOverTrackId !== track.id) {
        setDragOverTrackId(track.id);
      }
    }
  };

  const handleAssetDrop = (e: React.DragEvent, track: TimelineTrack) => {
    setDragOverTrackId(null);
    if (track.locked) return;
    const raw = e.dataTransfer.getData(MEDIA_BIN_DND_MIME);
    if (!raw) return;
    try {
      const parsed = JSON.parse(raw) as { assetId?: string; kind?: MediaAssetKind };
      if (!parsed.assetId || !parsed.kind) return;
      if (track.kind === "video" && parsed.kind === "audio") return;
      if (track.kind === "audio" && parsed.kind === "image") return;
      if (track.kind === "caption") return;
      e.preventDefault();
      const rawT = timeFromClientX(e.clientX) ?? projectTime;
      const dropT = snapEnabled
        ? snapTimelineTime(
            rawT,
            collectTimelineSnapCandidates(project, manifest, undefined, [projectTime]),
            Math.max(0.1, (timelineSpan * 0.015) / timelineZoom)
          )
        : Number(Math.max(0, rawT).toFixed(3));
      onDropMediaAsset(parsed.assetId, parsed.kind, track.id, dropT);
    } catch {
      // Ignore malformed DnD payload
    }
  };

  return (
    <section
      data-testid="timeline-panel"
      className="relative flex shrink-0 flex-col rounded-xl border border-zinc-800 bg-[#151821]"
    >
      {/* Draggable Timeline Height Splitter */}
      <div
        onMouseDown={(e) => {
          e.preventDefault();
          startDrag({
            type: "height-resize",
            startY: e.clientY,
            origHeight: timelineHeightPx,
          });
        }}
        title="Drag up/down to resize timeline height"
        className="flex h-2.5 cursor-ns-resize items-center justify-center rounded-t-xl border-b border-zinc-800/70 bg-zinc-900/70 text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300"
      >
        <GripHorizontal className="h-3 w-3" />
      </div>

      {/* Top Timeline Controls */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 px-3 py-2 text-xs">
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            onClick={onToggleProjectPlayback}
            disabled={rawDuration <= 0 || preparingPlayback}
            title="Play / Stop Project (Space)"
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

          <button
            type="button"
            disabled={!splitTarget}
            onClick={handleSplitAtPlayhead}
            title="Split clip at playhead (S or Ctrl+B)"
            className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-[11px] font-medium text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
          >
            <Scissors className="h-3 w-3" />
            Split (S)
          </button>

          <button
            type="button"
            onClick={() => handleAddCaptionAt()}
            className="inline-flex items-center gap-1 rounded border border-sky-500/40 bg-sky-500/15 px-2 py-1 text-[11px] font-medium text-sky-200 hover:bg-sky-500/25"
          >
            <Plus className="h-3 w-3" />
            Caption / VO
          </button>

          {onOpenBulkScript && (
            <button
              type="button"
              onClick={onOpenBulkScript}
              title="Paste full script and auto-split into timed captions"
              className="inline-flex items-center gap-1 rounded border border-sky-500/30 bg-sky-500/10 px-2 py-1 text-[11px] font-medium text-sky-200 hover:bg-sky-500/20"
            >
              <FileText className="h-3 w-3" />
              Script
            </button>
          )}

          <button
            type="button"
            onClick={() => handleAddCameraKeyframeAt()}
            className="inline-flex items-center gap-1 rounded border border-amber-500/40 bg-amber-500/15 px-2 py-1 text-[11px] font-medium text-amber-200 hover:bg-amber-500/25"
          >
            <Plus className="h-3 w-3" />
            Keyframe
          </button>

          {onUpdateMarkers && (
            <button
              type="button"
              onClick={() => handleAddMarkerAt(projectTime)}
              title="Drop timeline marker at playhead (M)"
              className="inline-flex items-center gap-1 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] font-medium text-amber-200 hover:bg-amber-500/20"
            >
              <Bookmark className="h-3 w-3" />
              Marker
            </button>
          )}

          <button
            type="button"
            onClick={() => setSnapEnabled((v) => !v)}
            title="Toggle magnetic timeline snapping (N, or hold Shift while dragging to bypass)"
            className={`inline-flex items-center gap-1 rounded border px-2 py-1 text-[11px] font-medium ${
              snapEnabled
                ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-200"
                : "border-zinc-700 bg-zinc-800 text-zinc-400"
            }`}
          >
            <Magnet className="h-3 w-3" />
            Snap
          </button>

          {multiSelected.length > 1 && (
            <button
              type="button"
              onClick={handleBatchDeleteMulti}
              title="Delete all Shift-selected clips"
              className="inline-flex items-center gap-1 rounded border border-rose-500/50 bg-rose-500/20 px-2 py-1 text-[11px] font-medium text-rose-200 hover:bg-rose-500/30"
            >
              <Trash2 className="h-3 w-3" />
              Delete ({multiSelected.length})
            </button>
          )}

          <div className="flex items-center gap-1 border-l border-zinc-800 pl-1.5">
            <button
              type="button"
              onClick={() => handleAddTrack("video")}
              title="Add Video / Overlay Layer Track"
              className="inline-flex items-center gap-0.5 rounded border border-zinc-700 bg-zinc-800/80 px-1.5 py-1 text-[10px] text-zinc-200 hover:bg-zinc-700"
            >
              <Layers className="h-3 w-3 text-purple-400" />
              +V
            </button>
            <button
              type="button"
              onClick={() => handleAddTrack("caption")}
              title="Add Caption / Narration Track"
              className="inline-flex items-center gap-0.5 rounded border border-zinc-700 bg-zinc-800/80 px-1.5 py-1 text-[10px] text-zinc-200 hover:bg-zinc-700"
            >
              <Captions className="h-3 w-3 text-sky-400" />
              +T
            </button>
            <button
              type="button"
              onClick={() => handleAddTrack("audio")}
              title="Add Audio Track"
              className="inline-flex items-center gap-0.5 rounded border border-zinc-700 bg-zinc-800/80 px-1.5 py-1 text-[10px] text-zinc-200 hover:bg-zinc-700"
            >
              <Music className="h-3 w-3 text-amber-400" />
              +A
            </button>
          </div>
        </div>

        {/* Source Capture Scrubber + Add Clip */}
        <div className="flex flex-wrap items-center gap-1.5">
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
            className="w-24 sm:w-32"
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
            onClick={handleAddClipFromSource}
            className="inline-flex items-center gap-1 rounded border border-emerald-500/40 bg-emerald-500/15 px-2 py-1 text-[11px] font-medium text-emerald-200 hover:bg-emerald-500/25"
          >
            <Plus className="h-3 w-3" />
            Add Clip
          </button>
        </div>
      </div>

      {/* Multi-Track NLE Area (always visible — supports 0 video clips & arbitrary gaps) */}
      <div className="bg-[#11141b] px-3 py-2 space-y-1.5">
        <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
          <div className="flex items-center gap-2 font-mono text-zinc-400">
            <span>
              {formatTimecode(projectTime)} / {formatTimecode(rawDuration)} •{" "}
              <span className="text-emerald-300">
                f{currentFrame} / {totalFrames}f
              </span>{" "}
              ({fps}fps)
            </span>
          </div>

          <div className="flex items-center gap-3">
            <span className="hidden xl:inline text-[10px] text-zinc-500">
              Drag clips freely (gaps supported) • Alt+Drag to slip • Drop assets from Bin • Double-click Camera/Captions to insert
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

        {/* Vertically Scrollable Synchronized Track Headers + Track Lanes */}
        <div
          className="flex gap-2 overflow-y-auto select-none pr-0.5"
          style={{ maxHeight: `${timelineHeightPx}px` }}
        >
          {/* Left Sticky Track Controls Column */}
          <div className="w-44 shrink-0 space-y-1.5 text-[10px] font-medium text-zinc-400">
            {/* Spacer aligning with DAW Zoom strip (h-3.5) + Ruler (h-5) */}
            <div className="flex h-[34px] items-end pb-1 text-[9px] uppercase tracking-wider text-zinc-500">
              Tracks ({tracks.length + 1})
            </div>

            {/* Video / Overlay Track Headers */}
            {displayVideoTracks.map((tr, dispIdx) => {
              const isSelected =
                selection?.type === "track" && selection.trackId === tr.id;
              const isHighest = dispIdx === 0;
              const isLowest = dispIdx === displayVideoTracks.length - 1;
              return (
                <div
                  key={`hdr-${tr.id}`}
                  onClick={() => onSelectItem({ type: "track", trackId: tr.id })}
                  className={`flex h-8 cursor-pointer items-center justify-between gap-1 rounded border px-1.5 ${
                    isSelected
                      ? "border-emerald-500/60 bg-emerald-500/15 text-emerald-100"
                      : "border-zinc-800 bg-zinc-900/70 hover:border-zinc-700"
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-1">
                    <Film className="h-3 w-3 shrink-0 text-emerald-400" />
                    <span className="truncate" title={tr.name}>
                      {tr.name}
                    </span>
                  </div>
                  <div
                    className="flex items-center gap-0.5"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      type="button"
                      disabled={isHighest}
                      onClick={() =>
                        onUpdateTracks(
                          (prev) => reorderVideoTrackZ(prev, tr.id, "higher"),
                          { label: `Move ${tr.name} layer up` }
                        )
                      }
                      title="Move visual layer up (renders in front)"
                      className="rounded p-0.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-25"
                    >
                      <ArrowUp className="h-2.5 w-2.5" />
                    </button>
                    <button
                      type="button"
                      disabled={isLowest}
                      onClick={() =>
                        onUpdateTracks(
                          (prev) => reorderVideoTrackZ(prev, tr.id, "lower"),
                          { label: `Move ${tr.name} layer down` }
                        )
                      }
                      title="Move visual layer down (renders behind)"
                      className="rounded p-0.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-25"
                    >
                      <ArrowDown className="h-2.5 w-2.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id && t.kind === "video"
                                ? { ...t, camera_follow: !t.camera_follow }
                                : t
                            ),
                          { label: `Toggle camera follow on ${tr.name}` }
                        )
                      }
                      title={
                        tr.camera_follow
                          ? "Camera Follow ON (crops to 9:16 lens)"
                          : "Camera Follow OFF (free canvas overlay/PiP)"
                      }
                      className={`rounded px-1 text-[9px] font-bold ${
                        tr.camera_follow
                          ? "bg-amber-500/20 text-amber-300"
                          : "bg-purple-500/20 text-purple-300"
                      }`}
                    >
                      {tr.camera_follow ? "CAM" : "OVR"}
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id && t.kind === "video"
                                ? { ...t, visible: !t.visible }
                                : t
                            ),
                          { label: `Toggle visibility on ${tr.name}` }
                        )
                      }
                      title={tr.visible ? "Hide track" : "Show track"}
                      className="rounded p-0.5 hover:bg-zinc-800"
                    >
                      {tr.visible ? (
                        <Eye className="h-3 w-3 text-zinc-300" />
                      ) : (
                        <EyeOff className="h-3 w-3 text-zinc-600" />
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id && t.kind === "video"
                                ? { ...t, muted: !t.muted }
                                : t
                            ),
                          { label: `Toggle mute on ${tr.name}` }
                        )
                      }
                      title={tr.muted ? "Unmute track audio" : "Mute track audio"}
                      className="rounded p-0.5 hover:bg-zinc-800"
                    >
                      {tr.muted ? (
                        <VolumeX className="h-3 w-3 text-rose-400" />
                      ) : (
                        <Volume2 className="h-3 w-3 text-zinc-400" />
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id ? { ...t, locked: !t.locked } : t
                            ),
                          { label: `${tr.locked ? "Unlock" : "Lock"} ${tr.name}` }
                        )
                      }
                      title={tr.locked ? "Unlock track" : "Lock track"}
                      className="rounded p-0.5 hover:bg-zinc-800"
                    >
                      {tr.locked ? (
                        <Lock className="h-2.5 w-2.5 text-amber-400" />
                      ) : (
                        <Unlock className="h-2.5 w-2.5 text-zinc-500" />
                      )}
                    </button>
                    {videoTracks.length > 1 && (
                      <button
                        type="button"
                        onClick={() => handleDeleteTrack(tr)}
                        title={`Delete track ${tr.name}`}
                        className="rounded p-0.5 text-zinc-500 hover:text-rose-400"
                      >
                        <Trash2 className="h-2.5 w-2.5" />
                      </button>
                    )}
                  </div>
                </div>
              );
            })}

            {/* Camera Track Header */}
            <div className="flex h-7 items-center justify-between rounded border border-amber-500/25 bg-amber-950/15 px-1.5 text-amber-200">
              <div className="flex items-center gap-1.5">
                <Camera className="h-3 w-3 text-amber-400" />
                <span>Camera</span>
              </div>
              <button
                type="button"
                onClick={() => handleAddCameraKeyframeAt()}
                title="Add Camera Keyframe at playhead"
                className="rounded border border-amber-500/40 bg-amber-500/15 px-1 text-[9px] text-amber-200 hover:bg-amber-500/30"
              >
                +◆
              </button>
            </div>

            {/* Caption / Narration Track Headers */}
            {captionTracks.map((tr) => {
              const isSelected =
                selection?.type === "track" && selection.trackId === tr.id;
              return (
                <div
                  key={`hdr-${tr.id}`}
                  onClick={() => onSelectItem({ type: "track", trackId: tr.id })}
                  className={`flex h-8 cursor-pointer items-center justify-between gap-1 rounded border px-1.5 ${
                    isSelected
                      ? "border-sky-500/60 bg-sky-500/15 text-sky-100"
                      : "border-zinc-800 bg-zinc-900/70 hover:border-zinc-700"
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-1">
                    <Captions className="h-3 w-3 shrink-0 text-sky-400" />
                    <span className="truncate" title={tr.name}>
                      {tr.name}
                    </span>
                  </div>
                  <div
                    className="flex items-center gap-0.5"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      type="button"
                      onClick={() => handleAddCaptionAt(tr.id)}
                      title="Insert caption block at playhead"
                      className="rounded px-1 text-[9px] font-semibold text-sky-300 hover:bg-sky-500/20"
                    >
                      +
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id && t.kind === "caption"
                                ? { ...t, visible: !t.visible }
                                : t
                            ),
                          { label: `Toggle visibility on ${tr.name}` }
                        )
                      }
                      title={tr.visible ? "Hide captions" : "Show captions"}
                      className="rounded p-0.5 hover:bg-zinc-800"
                    >
                      {tr.visible ? (
                        <Eye className="h-3 w-3 text-zinc-300" />
                      ) : (
                        <EyeOff className="h-3 w-3 text-zinc-600" />
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id && t.kind === "caption"
                                ? { ...t, muted: !t.muted }
                                : t
                            ),
                          { label: `Toggle mute on ${tr.name}` }
                        )
                      }
                      title={tr.muted ? "Unmute TTS narration" : "Mute TTS narration"}
                      className="rounded p-0.5 hover:bg-zinc-800"
                    >
                      {tr.muted ? (
                        <VolumeX className="h-3 w-3 text-rose-400" />
                      ) : (
                        <Volume2 className="h-3 w-3 text-zinc-400" />
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id ? { ...t, locked: !t.locked } : t
                            ),
                          { label: `${tr.locked ? "Unlock" : "Lock"} ${tr.name}` }
                        )
                      }
                      title={tr.locked ? "Unlock track" : "Lock track"}
                      className="rounded p-0.5 hover:bg-zinc-800"
                    >
                      {tr.locked ? (
                        <Lock className="h-2.5 w-2.5 text-amber-400" />
                      ) : (
                        <Unlock className="h-2.5 w-2.5 text-zinc-500" />
                      )}
                    </button>
                    {captionTracks.length > 1 && (
                      <button
                        type="button"
                        onClick={() => handleDeleteTrack(tr)}
                        title={`Delete track ${tr.name}`}
                        className="rounded p-0.5 text-zinc-500 hover:text-rose-400"
                      >
                        <Trash2 className="h-2.5 w-2.5" />
                      </button>
                    )}
                  </div>
                </div>
              );
            })}

            {/* Audio Track Headers */}
            {audioTracks.map((tr) => {
              const isSelected =
                selection?.type === "track" && selection.trackId === tr.id;
              return (
                <div
                  key={`hdr-${tr.id}`}
                  onClick={() => onSelectItem({ type: "track", trackId: tr.id })}
                  className={`flex h-8 cursor-pointer items-center justify-between gap-1 rounded border px-1.5 ${
                    isSelected
                      ? "border-amber-500/60 bg-amber-500/15 text-amber-100"
                      : "border-zinc-800 bg-zinc-900/70 hover:border-zinc-700"
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-1">
                    <Music className="h-3 w-3 shrink-0 text-amber-400" />
                    <span className="truncate" title={tr.name}>
                      {tr.name}
                    </span>
                  </div>
                  <div
                    className="flex items-center gap-0.5"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id && t.kind === "audio"
                                ? { ...t, muted: !t.muted }
                                : t
                            ),
                          { label: `Toggle mute on ${tr.name}` }
                        )
                      }
                      title="Mute"
                      className={`rounded px-1 text-[9px] font-bold ${
                        tr.muted ? "bg-rose-500/30 text-rose-200" : "text-zinc-400 hover:bg-zinc-800"
                      }`}
                    >
                      M
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id && t.kind === "audio"
                                ? { ...t, solo: !t.solo }
                                : t
                            ),
                          { label: `Toggle solo on ${tr.name}` }
                        )
                      }
                      title="Solo"
                      className={`rounded px-1 text-[9px] font-bold ${
                        tr.solo ? "bg-amber-500/35 text-amber-200" : "text-zinc-400 hover:bg-zinc-800"
                      }`}
                    >
                      S
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id && t.kind === "audio"
                                ? { ...t, duck_under_narration: !t.duck_under_narration }
                                : t
                            ),
                          { label: `Toggle ducking on ${tr.name}` }
                        )
                      }
                      title={
                        tr.duck_under_narration
                          ? "Auto-Duck under voiceover ON"
                          : "Auto-Duck under voiceover OFF"
                      }
                      className={`rounded px-1 text-[9px] font-bold ${
                        tr.duck_under_narration
                          ? "bg-emerald-500/20 text-emerald-300"
                          : "text-zinc-500 hover:bg-zinc-800"
                      }`}
                    >
                      D
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateTracks(
                          (prev) =>
                            prev.map((t) =>
                              t.id === tr.id ? { ...t, locked: !t.locked } : t
                            ),
                          { label: `${tr.locked ? "Unlock" : "Lock"} ${tr.name}` }
                        )
                      }
                      title={tr.locked ? "Unlock track" : "Lock track"}
                      className="rounded p-0.5 hover:bg-zinc-800"
                    >
                      {tr.locked ? (
                        <Lock className="h-2.5 w-2.5 text-amber-400" />
                      ) : (
                        <Unlock className="h-2.5 w-2.5 text-zinc-500" />
                      )}
                    </button>
                    {audioTracks.length > 1 && (
                      <button
                        type="button"
                        onClick={() => handleDeleteTrack(tr)}
                        title={`Delete track ${tr.name}`}
                        className="rounded p-0.5 text-zinc-500 hover:text-rose-400"
                      >
                        <Trash2 className="h-2.5 w-2.5" />
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Right Scrollable Horizontal Track Canvas */}
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded border border-zinc-800 bg-[#0b0d11]">
            <div
              ref={scrollContainerRef}
              onWheel={(e) => {
                if (e.ctrlKey || e.metaKey) {
                  e.preventDefault();
                  const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
                  applyZoom(timelineZoom * factor, e.clientX);
                }
              }}
              className="overflow-x-auto"
            >
              <div
                ref={trackLaneRef}
                className="relative"
                style={{ width: `${timelineZoom * 100}%`, minWidth: "100%" }}
              >
                {/* Top DAW Zoom Strip */}
                <div
                  role="slider"
                  aria-label="Timeline Zoom Bar"
                  aria-valuemin={MIN_TIMELINE_ZOOM}
                  aria-valuemax={MAX_TIMELINE_ZOOM}
                  aria-valuenow={timelineZoom}
                  title="DAW Zoom: Drag down to zoom in, up to zoom out, left/right to pan (or Ctrl+Wheel)"
                  onMouseDown={(e) => {
                    e.preventDefault();
                    startDrag({
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
                  <span>↕ Drag to Zoom ({timelineZoom.toFixed(1)}x) • Ctrl+Wheel</span>
                </div>

                {/* Global Scrubbable Ruler + Timeline Markers */}
                <div
                  data-log-intent="Scrub timeline ruler"
                  onMouseDown={(e) => {
                    startDrag({ type: "scrub" });
                    scrubAtClientX(e.clientX);
                  }}
                  className="relative h-5 cursor-pointer border-b border-zinc-800/80 text-[9px] font-mono text-zinc-500"
                >
                  {ticks.map((tk, idx) => (
                    <div
                      key={idx}
                      className="absolute top-0.5 whitespace-nowrap"
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
                  {markers.map((m) => {
                    const leftPct = Math.max(0, Math.min(100, (m.t / timelineSpan) * 100));
                    return (
                      <div
                        key={m.id}
                        onMouseDown={(e) => {
                          e.stopPropagation();
                          onProjectTimeChange(m.t);
                        }}
                        onContextMenu={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          handleDeleteMarker(m.id);
                        }}
                        title={`Marker ${m.label || m.id} @${formatTimecode(m.t)} (Click to jump, Right-click to delete)`}
                        style={{ left: `${leftPct}%`, transform: "translateX(-50%)" }}
                        className="absolute top-0 z-20 flex items-center gap-0.5 rounded-b border border-amber-400/70 bg-amber-500/85 px-1 text-[8px] font-bold text-zinc-950 shadow hover:bg-amber-400"
                      >
                        <span>{m.label || "M"}</span>
                      </div>
                    );
                  })}
                </div>

                {/* Track Rows Stack */}
                <div className="relative space-y-1.5 py-1.5">
                  {/* Video / Overlay Lanes */}
                  {displayVideoTracks.map((tr) => {
                    const adjacentPairs = computeAdjacentJunctionPairs(tr);
                    const isDropTarget = dragOverTrackId === tr.id;
                    return (
                      <div
                        key={`lane-${tr.id}`}
                        data-track-id={tr.id}
                        data-track-kind="video"
                        onDragOver={(e) => handleAssetDragOver(e, tr)}
                        onDragLeave={() => {
                          if (dragOverTrackId === tr.id) setDragOverTrackId(null);
                        }}
                        onDrop={(e) => handleAssetDrop(e, tr)}
                        onContextMenu={(e) => {
                          if (tr.locked) return;
                          const tSec = timeFromClientX(e.clientX);
                          if (tSec === null) return;
                          e.preventDefault();
                          setContextMenu({
                            x: e.clientX,
                            y: e.clientY,
                            target: { kind: "gap", trackId: tr.id, timeSec: tSec },
                          });
                        }}
                        className={`relative h-8 transition-colors ${
                          isDropTarget
                            ? "bg-emerald-500/15 ring-1 ring-inset ring-emerald-400"
                            : "bg-zinc-900/55"
                        } ${!tr.visible ? "opacity-50" : ""}`}
                      >
                        {tr.clips.length === 0 && (
                          <div className="pointer-events-none flex h-full items-center px-2 text-[10px] text-zinc-600">
                            {tr.camera_follow
                              ? "Gaps supported • Drag video from Media Bin or click + Add Clip"
                              : "Overlay layer • Drop transparent PNG, WebP, or PiP video here"}
                          </div>
                        )}

                        {tr.clips.map((clip) => {
                          const leftPct = (clip.start_sec / timelineSpan) * 100;
                          const widthPct = Math.max(
                            0.8,
                            (clip.duration_sec / timelineSpan) * 100
                          );
                          const isSelected =
                            (selection?.type === "video-clip" &&
                              selection.trackId === tr.id &&
                              selection.clipId === clip.id) ||
                            isMultiSelected(clip.id);
                          const asset = project.media_assets?.[clip.asset_id];
                          const assetMeta = manifest.media_metadata?.[clip.asset_id];
                          const assetDur =
                            assetMeta?.duration ??
                            (clip.asset_id === project.source_media.media_id
                              ? manifest.source_metadata?.duration ?? 60
                              : 60);
                          const hasTrIn = clip.transition_in.type !== "none";
                          const hasTrOut = clip.transition_out.type !== "none";
                          const isOverlayTrack = !tr.camera_follow;

                          const baseColors = isSelected
                            ? isOverlayTrack
                              ? "border-purple-300 bg-purple-500/30 text-purple-100 ring-1 ring-purple-300/60"
                              : "border-emerald-400 bg-emerald-500/25 text-emerald-100 ring-1 ring-emerald-400/50"
                            : isOverlayTrack
                              ? "border-purple-500/50 bg-purple-500/18 text-purple-100 hover:bg-purple-500/25"
                              : "border-emerald-500/45 bg-emerald-500/15 text-emerald-200 hover:bg-emerald-500/22";

                          return (
                            <div
                              key={clip.id}
                              data-testid={`clip-${clip.id}`}
                              onContextMenu={(e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                const sel: NonNullable<TimelineSelection> = {
                                  type: "video-clip",
                                  trackId: tr.id,
                                  clipId: clip.id,
                                };
                                onSelectItem(sel);
                                setContextMenu({
                                  x: e.clientX,
                                  y: e.clientY,
                                  target: {
                                    kind: "clip",
                                    sel,
                                    trackId: tr.id,
                                    clipId: clip.id,
                                    clipKind: clip.kind,
                                  },
                                });
                              }}
                              onMouseDown={(e) => {
                                if (e.button === 2) return;
                                e.stopPropagation();
                                if (e.shiftKey) {
                                  toggleMultiSelect({
                                    kind: "video-clip",
                                    trackId: tr.id,
                                    clipId: clip.id,
                                  });
                                } else {
                                  setMultiSelected([]);
                                }
                                onSelectItem({
                                  type: "video-clip",
                                  trackId: tr.id,
                                  clipId: clip.id,
                                });
                                if (tr.locked) return;
                                startDrag({
                                  type: e.altKey && clip.kind === "video" ? "vclip-slip" : "vclip-move",
                                  trackId: tr.id,
                                  clipId: clip.id,
                                  startX: e.clientX,
                                  origStart: clip.start_sec,
                                  origDur: clip.duration_sec,
                                  origMediaIn: clip.media_in,
                                  origMediaOut: clip.media_out ?? null,
                                  speed: clip.speed,
                                  kind: clip.kind,
                                  assetDur,
                                  spanSec: timelineSpan,
                                });
                              }}
                              title={`${asset?.name ?? clip.asset_id} • ${clip.start_sec.toFixed(2)}s → ${(clip.start_sec + clip.duration_sec).toFixed(2)}s • Drag freely (Alt+Drag slip • Right-click menu)`}
                              className={`group absolute inset-y-0.5 flex items-center justify-between overflow-hidden rounded-sm border text-[10px] font-mono ${
                                tr.locked ? "cursor-not-allowed" : "cursor-grab active:cursor-grabbing"
                              } ${baseColors}`}
                              style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                            >
                              {hasTrIn && (
                                <span
                                  className="pointer-events-none absolute inset-y-0 left-0 w-1.5 bg-gradient-to-r from-amber-400/70 to-transparent"
                                  title={`In: ${clip.transition_in.type}`}
                                />
                              )}
                              {hasTrOut && (
                                <span
                                  className="pointer-events-none absolute inset-y-0 right-0 w-1.5 bg-gradient-to-l from-amber-400/70 to-transparent"
                                  title={`Out: ${clip.transition_out.type}`}
                                />
                              )}

                              {!tr.locked && (
                                <div
                                  onMouseDown={(e) => {
                                    e.stopPropagation();
                                    onSelectItem({
                                      type: "video-clip",
                                      trackId: tr.id,
                                      clipId: clip.id,
                                    });
                                    startDrag({
                                      type: "vclip-left",
                                      trackId: tr.id,
                                      clipId: clip.id,
                                      startX: e.clientX,
                                      origStart: clip.start_sec,
                                      origDur: clip.duration_sec,
                                      origMediaIn: clip.media_in,
                                      origMediaOut: clip.media_out ?? null,
                                      speed: clip.speed,
                                      kind: clip.kind,
                                      assetDur,
                                      spanSec: timelineSpan,
                                    });
                                  }}
                                  className="relative z-10 h-full w-2 shrink-0 cursor-ew-resize hover:bg-white/25"
                                  title="Drag to trim clip start"
                                />
                              )}

                              <img
                                src={apiClient.assetThumbnailUrl(
                                  project.project_id,
                                  clip.asset_id
                                )}
                                alt=""
                                loading="lazy"
                                onError={(e) => {
                                  e.currentTarget.style.display = "none";
                                }}
                                className="pointer-events-none relative z-10 h-5 w-7 shrink-0 rounded-[2px] object-cover opacity-80"
                              />

                              <span className="relative z-10 min-w-0 flex-1 truncate px-1">
                                {asset?.name ?? clip.id} ({clip.duration_sec.toFixed(1)}s)
                              </span>

                              {!tr.locked && (
                                <button
                                  type="button"
                                  onMouseDown={(e) => e.stopPropagation()}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    deleteSelectedOrSpecified({
                                      type: "video-clip",
                                      trackId: tr.id,
                                      clipId: clip.id,
                                    });
                                  }}
                                  title={`Delete clip ${clip.id}`}
                                  aria-label={`Delete clip ${clip.id}`}
                                  className="relative z-10 mr-0.5 shrink-0 rounded p-0.5 text-zinc-200/70 opacity-0 transition group-hover:opacity-100 hover:text-rose-300"
                                >
                                  <X className="h-3 w-3" />
                                </button>
                              )}

                              {!tr.locked && (
                                <div
                                  onMouseDown={(e) => {
                                    e.stopPropagation();
                                    onSelectItem({
                                      type: "video-clip",
                                      trackId: tr.id,
                                      clipId: clip.id,
                                    });
                                    startDrag({
                                      type: "vclip-right",
                                      trackId: tr.id,
                                      clipId: clip.id,
                                      startX: e.clientX,
                                      origStart: clip.start_sec,
                                      origDur: clip.duration_sec,
                                      origMediaIn: clip.media_in,
                                      origMediaOut: clip.media_out ?? null,
                                      speed: clip.speed,
                                      kind: clip.kind,
                                      assetDur,
                                      spanSec: timelineSpan,
                                    });
                                  }}
                                  className="relative z-10 h-full w-2 shrink-0 cursor-ew-resize hover:bg-white/25"
                                  title="Drag to adjust clip duration"
                                />
                              )}
                            </div>
                          );
                        })}

                        {/* Adjacent Junction Transition Badges */}
                        {adjacentPairs.map((pair) => {
                          const leftPct = (pair.junctionTime / timelineSpan) * 100;
                          const isSelectedTr =
                            pair.existing &&
                            selection?.type === "transition" &&
                            selection.trackId === tr.id &&
                            selection.transitionId === pair.existing.id;
                          return (
                            <button
                              key={`tr-${pair.fromClip.id}-${pair.toClip.id}`}
                              type="button"
                              onMouseDown={(e) => e.stopPropagation()}
                              onClick={(e) => {
                                e.stopPropagation();
                                handleJunctionClick(
                                  tr,
                                  pair.fromClip,
                                  pair.toClip,
                                  pair.existing
                                );
                              }}
                              title={
                                pair.existing
                                  ? `${pair.existing.type} (${pair.existing.duration_sec.toFixed(2)}s)`
                                  : "Add junction transition (Crossfade / Dissolve / Wipe)"
                              }
                              style={{
                                left: `${leftPct}%`,
                                transform: "translate(-50%, -50%)",
                              }}
                              className={`absolute top-1/2 z-20 inline-flex items-center gap-0.5 rounded border px-1 py-0.2 font-mono text-[9px] transition ${
                                isSelectedTr
                                  ? "border-amber-300 bg-purple-600 text-white ring-1 ring-amber-300"
                                  : pair.existing
                                    ? "border-purple-300/70 bg-purple-600/85 text-white hover:bg-purple-500"
                                    : "border-zinc-600 bg-zinc-900/90 text-zinc-400 opacity-65 hover:border-purple-400 hover:text-purple-200 hover:opacity-100"
                              }`}
                            >
                              <Zap className="h-2.5 w-2.5" />
                              {pair.existing && (
                                <span>{junctionShortCode(pair.existing.type)}</span>
                              )}
                            </button>
                          );
                        })}
                      </div>
                    );
                  })}

                  {/* Dedicated Global Camera Keyframes Track */}
                  <div
                    data-testid="camera-track"
                    onDoubleClick={(e) => {
                      const clickedT = timeFromClientX(e.clientX);
                      if (clickedT !== null) handleAddCameraKeyframeAt(clickedT);
                    }}
                    title="Double-click anywhere (even during gaps or 0 video clips) to add a camera keyframe"
                    className="relative h-7 bg-zinc-900/55"
                  >
                    {cameraKeyframes.length === 0 && (
                      <div className="pointer-events-none flex h-full items-center px-2 text-[10px] text-zinc-600">
                        Double-click camera track to insert animated pan/zoom keyframes
                      </div>
                    )}
                    {cameraKeyframes.map((kf, idx) => {
                      const leftPct = Math.max(
                        0,
                        Math.min(100, (kf.t / timelineSpan) * 100)
                      );
                      const isSelected =
                        selection?.type === "keyframe" && selection.index === idx;
                      const modeLabel =
                        (kf.transition ?? "tween") === "snap" ? "S" : "T";
                      return (
                        <div
                          key={`cam-kf-${idx}-${kf.t}`}
                          onDoubleClick={(e) => e.stopPropagation()}
                          onMouseDown={(e) => {
                            e.stopPropagation();
                            onSelectItem({ type: "keyframe", index: idx });
                            onProjectTimeChange(kf.t);
                            startDrag({
                              type: "keyframe-move",
                              index: idx,
                              startX: e.clientX,
                              origT: kf.t,
                              spanSec: timelineSpan,
                            });
                          }}
                          title={`Camera Keyframe #${idx + 1} @${kf.t.toFixed(2)}s • Zoom ${kf.zoom.toFixed(2)}x • ${(kf.transition ?? "tween").toUpperCase()}`}
                          className={`group absolute top-0.5 inline-flex cursor-grab active:cursor-grabbing items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-mono ${
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
                          {(idx > 0 || cameraKeyframes.length > 1) && (
                            <button
                              type="button"
                              onMouseDown={(e) => e.stopPropagation()}
                              onClick={(e) => {
                                e.stopPropagation();
                                deleteSelectedOrSpecified({ type: "keyframe", index: idx });
                              }}
                              title="Delete keyframe"
                              aria-label={`Delete keyframe #${idx + 1}`}
                              className="rounded text-amber-200/70 opacity-0 transition group-hover:opacity-100 hover:text-rose-300"
                            >
                              <X className="h-2.5 w-2.5" />
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>

                  {/* Independent Caption & Narration Tracks */}
                  {captionTracks.map((tr, cIdx) => (
                    <div
                      key={`lane-${tr.id}`}
                      data-track-id={tr.id}
                      data-track-kind="caption"
                      data-testid={cIdx === 0 ? "narration-track" : `caption-track-${tr.id}`}
                      onDoubleClick={(e) => {
                        if (tr.locked) return;
                        const clickedT = timeFromClientX(e.clientX);
                        if (clickedT !== null) handleAddCaptionAt(tr.id, clickedT);
                      }}
                      onContextMenu={(e) => {
                        if (tr.locked) return;
                        const tSec = timeFromClientX(e.clientX);
                        if (tSec === null) return;
                        e.preventDefault();
                        setContextMenu({
                          x: e.clientX,
                          y: e.clientY,
                          target: { kind: "gap", trackId: tr.id, timeSec: tSec },
                        });
                      }}
                      title="Double-click anywhere (independent of video) to add Caption / Voiceover block"
                      className={`relative h-8 bg-zinc-900/55 ${!tr.visible ? "opacity-50" : ""}`}
                    >
                      {tr.clips.length === 0 && (
                        <div className="pointer-events-none flex h-full items-center px-2 text-[10px] text-zinc-600">
                          Double-click or click &ldquo;+ Caption / VO&rdquo; — works anywhere, even without video
                        </div>
                      )}
                      {tr.clips.map((clip) => {
                        const dur = getCaptionClipEffectiveDuration(clip, manifest);
                        const leftPct = (clip.start_sec / timelineSpan) * 100;
                        const widthPct = Math.max(1.5, (dur / timelineSpan) * 100);
                        const isSelected =
                          (selection?.type === "caption-clip" &&
                            selection.trackId === tr.id &&
                            selection.clipId === clip.id) ||
                          isMultiSelected(clip.id);
                        const capArt = manifest.caption_artifacts?.[clip.id];
                        const hasAudio = Boolean(capArt?.audio_artifact_id);
                        const isStale = staleCaptionSet.has(clip.id);
                        const capWfKey =
                          capArt?.audio_artifact_id
                            ? `art:${project.project_id}:${capArt.audio_artifact_id}:${capArt.tts_hash ?? ""}`
                            : "";

                        return (
                          <div
                            key={clip.id}
                            data-testid={`clip-${clip.id}`}
                            onDoubleClick={(e) => e.stopPropagation()}
                            onContextMenu={(e) => {
                              e.preventDefault();
                              e.stopPropagation();
                              const sel: NonNullable<TimelineSelection> = {
                                type: "caption-clip",
                                trackId: tr.id,
                                clipId: clip.id,
                              };
                              onSelectItem(sel);
                              setContextMenu({
                                x: e.clientX,
                                y: e.clientY,
                                target: {
                                  kind: "clip",
                                  sel,
                                  trackId: tr.id,
                                  clipId: clip.id,
                                  clipKind: "caption",
                                },
                              });
                            }}
                            onMouseDown={(e) => {
                              if (e.button === 2) return;
                              e.stopPropagation();
                              if (e.shiftKey) {
                                toggleMultiSelect({
                                  kind: "caption-clip",
                                  trackId: tr.id,
                                  clipId: clip.id,
                                });
                              } else {
                                setMultiSelected([]);
                              }
                              onSelectItem({
                                type: "caption-clip",
                                trackId: tr.id,
                                clipId: clip.id,
                              });
                              onProjectTimeChange(clip.start_sec);
                              if (tr.locked) return;
                              startDrag({
                                type: "cclip-move",
                                trackId: tr.id,
                                clipId: clip.id,
                                startX: e.clientX,
                                origStart: clip.start_sec,
                                origDur: dur,
                                spanSec: timelineSpan,
                              });
                            }}
                            title={`${clip.id}: "${clip.display_text}" (${clip.start_sec.toFixed(2)}s, ${dur.toFixed(2)}s) • ${clip.tts_enabled ? "Spoken VO + Caption" : "Text Only"}`}
                            className={`group absolute inset-y-0.5 flex items-center gap-1 overflow-hidden rounded border px-1.5 text-[10px] ${
                              tr.locked ? "cursor-not-allowed" : "cursor-grab active:cursor-grabbing"
                            } ${
                              isSelected
                                ? "z-10 border-sky-300 bg-sky-500/35 text-white ring-1 ring-sky-300"
                                : "border-sky-500/50 bg-sky-500/20 text-sky-100 hover:bg-sky-500/30"
                            }`}
                            style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                          >
                            {capWfKey && (
                              <WaveformBars
                                peaks={waveforms[capWfKey]}
                                colorClass="text-sky-300"
                              />
                            )}
                            {!tr.locked && (
                              <div
                                onMouseDown={(e) => {
                                  e.stopPropagation();
                                  onSelectItem({
                                    type: "caption-clip",
                                    trackId: tr.id,
                                    clipId: clip.id,
                                  });
                                  startDrag({
                                    type: "cclip-left",
                                    trackId: tr.id,
                                    clipId: clip.id,
                                    startX: e.clientX,
                                    origStart: clip.start_sec,
                                    origDur: dur,
                                    spanSec: timelineSpan,
                                  });
                                }}
                                className="absolute inset-y-0 left-0 z-10 w-2 cursor-ew-resize hover:bg-sky-300/40"
                                title="Drag to trim caption start"
                              />
                            )}
                            <span
                              className={`relative z-10 shrink-0 rounded px-1 font-mono text-[9px] ${
                                clip.tts_enabled
                                  ? hasAudio
                                    ? "bg-emerald-500/25 text-emerald-200"
                                    : isStale
                                      ? "bg-amber-500/25 text-amber-200"
                                      : "bg-sky-500/25 text-sky-200"
                                  : "bg-zinc-800 text-zinc-400"
                              }`}
                            >
                              {clip.tts_enabled ? "VO" : "TXT"}
                            </span>
                            <span className="relative z-10 min-w-0 flex-1 truncate">
                              {clip.display_text || "Empty caption..."}
                            </span>
                            {!tr.locked && (
                              <button
                                type="button"
                                onMouseDown={(e) => e.stopPropagation()}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  deleteSelectedOrSpecified({
                                    type: "caption-clip",
                                    trackId: tr.id,
                                    clipId: clip.id,
                                  });
                                }}
                                title={`Delete ${clip.id}`}
                                aria-label={`Delete caption ${clip.id}`}
                                className="relative z-10 shrink-0 rounded text-sky-200/70 opacity-0 transition group-hover:opacity-100 hover:text-rose-300"
                              >
                                <X className="h-3 w-3" />
                              </button>
                            )}
                            {!tr.locked && (
                              <div
                                onMouseDown={(e) => {
                                  e.stopPropagation();
                                  onSelectItem({
                                    type: "caption-clip",
                                    trackId: tr.id,
                                    clipId: clip.id,
                                  });
                                  startDrag({
                                    type: "cclip-right",
                                    trackId: tr.id,
                                    clipId: clip.id,
                                    startX: e.clientX,
                                    origStart: clip.start_sec,
                                    origDur: dur,
                                    spanSec: timelineSpan,
                                  });
                                }}
                                className="absolute inset-y-0 right-0 z-10 w-2 cursor-ew-resize hover:bg-sky-300/40"
                                title="Drag to set fixed display duration"
                              />
                            )}
                          </div>
                        );
                      })}
                    </div>
                  ))}

                  {/* Standalone Audio / Music / SFX Tracks */}
                  {audioTracks.map((tr) => {
                    const isDropTarget = dragOverTrackId === tr.id;
                    return (
                      <div
                        key={`lane-${tr.id}`}
                        data-track-id={tr.id}
                        data-track-kind="audio"
                        onDragOver={(e) => handleAssetDragOver(e, tr)}
                        onDragLeave={() => {
                          if (dragOverTrackId === tr.id) setDragOverTrackId(null);
                        }}
                        onDrop={(e) => handleAssetDrop(e, tr)}
                        onContextMenu={(e) => {
                          if (tr.locked) return;
                          const tSec = timeFromClientX(e.clientX);
                          if (tSec === null) return;
                          e.preventDefault();
                          setContextMenu({
                            x: e.clientX,
                            y: e.clientY,
                            target: { kind: "gap", trackId: tr.id, timeSec: tSec },
                          });
                        }}
                        className={`relative h-8 transition-colors ${
                          isDropTarget
                            ? "bg-amber-500/15 ring-1 ring-inset ring-amber-400"
                            : "bg-zinc-900/55"
                        } ${tr.muted ? "opacity-45" : ""}`}
                      >
                        {tr.clips.length === 0 && (
                          <div className="pointer-events-none flex h-full items-center px-2 text-[10px] text-zinc-600">
                            Drop music, SFX, or voiceover files from Media Bin
                          </div>
                        )}
                        {tr.clips.map((clip) => {
                          const leftPct = (clip.start_sec / timelineSpan) * 100;
                          const widthPct = Math.max(
                            0.8,
                            (clip.duration_sec / timelineSpan) * 100
                          );
                          const isSelected =
                            (selection?.type === "audio-clip" &&
                              selection.trackId === tr.id &&
                              selection.clipId === clip.id) ||
                            isMultiSelected(clip.id);
                          const asset = project.media_assets?.[clip.asset_id];
                          const assetDur =
                            manifest.media_metadata?.[clip.asset_id]?.duration ?? 60;
                          const wfKey = `asset:${project.project_id}:${clip.asset_id}`;
                          const fadeInPct = Math.min(
                            45,
                            (clip.fade_in_sec / Math.max(0.1, clip.duration_sec)) * 100
                          );
                          const fadeOutPct = Math.min(
                            45,
                            (clip.fade_out_sec / Math.max(0.1, clip.duration_sec)) * 100
                          );

                          return (
                            <div
                              key={clip.id}
                              data-testid={`clip-${clip.id}`}
                              onContextMenu={(e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                const sel: NonNullable<TimelineSelection> = {
                                  type: "audio-clip",
                                  trackId: tr.id,
                                  clipId: clip.id,
                                };
                                onSelectItem(sel);
                                setContextMenu({
                                  x: e.clientX,
                                  y: e.clientY,
                                  target: {
                                    kind: "clip",
                                    sel,
                                    trackId: tr.id,
                                    clipId: clip.id,
                                    clipKind: "audio",
                                  },
                                });
                              }}
                              onMouseDown={(e) => {
                                if (e.button === 2) return;
                                e.stopPropagation();
                                if (e.shiftKey) {
                                  toggleMultiSelect({
                                    kind: "audio-clip",
                                    trackId: tr.id,
                                    clipId: clip.id,
                                  });
                                } else {
                                  setMultiSelected([]);
                                }
                                onSelectItem({
                                  type: "audio-clip",
                                  trackId: tr.id,
                                  clipId: clip.id,
                                });
                                if (tr.locked) return;
                                startDrag({
                                  type: "aclip-move",
                                  trackId: tr.id,
                                  clipId: clip.id,
                                  startX: e.clientX,
                                  origStart: clip.start_sec,
                                  origDur: clip.duration_sec,
                                  origMediaIn: clip.media_in,
                                  speed: clip.speed,
                                  assetDur,
                                  spanSec: timelineSpan,
                                });
                              }}
                              title={`${asset?.name ?? clip.asset_id} • ${clip.start_sec.toFixed(2)}s (${clip.duration_sec.toFixed(2)}s) • Right-click menu`}
                              className={`group absolute inset-y-0.5 flex items-center justify-between overflow-hidden rounded-sm border text-[10px] font-mono ${
                                tr.locked ? "cursor-not-allowed" : "cursor-grab active:cursor-grabbing"
                              } ${
                                isSelected
                                  ? "z-10 border-amber-300 bg-amber-500/30 text-amber-50 ring-1 ring-amber-300/60"
                                  : "border-amber-500/45 bg-amber-500/15 text-amber-200 hover:bg-amber-500/22"
                              }`}
                              style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                            >
                              <WaveformBars
                                peaks={waveforms[wfKey]}
                                colorClass="text-amber-300"
                              />
                              {/* Visual Fade In / Out slopes */}
                              {fadeInPct > 0 && (
                                <div
                                  className="pointer-events-none absolute inset-y-0 left-0 bg-gradient-to-r from-black/50 to-transparent"
                                  style={{ width: `${fadeInPct}%` }}
                                />
                              )}
                              {fadeOutPct > 0 && (
                                <div
                                  className="pointer-events-none absolute inset-y-0 right-0 bg-gradient-to-l from-black/50 to-transparent"
                                  style={{ width: `${fadeOutPct}%` }}
                                />
                              )}
                              {/* Draggable Fade In / Out top-corner handles */}
                              {!tr.locked && (
                                <>
                                  <div
                                    onMouseDown={(e) => {
                                      e.stopPropagation();
                                      onSelectItem({
                                        type: "audio-clip",
                                        trackId: tr.id,
                                        clipId: clip.id,
                                      });
                                      startDrag({
                                        type: "aclip-fade-in",
                                        trackId: tr.id,
                                        clipId: clip.id,
                                        startX: e.clientX,
                                        origStart: clip.start_sec,
                                        origDur: clip.duration_sec,
                                        origMediaIn: clip.media_in,
                                        origFade: clip.fade_in_sec,
                                        speed: clip.speed,
                                        assetDur,
                                        spanSec: timelineSpan,
                                      });
                                    }}
                                    title={`Fade In: ${clip.fade_in_sec.toFixed(2)}s (drag horizontally)`}
                                    style={{ left: `${fadeInPct}%` }}
                                    className="absolute top-0 z-20 h-2.5 w-2.5 -translate-x-1/2 cursor-ew-resize rounded-br bg-amber-300/80 opacity-0 transition group-hover:opacity-100"
                                  />
                                  <div
                                    onMouseDown={(e) => {
                                      e.stopPropagation();
                                      onSelectItem({
                                        type: "audio-clip",
                                        trackId: tr.id,
                                        clipId: clip.id,
                                      });
                                      startDrag({
                                        type: "aclip-fade-out",
                                        trackId: tr.id,
                                        clipId: clip.id,
                                        startX: e.clientX,
                                        origStart: clip.start_sec,
                                        origDur: clip.duration_sec,
                                        origMediaIn: clip.media_in,
                                        origFade: clip.fade_out_sec,
                                        speed: clip.speed,
                                        assetDur,
                                        spanSec: timelineSpan,
                                      });
                                    }}
                                    title={`Fade Out: ${clip.fade_out_sec.toFixed(2)}s (drag horizontally)`}
                                    style={{ right: `${fadeOutPct}%` }}
                                    className="absolute top-0 z-20 h-2.5 w-2.5 translate-x-1/2 cursor-ew-resize rounded-bl bg-amber-300/80 opacity-0 transition group-hover:opacity-100"
                                  />
                                </>
                              )}

                              {!tr.locked && (
                                <div
                                  onMouseDown={(e) => {
                                    e.stopPropagation();
                                    onSelectItem({
                                      type: "audio-clip",
                                      trackId: tr.id,
                                      clipId: clip.id,
                                    });
                                    startDrag({
                                      type: "aclip-left",
                                      trackId: tr.id,
                                      clipId: clip.id,
                                      startX: e.clientX,
                                      origStart: clip.start_sec,
                                      origDur: clip.duration_sec,
                                      origMediaIn: clip.media_in,
                                      speed: clip.speed,
                                      assetDur,
                                      spanSec: timelineSpan,
                                    });
                                  }}
                                  className="relative z-10 h-full w-2 shrink-0 cursor-ew-resize hover:bg-amber-300/40"
                                />
                              )}
                              <span className="relative z-10 min-w-0 flex-1 truncate px-1">
                                ♫ {asset?.name ?? clip.id} ({Math.round(clip.volume * 100)}%)
                              </span>
                              {!tr.locked && (
                                <button
                                  type="button"
                                  onMouseDown={(e) => e.stopPropagation()}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    deleteSelectedOrSpecified({
                                      type: "audio-clip",
                                      trackId: tr.id,
                                      clipId: clip.id,
                                    });
                                  }}
                                  title={`Delete audio clip ${clip.id}`}
                                  aria-label={`Delete audio clip ${clip.id}`}
                                  className="relative z-10 mr-0.5 shrink-0 rounded p-0.5 text-amber-200/70 opacity-0 transition group-hover:opacity-100 hover:text-rose-300"
                                >
                                  <X className="h-3 w-3" />
                                </button>
                              )}
                              {!tr.locked && (
                                <div
                                  onMouseDown={(e) => {
                                    e.stopPropagation();
                                    onSelectItem({
                                      type: "audio-clip",
                                      trackId: tr.id,
                                      clipId: clip.id,
                                    });
                                    startDrag({
                                      type: "aclip-right",
                                      trackId: tr.id,
                                      clipId: clip.id,
                                      startX: e.clientX,
                                      origStart: clip.start_sec,
                                      origDur: clip.duration_sec,
                                      origMediaIn: clip.media_in,
                                      speed: clip.speed,
                                      assetDur,
                                      spanSec: timelineSpan,
                                    });
                                  }}
                                  className="relative z-10 h-full w-2 shrink-0 cursor-ew-resize hover:bg-amber-300/40"
                                />
                              )}
                            </div>
                          );
                        })}
                      </div>
                    );
                  })}

                  {/* Magnetic Snap Visual Guideline */}
                  {snapGuideTime !== null && (
                    <div
                      className="pointer-events-none absolute inset-y-0 z-25 w-px bg-cyan-400 shadow-[0_0_8px_rgba(34,211,238,0.95)]"
                      style={{
                        left: `${Math.max(0, Math.min(100, (snapGuideTime / timelineSpan) * 100))}%`,
                      }}
                    />
                  )}

                  {/* Shared Global Playhead across all lanes */}
                  <div
                    className="pointer-events-none absolute inset-y-0 z-30 w-0.5 bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.85)]"
                    style={{ left: `${playheadPct}%` }}
                  />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Right-Click Context Menu */}
      {contextMenu && (
        <div
          onMouseDown={(e) => e.stopPropagation()}
          style={{
            left: Math.min(contextMenu.x, window.innerWidth - 210),
            top: Math.min(contextMenu.y, window.innerHeight - 280),
          }}
          className="fixed z-50 w-48 rounded-lg border border-zinc-700 bg-[#181c26] p-1 text-xs text-zinc-200 shadow-2xl"
        >
          {contextMenu.target.kind === "gap" ? (
            <>
              <button
                type="button"
                onClick={() => {
                  const target = contextMenu.target;
                  if (target.kind === "gap") {
                    handleCloseGap(target.trackId, target.timeSec);
                  }
                  setContextMenu(null);
                }}
                className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left hover:bg-zinc-800"
              >
                <Magnet className="h-3.5 w-3.5 text-emerald-400" />
                Close Gap
              </button>
              {canPasteClip && onPasteClipAtPlayhead && (
                <button
                  type="button"
                  onClick={() => {
                    if (contextMenu.target.kind === "gap") {
                      onProjectTimeChange(contextMenu.target.timeSec);
                    }
                    onPasteClipAtPlayhead();
                    setContextMenu(null);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left hover:bg-zinc-800"
                >
                  <Copy className="h-3.5 w-3.5 text-sky-400" />
                  Paste Clip Here
                </button>
              )}
              {onUpdateMarkers && (
                <button
                  type="button"
                  onClick={() => {
                    if (contextMenu.target.kind === "gap") {
                      handleAddMarkerAt(contextMenu.target.timeSec);
                    }
                    setContextMenu(null);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left hover:bg-zinc-800"
                >
                  <Bookmark className="h-3.5 w-3.5 text-amber-400" />
                  Add Marker Here
                </button>
              )}
            </>
          ) : (
            <>
              {splitTarget && (
                <button
                  type="button"
                  onClick={() => {
                    handleSplitAtPlayhead();
                    setContextMenu(null);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left hover:bg-zinc-800"
                >
                  <Scissors className="h-3.5 w-3.5 text-zinc-400" />
                  Split at Playhead (S)
                </button>
              )}
              {onCopySelectedClip && (
                <button
                  type="button"
                  onClick={() => {
                    onCopySelectedClip();
                    setContextMenu(null);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left hover:bg-zinc-800"
                >
                  <Copy className="h-3.5 w-3.5 text-zinc-400" />
                  Copy Clip (Ctrl+C)
                </button>
              )}
              {onDuplicateSelectedClip && (
                <button
                  type="button"
                  onClick={() => {
                    onDuplicateSelectedClip();
                    setContextMenu(null);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left hover:bg-zinc-800"
                >
                  <Plus className="h-3.5 w-3.5 text-zinc-400" />
                  Duplicate (Ctrl+D)
                </button>
              )}
              {onCopyAttributes && (
                <button
                  type="button"
                  onClick={() => {
                    onCopyAttributes();
                    setContextMenu(null);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left hover:bg-zinc-800"
                >
                  <Copy className="h-3.5 w-3.5 text-sky-400" />
                  Copy Attributes
                </button>
              )}
              {canPasteAttributes && onPasteAttributes && (
                <button
                  type="button"
                  onClick={() => {
                    onPasteAttributes();
                    setContextMenu(null);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left hover:bg-zinc-800"
                >
                  <Copy className="h-3.5 w-3.5 text-emerald-400" />
                  Paste Attributes
                </button>
              )}
              {contextMenu.target.clipKind === "video" && onDetachAudio && (
                <button
                  type="button"
                  onClick={() => {
                    if (contextMenu.target.kind === "clip") {
                      onDetachAudio(contextMenu.target.trackId, contextMenu.target.clipId);
                    }
                    setContextMenu(null);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left hover:bg-zinc-800"
                >
                  <Music className="h-3.5 w-3.5 text-amber-400" />
                  Detach Audio
                </button>
              )}
              {contextMenu.target.clipKind === "video" && onFreezeFrame && (
                <button
                  type="button"
                  onClick={() => {
                    if (contextMenu.target.kind === "clip") {
                      onFreezeFrame(contextMenu.target.trackId, contextMenu.target.clipId);
                    }
                    setContextMenu(null);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left hover:bg-zinc-800"
                >
                  <Snowflake className="h-3.5 w-3.5 text-sky-400" />
                  Insert Freeze Frame
                </button>
              )}
              <div className="my-1 border-t border-zinc-800" />
              <button
                type="button"
                onClick={() => {
                  if (contextMenu.target.kind === "clip") {
                    handleRippleDelete(
                      contextMenu.target.trackId,
                      contextMenu.target.clipId
                    );
                  }
                  setContextMenu(null);
                }}
                className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left text-amber-200 hover:bg-amber-500/20"
              >
                <Trash2 className="h-3.5 w-3.5" />
                Ripple Delete (Shift+Del)
              </button>
              <button
                type="button"
                onClick={() => {
                  if (contextMenu.target.kind === "clip") {
                    deleteSelectedOrSpecified(contextMenu.target.sel);
                  }
                  setContextMenu(null);
                }}
                className="flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-left text-rose-300 hover:bg-rose-500/20"
              >
                <Trash2 className="h-3.5 w-3.5" />
                Delete Clip
              </button>
            </>
          )}
        </div>
      )}
    </section>
  );
}
