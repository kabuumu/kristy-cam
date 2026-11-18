import assert from "node:assert/strict";
import test from "node:test";
import type {
  ArtifactManifest,
  AudioClip,
  CaptionClip,
  ProjectInput,
  VideoTrack,
  VisualClip,
} from "../src/types/studio.ts";
import {
  collectTimelineSnapCandidates,
  computeAdjacentJunctionPairs,
  computeAudioDuckingFactorAt,
  computeAudioFadeGainAt,
  computeCaptionClipTtsSignature,
  computeClipEdgeTransitionState,
  findActiveAudioClipsAt,
  findActiveCaptionsAt,
  findActiveVisualLayersAt,
  getProjectDuration,
  migrateProjectToMultiTrack,
  snapTimelineTime,
  splitAudioClipAt,
  splitVisualClipAt,
} from "../src/utils/studioMath.ts";

function makeVisualClip(overrides: Partial<VisualClip> = {}): VisualClip {
  return {
    id: "vclip_01",
    asset_id: "src_1",
    kind: "video",
    start_sec: 0,
    duration_sec: 4,
    media_in: 0,
    media_out: 4,
    speed: 1.0,
    end_behavior: "freeze",
    volume: 1.0,
    audio_fade_in_sec: 0,
    audio_fade_out_sec: 0,
    transform: { x: 0.5, y: 0.5, scale: 1.0, rotation_deg: 0, opacity: 1.0 },
    transition_in: { type: "none", duration_sec: 0 },
    transition_out: { type: "none", duration_sec: 0 },
    ...overrides,
  };
}

function makeCaptionClip(overrides: Partial<CaptionClip> = {}): CaptionClip {
  return {
    id: "cap_01",
    start_sec: 0,
    duration_sec: 2.0,
    auto_fit_tts: false,
    display_text: "Hello Sul world",
    spoken_text_override: null,
    tts_enabled: true,
    speed: 1.0,
    voice_override: null,
    style_override: null,
    ...overrides,
  };
}

function makeAudioClip(overrides: Partial<AudioClip> = {}): AudioClip {
  return {
    id: "aclip_01",
    asset_id: "aud_1",
    start_sec: 0,
    duration_sec: 5.0,
    media_in: 0,
    volume: 0.8,
    speed: 1.0,
    fade_in_sec: 0,
    fade_out_sec: 0,
    loop: false,
    ...overrides,
  };
}

function makeBaseProject(overrides: Partial<ProjectInput> = {}): ProjectInput {
  return {
    schema_version: 1,
    project_id: "proj_mt",
    revision: 1,
    title: "MultiTrack Test",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    source_media: {
      media_id: "src_1",
      path: "/tmp/video.mp4",
      fingerprint: "fp_1",
      copied_to_project: false,
    },
    media_assets: {},
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
    tracks: [],
    segments: [],
    ...overrides,
  };
}

const emptyManifest: ArtifactManifest = {
  project_id: "proj_mt",
  source_metadata: {
    width: 1920,
    height: 1080,
    fps: 30,
    duration: 20,
    has_audio: true,
  },
  proxy: { status: "ready", artifact_id: "art_proxy", path: "proxy.mp4", error: null },
  media_metadata: {},
  media_proxies: {},
  caption_artifacts: {},
  segments: {},
  latest_preview_artifact_id: null,
  latest_preview_path: null,
  latest_export_artifact_id: null,
  latest_export_path: null,
};

