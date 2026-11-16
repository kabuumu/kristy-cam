import { AlertCircle, CheckCircle2, Loader2, X, XCircle } from "lucide-react";
import type { JobState } from "../types/studio";

interface JobStatusBarProps {
  activeJob: JobState | null;
  onCancel: (jobId: string) => void;
  onDismiss: () => void;
}

export function JobStatusBar({
  activeJob,
  onCancel,
  onDismiss,
}: JobStatusBarProps) {
  if (!activeJob) {
    return (
      <footer className="flex h-10 shrink-0 items-center justify-between border-t border-zinc-800 bg-[#13161c] px-4 text-xs text-zinc-400">
        <div className="flex items-center gap-2">
          <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" />
          <span>Studio Ready — Local CPU Engine</span>
        </div>
        <span className="text-zinc-500">1080×1920 • 30fps CFR • 48kHz AAC</span>
      </footer>
    );
  }

  const isRunning =
    activeJob.status === "queued" || activeJob.status === "running";
  const isSucceeded = activeJob.status === "succeeded";
  const isError =
    activeJob.status === "failed" || activeJob.status === "interrupted";
  const isCancelled = activeJob.status === "cancelled";

  const jobLabelMap: Record<JobState["job_type"], string> = {
    proxy: "Proxy Transcode",
    tts: "Voiceover Synthesis",
    preview: "Segment Preview",
    export: "Final Export",
  };

  return (
    <footer
      className="flex min-h-10 shrink-0 flex-wrap items-center justify-between gap-3 border-t border-zinc-800 bg-[#13161c] px-4 py-1.5 text-xs"
      role="status"
      aria-live="polite"
    >
      <div className="flex min-w-0 flex-1 items-center gap-3">
        {isRunning && (
          <span className="inline-flex items-center gap-1.5 rounded bg-emerald-500/15 px-2 py-0.5 font-medium text-emerald-300">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            <span>{activeJob.status.toUpperCase()}</span>
          </span>
        )}
        {isSucceeded && (
          <span className="inline-flex items-center gap-1.5 rounded bg-emerald-500/15 px-2 py-0.5 font-medium text-emerald-300">
            <CheckCircle2 className="h-3.5 w-3.5" />
            <span>SUCCEEDED</span>
          </span>
        )}
        {isError && (
          <span className="inline-flex items-center gap-1.5 rounded bg-rose-500/20 px-2 py-0.5 font-medium text-rose-300">
            <AlertCircle className="h-3.5 w-3.5" />
            <span>{activeJob.status.toUpperCase()}</span>
          </span>
        )}
        {isCancelled && (
          <span className="inline-flex items-center gap-1.5 rounded bg-amber-500/20 px-2 py-0.5 font-medium text-amber-300">
            <XCircle className="h-3.5 w-3.5" />
            <span>CANCELLED</span>
          </span>
        )}

        <span className="font-medium text-zinc-200">
          {jobLabelMap[activeJob.job_type] ?? activeJob.job_type}
        </span>

        {activeJob.stage && (
          <span className="truncate text-zinc-400">{activeJob.stage}</span>
        )}

        <div className="flex w-44 items-center gap-2">
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-800">
            <div
              className={`h-full transition-all duration-200 ${
                isError
                  ? "bg-rose-500"
                  : isCancelled
                    ? "bg-amber-500"
                    : "bg-emerald-500"
              }`}
              style={{ width: `${Math.min(100, Math.max(0, activeJob.progress))}%` }}
            />
          </div>
          <span className="w-9 text-right font-mono text-zinc-300">
            {activeJob.progress}%
          </span>
        </div>

        {activeJob.error && (
          <span className="truncate font-medium text-rose-300" title={activeJob.error}>
            {activeJob.error}
          </span>
        )}
      </div>

      <div className="flex items-center gap-2">
        {isRunning ? (
          <button
            type="button"
            onClick={() => onCancel(activeJob.job_id)}
            className="inline-flex items-center gap-1 rounded border border-rose-500/40 bg-rose-500/15 px-2.5 py-1 text-xs font-medium text-rose-200 hover:bg-rose-500/25"
          >
            <X className="h-3.5 w-3.5" />
            Cancel
          </button>
        ) : (
          <button
            type="button"
            onClick={onDismiss}
            className="rounded px-2 py-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
          >
            Dismiss
          </button>
        )}
      </div>
    </footer>
  );
}
