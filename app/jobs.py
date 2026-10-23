"""Tiny background job system with progress, logs and cancellation.

Two lanes so a long Whisper transcription never blocks a quick trim:
  ingest -> one worker (CPU-heavy transcription / vision captioning)
  render -> one worker (FFmpeg encodes)
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind: str, title: str, lane: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.title = title
        self.lane = lane
        self.status = "queued"          # queued | running | done | error | cancelled
        self.progress = 0.0
        self.message = ""
        self.result: Any = None
        self.error: str | None = None
        self.log: list[str] = []
        self.created = time.time()
        self.finished: float | None = None
        self._cancel = threading.Event()
        self.proc = None                # running subprocess, if any (killed on cancel)

    # called from worker code
    def update(self, progress: float | None = None, message: str | None = None) -> None:
        if self._cancel.is_set():
            raise Cancelled()
        if progress is not None:
            self.progress = max(0.0, min(1.0, float(progress)))
        if message is not None:
            self.message = message

    def write(self, line: str) -> None:
        self.log.append(line)
        if len(self.log) > 400:
            del self.log[:100]

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "lane": self.lane,
            "status": self.status, "progress": round(self.progress, 4), "message": self.message,
            "result": self.result, "error": self.error, "log": self.log[-60:],
            "created": self.created, "finished": self.finished,
        }


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lanes = {
            "ingest": ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest"),
            "render": ThreadPoolExecutor(max_workers=1, thread_name_prefix="render"),
            "misc": ThreadPoolExecutor(max_workers=2, thread_name_prefix="misc"),
            "download": ThreadPoolExecutor(max_workers=2, thread_name_prefix="download"),
            "clips": ThreadPoolExecutor(max_workers=1, thread_name_prefix="clips"),
        }
        self.lock = threading.Lock()

    def submit(self, kind: str, title: str, fn: Callable[[Job], Any], lane: str = "misc",
               on_error: Callable[[Job, BaseException], None] | None = None) -> Job:
        job = Job(kind, title, lane)
        with self.lock:
            self.jobs[job.id] = job
            self._prune()

        def runner():
            if job.cancelled:
                job.status = "cancelled"
                job.finished = time.time()
                return
            job.status = "running"
            try:
                job.result = fn(job)
                job.status = "done"
                job.progress = 1.0
            except Cancelled:
                job.status = "cancelled"
                job.message = "Cancelled"
            except Exception as exc:  # noqa: BLE001
                job.status = "error"
                job.error = str(exc) or exc.__class__.__name__
                job.write(traceback.format_exc())
                if on_error:
                    try:
                        on_error(job, exc)
                    except Exception:
                        pass
            finally:
                job.finished = time.time()
                job.proc = None

        self.lanes[lane].submit(runner)
        return job

    def get(self, jid: str) -> Job | None:
        return self.jobs.get(jid)

    def list(self) -> list[dict]:
        with self.lock:
            return [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)]

    def _prune(self) -> None:
        done = [j for j in self.jobs.values() if j.finished and time.time() - j.finished > 3600]
        for j in done:
            self.jobs.pop(j.id, None)


jobs = JobManager()
