"""Pause switch for the indexing: one gate all background work looks at between two steps.

Pausing never interrupts a step already running (a file being read, a batch at the embedding service,
a page at the model) – it finishes, then the work waits here until it is resumed. Nothing is lost:
what was not done is done after "Fortsetzen". The state lives in memory only, so a restart of the app
resumes the indexing (a pause is meant for hours, not for good – switch the folders off for that)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone


class PauseGate:
    POLL_SECONDS = 0.5  # how often a waiting thread looks at the gate (and at its stop event)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resumed = threading.Event()
        self._resumed.set()
        self._since: str | None = None

    @property
    def paused(self) -> bool:
        return not self._resumed.is_set()

    @property
    def since(self) -> str | None:
        return self._since

    def pause(self) -> bool:
        """Pause; False if it was paused already."""
        with self._lock:
            if self.paused:
                return False
            self._since = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._resumed.clear()
            return True

    def resume(self) -> bool:
        """Resume; False if it was not paused."""
        with self._lock:
            if not self.paused:
                return False
            self._since = None
            self._resumed.set()
            return True

    def wait(self, stop: threading.Event | None = None) -> None:
        """Return at once while running; while paused block until resumed or ``stop`` is set."""
        while not self._resumed.wait(self.POLL_SECONDS):
            if stop is not None and stop.is_set():
                return

    def snapshot(self) -> dict:
        return {"paused": self.paused, "since": self._since}


PAUSE = PauseGate()
# The background embedding alone ("Embeddings pausieren", EMBED_PAUSED=1): the model and the embedding service
# share one GPU – held back, the vision model reads more pages; full-text search finds new text meanwhile.
EMBED_PAUSE = PauseGate()
