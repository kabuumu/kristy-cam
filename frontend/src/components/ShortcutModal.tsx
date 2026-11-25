import { useEffect } from "react";
import { Keyboard, X } from "lucide-react";

interface ShortcutModalProps {
  open: boolean;
  onClose: () => void;
}

const SHORTCUT_GROUPS: Array<{
  title: string;
  items: Array<{ keys: string; desc: string }>;
}> = [
  {
    title: "Playback & Navigation",
    items: [
      { keys: "Space", desc: "Play / Pause" },
      { keys: "← / →", desc: "Step backward / forward 1 frame" },
      { keys: "Shift + ← / →", desc: "Step backward / forward 10 frames" },
      { keys: "↑ / ↓", desc: "Jump to previous / next cut or marker" },
      { keys: "Home / End", desc: "Jump to timeline start / end" },
      { keys: "I / O", desc: "Mark Source In / Out point" },
    ],
  },
  {
    title: "Timeline Editing",
    items: [
      { keys: "S or Ctrl/Cmd + B", desc: "Split clip at playhead" },
      { keys: "Backspace / Delete", desc: "Delete selected clip(s) or keyframe" },
      { keys: "Shift + Backspace", desc: "Ripple delete (closes gap)" },
      { keys: "Ctrl/Cmd + C / V", desc: "Copy / Paste selected clip at playhead" },
      { keys: "Ctrl/Cmd + D", desc: "Duplicate selected clip" },
      { keys: "Alt + Drag clip", desc: "Slip video media in/out without moving clip" },
      { keys: "Shift + Click / Drag", desc: "Multi-select clips or bypass snapping" },
      { keys: "Right-Click clip/gap", desc: "Open context menu (Split, Ripple, Close Gap...)" },
    ],
  },
  {
    title: "Tools, Zoom & Project",
    items: [
      { keys: "N", desc: "Toggle magnetic timeline snapping" },
      { keys: "M", desc: "Drop timeline marker at playhead" },
      { keys: "+ / - or Ctrl + Wheel", desc: "Zoom timeline in / out" },
      { keys: "Ctrl/Cmd + S", desc: "Save project immediately" },
      { keys: "Ctrl/Cmd + Z", desc: "Undo" },
      { keys: "Ctrl/Cmd + Shift + Z / Y", desc: "Redo" },
      { keys: "?", desc: "Toggle this shortcuts guide" },
    ],
  },
];

export function ShortcutModal({ open, onClose }: ShortcutModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard Shortcuts"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl rounded-xl border border-zinc-700 bg-[#151821] p-5 shadow-2xl space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 pb-2.5">
          <div className="flex items-center gap-2">
            <Keyboard className="h-4 w-4 text-emerald-400" />
            <h2 className="text-sm font-semibold text-zinc-100">
              Keyboard Shortcuts
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

        <div className="grid gap-4 md:grid-cols-2">
          {SHORTCUT_GROUPS.map((group) => (
            <div
              key={group.title}
              className="rounded-lg border border-zinc-800 bg-[#11141b] p-3 space-y-2"
            >
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-emerald-300">
                {group.title}
              </h3>
              <div className="space-y-1.5 text-xs">
                {group.items.map((item) => (
                  <div
                    key={item.keys}
                    className="flex items-center justify-between gap-2"
                  >
                    <span className="text-zinc-300">{item.desc}</span>
                    <kbd className="shrink-0 rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-[10px] text-zinc-200">
                      {item.keys}
                    </kbd>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
