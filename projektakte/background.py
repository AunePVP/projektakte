"""Background indexing: runs at start-up and every INDEX_INTERVAL seconds in a daemon thread, so
the web server starts immediately and keeps answering while documents and mails are processed."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import demo, extract_worker
from .ai.services import AIServices
from .config import Settings
from .demo_cleanup import demo_purge_note
from .indexer import run_index

log = logging.getLogger("projektakte.background")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_warnings(stats: dict | None) -> list[str]:
    """Warnings and notes of an indexing pass (German, e.g. PROJECTS_DIR not mounted, demo data
    removed) for the status page."""
    stats = stats or {}
    out = [str(v["warning"]) for v in stats.values() if isinstance(v, dict) and v.get("warning")]
    note = demo_purge_note(stats.get("demo_purge"))
    return [note, *out] if note else out


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
                            "last_warnings": [], "next_run": None, "demo_generated": None}

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
        # the extraction worker process ends with the app: a file being read is abandoned at once (it is
        # read again on the next start) instead of holding up the shutdown
        extract_worker.shutdown()
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
            warnings = run_warnings(stats)
            purged = demo_purge_note(stats.get("demo_purge"))
            for w in warnings:
                if w != purged:  # the demo purge logged its note already
                    log.warning("Indexierung: %s", w)
            self._set(last_stats=stats, last_error=None, last_warnings=warnings, last_success=_now())
            return stats
        except extract_worker.ExtractionAborted:  # the app shuts down: the pass ends, nothing failed
            log.info("Indexierung beim Beenden abgebrochen – angefangene Dateien werden beim nächsten Start "
                     "gelesen")
            return None
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
