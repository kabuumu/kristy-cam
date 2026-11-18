import { useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Film,
  FolderOpen,
  Image as ImageIcon,
  Loader2,
  Music,
  Plus,
  Sparkles,
  Trash2,
  Upload,
} from "lucide-react";
import type {
  ArtifactManifest,
  MediaAssetKind,
  MediaAssetMetadata,
  MediaAssetRef,
  ProjectInput,
} from "../types/studio";

export const MEDIA_BIN_DND_MIME = "application/x-studio-asset";

interface MediaBinProps {
  project: ProjectInput;
  manifest: ArtifactManifest;
  nativePickerSupported?: boolean;
  importBusy?: boolean;
  importError?: string | null;
  previewAssetId: string | null;
  onImportAsset: (options: {
    path?: string;
    file?: File;
    kind?: MediaAssetKind;
    copyToProject?: boolean;
  }) => Promise<void>;
  onDeleteAsset: (assetId: string) => Promise<void>;
  onAddAssetAtPlayhead: (asset: MediaAssetRef) => void;
  onPreviewVideoAsset: (assetId: string) => void;
}

function formatMetaSummary(
  asset: MediaAssetRef,
  meta: MediaAssetMetadata | undefined
): string {
  if (!meta) return asset.kind.toUpperCase();
  if (asset.kind === "image") {
    const dims = meta.width && meta.height ? `${meta.width}×${meta.height}` : "Image";
    return meta.has_alpha ? `${dims} • RGBA` : dims;
  }
  if (asset.kind === "audio") {
    const ch = meta.audio_channels ? `${meta.audio_channels}ch` : "Audio";
    return `${meta.duration.toFixed(1)}s • ${ch}`;
  }
  const dims = meta.width && meta.height ? `${meta.width}×${meta.height}` : "Video";
  return `${dims} • ${meta.duration.toFixed(1)}s`;
}

