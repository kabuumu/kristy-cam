"""Atomic JSON persistence, .bak recovery, optimistic revision locking, and source portability."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.bootstrap import get_data_dir
from backend.schemas import (
    CURRENT_SCHEMA_VERSION,
    ArtifactManifest,
    CaptionSettings,
    OutputSettings,
    ProjectInput,
    SegmentInput,
    SourceMediaRef,
    utc_now_iso,
)

FINGERPRINT_CHUNK_BYTES = 64 * 1024  # 64 KB header + 64 KB footer


class StorageError(Exception):
    """Base error for persistence and storage operations."""


class ProjectNotFoundError(StorageError):
    """Raised when a project directory or project.json/.bak does not exist."""


class RevisionConflictError(StorageError):
    """Raised when expected_revision does not match persisted project revision (HTTP 409)."""

    def __init__(self, expected_revision: int, actual_revision: int, project_id: str = "") -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.project_id = project_id
        super().__init__(
            f"Revision conflict for project {project_id!r}: "
            f"expected {expected_revision}, actual {actual_revision}"
        )


class UnsupportedSchemaVersionError(StorageError):
    """Raised when project.json has a schema_version newer than supported."""

    def __init__(self, found_version: Any, max_supported_version: int = CURRENT_SCHEMA_VERSION) -> None:
        self.found_version = found_version
        self.max_supported_version = max_supported_version
        super().__init__(
            f"Unsupported schema_version {found_version!r}; "
            f"max supported version is {max_supported_version}"
        )


class CorruptProjectError(StorageError):
    """Raised when both project.json and project.json.bak are corrupt or invalid."""


def _resolve_data_dir(data_dir: Path | None = None) -> Path:
    return data_dir if data_dir is not None else get_data_dir()


def get_project_dir(project_id: str, data_dir: Path | None = None) -> Path:
    """Resolve a project directory safely, rejecting empty IDs and path traversal."""
    if not project_id or not isinstance(project_id, str):
        raise StorageError(f"Invalid project_id: {project_id!r}")
    if (
        "/" in project_id
        or "\\" in project_id
        or ".." in project_id
        or "\x00" in project_id
        or project_id.strip() != project_id
    ):
        raise StorageError(f"Invalid project_id (path traversal rejected): {project_id!r}")

    base = _resolve_data_dir(data_dir)
    projects_root = (base / "projects").resolve()
    projects_root.mkdir(parents=True, exist_ok=True)
    candidate = (projects_root / project_id).resolve()

    if candidate.parent != projects_root:
        raise StorageError(f"Invalid project_id (escapes projects root): {project_id!r}")

    return candidate


def atomic_write_bytes(path: Path, data: bytes, create_backup: bool = False) -> None:
    """Atomically write bytes via temp file in same dir, flush+fsync, optional .bak, os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        if create_backup and path.is_file():
            bak_path = path.with_name(f"{path.name}.bak")
            shutil.copy2(path, bak_path)

        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Any, create_backup: bool = False) -> None:
    """Serialize payload as UTF-8 JSON and write atomically."""
    encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    atomic_write_bytes(path, encoded, create_backup=create_backup)


