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
  Upload,
  X,
} from "lucide-react";
import { ApiError, apiClient } from "./api/client";
import { CropCanvas } from "./components/CropCanvas";
import { JobStatusBar } from "./components/JobStatusBar";
import { ScriptPanel } from "./components/ScriptPanel";
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
} from "./types/studio";
import {
  computeClientTtsSignature,
  computeDurationPlan,
} from "./utils/studioMath";

export default function App() {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [project, setProject] = useState<ProjectInput | null>(null);
  const [manifest, setManifest] = useState<ArtifactManifest | null>(null);
  const [sourceStatus, setSourceStatus] = useState<SourceStatus | null>(null);
  const [selectedSegId, setSelectedSegId] = useState<string | null>(null);

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

  // Modals
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [manualMediaPath, setManualMediaPath] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [copyToProject, setCopyToProject] = useState(false);
  const [selectedMedia, setSelectedMedia] = useState<MediaSelectResult | null>(
    null
  );
  const [mediaSelectBusy, setMediaSelectBusy] = useState(false);
  const [createBusy, setCreateBusy] = useState(false);

  const [relinkOpen, setRelinkOpen] = useState(false);
  const [relinkPath, setRelinkPath] = useState("");
  const [relinkFile, setRelinkFile] = useState<File | null>(null);
  const [relinkBusy, setRelinkBusy] = useState(false);

  const [previewModalOpen, setPreviewModalOpen] = useState(false);
  const [exportModalOpen, setExportModalOpen] = useState(false);

  const projectRef = useRef<ProjectInput | null>(null);
  projectRef.current = project;

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
    }
    setLastSynthesizedSignatures((prev) => ({ ...sigs, ...prev }));
  }, []);

  const loadProject = useCallback(
    async (projectId: string) => {
      try {
        setBannerError(null);
        const bundle = await apiClient.getProject(projectId);
        applyBundle(bundle);
        const url = new URL(window.location.href);
        url.searchParams.set("project", projectId);
        window.history.replaceState({}, "", url.toString());
      } catch (err) {
        setBannerError(
          err instanceof Error ? err.message : "Failed to load project"
        );
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

            if (updated.job_type === "tts" && updated.seg_id) {
              setTtsError(null);
              const seg = projectRef.current?.segments.find(
                (s) => s.id === updated.seg_id
              );
              if (seg && projectRef.current) {
                const sig = computeClientTtsSignature(
                  seg,
                  projectRef.current.pronunciations,
                  projectRef.current.voice,
                  projectRef.current.language,
                  projectRef.current.output_settings.inter_clause_pause_ms
                );
                setLastSynthesizedSignatures((prev) => ({
                  ...prev,
                  [seg.id]: sig,
                }));
              }
            } else if (updated.job_type === "preview") {
              setPreviewModalOpen(true);
            } else if (updated.job_type === "export") {
              setExportModalOpen(true);
            }
          } else if (updated.job_type === "tts" && updated.error) {
            setTtsError(updated.error);
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

  const handleGenerateVoiceover = async (segId: string) => {
    const saved = await ensureSavedBeforeJob();
    if (!saved) return;
    setTtsError(null);
    try {
      const res = await apiClient.synthesizeTts(saved.project_id, segId);
      const job = await apiClient.getJob(res.job_id);
      setActiveJob(job);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to start TTS";
      setTtsError(msg);
      setBannerError(msg);
    }
  };

  const handleRenderPreview = async (segId: string) => {
    const saved = await ensureSavedBeforeJob();
    if (!saved) return;
    try {
      const res = await apiClient.renderPreview(saved.project_id, segId);
      const job = await apiClient.getJob(res.job_id);
      setActiveJob(job);
    } catch (err) {
      setBannerError(
        err instanceof Error ? err.message : "Failed to start preview"
      );
    }
  };

  const handleExport = async () => {
    const saved = await ensureSavedBeforeJob();
    if (!saved) return;
    try {
      const res = await apiClient.exportProject(saved.project_id);
      const job = await apiClient.getJob(res.job_id);
      setActiveJob(job);
    } catch (err) {
      setBannerError(
        err instanceof Error ? err.message : "Failed to start export"
      );
    }
  };

  const handleCancelJob = async (jobId: string) => {
    try {
      const job = await apiClient.cancelJob(jobId);
      setActiveJob(job);
    } catch (err) {
      setBannerError(
        err instanceof Error ? err.message : "Failed to cancel job"
      );
    }
  };

  // Segment CRUD
  const handleAddSegment = (videoIn: number, videoOut: number) => {
    if (!project) return;
    const existing = new Set(project.segments.map((s) => s.id));
    let n = project.segments.length + 1;
    while (existing.has(`seg_${String(n).padStart(2, "0")}`)) n += 1;
    const id = `seg_${String(n).padStart(2, "0")}`;
    const newSeg: SegmentInput = {
      id,
      video_in: videoIn,
      video_out: videoOut,
      duration_mode: "preserve_video",
      source_audio_volume: project.output_settings.default_source_volume,
      speed: 1.0,
      crop_keyframes: [{ t: 0.0, x: 0.28, y: 0.0, zoom: 1.0 }],
      clauses: [],
    };
    handleUpdateProject((prev) => ({
      ...prev,
      segments: [...prev.segments, newSeg],
    }));
    setSelectedSegId(id);
    setSourceTime(videoIn);
  };

  const handleReorderSegment = (index: number, dir: -1 | 1) => {
    if (!project) return;
    const target = index + dir;
    if (target < 0 || target >= project.segments.length) return;
    handleUpdateProject((prev) => {
      const next = [...prev.segments];
      [next[index], next[target]] = [next[target], next[index]];
      return { ...prev, segments: next };
    });
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
  };

  const handleDeleteSegment = (index: number) => {
    if (!project) return;
    const removed = project.segments[index];
    const next = project.segments.filter((_, i) => i !== index);
    handleUpdateProject((prev) => ({ ...prev, segments: next }));
    if (selectedSegId === removed.id) {
      setSelectedSegId(next[Math.max(0, index - 1)]?.id ?? null);
    }
  };

  // New Project Media Selection
  const handleSelectMedia = async (mode: "native" | "path" | "file") => {
    setMediaSelectBusy(true);
    setBannerError(null);
    try {
      const res = await apiClient.selectMedia({
        path: mode === "path" ? manualMediaPath.trim() : undefined,
        file: mode === "file" ? selectedFile ?? undefined : undefined,
        copyToProject,
      });
      setSelectedMedia(res);
      if (!newTitle.trim()) {
        const base = res.path.split(/[\\/]/).pop()?.replace(/\.[^.]+$/, "") ?? "New Sims Project";
        setNewTitle(base);
      }
    } catch (err) {
      setBannerError(
        err instanceof Error ? err.message : "Media selection failed"
      );
    } finally {
      setMediaSelectBusy(false);
    }
  };

  const handleCreateProject = async () => {
    if (!selectedMedia) return;
    setCreateBusy(true);
    setBannerError(null);
    try {
      const res = await apiClient.createProject({
        media_id: selectedMedia.media_id,
        title: newTitle.trim() || "Untitled Sims Short",
        copy_to_project: copyToProject,
      });
      const pid = res.project_id || res.project?.project_id;
      if (res.job_id) {
        const job = await apiClient.getJob(res.job_id);
        setActiveJob(job);
      }
      setNewProjectOpen(false);
      setSelectedMedia(null);
      setNewTitle("");
      setManualMediaPath("");
      setSelectedFile(null);
      await refreshProjectList();
      if (pid) {
        await loadProject(pid);
      }
    } catch (err) {
      setBannerError(
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
      setBannerError(
        err instanceof Error ? err.message : "Source relink failed"
      );
    } finally {
      setRelinkBusy(false);
    }
  };

  const isJobBusy =
    activeJob?.status === "queued" || activeJob?.status === "running";
  const isModalOpen =
    newProjectOpen || relinkOpen || previewModalOpen || exportModalOpen;
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
        </div>

        <div className="flex items-center gap-2">
          {project && (
            <>
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
                Export 9:16 MP4
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
              Import a 16:9 Sims 4 gameplay recording to start slicing vertical
              segments, animating 9:16 crop keyframes, and synthesizing offline
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

            <ScriptPanel
              project={project}
              manifest={manifest}
              selectedSegment={selectedSegment}
              voices={capabilities?.voices ?? []}
              ttsBusy={Boolean(isJobBusy && activeJob?.job_type === "tts")}
              ttsError={ttsError}
              lastSynthesizedSignatures={lastSynthesizedSignatures}
              onUpdateProject={handleUpdateProject}
              onUpdateSegment={handleUpdateSegment}
              onGenerateVoiceover={handleGenerateVoiceover}
            />
          </div>

          {/* Lower Storyboard Timeline */}
          <Timeline
            segments={project.segments}
            manifest={manifest}
            selectedSegId={selectedSegId}
            tailPaddingSec={project.output_settings.tail_padding_sec}
            sourceTime={sourceTime}
            isModalOpen={isModalOpen}
            isProjectPlaying={isProjectPlaying}
            onSelectSegment={(segId) => {
              setSelectedSegId(segId);
              const seg = project.segments.find((s) => s.id === segId);
              if (seg) setSourceTime(seg.video_in);
            }}
            onSourceTimeChange={(t) => setSourceTime(t)}
            onAddSegment={handleAddSegment}
            onUpdateSegment={handleUpdateSegment}
            onReorderSegment={handleReorderSegment}
            onDuplicateSegment={handleDuplicateSegment}
            onDeleteSegment={handleDeleteSegment}
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
                Create New Vertical Project
              </h2>
              <button
                type="button"
                onClick={() => setNewProjectOpen(false)}
                className="rounded p-1 text-zinc-400 hover:bg-zinc-800"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="space-y-3 text-xs">
              <div className="space-y-1.5">
                <span className="font-medium text-zinc-300">
                  1. Select 16:9 Gameplay Recording
                </span>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    disabled={mediaSelectBusy}
                    onClick={() => handleSelectMedia("native")}
                    className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-3 py-1.5 font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
                  >
                    {mediaSelectBusy ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <FolderPlus className="h-3.5 w-3.5" />
                    )}
                    Open Native File Picker
                  </button>
                </div>

                <div className="flex gap-2 pt-1">
                  <input
                    type="text"
                    value={manualMediaPath}
                    onChange={(e) => setManualMediaPath(e.target.value)}
                    placeholder="Or enter local video path..."
                    className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-zinc-100 focus:border-emerald-500 focus:outline-none"
                  />
                  <button
                    type="button"
                    disabled={!manualMediaPath.trim() || mediaSelectBusy}
                    onClick={() => handleSelectMedia("path")}
                    className="rounded border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
                  >
                    Use Path
                  </button>
                </div>

                <div className="flex items-center gap-2 pt-1">
                  <input
                    type="file"
                    accept="video/mp4,video/*"
                    onChange={(e) =>
                      setSelectedFile(e.target.files?.[0] ?? null)
                    }
                    className="flex-1 text-zinc-400 file:mr-2 file:rounded file:border-0 file:bg-zinc-800 file:px-2.5 file:py-1 file:text-xs file:text-zinc-200"
                  />
                  <button
                    type="button"
                    disabled={!selectedFile || mediaSelectBusy}
                    onClick={() => handleSelectMedia("file")}
                    className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
                  >
                    <Upload className="h-3.5 w-3.5" />
                    Upload
                  </button>
                </div>
              </div>

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
                onClick={() => setNewProjectOpen(false)}
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
                Export Ready (1080×1920 MP4)
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
    </div>
  );
}
