"""One cancellable offline enhancement worker per editor window."""
from __future__ import annotations

import math
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from . import imaging as im


@dataclass
class Job:
    identifier: str
    photo: object
    denoise: float
    sharpen: float
    state: str = "queued"
    progress: float = 0.0
    message: str = "Waiting to prepare AI detail."
    error: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)


def _prepare(*args, **kwargs):
    return im.prepare_ai(*args, **kwargs)


def _cancelled_exception(error):
    try:
        from .enhance import EnhancementCancelled
    except ImportError:
        return False
    return isinstance(error, EnhancementCancelled)


class EnhancementJobs:
    """Jobs warm the imaging cache only; applying a recipe remains a UI action."""

    def __init__(self, prepare=None):
        self.prepare = prepare or _prepare
        self.lock = threading.RLock()
        self.jobs = OrderedDict()
        self.active = None
        self.closed = False

    @staticmethod
    def _amount(value, name):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError(f"AI {name} must be between 0 and 100.")
        return float(value)

    def start(self, photo, denoise=0, sharpen=0):
        denoise = self._amount(denoise, "noise reduction")
        sharpen = self._amount(sharpen, "sharpening")
        photo.verify()
        with self.lock:
            if self.closed:
                raise ValueError("This editor is closing. Reopen Fieldroom to prepare AI detail.")
            if self.active is not None:
                raise ValueError("An AI enhancement is still running. Cancel it and wait for it to stop before starting another.")
            job = Job(secrets.token_urlsafe(18), photo, denoise, sharpen)
            self.jobs[job.identifier] = job
            self.active = job.identifier
            while len(self.jobs) > 8:
                self.jobs.popitem(last=False)
            worker = threading.Thread(target=self._run, args=(job,), name="Fieldroom-AI", daemon=True)
            try:
                worker.start()
            except BaseException:
                self.active = None
                del self.jobs[job.identifier]
                raise
            return {"job": job.identifier, "state": "queued"}

    def _run(self, job):
        completed, failure = False, None
        try:
            with self.lock:
                job.state = "running"
                job.message = "Preparing the original photograph for offline AI detail."
            if job.cancelled.is_set():
                return
            job.photo.verify()

            def progress(fraction, message=""):
                if not isinstance(fraction, (int, float)) or not math.isfinite(fraction):
                    return
                with self.lock:
                    job.progress = max(job.progress, min(.999, max(0.0, float(fraction))))
                    if message and not job.cancelled.is_set():
                        job.message = str(message)[:1000]

            self.prepare(job.photo.path, job.photo.revision, job.denoise, job.sharpen,
                         progress=progress, cancelled=job.cancelled.is_set)
            job.photo.verify()
            completed = True
        except Exception as exc:
            if job.cancelled.is_set() or _cancelled_exception(exc):
                job.cancelled.set()
            else:
                failure = str(exc)[:2000] or "AI detail could not be prepared."
        finally:
            with self.lock:
                if job.cancelled.is_set():
                    job.state = "cancelled"
                    job.message = "AI enhancement cancelled. Your edits are unchanged."
                    job.error = None
                elif completed:
                    job.state = "ready"
                    job.progress = 1.0
                    job.message = "AI detail is ready."
                else:
                    job.state = "error"
                    job.error = failure or "AI detail stopped before it was ready."
                    job.message = job.error
                if self.active == job.identifier:
                    self.active = None

    def _get(self, identifier):
        if not isinstance(identifier, str) or identifier not in self.jobs:
            raise ValueError("That AI enhancement is no longer available.")
        return self.jobs[identifier]

    @staticmethod
    def _snapshot(job):
        result = {"job": job.identifier, "state": job.state, "progress": job.progress,
                  "message": job.message, "cancel_requested": job.cancelled.is_set()}
        if job.error:
            result["error"] = job.error
        return result

    def progress(self, identifier):
        with self.lock:
            return self._snapshot(self._get(identifier))

    def cancel(self, identifier):
        with self.lock:
            job = self._get(identifier)
            if job.state in ("queued", "running"):
                job.cancelled.set()
                job.message = "Cancelling AI detail after the current tile."
            elif job.state == "ready":
                job.cancelled.set()
                job.state = "cancelled"
                job.message = "AI enhancement cancelled. Your edits are unchanged."
            return self._snapshot(job)

    def close(self):
        with self.lock:
            self.closed = True
            if self.active is not None:
                self.cancel(self.active)
