"""Background indexing: runs at start-up and every INDEX_INTERVAL seconds in a daemon thread, so
the web server starts immediately and keeps answering while documents and mails are processed."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import demo
from .ai.services import AIServices
from .config import Settings
from .indexer import run_index

log = logging.getLogger("projektakte.background")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BackgroundIndexer:
    def __init__(self, settings: Settings, ai: AIServices):
        self.settings = settings
        self.ai = ai
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.state: dict = {"running": False, "phase": "wartet", "runs": 0, "last_started": None,
                            "last_finished": None, "last_stats": None, "last_error": None,
                            "next_run": None, "demo_generated": None}

    # -- control ---------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="indexer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def trigger(self) -> None:
        """Run as soon as possible (e.g. after POST /api/reindex)."""
        self._wake.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        with self._lock:
            return {**self.state, "alive": self.alive, "interval_s": self.settings.index_interval}

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    # -- work ------------------------------------------------------------------------------------
    def run_once(self) -> dict | None:
        self._set(running=True, last_started=_now(), phase="startet")
        try:
            stats = run_index(self.settings, self.ai, progress=lambda phase: self._set(phase=phase))
            self._set(last_stats=stats, last_error=None)
            return stats
        except Exception as exc:  # noqa: BLE001 – the worker must survive any single failure
            log.exception("Indexierung fehlgeschlagen")
            self._set(last_error=f"{type(exc).__name__}: {exc}")
            return None
        finally:
            with self._lock:
                self.state["runs"] += 1
                self.state.update(running=False, phase="wartet", last_finished=_now())

    def _loop(self) -> None:
        if self.settings.demo_mode:
            try:
                generated = demo.ensure(self.settings.demo_dir)
                self._set(demo_generated=generated)
                if generated:
                    log.info("DEMO_MODE: Demo-Datensatz in %s erzeugt", self.settings.demo_dir)
            except Exception as exc:  # noqa: BLE001
                log.exception("Demo-Daten konnten nicht erzeugt werden")
                self._set(last_error=f"Demo-Daten: {exc}")
        while not self._stop.is_set():
            self._wake.clear()
            self.run_once()
            interval = self.settings.index_interval
            if interval <= 0:  # only on start-up and on demand
                self._set(next_run=None)
                self._wake.wait()
            else:
                self._set(next_run=datetime.fromtimestamp(time.time() + interval, timezone.utc)
                          .isoformat(timespec="seconds"))
                self._wake.wait(interval)
