import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Download,
  RefreshCw,
  ScrollText,
  X,
} from "lucide-react";
import { apiClient } from "../api/client";
import { usageLogger, type UsageLogEvent } from "../utils/usageLogger";

interface ActivityLogModalProps {
  open: boolean;
  projectId: string | null;
  onClose: () => void;
}

const CATEGORY_FILTERS = [
  { value: "", label: "All Events" },
  { value: "error", label: "Errors" },
  { value: "action_output", label: "Action Outputs" },
  { value: "intention", label: "Intentions" },
  { value: "click", label: "Clicks" },
  { value: "keypress", label: "Key Presses" },
  { value: "job", label: "Jobs" },
] as const;

export function ActivityLogModal({
  open,
  projectId,
  onClose,
}: ActivityLogModalProps) {
  const [events, setEvents] = useState<UsageLogEvent[]>([]);
  const [category, setCategory] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [logPath, setLogPath] = useState<string>("");

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await usageLogger.flush();
      const res = await apiClient.getLogs({
        limit: 250,
        category: category || undefined,
      });
      setEvents([...res.events].reverse());
      setLogPath(res.usage_log_path);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load logs");
      setEvents([...usageLogger.getRecentLocalEvents()].reverse());
    } finally {
      setLoading(false);
    }
  }, [category]);

  useEffect(() => {
    if (open) {
      void fetchLogs();
    }
  }, [open, fetchLogs]);

  if (!open) return null;

  const handleDownload = () => {
    const lines = [...events]
      .reverse()
      .map((ev) => JSON.stringify(ev))
      .join("\n");
    const blob = new Blob([lines ? `${lines}\n` : ""], {
      type: "application/x-ndjson",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `usage-log-${projectId ?? "studio"}.jsonl`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Usage & Debug Log"
    >
      <div className="flex max-h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-zinc-700 bg-[#151821] shadow-2xl">
        <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
          <div className="flex items-center gap-2">
            <ScrollText className="h-4 w-4 text-emerald-400" />
            <h2 className="text-sm font-semibold text-zinc-100">
              Usage &amp; Debug Log
            </h2>
            {logPath && (
              <span className="hidden md:inline truncate max-w-[340px] font-mono text-[11px] text-zinc-400">
                {logPath}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <select
              aria-label="Filter Log Category"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 focus:border-emerald-500 focus:outline-none"
            >
              {CATEGORY_FILTERS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => void fetchLogs()}
              disabled={loading}
              className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2.5 py-1 text-xs text-zinc-200 hover:bg-zinc-700 disabled:opacity-50"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
              />
              Refresh
            </button>
            <button
              type="button"
              onClick={handleDownload}
              className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2.5 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
            >
              <Download className="h-3.5 w-3.5" />
              Export JSONL
            </button>
            <button
              type="button"
              aria-label="Close Usage Log"
              onClick={onClose}
              className="rounded p-1 text-zinc-400 hover:bg-zinc-800"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {error && (
          <div className="flex items-center gap-2 border-b border-amber-500/40 bg-amber-500/15 px-4 py-2 text-xs text-amber-200">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>{error} (showing local buffer)</span>
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-3 space-y-2 text-xs font-mono">
          {events.length === 0 ? (
            <div className="py-12 text-center text-zinc-500">
              No log events recorded yet.
            </div>
          ) : (
            events.map((ev, idx) => {
              const isErr = ev.category === "error" || Boolean(ev.error);
              return (
                <div
                  key={`${ev.ts}-${idx}`}
                  className={`rounded-lg border p-2.5 space-y-1 ${
                    isErr
                      ? "border-rose-500/40 bg-rose-500/10 text-rose-100"
                      : "border-zinc-800 bg-zinc-900/70 text-zinc-200"
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                          isErr
                            ? "bg-rose-500/30 text-rose-200"
                            : ev.category === "action_output"
                              ? "bg-emerald-500/20 text-emerald-300"
                              : ev.category === "click" ||
                                  ev.category === "keypress"
                                ? "bg-sky-500/20 text-sky-300"
                                : "bg-zinc-700/60 text-zinc-300"
                        }`}
                      >
                        {ev.category}
                      </span>
                      <span className="font-semibold text-zinc-100">
                        {ev.intention || ev.action}
                      </span>
                      <span className="text-[11px] text-zinc-400">
                        ({ev.action}
                        {ev.target ? ` -> ${ev.target}` : ""})
                      </span>
                    </div>
                    <div className="flex items-center gap-2 text-[10px] text-zinc-400">
                      {ev.source && <span>{ev.source}</span>}
                      {ev.duration_ms !== undefined && (
                        <span>{ev.duration_ms.toFixed(1)}ms</span>
                      )}
                      <span>{ev.ts}</span>
                    </div>
                  </div>

                  {ev.input !== undefined && (
                    <div className="text-[11px] text-zinc-400 break-all">
                      <span className="text-zinc-500">in:</span>{" "}
                      {JSON.stringify(ev.input)}
                    </div>
                  )}
                  {ev.output !== undefined && (
                    <div className="text-[11px] text-emerald-300/90 break-all">
                      <span className="text-emerald-500">out:</span>{" "}
                      {JSON.stringify(ev.output)}
                    </div>
                  )}
                  {ev.error && (
                    <div className="space-y-1 text-[11px] text-rose-200 break-all">
                      <div>
                        <span className="font-semibold">
                          {ev.error.type}:
                        </span>{" "}
                        {ev.error.message}
                        {ev.error.status_code
                          ? ` (HTTP ${ev.error.status_code})`
                          : ""}
                      </div>
                      {ev.error.context && (
                        <div className="text-rose-300/80">
                          context: {JSON.stringify(ev.error.context)}
                        </div>
                      )}
                      {ev.error.stack && (
                        <pre className="mt-1 max-h-36 overflow-auto rounded bg-black/40 p-2 text-[10px] text-rose-200/90 whitespace-pre-wrap">
                          {ev.error.stack}
                        </pre>
                      )}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
