"""Embeddings in the background: sections get their vectors while the documents pass, Tesseract and the
Bildauswertung are still running, not only in the phase "Embeddings" after documents and mails.

The embedding service is a service of its own (EMBED_URL): it is idle while the documents pass reads the
share for days, and the new sections would only become findable by meaning at the end of that pass. So the
app runs :class:`EmbedWorker`, a thread with its own database connection, which embeds the sections without
a vector continuously – new documents, mail texts, and the sections the Bildauswertung writes anew:

* in rounds of up to EMBED_CONCURRENCY batches (the batch size of the embedding client, 32), sent to the
  service at the same time from short-lived threads; nothing is held in a transaction meanwhile, each batch
  is written in a short write transaction of its own;
* a vector is stored only for the section it was made from (``vectors.STORE_SQL``: still without a vector
  and still the same text) – a document written anew meanwhile deletes its sections, SQLite may hand a new
  section the same id; nothing is stored twice, nothing for a section that is gone, and a new section with a
  reused id is taken in the next round (the worker starts again at the lowest id once it reached the end);
* one embedding loop at a time in the process (``vectors.EMBED_LOCK``, per round): the phase "Embeddings" of
  an indexing pass is left out while the worker runs (``run_index(embeddings=False)``), the CLI keeps it;
* new vectors become searchable at most every ``vectors.PUBLISH_INTERVAL_S`` and when the worker has caught
  up (each publication makes every search reload its vectors once);
* the sections waiting are counted before every round (cheap through the partial index of migration 13);
* with nothing to do it sleeps until sections are written (``vectors.CHUNKS_WAITING``) or IDLE_SECONDS;
  when the service fails, it waits ERROR_SECONDS (the phase of the next pass is skipped as well – the worker
  catches up once the service answers);
* before its first batch, and after an error, it checks the fingerprint of the embedding service (another
  model: all vectors are made anew, as before).

:data:`LIVE` holds what it does for the status page: sections embedded and waiting, the project and file of
the last batch, and the moving average per section (zeiten.EMBED_KEY) for the rate and the rough time left.
"""
from __future__ import annotations

import logging
import threading
import time

from . import db, vectors, zeiten
from .ai import AIServiceError
from .pause import PAUSE

log = logging.getLogger("projektakte.embed_worker")


