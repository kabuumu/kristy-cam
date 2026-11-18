import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Download,
  Film,
  FolderOpen,
  FolderPlus,
  Link2,
  Loader2,
  Redo2,
  RefreshCw,
  Save,
  ScrollText,
  Undo2,
  X,
} from "lucide-react";
import { ApiError, apiClient } from "./api/client";
import { ActivityLogModal } from "./components/ActivityLogModal";
import { ContextualInspector } from "./components/ContextualInspector";
import { CropCanvas } from "./components/CropCanvas";
import { JobStatusBar } from "./components/JobStatusBar";
import { MediaBin } from "./components/MediaBin";
import { Timeline } from "./components/Timeline";
import type {
  ArtifactManifest,
  AudioClip,
  AudioTrack,
  Capabilities,
  CaptionClip,
  CropKeyframe,
  JobState,
  MediaAssetKind,
  MediaAssetRef,
  MediaSelectResult,
  ProjectBundle,
  ProjectInput,
  ProjectSummary,
  SourceStatus,
  TimelineSelection,
  TimelineTrack,
  VideoTrack,
  VisualClip,
} from "./types/studio";
import {
  clampCropPosition,
  computeCaptionClipTtsSignature,
  computeIdealCrop,
  computeMultiTrackPreviewSignature,
  ensureDefaultTracks,
  findNextClipId,
  findNextTrackId,
  formatAspectRatio,
  getProjectDuration,
  migrateProjectToMultiTrack,
  resolveCaptionClipSpokenText,
} from "./utils/studioMath";
import {
  beginHistoryTransaction,
  createInitialHistoryState,
  endHistoryTransaction,
  recordHistoryUpdate,
  serializeProjectContent,
  stepRedo,
  stepUndo,
  type HistorySnapshot,
  type UndoHistoryState,
  type UpdateMeta,
} from "./utils/undoHistory";
import { usageLogger } from "./utils/usageLogger";

const OUTPUT_ASPECT_PRESETS = [
  { label: "9:16 (1080×1920)", width: 1080, height: 1920 },
  { label: "16:9 (1920×1080)", width: 1920, height: 1080 },
  { label: "1:1 (1080×1080)", width: 1080, height: 1080 },
  { label: "4:5 (1080×1350)", width: 1080, height: 1350 },
  { label: "4:3 (1440×1080)", width: 1440, height: 1080 },
] as const;

function pickValidSelection(
  prevSel: TimelineSelection,
  proj: ProjectInput
): TimelineSelection {
  const tracks = proj.tracks ?? [];
  if (prevSel) {
    if (prevSel.type === "video-clip") {
      const tr = tracks.find((t) => t.id === prevSel.trackId && t.kind === "video");
      if (tr && tr.clips.some((c) => c.id === prevSel.clipId)) return prevSel;
    } else if (prevSel.type === "audio-clip") {
      const tr = tracks.find((t) => t.id === prevSel.trackId && t.kind === "audio");
      if (tr && tr.clips.some((c) => c.id === prevSel.clipId)) return prevSel;
    } else if (prevSel.type === "caption-clip") {
      const tr = tracks.find((t) => t.id === prevSel.trackId && t.kind === "caption");
      if (tr && tr.clips.some((c) => c.id === prevSel.clipId)) return prevSel;
    } else if (prevSel.type === "transition") {
      const tr = tracks.find((t) => t.id === prevSel.trackId && t.kind === "video");
      if (
        tr &&
        tr.kind === "video" &&
        tr.transitions.some((x) => x.id === prevSel.transitionId)
      ) {
        return prevSel;
      }
    } else if (prevSel.type === "track") {
      if (tracks.some((t) => t.id === prevSel.trackId)) return prevSel;
    } else if (prevSel.type === "keyframe") {
      const kfs = proj.camera_keyframes ?? [];
      if (prevSel.index >= 0 && prevSel.index < kfs.length) return prevSel;
    }
  }

  const firstVideoTrack = tracks.find(
    (t): t is VideoTrack => t.kind === "video" && t.clips.length > 0
  );
  if (firstVideoTrack && firstVideoTrack.clips[0]) {
    return {
      type: "video-clip",
      trackId: firstVideoTrack.id,
      clipId: firstVideoTrack.clips[0].id,
    };
  }
  const firstCapTrack = tracks.find((t) => t.kind === "caption" && t.clips.length > 0);
  if (firstCapTrack && firstCapTrack.clips[0]) {
    return {
      type: "caption-clip",
      trackId: firstCapTrack.id,
      clipId: firstCapTrack.clips[0].id,
    };
  }
  return null;
}

function buildCaptionSignatureMap(
  proj: ProjectInput,
  manifest: ArtifactManifest,
  staleList?: string[]
): Record<string, string> {
  const staleSet = new Set(staleList ?? []);
  const sigs: Record<string, string> = {};
  for (const tr of proj.tracks ?? []) {
    if (tr.kind !== "caption") continue;
    for (const clip of tr.clips) {
      if (
        clip.tts_enabled &&
        !staleSet.has(clip.id) &&
        manifest.caption_artifacts?.[clip.id]?.audio_artifact_id
      ) {
        sigs[clip.id] = computeCaptionClipTtsSignature(
          clip,
          proj.pronunciations,
          proj.voice,
          proj.language
        );
      }
    }
  }
  return sigs;
}

function findNonOverlappingStart(
  existingClips: Array<{ start_sec: number; duration_sec: number }>,
  preferredStart: number,
  duration: number
): number {
  const start = Math.max(0, Number(preferredStart.toFixed(3)));
  const end = start + Math.max(0.05, duration);
  const overlaps = existingClips.some(
    (c) => start < c.start_sec + c.duration_sec - 0.02 && end > c.start_sec + 0.02
  );
  if (!overlaps) return start;
  const maxEnd = Math.max(
    0,
    ...existingClips.map((c) => c.start_sec + c.duration_sec)
  );
  return Number(maxEnd.toFixed(3));
}

