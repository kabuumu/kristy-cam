"""Structured usage, interaction, action-output, and debug error logging."""

from __future__ import annotations

import json
import logging
import os
import threading
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from backend.bootstrap import get_data_dir

MAX_USAGE_LOG_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_DEBUG_LOG_BYTES = 5 * 1024 * 1024   # 5 MiB
BACKUP_COUNT = 3
MAX_STRING_LEN = 2000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_for_log(value: Any, *, max_str_len: int = MAX_STRING_LEN, _depth: int = 0) -> Any:
    """Convert arbitrary values into bounded, JSON-safe structures for logging."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        if len(value) > max_str_len:
            return value[:max_str_len] + f"... [truncated {len(value) - max_str_len} chars]"
        return value
    if _depth >= 5:
        return sanitize_for_log(repr(value), max_str_len=max_str_len, _depth=0)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (k, v) in enumerate(value.items()):
            if idx >= 50:
                out["__truncated_keys__"] = len(value) - 50
                break
            out[str(k)] = sanitize_for_log(v, max_str_len=max_str_len, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        items = [sanitize_for_log(item, max_str_len=max_str_len, _depth=_depth + 1) for item in seq[:50]]
        if len(seq) > 50:
            items.append(f"... [{len(seq) - 50} more items]")
        return items
    return sanitize_for_log(repr(value), max_str_len=max_str_len, _depth=0)


def _rotate_if_needed(path: Path, max_bytes: int, backup_count: int) -> None:
    if not path.is_file():
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    for idx in range(backup_count, 0, -1):
        src = path.with_name(f"{path.name}.{idx}") if idx > 0 else path
        dst = path.with_name(f"{path.name}.{idx + 1}")
        if idx == backup_count and dst.exists():
            dst.unlink(missing_ok=True)
        if src.exists():
            src.replace(dst)
    first_backup = path.with_name(f"{path.name}.1")
    if first_backup.exists():
        first_backup.unlink(missing_ok=True)
    path.replace(first_backup)


class UsageLogger:
    """Thread-safe usage & debug logger writing to data_dir/logs/{usage.jsonl, studio.log}."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.logs_dir = self.data_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.usage_log_path = self.logs_dir / "usage.jsonl"
        self.debug_log_path = self.logs_dir / "studio.log"
        self._lock = threading.Lock()

        # Per-instance Python logger for human-readable debug output in studio.log
        logger_name = f"sims_studio.{abs(hash(str(self.data_dir)))}"
        self._logger = logging.getLogger(logger_name)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = RotatingFileHandler(
                self.debug_log_path,
                maxBytes=MAX_DEBUG_LOG_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
            )
            self._logger.addHandler(handler)

    def _format_debug_line(self, entry: dict[str, Any]) -> str:
        parts = [
            f"src={entry.get('source', '-')}",
            f"cat={entry.get('category', '-')}",
            f"action={entry.get('action', '-')}",
        ]
        if entry.get("intention"):
            parts.append(f"intent={entry['intention']!r}")
        if entry.get("target"):
            parts.append(f"target={entry['target']!r}")
        if entry.get("project_id"):
            parts.append(f"project={entry['project_id']}")
        if entry.get("seg_id"):
            parts.append(f"seg={entry['seg_id']}")
        if entry.get("job_id"):
            parts.append(f"job={entry['job_id']}")
        if entry.get("duration_ms") is not None:
            parts.append(f"dur_ms={entry['duration_ms']}")
        if entry.get("input") is not None:
            parts.append(f"in={json.dumps(entry['input'], ensure_ascii=False)}")
        if entry.get("output") is not None:
            parts.append(f"out={json.dumps(entry['output'], ensure_ascii=False)}")
        if entry.get("error") is not None:
            parts.append(f"err={json.dumps(entry['error'], ensure_ascii=False)}")
        return " | ".join(parts)

    def log_event(
        self,
        *,
        source: str,
        category: str,
        action: str,
        intention: str | None = None,
        target: str | None = None,
        project_id: str | None = None,
        seg_id: str | None = None,
        job_id: str | None = None,
        input_data: Any = None,
        output_data: Any = None,
        error_data: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        ts: str | None = None,
    ) -> dict[str, Any]:
        """Persist a structured usage event to usage.jsonl and summarize in studio.log."""
        entry: dict[str, Any] = {
            "ts": ts or _utc_now_iso(),
            "source": source,
            "category": category,
            "action": action,
        }
        if intention:
            entry["intention"] = sanitize_for_log(intention, max_str_len=500)
        if target:
            entry["target"] = sanitize_for_log(target, max_str_len=500)
        if project_id:
            entry["project_id"] = str(project_id)
        if seg_id:
            entry["seg_id"] = str(seg_id)
        if job_id:
            entry["job_id"] = str(job_id)
        if input_data is not None:
            entry["input"] = sanitize_for_log(input_data)
        if output_data is not None:
            entry["output"] = sanitize_for_log(output_data)
        if error_data is not None:
            entry["error"] = sanitize_for_log(error_data, max_str_len=4000)
        if duration_ms is not None:
            entry["duration_ms"] = round(float(duration_ms), 2)

        line = json.dumps(entry, ensure_ascii=False)
        debug_line = self._format_debug_line(entry)

        with self._lock:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(self.usage_log_path, MAX_USAGE_LOG_BYTES, BACKUP_COUNT)
            with self.usage_log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

            if category == "error":
                self._logger.error(debug_line)
            else:
                self._logger.info(debug_line)

        return entry

    def log_error(
        self,
        *,
        source: str = "backend",
        action: str,
        intention: str | None = None,
        target: str | None = None,
        project_id: str | None = None,
        seg_id: str | None = None,
        job_id: str | None = None,
        input_data: Any = None,
        message: str | None = None,
        error_type: str | None = None,
        status_code: int | None = None,
        detail: Any = None,
        context: dict[str, Any] | None = None,
        exc: BaseException | None = None,
        duration_ms: float | None = None,
    ) -> dict[str, Any]:
        """Log an error with rich debugging details (type, message, stack trace, context)."""
        stack_str: str | None = None
        if exc is not None:
            error_type = error_type or type(exc).__name__
            message = message or str(exc) or type(exc).__name__
            stack_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

        err_payload: dict[str, Any] = {
            "message": message or "Unknown error",
            "type": error_type or "Error",
        }
        if status_code is not None:
            err_payload["status_code"] = status_code
        if detail is not None:
            err_payload["detail"] = detail
        if context:
            err_payload["context"] = context
        if stack_str:
            err_payload["stack"] = stack_str

        return self.log_event(
            source=source,
            category="error",
            action=action,
            intention=intention,
            target=target,
            project_id=project_id,
            seg_id=seg_id,
            job_id=job_id,
            input_data=input_data,
            error_data=err_payload,
            duration_ms=duration_ms,
        )

    def read_recent(
        self,
        *,
        limit: int = 200,
        category: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return up to `limit` most recent matching usage events (chronological order)."""
        safe_limit = max(1, min(1000, int(limit)))
        with self._lock:
            if not self.usage_log_path.is_file():
                return []
            try:
                raw_lines = self.usage_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return []

        matched: list[dict[str, Any]] = []
        for raw in reversed(raw_lines):
            if not raw.strip():
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if category and item.get("category") != category:
                continue
            if project_id and item.get("project_id") != project_id:
                continue
            matched.append(item)
            if len(matched) >= safe_limit:
                break

        matched.reverse()
        return matched


_LOGGER_CACHE_LOCK = threading.Lock()
_LOGGER_CACHE: dict[Path, UsageLogger] = {}


def get_usage_logger(data_dir: Path | None = None) -> UsageLogger:
    """Get or create the UsageLogger for the given (or default) data directory."""
    resolved = Path(data_dir).resolve() if data_dir is not None else get_data_dir().resolve()
    with _LOGGER_CACHE_LOCK:
        instance = _LOGGER_CACHE.get(resolved)
        if instance is None:
            instance = UsageLogger(resolved)
            _LOGGER_CACHE[resolved] = instance
        return instance


def close_usage_logger_handlers() -> None:
    """Close cached file handlers (useful in tests or clean shutdown)."""
    with _LOGGER_CACHE_LOCK:
        for instance in _LOGGER_CACHE.values():
            for handler in list(instance._logger.handlers):
                handler.close()
                instance._logger.removeHandler(handler)
        _LOGGER_CACHE.clear()
