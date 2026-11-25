import { useEffect, useState } from "react";
import { Download, FileText, Film, X } from "lucide-react";
import { apiClient } from "../api/client";
import type { ExportOptions, ExportQuality, ProjectInput } from "../types/studio";
import { formatAspectRatio } from "../utils/studioMath";

interface ExportConfigModalProps {
  open: boolean;
  project: ProjectInput;
  durationSec: number;
  onClose: () => void;
  onStartExport: (options: ExportOptions) => void;
}

export function ExportConfigModal({
  open,
  project,
  durationSec,
  onClose,
  onStartExport,
}: ExportConfigModalProps) {
  const defaultFilename = `${project.title
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/^[._]+|[._]+$/g, "") || project.project_id}.mp4`;

  const [filename, setFilename] = useState(defaultFilename);
  const [quality, setQuality] = useState<ExportQuality>("standard");
  const [fps, setFps] = useState<number>(project.output_settings.fps || 30);

  useEffect(() => {
    if (open) {
      setFilename(defaultFilename);
      setFps(project.output_settings.fps || 30);
    }
  }, [open, defaultFilename, project.output_settings.fps]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const estMbps = quality === "high" ? 12 : quality === "standard" ? 7 : 3.5;
  const estMb = Math.max(0.2, (durationSec * estMbps) / 8);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Export Configuration"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-xl border border-zinc-700 bg-[#151821] p-5 shadow-2xl space-y-4 text-xs"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 pb-2.5">
          <div className="flex items-center gap-2">
            <Film className="h-4 w-4 text-emerald-400" />
            <h2 className="text-sm font-semibold text-zinc-100">
              Export Settings
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-3">
          <label className="block space-y-1">
            <span className="text-zinc-400">Output Filename</span>
            <input
              type="text"
              value={filename}
              onChange={(e) => setFilename(e.target.value)}
              className="w-full rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-zinc-100 focus:border-emerald-500 focus:outline-none"
            />
          </label>

          <div className="grid grid-cols-2 gap-2.5">
            <label className="flex flex-col gap-1">
              <span className="text-zinc-400">Quality Preset</span>
              <select
                value={quality}
                onChange={(e) => setQuality(e.target.value as ExportQuality)}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-zinc-100"
              >
                <option value="draft">Draft (Fast, CRF 24)</option>
                <option value="standard">Standard (CRF 19)</option>
                <option value="high">High (Crisp, CRF 16)</option>
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-zinc-400">Frame Rate</span>
              <select
                value={fps}
                onChange={(e) => setFps(Number(e.target.value))}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1.5 font-mono text-zinc-100"
              >
                <option value={24}>24 fps</option>
                <option value={30}>30 fps (Default)</option>
                <option value={60}>60 fps (Smooth)</option>
              </select>
            </label>
          </div>

          <div className="rounded-lg border border-zinc-800 bg-[#11141b] p-3 font-mono text-[11px] text-zinc-400 space-y-1">
            <div className="flex justify-between">
              <span>Resolution</span>
              <span className="text-zinc-200">
                {project.output_settings.width}×{project.output_settings.height} (
                {formatAspectRatio(
                  project.output_settings.width,
                  project.output_settings.height
                )}
                )
              </span>
            </div>
            <div className="flex justify-between">
              <span>Duration</span>
              <span className="text-zinc-200">{durationSec.toFixed(2)}s</span>
            </div>
            <div className="flex justify-between">
              <span>Est. File Size</span>
              <span className="text-emerald-300">~{estMb.toFixed(1)} MB</span>
            </div>
          </div>

          <a
            href={apiClient.subtitlesSrtUrl(project.project_id)}
            download={`${project.title.trim() || project.project_id}.srt`}
            className="inline-flex w-full items-center justify-center gap-1.5 rounded border border-zinc-700 bg-zinc-800/80 px-3 py-1.5 font-medium text-zinc-200 hover:bg-zinc-700"
          >
            <FileText className="h-3.5 w-3.5 text-sky-400" />
            Download Subtitles (.srt)
          </a>
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-zinc-700 px-3 py-1.5 text-zinc-300 hover:bg-zinc-800"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => {
              onStartExport({
                filename: filename.trim() || defaultFilename,
                quality,
                fps,
              });
              onClose();
            }}
            className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-4 py-1.5 font-semibold text-white hover:bg-emerald-500"
          >
            <Download className="h-3.5 w-3.5" />
            Start Export
          </button>
        </div>
      </div>
    </div>
  );
}
