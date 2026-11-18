"""Tier 5: Decoupled Multi-Track NLE End-to-End Tests (API, FFmpeg Compositor, and Chromium UI)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from e2e.conftest import StudioClient, StudioServer
from e2e.fixtures.media_gen import (
    _run_ffmpeg,
    generate_sdr_cfr_mp4,
    probe_media,
)


def _generate_rgba_badge_png(
    path: Path,
    *,
    width: int = 320,
    height: int = 320,
) -> Path:
    """Create a real RGBA PNG overlay (red center badge surrounded by transparent pixels)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        "format=rgba,"
        f"geq=r='if(between(X,48,{width - 48})*between(Y,48,{height - 48}),255,0)':"
        "g='0':b='0':"
        f"a='if(between(X,48,{width - 48})*between(Y,48,{height - 48}),255,0)'"
    )
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=black@0.0:s={width}x{height}:duration=0.1",
            "-vf",
            vf,
            "-frames:v",
            "1",
            str(out),
        ]
    )
    return out


def _generate_wav_tone(
    path: Path,
    *,
    duration: float = 0.9,
    freq: int = 330,
) -> Path:
    """Create a 48kHz stereo PCM WAV fixture for audio track E2E tests."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate=48000:duration={duration}",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(out),
        ]
    )
    return out


def _create_project(client: StudioClient, media_path: Path, title: str) -> tuple[str, dict[str, Any]]:
    sel = client.select_media(media_path)
    assert sel.status == 200, sel.text
    created = client.post(
        "/api/projects",
        json_body={"media_id": sel.json()["media_id"], "title": title},
    )
    assert created.status in (200, 201, 202), created.text
    payload = created.json()
    pid = payload.get("project_id") or payload["project"]["project_id"]
    if "job_id" in payload:
        assert client.wait_for_job(payload["job_id"])["status"] == "succeeded"
    return pid, client.get(f"/api/projects/{pid}").json()


def _assert_v1_baseline_export(export_path: Path, *, min_duration: float = 1.0) -> dict[str, Any]:
    """Verify baseline export output contract: 1080x1920 SAR 1:1 30fps yuv420p H.264, 48kHz stereo AAC, +faststart."""
    assert export_path.is_file() and export_path.stat().st_size > 10_000
    info = probe_media(export_path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")

    assert v["width"] == 1080
    assert v["height"] == 1920
    assert v.get("sample_aspect_ratio", "1:1") == "1:1"
    assert v["codec_name"] == "h264"
    assert v["pix_fmt"] == "yuv420p"
    assert v["r_frame_rate"] == "30/1"
    assert v["avg_frame_rate"] == "30/1"

    assert a["codec_name"] == "aac"
    assert a["sample_rate"] == "48000"
    assert int(a["channels"]) == 2
    assert float(v["duration"]) >= min_duration
    assert abs(float(v["duration"]) - float(a["duration"])) <= (1.0 / 30.0) + 0.03

    head = export_path.read_bytes()[:65536]
    moov_idx = head.find(b"moov")
    mdat_idx = head.find(b"mdat")
    assert moov_idx != -1, "moov atom missing in first 64KB (+faststart required)"
    if mdat_idx != -1:
        assert moov_idx < mdat_idx, "moov must precede mdat for +faststart"
    return info


def test_tier5_multitrack_assets_gaps_overlay_xfade_audio_preview_export(
    studio_server: StudioServer, tmp_path: Path
) -> None:
    """Import PNG + WAV assets, build gapped/layered multi-track timeline, preview, export, and guard deletions."""
    client = studio_server.client
    src_mp4 = generate_sdr_cfr_mp4(tmp_path / "gameplay_main.mp4", 1920, 1080, 30, 4.0, True)
    badge_png = _generate_rgba_badge_png(tmp_path / "hud_badge.png", width=320, height=320)
    bgm_wav = _generate_wav_tone(tmp_path / "bgm_short.wav", duration=0.9, freq=330)

    pid, bundle = _create_project(client, src_mp4, "Tier 5 Multi-Track Pipeline")
    p = bundle["project"]

    # 1. Import RGBA image & WAV audio into Project Media Bin
    r_img = client.post(
        f"/api/projects/{pid}/assets",
        json_body={"path": str(badge_png), "kind": "image", "name": "HUD Badge", "copy_to_project": True},
    )
    assert r_img.status == 201, r_img.text
    img_payload = r_img.json()
    img_id = img_payload["asset"]["asset_id"]
    assert img_payload["metadata"]["kind"] == "image"
    assert img_payload["metadata"]["has_alpha"] is True
    assert img_id in img_payload["project"]["media_assets"]

    r_aud = client.post(
        f"/api/projects/{pid}/assets",
        json_body={"path": str(bgm_wav), "kind": "audio", "name": "BGM Loop"},
    )
    assert r_aud.status == 201, r_aud.text
    aud_payload = r_aud.json()
    aud_id = aud_payload["asset"]["asset_id"]
    assert aud_payload["metadata"]["kind"] == "audio"
    assert aud_payload["metadata"]["has_audio"] is True

    # HTTP 200 full + 206 Byte-Range streaming for registered project assets
    r_img_get = client.get(f"/api/projects/{pid}/media/{img_id}")
    assert r_img_get.status == 200
    assert r_img_get.headers.get("content-type", "").startswith("image/png")

    r_aud_range = client.get(
        f"/api/projects/{pid}/media/{aud_id}",
        headers={"Range": "bytes=0-127"},
    )
    assert r_aud_range.status == 206
    assert len(r_aud_range.body) == 128

    # Primary source asset cannot be deleted (409)
    primary_id = p["source_media"]["media_id"]
    r_del_primary = client.request("DELETE", f"/api/projects/{pid}/assets/{primary_id}")
    assert r_del_primary.status == 409

    # 2. Save multi-track timeline with:
    # - Global continuous camera_keyframes panning across an explicit black gap
    # - V1 (camera_follow=True): c1 + c2 joined by crossfade TrackTransition, gap 2.0..2.8s, c3
    # - V2 (camera_follow=False): RGBA PNG overlay spanning 1.8..3.1s (across the gap) with edge transitions
    # - T1 (CaptionTrack): visual-only caption placed directly in the gap (tts_enabled=False for offline VM)
    # - A1 (AudioTrack): looping short WAV with fade in/out
    proj_latest = aud_payload["project"]
    proj_latest["segments"] = []
    proj_latest["camera_keyframes"] = [
        {"t": 0.0, "x": 0.1, "y": 0.0, "zoom": 1.0, "transition": "tween"},
        {"t": 3.5, "x": 0.35, "y": 0.08, "zoom": 1.35, "transition": "tween"},
    ]
    proj_latest["tracks"] = [
        {
            "id": "v1",
            "name": "Video 1",
            "kind": "video",
            "visible": True,
            "muted": False,
            "locked": False,
            "camera_follow": True,
            "clips": [
                {
                    "id": "vc_1",
                    "asset_id": primary_id,
                    "kind": "video",
                    "start_sec": 0.0,
                    "duration_sec": 1.2,
                    "media_in": 0.0,
                    "media_out": 1.2,
                    "speed": 1.0,
                },
                {
                    "id": "vc_2",
                    "asset_id": primary_id,
                    "kind": "video",
                    "start_sec": 1.2,
                    "duration_sec": 1.2,
                    "media_in": 1.2,
                    "media_out": 2.4,
                    "speed": 1.0,
                },
                # Explicit gap between ~2.0s (after 0.4s crossfade overlap) and 2.8s
                {
                    "id": "vc_3",
                    "asset_id": primary_id,
                    "kind": "video",
                    "start_sec": 2.8,
                    "duration_sec": 0.8,
                    "media_in": 2.4,
                    "media_out": 3.2,
                    "speed": 1.0,
                },
            ],
            "transitions": [
                {
                    "id": "tr_12",
                    "from_clip_id": "vc_1",
                    "to_clip_id": "vc_2",
                    "type": "crossfade",
                    "duration_sec": 0.4,
                }
            ],
        },
        {
            "id": "v2",
            "name": "Overlay 1",
            "kind": "video",
            "visible": True,
            "muted": False,
            "locked": False,
            "camera_follow": False,
            "clips": [
                {
                    "id": "ov_1",
                    "asset_id": img_id,
                    "kind": "image",
                    "start_sec": 1.8,
                    "duration_sec": 1.3,
                    "transform": {
                        "x": 0.5,
                        "y": 0.45,
                        "scale": 0.65,
                        "rotation_deg": 0.0,
                        "opacity": 0.9,
                    },
                    "transition_in": {"type": "slide_left", "duration_sec": 0.25},
                    "transition_out": {"type": "fade", "duration_sec": 0.25},
                }
            ],
            "transitions": [],
        },
        {
            "id": "t1",
            "name": "Captions 1",
            "kind": "caption",
            "visible": True,
            "muted": False,
            "locked": False,
            "clips": [
                {
                    "id": "cap_gap",
                    "start_sec": 2.05,
                    "duration_sec": 0.7,
                    "auto_fit_tts": False,
                    "display_text": "Bridge across the gap!",
                    "tts_enabled": False,
                    "style_override": {"align": "center", "font_size": 72},
                }
            ],
        },
        {
            "id": "a1",
            "name": "BGM Track",
            "kind": "audio",
            "volume": 0.8,
            "muted": False,
            "solo": False,
            "duck_under_narration": True,
            "locked": False,
            "clips": [
                {
                    "id": "ac_bgm",
                    "asset_id": aud_id,
                    "start_sec": 0.2,
                    "duration_sec": 3.0,
                    "loop": True,
                    "fade_in_sec": 0.2,
                    "fade_out_sec": 0.3,
                }
            ],
        },
    ]

    put_resp = client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": proj_latest["revision"], "project": proj_latest},
    )
    assert put_resp.status == 200, put_resp.text
    rev_saved = put_resp.json()["project"]

    # Deleting referenced image or audio asset is guarded with HTTP 409
    assert client.request("DELETE", f"/api/projects/{pid}/assets/{img_id}").status == 409
    assert client.request("DELETE", f"/api/projects/{pid}/assets/{aud_id}").status == 409

    # 3. Render Full-Project Preview & Verify Stream
    prev_req = client.post(f"/api/projects/{pid}/preview")
    assert prev_req.status == 202, prev_req.text
    prev_job = client.wait_for_job(prev_req.json()["job_id"])
    assert prev_job["status"] == "succeeded", prev_job

    manifest_after_prev = client.get(f"/api/projects/{pid}").json()["manifest"]
    assert manifest_after_prev["latest_preview_artifact_id"] == "art_preview_latest"
    prev_range = client.get(
        f"/api/projects/{pid}/media/art_preview_latest",
        headers={"Range": "bytes=0-1023"},
    )
    assert prev_range.status == 206
    assert len(prev_range.body) == 1024

    # 4. Export Final Multi-Track MP4 & Probe
    exp_req = client.post(f"/api/projects/{pid}/export")
    assert exp_req.status == 202, exp_req.text
    exp_job = client.wait_for_job(exp_req.json()["job_id"])
    assert exp_job["status"] == "succeeded", exp_job

    manifest_after_exp = client.get(f"/api/projects/{pid}").json()["manifest"]
    assert manifest_after_exp["latest_export_artifact_id"] == "art_export_latest"
    out_path = studio_server.data_dir / "projects" / pid / manifest_after_exp["latest_export_path"]
    _assert_v1_baseline_export(out_path, min_duration=3.4)

    # 5. Remove A1 clip reference -> deleting aud_id succeeds with HTTP 200
    for tr in rev_saved["tracks"]:
        if tr["id"] == "a1":
            tr["clips"] = []
    put_clear = client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": rev_saved["revision"], "project": rev_saved},
    )
    assert put_clear.status == 200, put_clear.text

    del_aud = client.request("DELETE", f"/api/projects/{pid}/assets/{aud_id}")
    assert del_aud.status == 200, del_aud.text
    assert aud_id not in del_aud.json()["project"]["media_assets"]


def test_tier5_zero_video_clips_captions_and_camera_export(
    studio_server: StudioServer, sample_1080p_mp4: Path
) -> None:
    """Export succeeds on a timeline with 0 video clips when independent captions/camera keyframes exist."""
    client = studio_server.client
    pid, bundle = _create_project(client, sample_1080p_mp4, "Zero Video Timeline")
    p = bundle["project"]
    p["segments"] = []
    p["camera_keyframes"] = [
        {"t": 0.0, "x": 0.15, "y": 0.0, "zoom": 1.0, "transition": "tween"},
        {"t": 2.5, "x": 0.35, "y": 0.1, "zoom": 1.25, "transition": "tween"},
    ]
    p["tracks"] = [
        {
            "id": "v1",
            "name": "Video 1",
            "kind": "video",
            "visible": True,
            "muted": False,
            "locked": False,
            "camera_follow": True,
            "clips": [],
            "transitions": [],
        },
        {
            "id": "t1",
            "name": "Captions 1",
            "kind": "caption",
            "visible": True,
            "muted": False,
            "locked": False,
            "clips": [
                {
                    "id": "cap_solo",
                    "start_sec": 0.4,
                    "duration_sec": 1.6,
                    "auto_fit_tts": False,
                    "display_text": "Independent caption with zero video!",
                    "tts_enabled": False,
                }
            ],
        },
    ]

    put_resp = client.put(
        f"/api/projects/{pid}",
        json_body={"expected_revision": p["revision"], "project": p},
    )
    assert put_resp.status == 200, put_resp.text

    exp_req = client.post(f"/api/projects/{pid}/export")
    assert exp_req.status == 202, exp_req.text
    exp_job = client.wait_for_job(exp_req.json()["job_id"])
    assert exp_job["status"] == "succeeded", exp_job

    manifest = client.get(f"/api/projects/{pid}").json()["manifest"]
    out_path = studio_server.data_dir / "projects" / pid / manifest["latest_export_path"]
    info = _assert_v1_baseline_export(out_path, min_duration=2.3)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert 2.3 <= float(v["duration"]) <= 2.7


def test_tier5_playwright_multitrack_ui_workflow(
    studio_client: StudioClient,
    sample_1080p_mp4: Path,
    tmp_path: Path,
    browser_page: Any,
) -> None:
    """Exercise full multi-track Studio UI in headless Chromium and assert persistence + zero runtime errors."""
    page_errors: list[str] = []
    console_errors: list[str] = []
    browser_page.on("pageerror", lambda err: page_errors.append(str(err)))
    browser_page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )

    badge_png = _generate_rgba_badge_png(tmp_path / "ui_overlay.png", width=240, height=240)
    tone_wav = _generate_wav_tone(tmp_path / "ui_audio.wav", duration=1.5, freq=440)

    pid, _bundle = _create_project(studio_client, sample_1080p_mp4, "Playwright Multi-Track UI")

    # Import WAV via API so Media Bin displays both audio and UI-imported RGBA PNG
    r_wav = studio_client.post(
        f"/api/projects/{pid}/assets",
        json_body={"path": str(tone_wav), "kind": "audio", "name": "UI Tone WAV"},
    )
    assert r_wav.status == 201, r_wav.text

    browser_page.goto(f"{studio_client.base_url}/?project={pid}", wait_until="networkidle")

    media_bin = browser_page.locator("[data-testid='media-bin']")
    timeline = browser_page.locator("[data-testid='timeline-panel']")
    program_canvas = browser_page.locator("[data-testid='program-canvas']")

    media_bin.wait_for(state="visible")
    timeline.wait_for(state="visible")
    assert "UI Tone WAV" in media_bin.inner_text()

    # Import RGBA PNG through the Media Bin Path form in the UI
    media_bin.get_by_role("button", name="Path", exact=True).click()
    path_input = media_bin.locator("input[placeholder*='media']")
    path_input.fill(str(badge_png))
    media_bin.get_by_role("button", name="Add", exact=True).click()
    browser_page.wait_for_function(
        "() => document.querySelector('[data-testid=\"media-bin\"]')?.innerText.includes('ui_overlay.png')"
    )

    # Create additional Video, Caption, and Audio tracks via +V, +T, +A buttons
    assert timeline.locator("[data-track-kind='video']").count() == 1
    assert timeline.locator("[data-track-kind='caption']").count() == 1
    assert timeline.locator("[data-track-kind='audio']").count() == 1

    timeline.get_by_role("button", name="+V", exact=True).click()
    timeline.get_by_role("button", name="+T", exact=True).click()
    timeline.get_by_role("button", name="+A", exact=True).click()

    assert timeline.locator("[data-track-kind='video']").count() == 2
    assert timeline.locator("[data-track-kind='caption']").count() == 2
    assert timeline.locator("[data-track-kind='audio']").count() == 2

    # Add an independent Caption / VO block, switch it to visual-only text, and verify live compositor preview
    timeline.get_by_role("button", name="Caption / VO", exact=True).click()
    speak_checkbox = browser_page.locator("[aria-label='Speak Voiceover']")
    speak_checkbox.wait_for(state="visible")
    if speak_checkbox.is_checked():
        speak_checkbox.uncheck()

    cap_textarea = browser_page.locator("textarea[placeholder='On-screen caption...']")
    cap_textarea.fill("HUD Layer Active")
    assert "HUD LAYER ACTIVE" in program_canvas.inner_text().upper()

    # Switch to Camera Frame mode, verify interactive crop box & add a global keyframe, then return to Output view
    browser_page.get_by_role("button", name="Camera Frame", exact=True).click()
    browser_page.locator("[data-testid='camera-crop-box']").wait_for(state="visible")
    browser_page.locator("button:has-text('Add Keyframe @')").click()
    browser_page.locator("button:has-text('Output ')").click()
    program_canvas.wait_for(state="visible")

    # Add the imported RGBA PNG overlay onto the timeline from Media Bin and verify stage overlay renders
    overlay_card = media_bin.locator("div.group", has_text="ui_overlay.png").first
    overlay_card.get_by_role("button", name="Add to Timeline").click()
    browser_page.locator("[data-testid^='stage-overlay-']").first.wait_for(state="visible")

    # Verify Undo/Redo restores overlay placement cleanly
    browser_page.locator("button[aria-label='Undo']").click()
    assert browser_page.locator("[data-testid^='stage-overlay-']").count() == 0
    browser_page.locator("button[aria-label='Redo']").click()
    browser_page.locator("[data-testid^='stage-overlay-']").first.wait_for(state="visible")

    # Save edits and verify clean reload persistence
    rev_before = browser_page.locator("header").inner_text()
    browser_page.get_by_role("button", name="Save", exact=True).click()
    browser_page.wait_for_function(
        "(prev) => { const h = document.querySelector('header')?.innerText || ''; return h !== prev && !h.includes('unsaved'); }",
        arg=rev_before,
    )

    browser_page.reload(wait_until="networkidle")
    timeline.wait_for(state="visible")
    assert timeline.locator("[data-track-kind='video']").count() == 2
    assert timeline.locator("[data-track-kind='caption']").count() == 2
    assert timeline.locator("[data-track-kind='audio']").count() == 2
    assert "HUD LAYER ACTIVE" in browser_page.locator("body").inner_text().upper()
    browser_page.locator("[data-testid^='stage-overlay-']").first.wait_for(state="visible")

    assert not page_errors, f"Unexpected browser page errors: {page_errors}"
    assert not console_errors, f"Unexpected browser console errors: {console_errors}"
