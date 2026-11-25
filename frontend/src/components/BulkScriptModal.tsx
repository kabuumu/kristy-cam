import { useEffect, useMemo, useState } from "react";
import { Captions, Sparkles, X } from "lucide-react";
import type { ProjectInput } from "../types/studio";
import { parseBulkScriptToCaptionClips } from "../utils/studioMath";

interface BulkScriptModalProps {
  open: boolean;
  project: ProjectInput;
  defaultStartSec: number;
  onClose: () => void;
  onInsertScript: (options: {
    scriptText: string;
    startSec: number;
    ttsEnabled: boolean;
    speed: number;
    autoSynthesize: boolean;
  }) => void;
}

export function BulkScriptModal({
  open,
  project,
  defaultStartSec,
  onClose,
  onInsertScript,
}: BulkScriptModalProps) {
  const [scriptText, setScriptText] = useState("");
  const [startSec, setStartSec] = useState(defaultStartSec);
  const [ttsEnabled, setTtsEnabled] = useState(true);
  const [speed, setSpeed] = useState(1.0);
  const [autoSynthesize, setAutoSynthesize] = useState(true);

  useEffect(() => {
    if (open) {
      setStartSec(defaultStartSec);
    }
  }, [open, defaultStartSec]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const previewClips = useMemo(
    () =>
      parseBulkScriptToCaptionClips(scriptText, startSec, project, {
        ttsEnabled,
        speed,
      }),
    [scriptText, startSec, project, ttsEnabled, speed]
  );

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Bulk Script Import"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl rounded-xl border border-zinc-700 bg-[#151821] p-5 shadow-2xl space-y-4 text-xs"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 pb-2.5">
          <div className="flex items-center gap-2">
            <Captions className="h-4 w-4 text-sky-400" />
            <h2 className="text-sm font-semibold text-zinc-100">
              Bulk Script → Auto-Split Captions
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

        <label className="block space-y-1">
          <span className="text-zinc-300">
            Paste full narration script (auto-splits by sentence &amp; clause):
          </span>
          <textarea
            rows={5}
            value={scriptText}
            onChange={(e) => setScriptText(e.target.value)}
            placeholder="So my Sim tried to cook grilled cheese at 3 AM. Naturally, the entire kitchen burst into flames..."
            className="w-full rounded border border-zinc-700 bg-zinc-900 p-2.5 text-zinc-100 focus:border-sky-500 focus:outline-none"
          />
        </label>

        <div className="grid grid-cols-3 gap-2.5">
          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Insert Start (s)</span>
            <input
              type="number"
              min={0}
              step={0.1}
              value={Number(startSec.toFixed(2))}
              onChange={(e) => setStartSec(Math.max(0, parseFloat(e.target.value) || 0))}
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-zinc-400">Speed ({speed.toFixed(2)}x)</span>
            <input
              type="range"
              min={0.8}
              max={1.25}
              step={0.05}
              value={speed}
              onChange={(e) => setSpeed(parseFloat(e.target.value))}
            />
          </label>
          <div className="flex flex-col justify-center gap-1.5 pt-2">
            <label className="flex items-center gap-1.5 text-zinc-200">
              <input
                type="checkbox"
                checked={ttsEnabled}
                onChange={(e) => setTtsEnabled(e.target.checked)}
              />
              <span>Speak Kokoro TTS</span>
            </label>
            {ttsEnabled && (
              <label className="flex items-center gap-1.5 text-zinc-300">
                <input
                  type="checkbox"
                  checked={autoSynthesize}
                  onChange={(e) => setAutoSynthesize(e.target.checked)}
                />
                <span>Synthesize right away</span>
              </label>
            )}
          </div>
        </div>

        <div className="rounded-lg border border-zinc-800 bg-[#11141b] p-2.5 space-y-1.5">
          <div className="flex items-center justify-between text-[11px] text-zinc-400">
            <span>Preview ({previewClips.length} caption block(s))</span>
            {previewClips.length > 0 && (
              <span className="font-mono text-sky-300">
                Est. end:{" "}
                {(
                  previewClips[previewClips.length - 1].start_sec +
                  (previewClips[previewClips.length - 1].duration_sec ?? 1.5)
                ).toFixed(1)}
                s
              </span>
            )}
          </div>
          <div className="max-h-36 space-y-1 overflow-y-auto pr-1">
            {previewClips.length === 0 ? (
              <p className="text-[11px] text-zinc-500">
                Enter script text above to preview generated blocks.
              </p>
            ) : (
              previewClips.map((c, idx) => (
                <div
                  key={c.id}
                  className="flex items-center gap-2 rounded bg-zinc-900/80 px-2 py-1 text-[11px]"
                >
                  <span className="w-6 shrink-0 font-mono text-zinc-500">
                    #{idx + 1}
                  </span>
                  <span className="w-12 shrink-0 font-mono text-sky-300">
                    {c.start_sec.toFixed(1)}s
                  </span>
                  <span className="truncate text-zinc-200">{c.display_text}</span>
                </div>
              ))
            )}
          </div>
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
            disabled={previewClips.length === 0}
            onClick={() => {
              onInsertScript({
                scriptText,
                startSec,
                ttsEnabled,
                speed,
                autoSynthesize,
              });
              setScriptText("");
              onClose();
            }}
            className="inline-flex items-center gap-1.5 rounded bg-sky-600 px-4 py-1.5 font-semibold text-white hover:bg-sky-500 disabled:opacity-40"
          >
            <Sparkles className="h-3.5 w-3.5" />
            Insert {previewClips.length} Block(s)
          </button>
        </div>
      </div>
    </div>
  );
}