test("migrateProjectToMultiTrack decouples legacy segments into independent tracks & registers primary asset", () => {
  const legacy = makeBaseProject({
    camera_keyframes: [],
    segments: [
      {
        id: "seg_01",
        video_in: 1.0,
        video_out: 4.0,
        duration_mode: "preserve_video",
        source_audio_volume: 0.4,
        speed: 1.0,
        crop_keyframes: [{ t: 0, x: 0.15, y: 0.1, zoom: 1.2 }],
        clauses: [{ id: "c1", display_text: "First line", spoken_text_override: null }],
      },
      {
        id: "seg_02",
        video_in: 5.0,
        video_out: 8.0,
        duration_mode: "preserve_video",
        source_audio_volume: 0.5,
        speed: 1.1,
        crop_keyframes: [{ t: 0, x: 0.35, y: 0.0, zoom: 1.0 }],
        clauses: [{ id: "c1", display_text: "Second line", spoken_text_override: null }],
      },
    ],
  });

  const migrated = migrateProjectToMultiTrack(legacy, emptyManifest);
  assert.ok(migrated.media_assets?.["src_1"]);
  assert.equal(migrated.media_assets?.["src_1"]?.kind, "video");
  const v1 = migrated.tracks?.find((t) => t.kind === "video");
  const t1 = migrated.tracks?.find((t) => t.kind === "caption");
  assert.ok(v1 && v1.kind === "video");
  assert.ok(t1 && t1.kind === "caption");
  assert.equal(v1.clips.length, 2);
  assert.equal(v1.clips[0].start_sec, 0);
  assert.equal(v1.clips[1].start_sec, 3);
  assert.equal(t1.clips.length, 2);
  assert.equal(t1.clips[0].display_text, "First line");
  assert.equal(t1.clips[1].start_sec, 3);
  assert.equal(migrated.camera_keyframes?.length, 2);
  assert.equal(migrated.camera_keyframes?.[1]?.t, 3);
});

test("getProjectDuration supports 0-video-clip timelines, gaps, captions, and camera keyframes", () => {
  const zeroVideoProj = makeBaseProject({
    camera_keyframes: [
      { t: 0, x: 0.2, y: 0, zoom: 1.0 },
      { t: 6.5, x: 0.4, y: 0, zoom: 1.4 },
    ],
    tracks: [
      {
        id: "v1",
        name: "Video 1",
        kind: "video",
        visible: true,
        muted: false,
        locked: false,
        camera_follow: true,
        clips: [],
        transitions: [],
      },
      {
        id: "t1",
        name: "Captions 1",
        kind: "caption",
        visible: true,
        muted: false,
        locked: false,
        clips: [makeCaptionClip({ start_sec: 1.5, duration_sec: 3.0 })],
      },
    ],
  });
  assert.equal(getProjectDuration(zeroVideoProj, emptyManifest, 0), 6.5);

  const gappedVideoProj = makeBaseProject({
    camera_keyframes: [{ t: 0, x: 0.2, y: 0, zoom: 1.0 }],
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
          makeVisualClip({ id: "c1", start_sec: 0, duration_sec: 2 }),
          makeVisualClip({ id: "c2", start_sec: 5, duration_sec: 3 }),
        ],
        transitions: [],
      },
    ],
  });
  assert.equal(getProjectDuration(gappedVideoProj, emptyManifest, 0), 8);
  assert.equal(findActiveVisualLayersAt(gappedVideoProj.tracks!, 3.5).length, 0);
  assert.equal(findActiveVisualLayersAt(gappedVideoProj.tracks!, 6.0).length, 1);
});

test("computeClipEdgeTransitionState computes fade, slide, wipe, and zoom progress", () => {
  const clip = makeVisualClip({
    duration_sec: 4,
    transition_in: { type: "fade", duration_sec: 1 },
    transition_out: { type: "slide_left", duration_sec: 1 },
  });
  const atStart = computeClipEdgeTransitionState(clip, 0.5);
  assert.equal(atStart.opacityMultiplier, 0.5);
  assert.equal(atStart.offsetXPct, 0);

  const atEnd = computeClipEdgeTransitionState(clip, 3.5);
  assert.equal(atEnd.opacityMultiplier, 1);
  assert.equal(atEnd.offsetXPct, -50);

  const wipeClip = makeVisualClip({
    duration_sec: 2,
    transition_in: { type: "wipe_left", duration_sec: 1 },
  });
  assert.equal(computeClipEdgeTransitionState(wipeClip, 0.25).clipPathCss, "inset(0 75.00% 0 0)");
});

test("computeAdjacentJunctionPairs finds touching/overlapping pairs and preserves transitions", () => {
  const track: VideoTrack = {
    id: "v1",
    name: "Video 1",
    kind: "video",
    visible: true,
    muted: false,
    locked: false,
    camera_follow: true,
    clips: [
      makeVisualClip({ id: "a", start_sec: 0, duration_sec: 3 }),
      makeVisualClip({ id: "b", start_sec: 3.05, duration_sec: 2 }),
      makeVisualClip({ id: "c", start_sec: 7.0, duration_sec: 2 }),
    ],
    transitions: [
      { id: "tr1", from_clip_id: "a", to_clip_id: "b", type: "crossfade", duration_sec: 0.5 },
    ],
  };
  const pairs = computeAdjacentJunctionPairs(track);
  assert.equal(pairs.length, 1);
  assert.equal(pairs[0].fromClip.id, "a");
  assert.equal(pairs[0].toClip.id, "b");
  assert.equal(pairs[0].existing?.type, "crossfade");
});

