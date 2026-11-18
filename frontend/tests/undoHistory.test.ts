import assert from "node:assert/strict";
import test from "node:test";
import type { ProjectInput } from "../src/types/studio.ts";
import {
  COALESCE_WINDOW_MS,
  MAX_HISTORY_ENTRIES,
  beginHistoryTransaction,
  createInitialHistoryState,
  endHistoryTransaction,
  recordHistoryUpdate,
  serializeProjectContent,
  stepRedo,
  stepUndo,
  type HistorySnapshot,
} from "../src/utils/undoHistory.ts";

function makeProject(overrides: Partial<ProjectInput> = {}): ProjectInput {
  return {
    schema_version: 1,
    project_id: "proj_test",
    revision: 1,
    title: "Test Project",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    source_media: {
      media_id: "src_1",
      path: "/tmp/video.mp4",
      fingerprint: "fp_1",
      copied_to_project: false,
    },
    voice: "af_heart",
    language: "en-us",
    output_settings: {
      width: 1080,
      height: 1920,
      fps: 30,
      default_source_volume: 0.25,
      tail_padding_sec: 0.2,
      inter_clause_pause_ms: 120,
    },
    caption_settings: {
      font_file: "Montserrat-ExtraBold.ttf",
      font_name: "Montserrat ExtraBold",
      font_size: 72,
      primary_color: "&H00FFFFFF",
      outline_color: "&H00000000",
      outline_width: 4,
      margin_v: 260,
    },
    pronunciations: { Sul: "Sool" },
    camera_keyframes: [{ t: 0, x: 0.25, y: 0, zoom: 1.0, transition: "tween" }],
    segments: [
      {
        id: "seg_01",
        video_in: 0,
        video_out: 4,
        duration_mode: "preserve_video",
        source_audio_volume: 0.25,
        speed: 1.0,
        crop_keyframes: [{ t: 0, x: 0.25, y: 0, zoom: 1.0 }],
        clauses: [
          { id: "c1", display_text: "Hello", spoken_text_override: null },
        ],
      },
    ],
    ...overrides,
  };
}

test("basic undo and redo restores project, selection, and clears redo on new edit", () => {
  let state = createInitialHistoryState();
  const p0 = makeProject();
  const s0: HistorySnapshot = {
    project: p0,
    selectedSegId: "seg_01",
    selection: { type: "video", segId: "seg_01" },
  };

  const p1 = makeProject({ voice: "bf_emma" });
  const s1: HistorySnapshot = {
    project: p1,
    selectedSegId: "seg_01",
    selection: { type: "clause", segId: "seg_01", index: 0 },
  };

  state = recordHistoryUpdate(state, s0, s1, { label: "Change voice" }, 1000);
  assert.equal(state.past.length, 1);
  assert.equal(state.future.length, 0);

  const undoRes = stepUndo(state, p1);
  assert.ok(undoRes);
  assert.equal(undoRes.restored.project.voice, "af_heart");
  assert.deepEqual(undoRes.restored.selection, { type: "video", segId: "seg_01" });
  assert.equal(undoRes.state.past.length, 0);
  assert.equal(undoRes.state.future.length, 1);

  const redoRes = stepRedo(undoRes.state, undoRes.restored.project);
  assert.ok(redoRes);
  assert.equal(redoRes.restored.project.voice, "bf_emma");
  assert.deepEqual(redoRes.restored.selection, { type: "clause", segId: "seg_01", index: 0 });
  assert.equal(redoRes.state.past.length, 1);
  assert.equal(redoRes.state.future.length, 0);

  // Undo again, then make a new edit -> future should clear
  const undo2 = stepUndo(redoRes.state, redoRes.restored.project)!;
  const p2 = makeProject({ voice: "am_adam" });
  const s2: HistorySnapshot = { ...s0, project: p2 };
  const afterNewEdit = recordHistoryUpdate(undo2.state, undo2.restored, s2, { label: "Change voice" }, 2000);
  assert.equal(afterNewEdit.future.length, 0);
  assert.equal(afterNewEdit.past.length, 1);
});

