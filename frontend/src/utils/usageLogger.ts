export type UsageEventCategory =
  | "intention"
  | "click"
  | "keypress"
  | "action_output"
  | "error";

export interface UsageLogErrorPayload {
  message: string;
  type: string;
  status_code?: number;
  detail?: unknown;
  stack?: string;
  context?: Record<string, unknown>;
}

export interface UsageLogEvent {
  ts: string;
  source?: "frontend" | "backend" | "job";
  category: UsageEventCategory | string;
  action: string;
  intention?: string;
  target?: string;
  project_id?: string | null;
  seg_id?: string | null;
  job_id?: string | null;
  input?: unknown;
  output?: unknown;
  error?: UsageLogErrorPayload;
  duration_ms?: number;
}

export interface UsageContext {
  projectId?: string | null;
  segId?: string | null;
}

function readSessionToken(): string | null {
  if (typeof document === "undefined") return null;
  const meta = document.querySelector('meta[name="session-token"]');
  const token = meta?.getAttribute("content")?.trim() ?? "";
  if (!token || token === "__SESSION_TOKEN__") return null;
  return token;
}

function describeElement(el: HTMLElement): {
  label: string;
  tag: string;
  intent?: string;
} {
  const tag = el.tagName.toLowerCase();
  const intent = el.getAttribute("data-log-intent")?.trim() || undefined;
  const aria = el.getAttribute("aria-label")?.trim();
  const title = el.getAttribute("title")?.trim();
  const text = (el.textContent ?? "").replace(/\s+/g, " ").trim();
  const label =
    intent ||
    aria ||
    title ||
    (text ? text.slice(0, 80) : "") ||
    (el instanceof HTMLInputElement
      ? `${tag}[type=${el.type || "text"}]`
      : tag);
  return { label, tag, intent };
}

function isTextEditableElement(el: Element | null): boolean {
  if (!el) return false;
  const tag = el.tagName.toLowerCase();
  if (tag === "textarea" || tag === "select") return true;
  if (tag === "input") {
    const type = ((el as HTMLInputElement).type || "text").toLowerCase();
    return !["button", "submit", "reset", "checkbox", "radio", "range", "file"].includes(
      type
    );
  }
  return el instanceof HTMLElement && el.isContentEditable;
}

function formatKeyCombo(e: KeyboardEvent): string {
  const parts: string[] = [];
  if (e.ctrlKey) parts.push("Ctrl");
  if (e.metaKey) parts.push("Cmd");
  if (e.altKey) parts.push("Alt");
  if (e.shiftKey && e.key.length > 1) parts.push("Shift");
  const keyName = e.key === " " ? "Space" : e.key;
  parts.push(keyName);
  return parts.join("+");
}

function normalizeError(err: unknown): UsageLogErrorPayload {
  if (err instanceof Error) {
    const anyErr = err as Error & {
      status?: number;
      detail?: unknown;
      payload?: unknown;
    };
    return {
      message: err.message || err.name,
      type: err.name || "Error",
      status_code: typeof anyErr.status === "number" ? anyErr.status : undefined,
      detail: anyErr.detail ?? anyErr.payload,
      stack: err.stack,
    };
  }
  if (typeof err === "string") {
    return { message: err, type: "Error" };
  }
  return {
    message: String(err),
    type: "UnknownError",
  };
}

class UsageLogger {
  private initialized = false;
  private getContext: () => UsageContext = () => ({});
  private queue: UsageLogEvent[] = [];
  private recent: UsageLogEvent[] = [];
  private flushTimer: number | null = null;
  private readonly maxRecent = 300;
  private readonly maxQueue = 100;