test("computeCaptionClipTtsSignature stays stable across position moves and duration resizes", () => {
  const c1 = makeCaptionClip({ start_sec: 1.0, duration_sec: 2.0 });
  const c2 = makeCaptionClip({ start_sec: 8.5, duration_sec: 4.5 });
  const sig1 = computeCaptionClipTtsSignature(c1, { Sul: "Sool" }, "af_heart", "en-us");
  const sig2 = computeCaptionClipTtsSignature(c2, { Sul: "Sool" }, "af_heart", "en-us");
  assert.equal(sig1, sig2);

  const cChangedText = makeCaptionClip({ display_text: "Goodbye Sul world" });
  const sig3 = computeCaptionClipTtsSignature(cChangedText, { Sul: "Sool" }, "af_heart", "en-us");
  assert.notEqual(sig1, sig3);
});

test("splitVisualClipAt and splitAudioClipAt split timing and media offsets cleanly", () => {
  const vClip = makeVisualClip({ start_sec: 2.0, duration_sec: 4.0, media_in: 1.0, media_out: 5.0, speed: 1.0 });
  const vSplit = splitVisualClipAt(vClip, 3.5, "vclip_02");
  assert.ok(vSplit);
  assert.equal(vSplit[0].duration_sec, 1.5);
  assert.equal(vSplit[0].media_out, 2.5);
  assert.equal(vSplit[1].start_sec, 3.5);
  assert.equal(vSplit[1].duration_sec, 2.5);
  assert.equal(vSplit[1].media_in, 2.5);

  const aClip = makeAudioClip({ start_sec: 1.0, duration_sec: 4.0, media_in: 0.5, speed: 1.5 });
  const aSplit = splitAudioClipAt(aClip, 3.0, "aclip_02");
  assert.ok(aSplit);
  assert.equal(aSplit[0].duration_sec, 2.0);
  assert.equal(aSplit[1].start_sec, 3.0);
  assert.equal(aSplit[1].media_in, 3.5);
});

test("snapTimelineTime and audio ducking / fades / solo behave consistently", () => {
  const proj = makeBaseProject({
    camera_keyframes: [{ t: 2.5, x: 0, y: 0, zoom: 1 }],
    tracks: [
      {
        id: "t1",
        name: "Captions 1",
        kind: "caption",
        visible: true,
        muted: false,
        locked: false,
        clips: [makeCaptionClip({ start_sec: 1.0, duration_sec: 2.0, tts_enabled: true })],
      },
      {
        id: "a1",
        name: "Music",
        kind: "audio",
        volume: 1.0,
        muted: false,
        solo: false,
        duck_under_narration: true,
        locked: false,
        clips: [makeAudioClip({ id: "a1_c1", start_sec: 0, duration_sec: 6, fade_in_sec: 1.0 })],
      },
      {
        id: "a2",
        name: "SFX",
        kind: "audio",
        volume: 1.0,
        muted: false,
        solo: true,
        duck_under_narration: false,
        locked: false,
        clips: [makeAudioClip({ id: "a2_c1", start_sec: 0, duration_sec: 6 })],
      },
    ],
  });

  const cands = collectTimelineSnapCandidates(proj, emptyManifest);
  assert.equal(snapTimelineTime(2.44, cands, 0.1), 2.5);
  assert.equal(snapTimelineTime(2.75, cands, 0.1), 2.75);

  assert.equal(computeAudioDuckingFactorAt(proj.tracks!, emptyManifest, 1.5), 0.28);
  assert.equal(computeAudioDuckingFactorAt(proj.tracks!, emptyManifest, 4.5), 1.0);
  assert.equal(computeAudioFadeGainAt(5.0, 2.0, 1.0, 1.0), 0.5);

  const activeAud = findActiveAudioClipsAt(proj.tracks!, 0.5);
  assert.equal(activeAud.length, 1);
  assert.equal(activeAud[0].clip.id, "a2_c1");

  assert.equal(findActiveCaptionsAt(proj.tracks!, emptyManifest, 1.5).length, 1);
  assert.equal(findActiveCaptionsAt(proj.tracks!, emptyManifest, 3.5).length, 0);
});
