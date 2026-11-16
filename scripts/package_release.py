"""Release packaging, manifest verification, and end-to-end export benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.bootstrap import get_data_dir, verify_ffmpeg, verify_font  # noqa: E402
from backend.media import probe_and_validate_media  # noqa: E402
from backend.renderer import plan_segment_duration, render_final_export  # noqa: E402
from backend.schemas import (  # noqa: E402
    ArtifactManifest,
    ClauseInput,
    CropKeyframe,
    ProjectInput,
    SegmentArtifactState,
    SegmentInput,
    SourceMediaRef,
)
from backend.storage import compute_file_fingerprint  # noqa: E402
from backend.tts import get_kokoro, synthesize_segment_tts  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "dist" / "sims-vertical-studio-v1.0.0.zip"
EXCLUDED_DIR_NAMES = {"__pycache__", ".pytest_cache", "node_modules"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".pyd"}
EXCLUDED_FILE_NAMES = {".DS_Store"}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _run_checked(cmd: list[str], cwd: Path, label: str) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(
        cmd,
        cwd=str(cwd),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"{label} failed (exit {res.returncode}):\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    return res


def _build_frontend(skip_frontend_build: bool) -> None:
    frontend_dir = REPO_ROOT / "frontend"
    dist_index = frontend_dir / "dist" / "index.html"

    if skip_frontend_build:
        if not dist_index.is_file():
            raise RuntimeError(
                f"--skip-frontend-build specified, but {dist_index} does not exist."
            )
        return

    npm_bin = shutil.which("npm")
    if not npm_bin:
        raise RuntimeError("npm executable not found on PATH; cannot build frontend/dist/.")

    node_modules = frontend_dir / "node_modules"
    if not node_modules.is_dir():
        _run_checked([npm_bin, "ci"], cwd=frontend_dir, label="npm ci")

    _run_checked([npm_bin, "run", "build"], cwd=frontend_dir, label="npm run build")

    if not dist_index.is_file():
        raise RuntimeError(f"Frontend build completed, but {dist_index} is missing.")


def _check_uv_lock() -> None:
    uv_bin = shutil.which("uv")
    if not uv_bin:
        raise RuntimeError("uv executable not found on PATH; cannot run `uv lock --check`.")
    _run_checked([uv_bin, "lock", "--check"], cwd=REPO_ROOT, label="uv lock --check")


def _iter_dir_files(root_dir: Path) -> list[Path]:
    collected: list[Path] = []
    for path in sorted(root_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root_dir).parts
        if any(part in EXCLUDED_DIR_NAMES for part in rel_parts):
            continue
        if path.name in EXCLUDED_FILE_NAMES or path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        collected.append(path)
    return collected


def _write_zip_entry(zf: zipfile.ZipFile, src_path: Path, arcname: str, mode: int | None = None) -> None:
    info = zipfile.ZipInfo.from_file(src_path, arcname=arcname)
    info.compress_type = zipfile.ZIP_DEFLATED
    if mode is not None:
        info.external_attr = ((0o100000 | (mode & 0o777)) & 0xFFFF) << 16
    with src_path.open("rb") as f:
        zf.writestr(info, f.read())


def assemble_release_archive(output_path: Path, skip_frontend_build: bool = False) -> Path:
    """Build frontend assets (if needed), check uv.lock, and package release zip."""
    _build_frontend(skip_frontend_build=skip_frontend_build)
    _check_uv_lock()

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = output_path.with_name(f".{output_path.name}.tmp")

    include_dirs = [
        REPO_ROOT / "backend",
        REPO_ROOT / "scripts",
        REPO_ROOT / "frontend" / "dist",
        REPO_ROOT / "assets" / "fonts",
    ]
    include_files = [
        (REPO_ROOT / "pyproject.toml", None),
        (REPO_ROOT / "uv.lock", None),
        (REPO_ROOT / "SPEC.md", None),
        (REPO_ROOT / "start.bat", None),
        (REPO_ROOT / "start.sh", 0o755),
        (REPO_ROOT / "assets" / "asset_manifest.json", None),
        (REPO_ROOT / "assets" / "THIRD_PARTY_NOTICES.md", None),
    ]

    for d in include_dirs:
        if not d.is_dir():
            raise RuntimeError(f"Required release directory missing: {d}")
    for fpath, _ in include_files:
        if not fpath.is_file():
            raise RuntimeError(f"Required release file missing: {fpath}")

    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for d in include_dirs:
                for file_path in _iter_dir_files(d):
                    arcname = file_path.relative_to(REPO_ROOT).as_posix()
                    _write_zip_entry(zf, file_path, arcname)

            for file_path, mode in include_files:
                arcname = file_path.relative_to(REPO_ROOT).as_posix()
                _write_zip_entry(zf, file_path, arcname, mode=mode)

        os.replace(tmp_zip, output_path)
    except Exception:
        tmp_zip.unlink(missing_ok=True)
        raise

    return output_path


def verify_release_archive(archive_path: Path) -> dict[str, Any]:
    """Verify uv.lock, archive contents, permissions, and asset_manifest.json checksums."""
    _check_uv_lock()

    if not archive_path.is_file():
        raise RuntimeError(f"Release archive not found: {archive_path}")

    manifest_path = REPO_ROOT / "assets" / "asset_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    with zipfile.ZipFile(archive_path, "r") as zf:
        infos = zf.infolist()
        names = {info.filename for info in infos}

        # Check forbidden artifacts
        for name in names:
            parts = Path(name).parts
            if "__pycache__" in parts or name.endswith((".pyc", ".pyo", ".pyd")):
                raise RuntimeError(f"Forbidden Python cache artifact in archive: {name}")

        required_entries = [
            "pyproject.toml",
            "uv.lock",
            "SPEC.md",
            "start.bat",
            "start.sh",
            "frontend/dist/index.html",
            "assets/asset_manifest.json",
            "assets/THIRD_PARTY_NOTICES.md",
        ]
        for req in required_entries:
            if req not in names:
                raise RuntimeError(f"Missing required entry in release archive: {req}")

        if not any(n.startswith("backend/") and n.endswith(".py") for n in names):
            raise RuntimeError("Missing backend/*.py files in release archive.")
        if not any(n.startswith("scripts/") and n.endswith(".py") for n in names):
            raise RuntimeError("Missing scripts/*.py files in release archive.")

        # Verify start.sh executable permission (0o755)
        start_sh_info = zf.getinfo("start.sh")
        unix_mode = (start_sh_info.external_attr >> 16) & 0o777
        if (unix_mode & 0o111) == 0 or (unix_mode & 0o755) != 0o755:
            raise RuntimeError(
                f"start.sh in archive must have executable permissions (0o755), got {oct(unix_mode)}"
            )

        # Verify archived asset_manifest.json matches repo manifest
        archived_manifest = json.loads(zf.read("assets/asset_manifest.json").decode("utf-8"))
        if archived_manifest != manifest:
            raise RuntimeError("Archived assets/asset_manifest.json does not match repository copy.")

        # Verify bundled font sizes and SHA-256 checksums inside the archive
        verified_fonts: dict[str, dict[str, Any]] = {}
        for font_name, meta in manifest.get("bundled_fonts", {}).items():
            arc_path = str(meta["path"])
            if arc_path not in names:
                raise RuntimeError(f"Bundled font entry missing from archive: {arc_path}")
            data = zf.read(arc_path)
            actual_size = len(data)
            actual_sha256 = _sha256_bytes(data)
            if actual_size != int(meta["size_bytes"]):
                raise RuntimeError(
                    f"Size mismatch for {arc_path}: expected {meta['size_bytes']}, got {actual_size}"
                )
            if actual_sha256 != str(meta["sha256"]):
                raise RuntimeError(
                    f"SHA-256 mismatch for {arc_path}: expected {meta['sha256']}, got {actual_sha256}"
                )
            verified_fonts[font_name] = {
                "path": arc_path,
                "size_bytes": actual_size,
                "sha256": actual_sha256,
            }

        return {
            "archive_path": str(archive_path),
            "archive_size_bytes": archive_path.stat().st_size,
            "entry_count": len(names),
            "start_sh_mode": oct(unix_mode),
            "verified_fonts": verified_fonts,
        }


def _parse_top_level_mp4_boxes(mp4_path: Path) -> list[tuple[str, int, int]]:
    """Return list of (box_type, offset, size) for top-level ISO BMFF / MP4 boxes."""
    boxes: list[tuple[str, int, int]] = []
    file_size = mp4_path.stat().st_size
    with mp4_path.open("rb") as f:
        offset = 0
        while offset + 8 <= file_size:
            f.seek(offset)
            header = f.read(8)
            if len(header) < 8:
                break
            size32, box_type_bytes = struct.unpack(">I4s", header)
            box_type = box_type_bytes.decode("latin-1", errors="replace")
            if size32 == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    raise RuntimeError(f"Truncated 64-bit MP4 box header at offset {offset}")
                box_size = struct.unpack(">Q", ext)[0]
            elif size32 == 0:
                box_size = file_size - offset
            else:
                box_size = size32

            if box_size < 8:
                raise RuntimeError(f"Invalid MP4 box size {box_size} for {box_type!r} at {offset}")
            boxes.append((box_type, offset, box_size))
            offset += box_size
    return boxes


def _parse_fps(rate_str: str) -> float:
    if "/" in rate_str:
        num_s, den_s = rate_str.split("/", 1)
        num = float(num_s)
        den = float(den_s)
        return num / den if den > 0 else 0.0
    return float(rate_str)


def _validate_exported_mp4(
    export_path: Path,
    ffprobe_path: Path,
    expected_frame_count: int,
) -> dict[str, Any]:
    probe_res = _run_checked(
        [
            str(ffprobe_path),
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-count_frames",
            str(export_path),
        ],
        cwd=export_path.parent,
        label="ffprobe exported MP4",
    )
    payload = json.loads(probe_res.stdout)
    streams = payload.get("streams", [])
    vstreams = [s for s in streams if s.get("codec_type") == "video"]
    astreams = [s for s in streams if s.get("codec_type") == "audio"]

    if len(vstreams) != 1:
        raise RuntimeError(f"Expected 1 video stream, found {len(vstreams)}")
    if len(astreams) != 1:
        raise RuntimeError(f"Expected 1 audio stream, found {len(astreams)}")

    vs = vstreams[0]
    aus = astreams[0]

    width = int(vs["width"])
    height = int(vs["height"])
    sar = str(vs.get("sample_aspect_ratio") or "")
    pix_fmt = str(vs.get("pix_fmt") or "")
    vcodec = str(vs.get("codec_name") or "")
    avg_fps = _parse_fps(str(vs.get("avg_frame_rate") or "0"))
    r_fps = _parse_fps(str(vs.get("r_frame_rate") or "0"))
    nb_read_frames = int(vs.get("nb_read_frames") or vs.get("nb_frames") or 0)
    video_dur = float(vs.get("duration") or payload.get("format", {}).get("duration") or 0.0)

    acodec = str(aus.get("codec_name") or "")
    sample_rate = int(aus.get("sample_rate") or 0)
    channels = int(aus.get("channels") or 0)
    audio_dur = float(aus.get("duration") or video_dur)

    if (width, height) != (1080, 1920):
        raise RuntimeError(f"Expected 1080x1920, got {width}x{height}")
    if sar != "1:1":
        raise RuntimeError(f"Expected SAR 1:1, got {sar!r}")
    if pix_fmt != "yuv420p":
        raise RuntimeError(f"Expected yuv420p, got {pix_fmt!r}")
    if vcodec != "h264":
        raise RuntimeError(f"Expected h264 video codec, got {vcodec!r}")
    if abs(avg_fps - 30.0) > 0.05 or abs(r_fps - 30.0) > 0.05:
        raise RuntimeError(f"Expected 30fps CFR, got avg_fps={avg_fps:.4f}, r_fps={r_fps:.4f}")
    if nb_read_frames != expected_frame_count:
        raise RuntimeError(
            f"Frame count mismatch: expected {expected_frame_count}, got {nb_read_frames}"
        )
    if acodec != "aac" or sample_rate != 48000 or channels != 2:
        raise RuntimeError(
            f"Expected 48000 Hz stereo aac, got codec={acodec!r}, sr={sample_rate}, ch={channels}"
        )

    av_delta = abs(video_dur - audio_dur)
    if av_delta > (1.0 / 30.0):
        raise RuntimeError(
            f"A/V duration alignment {av_delta:.6f}s exceeds 1/30s "
            f"(video_dur={video_dur:.6f}s, audio_dur={audio_dur:.6f}s)"
        )

    boxes = _parse_top_level_mp4_boxes(export_path)
    box_offsets = {btype: offset for btype, offset, _ in boxes}
    if "moov" not in box_offsets or "mdat" not in box_offsets:
        raise RuntimeError(f"Missing moov/mdat MP4 boxes: {boxes}")
    faststart = box_offsets["moov"] < box_offsets["mdat"]
    if not faststart:
        raise RuntimeError(
            f"MP4 is not +faststart (moov@{box_offsets['moov']} >= mdat@{box_offsets['mdat']})"
        )

    return {
        "width": width,
        "height": height,
        "sar": sar,
        "pix_fmt": pix_fmt,
        "video_codec": vcodec,
        "avg_fps": round(avg_fps, 4),
        "r_fps": round(r_fps, 4),
        "frame_count": nb_read_frames,
        "expected_frame_count": expected_frame_count,
        "audio_codec": acodec,
        "sample_rate": sample_rate,
        "channels": channels,
        "video_duration_sec": round(video_dur, 6),
        "audio_duration_sec": round(audio_dur, 6),
        "av_duration_delta_sec": round(av_delta, 6),
        "faststart": faststart,
        "moov_offset": box_offsets["moov"],
        "mdat_offset": box_offsets["mdat"],
    }


def run_benchmark() -> dict[str, Any]:
    """Run isolated end-to-end TTS synthesis + multi-segment export benchmark."""
    default_data_dir = get_data_dir()
    prev_data_dir = os.environ.get("SIMS_STUDIO_DATA_DIR")

    with tempfile.TemporaryDirectory(prefix="sims_studio_bench_") as tmp_str:
        tmp_root = Path(tmp_str)
        bench_data_dir = tmp_root / "data"
        (bench_data_dir / "models").mkdir(parents=True, exist_ok=True)
        (bench_data_dir / "bin").mkdir(parents=True, exist_ok=True)

        # Copy cached Kokoro models if present so benchmark runs offline
        cached_models_present = True
        for model_name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            src_model = default_data_dir / "models" / model_name
            if src_model.is_file():
                shutil.copy2(src_model, bench_data_dir / "models" / model_name)
            else:
                cached_models_present = False

        # Copy cached ffmpeg/ffprobe if present
        exe_suffix = ".exe" if sys.platform == "win32" else ""
        for tool_name in (f"ffmpeg{exe_suffix}", f"ffprobe{exe_suffix}"):
            src_tool = default_data_dir / "bin" / tool_name
            if src_tool.is_file():
                dst_tool = bench_data_dir / "bin" / tool_name
                shutil.copy2(src_tool, dst_tool)
                if sys.platform != "win32":
                    dst_tool.chmod(0o755)

        os.environ["SIMS_STUDIO_DATA_DIR"] = str(bench_data_dir)
        try:
            ffmpeg_bin, ffprobe_bin = verify_ffmpeg(data_dir=bench_data_dir)
            fonts_dir = verify_font(REPO_ROOT).parent

            # Generate synthetic 1920x1080 30fps 16:9 test source with stereo sine audio
            source_path = tmp_root / "synthetic_16x9_30fps.mp4"
            _run_checked(
                [
                    str(ffmpeg_bin),
                    "-y",
                    "-hide_banner",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=1920x1080:rate=30:duration=10",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=10",
                    "-af",
                    "aformat=channel_layouts=stereo",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    str(source_path),
                ],
                cwd=tmp_root,
                label="generate synthetic 16:9 test source",
            )

            source_sha256_before = _sha256_file(source_path)
            source_meta = probe_and_validate_media(
                source_path,
                ffprobe_path=ffprobe_bin,
                ffmpeg_path=ffmpeg_bin,
                data_dir=bench_data_dir,
            )

            project_dir = bench_data_dir / "projects" / "bench_proj"
            artifacts_dir = project_dir / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)

            pronunciations = {"Vaelen": "Vay-len"}
            seg1 = SegmentInput(
                id="seg_01",
                video_in=0.0,
                video_out=2.7,
                duration_mode="preserve_video",
                source_audio_volume=0.15,
                speed=1.0,
                crop_keyframes=[
                    CropKeyframe(t=0.0, x=0.28, y=0.0, zoom=1.0),
                    CropKeyframe(t=1.8, x=0.40, y=0.10, zoom=1.35),
                ],
                clauses=[
                    ClauseInput(id="c1", display_text="Lord Vaelen."),
                ],
            )
            seg2 = SegmentInput(
                id="seg_02",
                video_in=4.0,
                video_out=6.0,
                duration_mode="fit_narration",
                source_audio_volume=0.15,
                speed=1.0,
                crop_keyframes=[
                    CropKeyframe(t=0.0, x=0.34, y=0.0, zoom=1.0),
                ],
                clauses=[
                    ClauseInput(id="c1", display_text="Welcome."),
                ],
            )

            project = ProjectInput(
                project_id="bench_proj",
                title="Release Benchmark",
                source_media=SourceMediaRef(
                    media_id="src_bench",
                    path=str(source_path),
                    fingerprint=compute_file_fingerprint(source_path),
                    copied_to_project=False,
                ),
                voice="bf_emma",
                language="en-gb",
                pronunciations=pronunciations,
                segments=[seg1, seg2],
            )

            kokoro = get_kokoro(data_dir=bench_data_dir, offline=cached_models_present)

            tts_start = time.perf_counter()
            seg_artifacts: dict[str, SegmentArtifactState] = {}
            total_tts_audio_sec = 0.0
            expected_frame_count = 0

            for seg in project.segments:
                tts_res = synthesize_segment_tts(
                    segment=seg,
                    pronunciations=project.pronunciations,
                    voice=project.voice,
                    language=project.language,
                    inter_clause_pause_ms=project.output_settings.inter_clause_pause_ms,
                    output_dir=artifacts_dir,
                    kokoro=kokoro,
                )
                total_tts_audio_sec += tts_res.audio_duration
                plan = plan_segment_duration(
                    segment=seg,
                    audio_duration=tts_res.audio_duration,
                    source_duration=source_meta.duration,
                    tail_padding_sec=project.output_settings.tail_padding_sec,
                    fps=project.output_settings.fps,
                )
                expected_frame_count += plan.frame_count
                seg_artifacts[seg.id] = SegmentArtifactState(
                    tts_hash=tts_res.tts_hash,
                    audio_artifact_id=f"art_tts_{seg.id}",
                    audio_path=tts_res.audio_path.relative_to(project_dir).as_posix()
                    if tts_res.audio_path
                    else None,
                    sample_rate=tts_res.sample_rate,
                    total_samples=tts_res.total_samples,
                    audio_duration=tts_res.audio_duration,
                    clause_timings=tts_res.clause_timings,
                )
            tts_elapsed_sec = time.perf_counter() - tts_start

            manifest = ArtifactManifest(
                project_id=project.project_id,
                source_metadata=source_meta,
                segments=seg_artifacts,
            )

            export_start = time.perf_counter()
            export_path = render_final_export(
                project=project,
                source_metadata=source_meta,
                manifest=manifest,
                project_dir=project_dir,
                ffmpeg_path=ffmpeg_bin,
                ffprobe_path=ffprobe_bin,
                fonts_dir=fonts_dir,
            )
            export_elapsed_sec = time.perf_counter() - export_start

            source_sha256_after = _sha256_file(source_path)
            if source_sha256_before != source_sha256_after:
                raise RuntimeError(
                    f"Source file SHA-256 mutated during export! "
                    f"{source_sha256_before} != {source_sha256_after}"
                )

            validation = _validate_exported_mp4(
                export_path=export_path,
                ffprobe_path=ffprobe_bin,
                expected_frame_count=expected_frame_count,
            )

            return {
                "offline_models_cached": cached_models_present,
                "tts_elapsed_sec": round(tts_elapsed_sec, 4),
                "total_tts_audio_sec": round(total_tts_audio_sec, 4),
                "export_elapsed_sec": round(export_elapsed_sec, 4),
                "export_size_bytes": export_path.stat().st_size,
                "source_sha256_unchanged": True,
                "source_sha256": source_sha256_before,
                "validation": validation,
            }
        finally:
            if prev_data_dir is None:
                os.environ.pop("SIMS_STUDIO_DATA_DIR", None)
            else:
                os.environ["SIMS_STUDIO_DATA_DIR"] = prev_data_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Package Sims 4 Vertical Video Studio release archive and optionally verify/benchmark."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output zip path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--skip-frontend-build",
        action="store_true",
        help="Skip `npm run build` if frontend/dist/index.html already exists.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify uv.lock, archive contents, permissions, and bundled font checksums.",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run end-to-end TTS synthesis + multi-segment export benchmark in an isolated temp dir.",
    )

    args = parser.parse_args(argv)
    archive_path = assemble_release_archive(
        output_path=args.output,
        skip_frontend_build=args.skip_frontend_build,
    )

    report: dict[str, Any] = {
        "archive_path": str(archive_path),
        "archive_size_bytes": archive_path.stat().st_size,
    }
    if args.verify:
        report["verification"] = verify_release_archive(archive_path)
    if args.benchmark:
        report["benchmark"] = run_benchmark()

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