test("no-op updates are ignored", () => {
  let state = createInitialHistoryState();
  const p0 = makeProject();
  const s0: HistorySnapshot = { project: p0, selectedSegId: "seg_01", selection: null };
  const s0Clone: HistorySnapshot = {
    project: makeProject({ revision: 99, updated_at: "2026-02-01T00:00:00Z" }),
    selectedSegId: "seg_01",
    selection: null,
  };

  state = recordHistoryUpdate(state, s0, s0Clone, { label: "No-op" }, 1000);
  assert.equal(state.past.length, 0);
});

test("coalescing merges rapid edits with same coalesceKey within window", () => {
  let state = createInitialHistoryState();
  const p0 = makeProject();
  const p1 = makeProject({
    segments: [{ ...p0.segments[0], clauses: [{ id: "c1", display_text: "H", spoken_text_override: null }] }],
  });
  const p2 = makeProject({
    segments: [{ ...p0.segments[0], clauses: [{ id: "c1", display_text: "Hi", spoken_text_override: null }] }],
  });
  const p3 = makeProject({
    segments: [{ ...p0.segments[0], clauses: [{ id: "c1", display_text: "Hi!", spoken_text_override: null }] }],
  });

  state = recordHistoryUpdate(
    state,
    { project: p0, selectedSegId: "seg_01", selection: null },
    { project: p1, selectedSegId: "seg_01", selection: null },
    { label: "Edit caption", coalesceKey: "caption-c1" },
    1000
  );
  state = recordHistoryUpdate(
    state,
    { project: p1, selectedSegId: "seg_01", selection: null },
    { project: p2, selectedSegId: "seg_01", selection: null },
    { label: "Edit caption", coalesceKey: "caption-c1" },
    1200
  );
  assert.equal(state.past.length, 1);
  assert.equal(state.past[0].before.project.segments[0].clauses[0].display_text, "Hello");
  assert.equal(state.past[0].after.project.segments[0].clauses[0].display_text, "Hi");

  // Beyond coalesce window -> creates a new entry
  state = recordHistoryUpdate(
    state,
    { project: p2, selectedSegId: "seg_01", selection: null },
    { project: p3, selectedSegId: "seg_01", selection: null },
    { label: "Edit caption", coalesceKey: "caption-c1" },
    1200 + COALESCE_WINDOW_MS + 100
  );
  assert.equal(state.past.length, 2);
});

test("transactions group multiple drag updates into a single undo entry", () => {
  let state = createInitialHistoryState();
  const p0 = makeProject();
  const s0: HistorySnapshot = { project: p0, selectedSegId: "seg_01", selection: null };

  state = beginHistoryTransaction(state, s0, "Move crop box");
  const p1 = makeProject({
    camera_keyframes: [{ t: 0, x: 0.3, y: 0.1, zoom: 1.0, transition: "tween" }],
  });
  state = recordHistoryUpdate(state, s0, { ...s0, project: p1 }, { label: "ignored" }, 1000);
  assert.equal(state.past.length, 0);

  const p2 = makeProject({
    camera_keyframes: [{ t: 0, x: 0.4, y: 0.2, zoom: 1.2, transition: "tween" }],
  });
  state = recordHistoryUpdate(state, { ...s0, project: p1 }, { ...s0, project: p2 }, { label: "ignored" }, 1016);
  assert.equal(state.past.length, 0);

  state = endHistoryTransaction(state, { ...s0, project: p2 }, 1050);
  assert.equal(state.past.length, 1);
  assert.equal(state.past[0].label, "Move crop box");
  assert.equal(state.past[0].before.project.camera_keyframes?.[0].x, 0.25);
  assert.equal(state.past[0].after.project.camera_keyframes?.[0].x, 0.4);
});

test("undo preserves current server revision to prevent 409 conflicts", () => {
  let state = createInitialHistoryState();
  const p0 = makeProject({ revision: 1 });
  const p1 = makeProject({ revision: 1, title: "Edited" });
  state = recordHistoryUpdate(
    state,
    { project: p0, selectedSegId: "seg_01", selection: null },
    { project: p1, selectedSegId: "seg_01", selection: null },
    { label: "Edit title" },
    1000
  );

  // Simulate save advancing server revision to 5
  const currentAfterSave = makeProject({ revision: 5, title: "Edited", updated_at: "2026-01-02T00:00:00Z" });
  const undoRes = stepUndo(state, currentAfterSave)!;
  assert.equal(undoRes.restored.project.title, "Test Project");
  assert.equal(undoRes.restored.project.revision, 5);
  assert.equal(undoRes.restored.project.updated_at, "2026-01-02T00:00:00Z");
  assert.equal(serializeProjectContent(undoRes.restored.project), serializeProjectContent(p0));
});