  init(getContext?: () => UsageContext): void {
    if (getContext) {
      this.getContext = getContext;
    }
    if (this.initialized || typeof window === "undefined") {
      return;
    }
    this.initialized = true;

    window.addEventListener("click", this.handleGlobalClick, true);
    window.addEventListener("keydown", this.handleGlobalKeyDown, true);
    window.addEventListener("error", this.handleWindowError);
    window.addEventListener("unhandledrejection", this.handleUnhandledRejection);
    window.addEventListener("pagehide", () => {
      void this.flush(true);
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        void this.flush(true);
      }
    });
  }

  setContextProvider(getContext: () => UsageContext): void {
    this.getContext = getContext;
  }

  private enqueue(event: UsageLogEvent, flushImmediately = false): void {
    const ctx = this.getContext();
    const enriched: UsageLogEvent = {
      ...event,
      project_id: event.project_id !== undefined ? event.project_id : ctx.projectId ?? undefined,
      seg_id: event.seg_id !== undefined ? event.seg_id : ctx.segId ?? undefined,
    };

    this.recent.push(enriched);
    if (this.recent.length > this.maxRecent) {
      this.recent.shift();
    }

    this.queue.push(enriched);
    if (this.queue.length > this.maxQueue) {
      this.queue.shift();
    }

    if (flushImmediately || this.queue.length >= 15) {
      void this.flush();
    } else if (this.flushTimer === null && typeof window !== "undefined") {
      this.flushTimer = window.setTimeout(() => {
        this.flushTimer = null;
        void this.flush();
      }, 1500);
    }
  }

  logIntention(
    intention: string,
    action: string,
    details?: {
      target?: string;
      input?: unknown;
      projectId?: string | null;
      segId?: string | null;
      jobId?: string | null;
    }
  ): void {
    this.enqueue({
      ts: new Date().toISOString(),
      category: "intention",
      action,
      intention,
      target: details?.target,
      project_id: details?.projectId,
      seg_id: details?.segId,
      job_id: details?.jobId,
      input: details?.input,
    });
  }

  logActionOutput(
    intention: string,
    action: string,
    output: unknown,
    details?: {
      target?: string;
      input?: unknown;
      projectId?: string | null;
      segId?: string | null;
      jobId?: string | null;
      durationMs?: number;
    }
  ): void {
    this.enqueue({
      ts: new Date().toISOString(),
      category: "action_output",
      action,
      intention,
      target: details?.target,
      project_id: details?.projectId,
      seg_id: details?.segId,
      job_id: details?.jobId,
      input: details?.input,
      output,
      duration_ms: details?.durationMs,
    });
  }

  logError(
    intention: string,
    action: string,
    error: unknown,
    details?: {
      target?: string;
      input?: unknown;
      context?: Record<string, unknown>;
      projectId?: string | null;
      segId?: string | null;
      jobId?: string | null;
      statusCode?: number;
      durationMs?: number;
    }
  ): void {
    const normalized = normalizeError(error);
    if (details?.statusCode !== undefined) {
      normalized.status_code = details.statusCode;
    }
    if (details?.context) {
      normalized.context = { ...(normalized.context ?? {}), ...details.context };
    }
    this.enqueue(
      {
        ts: new Date().toISOString(),
        category: "error",
        action,
        intention,
        target: details?.target,
        project_id: details?.projectId,
        seg_id: details?.segId,
        job_id: details?.jobId,
        input: details?.input,
        error: normalized,
        duration_ms: details?.durationMs,
      },
      true
    );
  }

  getRecentLocalEvents(): UsageLogEvent[] {
    return [...this.recent];
  }

  async flush(keepalive = false): Promise<void> {
    if (this.flushTimer !== null && typeof window !== "undefined") {
      window.clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }
    if (this.queue.length === 0) return;

    const token = readSessionToken();
    if (!token) {
      return;
    }

    const batch = this.queue.splice(0, 100);
    try {
      await fetch("/api/logs/events", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Session-Token": token,
        },
        body: JSON.stringify({ events: batch }),
        keepalive,
      });
    } catch {
      // Never throw or recurse on logging transport failures
    }
  }

  private handleGlobalClick = (e: MouseEvent): void => {
    const targetEl = e.target instanceof HTMLElement ? e.target : null;
    if (!targetEl) return;
    const interactive =
      targetEl.closest(
        'button, a, select, input, [role="button"], [data-log-intent]'
      ) ?? targetEl;
    if (!(interactive instanceof HTMLElement)) return;

    const { label, tag, intent } = describeElement(interactive);
    const inputDetails: Record<string, unknown> = { tag };
    if (interactive instanceof HTMLInputElement) {
      inputDetails.type = interactive.type;
      if (interactive.type === "checkbox" || interactive.type === "radio") {
        inputDetails.checked = !interactive.checked;
      }
    }
    this.enqueue({
      ts: new Date().toISOString(),
      category: "click",
      action: "ui.click",
      intention: intent ? intent : `Click ${label}`,
      target: label,
      input: inputDetails,
    });
  };

  private handleGlobalKeyDown = (e: KeyboardEvent): void => {
    if (e.repeat) return;
    const active = document.activeElement;
    const inTextInput = isTextEditableElement(active);
    const hasModifier = e.ctrlKey || e.metaKey || e.altKey;
    const isSpecialKey = [
      "Escape",
      "Enter",
      "Delete",
      "Backspace",
      "Tab",
      " ",
      "ArrowLeft",
      "ArrowRight",
      "ArrowUp",
      "ArrowDown",
    ].includes(e.key);

    if (inTextInput && !hasModifier && e.key !== "Enter" && e.key !== "Escape") {
      return;
    }
    if (!inTextInput && !hasModifier && !isSpecialKey && e.key.length > 1) {
      return;
    }

    const combo = formatKeyCombo(e);
    let intention = `Press ${combo}`;
    if (!inTextInput && !hasModifier) {
      if (e.key === "i" || e.key === "I") intention = "Shortcut: Set Mark In";
      else if (e.key === "o" || e.key === "O") intention = "Shortcut: Set Mark Out";
      else if (e.key === "Escape") intention = "Shortcut: Escape / Dismiss";
    }

    this.enqueue({
      ts: new Date().toISOString(),
      category: "keypress",
      action: "ui.keypress",
      intention,
      target: combo,
      input: {
        key: e.key,
        code: e.code,
        in_text_input: inTextInput,
      },
    });
  };

  private handleWindowError = (e: ErrorEvent): void => {
    this.logError(
      "Unhandled frontend runtime error",
      "ui.unhandled_error",
      e.error ?? e.message,
      {
        context: {
          filename: e.filename,
          lineno: e.lineno,
          colno: e.colno,
        },
      }
    );
  };

  private handleUnhandledRejection = (e: PromiseRejectionEvent): void => {
    this.logError(
      "Unhandled promise rejection",
      "ui.unhandled_rejection",
      e.reason
    );
  };
}

export const usageLogger = new UsageLogger();
