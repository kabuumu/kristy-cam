import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";

export interface ToastItem {
  id: string;
  message: string;
  kind?: "info" | "success" | "warning";
}

interface ToastContainerProps {
  toasts: ToastItem[];
  onDismiss: (id: string) => void;
}

export function ToastContainer({ toasts, onDismiss }: ToastContainerProps) {
  if (toasts.length === 0) return null;
  return (
    <div
      aria-live="polite"
      className="pointer-events-none fixed right-4 bottom-12 z-50 flex flex-col gap-2"
    >
      {toasts.map((t) => {
        const kind = t.kind ?? "info";
        const Icon =
          kind === "success"
            ? CheckCircle2
            : kind === "warning"
              ? AlertTriangle
              : Info;
        const colors =
          kind === "success"
            ? "border-emerald-500/50 bg-emerald-950/95 text-emerald-100"
            : kind === "warning"
              ? "border-amber-500/50 bg-amber-950/95 text-amber-100"
              : "border-zinc-700 bg-zinc-900/95 text-zinc-100";
        return (
          <div
            key={t.id}
            className={`pointer-events-auto flex items-center gap-2 rounded-lg border px-3 py-2 text-xs shadow-xl backdrop-blur ${colors}`}
          >
            <Icon className="h-3.5 w-3.5 shrink-0" />
            <span>{t.message}</span>
            <button
              type="button"
              onClick={() => onDismiss(t.id)}
              aria-label="Dismiss notification"
              className="ml-1 rounded p-0.5 opacity-70 hover:opacity-100"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
