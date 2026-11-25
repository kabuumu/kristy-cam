import { useEffect } from "react";
import { ArrowUpCircle, CheckCircle2, Download, ExternalLink, Loader2, RefreshCw, X } from "lucide-react";
import type { UpdateCheckResult, UpdateInstallState } from "../types/studio";

interface UpdateModalProps {
  open: boolean;
  updateInfo: UpdateCheckResult | null;
  installState: UpdateInstallState;
  checking: boolean;
  onClose: () => void;
  onRecheck: () => void;
  onInstall: () => void;
}

export function UpdateModal({
  open,
  updateInfo,
  installState,
  checking,
  onClose,
  onRecheck,
  onInstall,
}: UpdateModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && installState.status !== "downloading" && installState.status !== "installing") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, installState.status]);

  if (!open) return null;

  const isBusy = installState.status === "downloading" || installState.status === "installing";
  const sizeMb = updateInfo?.asset_size ? (updateInfo.asset_size / (1024 * 1024)).toFixed(1) : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="App Update"
      onClick={() => {
        if (!isBusy) onClose();
      }}
    >
      <div
        className="w-full max-w-md rounded-xl border border-zinc-700 bg-[#151821] p-5 shadow-2xl space-y-4 text-xs"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 pb-2.5">
          <div className="flex items-center gap-2">
            <ArrowUpCircle className="h-4 w-4 text-emerald-400" />
            <h2 className="text-sm font-semibold text-zinc-100">Software Update</h2>
          </div>
          {!isBusy && (
            <button
              type="button"
              onClick={onClose}
              className="rounded p-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>

        {checking && !updateInfo ? (
          <div className="flex items-center gap-2 py-4 text-zinc-300">
            <Loader2 className="h-4 w-4 animate-spin text-emerald-400" />
            <span>Checking GitHub releases...</span>
          </div>
        ) : updateInfo ? (
          <div className="space-y-3">
            <div className="rounded-lg border border-zinc-800 bg-[#11141b] p-3 font-mono text-[11px] space-y-1.5">
              <div className="flex justify-between">
                <span className="text-zinc-400">Installed Version</span>
                <span className="text-zinc-200">{updateInfo.current_version}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-zinc-400">Latest Release</span>
                <span className={updateInfo.update_available ? "text-emerald-300 font-semibold" : "text-zinc-200"}>
                  {updateInfo.latest_version ?? "None published"}
                </span>
              </div>
              {sizeMb && (
                <div className="flex justify-between">
                  <span className="text-zinc-400">Installer Size</span>
                  <span className="text-zinc-300">{sizeMb} MB</span>
                </div>
              )}
            </div>

            {updateInfo.error && (
              <div className="rounded border border-amber-500/40 bg-amber-500/10 p-2.5 text-amber-200">
                {updateInfo.error}
              </div>
            )}

            {!updateInfo.error && !updateInfo.update_available && (
              <div className="flex items-center gap-2 rounded border border-emerald-500/30 bg-emerald-500/10 p-2.5 text-emerald-200">
                <CheckCircle2 className="h-4 w-4 shrink-0" />
                <span>You&apos;re up to date!</span>
              </div>
            )}

            {updateInfo.update_available && updateInfo.release_name && (
              <div className="space-y-1">
                <div className="text-[11px] font-semibold text-zinc-200">
                  {updateInfo.release_name}
                </div>
                {updateInfo.release_notes && (
                  <pre className="max-h-36 overflow-y-auto whitespace-pre-wrap rounded border border-zinc-800 bg-zinc-950/70 p-2.5 font-sans text-[11px] text-zinc-300">
                    {updateInfo.release_notes.trim()}
                  </pre>
                )}
              </div>
            )}

            {isBusy && (
              <div className="space-y-1.5 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3">
                <div className="flex items-center justify-between text-[11px] text-emerald-200">
                  <span className="flex items-center gap-1.5">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    {installState.stage || "Updating..."}
                  </span>
                  <span className="font-mono">{installState.progress}%</span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-zinc-800">
                  <div
                    className="h-full bg-emerald-400 transition-all duration-300"
                    style={{ width: `${Math.max(4, Math.min(100, installState.progress))}%` }}
                  />
                </div>
              </div>
            )}

            {installState.status === "error" && installState.error && (
              <div className="rounded border border-rose-500/40 bg-rose-500/10 p-2.5 text-rose-200">
                Update failed: {installState.error}
              </div>
            )}

            {!updateInfo.can_self_update && updateInfo.update_available && (
              <p className="text-[11px] text-zinc-400">
                One-click auto-install is available in the installed Windows desktop app. You can download the installer directly from GitHub below.
              </p>
            )}
          </div>
        ) : null}

        <div className="flex items-center justify-between gap-2 pt-1">
          <button
            type="button"
            disabled={checking || isBusy}
            onClick={onRecheck}
            className="inline-flex items-center gap-1.5 rounded border border-zinc-700 bg-zinc-800/80 px-2.5 py-1.5 text-zinc-300 hover:bg-zinc-700 disabled:opacity-40"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${checking ? "animate-spin" : ""}`} />
            Check Again
          </button>

          <div className="flex items-center gap-2">
            {updateInfo?.release_url && (
              <a
                href={updateInfo.release_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 rounded border border-zinc-700 px-2.5 py-1.5 text-zinc-300 hover:bg-zinc-800"
              >
                <ExternalLink className="h-3.5 w-3.5" />
                Release Page
              </a>
            )}
            {updateInfo?.update_available && updateInfo.can_self_update && updateInfo.download_url && (
              <button
                type="button"
                disabled={isBusy}
                onClick={onInstall}
                className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-3.5 py-1.5 font-semibold text-white hover:bg-emerald-500 disabled:opacity-40"
              >
                <Download className="h-3.5 w-3.5" />
                Download &amp; Install
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
