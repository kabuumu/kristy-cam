"""Bounded background job queue, progress tracking, cancellation, and startup recovery."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from backend.bootstrap import get_data_dir
from backend.schemas import JobState, JobType, utc_now_iso
from backend.storage import atomic_write_json
from backend.usage_log import get_usage_logger


class JobCancelledError(RuntimeError):
    """Raised when an executing job is cancelled."""


class JobContext:
    """Execution context passed to background job worker functions."""

    def __init__(
        self,
        manager: JobManager,
        job_id: str,
        project_id: str,
        seg_id: str | None,
        cancel_event: threading.Event,
    ) -> None:
        self._manager = manager
        self.job_id = job_id
        self.project_id = project_id
        self.seg_id = seg_id
        self.cancel_event = cancel_event

    def update_progress(self, progress: int, stage: str = "") -> None:
        """Update job progress (0-100) and stage description."""
        self.check_cancelled()
        clamped = max(0, min(100, int(progress)))
        self._manager._update_job_progress(self.job_id, clamped, stage)

    def register_process(self, proc: subprocess.Popen[Any]) -> None:
        """Register an active subprocess for termination if the job is cancelled."""
        self._manager._register_process(self.job_id, proc)
        if self.cancel_event.is_set():
            _terminate_popen(proc)
            raise JobCancelledError(f"Job {self.job_id} cancelled")

    def check_cancelled(self) -> None:
        """Raise JobCancelledError if cancellation has been requested."""
        if self.cancel_event.is_set():
            raise JobCancelledError(f"Job {self.job_id} cancelled")


def _terminate_popen(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=1.0)
    except OSError:
        pass


class JobManager:
    """Bounded ThreadPoolExecutor job manager with atomic JSON persistence and recovery."""

    def __init__(self, data_dir: Path | None = None, max_workers: int = 2) -> None:
        self.data_dir = data_dir if data_dir is not None else get_data_dir()
        self.jobs_dir = self.data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._usage_logger = get_usage_logger(self.data_dir)

        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="studio-job")
        self._jobs: dict[str, JobState] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._processes: dict[str, list[subprocess.Popen[Any]]] = {}
        self._futures: dict[str, Future[Any]] = {}

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _persist_job(self, job: JobState) -> None:
        atomic_write_json(self._job_path(job.job_id), job.model_dump(mode="json"), create_backup=False)

    def _register_process(self, job_id: str, proc: subprocess.Popen[Any]) -> None:
        with self._lock:
            procs = self._processes.setdefault(job_id, [])
            procs.append(proc)

    def _update_job_progress(self, job_id: str, progress: int, stage: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in {"cancelled", "succeeded", "failed", "interrupted"}:
                return
            updated = job.model_copy(
                update={
                    "progress": progress,
                    "stage": stage,
                    "updated_at": utc_now_iso(),
                }
            )
            self._jobs[job_id] = updated
            self._persist_job(updated)

    def submit_job(
        self,
        project_id: str,
        job_type: JobType,
        worker_fn: Callable[[JobContext], dict[str, Any] | None],
        *,
        seg_id: str | None = None,
    ) -> JobState:
        """Persist a queued JobState immediately and enqueue worker_fn on the bounded pool."""
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        now = utc_now_iso()
        job = JobState(
            job_id=job_id,
            project_id=project_id,
            seg_id=seg_id,
            job_type=job_type,
            status="queued",
            progress=0,
            stage="Queued",
            created_at=now,
            updated_at=now,
        )
        cancel_event = threading.Event()

        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = cancel_event
            self._processes[job_id] = []
            self._persist_job(job)
            future = self._executor.submit(self._execute_job, job_id, worker_fn)
            self._futures[job_id] = future

        self._usage_logger.log_event(
            source="job",
            category="job",
            action="job.queued",
            intention=f"Queue {job_type} job",
            project_id=project_id,
            seg_id=seg_id,
            job_id=job_id,
            input_data={"job_type": job_type},
        )
        return job.model_copy(deep=True)

    def _execute_job(
        self,
        job_id: str,
        worker_fn: Callable[[JobContext], dict[str, Any] | None],
    ) -> None:
        t0 = time.perf_counter()
        with self._lock:
            job = self._jobs.get(job_id)
            cancel_event = self._cancel_events.get(job_id)
            if job is None or cancel_event is None:
                return
            if cancel_event.is_set() or job.status == "cancelled":
                cancelled = job.model_copy(
                    update={"status": "cancelled", "stage": "Cancelled", "updated_at": utc_now_iso()}
                )
                self._jobs[job_id] = cancelled
                self._persist_job(cancelled)
                self._processes.pop(job_id, None)
                self._usage_logger.log_event(
                    source="job",
                    category="action_output",
                    action="job.cancelled",
                    intention=f"Cancel {job.job_type} job before start",
                    project_id=job.project_id,
                    seg_id=job.seg_id,
                    job_id=job_id,
                    output_data={"status": "cancelled"},
                )
                return

            running = job.model_copy(
                update={"status": "running", "stage": "Running", "updated_at": utc_now_iso()}
            )
            self._jobs[job_id] = running
            self._persist_job(running)
            ctx = JobContext(
                manager=self,
                job_id=job_id,
                project_id=job.project_id,
                seg_id=job.seg_id,
                cancel_event=cancel_event,
            )

        self._usage_logger.log_event(
            source="job",
            category="job",
            action="job.started",
            intention=f"Run {job.job_type} job",
            project_id=job.project_id,
            seg_id=job.seg_id,
            job_id=job_id,
        )

        try:
            result = worker_fn(ctx)
            duration_ms = (time.perf_counter() - t0) * 1000.0
            with self._lock:
                current = self._jobs[job_id]
                if cancel_event.is_set() or current.status == "cancelled":
                    final_job = current.model_copy(
                        update={"status": "cancelled", "stage": "Cancelled", "updated_at": utc_now_iso()}
                    )
                else:
                    final_job = current.model_copy(
                        update={
                            "status": "succeeded",
                            "progress": 100,
                            "stage": "Completed",
                            "result": result if isinstance(result, dict) else None,
                            "error": None,
                            "updated_at": utc_now_iso(),
                        }
                    )
                self._jobs[job_id] = final_job
                self._persist_job(final_job)
            self._usage_logger.log_event(
                source="job",
                category="action_output",
                action=f"job.{final_job.status}",
                intention=f"Complete {job.job_type} job",
                project_id=job.project_id,
                seg_id=job.seg_id,
                job_id=job_id,
                output_data={"status": final_job.status, "result": final_job.result},
                duration_ms=duration_ms,
            )
        except JobCancelledError:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            with self._lock:
                current = self._jobs[job_id]
                final_job = current.model_copy(
                    update={"status": "cancelled", "stage": "Cancelled", "updated_at": utc_now_iso()}
                )
                self._jobs[job_id] = final_job
                self._persist_job(final_job)
            self._usage_logger.log_event(
                source="job",
                category="action_output",
                action="job.cancelled",
                intention=f"Cancel {job.job_type} job",
                project_id=job.project_id,
                seg_id=job.seg_id,
                job_id=job_id,
                output_data={"status": "cancelled", "stage": current.stage, "progress": current.progress},
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            with self._lock:
                current = self._jobs[job_id]
                if cancel_event.is_set() or current.status == "cancelled":
                    final_job = current.model_copy(
                        update={"status": "cancelled", "stage": "Cancelled", "updated_at": utc_now_iso()}
                    )
                else:
                    final_job = current.model_copy(
                        update={
                            "status": "failed",
                            "stage": "Failed",
                            "error": str(exc),
                            "updated_at": utc_now_iso(),
                        }
                    )
                self._jobs[job_id] = final_job
                self._persist_job(final_job)
            if final_job.status == "failed":
                self._usage_logger.log_error(
                    source="job",
                    action="job.failed",
                    intention=f"Execute {job.job_type} job",
                    project_id=job.project_id,
                    seg_id=job.seg_id,
                    job_id=job_id,
                    context={
                        "job_type": job.job_type,
                        "last_stage": current.stage,
                        "last_progress": current.progress,
                    },
                    exc=exc,
                    duration_ms=duration_ms,
                )
            else:
                self._usage_logger.log_event(
                    source="job",
                    category="action_output",
                    action="job.cancelled",
                    intention=f"Cancel {job.job_type} job",
                    project_id=job.project_id,
                    seg_id=job.seg_id,
                    job_id=job_id,
                    output_data={"status": "cancelled"},
                    duration_ms=duration_ms,
                )
        finally:
            with self._lock:
                self._processes.pop(job_id, None)

    def get_job(self, job_id: str) -> JobState | None:
        """Return JobState from memory or persisted JSON on disk, or None if not found."""
        with self._lock:
            if job_id in self._jobs:
                return self._jobs[job_id].model_copy(deep=True)

        path = self._job_path(job_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            job = JobState.model_validate(data)
            with self._lock:
                self._jobs[job_id] = job
            return job.model_copy(deep=True)
        except Exception:
            return None

    def list_jobs(self, project_id: str | None = None) -> list[JobState]:
        """List all known jobs, optionally filtered by project_id."""
        with self._lock:
            merged: dict[str, JobState] = {k: v.model_copy(deep=True) for k, v in self._jobs.items()}

        if self.jobs_dir.is_dir():
            for path in self.jobs_dir.glob("*.json"):
                job_id = path.stem
                if job_id in merged:
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    job = JobState.model_validate(data)
                    merged[job.job_id] = job
                except Exception:
                    continue

        jobs = list(merged.values())
        if project_id is not None:
            jobs = [j for j in jobs if j.project_id == project_id]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def cancel_job(self, job_id: str) -> JobState | None:
        """Request cancellation of a job and terminate any registered subprocesses."""
        with self._lock:
            job = self.get_job(job_id)
            if job is None:
                return None

            if job.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                return job

            cancel_event = self._cancel_events.get(job_id)
            if cancel_event is not None:
                cancel_event.set()

            procs = list(self._processes.get(job_id, []))
            cancelled = job.model_copy(
                update={"status": "cancelled", "stage": "Cancelled", "updated_at": utc_now_iso()}
            )
            self._jobs[job_id] = cancelled
            self._persist_job(cancelled)

        for proc in procs:
            _terminate_popen(proc)

        return cancelled.model_copy(deep=True)

    def recover_on_startup(self) -> list[JobState]:
        """Mark persisted queued/running jobs as interrupted and purge projects/*/tmp/*."""
        recovered: list[JobState] = []
        if self.jobs_dir.is_dir():
            for path in sorted(self.jobs_dir.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    job = JobState.model_validate(data)
                except Exception:
                    continue

                if job.status in {"queued", "running"}:
                    interrupted = job.model_copy(
                        update={
                            "status": "interrupted",
                            "stage": "Interrupted",
                            "error": "Server restarted while job was in progress.",
                            "updated_at": utc_now_iso(),
                        }
                    )
                    self._persist_job(interrupted)
                    with self._lock:
                        self._jobs[interrupted.job_id] = interrupted
                    recovered.append(interrupted)
                else:
                    with self._lock:
                        self._jobs.setdefault(job.job_id, job)

        projects_dir = self.data_dir / "projects"
        if projects_dir.is_dir():
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                tmp_dir = proj_dir / "tmp"
                if not tmp_dir.is_dir():
                    continue
                for item in tmp_dir.iterdir():
                    if item.is_dir() and not item.is_symlink():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)

        return recovered

    def shutdown(self, wait: bool = False) -> None:
        """Shut down the worker thread pool."""
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
