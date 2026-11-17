import type { ProjectInput, TimelineSelection } from "../types/studio";

export const MAX_HISTORY_ENTRIES = 150;
export const COALESCE_WINDOW_MS = 600;

export interface HistorySnapshot {
  project: ProjectInput;
  selectedSegId: string | null;
  selection: TimelineSelection;
}

export interface HistoryEntry {
  before: HistorySnapshot;
  after: HistorySnapshot;
  label: string;
  coalesceKey?: string;
  timestamp: number;
}

export interface UpdateMeta {
  label?: string;
  coalesceKey?: string;
}

export interface HistoryTransaction {
  startSnapshot: HistorySnapshot;
  label: string;
  depth: number;
}

export interface UndoHistoryState {
  past: HistoryEntry[];
  future: HistoryEntry[];
  transaction: HistoryTransaction | null;
}

export function createInitialHistoryState(): UndoHistoryState {
  return {
    past: [],
    future: [],
    transaction: null,
  };
}

export function serializeProjectContent(project: ProjectInput): string {
  const { revision: _rev, updated_at: _upd, ...rest } = project;
  return JSON.stringify(rest);
}

export function areProjectsEqual(a: ProjectInput, b: ProjectInput): boolean {
  if (a === b) return true;
  return serializeProjectContent(a) === serializeProjectContent(b);
}

export function cloneSnapshot(snapshot: HistorySnapshot): HistorySnapshot {
  return structuredClone(snapshot);
}

export function beginHistoryTransaction(
  state: UndoHistoryState,
  snapshot: HistorySnapshot,
  label: string
): UndoHistoryState {
  if (state.transaction) {
    return {
      ...state,
      transaction: {
        ...state.transaction,
        depth: state.transaction.depth + 1,
      },
    };
  }
  return {
    ...state,
    transaction: {
      startSnapshot: cloneSnapshot(snapshot),
      label,
      depth: 1,
    },
  };
}

export function endHistoryTransaction(
  state: UndoHistoryState,
  currentSnapshot: HistorySnapshot,
  now: number = Date.now()
): UndoHistoryState {
  const tx = state.transaction;
  if (!tx) return state;

  if (tx.depth > 1) {
    return {
      ...state,
      transaction: {
        ...tx,
        depth: tx.depth - 1,
      },
    };
  }

  if (areProjectsEqual(tx.startSnapshot.project, currentSnapshot.project)) {
    return {
      ...state,
      transaction: null,
    };
  }

  const entry: HistoryEntry = {
    before: tx.startSnapshot,
    after: cloneSnapshot(currentSnapshot),
    label: tx.label,
    timestamp: now,
  };

  const nextPast = [...state.past, entry];
  if (nextPast.length > MAX_HISTORY_ENTRIES) {
    nextPast.splice(0, nextPast.length - MAX_HISTORY_ENTRIES);
  }

  return {
    past: nextPast,
    future: [],
    transaction: null,
  };
}

export function recordHistoryUpdate(
  state: UndoHistoryState,
  before: HistorySnapshot,
  after: HistorySnapshot,
  meta?: UpdateMeta,
  now: number = Date.now()
): UndoHistoryState {
  // While inside an explicit drag/batch transaction, defer committing until endHistoryTransaction.
  if (state.transaction) {
    return state;
  }

  if (areProjectsEqual(before.project, after.project)) {
    return state;
  }

  const label = meta?.label ?? "Edit project";
  const coalesceKey = meta?.coalesceKey;
  const last = state.past[state.past.length - 1];

  if (
    coalesceKey &&
    last &&
    last.coalesceKey === coalesceKey &&
    now - last.timestamp <= COALESCE_WINDOW_MS
  ) {
    const clonedAfter = cloneSnapshot(after);
    if (areProjectsEqual(last.before.project, clonedAfter.project)) {
      // Coalesced back to original state -> remove the no-op entry
      return {
        ...state,
        past: state.past.slice(0, -1),
        future: [],
      };
    }
    const updatedLast: HistoryEntry = {
      ...last,
      after: clonedAfter,
      label,
      timestamp: now,
    };
    return {
      ...state,
      past: [...state.past.slice(0, -1), updatedLast],
      future: [],
    };
  }

  const entry: HistoryEntry = {
    before: cloneSnapshot(before),
    after: cloneSnapshot(after),
    label,
    coalesceKey,
    timestamp: now,
  };

  const nextPast = [...state.past, entry];
  if (nextPast.length > MAX_HISTORY_ENTRIES) {
    nextPast.splice(0, nextPast.length - MAX_HISTORY_ENTRIES);
  }

  return {
    ...state,
    past: nextPast,
    future: [],
  };
}

export function sanitizeRestoredSnapshot(
  snapshot: HistorySnapshot,
  currentProject: ProjectInput
): HistorySnapshot {
  const cloned = cloneSnapshot(snapshot);
  // Always preserve current server revision & updated_at to avoid stale 409 conflicts on save
  cloned.project.revision = currentProject.revision;
  cloned.project.updated_at = currentProject.updated_at;

  const segIds = new Set(cloned.project.segments.map((s) => s.id));
  let validSegId =
    cloned.selectedSegId && segIds.has(cloned.selectedSegId)
      ? cloned.selectedSegId
      : cloned.project.segments[0]?.id ?? null;

  let validSelection: TimelineSelection = cloned.selection;
  if (validSelection) {
    if (validSelection.type === "video") {
      if (!segIds.has(validSelection.segId)) {
        validSelection = validSegId ? { type: "video", segId: validSegId } : null;
      }
    } else if (validSelection.type === "clause") {
      const seg = cloned.project.segments.find((s) => s.id === validSelection!.segId);
      if (!seg || validSelection.index < 0 || validSelection.index >= seg.clauses.length) {
        validSelection = validSegId ? { type: "video", segId: validSegId } : null;
      } else {
        validSegId = seg.id;
      }
    } else if (validSelection.type === "keyframe") {
      const kfs = cloned.project.camera_keyframes ?? [];
      if (validSelection.index < 0 || validSelection.index >= kfs.length) {
        validSelection = validSegId ? { type: "video", segId: validSegId } : null;
      }
    }
  }

  return {
    project: cloned.project,
    selectedSegId: validSegId,
    selection: validSelection,
  };
}

export function stepUndo(
  state: UndoHistoryState,
  currentProject: ProjectInput
): {
  state: UndoHistoryState;
  restored: HistorySnapshot;
  entry: HistoryEntry;
} | null {
  if (state.past.length === 0) return null;
  const entry = state.past[state.past.length - 1];
  const restored = sanitizeRestoredSnapshot(entry.before, currentProject);
  return {
    state: {
      past: state.past.slice(0, -1),
      future: [entry, ...state.future],
      transaction: null,
    },
    restored,
    entry,
  };
}

export function stepRedo(
  state: UndoHistoryState,
  currentProject: ProjectInput
): {
  state: UndoHistoryState;
  restored: HistorySnapshot;
  entry: HistoryEntry;
} | null {
  if (state.future.length === 0) return null;
  const entry = state.future[0];
  const restored = sanitizeRestoredSnapshot(entry.after, currentProject);
  return {
    state: {
      past: [...state.past, entry],
      future: state.future.slice(1),
      transaction: null,
    },
    restored,
    entry,
  };
}