def _parse_and_validate_project_bytes(raw_text: str) -> ProjectInput:
    """Parse JSON, check schema_version BEFORE Pydantic validation, and return ProjectInput."""
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("project.json root must be a JSON object")

    raw_version = data.get("schema_version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise UnsupportedSchemaVersionError(raw_version, CURRENT_SCHEMA_VERSION)
    if raw_version > CURRENT_SCHEMA_VERSION or raw_version < 1:
        raise UnsupportedSchemaVersionError(raw_version, CURRENT_SCHEMA_VERSION)

    return ProjectInput.model_validate(data)


def load_project(project_id: str, data_dir: Path | None = None) -> ProjectInput:
    """Load project.json, falling back to project.json.bak if missing or corrupt JSON.

    Never overwrites or modifies files on UnsupportedSchemaVersionError.
    """
    project_dir = get_project_dir(project_id, data_dir)
    project_path = project_dir / "project.json"
    bak_path = project_dir / "project.json.bak"

    if not project_path.is_file() and not bak_path.is_file():
        raise ProjectNotFoundError(f"Project {project_id!r} not found at {project_dir}")

    primary_error: Exception | None = None
    if project_path.is_file():
        try:
            raw_text = project_path.read_text(encoding="utf-8")
            return _parse_and_validate_project_bytes(raw_text)
        except UnsupportedSchemaVersionError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
            primary_error = exc

    if bak_path.is_file():
        try:
            raw_bak = bak_path.read_text(encoding="utf-8")
            return _parse_and_validate_project_bytes(raw_bak)
        except UnsupportedSchemaVersionError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as bak_exc:
            raise CorruptProjectError(
                f"Both project.json and project.json.bak are unreadable/invalid for {project_id!r}: "
                f"primary={primary_error}; backup={bak_exc}"
            ) from bak_exc

    raise CorruptProjectError(
        f"project.json is corrupt for {project_id!r} and no .bak exists: {primary_error}"
    )


def save_project(
    project: ProjectInput,
    expected_revision: int,
    data_dir: Path | None = None,
) -> ProjectInput:
    """Persist ProjectInput with optimistic revision locking and .bak backup."""
    project_dir = get_project_dir(project.project_id, data_dir)
    existing = load_project(project.project_id, data_dir)

    if existing.revision != expected_revision:
        raise RevisionConflictError(
            expected_revision=expected_revision,
            actual_revision=existing.revision,
            project_id=project.project_id,
        )

    updated = project.model_copy(
        update={
            "revision": existing.revision + 1,
            "updated_at": utc_now_iso(),
        }
    )
    project_path = project_dir / "project.json"
    atomic_write_json(project_path, updated.model_dump(mode="json"), create_backup=True)
    return updated


def load_global_pronunciations(data_dir: Path | None = None) -> dict[str, str]:
    """Load global default pronunciation dictionary from data_dir/pronunciations.json."""
    base = _resolve_data_dir(data_dir)
    pron_path = base / "pronunciations.json"
    if not pron_path.is_file():
        return {}
    raw = json.loads(pron_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise StorageError("Global pronunciations.json must be a JSON object")
    return {str(k): str(v) for k, v in raw.items()}


def save_global_pronunciations(
    pronunciations: dict[str, str],
    data_dir: Path | None = None,
) -> dict[str, str]:
    """Atomically save global default pronunciation dictionary."""
    base = _resolve_data_dir(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    cleaned = {str(k): str(v) for k, v in pronunciations.items()}
    atomic_write_json(base / "pronunciations.json", cleaned, create_backup=True)
    return dict(cleaned)


def create_project(
    project_id: str,
    title: str,
    source_media: SourceMediaRef,
    *,
    voice: str = "bf_emma",
    language: str = "en-gb",
    output_settings: OutputSettings | None = None,
    caption_settings: CaptionSettings | None = None,
    segments: list[SegmentInput] | None = None,
    data_dir: Path | None = None,
) -> ProjectInput:
    """Create a new project directory layout, seed pronunciations from global defaults, and persist."""
    project_dir = get_project_dir(project_id, data_dir)
    project_path = project_dir / "project.json"
    if project_path.is_file():
        raise StorageError(f"Project {project_id!r} already exists")

    for sub in ("artifacts", "exports", "tmp", "source"):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    seeded_pronunciations = dict(load_global_pronunciations(data_dir))
    now = utc_now_iso()
    project = ProjectInput(
        schema_version=CURRENT_SCHEMA_VERSION,
        project_id=project_id,
        revision=1,
        title=title,
        created_at=now,
        updated_at=now,
        source_media=source_media,
        voice=voice,
        language=language,
        output_settings=output_settings if output_settings is not None else OutputSettings(),
        caption_settings=caption_settings if caption_settings is not None else CaptionSettings(),
        pronunciations=seeded_pronunciations,
        segments=segments if segments is not None else [],
    )
    atomic_write_json(project_path, project.model_dump(mode="json"), create_backup=False)
    manifest = ArtifactManifest(project_id=project_id)
    atomic_write_json(project_dir / "manifest.json", manifest.model_dump(mode="json"), create_backup=False)
    return project


def load_manifest(project_id: str, data_dir: Path | None = None) -> ArtifactManifest:
    """Load manifest.json for a project, returning an empty ArtifactManifest if absent."""
    project_dir = get_project_dir(project_id, data_dir)
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.is_file():
        return ArtifactManifest(project_id=project_id)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return ArtifactManifest.model_validate(data)


def save_manifest(manifest: ArtifactManifest, data_dir: Path | None = None) -> None:
    """Atomically persist manifest.json."""
    project_dir = get_project_dir(manifest.project_id, data_dir)
    atomic_write_json(project_dir / "manifest.json", manifest.model_dump(mode="json"), create_backup=True)


def compute_file_fingerprint(path: str | Path) -> str:
    """Compute fast portable fingerprint from size, mtime_ns, and 64KB header/footer SHA-256."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Cannot fingerprint missing file: {file_path}")

    st = file_path.stat()
    h = hashlib.sha256()
    h.update(f"{st.st_size}:{st.st_mtime_ns}:".encode("utf-8"))

    with file_path.open("rb") as f:
        header = f.read(FINGERPRINT_CHUNK_BYTES)
        h.update(header)
        if st.st_size > FINGERPRINT_CHUNK_BYTES:
            offset = max(FINGERPRINT_CHUNK_BYTES, st.st_size - FINGERPRINT_CHUNK_BYTES)
            f.seek(offset)
            footer = f.read(FINGERPRINT_CHUNK_BYTES)
            h.update(footer)

    return f"sha256:{h.hexdigest()}"


def check_source_media(source_media: SourceMediaRef) -> dict[str, Any]:
    """Check whether source_media exists on disk and matches its stored fingerprint."""
    path = Path(source_media.path)
    if not path.is_file():
        return {
            "exists": False,
            "fingerprint_match": False,
            "actual_fingerprint": None,
            "status": "missing",
        }

    actual = compute_file_fingerprint(path)
    match = actual == source_media.fingerprint
    return {
        "exists": True,
        "fingerprint_match": match,
        "actual_fingerprint": actual,
        "status": "ok" if match else "modified",
    }


def relink_source_media(
    project_id: str,
    new_path: str | Path,
    media_id: str,
    copy_to_project: bool = False,
    data_dir: Path | None = None,
) -> ProjectInput:
    """Relink a project's source_media to new_path (optionally copying into projects/<id>/source/)."""
    src_path = Path(new_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"Relink target file does not exist: {src_path}")

    project = load_project(project_id, data_dir)
    project_dir = get_project_dir(project_id, data_dir)

    target_path = src_path.resolve()
    if copy_to_project:
        source_dir = project_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        dest_path = source_dir / src_path.name
        if dest_path.resolve() != target_path:
            shutil.copy2(target_path, dest_path)
        target_path = dest_path.resolve()

    fingerprint = compute_file_fingerprint(target_path)
    updated_ref = SourceMediaRef(
        media_id=media_id,
        path=str(target_path),
        fingerprint=fingerprint,
        copied_to_project=copy_to_project,
    )
    updated_project = project.model_copy(update={"source_media": updated_ref})
    return save_project(updated_project, expected_revision=project.revision, data_dir=data_dir)