class EmbedLive:
    """What the background embedding does right now (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.enabled = False
            self.concurrency = 0
            self.busy = False
            self.in_flight = 0  # batches with the service right now
            self.done = 0  # sections embedded since the app started
            self.pending: int | None = None
            self.current: dict | None = None  # project and file of the last batch
            self.error: str | None = None

    def enable(self, concurrency: int) -> None:
        with self._lock:
            self.enabled, self.concurrency = True, concurrency

    def update(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def embedded(self, n: int) -> None:
        with self._lock:
            self.done += n
            if self.pending is not None:
                self.pending = max(0, self.pending - n)

    def snapshot(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "concurrency": self.concurrency, "busy": self.busy,
                    "in_flight": self.in_flight, "done": self.done, "pending": self.pending,
                    "current": dict(self.current) if self.current else None, "error": self.error}


LIVE = EmbedLive()


class EmbedWorker:
    """The background embedding of the app (see the module docstring)."""

    IDLE_SECONDS = 30.0  # nothing to embed: look again after this (or when sections are written)
    ERROR_SECONDS = 60.0  # the service failed

    def __init__(self, settings, client, *, live: EmbedLive | None = None):
        self.settings, self.client = settings, client
        self.live = live or LIVE
        self.concurrency = max(1, int(getattr(settings, "embed_concurrency", 2)))
        self.batch = max(1, int(getattr(client, "batch_size", 32)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self._after = 0  # id cursor of the current sweep
        self._checked = False  # fingerprint checked
        self._unpublished = 0
        self._published = time.monotonic()

    @staticmethod
    def wanted(client) -> bool:
        return client is not None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.live.reset()
        self.live.enable(self.concurrency)
        self._thread = threading.Thread(target=self._loop, name="einbetten", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Ends after the batches with the service now (their vectors are dropped if it takes longer than
        ``timeout``; the sections stay without a vector and are embedded after the next start)."""
        self._stop.set()
        vectors.CHUNKS_WAITING.set()
        if self._thread:
            self._thread.join(timeout)
        self.live.reset()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        conn = None
        try:
            while not self._stop.is_set():
                PAUSE.wait(self._stop)
                if self._stop.is_set():
                    break
                vectors.CHUNKS_WAITING.clear()
                try:
                    if conn is None:
                        db.init_db(self.settings.db_path)
                        conn = db.connect(self.settings.db_path)
                    worked = self.run_round(conn)
                    self.last_error = None
                    self.live.update(error=None)
                    pause = 0.0 if worked else self.IDLE_SECONDS
                except AIServiceError as exc:
                    log.warning("Embeddings im Hintergrund: %s – neuer Versuch in %.0f s", exc, self.ERROR_SECONDS)
                    self._failed(conn, str(exc))
                    pause = self.ERROR_SECONDS
                except Exception as exc:  # noqa: BLE001 – the thread must survive any single failure
                    log.exception("Embeddings im Hintergrund fehlgeschlagen")
                    self._failed(conn, f"{type(exc).__name__}: {exc}")
                    pause = self.ERROR_SECONDS
                if pause and not self._stop.is_set():
                    self.live.update(busy=False, in_flight=0)
                    vectors.CHUNKS_WAITING.wait(pause)
        finally:
            if conn is not None:
                try:
                    self._publish(conn, force=True)
                except Exception:  # noqa: BLE001 – shutting down
                    pass
                conn.close()

    def _failed(self, conn, message: str) -> None:
        self.last_error = message
        self._checked = False
        self.live.update(error=message, busy=False, in_flight=0)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    def _publish(self, conn, *, force: bool = False) -> None:
        if self._unpublished and (force or time.monotonic() - self._published >= vectors.PUBLISH_INTERVAL_S):
            vectors.bump_generation(conn)
            conn.commit()
            self._unpublished, self._published = 0, time.monotonic()

    def run_round(self, conn) -> bool:
        """One round: up to EMBED_CONCURRENCY batches at a time. False if there was nothing to embed."""
        with vectors.EMBED_LOCK:
            if not self._checked:
                if vectors.check_fingerprint(conn, self.client):
                    self._after = 0
                conn.commit()
                self._checked = True
            self.live.update(pending=vectors.count_pending(conn))  # cheap: the partial index of migration 13
            rows = vectors.pending_rows(conn, self._after, self.batch * self.concurrency)
            if not rows:
                if self._after:  # end of the sweep: once more from the start (ids reused meanwhile)
                    self._after = 0
                    rows = vectors.pending_rows(conn, 0, self.batch * self.concurrency)
                if not rows:
                    self._publish(conn, force=True)
                    self.live.update(busy=False, pending=0, current=None)
                    return False
            self._after = rows[-1]["id"]
            batches = [rows[i:i + self.batch] for i in range(0, len(rows), self.batch)]
            last = rows[-1]
            self.live.update(busy=True, in_flight=len(batches),
                             current={"project": last["project"], "file": last["filename"]})
            started = time.monotonic()
            results = self._embed_all(batches)
            seconds = time.monotonic() - started
            stored = 0
            for batch, vecs in zip(batches, results):
                if isinstance(vecs, BaseException):
                    raise vecs
                conn.execute("BEGIN IMMEDIATE")
                try:
                    stored += vectors.store_vectors(conn, batch, vecs)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            self.live.update(in_flight=0)
            zeiten_add(conn, seconds, len(rows))
            self.live.embedded(stored)
            self._unpublished += stored
            self._publish(conn)
            return True

    def _embed_all(self, batches: list[list]) -> list:
        """The batches at the same time (at most EMBED_CONCURRENCY, one thread each); a result is the list
        of vectors or the exception of that batch."""
        texts = [[vectors.embedding_text(r["filename"] or ("Mail" if r["heading"] else None), r["project_name"],
                                         r["heading"], r["text"]) for r in b] for b in batches]
        out: list = [None] * len(batches)

        def one(i: int) -> None:
            try:
                out[i] = self.client.embed(texts[i])
            except BaseException as exc:  # noqa: BLE001 – handed to the caller
                out[i] = exc

        threads = [threading.Thread(target=one, args=(i,), name="einbetten-stapel", daemon=True)
                   for i in range(1, len(batches))]
        for t in threads:
            t.start()
        one(0)
        for t in threads:
            t.join()
        return out


def zeiten_add(conn, seconds: float, sections: int) -> None:
    """The moving average per section with EMBED_CONCURRENCY batches at a time (the time of the round)."""
    zeiten.add(conn, zeiten.EMBED_KEY, seconds, sections)
    conn.commit()
