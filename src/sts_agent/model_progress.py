"""Immediate model-call progress for console logs and session traces."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from .providers.anthropic import sanitize


class ModelProgress:
    def __init__(self, *, log=None, record=None, heartbeat_s=15.0, clock=time.monotonic):
        self.log, self.record = log, record
        self.heartbeat_s, self.clock = heartbeat_s, clock
        self._lock = threading.Lock()
        self._calls = {}
        self._retry_wait_s = 0.0

    def summary(self):
        with self._lock:
            return {
                "by_purpose": {purpose: {**totals,
                    "elapsed_s": round(totals["elapsed_s"], 3),
                    "retry_elapsed_s": round(totals["retry_elapsed_s"], 3)}
                    for purpose, totals in self._calls.items()},
                "retry_backoff_s": round(self._retry_wait_s, 3),
                "note": "Retry call time is included in each purpose's elapsed_s; backoff is separate.",
            }

    def wait_for_retry(self, purpose, attempt, *, delay_s, sleep=time.sleep, **context):
        self.emit("retry_scheduled", purpose, attempt, delay_s=delay_s, **context)
        start = self.clock()
        try:
            sleep(delay_s)
        finally:
            elapsed = self.clock() - start
            with self._lock:
                self._retry_wait_s += elapsed
            self.emit("retry_wait_finished", purpose, attempt, elapsed_s=round(elapsed, 3), **context)

    def emit(self, phase, purpose, attempt, **fields):
        if self.log is None and self.record is None:
            return
        data = {"purpose": purpose, "attempt": attempt, **fields}
        if "error" in data:
            data["error"] = sanitize(str(data["error"]))
        label = purpose
        if fields.get("screen"):
            label += " " + fields["screen"]
        if fields.get("revision") is not None:
            label += f" revision {fields['revision']}"
        label += f" attempt {attempt}"
        if fields.get("retry_kind"):
            label += f" ({fields['retry_kind']} retry)"
        elapsed = fields.get("elapsed_s", 0)
        if phase == "call_started":
            detail = "started"
        elif phase == "call_waiting":
            detail = f"waiting ({elapsed:.0f}s elapsed)"
        elif phase == "call_finished":
            detail = f"{fields['status']} after {elapsed:.1f}s"
        elif phase == "retry_scheduled":
            detail = f"retrying in {fields.get('delay_s', 0):g}s"
        elif phase == "retry_wait_finished":
            detail = f"retry wait finished after {elapsed:.1f}s"
        else:
            detail = phase.replace("_", " ")
        if data.get("error"):
            detail += ": " + data["error"]
        message = f"model: {label} {detail}"
        with self._lock:
            if self.record is not None:
                self.record("model_" + phase, data)
            if self.log is print:
                print(message, flush=True)
            elif self.log is not None:
                self.log(message)

    @contextmanager
    def call(self, purpose, attempt, **context):
        start = self.clock()
        done = threading.Event()
        self.emit("call_started", purpose, attempt, **context)

        def heartbeat():
            while not done.wait(self.heartbeat_s):
                self.emit("call_waiting", purpose, attempt,
                          elapsed_s=round(self.clock() - start, 1), **context)

        worker = None
        if self.log is not None or self.record is not None:
            worker = threading.Thread(target=heartbeat, name="model-progress", daemon=True)
            worker.start()
        status, error = "response received", None
        try:
            yield
        except BaseException as exc:
            status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
            # Categories only: raw provider exceptions may contain credentials.
            error = getattr(exc, "kind", type(exc).__name__)
            raise
        finally:
            elapsed = self.clock() - start
            done.set()
            if worker is not None:
                worker.join()
            with self._lock:
                totals = self._calls.setdefault(purpose, {"calls": 0, "elapsed_s": 0.0,
                                                         "retry_calls": 0, "retry_elapsed_s": 0.0})
                totals["calls"] += 1
                totals["elapsed_s"] += elapsed
                if context.get("retry_kind"):
                    totals["retry_calls"] += 1
                    totals["retry_elapsed_s"] += elapsed
            self.emit("call_finished", purpose, attempt, status=status,
                      elapsed_s=round(elapsed, 3),
                      **({"error": error} if error is not None else {}), **context)