export function MediaBin({
  project,
  manifest,
  nativePickerSupported = false,
  importBusy = false,
  importError = null,
  previewAssetId,
  onImportAsset,
  onDeleteAsset,
  onAddAssetAtPlayhead,
  onPreviewVideoAsset,
}: MediaBinProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [filter, setFilter] = useState<"all" | MediaAssetKind>("all");
  const [showPathForm, setShowPathForm] = useState(false);
  const [pathInput, setPathInput] = useState("");
  const [copyToProject, setCopyToProject] = useState(true);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const referencedIds = useMemo(() => {
    const ids = new Set<string>([project.source_media.media_id]);
    for (const tr of project.tracks ?? []) {
      if (tr.kind === "video" || tr.kind === "audio") {
        for (const clip of tr.clips) {
          ids.add(clip.asset_id);
        }
      }
    }
    return ids;
  }, [project]);

  const assets = useMemo(() => {
    const list = Object.values(project.media_assets ?? {});
    const primaryId = project.source_media.media_id;
    list.sort((a, b) => {
      if (a.asset_id === primaryId) return -1;
      if (b.asset_id === primaryId) return 1;
      return a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name);
    });
    return filter === "all" ? list : list.filter((a) => a.kind === filter);
  }, [project, filter]);

  const getAssetMeta = (asset: MediaAssetRef): MediaAssetMetadata | undefined => {
    const direct = manifest.media_metadata?.[asset.asset_id];
    if (direct) return direct;
    if (asset.asset_id === project.source_media.media_id && manifest.source_metadata) {
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

  const getVideoProxyStatus = (asset: MediaAssetRef) => {
    if (asset.kind !== "video") return null;
    if (asset.asset_id === project.source_media.media_id) {
      return manifest.proxy.status;
    }
    return manifest.media_proxies?.[asset.asset_id]?.status ?? null;
  };

  if (collapsed) {
    return (
      <aside className="flex shrink-0 flex-row lg:flex-col items-center justify-between lg:justify-start gap-2 rounded-xl border border-zinc-800 bg-[#151821] px-2 py-2 lg:w-11">
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          title="Expand Media Bin"
          aria-label="Expand Media Bin"
          className="rounded border border-zinc-700 bg-zinc-800 p-1.5 text-zinc-200 hover:bg-zinc-700"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
        <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400 lg:[writing-mode:vertical-rl]">
          Media Bin ({Object.keys(project.media_assets ?? {}).length})
        </span>
      </aside>
    );
  }

  return (
    <aside
      data-testid="media-bin"
      className="flex w-full lg:w-[245px] xl:w-[270px] shrink-0 flex-col overflow-hidden rounded-xl border border-zinc-800 bg-[#151821]"
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <div className="flex items-center gap-1.5">
          <FolderOpen className="h-4 w-4 text-emerald-400" />
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-200">
            Media Bin
          </h2>
          <span className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
            {Object.keys(project.media_assets ?? {}).length}
          </span>
        </div>

        <div className="flex items-center gap-1">
          <input
            ref={fileInputRef}
            type="file"
            accept="video/*,image/*,audio/*"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) {
                void onImportAsset({ file, copyToProject: true });
              }
            }}
          />
          <button
            type="button"
            disabled={importBusy}
            onClick={() => {
              if (nativePickerSupported) {
                void onImportAsset({ copyToProject });
              } else {
                fileInputRef.current?.click();
              }
            }}
            title="Import video, PNG/WebP overlay, or WAV/MP3 audio"
            className="inline-flex items-center gap-1 rounded bg-emerald-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {importBusy ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Upload className="h-3 w-3" />
            )}
            Import
          </button>
          <button
            type="button"
            onClick={() => setShowPathForm((v) => !v)}
            title="Import asset by file path"
            className="rounded border border-zinc-700 bg-zinc-800 px-1.5 py-1 text-[10px] text-zinc-300 hover:bg-zinc-700"
          >
            Path
          </button>
          <button
            type="button"
            onClick={() => setCollapsed(true)}
            title="Collapse Media Bin"
            aria-label="Collapse Media Bin"
            className="hidden lg:inline-flex rounded p-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Optional Path Import Form */}
      {showPathForm && (
        <div className="border-b border-zinc-800 bg-[#11141b] p-2 space-y-1.5 text-[11px]">
          <div className="flex gap-1">
            <input
              type="text"
              value={pathInput}
              onChange={(e) => setPathInput(e.target.value)}
              placeholder="/path/to/media.{mp4,png,wav}"
              className="min-w-0 flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100 focus:border-emerald-500 focus:outline-none"
            />
            <button
              type="button"
              disabled={!pathInput.trim() || importBusy}
              onClick={async () => {
                const trimmed = pathInput.trim();
                if (!trimmed) return;
                await onImportAsset({ path: trimmed, copyToProject });
                setPathInput("");
              }}
              className="rounded bg-emerald-600 px-2 py-1 font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
            >
              Add
            </button>
          </div>
          <label className="flex items-center gap-1.5 text-[10px] text-zinc-400">
            <input
              type="checkbox"
              checked={copyToProject}
              onChange={(e) => setCopyToProject(e.target.checked)}
            />
            <span>Copy into project folder</span>
          </label>
        </div>
      )}

      {importError && (
        <div className="border-b border-rose-500/40 bg-rose-500/15 px-2.5 py-1.5 text-[11px] text-rose-200">
          {importError}
        </div>
      )}

      {/* Kind Filter Tabs */}
      <div className="flex gap-1 border-b border-zinc-800 px-2 py-1.5 text-[10px]">
        {(["all", "video", "image", "audio"] as const).map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => setFilter(tab)}
            className={`rounded px-2 py-0.5 font-medium capitalize transition ${
              filter === tab
                ? "bg-emerald-500/20 text-emerald-200 border border-emerald-500/40"
                : "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Asset List */}
      <div className="flex-1 space-y-1.5 overflow-y-auto p-2">
        {assets.length === 0 ? (
          <div className="rounded-lg border border-dashed border-zinc-800 p-4 text-center text-[11px] text-zinc-500">
            No {filter === "all" ? "media assets" : `${filter} assets`} yet. Import videos, RGBA overlays, or audio tracks.
          </div>
        ) : (
          assets.map((asset) => {
            const meta = getAssetMeta(asset);
            const proxyStatus = getVideoProxyStatus(asset);
            const isPrimary = asset.asset_id === project.source_media.media_id;
            const isInUse = referencedIds.has(asset.asset_id);
            const isPreviewed = previewAssetId === asset.asset_id;
            const KindIcon =
              asset.kind === "image"
                ? ImageIcon
                : asset.kind === "audio"
                  ? Music
                  : Film;
            const badgeColor =
              asset.kind === "image"
                ? "text-purple-300 bg-purple-500/15 border-purple-500/30"
                : asset.kind === "audio"
                  ? "text-amber-300 bg-amber-500/15 border-amber-500/30"
                  : "text-emerald-300 bg-emerald-500/15 border-emerald-500/30";

            return (
              <div
                key={asset.asset_id}
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.setData(
                    MEDIA_BIN_DND_MIME,
                    JSON.stringify({
                      assetId: asset.asset_id,
                      kind: asset.kind,
                    })
                  );
                  e.dataTransfer.effectAllowed = "copy";
                }}
                title="Drag onto timeline track, or click + Add"
                className={`group rounded-lg border p-2 text-xs transition cursor-grab active:cursor-grabbing ${
                  isPreviewed
                    ? "border-sky-500/60 bg-sky-950/25"
                    : "border-zinc-800 bg-[#11141b] hover:border-zinc-700"
                }`}
              >
                <div className="flex items-start justify-between gap-1.5">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <KindIcon
                      className={`h-3.5 w-3.5 shrink-0 ${
                        asset.kind === "image"
                          ? "text-purple-400"
                          : asset.kind === "audio"
                            ? "text-amber-400"
                            : "text-emerald-400"
                      }`}
                    />
                    <span
                      className="truncate font-medium text-zinc-200"
                      title={asset.name}
                    >
                      {asset.name}
                    </span>
                  </div>
                  <span
                    className={`shrink-0 rounded border px-1 py-0.2 text-[9px] font-semibold uppercase ${badgeColor}`}
                  >
                    {isPrimary ? "MAIN" : asset.kind}
                  </span>
                </div>

                <div className="mt-1 flex flex-wrap items-center justify-between gap-1 text-[10px] font-mono text-zinc-400">
                  <span>{formatMetaSummary(asset, meta)}</span>
                  {proxyStatus && (
                    <span
                      className={
                        proxyStatus === "ready"
                          ? "text-emerald-400"
                          : proxyStatus === "failed"
                            ? "text-rose-400"
                            : "text-amber-300"
                      }
                    >
                      {proxyStatus === "ready"
                        ? "Proxy ✓"
                        : proxyStatus === "failed"
                          ? "Proxy ✕"
                          : "Proxy…"}
                    </span>
                  )}
                </div>

                <div className="mt-1.5 flex items-center justify-between gap-1">
                  <button
                    type="button"
                    onClick={() => onAddAssetAtPlayhead(asset)}
                    className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-emerald-500/40 bg-emerald-500/15 px-2 py-0.5 text-[10px] font-medium text-emerald-200 hover:bg-emerald-500/25"
                  >
                    <Plus className="h-3 w-3" />
                    Add to Timeline
                  </button>

                  {asset.kind === "video" && (
                    <button
                      type="button"
                      onClick={() => onPreviewVideoAsset(asset.asset_id)}
                      title="Load in Source Monitor for I/O trimming"
                      className="rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-300 hover:bg-zinc-700"
                    >
                      <Sparkles className="h-3 w-3" />
                    </button>
                  )}

                  {!isPrimary && (
                    <button
                      type="button"
                      disabled={isInUse || deletingId === asset.asset_id}
                      onClick={async () => {
                        setDeletingId(asset.asset_id);
                        try {
                          await onDeleteAsset(asset.asset_id);
                        } finally {
                          setDeletingId(null);
                        }
                      }}
                      title={
                        isInUse
                          ? "Remove clips using this asset before deleting"
                          : "Remove unused asset"
                      }
                      aria-label={`Delete asset ${asset.name}`}
                      className="rounded border border-zinc-700 bg-zinc-800 p-1 text-zinc-400 hover:border-rose-500/50 hover:text-rose-300 disabled:cursor-not-allowed disabled:opacity-35"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}
