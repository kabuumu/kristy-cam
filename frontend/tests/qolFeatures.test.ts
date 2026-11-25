import assert from "node:assert/strict";
import test from "node:test";
import type { ProjectInput, VideoTrack, VisualClip } from "../src/types/studio.ts";
import {
  CAPTION_STYLE_PRESETS,
  applyKeyframeEasing,
  assColorToHex,
  closeGapOnTrack,
  evaluateCropAt,
  findCutPoints,
  hexToAssColor,
  parseBulkScriptToCaptionClips,
  rippleDeleteClip,
} from "../src/utils/studioMath.ts";

function makeClip(id: string, start: number, dur: number): VisualClip {
  return {
    id,
    asset_id: "src_1",
    kind: "video",
    start_sec: start,
    duration_sec: dur,
    media_in: 0,
    media_out: dur,
    speed: 1.0,
    end_behavior: "freeze",
    volume: 1.0,
    audio_fade_in_sec: 0,
    audio_fade_out_sec: 0,
    transform: { x: 0.5, y: 0.5, scale: 1.0, rotation_deg: 0, opacity: 1.0 },
    transition_in: { type: "none", duration_sec: 0 },
    transition_out: { type: "none", duration_sec: 0 },
  };
}

function makeProject(): ProjectInput {
  return {
    schema_version: 1,
    project_id: "proj_qol",
    revision: 1,
    title: "QOL Test",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    source_media: {
      media_id: "src_1",
      path: "/tmp/src.mp4",
      fingerprint: "fp",
      copied_to_project: false,
    },
    voice: "bf_emma",
    language: "en-gb",
    output_settings: {
      width: 1080,
      height: 1920,
      fps: 30,
      default_source_volume: 0.15,
      tail_padding_sec: 0.4,
      inter_clause_pause_ms: 120,
    },
    caption_settings: {
      font_file: "Montserrat-ExtraBold.ttf",
      font_name: "Montserrat ExtraBold",
      font_size: 64,
      primary_color: "&H00FFFFFF",
      outline_color: "&H00000000",
      outline_width: 5,
      margin_v: 340,
    },
    pronunciations: {},
    camera_keyframes: [{ t: 0, x: 0.2, y: 0, zoom: 1.0, transition: "tween" }],
    markers: [{ id: "m1", t: 2.5, label: "Beat", color: "#f59e0b" }],
    tracks: [
      {
        id: "v1",
        name: "Video 1",
        kind: "video",
        visible: true,
        muted: false,
        locked: false,
        camera_follow: true,
        clips: [
          makeClip("v1_c1", 0, 2),
          makeClip("v1_c2", 2, 3),
          makeClip("v1_c3", 7, 2),
        ],
        transitions: [
          {
            id: "tr_1",
            from_clip_id: "v1_c1",
            to_clip_id: "v1_c2",
            type: "crossfade",
            duration_sec: 0.5,
          },
        ],
      },
    ],
    segments: [],
  };
}

test("applyKeyframeEasing and evaluateCropAt compute ease_in, ease_out, and ease_in_out curves", () => {
  assert.equal(applyKeyframeEasing(0.5, "tween"), 0.5);
  assert.equal(applyKeyframeEasing(0.5, "ease_in"), 0.25);
  assert.equal(applyKeyframeEasing(0.5, "ease_out"), 0.75);
  assert.equal(applyKeyframeEasing(0.5, "ease_in_out"), 0.5);
  assert.ok(applyKeyframeEasing(0.25, "ease_in_out") < 0.25);
  assert.ok(applyKeyframeEasing(0.75, "ease_in_out") > 0.75);

  const kfs = [
    { t: 0, x: 0.0, y: 0.0, zoom: 1.0, transition: "ease_in" as const },
    { t: 2, x: 0.2, y: 0.0, zoom: 2.0, transition: "tween" as const },
  ];
  const mid = evaluateCropAt(kfs, 1.0, 1920, 1080, 1080, 1920);
  assert.ok(Math.abs(mid.zoom - 1.25) < 1e-6);
});

test("rippleDeleteClip removes target clip, shifts downstream clips, and cleans transitions", () => {
  const proj = makeProject();
  const next = rippleDeleteClip(proj.tracks!, "v1", "v1_c2");
  const v1 = next[0] as VideoTrack;
  assert.deepEqual(
    v1.clips.map((c) => [c.id, c.start_sec]),
    [
      ["v1_c1", 0],
      ["v1_c3", 4],
    ]
  );
  assert.equal(v1.transitions.length, 0);
});

test("closeGapOnTrack shifts clips after the gap left to close empty space", () => {
  const proj = makeProject();
  // Gap exists between v1_c2 (end=5) and v1_c3 (start=7)
  const next = closeGapOnTrack(proj.tracks!, "v1", 6.0);
  const v1 = next[0] as VideoTrack;
  assert.deepEqual(
    v1.clips.map((c) => [c.id, c.start_sec]),
    [
      ["v1_c1", 0],
      ["v1_c2", 2],
      ["v1_c3", 5],
    ]
  );
});

test("findCutPoints collects clip edges, keyframes, and markers in sorted order", () => {
  const proj = makeProject();
  const pts = findCutPoints(proj, null);
  assert.ok(pts.includes(0));
  assert.ok(pts.includes(2));
  assert.ok(pts.includes(2.5)); // marker
  assert.ok(pts.includes(5));
  assert.ok(pts.includes(7));
  assert.ok(pts.includes(9));
});

test("parseBulkScriptToCaptionClips splits full script into sequential non-overlapping captions", () => {
  const proj = makeProject();
  const clips = parseBulkScriptToCaptionClips(
    "Hello world! My Sim just set the stove on fire. Send help immediately.",
    1.0,
    proj,
    { ttsEnabled: true, speed: 1.0 }
  );
  assert.ok(clips.length >= 3);
  assert.equal(clips[0].start_sec, 1.0);
  for (let i = 1; i < clips.length; i++) {
    assert.ok(clips[i].start_sec > clips[i - 1].start_sec);
    assert.notEqual(clips[i].id, clips[i - 1].id);
  }
});

test("assColorToHex and hexToAssColor round-trip cleanly and presets exist", () => {
  assert.equal(assColorToHex("&H0000E5FF"), "#ffe500");
  assert.equal(hexToAssColor("#ffe500"), "&H0000E5FF");
  assert.equal(hexToAssColor("#112233", "40"), "&H40332211");
  assert.ok(CAPTION_STYLE_PRESETS.length >= 4);
});
