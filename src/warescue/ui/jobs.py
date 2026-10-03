"""Background jobs for long operations (decrypt, merge, encrypt).

Jobs run one at a time on a single worker thread. Each one may hold a few
hundred megabytes in memory, and they usually depend on each other anyway.
The page polls a job's snapshot until it is done or failed.
"""
from __future__ import annotations

import secrets
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from .. import crypt15, merge, service
from .web import ApiError

EXPECTED_ERRORS = (crypt15.Crypt15Error, merge.MergeError, service.ServiceError)

Progress = Callable[[float | None, str], None]
Task = Callable[[Progress], Any]


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    state: JobState = JobState.QUEUED
    progress: float | None = None    # 0..1, None when unknown
    message: str = ""
    result: Any = None               # JSON-serialisable value returned by the task
    error: str | None = None
    error_code: str | None = None    # selects the translated text on the page

    @property
    def finished(self) -> bool:
        return self.state in (JobState.DONE, JobState.FAILED)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "state": self.state.value,
                "progress": self.progress, "message": self.message,
                "result": self.result, "error": self.error,
                "error_code": self.error_code}


class JobRunner:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="warescue-job")

    def submit(self, kind: str, task: Task) -> Job:
        job = Job(secrets.token_urlsafe(9), kind)
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run, job.id, task)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    @property
    def busy(self) -> bool:
        with self._lock:
            return any(not job.finished for job in self._jobs.values())

    def shutdown(self, *, wait: bool = True, cancel_pending: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_pending)

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            self._jobs[job_id] = replace(self._jobs[job_id], **changes)

    def _run(self, job_id: str, task: Task) -> None:
        def report(fraction: float | None, message: str) -> None:
            self._update(job_id, progress=fraction, message=message)

        self._update(job_id, state=JobState.RUNNING)
        try:
            result = task(report)
        except ApiError as exc:
            self._update(job_id, state=JobState.FAILED, error=exc.message, error_code=exc.code)
        except EXPECTED_ERRORS as exc:
            self._update(job_id, state=JobState.FAILED, error=str(exc), error_code="failed")
        except Exception as exc:
            traceback.print_exc()
            self._update(job_id, state=JobState.FAILED, error_code="unexpected",
                         error=f"unexpected error ({type(exc).__name__})")
        else:
            self._update(job_id, state=JobState.DONE, progress=1.0, result=result)
