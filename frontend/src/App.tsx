import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Download,
  Film,
  FolderPlus,
  Link2,
  Loader2,
  RefreshCw,
  Save,
  ScrollText,
  Upload,
  X,
} from "lucide-react";
import { ApiError, apiClient } from "./api/client";
import { ActivityLogModal } from "./components/ActivityLogModal";
import { CropCanvas } from "./components/CropCanvas";
import { ContextualInspector } from "./components/ContextualInspector";
import { JobStatusBar } from "./components/JobStatusBar";
import { Timeline } from "./components/Timeline";
import type {
  ArtifactManifest,
  Capabilities,
  JobState,
  MediaSelectResult,
  ProjectBundle,
  ProjectInput,
  ProjectSummary,
  SegmentInput,
  SourceStatus,
  TimelineSelection,
} from "./types/studio";
import {
  clampCropPosition,
  computeClientPreviewSignature,
  computeClientTtsSignature,
  computeDurationPlan,
  computeIdealCrop,
  formatAspectRatio,
} from "./utils/studioMath";
import { usageLogger } from "./utils/usageLogger";

const OUTPUT_ASPECT_PRESETS = [
  { label: "9:16 (1080×1920)", width: 1080, height: 1920 },
  { label: "16:9 (1920×1080)", width: 1920, height: 1080 },
  { label: "1:1 (1080×1080)", width: 1080, height: 1080 },
  { label: "4:5 (1080×1350)", width: 1080, height: 1350 },
  { label: "4:3 (1440×1080)", width: 1440, height: 1080 },
] as const;

