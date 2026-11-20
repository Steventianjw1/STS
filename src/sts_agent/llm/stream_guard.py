"""Ends a streamed model call that has gone quiet, or that the session wants stopped.

A streamed response is read in a blocking socket wait. The SDK's timeout bounds each
wait for bytes, so it never fires while the server keeps the connection alive with
keep-alive comments or pings and sends no text. The guard is a thread beside that
read: when no text has arrived for the limit, or ``should_stop`` says so, it closes
the response, which makes the blocked read raise in the reading thread. The reader
then finds the reason in ``ended`` and reports that instead of the SDK's error.

Two limits, because the waits differ: before the first text a provider may hold a
request in its queue, after it the text normally arrives without long gaps.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .base import ProviderError

POLL_S = 0.25   # how often the limits and the stop request are checked


class StreamEnded(ProviderError):
    """The guard ended the call. ``kind`` is ``timeout`` (went quiet) or ``interrupted`` (stop requested)."""


def _limit(seconds) -> float | None:
    return float(seconds) if seconds is not None and seconds > 0 else None


class StreamGuard:
    """Use as a context manager around one streamed call; ``attach`` the stream once it exists."""

    def __init__(self, *, first_text_s: float | None = None, idle_s: float | None = None,
                 should_stop: Callable[[], bool] | None = None, poll_s: float = POLL_S,
                 clock: Callable[[], float] = time.monotonic):
        self._first_s, self._idle_s = _limit(first_text_s), _limit(idle_s)
        self._should_stop, self._poll_s, self._clock = should_stop, poll_s, clock
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._close: Callable[[], None] | None = None
        self._seen, self._last = False, clock()   # any text yet; when the call began or text last arrived
        self.ended: StreamEnded | None = None      # set when the guard ended the call
        self.longest_quiet_s: float | None = None  # longest wait for more text after the first

    def __enter__(self) -> "StreamGuard":
        self._last = self._clock()
        if self._first_s is not None or self._idle_s is not None or self._should_stop is not None:
            threading.Thread(target=self._run, name="stream-guard", daemon=True).start()
        return self

    def __exit__(self, *exc) -> bool:
        with self._lock:   # after this the guard thread never touches the stream
            self._done.set()
            if self._seen:
                self._quiet(self._clock())
        return False

    def attach(self, stream) -> None:
        """The stream to close. A call the guard already ended while connecting is closed at once."""
        close = getattr(stream, "close", None)
        with self._lock:
            self._close = close if callable(close) else None
            ended = self.ended
        if ended is not None:
            self._shut()

    def text(self) -> None:
        """The reader calls this for every piece of reasoning or answer text."""
        now = self._clock()
        with self._lock:
            if self._seen:
                self._quiet(now)
            self._seen, self._last = True, now

    def _quiet(self, now: float) -> None:
        self.longest_quiet_s = round(max(self.longest_quiet_s or 0.0, now - self._last), 3)

    def _due(self) -> StreamEnded | None:
        if self._should_stop is not None:
            try:
                if self._should_stop():
                    return StreamEnded("interrupted", "stop requested while the model call was in flight")
            except Exception:
                pass   # a broken stop hook must never fail a model call
        with self._lock:
            seen, quiet = self._seen, self._clock() - self._last
        limit = self._idle_s if seen else self._first_s
        if limit is None or quiet < limit:
            return None
        return StreamEnded("timeout", f"the stream went quiet, no text for {limit:g}s" if seen
                           else f"no text in the first {limit:g}s")

    def _run(self) -> None:
        while not self._done.wait(self._poll_s):
            reason = self._due()
            if reason is None:
                continue
            with self._lock:
                if self._done.is_set():
                    return   # the call finished by itself first
                self.ended = reason
            self._shut()
            return

    def _shut(self) -> None:
        close = self._close
        if close is not None:
            try:
                close()
            except Exception:
                pass