export default function App() {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [project, setProject] = useState<ProjectInput | null>(null);
  const [manifest, setManifest] = useState<ArtifactManifest | null>(null);
  const [sourceStatus, setSourceStatus] = useState<SourceStatus | null>(null);
  const [selection, setSelection] = useState<TimelineSelection>(null);

  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [bannerError, setBannerError] = useState<string | null>(null);
  const [revisionConflict, setRevisionConflict] = useState(false);

  const [sourceTime, setSourceTime] = useState(0);
  const [projectTime, setProjectTime] = useState(0);
  const [monitorMode, setMonitorMode] = useState<"timeline" | "source">("timeline");
  const [previewAssetId, setPreviewAssetId] = useState<string | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isProjectPlaying, setIsProjectPlaying] = useState(false);
  const [preparingPlayback, setPreparingPlayback] = useState(false);

  const [activeJob, setActiveJob] = useState<JobState | null>(null);
  const [ttsError, setTtsError] = useState<string | null>(null);
  const [synthesizingClipId, setSynthesizingClipId] = useState<string | null>(null);
  const [lastSynthesizedCaptionSignatures, setLastSynthesizedCaptionSignatures] =
    useState<Record<string, string>>({});
  const [
    lastRenderedProjectPreviewSignature,
    setLastRenderedProjectPreviewSignature,
  ] = useState<string | null>(null);

  // Asset Bin state
  const [assetImportBusy, setAssetImportBusy] = useState(false);
  const [assetImportError, setAssetImportError] = useState<string | null>(null);

  // Modals
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [copyToProject, setCopyToProject] = useState(false);
  const [selectedMedia, setSelectedMedia] = useState<MediaSelectResult | null>(null);
  const [newProjectError, setNewProjectError] = useState<string | null>(null);
  const [mediaSelectBusy, setMediaSelectBusy] = useState(false);
  const [createBusy, setCreateBusy] = useState(false);
  const newProjectFileInputRef = useRef<HTMLInputElement | null>(null);

  const [relinkOpen, setRelinkOpen] = useState(false);
  const [relinkPath, setRelinkPath] = useState("");
  const [relinkFile, setRelinkFile] = useState<File | null>(null);
  const [relinkBusy, setRelinkBusy] = useState(false);

  const [previewModalOpen, setPreviewModalOpen] = useState(false);
  const [exportModalOpen, setExportModalOpen] = useState(false);
  const [logModalOpen, setLogModalOpen] = useState(false);
  const [customResolutionMode, setCustomResolutionMode] = useState(false);

  const [historyState, setHistoryState] = useState<UndoHistoryState>(() =>
    createInitialHistoryState()
  );
  const historyRef = useRef<UndoHistoryState>(historyState);
  const savedBaselineRef = useRef<string | null>(null);

  const projectRef = useRef<ProjectInput | null>(null);
  projectRef.current = project;
  const manifestRef = useRef<ArtifactManifest | null>(null);
  manifestRef.current = manifest;
  const selectionRef = useRef<TimelineSelection>(null);
  selectionRef.current = selection;

  useEffect(() => {
    usageLogger.init(() => ({
      projectId: projectRef.current?.project_id ?? null,
      segId:
        selectionRef.current && "clipId" in selectionRef.current
          ? selectionRef.current.clipId
          : null,
    }));
  }, []);

  const applyBundle = useCallback(
    (bundle: ProjectBundle, options?: { resetHistory?: boolean }) => {
      const resetHistory = options?.resetHistory ?? true;
      const normalizedProject = migrateProjectToMultiTrack(
        bundle.project,
        bundle.manifest
      );

      projectRef.current = normalizedProject;
      setProject(normalizedProject);
      setManifest(bundle.manifest);
      setSourceStatus(bundle.source_status ?? null);
      savedBaselineRef.current = serializeProjectContent(normalizedProject);
      setDirty(false);
      setRevisionConflict(false);

      if (resetHistory) {
        const fresh = createInitialHistoryState();
        historyRef.current = fresh;
        setHistoryState(fresh);
      }

      const nextSel = pickValidSelection(selectionRef.current, normalizedProject);
      selectionRef.current = nextSel;
      setSelection(nextSel);

      setPreviewAssetId((prev) =>
        prev && normalizedProject.media_assets?.[prev]
          ? prev
          : normalizedProject.source_media.media_id
      );

      setLastSynthesizedCaptionSignatures(
        buildCaptionSignatureMap(
          normalizedProject,
          bundle.manifest,
          bundle.stale_tts_captions
        )
      );

      if (bundle.manifest.latest_preview_artifact_id) {
        setLastRenderedProjectPreviewSignature(
          (prev) =>
            prev ?? computeMultiTrackPreviewSignature(normalizedProject)
        );
      } else {
        setLastRenderedProjectPreviewSignature(null);
      }
    },
    []
  );

  const loadProject = useCallback(
    async (projectId: string) => {
      usageLogger.logIntention("Load project", "project.load", { projectId });
      try {
        setBannerError(null);
        setAssetImportError(null);
        const bundle = await apiClient.getProject(projectId);
        applyBundle(bundle);
        setProjectTime(0);
        setSourceTime(0);
        setMonitorMode("timeline");
        const url = new URL(window.location.href);
        url.searchParams.set("project", projectId);
        window.history.replaceState({}, "", url.toString());
        usageLogger.logActionOutput(
          "Load project",
          "project.load",
          {
            title: bundle.project.title,
            revision: bundle.project.revision,
            track_count: bundle.project.tracks?.length ?? 0,
            source_status: bundle.source_status?.status ?? "unknown",
          },
          { projectId }
        );
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Failed to load project";
        usageLogger.logError("Load project", "project.load_failed", err, {
          projectId,
        });
        setBannerError(msg);
      }
    },
    [applyBundle]
  );

  const refreshProjectList = useCallback(async () => {
    try {
      const list = await apiClient.listProjects();
      setProjects(list);
      return list;
    } catch {
      return [];
    }
  }, []);

  // Bootstrap capabilities + projects + ?project= param
  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const [caps, list] = await Promise.all([
          apiClient.capabilities().catch(() => null),
          apiClient.listProjects().catch(() => []),
        ]);
        if (!mounted) return;
        if (caps) setCapabilities(caps);
        setProjects(list);

        const params = new URLSearchParams(window.location.search);
        const requestedId = params.get("project");
        const targetId = requestedId || list[0]?.project_id || null;
        if (targetId) {
          await loadProject(targetId);
        }
      } catch (err) {
        if (mounted) {
          setBannerError(
            err instanceof Error ? err.message : "Failed to initialize studio"
          );
        }
      }
    })();
    return () => {
      mounted = false;
    };
  }, [loadProject]);

  // Poll active job until terminal state
  useEffect(() => {
    if (
      !activeJob ||
      (activeJob.status !== "queued" && activeJob.status !== "running")
    ) {
      return;
    }
    const timer = window.setInterval(async () => {
      try {
        const updated = await apiClient.getJob(activeJob.job_id);
        setActiveJob(updated);
        if (
          updated.status === "succeeded" ||
          updated.status === "failed" ||
          updated.status === "cancelled" ||
          updated.status === "interrupted"
        ) {
          window.clearInterval(timer);
          if (updated.status === "succeeded") {
            const pid = updated.project_id;
            const bundle = await apiClient.getProject(pid);
            setManifest(bundle.manifest);
            setSourceStatus(bundle.source_status ?? null);

            if (
              (updated.job_type === "tts" ||
                updated.job_type === "preview" ||
                updated.job_type === "export") &&
              projectRef.current
            ) {
              setTtsError(null);
              const sigs = buildCaptionSignatureMap(
                projectRef.current,
                bundle.manifest,
                bundle.stale_tts_captions
              );
              setLastSynthesizedCaptionSignatures((prev) => ({ ...prev, ...sigs }));
            }
            if (updated.job_type === "preview") {
              if (projectRef.current) {
                setLastRenderedProjectPreviewSignature(
                  computeMultiTrackPreviewSignature(projectRef.current)
                );
              }
              setPreviewModalOpen(true);
            } else if (updated.job_type === "export") {
              setExportModalOpen(true);
            }
            usageLogger.logActionOutput(
              `Job ${updated.job_type} succeeded`,
              `job.${updated.job_type}.succeeded`,
              updated.result ?? { status: "succeeded" },
              {
                projectId: updated.project_id,
                segId: updated.seg_id,
                jobId: updated.job_id,
              }
            );
          } else {
            if (updated.job_type === "tts" && updated.error) {
              setTtsError(updated.error);
            }
            if (updated.status === "failed" || updated.status === "interrupted") {
              usageLogger.logError(
                `Job ${updated.job_type} ${updated.status}`,
                `job.${updated.job_type}.${updated.status}`,
                updated.error || `Job ${updated.status}`,
                {
                  projectId: updated.project_id,
                  segId: updated.seg_id,
                  jobId: updated.job_id,
                  context: {
                    stage: updated.stage,
                    progress: updated.progress,
                  },
                }
              );
            }
          }
        }
      } catch {
        // Ignore transient poll errors
      }
    }, 450);
    return () => window.clearInterval(timer);
  }, [activeJob]);

  const totalProjectDuration = useMemo(() => {
    if (!project) return 0;
    return getProjectDuration(project, manifest, 0);
  }, [project, manifest]);

  // Continuous global multi-track playback loop
  useEffect(() => {
    if (
      !isProjectPlaying ||
      !project ||
      !manifest ||
      totalProjectDuration <= 0
    ) {
      return;
    }
    setMonitorMode("timeline");
    setIsPlaying(true);

    let lastTick = performance.now();
    const timer = window.setInterval(() => {
      const now = performance.now();
      const dt = (now - lastTick) / 1000;
      lastTick = now;

      setProjectTime((prev) => {
        const startFrom = prev >= totalProjectDuration - 0.02 ? 0 : prev;
        const next = startFrom + dt;
        if (next >= totalProjectDuration) {
          setIsProjectPlaying(false);
          setIsPlaying(false);
          return totalProjectDuration;
        }
        return next;
      });
    }, 33);

    return () => window.clearInterval(timer);
  }, [isProjectPlaying, project, manifest, totalProjectDuration]);

  const isModalOpenRef = useRef(false);

  const handleUpdateProject = useCallback(
    (
      updater: (prev: ProjectInput) => ProjectInput,
      meta?: UpdateMeta,
      nextSel?: { selectedSegId?: string | null; selection?: TimelineSelection }
    ) => {
      const prevProject = projectRef.current;
      if (!prevProject) return;
      const nextProject = updater(prevProject);
      const prevSnapshot: HistorySnapshot = {
        project: prevProject,
        selectedSegId: null,
        selection: selectionRef.current,
      };
      const nextSelection =
        nextSel?.selection !== undefined
          ? nextSel.selection
          : selectionRef.current;
      const nextSnapshot: HistorySnapshot = {
        project: nextProject,
        selectedSegId: null,
        selection: nextSelection,
      };

      projectRef.current = nextProject;
      selectionRef.current = nextSelection;
      setProject(nextProject);
      if (nextSel?.selection !== undefined) {
        setSelection(nextSelection);
      }

      const nextHistory = recordHistoryUpdate(
        historyRef.current,
        prevSnapshot,
        nextSnapshot,
        meta
      );
      if (nextHistory !== historyRef.current) {
        historyRef.current = nextHistory;
        setHistoryState(nextHistory);
      }

      const nextDirty =
        savedBaselineRef.current !== null
          ? serializeProjectContent(nextProject) !== savedBaselineRef.current
          : true;
      setDirty(nextDirty);
    },
    []
  );

  const handleUpdateTracks = useCallback(
    (
      updater: (tracks: TimelineTrack[]) => TimelineTrack[],
      meta?: UpdateMeta,
      nextSelection?: TimelineSelection
    ) => {
      handleUpdateProject(
        (prev) => ({
          ...prev,
          tracks: updater(ensureDefaultTracks(prev)),
          segments: [], // Prevent legacy segment resurrection once multi-track edits are committed
        }),
        meta ?? { label: "Edit timeline" },
        nextSelection !== undefined ? { selection: nextSelection } : undefined
      );
    },
    [handleUpdateProject]
  );

  const handleUpdateCameraKeyframes = useCallback(
    (keyframes: CropKeyframe[], meta?: UpdateMeta) => {
      handleUpdateProject(
        (prev) => ({
          ...prev,
          camera_keyframes: keyframes,
        }),
        meta ?? { label: "Edit camera keyframes" }
      );
    },
    [handleUpdateProject]
  );

  const handleBeginHistoryTransaction = useCallback((label: string) => {
    const cur = projectRef.current;
    if (!cur) return;
    const nextHistory = beginHistoryTransaction(
      historyRef.current,
      {
        project: cur,
        selectedSegId: null,
        selection: selectionRef.current,
      },
      label
    );
    historyRef.current = nextHistory;
    setHistoryState(nextHistory);
  }, []);

  const handleEndHistoryTransaction = useCallback(() => {
    const cur = projectRef.current;
    if (!cur) return;
    const nextHistory = endHistoryTransaction(historyRef.current, {
      project: cur,
      selectedSegId: null,
      selection: selectionRef.current,
    });
    if (nextHistory !== historyRef.current) {
      historyRef.current = nextHistory;
      setHistoryState(nextHistory);
    }
  }, []);

  const handleUndo = useCallback(() => {
    const cur = projectRef.current;
    if (!cur) return;
    const res = stepUndo(historyRef.current, cur);
    if (!res) return;
    historyRef.current = res.state;
    setHistoryState(res.state);
    projectRef.current = res.restored.project;
    selectionRef.current = res.restored.selection;
    setProject(res.restored.project);
    setSelection(res.restored.selection);
    const nextDirty =
      savedBaselineRef.current !== null
        ? serializeProjectContent(res.restored.project) !== savedBaselineRef.current
        : true;
    setDirty(nextDirty);
    usageLogger.logActionOutput(
      `Undo: ${res.entry.label}`,
      "project.undo",
      { label: res.entry.label, remaining_undo: res.state.past.length },
      { projectId: res.restored.project.project_id }
    );
  }, []);

  const handleRedo = useCallback(() => {
    const cur = projectRef.current;
    if (!cur) return;
    const res = stepRedo(historyRef.current, cur);
    if (!res) return;
    historyRef.current = res.state;
    setHistoryState(res.state);
    projectRef.current = res.restored.project;
    selectionRef.current = res.restored.selection;
    setProject(res.restored.project);
    setSelection(res.restored.selection);
    const nextDirty =
      savedBaselineRef.current !== null
        ? serializeProjectContent(res.restored.project) !== savedBaselineRef.current
        : true;
    setDirty(nextDirty);
    usageLogger.logActionOutput(
      `Redo: ${res.entry.label}`,
      "project.redo",
      { label: res.entry.label, remaining_redo: res.state.future.length },
      { projectId: res.restored.project.project_id }
    );
  }, []);

  // Global Undo/Redo shortcuts
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!projectRef.current || isModalOpenRef.current) return;
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      const key = e.key.toLowerCase();
      if (key === "z") {
        e.preventDefault();
        if (e.shiftKey) {
          handleRedo();
        } else {
          handleUndo();
        }
      } else if (key === "y" && !e.shiftKey) {
        e.preventDefault();
        handleRedo();
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [handleUndo, handleRedo]);

  const handleSave = async (): Promise<ProjectInput | null> => {
    const current = projectRef.current;
    if (!current) return null;
    usageLogger.logIntention("Save project", "project.save", {
      projectId: current.project_id,
      input: {
        expected_revision: current.revision,
        track_count: current.tracks?.length ?? 0,
      },
    });
    setSaving(true);
    setBannerError(null);
    setRevisionConflict(false);
    try {
      const bundle = await apiClient.saveProject(
        current.project_id,
        current,
        current.revision
      );
      applyBundle(bundle, { resetHistory: false });
      await refreshProjectList();
      usageLogger.logActionOutput(
        "Save project",
        "project.save",
        {
          revision: bundle.project.revision,
          track_count: bundle.project.tracks?.length ?? 0,
        },
        { projectId: bundle.project.project_id }
      );
      return bundle.project;
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setRevisionConflict(true);
        setBannerError(
          "Revision conflict: project was modified elsewhere. Reload latest or save again."
        );
      } else {
        setBannerError(
          err instanceof Error ? err.message : "Failed to save project"
        );
      }
      usageLogger.logError("Save project", "project.save_failed", err, {
        projectId: current.project_id,
        context: { expected_revision: current.revision },
      });
      return null;
    } finally {
      setSaving(false);
    }
  };

  const ensureSavedBeforeJob = async (): Promise<ProjectInput | null> => {
    const cur = projectRef.current;
    if (!cur) return null;
    if (!dirty) return cur;
    return handleSave();
  };

  const runCaptionTtsSynthesis = async (clipIds?: string[]): Promise<boolean> => {
    const cur = projectRef.current;
    const curManifest = manifestRef.current;
    if (!cur || !curManifest) return true;

    const captionClips: CaptionClip[] = [];
    for (const tr of cur.tracks ?? []) {
      if (tr.kind !== "caption" || tr.muted) continue;
      for (const c of tr.clips) {
        if (clipIds && !clipIds.includes(c.id)) continue;
        captionClips.push(c);
      }
    }

    const staleIds: string[] = [];
    for (const clip of captionClips) {
      const spoken = resolveCaptionClipSpokenText(clip, cur.pronunciations).trim();
      if (!clip.tts_enabled || !spoken) continue;
      const sig = computeCaptionClipTtsSignature(
        clip,
        cur.pronunciations,
        cur.voice,
        cur.language
      );
      const hasAudio = Boolean(curManifest.caption_artifacts?.[clip.id]?.audio_artifact_id);
      const lastSig = lastSynthesizedCaptionSignatures[clip.id];
      if (!hasAudio || lastSig !== sig) {
        staleIds.push(clip.id);
      }
    }

    if (staleIds.length === 0) return true;

    setTtsError(null);
    setBannerError(null);
    try {
      const saved = await ensureSavedBeforeJob();
      if (!saved) return false;

      const accepted = await apiClient.synthesizeProjectTts(saved.project_id, staleIds);
      let job = await apiClient.getJob(accepted.job_id);
      setActiveJob(job);
      while (job.status === "queued" || job.status === "running") {
        await new Promise((r) => window.setTimeout(r, 300));
        job = await apiClient.getJob(accepted.job_id);
        setActiveJob(job);
      }
      if (job.status !== "succeeded") {
        const msg = job.error || `Audio synthesis ${job.status}`;
        setTtsError(msg);
        setBannerError(msg);
        return false;
      }

      const bundle = await apiClient.getProject(saved.project_id);
      setManifest(bundle.manifest);
      setSourceStatus(bundle.source_status ?? null);
      const normalized = migrateProjectToMultiTrack(bundle.project, bundle.manifest);
      const sigs = buildCaptionSignatureMap(
        normalized,
        bundle.manifest,
        bundle.stale_tts_captions
      );
      setLastSynthesizedCaptionSignatures((prev) => ({ ...prev, ...sigs }));
      return true;
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to synthesize caption narration";
      setTtsError(msg);
      setBannerError(msg);
      return false;
    }
  };

  const handleSynthesizeCaptionTts = async (clipId?: string) => {
    setSynthesizingClipId(clipId ?? "all");
    try {
      await runCaptionTtsSynthesis(clipId ? [clipId] : undefined);
    } finally {
      setSynthesizingClipId(null);
    }
  };

  const handleTogglePlay = async () => {
    if (isPlaying || isProjectPlaying) {
      setIsProjectPlaying(false);
      setIsPlaying(false);
      return;
    }
    if (monitorMode === "timeline") {
      if (totalProjectDuration <= 0) return;
      setPreparingPlayback(true);
      try {
        const ok = await runCaptionTtsSynthesis();
        if (ok) {
          setIsProjectPlaying(true);
        }
      } finally {
        setPreparingPlayback(false);
      }
      return;
    }
    setIsPlaying(true);
  };

  const handleToggleProjectPlayback = async () => {
    if (isProjectPlaying) {
      setIsProjectPlaying(false);
      setIsPlaying(false);
      return;
    }
    if (totalProjectDuration <= 0) return;
    setPreparingPlayback(true);
    try {
      const ok = await runCaptionTtsSynthesis();
      if (ok) {
        setIsProjectPlaying(true);
      }
    } finally {
      setPreparingPlayback(false);
    }
  };

  const handleRenderPreview = async () => {
    if (!project || totalProjectDuration <= 0) return;
    usageLogger.logIntention("Render project preview", "preview.render");
    if (
      !dirty &&
      manifest?.latest_preview_artifact_id &&
      lastRenderedProjectPreviewSignature === computeMultiTrackPreviewSignature(project)
    ) {
      setPreviewModalOpen(true);
      return;
    }
    const saved = await ensureSavedBeforeJob();
    if (!saved) return;
    try {
      const res = await apiClient.renderPreview(saved.project_id);
      const job = await apiClient.getJob(res.job_id);
      setActiveJob(job);
    } catch (err) {
      setBannerError(err instanceof Error ? err.message : "Failed to start preview");
    }
  };

  const handleExport = async () => {
    if (!project || totalProjectDuration <= 0) return;
    usageLogger.logIntention("Export final MP4", "export.render");
    const saved = await ensureSavedBeforeJob();
    if (!saved) return;
    try {
      const res = await apiClient.exportProject(saved.project_id);
      const job = await apiClient.getJob(res.job_id);
      setActiveJob(job);
    } catch (err) {
      setBannerError(err instanceof Error ? err.message : "Failed to start export");
    }
  };

  const handleCancelJob = async (jobId: string) => {
    try {
      const job = await apiClient.cancelJob(jobId);
      setActiveJob(job);
    } catch (err) {
      setBannerError(err instanceof Error ? err.message : "Failed to cancel job");
    }
  };

  // Output resolution / aspect ratio change (re-clamps keyframes)
  const handleChangeOutputResolution = (
    rawW: number,
    rawH: number,
    coalesceKey?: string
  ) => {
    if (!project) return;
    const nextW = Math.max(2, Math.round(rawW / 2) * 2);
    const nextH = Math.max(2, Math.round(rawH / 2) * 2);
    if (!Number.isFinite(nextW) || !Number.isFinite(nextH)) return;
    const srcW = manifest?.source_metadata?.width ?? 1920;
    const srcH = manifest?.source_metadata?.height ?? 1080;

    handleUpdateProject(
      (prev) => ({
        ...prev,
        output_settings: {
          ...prev.output_settings,
          width: nextW,
          height: nextH,
        },
        camera_keyframes: (prev.camera_keyframes ?? []).map((kf) => {
          const clamped = clampCropPosition(kf.x, kf.y, kf.zoom, srcW, srcH, nextW, nextH);
          return {
            ...kf,
            x: Number(clamped.x.toFixed(4)),
            y: Number(clamped.y.toFixed(4)),
            zoom: Number(clamped.zoom.toFixed(3)),
          };
        }),
      }),
      {
        label: `Change output resolution (${nextW}×${nextH})`,
        coalesceKey,
      }
    );
  };

  // Media Bin Asset Handlers
  const handleImportAsset = async (options: {
    path?: string;
    file?: File;
    kind?: MediaAssetKind;
    copyToProject?: boolean;
  }) => {
    if (!project) return;
    setAssetImportBusy(true);
    setAssetImportError(null);
    try {
      const baseProj = dirty ? await handleSave() : project;
      if (!baseProj) return;
      const res = await apiClient.importProjectAsset(baseProj.project_id, options);
      applyBundle(res, { resetHistory: false });
      if (res.asset.kind === "video") {
        setPreviewAssetId(res.asset.asset_id);
      }
      if (res.job_id) {
        const job = await apiClient.getJob(res.job_id);
        setActiveJob(job);
      }
    } catch (err) {
      const isCancelled =
        err instanceof ApiError &&
        err.status === 400 &&
        err.detail.toLowerCase().includes("cancel");
      if (!isCancelled) {
        setAssetImportError(
          err instanceof Error ? err.message : "Failed to import asset"
        );
      }
    } finally {
      setAssetImportBusy(false);
    }
  };

  const handleDeleteAsset = async (assetId: string) => {
    if (!project) return;
    setAssetImportError(null);
    try {
      const baseProj = dirty ? await handleSave() : project;
      if (!baseProj) return;
      const res = await apiClient.deleteProjectAsset(baseProj.project_id, assetId);
      applyBundle(res, { resetHistory: false });
      if (previewAssetId === assetId) {
        setPreviewAssetId(res.project.source_media.media_id);
      }
    } catch (err) {
      setAssetImportError(
        err instanceof Error ? err.message : "Failed to remove asset"
      );
    }
  };

  const handleAddSourceClip = (
    assetId: string,
    videoIn: number,
    videoOut: number,
    startSec?: number,
    trackId?: string
  ) => {
    if (!project) return;
    const dur = Math.max(0.2, Number((videoOut - videoIn).toFixed(3)));
    const newId = findNextClipId(project, "vclip");
    const tracks = ensureDefaultTracks(project);

    let resolvedTrackId = trackId;
    if (!resolvedTrackId) {
      const unlocked = tracks.find((t): t is VideoTrack => t.kind === "video" && !t.locked);
      resolvedTrackId = unlocked?.id ?? findNextTrackId(project, "v");
    }

    const newClip: VisualClip = {
      id: newId,
      asset_id: assetId,
      kind: "video",
      start_sec: 0,
      duration_sec: dur,
      media_in: Number(videoIn.toFixed(3)),
      media_out: Number(videoOut.toFixed(3)),
      speed: 1.0,
      end_behavior: "freeze",
      volume: project.output_settings.default_source_volume,
      audio_fade_in_sec: 0,
      audio_fade_out_sec: 0,
      transform: { x: 0.5, y: 0.5, scale: 1.0, rotation_deg: 0, opacity: 1.0 },
      transition_in: { type: "none", duration_sec: 0 },
      transition_out: { type: "none", duration_sec: 0 },
    };

    const srcW = manifest?.source_metadata?.width ?? 1920;
    const srcH = manifest?.source_metadata?.height ?? 1080;
    const ideal = computeIdealCrop(
      srcW,
      srcH,
      1.0,
      project.output_settings.width,
      project.output_settings.height
    );

    handleUpdateProject(
      (prev) => {
        const curTracks = ensureDefaultTracks(prev);
        let targetTrack = curTracks.find(
          (t): t is VideoTrack => t.kind === "video" && t.id === resolvedTrackId
        );
        let nextTracks: TimelineTrack[];
        if (!targetTrack) {
          targetTrack = {
            id: resolvedTrackId!,
            name: `Video ${curTracks.filter((t) => t.kind === "video").length + 1}`,
            kind: "video",
            visible: true,
            muted: false,
            locked: false,
            camera_follow: true,
            clips: [],
            transitions: [],
          };
          nextTracks = [...curTracks, targetTrack];
        } else {
          nextTracks = curTracks;
        }
        const computedStart =
          startSec !== undefined
            ? Math.max(0, Number(startSec.toFixed(3)))
            : findNonOverlappingStart(targetTrack.clips, projectTime, dur);
        const placedClip: VisualClip = { ...newClip, start_sec: computedStart };

        nextTracks = nextTracks.map((tr) =>
          tr.kind === "video" && tr.id === resolvedTrackId
            ? {
                ...tr,
                clips: [...tr.clips, placedClip].sort((a, b) => a.start_sec - b.start_sec),
              }
            : tr
        );

        const hasKfs = (prev.camera_keyframes ?? []).length > 0;
        const kfs = hasKfs
          ? prev.camera_keyframes!
          : [
              {
                t: 0,
                x: Number((ideal.maxNormX / 2).toFixed(4)),
                y: Number((ideal.maxNormY / 2).toFixed(4)),
                zoom: 1.0,
                transition: "tween" as const,
              },
            ];

        return {
          ...prev,
          tracks: nextTracks,
          segments: [],
          camera_keyframes: kfs,
        };
      },
      { label: `Add video clip ${newId}` },
      { selection: { type: "video-clip", trackId: resolvedTrackId, clipId: newId } }
    );
    setMonitorMode("timeline");
  };

  const handlePlaceAssetOnTrack = (
    assetId: string,
    kind: MediaAssetKind,
    trackId: string,
    startSec: number
  ) => {
    if (!project) return;
    const meta = manifest?.media_metadata?.[assetId];
    if (kind === "audio") {
      const dur = Math.max(0.5, Number((meta?.duration || 3.0).toFixed(3)));
      const newId = findNextClipId(project, "aclip");
      handleUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "audio" || tr.id !== trackId || tr.locked) return tr;
            const clip: AudioClip = {
              id: newId,
              asset_id: assetId,
              start_sec: Math.max(0, Number(startSec.toFixed(3))),
              duration_sec: dur,
              media_in: 0,
              volume: 1.0,
              speed: 1.0,
              fade_in_sec: 0,
              fade_out_sec: 0,
              loop: false,
            };
            return {
              ...tr,
              clips: [...tr.clips, clip].sort((a, b) => a.start_sec - b.start_sec),
            };
          }),
        { label: `Add audio clip ${newId}` },
        { type: "audio-clip", trackId, clipId: newId }
      );
      return;
    }

    if (kind === "image") {
      const newId = findNextClipId(project, "vclip");
      handleUpdateTracks(
        (prevTracks) =>
          prevTracks.map((tr) => {
            if (tr.kind !== "video" || tr.id !== trackId || tr.locked) return tr;
            const clip: VisualClip = {
              id: newId,
              asset_id: assetId,
              kind: "image",
              start_sec: Math.max(0, Number(startSec.toFixed(3))),
              duration_sec: 3.0,
              media_in: 0,
              media_out: null,
              speed: 1.0,
              end_behavior: "freeze",
              volume: 1.0,
              audio_fade_in_sec: 0,
              audio_fade_out_sec: 0,
              transform: { x: 0.5, y: 0.5, scale: 1.0, rotation_deg: 0, opacity: 1.0 },
              transition_in: { type: "none", duration_sec: 0 },
              transition_out: { type: "none", duration_sec: 0 },
            };
            return {
              ...tr,
              clips: [...tr.clips, clip].sort((a, b) => a.start_sec - b.start_sec),
            };
          }),
        { label: `Add image overlay ${newId}` },
        { type: "video-clip", trackId, clipId: newId }
      );
      setMonitorMode("timeline");
      return;
    }

    const dur = Math.max(0.2, Number((meta?.duration || 3.0).toFixed(3)));
    handleAddSourceClip(assetId, 0, dur, startSec, trackId);
  };

  const handleAddAssetAtPlayhead = (asset: MediaAssetRef) => {
    if (!project) return;
    const tracks = ensureDefaultTracks(project);

    if (asset.kind === "audio") {
      const existingA = tracks.find((t): t is AudioTrack => t.kind === "audio" && !t.locked);
      if (existingA) {
        const meta = manifest?.media_metadata?.[asset.asset_id];
        const start = findNonOverlappingStart(
          existingA.clips,
          projectTime,
          meta?.duration || 3.0
        );
        handlePlaceAssetOnTrack(asset.asset_id, "audio", existingA.id, start);
      } else {
        const newTid = findNextTrackId(project, "a");
        const meta = manifest?.media_metadata?.[asset.asset_id];
        const newCid = findNextClipId(project, "aclip");
        const newClip: AudioClip = {
          id: newCid,
          asset_id: asset.asset_id,
          start_sec: Math.max(0, Number(projectTime.toFixed(3))),
          duration_sec: Math.max(0.5, Number((meta?.duration || 3.0).toFixed(3))),
          media_in: 0,
          volume: 1.0,
          speed: 1.0,
          fade_in_sec: 0,
          fade_out_sec: 0,
          loop: false,
        };
        handleUpdateTracks(
          (prev) => [
            ...prev,
            {
              id: newTid,
              name: `Audio ${prev.filter((t) => t.kind === "audio").length + 1}`,
              kind: "audio",
              volume: 1.0,
              muted: false,
              solo: false,
              duck_under_narration: true,
              locked: false,
              clips: [newClip],
            },
          ],
          { label: `Add audio track & clip ${newCid}` },
          { type: "audio-clip", trackId: newTid, clipId: newCid }
        );
      }
      return;
    }

    if (asset.kind === "image") {
      // Layer image overlays on an overlay track (!camera_follow) above V1 automatically
      const overlayTrack = tracks.find(
        (t): t is VideoTrack => t.kind === "video" && !t.camera_follow && !t.locked
      );
      if (overlayTrack) {
        const start = findNonOverlappingStart(overlayTrack.clips, projectTime, 3.0);
        handlePlaceAssetOnTrack(asset.asset_id, "image", overlayTrack.id, start);
      } else {
        const newTid = findNextTrackId(project, "v");
        const newCid = findNextClipId(project, "vclip");
        const newClip: VisualClip = {
          id: newCid,
          asset_id: asset.asset_id,
          kind: "image",
          start_sec: Math.max(0, Number(projectTime.toFixed(3))),
          duration_sec: 3.0,
          media_in: 0,
          media_out: null,
          speed: 1.0,
          end_behavior: "freeze",
          volume: 1.0,
          audio_fade_in_sec: 0,
          audio_fade_out_sec: 0,
          transform: { x: 0.5, y: 0.5, scale: 0.65, rotation_deg: 0, opacity: 1.0 },
          transition_in: { type: "none", duration_sec: 0 },
          transition_out: { type: "none", duration_sec: 0 },
        };
        handleUpdateTracks(
          (prev) => [
            ...prev,
            {
              id: newTid,
              name: `Overlay ${prev.filter((t) => t.kind === "video").length + 1}`,
              kind: "video",
              visible: true,
              muted: false,
              locked: false,
              camera_follow: false,
              clips: [newClip],
              transitions: [],
            },
          ],
          { label: `Add image overlay layer ${newCid}` },
          { type: "video-clip", trackId: newTid, clipId: newCid }
        );
        setMonitorMode("timeline");
      }
      return;
    }

    // Video asset
    const meta =
      manifest?.media_metadata?.[asset.asset_id] ??
      (asset.asset_id === project.source_media.media_id ? manifest?.source_metadata : undefined);
    const dur = Math.max(0.5, Number((meta?.duration || 3.0).toFixed(3)));
    handleAddSourceClip(asset.asset_id, 0, Math.min(dur, meta?.duration || dur));
  };

  // New Project Media Selection
  const handleSelectMediaNative = async () => {
    setMediaSelectBusy(true);
    setNewProjectError(null);
    try {
      const res = await apiClient.selectMedia({ copyToProject });
      setSelectedMedia(res);
      if (!newTitle.trim()) {
        const base =
          res.path.split(/[\\/]/).pop()?.replace(/\.[^.]+$/, "") || "New Sims Project";
        setNewTitle(base);
      }
    } catch (err) {
      const isCancelled =
        err instanceof ApiError &&
        err.status === 400 &&
        err.detail.toLowerCase().includes("cancel");
      if (!isCancelled) {
        setNewProjectError(
          err instanceof Error ? err.message : "Media selection failed"
        );
      }
    } finally {
      setMediaSelectBusy(false);
    }
  };

  const handleUploadMedia = async (file: File) => {
    setMediaSelectBusy(true);
    setNewProjectError(null);
    setSelectedMedia(null);
    try {
      const res = await apiClient.selectMedia({ file, copyToProject });
      setSelectedMedia(res);
      if (!newTitle.trim()) {
        const base =
          file.name.replace(/\.[^.]+$/, "") ||
          res.path.split(/[\\/]/).pop()?.replace(/\.[^.]+$/, "") ||
          "New Sims Project";
        setNewTitle(base);
      }
    } catch (err) {
      setNewProjectError(err instanceof Error ? err.message : "Media import failed");
    } finally {
      setMediaSelectBusy(false);
    }
  };

  const handleCreateProject = async () => {
    if (!selectedMedia) return;
    const title = newTitle.trim() || "Untitled Sims Short";
    setCreateBusy(true);
    setNewProjectError(null);
    try {
      const res = await apiClient.createProject({
        media_id: selectedMedia.media_id,
        title,
        copy_to_project: copyToProject,
      });
      const pid = res.project_id || res.project?.project_id;
      if (res.job_id) {
        const job = await apiClient.getJob(res.job_id);
        setActiveJob(job);
      }
      setNewProjectOpen(false);
      setSelectedMedia(null);
      setNewProjectError(null);
      setNewTitle("");
      await refreshProjectList();
      if (pid) {
        await loadProject(pid);
      }
    } catch (err) {
      setNewProjectError(
        err instanceof Error ? err.message : "Failed to create project"
      );
    } finally {
      setCreateBusy(false);
    }
  };

  const handleRelink = async (mode: "native" | "path" | "file") => {
    if (!project) return;
    setRelinkBusy(true);
    setBannerError(null);
    try {
      const res = await apiClient.relinkSource(project.project_id, {
        path: mode === "path" ? relinkPath.trim() : undefined,
        file: mode === "file" ? relinkFile ?? undefined : undefined,
      });
      applyBundle(res);
      if (res.job_id) {
        const job = await apiClient.getJob(res.job_id);
        setActiveJob(job);
      }
      setRelinkOpen(false);
      setRelinkPath("");
      setRelinkFile(null);
    } catch (err) {
      setBannerError(err instanceof Error ? err.message : "Source relink failed");
    } finally {
      setRelinkBusy(false);
    }
  };

  const isJobBusy =
    activeJob?.status === "queued" || activeJob?.status === "running";
  const isModalOpen =
    newProjectOpen ||
    relinkOpen ||
    previewModalOpen ||
    exportModalOpen ||
    logModalOpen;
  isModalOpenRef.current = isModalOpen;
  const sourceBroken =
    sourceStatus &&
    (sourceStatus.status === "missing" || sourceStatus.status === "modified");

  const canUndo = historyState.past.length > 0;
  const canRedo = historyState.future.length > 0;
  const lastUndoLabel = canUndo
    ? historyState.past[historyState.past.length - 1].label
    : null;
  const nextRedoLabel = canRedo ? historyState.future[0].label : null;
  const hasTimelineContent = totalProjectDuration > 0;

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-[#0f1115] text-zinc-100">
      {/* Persistent Top Header */}
      <header className="flex h-13 shrink-0 items-center justify-between gap-3 border-b border-zinc-800 bg-[#13161c] px-4">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <Film className="h-5 w-5 text-emerald-400" />
            <span className="text-sm font-bold tracking-tight text-zinc-100">
              Sims 4 Vertical Video Studio
            </span>
          </div>

          <select
            aria-label="Project Selector"
            value={project?.project_id ?? ""}
            onChange={(e) => {
              if (e.target.value) loadProject(e.target.value);
            }}
            className="max-w-[220px] rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-100 focus:border-emerald-500 focus:outline-none"
          >
            <option value="" disabled>
              Select a project...
            </option>
            {projects.map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.title}
              </option>
            ))}
          </select>

          <button
            type="button"
            onClick={() => setNewProjectOpen(true)}
            className="inline-flex items-center gap-1.5 rounded border border-zinc-700 bg-zinc-800 px-2.5 py-1 text-xs font-medium text-zinc-100 hover:bg-zinc-700"
          >
            <FolderPlus className="h-3.5 w-3.5 text-emerald-400" />
            + New Project
          </button>

          <button
            type="button"
            data-log-intent="Open Usage & Debug Log"
            onClick={() => setLogModalOpen(true)}
            className="inline-flex items-center gap-1.5 rounded border border-zinc-700 bg-zinc-800 px-2.5 py-1 text-xs font-medium text-zinc-200 hover:bg-zinc-700"
          >
            <ScrollText className="h-3.5 w-3.5 text-sky-400" />
            Logs
          </button>
        </div>

        <div className="flex items-center gap-2">
          {project && (
            <>
              <div className="flex items-center gap-1.5 rounded border border-zinc-700 bg-zinc-800/80 px-2 py-0.5 text-xs">
                <span className="text-[11px] text-zinc-400">Output</span>
                <select
                  aria-label="Output Aspect Ratio"
                  value={
                    customResolutionMode
                      ? "custom"
                      : (OUTPUT_ASPECT_PRESETS.find(
                          (p) =>
                            p.width === project.output_settings.width &&
                            p.height === project.output_settings.height
                        )?.label ?? "custom")
                  }
                  onChange={(e) => {
                    if (e.target.value === "custom") {
                      setCustomResolutionMode(true);
                      return;
                    }
                    const preset = OUTPUT_ASPECT_PRESETS.find(
                      (p) => p.label === e.target.value
                    );
                    if (preset) {
                      setCustomResolutionMode(false);
                      handleChangeOutputResolution(preset.width, preset.height);
                    }
                  }}
                  className="bg-transparent text-xs text-zinc-100 focus:outline-none"
                >
                  {OUTPUT_ASPECT_PRESETS.map((p) => (
                    <option key={p.label} value={p.label} className="bg-zinc-900">
                      {p.label}
                    </option>
                  ))}
                  <option value="custom" className="bg-zinc-900">
                    Custom ({project.output_settings.width}×{project.output_settings.height})
                  </option>
                </select>
                {(customResolutionMode ||
                  !OUTPUT_ASPECT_PRESETS.some(
                    (p) =>
                      p.width === project.output_settings.width &&
                      p.height === project.output_settings.height
                  )) && (
                  <div className="flex items-center gap-1">
                    <input
                      type="number"
                      min={2}
                      step={2}
                      aria-label="Custom Output Width"
                      value={project.output_settings.width}
                      onChange={(e) =>
                        handleChangeOutputResolution(
                          Number(e.target.value) || 2,
                          project.output_settings.height,
                          "output-width"
                        )
                      }
                      className="w-14 rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 font-mono text-[11px] text-zinc-100"
                    />
                    <span className="text-zinc-500">×</span>
                    <input
                      type="number"
                      min={2}
                      step={2}
                      aria-label="Custom Output Height"
                      value={project.output_settings.height}
                      onChange={(e) =>
                        handleChangeOutputResolution(
                          project.output_settings.width,
                          Number(e.target.value) || 2,
                          "output-height"
                        )
                      }
                      className="w-14 rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 font-mono text-[11px] text-zinc-100"
                    />
                  </div>
                )}
              </div>

              <div className="flex items-center gap-1">
                <button
                  type="button"
                  aria-label="Undo"
                  disabled={!canUndo}
                  onClick={handleUndo}
                  title={
                    lastUndoLabel
                      ? `Undo: ${lastUndoLabel} (Ctrl+Z)`
                      : "Nothing to undo (Ctrl+Z)"
                  }
                  className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs font-medium text-zinc-200 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Undo2 className="h-3.5 w-3.5" />
                  <span className="hidden md:inline">Undo</span>
                </button>
                <button
                  type="button"
                  aria-label="Redo"
                  disabled={!canRedo}
                  onClick={handleRedo}
                  title={
                    nextRedoLabel
                      ? `Redo: ${nextRedoLabel} (Ctrl+Shift+Z)`
                      : "Nothing to redo (Ctrl+Shift+Z)"
                  }
                  className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs font-medium text-zinc-200 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Redo2 className="h-3.5 w-3.5" />
                  <span className="hidden md:inline">Redo</span>
                </button>
              </div>

              <span className="hidden sm:inline text-[11px] font-mono text-zinc-400">
                rev {project.revision}
                {dirty ? " • unsaved" : ""}
              </span>

              <button
                type="button"
                disabled={saving}
                onClick={handleSave}
                className={`inline-flex items-center gap-1.5 rounded px-3 py-1 text-xs font-medium transition ${
                  dirty
                    ? "bg-amber-500 text-zinc-950 hover:bg-amber-400"
                    : "border border-zinc-700 bg-zinc-800 text-zinc-200 hover:bg-zinc-700"
                }`}
              >
                {saving ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Save className="h-3.5 w-3.5" />
                )}
                Save
              </button>

              {manifest?.latest_export_artifact_id && (
                <button
                  type="button"
                  onClick={() => setExportModalOpen(true)}
                  className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2.5 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
                >
                  <Download className="h-3.5 w-3.5" />
                  Latest Export
                </button>
              )}

              <button
                type="button"
                disabled={isJobBusy || !hasTimelineContent}
                onClick={handleExport}
                className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-3 py-1 text-xs font-semibold text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Download className="h-3.5 w-3.5" />
                Export{" "}
                {formatAspectRatio(
                  project.output_settings.width,
                  project.output_settings.height
                )}{" "}
                MP4
              </button>
            </>
          )}
        </div>
      </header>

      {/* Missing/Modified Source Warning Banner */}
      {project && sourceBroken && (
        <div
          className="flex items-center justify-between gap-3 border-b border-rose-500/40 bg-rose-500/15 px-4 py-2 text-xs text-rose-200"
          role="alert"
        >
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0 text-rose-400" />
            <span>
              Source recording is {sourceStatus.status}:{" "}
              {sourceStatus.reason ?? project.source_media.path}
            </span>
          </div>
          <button
            type="button"
            onClick={() => setRelinkOpen(true)}
            className="inline-flex items-center gap-1 rounded border border-rose-400/50 bg-rose-500/20 px-2.5 py-1 font-medium text-rose-100 hover:bg-rose-500/30"
          >
            <Link2 className="h-3.5 w-3.5" />
            Relink Source
          </button>
        </div>
      )}

      {/* General Error / Revision Conflict Banner */}
      {bannerError && (
        <div
          className="flex items-center justify-between gap-3 border-b border-amber-500/40 bg-amber-500/15 px-4 py-1.5 text-xs text-amber-200"
          role="alert"
        >
          <span>{bannerError}</span>
          <div className="flex items-center gap-2">
            {revisionConflict && project && (
              <button
                type="button"
                onClick={() => loadProject(project.project_id)}
                className="inline-flex items-center gap-1 rounded bg-amber-500/25 px-2 py-0.5 font-medium hover:bg-amber-500/40"
              >
                <RefreshCw className="h-3 w-3" />
                Reload Latest
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                setBannerError(null);
                setRevisionConflict(false);
              }}
              className="rounded p-0.5 hover:bg-amber-500/20"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}

      {/* Main Three-Zone Multi-Track NLE Workspace */}
      {!project || !manifest ? (
        <main className="flex flex-1 flex-col items-center justify-center gap-4 p-6 text-center">
          <Film className="h-12 w-12 text-zinc-600" />
          <div className="space-y-1">
            <h1 className="text-lg font-semibold text-zinc-200">
              No Project Loaded
            </h1>
            <p className="max-w-md text-xs text-zinc-400">
              Import gameplay recordings, overlay images, audio tracks, and edit
              decoupled captions &amp; camera keyframes across multi-layer tracks.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setNewProjectOpen(true)}
            className="inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500"
          >
            <FolderPlus className="h-4 w-4" />
            + New Project
          </button>
        </main>
      ) : (
        <main className="flex min-h-0 flex-1 flex-col gap-3 p-3 overflow-hidden">
          {/* Upper Zone: Media Bin + Stage Compositor + Contextual Inspector */}
          <div className="flex min-h-0 flex-1 flex-col gap-3 lg:flex-row">
            <MediaBin
              project={project}
              manifest={manifest}
              nativePickerSupported={capabilities?.native_picker_supported}
              importBusy={assetImportBusy}
              importError={assetImportError}
              previewAssetId={previewAssetId}
              onImportAsset={handleImportAsset}
              onDeleteAsset={handleDeleteAsset}
              onAddAssetAtPlayhead={handleAddAssetAtPlayhead}
              onPreviewVideoAsset={(assetId) => {
                setPreviewAssetId(assetId);
                setMonitorMode("source");
                setSourceTime(0);
                setIsProjectPlaying(false);
                setIsPlaying(false);
              }}
            />

            <CropCanvas
              project={project}
              manifest={manifest}
              selection={selection}
              projectTime={projectTime}
              sourceTime={sourceTime}
              monitorMode={monitorMode}
              previewAssetId={previewAssetId}
              isPlaying={isPlaying}
              isProjectPlaying={isProjectPlaying}
              preparingPlayback={preparingPlayback}
              canPreview={hasTimelineContent}
              previewBusy={Boolean(isJobBusy && activeJob?.job_type === "preview")}
              previewModalOpen={previewModalOpen}
              onTogglePlay={handleTogglePlay}
              onSourceTimeChange={(t) => setSourceTime(t)}
              onUpdateCameraKeyframes={handleUpdateCameraKeyframes}
              onUpdateTracks={handleUpdateTracks}
              onBeginHistoryTransaction={handleBeginHistoryTransaction}
              onEndHistoryTransaction={handleEndHistoryTransaction}
              onRenderPreview={handleRenderPreview}
              onSetPreviewModalOpen={setPreviewModalOpen}
              onSelectItem={setSelection}
              onSetMonitorMode={(mode) => {
                setMonitorMode(mode);
                setIsPlaying(false);
                setIsProjectPlaying(false);
              }}
            />

            <ContextualInspector
              project={project}
              manifest={manifest}
              selection={selection}
              projectTime={projectTime}
              voices={capabilities?.voices ?? []}
              ttsError={ttsError}
              synthesizingClipId={synthesizingClipId}
              lastSynthesizedCaptionSignatures={lastSynthesizedCaptionSignatures}
              onUpdateProject={handleUpdateProject}
              onUpdateTracks={handleUpdateTracks}
              onUpdateCameraKeyframes={handleUpdateCameraKeyframes}
              onSynthesizeCaptionTts={handleSynthesizeCaptionTts}
              onSelectItem={setSelection}
            />
          </div>

          {/* Lower Multi-Track Layered NLE Timeline */}
          <Timeline
            project={project}
            manifest={manifest}
            selection={selection}
            projectTime={projectTime}
            sourceTime={sourceTime}
            previewAssetId={previewAssetId}
            isModalOpen={isModalOpen}
            isProjectPlaying={isProjectPlaying}
            preparingPlayback={preparingPlayback}
            onSelectItem={setSelection}
            onProjectTimeChange={(t) => {
              setMonitorMode("timeline");
              setProjectTime(t);
            }}
            onSourceTimeChange={(t) => {
              setIsProjectPlaying(false);
              setIsPlaying(false);
              setMonitorMode("source");
              setSourceTime(t);
            }}
            onUpdateTracks={handleUpdateTracks}
            onUpdateCameraKeyframes={handleUpdateCameraKeyframes}
            onBeginHistoryTransaction={handleBeginHistoryTransaction}
            onEndHistoryTransaction={handleEndHistoryTransaction}
            onAddSourceClip={handleAddSourceClip}
            onDropMediaAsset={handlePlaceAssetOnTrack}
            onToggleProjectPlayback={handleToggleProjectPlayback}
          />
        </main>
      )}

      {/* Bottom JobStatusBar */}
      <JobStatusBar
        activeJob={activeJob}
        outputWidth={project?.output_settings.width}
        outputHeight={project?.output_settings.height}
        outputFps={project?.output_settings.fps}
        onCancel={handleCancelJob}
        onDismiss={() => setActiveJob(null)}
      />

      {/* New Project Modal */}
      {newProjectOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="New Project"
        >
          <div className="w-full max-w-lg rounded-xl border border-zinc-700 bg-[#151821] p-5 shadow-2xl space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-zinc-100">
                Create New Project
              </h2>
              <button
                type="button"
                onClick={() => {
                  setNewProjectOpen(false);
                  setNewProjectError(null);
                }}
                className="rounded p-1 text-zinc-400 hover:bg-zinc-800"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="space-y-3 text-xs">
              <div className="space-y-1.5">
                <span className="block font-medium text-zinc-300">
                  1. Select Gameplay Recording
                </span>
                {!capabilities?.native_picker_supported && (
                  <input
                    ref={newProjectFileInputRef}
                    type="file"
                    accept="video/mp4,video/*"
                    className="hidden"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      e.target.value = "";
                      if (file) {
                        void handleUploadMedia(file);
                      }
                    }}
                  />
                )}
                <button
                  type="button"
                  disabled={mediaSelectBusy}
                  onClick={() => {
                    if (capabilities?.native_picker_supported) {
                      void handleSelectMediaNative();
                    } else {
                      newProjectFileInputRef.current?.click();
                    }
                  }}
                  className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-3 py-1.5 font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
                >
                  {mediaSelectBusy ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <FolderOpen className="h-3.5 w-3.5" />
                  )}
                  {mediaSelectBusy
                    ? capabilities?.native_picker_supported
                      ? "Waiting for file picker…"
                      : "Importing…"
                    : "Select Recording"}
                </button>
              </div>

              {newProjectError && (
                <div
                  role="alert"
                  className="flex items-start gap-2 rounded-lg border border-rose-500/40 bg-rose-500/15 p-2.5 text-rose-200"
                >
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-rose-400" />
                  <span>{newProjectError}</span>
                </div>
              )}

              {selectedMedia && (
                <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-2.5 text-emerald-200">
                  <div className="font-medium">Media Validated</div>
                  <div className="truncate font-mono text-[11px] text-emerald-300">
                    {selectedMedia.path}
                  </div>
                  <div className="mt-1 text-[11px] text-emerald-200/80">
                    {selectedMedia.metadata.width}×{selectedMedia.metadata.height} •{" "}
                    {selectedMedia.metadata.fps.toFixed(2)}fps •{" "}
                    {selectedMedia.metadata.duration.toFixed(2)}s
                  </div>
                </div>
              )}

              <label className="block space-y-1">
                <span className="font-medium text-zinc-300">2. Project Title</span>
                <input
                  type="text"
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  placeholder="The Curse of Ravenswood Manor"
                  className="w-full rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-zinc-100 focus:border-emerald-500 focus:outline-none"
                />
              </label>

              <label className="flex items-center gap-2 text-zinc-300">
                <input
                  type="checkbox"
                  checked={copyToProject}
                  onChange={(e) => setCopyToProject(e.target.checked)}
                />
                <span>Copy source recording into project folder (portable)</span>
              </label>
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => {
                  setNewProjectOpen(false);
                  setNewProjectError(null);
                }}
                className="rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={!selectedMedia || createBusy}
                onClick={handleCreateProject}
                className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-40"
              >
                {createBusy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                Create Project
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Relink Source Modal */}
      {relinkOpen && project && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Relink Source Media"
        >
          <div className="w-full max-w-md rounded-xl border border-zinc-700 bg-[#151821] p-5 shadow-2xl space-y-4 text-xs">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-zinc-100">
                Relink Missing Source Media
              </h2>
              <button
                type="button"
                onClick={() => setRelinkOpen(false)}
                className="rounded p-1 text-zinc-400 hover:bg-zinc-800"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <button
              type="button"
              disabled={relinkBusy}
              onClick={() => handleRelink("native")}
              className="w-full rounded bg-emerald-600 py-2 font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
            >
              Open Native File Picker
            </button>

            <div className="flex gap-2">
              <input
                type="text"
                value={relinkPath}
                onChange={(e) => setRelinkPath(e.target.value)}
                placeholder="Or paste replacement path..."
                className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-zinc-100"
              />
              <button
                type="button"
                disabled={!relinkPath.trim() || relinkBusy}
                onClick={() => handleRelink("path")}
                className="rounded border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
              >
                Relink
              </button>
            </div>

            <div className="flex items-center gap-2">
              <input
                type="file"
                accept="video/mp4,video/*"
                onChange={(e) => setRelinkFile(e.target.files?.[0] ?? null)}
                className="flex-1 text-zinc-400"
              />
              <button
                type="button"
                disabled={!relinkFile || relinkBusy}
                onClick={() => handleRelink("file")}
                className="rounded border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
              >
                Upload
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Export Result Modal */}
      {exportModalOpen && project && manifest?.latest_export_artifact_id && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Exported Vertical Video"
        >
          <div className="flex max-h-[90vh] w-full max-w-md flex-col overflow-hidden rounded-xl border border-zinc-700 bg-[#151821] shadow-2xl">
            <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-2.5">
              <span className="text-sm font-semibold text-zinc-100">
                Export Ready ({project.output_settings.width}×
                {project.output_settings.height} MP4)
              </span>
              <button
                type="button"
                onClick={() => setExportModalOpen(false)}
                className="rounded p-1 text-zinc-400 hover:bg-zinc-800"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex items-center justify-center bg-black p-3">
              <video
                src={apiClient.mediaUrl(
                  project.project_id,
                  manifest.latest_export_artifact_id
                )}
                controls
                autoPlay
                className="max-h-[70vh] w-auto rounded"
              />
            </div>
            <div className="flex items-center justify-between border-t border-zinc-800 px-4 py-2.5 text-xs">
              <span className="truncate font-mono text-zinc-400">
                {manifest.latest_export_path}
              </span>
              <a
                href={apiClient.mediaUrl(
                  project.project_id,
                  manifest.latest_export_artifact_id
                )}
                download={`${project.project_id}.mp4`}
                className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-3 py-1.5 font-medium text-white hover:bg-emerald-500"
              >
                <Download className="h-3.5 w-3.5" />
                Download MP4
              </a>
            </div>
          </div>
        </div>
      )}

      <ActivityLogModal
        open={logModalOpen}
        projectId={project?.project_id ?? null}
        onClose={() => setLogModalOpen(false)}
      />
    </div>
  );
}