test("history stack respects MAX_HISTORY_ENTRIES cap", () => {
  let state = createInitialHistoryState();
  let prev = makeProject();
  for (let i = 1; i <= MAX_HISTORY_ENTRIES + 25; i++) {
    const next = makeProject({ title: `Title ${i}` });
    state = recordHistoryUpdate(
      state,
      { project: prev, selectedSegId: "seg_01", selection: null },
      { project: next, selectedSegId: "seg_01", selection: null },
      { label: `Edit ${i}` },
      1000 + i * 1000
    );
    prev = next;
  }
  assert.equal(state.past.length, MAX_HISTORY_ENTRIES);
  assert.equal(state.past[0].after.project.title, "Title 26");
});

test("multi-track undo/redo restores and sanitizes clip, transition, and track selections", () => {
  let state = createInitialHistoryState();
  const v1Base: ProjectInput["tracks"] = [
    {
      id: "v1",
      name: "Video 1",
      kind: "video",
      visible: true,
      muted: false,
      locked: false,
      camera_follow: true,
      clips: [
        {
          id: "vc_1",
          asset_id: "src_1",
          kind: "video",
          start_sec: 0,
          duration_sec: 3,
          media_in: 0,
          media_out: 3,
          speed: 1,
          end_behavior: "freeze",
          volume: 1,
          audio_fade_in_sec: 0,
          audio_fade_out_sec: 0,
          transform: { x: 0.5, y: 0.5, scale: 1, rotation_deg: 0, opacity: 1 },
          transition_in: { type: "none", duration_sec: 0 },
          transition_out: { type: "none", duration_sec: 0 },
        },
        {
          id: "vc_2",
          asset_id: "src_1",
          kind: "video",
          start_sec: 3,
          duration_sec: 2,
          media_in: 3,
          media_out: 5,
          speed: 1,
          end_behavior: "freeze",
          volume: 1,
          audio_fade_in_sec: 0,
          audio_fade_out_sec: 0,
          transform: { x: 0.5, y: 0.5, scale: 1, rotation_deg: 0, opacity: 1 },
          transition_in: { type: "none", duration_sec: 0 },
          transition_out: { type: "none", duration_sec: 0 },
        },
      ],
      transitions: [
        { id: "tr_1", from_clip_id: "vc_1", to_clip_id: "vc_2", type: "crossfade", duration_sec: 0.5 },
      ],
    },
  ];
  const p0 = makeProject({ tracks: v1Base, segments: [] });
  const p1 = makeProject({
    tracks: [
      {
        ...v1Base[0],
        clips: [v1Base[0].clips[0]],
        transitions: [],
      },
    ],
    segments: [],
  });

  state = recordHistoryUpdate(
    state,
    { project: p0, selectedSegId: null, selection: { type: "transition", trackId: "v1", transitionId: "tr_1" } },
    { project: p1, selectedSegId: null, selection: { type: "video-clip", trackId: "v1", clipId: "vc_1" } },
    { label: "Delete clip vc_2" },
    1000
  );

  const undone = stepUndo(state, p1);
  assert.ok(undone);
  assert.deepEqual(undone.restored.selection, { type: "transition", trackId: "v1", transitionId: "tr_1" });
  const vTrack = undone.restored.project.tracks?.[0];
  assert.ok(vTrack && vTrack.kind === "video");
  assert.equal(vTrack.clips.length, 2);
  assert.equal(vTrack.transitions.length, 1);

  const redone = stepRedo(undone.state, undone.restored.project);
  assert.ok(redone);
  assert.deepEqual(redone.restored.selection, { type: "video-clip", trackId: "v1", clipId: "vc_1" });
});