export default function App() {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [project, setProject] = useState<ProjectInput | null>(null);
  const [manifest, setManifest] = useState<ArtifactManifest | null>(null);
  const [sourceStatus, setSourceStatus] = useState<SourceStatus | null>(null);
  const [selectedSegId, setSelectedSegId] = useState<string | null>(null);
  const [selection, setSelection] = useState<TimelineSelection>(null);

  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [bannerError, setBannerError] = useState<string | null>(null);
  const [revisionConflict, setRevisionConflict] = useState(false);

  const [sourceTime, setSourceTime] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isProjectPlaying, setIsProjectPlaying] = useState(false);

  const [activeJob, setActiveJob] = useState<JobState | null>(null);
  const [ttsError, setTtsError] = useState<string | null>(null);
  const [lastSynthesizedSignatures, setLastSynthesizedSignatures] = useState<
    Record<string, string>
  >({});
  const [lastRenderedPreviewSignatures, setLastRenderedPreviewSignatures] =
    useState<Record<string, string>>({});

  // Modals
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [copyToProject, setCopyToProject] = useState(false);
  const [selectedMedia, setSelectedMedia] = useState<MediaSelectResult | null>(
    null
  );
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

  const projectRef = useRef<ProjectInput | null>(null);
  projectRef.current = project;
  const selectedSegIdRef = useRef<string | null>(null);
  selectedSegIdRef.current = selectedSegId;

  useEffect(() => {
    usageLogger.init(() => ({
      projectId: projectRef.current?.project_id ?? null,
      segId: selectedSegIdRef.current ?? null,
    }));
  }, []);

  const applyBundle = useCallback((bundle: ProjectBundle) => {
    setProject(bundle.project);
    setManifest(bundle.manifest);
    setSourceStatus(bundle.source_status ?? null);
    setDirty(false);
    setRevisionConflict(false);
    setSelectedSegId((prev) => {
      if (
        prev &&
        bundle.project.segments.some((s) => s.id === prev)
      ) {
        return prev;
      }
      return bundle.project.segments[0]?.id ?? null;
    });
    const sigs: Record<string, string> = {};
    const previewSigs: Record<string, string> = {};
    for (const seg of bundle.project.segments) {
      if (bundle.manifest.segments[seg.id]?.audio_artifact_id) {
        sigs[seg.id] = computeClientTtsSignature(
          seg,
          bundle.project.pronunciations,
          bundle.project.voice,
          bundle.project.language,
          bundle.project.output_settings.inter_clause_pause_ms
        );
      }
      if (bundle.manifest.segments[seg.id]?.preview_artifact_id) {
        previewSigs[seg.id] = computeClientPreviewSignature(seg, bundle.project);
      }
    }
    setLastSynthesizedSignatures((prev) => ({ ...sigs, ...prev }));
    setLastRenderedPreviewSignatures((prev) => ({ ...previewSigs, ...prev }));
  }, []);

  const loadProject = useCallback(
    async (projectId: string) => {
      usageLogger.logIntention("Load project", "project.load", {
        projectId,
      });
      try {
        setBannerError(null);
        const bundle = await apiClient.getProject(projectId);
        applyBundle(bundle);
        const url = new URL(window.location.href);
        url.searchParams.set("project", projectId);
        window.history.replaceState({}, "", url.toString());
        usageLogger.logActionOutput(
          "Load project",
          "project.load",
          {
            title: bundle.project.title,
            revision: bundle.project.revision,
            segment_count: bundle.project.segments.length,
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

  // Initial bootstrap: capabilities + project list + ?project= query param
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
              const sigs: Record<string, string> = {};
              for (const seg of projectRef.current.segments) {
                if (bundle.manifest.segments[seg.id]?.audio_artifact_id) {
                  sigs[seg.id] = computeClientTtsSignature(
                    seg,
                    projectRef.current.pronunciations,
                    projectRef.current.voice,
                    projectRef.current.language,
                    projectRef.current.output_settings.inter_clause_pause_ms
                  );
                }
              }
              setLastSynthesizedSignatures((prev) => ({ ...prev, ...sigs }));
            }
            if (updated.job_type === "preview") {
              if (updated.seg_id && projectRef.current) {
                const seg = projectRef.current.segments.find(
                  (s) => s.id === updated.seg_id
                );
                if (seg) {
                  const pSig = computeClientPreviewSignature(
                    seg,
                    projectRef.current
                  );
                  setLastRenderedPreviewSignatures((prev) => ({
                    ...prev,
                    [seg.id]: pSig,
                  }));
                }
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
            } else {
              usageLogger.logActionOutput(
                `Job ${updated.job_type} ${updated.status}`,
                `job.${updated.job_type}.${updated.status}`,
                { status: updated.status, stage: updated.stage },
                {
                  projectId: updated.project_id,
                  segId: updated.seg_id,
                  jobId: updated.job_id,
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

  // Continuous project playback timer following DurationPlan
  useEffect(() => {
    if (!isProjectPlaying || !project || !manifest || project.segments.length === 0) {
      return;
    }
    let segIdx = Math.max(
      0,
      project.segments.findIndex((s) => s.id === selectedSegId)
    );
    let segElapsed = 0;
    const srcDur = manifest.source_metadata?.duration ?? 0;
    const tailPad = project.output_settings.tail_padding_sec;

    setIsPlaying(true);
    setSelectedSegId(project.segments[segIdx].id);
    setSourceTime(project.segments[segIdx].video_in);

    const stepMs = 50;
    const timer = window.setInterval(() => {
      const seg = project.segments[segIdx];
      if (!seg) {
        setIsProjectPlaying(false);
        setIsPlaying(false);
        return;
      }
      const audioDur = manifest.segments[seg.id]?.audio_duration ?? 0;
      const plan = computeDurationPlan(seg, audioDur, srcDur, tailPad);

      segElapsed += stepMs / 1000;
      if (segElapsed >= plan.finalDur) {
        segIdx += 1;
        segElapsed = 0;
        if (segIdx >= project.segments.length) {
          setIsProjectPlaying(false);
          setIsPlaying(false);
          return;
        }
        const nextSeg = project.segments[segIdx];
        setSelectedSegId(nextSeg.id);
        setSourceTime(nextSeg.video_in);
      } else {
        const clampedSrcTime = Math.min(
          seg.video_out,
          seg.video_in + segElapsed
        );
        setSourceTime(clampedSrcTime);
      }
    }, stepMs);

    return () => {
      window.clearInterval(timer);
    };
  }, [isProjectPlaying, project, manifest, selectedSegId]);

  const selectedSegment = useMemo(() => {
    if (!project || !selectedSegId) return null;
    return project.segments.find((s) => s.id === selectedSegId) ?? null;
  }, [project, selectedSegId]);

  const handleUpdateProject = (
    updater: (prev: ProjectInput) => ProjectInput
  ) => {
    setProject((prev) => (prev ? updater(prev) : prev));
    setDirty(true);
  };

  const handleUpdateSegment = (updated: SegmentInput) => {
    handleUpdateProject((prev) => ({
      ...prev,
      segments: prev.segments.map((s) => (s.id === updated.id ? updated : s)),
    }));
  };

  const handleSave = async (): Promise<ProjectInput | null> => {
    if (!project) return null;
    usageLogger.logIntention("Save project", "project.save", {
      projectId: project.project_id,
      input: {
        expected_revision: project.revision,
        segment_count: project.segments.length,
      },
    });
    setSaving(true);
    setBannerError(null);
    setRevisionConflict(false);
    try {
      const bundle = await apiClient.saveProject(
        project.project_id,
        project,
        project.revision
      );
      applyBundle(bundle);
      await refreshProjectList();
      usageLogger.logActionOutput(
        "Save project",
        "project.save",
        {
          revision: bundle.project.revision,
          segment_count: bundle.project.segments.length,
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
        projectId: project.project_id,
        context: { expected_revision: project.revision },
      });
      return null;
    } finally {
      setSaving(false);
    }
  };

  const ensureSavedBeforeJob = async (): Promise<ProjectInput | null> => {
    if (!project) return null;
    if (!dirty) return project;
    return handleSave();
  };

  const handleRenderPreview = async (segId: string) => {
    usageLogger.logIntention("Render segment preview", "preview.render", {
      segId,
    });
    if (project && !dirty && manifest?.segments[segId]?.preview_artifact_id) {
      const seg = project.segments.find((s) => s.id === segId);
      if (
        seg &&
        lastRenderedPreviewSignatures[segId] ===
          computeClientPreviewSignature(seg, project)
      ) {
        setPreviewModalOpen(true);
        usageLogger.logActionOutput(
          "Open cached segment preview",
          "preview.cached",
          { seg_id: segId, artifact_id: manifest.segments[segId]?.preview_artifact_id },
          { projectId: project.project_id, segId }
        );
        return;
      }
    }
    const saved = await ensureSavedBeforeJob();
    if (!saved) return;
    try {
      const res = await apiClient.renderPreview(saved.project_id, segId);
      const job = await apiClient.getJob(res.job_id);
      setActiveJob(job);
      usageLogger.logActionOutput(
        "Queue segment preview job",
        "preview.queued",
        { job_id: job.job_id, status: job.status },
        { projectId: saved.project_id, segId, jobId: job.job_id }
      );
    } catch (err) {
      setBannerError(
        err instanceof Error ? err.message : "Failed to start preview"
      );
      usageLogger.logError("Render segment preview", "preview.start_failed", err, {
        projectId: saved.project_id,
        segId,
      });
    }
  };

  const handleExport = async () => {
    usageLogger.logIntention("Export final vertical MP4", "export.render");
    const saved = await ensureSavedBeforeJob();
    if (!saved) return;
    try {
      const res = await apiClient.exportProject(saved.project_id);
      const job = await apiClient.getJob(res.job_id);
      setActiveJob(job);
      usageLogger.logActionOutput(
        "Queue final export job",
        "export.queued",
        { job_id: job.job_id, status: job.status },
        { projectId: saved.project_id, jobId: job.job_id }
      );
    } catch (err) {
      setBannerError(
        err instanceof Error ? err.message : "Failed to start export"
      );
      usageLogger.logError("Export final vertical MP4", "export.start_failed", err, {
        projectId: saved.project_id,
      });
    }
  };

  const handleCancelJob = async (jobId: string) => {
    usageLogger.logIntention("Cancel active background job", "job.cancel", {
      jobId,
    });
    try {
      const job = await apiClient.cancelJob(jobId);
      setActiveJob(job);
      usageLogger.logActionOutput(
        "Cancel background job",
        "job.cancel",
        { job_id: job.job_id, status: job.status },
        { jobId: job.job_id }
      );
    } catch (err) {
      setBannerError(
        err instanceof Error ? err.message : "Failed to cancel job"
      );
      usageLogger.logError("Cancel background job", "job.cancel_failed", err, {
        jobId,
      });
    }
  };

  // Output resolution / aspect ratio change (re-clamps existing keyframes)
  const handleChangeOutputResolution = (rawW: number, rawH: number) => {
    if (!project) return;
    const nextW = Math.max(2, Math.round(rawW / 2) * 2);
    const nextH = Math.max(2, Math.round(rawH / 2) * 2);
    if (!Number.isFinite(nextW) || !Number.isFinite(nextH)) return;
    const srcW = manifest?.source_metadata?.width ?? 1920;
    const srcH = manifest?.source_metadata?.height ?? 1080;

    handleUpdateProject((prev) => ({
      ...prev,
      output_settings: {
        ...prev.output_settings,
        width: nextW,
        height: nextH,
      },
      segments: prev.segments.map((seg) => ({
        ...seg,
        crop_keyframes: seg.crop_keyframes.map((kf) => {
          const clamped = clampCropPosition(
            kf.x,
            kf.y,
            kf.zoom,
            srcW,
            srcH,
            nextW,
            nextH
          );
          return {
            ...kf,
            x: Number(clamped.x.toFixed(4)),
            y: Number(clamped.y.toFixed(4)),
            zoom: Number(clamped.zoom.toFixed(3)),
          };
        }),
      })),
    }));
    usageLogger.logActionOutput(
      "Change output resolution",
      "project.resolution_change",
      { width: nextW, height: nextH },
      { projectId: project.project_id }
    );
  };

  // Segment CRUD
  const handleAddSegment = (videoIn: number, videoOut: number) => {
    if (!project) return;
    const existing = new Set(project.segments.map((s) => s.id));
    let n = project.segments.length + 1;
    while (existing.has(`seg_${String(n).padStart(2, "0")}`)) n += 1;
    const id = `seg_${String(n).padStart(2, "0")}`;
    const srcW = manifest?.source_metadata?.width ?? 1920;
    const srcH = manifest?.source_metadata?.height ?? 1080;
    const ideal = computeIdealCrop(
      srcW,
      srcH,
      1.0,
      project.output_settings.width,
      project.output_settings.height
    );
    const initialX = Number((ideal.maxNormX / 2).toFixed(4));
    const initialY = Number((ideal.maxNormY / 2).toFixed(4));
    const newSeg: SegmentInput = {
      id,
      video_in: videoIn,
      video_out: videoOut,
      duration_mode: "preserve_video",
      source_audio_volume: project.output_settings.default_source_volume,
      speed: 1.0,
      crop_keyframes: [{ t: 0.0, x: initialX, y: initialY, zoom: 1.0 }],
      clauses: [],
    };
    handleUpdateProject((prev) => ({
      ...prev,
      segments: [...prev.segments, newSeg],
    }));
    setSelectedSegId(id);
    setSourceTime(videoIn);
    usageLogger.logActionOutput(
      "Add segment to timeline",
      "segment.add",
      { seg_id: id, video_in: videoIn, video_out: videoOut },
      { projectId: project.project_id, segId: id }
    );
  };

  const handleDuplicateSegment = (index: number) => {
    if (!project) return;
    const orig = project.segments[index];
    const existing = new Set(project.segments.map((s) => s.id));
    let n = project.segments.length + 1;
    while (existing.has(`seg_${String(n).padStart(2, "0")}`)) n += 1;
    const newId = `seg_${String(n).padStart(2, "0")}`;
    const copy: SegmentInput = {
      ...orig,
      id: newId,
      crop_keyframes: orig.crop_keyframes.map((k) => ({ ...k })),
      clauses: orig.clauses.map((c) => ({ ...c })),
    };
    handleUpdateProject((prev) => {
      const next = [...prev.segments];
      next.splice(index + 1, 0, copy);
      return { ...prev, segments: next };
    });
    setSelectedSegId(newId);
    usageLogger.logActionOutput(
      "Duplicate segment",
      "segment.duplicate",
      { source_seg_id: orig.id, new_seg_id: newId },
      { projectId: project.project_id, segId: newId }
    );
  };

  const handleDeleteSegment = (index: number) => {
    if (!project) return;
    const removed = project.segments[index];
    const next = project.segments.filter((_, i) => i !== index);
    handleUpdateProject((prev) => ({ ...prev, segments: next }));
    if (selectedSegId === removed.id) {
      setSelectedSegId(next[Math.max(0, index - 1)]?.id ?? null);
    }
    usageLogger.logActionOutput(
      "Delete segment",
      "segment.delete",
      { deleted_seg_id: removed.id, remaining_count: next.length },
      { projectId: project.project_id, segId: removed.id }
    );
  };

  // New Project Media Upload
  const handleUploadMedia = async (file: File) => {
    usageLogger.logIntention("Upload gameplay media", "media.upload", {
      input: { name: file.name, size: file.size, type: file.type },
    });
    setMediaSelectBusy(true);
    setNewProjectError(null);
    setSelectedMedia(null);
    try {
      const res = await apiClient.selectMedia({
        file,
        copyToProject,
      });
      setSelectedMedia(res);
      if (!newTitle.trim()) {
        const base =
          file.name.replace(/\.[^.]+$/, "") ||
          res.path.split(/[\\/]/).pop()?.replace(/\.[^.]+$/, "") ||
          "New Sims Project";
        setNewTitle(base);
      }
      usageLogger.logActionOutput(
        "Upload and validate gameplay media",
        "media.upload",
        {
          media_id: res.media_id,
          width: res.metadata.width,
          height: res.metadata.height,
          fps: res.metadata.fps,
          duration: res.metadata.duration,
        }
      );
    } catch (err) {
      setNewProjectError(
        err instanceof Error ? err.message : "Media upload failed"
      );
      usageLogger.logError("Upload gameplay media", "media.upload_failed", err, {
        input: { name: file.name, size: file.size },
      });
    } finally {
      setMediaSelectBusy(false);
    }
  };

  const handleCreateProject = async () => {
    if (!selectedMedia) return;
    const title = newTitle.trim() || "Untitled Sims Short";
    usageLogger.logIntention("Create new project", "project.create", {
      input: { title, media_id: selectedMedia.media_id, copy_to_project: copyToProject },
    });
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
      usageLogger.logActionOutput(
        "Create new project",
        "project.create",
        { project_id: pid, title, job_id: res.job_id },
        { projectId: pid ?? null, jobId: res.job_id ?? null }
      );
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
      usageLogger.logError("Create new project", "project.create_failed", err, {
        input: { title },
      });
    } finally {
      setCreateBusy(false);
    }
  };

  const handleRelink = async (mode: "native" | "path" | "file") => {
    if (!project) return;
    usageLogger.logIntention("Relink missing source media", "source.relink", {
      projectId: project.project_id,
      input: { mode },
    });
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
      usageLogger.logActionOutput(
        "Relink source media",
        "source.relink",
        {
          status: res.source_status?.status,
          path: res.project.source_media.path,
          job_id: res.job_id,
        },
        { projectId: project.project_id, jobId: res.job_id ?? null }
      );
      setRelinkOpen(false);
      setRelinkPath("");
      setRelinkFile(null);
    } catch (err) {
      setBannerError(
        err instanceof Error ? err.message : "Source relink failed"
      );
      usageLogger.logError("Relink source media", "source.relink_failed", err, {
        projectId: project.project_id,
        input: { mode },
      });
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
  const sourceBroken =
    sourceStatus &&
    (sourceStatus.status === "missing" || sourceStatus.status === "modified");

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
                          project.output_settings.height
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
                          Number(e.target.value) || 2
                        )
                      }
                      className="w-14 rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 font-mono text-[11px] text-zinc-100"
                    />
                  </div>
                )}
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
                disabled={isJobBusy || project.segments.length === 0}
                onClick={handleExport}
                className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-3 py-1 text-xs font-semibold text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Download className="h-3.5 w-3.5" />
                Export {formatAspectRatio(project.output_settings.width, project.output_settings.height)} MP4
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

      {/* Main Three-Zone Studio Workspace */}
      {!project || !manifest ? (
        <main className="flex flex-1 flex-col items-center justify-center gap-4 p-6 text-center">
          <Film className="h-12 w-12 text-zinc-600" />
          <div className="space-y-1">
            <h1 className="text-lg font-semibold text-zinc-200">
              No Project Loaded
            </h1>
            <p className="max-w-md text-xs text-zinc-400">
              Import a gameplay recording of any aspect ratio to start slicing
              segments, animating crop keyframes, and synthesizing offline
              Kokoro voiceovers.
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
          {/* Upper Stage + Inspector */}
          <div className="flex min-h-0 flex-1 flex-col gap-3 lg:flex-row">
            <CropCanvas
              projectId={project.project_id}
              segment={selectedSegment}
              manifest={manifest}
              captionSettings={project.caption_settings}
              outputWidth={project.output_settings.width}
              outputHeight={project.output_settings.height}
              tailPaddingSec={project.output_settings.tail_padding_sec}
              sourceTime={sourceTime}
              isPlaying={isPlaying}
              onTogglePlay={() => {
                setIsProjectPlaying(false);
                setIsPlaying((p) => !p);
              }}
              onSourceTimeChange={(t) => setSourceTime(t)}
              onUpdateSegment={handleUpdateSegment}
              onRenderPreview={handleRenderPreview}
              previewBusy={
                Boolean(isJobBusy && activeJob?.job_type === "preview")
              }
              previewModalOpen={previewModalOpen}
              onSetPreviewModalOpen={setPreviewModalOpen}
            />

            <ContextualInspector
              project={project}
              manifest={manifest}
              selectedSegment={selectedSegment}
              selection={
                selection ??
                (selectedSegment
                  ? { type: "video", segId: selectedSegment.id }
                  : null)
              }
              voices={capabilities?.voices ?? []}
              ttsError={ttsError}
              lastSynthesizedSignatures={lastSynthesizedSignatures}
              onUpdateProject={handleUpdateProject}
              onUpdateSegment={handleUpdateSegment}
              onDuplicateSegment={(segId) => {
                const idx = project.segments.findIndex((s) => s.id === segId);
                if (idx >= 0) handleDuplicateSegment(idx);
              }}
              onDeleteSegment={(segId) => {
                const idx = project.segments.findIndex((s) => s.id === segId);
                if (idx >= 0) handleDeleteSegment(idx);
              }}
              onSelectItem={setSelection}
            />
          </div>

          {/* Lower Unified Multi-Track Timeline */}
          <Timeline
            segments={project.segments}
            manifest={manifest}
            selectedSegId={selectedSegId}
            selection={
              selection ??
              (selectedSegment
                ? { type: "video", segId: selectedSegment.id }
                : null)
            }
            tailPaddingSec={project.output_settings.tail_padding_sec}
            sourceTime={sourceTime}
            isModalOpen={isModalOpen}
            isProjectPlaying={isProjectPlaying}
            onSelectSegment={(segId) => {
              setSelectedSegId(segId);
              setSelection({ type: "video", segId });
              const seg = project.segments.find((s) => s.id === segId);
              if (seg) setSourceTime(seg.video_in);
            }}
            onSelectItem={setSelection}
            onSourceTimeChange={(t) => setSourceTime(t)}
            onAddSegment={handleAddSegment}
            onUpdateSegment={handleUpdateSegment}
            onToggleProjectPlayback={() => {
              if (isProjectPlaying) {
                setIsProjectPlaying(false);
                setIsPlaying(false);
              } else {
                setIsProjectPlaying(true);
              }
            }}
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
                <button
                  type="button"
                  disabled={mediaSelectBusy}
                  onClick={() => newProjectFileInputRef.current?.click()}
                  className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-3 py-1.5 font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
                >
                  {mediaSelectBusy ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Upload className="h-3.5 w-3.5" />
                  )}
                  {mediaSelectBusy ? "Uploading..." : "Upload File"}
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
                Export Ready ({project.output_settings.width}×{project.output_settings.height} MP4)
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
