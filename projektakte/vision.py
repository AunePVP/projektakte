"""Phase "Bildauswertung": the multimodal model reads the scan pages of the documents (stage 2).

In an indexing pass a file is read in two stages (see extract.py, ``defer_vision``):

1. The documents and mail passes store it at once – its text layers, and for scan pages without one
   Tesseract's text (if installed) – so it is searchable right away. Its scan pages are listed as
   pending (``documents.vision_pending``, ``page_texts``): "Bildauswertung ausstehend".
2. The model reads them – in the app in the background, beside the documents pass (:class:`VisionWorker`,
   a thread of its own, from the start of the app and between the indexing passes), so the model works
   while Tesseract reads the share; in a pass without the app (``python -m projektakte.index``, tests) as
   the last phase of the pass (:func:`run_vision`). Pinned projects and the folder order first (files of
   folders switched off not at all), newest documents first inside, up to VISION_CONCURRENCY calls at a time
   (the AI server takes a few at once and answers the questions as well, so one of its places stays free).
   A run (:class:`VisionPass`) lasts at most VISION_PASS_SECONDS; the background worker starts the next one
   at once – it then takes failed pages whose wait is over and documents it skipped. The model gets the page
   image *and* the page's text (OCR layer or Tesseract), its answer replaces that text; the document's
   sections are written again (only that document's), and their vectors are made by the next embedding run.

A page result is stored only if the file is still the one the page was taken from (``sha256``, checked in
the same write transaction): if the documents pass read a changed file meanwhile, its new pages wait for
the model and the old answer is dropped.

The page images are decoded and prepared by an extraction worker under its limits (untrusted PDF data is
never parsed in the app process); the background worker has a worker process of its own, so it does not
wait behind a scan Tesseract is reading. The app only sends the finished JPEG/PNG bytes to the model, from
a few daemon threads. While the model works, the phase also checks complete PDFs read by an earlier
version without the model for scan pages with a text layer (``recheck``) – their handwriting is only in the
image.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field
from pathlib import Path

from . import db, documents, zeiten
from . import extract as extract_mod
from . import extract_worker, reihenfolge
from .ai.http import seconds_text
from .chunking import chunk_pages
from .documents import (LIMIT_MARK, _read_inside, _sqlite_time, _utcnow, document_date, pending_message, printable,
                        real_folder, retry_delay, sha256, store_page_texts, stored_profile, write_chunks)
from .extract import IMAGE_EXT, UNCHANGED, Page, extraction_profile, vision_prompt

log = logging.getLogger("projektakte.vision")

BATCH_PAGES = 4  # pages of a PDF whose images are prepared by one request to the extraction worker
_WAIT_SECONDS = 1.0  # waiting for a model answer in slices (the app's shutdown is noticed)
_RECHECK_BATCH = 100  # candidates for the re-check fetched at a time
WAITING = ("pending", "failed")  # page states that still wait for the model
NO_TEXT = "kein Text"  # note of a page the model read without finding text (its text so far stands)


def pending_counts(conn) -> dict:
    """Pages and documents waiting for the model, and the PDFs read earlier that are still to be checked
    for scan pages with a text layer (status page)."""
    pages, docs = conn.execute(
        "SELECT COALESCE(SUM(vision_pending), 0), COUNT(*) FROM documents WHERE vision_pending > 0").fetchone()
    return {"pages": pages, "documents": docs, "recheck": conn.execute(_RECHECK_COUNT).fetchone()[0]}


_RECHECK_WHERE = ("ext = '.pdf' AND incomplete = 0 AND status = 'ok' AND COALESCE(method, 'text') = 'text' AND "
                  "vision_pending = 0 AND COALESCE(extract_profile, '') NOT LIKE "
                  f"'v{extract_mod.EXTRACT_VERSION}:%'")
_RECHECK_COUNT = f"SELECT COUNT(*) FROM documents WHERE {_RECHECK_WHERE}"
_ORDER = "ORDER BY COALESCE(doc_date, '') DESC, COALESCE(mtime, 0) DESC, id DESC"


@dataclass
class _Task:
    """One page for the model."""
    doc_id: int
    page: int
    name: str
    known: str  # the page's text so far (OCR layer or Tesseract)
    sha: str | None = None  # the file the page was taken from (a result for another version is dropped)
    images: list = field(default_factory=list)  # (mime, data)
    partial: str | None = None  # images of the page left out, and why (see extract.PageImages)
    retry: bool = False
    limit: str | None = None


@dataclass
class _Doc:
    """The document whose pages are being prepared."""
    row: dict
    data: bytes
    due: deque  # (page number, text so far) of the pages still to prepare, in page order
    open: int = 0  # its tasks queued or with the model


class VisionPass:
    """One run of the phase within its time budget (see the module docstring).

    ``worker``: the extraction worker preparing the page images (default: the one of the running indexing
    pass); ``stop``: an event that ends the run at once (app shutdown; answers on their way are dropped, the
    pages wait for the next start); ``refresh``: seconds after which the documents waiting are read again (new
    ones of the documents pass running meanwhile; 0: only at the start); ``live``: a :class:`VisionLive` the
    status page reads."""

    def __init__(self, conn, llm, *, seconds: float = 1800.0, concurrency: int = 3, projects_dir=None,
                 progress=None, clock=time.monotonic, prepare=None, extract=None, order=None,
                 worker=extract_worker._PASS, stop: threading.Event | None = None, refresh: float = 0.0,
                 live: "VisionLive | None" = None):
        self.conn, self.llm = conn, llm
        self.seconds = float(seconds)
        self.concurrency = max(1, int(concurrency))
        self.boundary = Path(projects_dir).resolve() if projects_dir else None
        self.progress = progress
        self.order = order  # reihenfolge.VisionOrder: pinned projects and the folder order first
        self.worker, self.stop, self.refresh, self.live = worker, stop, float(refresh), live
        self._project: dict[int, tuple] = {}  # document id → (project, mail id)
        self._sha: dict[int, str | None] = {}  # document id → the file its pages were taken from
        self._loaded = 0.0
        self.clock = clock
        self.prepare = prepare or extract_mod.page_images
        self.extract = extract or extract_mod.extract
        self.profile = extraction_profile(llm)
        self.stats = {"pages": 0, "failed": 0, "not_readable": 0, "documents": 0, "checked": 0, "found": 0,
                      "discarded": 0, "stopped": False}
        self._ids: deque = deque()
        self._seen: set[int] = set()
        self._doc: _Doc | None = None
        self._docs: dict[int, _Doc] = {}  # documents with tasks queued or with the model
        self._tasks: deque = deque()
        self._touched: set[int] = set()  # documents with a new page result not yet written
        self._recheck: deque = deque()
        self._recheck_seen: set[int] = set()
        self._recheck_done = False
        self._end = 0.0

    # -- the run -------------------------------------------------------------------------------------
    def run(self) -> dict:
        counts = pending_counts(self.conn)
        if not counts["pages"] and not counts["recheck"]:
            if self.live is not None:
                self.live.pending(0, 0)
            return {**self.stats, "pending_pages": 0, "pending_documents": 0, "recheck": 0}
        log.info("Bildauswertung: %d Seite(n) in %d Dokument(en) ausstehend%s – bis zu %d gleichzeitig, Zeitbudget %s",
                 counts["pages"], counts["documents"],
                 f", {counts['recheck']} ältere PDF-Datei(en) zu prüfen" if counts["recheck"] else "",
                 self.concurrency, f"{seconds_text(self.seconds)} s" if self.seconds > 0 else "unbegrenzt")
        self._end = self.clock() + self.seconds if self.seconds > 0 else float("inf")
        self._load_ids()
        pool = _Pool()  # (app shutdown: answers still on their way are not waited for – daemon threads)
        inflight: dict[Future, _Task] = {}
        while True:
            self._check_stop()
            while len(inflight) < self.concurrency and self._time_left():
                task = self._next_task()
                if task is None:
                    break
                inflight[pool.submit(self._call, task)] = task
                if self.live is not None:
                    self.live.calls(len(inflight), task.name, task.page)
            if not inflight:
                if self._time_left() and self._recheck_one():
                    continue
                break
            # the model is busy: meanwhile an older file is checked for scan pages (cheap, no model)
            if len(inflight) >= self.concurrency and self._time_left() and self._recheck_one():
                done = [f for f in inflight if f.done()]
            else:
                done, _pending = wait(inflight, timeout=_WAIT_SECONDS, return_when=FIRST_COMPLETED)
            for future in done:
                self._finish(inflight.pop(future), future)
            if done and self.live is not None:
                self.live.calls(len(inflight))
        for doc_id in sorted(self._touched):
            self._write(doc_id)
        self.stats["stopped"] = not self._time_left()
        counts = pending_counts(self.conn)
        self.stats.update(pending_pages=counts["pages"], pending_documents=counts["documents"],
                          recheck=counts["recheck"])
        log.info("Bildauswertung: %d Seite(n) gelesen%s%s, noch %d Seite(n) in %d Dokument(en) ausstehend%s",
                 self.stats["pages"], f", {self.stats['failed']} fehlgeschlagen" if self.stats["failed"] else "",
                 f", {self.stats['checked']} ältere PDF-Datei(en) geprüft ({self.stats['found']} mit Scans)"
                 if self.stats["checked"] else "", counts["pages"], counts["documents"],
                 " – Zeitbudget erreicht, weiter im nächsten Durchlauf" if self.stats["stopped"] and counts["pages"]
                 else "")
        return self.stats

    def _load_ids(self) -> None:
        """The documents waiting for the model, in their order – without those of folders switched off and
        those this run took already."""
        rows = self.conn.execute(
            f"SELECT id, project, mail_id, vision_pending FROM documents WHERE vision_pending > 0 {_ORDER}").fetchall()
        self._project = {r[0]: (r[1], r[2]) for r in rows}
        ids = self.order.sort([(r[0], r[1], r[2]) for r in rows]) if self.order else [r[0] for r in rows]
        kept = set(ids)
        self._ids = deque(i for i in ids if i not in self._seen)
        self._loaded = self.clock()
        if self.live is not None:
            pages = sum(r[3] for r in rows if r[0] in kept)
            self.live.pending(pages, sum(r[3] for r in rows) - pages)

    def _time_left(self) -> bool:
        return self.clock() < self._end

    def _check_stop(self) -> None:
        if self.stop is not None and self.stop.is_set():  # the app shuts down
            raise extract_worker.ExtractionAborted("Bildauswertung wird beendet")
        worker = extract_worker.current() if self.worker is extract_worker._PASS else self.worker
        if worker is not None and worker.closed:  # the app shuts down
            raise extract_worker.ExtractionAborted("Leseprozess wurde beendet")

    def _begin(self) -> None:
        """A write transaction of its own, taken at once (the check of the file and the write belong
        together: the documents pass may write the same document meanwhile)."""
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")

    def _same_file(self, doc_id: int, sha: str | None) -> bool:
        if sha is None:
            return True
        row = self.conn.execute("SELECT sha256 FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return row is not None and row[0] == sha

    def _call(self, task: _Task) -> tuple[str, float]:
        """In a thread of the pool: the model reads the page (each image of it, with the page's text)."""
        started = time.monotonic()
        answers = []
        prompt = vision_prompt(task.known)
        for mime, data in task.images:
            text = (self.llm.describe_image(data, mime, prompt).text or "").strip()
            if text:
                answers.append(text)
        return "\n".join(answers), time.monotonic() - started

    # -- preparing the pages -----------------------------------------------------------------------------
    def _next_task(self) -> _Task | None:
        while not self._tasks:
            if self._doc is None or not self._doc.due:
                if self._doc is not None and not self._doc.open:
                    self._docs.pop(self._doc.row["id"], None)
                self._doc = self._next_doc()
                if self._doc is None:
                    return None
            self._prepare(self._doc)
        return self._tasks.popleft()

    def _next_doc(self) -> _Doc | None:
        now = _sqlite_time(_utcnow())
        while True:
            if self.refresh > 0 and self.clock() - self._loaded >= self.refresh:
                self._load_ids()  # new documents of the documents pass running meanwhile, in their place
            elif self.order is not None and self.order.stale():  # the order was changed on the status page
                self._ids = deque(self.order.sort([(i, *self._project.get(i, (None, None))) for i in self._ids]))
            if not self._ids:
                return None
            doc_id = self._ids.popleft()
            if doc_id in self._seen:
                continue
            self._seen.add(doc_id)
            row = self.conn.execute(
                "SELECT id, project, path, rel_path, filename, ext, sha256, cache_path, mail_id, vision_pending "
                "FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if row is None or not row["vision_pending"]:
                continue
            if self.order is not None and self.order.skipped(row["project"], row["mail_id"]):
                continue  # its folder was switched off meanwhile
            due = deque(self.conn.execute(
                "SELECT page, text FROM page_texts WHERE doc_id = ? AND (state = 'pending' OR (state = 'failed' AND "
                "(retry_after IS NULL OR retry_after <= ?))) ORDER BY page", (doc_id, now)).fetchall())
            if not due:
                if not self.conn.execute(f"SELECT 1 FROM page_texts WHERE doc_id = ? AND state IN {WAITING} LIMIT 1",
                                         (doc_id,)).fetchone():
                    # every page was read, but the document was not put together (the app ended in between)
                    self._sha[doc_id] = row["sha256"]
                    self._touched.add(doc_id)
                    self._write(doc_id)
                continue
            data = self._read(dict(row))
            if data is None:
                continue
            doc = _Doc(dict(row), data, deque((r["page"], r["text"]) for r in due))
            self._sha[doc_id] = row["sha256"]
            number = row["project"]
            folder = self.order.project_of(number) if self.order is not None else ""
            if self.live is not None:
                self.live.project(number, folder)
            elif self.order is not None:
                reihenfolge.LIVE.vision_project(number, folder)
            self._docs[doc_id] = doc
            return doc

    def _read(self, row: dict) -> bytes | None:
        """The file's bytes now – None if it cannot be read or changed since it was stored (then the
        documents pass reads it anew)."""
        name = printable(row["rel_path"])
        try:
            if row["mail_id"] is not None:
                if not row["cache_path"]:
                    raise FileNotFoundError("kein Cache-Pfad")
                data = Path(row["cache_path"]).read_bytes()
            else:
                stored = Path(row["path"])
                folder = real_folder(stored.parent)
                if folder is None:
                    raise FileNotFoundError("Ordner nicht erreichbar")
                path = folder / stored.name
                data = _read_inside(path, self.boundary) if self.boundary else path.read_bytes()
        except OSError as exc:
            log.warning("Bildauswertung: %s nicht lesbar (%s) – später erneut", name, exc)
            return None
        if sha256(data) != row["sha256"]:
            log.info("Bildauswertung: %s wurde geändert – wird zuerst neu gelesen", name)
            return None
        return data

    def _prepare(self, doc: _Doc) -> None:
        """Tasks for the next pages of ``doc``: an image file as it is, the pages of a PDF prepared by the
        extraction worker a few at a time (their images stay in memory only until the model has them)."""
        row = doc.row
        name = printable(row["rel_path"])
        if row["ext"] in IMAGE_EXT:
            number, known = doc.due.popleft()
            self._queue(doc, _Task(row["id"], number, name, known, row["sha256"], [(IMAGE_EXT[row["ext"]], doc.data)]))
            doc.due.clear()
            return
        batch = [doc.due.popleft() for _ in range(min(BATCH_PAGES, len(doc.due)))]
        known = dict(batch)
        prepared = extract_worker.page_images(row["filename"], doc.data, [n for n, _t in batch], self.prepare,
                                              worker=self.worker)
        for page in prepared:
            if page.images:
                self._queue(doc, _Task(row["id"], page.number, name, known.get(page.number, ""), row["sha256"],
                                       page.images, page.partial, page.retry, page.limit))
            else:
                log.warning("Bildauswertung: %s Seite %d nicht auswertbar (%s)", name, page.number, page.problem)
                self._store(row["id"], page.number, problem=page.problem or "kein Bild", retry=page.retry,
                            limit=page.limit, sha=row["sha256"])

    def _queue(self, doc: _Doc, task: _Task) -> None:
        doc.open += 1
        self._tasks.append(task)

    # -- results -----------------------------------------------------------------------------------------
    def _finish(self, task: _Task, future: Future) -> None:
        try:
            text, seconds = future.result()
        except Exception as exc:  # noqa: BLE001 – the model failed (timeout, server down): later again
            log.warning("Bildauswertung: %s Seite %d fehlgeschlagen (%s)", task.name, task.page, exc)
            self._store(task.doc_id, task.page, error=str(exc), sha=task.sha)
        else:
            log.info("Bildauswertung: %s Seite %d gelesen (%.0f s)%s", task.name, task.page, seconds,
                     "" if text else " – kein Text")
            self._store(task.doc_id, task.page, text=text, partial=task.partial, retry=task.retry, limit=task.limit,
                        sha=task.sha, seconds=seconds)
        doc = self._docs.get(task.doc_id)
        if doc is not None:
            doc.open -= 1
            if not doc.open and not doc.due:  # all its pages of this pass are read: written at once
                self._docs.pop(task.doc_id, None)
                self._write(task.doc_id)
        if self.progress:
            self.progress(f"Bildauswertung – {self.stats['pages']} Seite(n) gelesen")

    def _store(self, doc_id: int, page: int, *, text: str | None = None, error: str | None = None,
               problem: str | None = None, retry: bool = False, partial: str | None = None,
               limit: str | None = None, sha: str | None = None, seconds: float | None = None) -> None:
        """Record the outcome of one page (committed at once: an interrupted pass loses no page read) – only
        if the file is still the one the page was taken from (``sha``): else the documents pass has read
        a changed file meanwhile, its pages wait anew and this answer is dropped."""
        self._begin()
        if not self._same_file(doc_id, sha):
            self.conn.rollback()
            self.stats["discarded"] += 1
            log.info("Bildauswertung: Dokument %d wurde inzwischen neu gelesen – Ergebnis von Seite %d verworfen",
                     doc_id, page)
            return
        if error is not None or (problem is not None and retry):
            row = self.conn.execute("SELECT attempts FROM page_texts WHERE doc_id = ? AND page = ?",
                                    (doc_id, page)).fetchone()
            attempts = (row[0] if row else 0) + 1
            self.conn.execute(
                "UPDATE page_texts SET state = 'failed', attempts = ?, retry_after = ?, note = ?, size_limit = ? "
                "WHERE doc_id = ? AND page = ?", (attempts, _sqlite_time(_utcnow() + retry_delay(attempts)),
                                                  (error or problem)[:300], limit, doc_id, page))
            self.stats["failed"] += 1
        elif problem is not None:  # nothing the model could be given, for good: the text so far stands
            self.conn.execute("UPDATE page_texts SET state = 'skipped', note = ?, size_limit = ? WHERE doc_id = ? "
                              "AND page = ?", (problem[:300], limit, doc_id, page))
            self.stats["not_readable"] += 1
            if self.live is not None:
                self.live.page_done(None)
        else:
            note = partial or (None if text else NO_TEXT)
            self.conn.execute(
                "UPDATE page_texts SET state = 'done', text = CASE WHEN ? != '' THEN ? ELSE text END, note = ?, "
                "partial = ?, retry = ?, size_limit = ? WHERE doc_id = ? AND page = ?",
                (text or "", text or "", note, int(partial is not None), int(partial is not None and retry), limit,
                 doc_id, page))
            self.stats["pages"] += 1
            if seconds is not None:  # the moving average for the estimate on the status page
                zeiten.add(self.conn, zeiten.PAGE_KEY, seconds)
            if self.live is not None:
                self.live.page_done(seconds)
        self.conn.commit()
        self._touched.add(doc_id)

    def _write(self, doc_id: int) -> None:
        """Put the document together again from its page texts: sections (and full-text rows) anew; once no
        page waits any more, its message and completeness are those of the first stage plus the pages that
        could not be read (or only partly) – retried like a file read in one go, and read again once a
        limit of the worker that cut them short is raised – and its page texts are dropped."""
        if doc_id not in self._touched:
            return
        self._touched.discard(doc_id)
        self._begin()
        try:
            self._write_document(doc_id)
        finally:
            if self.conn.in_transaction:
                self.conn.commit()

    def _write_document(self, doc_id: int) -> None:
        doc = self.conn.execute(
            "SELECT d.*, p.name AS project_name FROM documents d LEFT JOIN projects p ON p.number = d.project "
            "WHERE d.id = ?", (doc_id,)).fetchone()
        if doc is None:
            return
        if self._sha.get(doc_id, doc["sha256"]) != doc["sha256"]:  # read anew meanwhile: its pages wait again
            log.info("Bildauswertung: %s wurde inzwischen neu gelesen – wird nicht zusammengesetzt",
                     printable(doc["rel_path"]))
            return
        rows = self.conn.execute("SELECT * FROM page_texts WHERE doc_id = ? ORDER BY page", (doc_id,)).fetchall()
        if not rows:
            return
        pages = [Page(r["page"], r["text"]) for r in rows if r["text"].strip()]
        waiting = sum(r["state"] in WAITING for r in rows)
        skipped = [r for r in rows if r["state"] == "skipped"]
        partial = [r for r in rows if r["state"] == "done" and r["partial"]]
        read = any(r["state"] == "done" and r["note"] != NO_TEXT for r in rows)
        chunks = chunk_pages(pages)
        self.conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        write_chunks(self.conn, doc_id, chunks, filename=doc["filename"], project=doc["project"],
                     project_name=doc["project_name"], mail_id=doc["mail_id"])
        note = doc["vision_note"]
        profile, attempts, retry_after = doc["extract_profile"], doc["attempts"], doc["retry_after"]
        if waiting:
            message, incomplete = pending_message(note, waiting), 1
        else:
            notes = [note] if note else []
            if skipped and not chunks:
                notes.insert(0, f"Scan ohne Textebene – {skipped[0]['note']}")
            elif skipped:
                notes.append(f"{len(skipped)} Seite(n) nicht ausgewertet ({skipped[0]['note']})")
            if partial:
                notes.append(f"{len(partial)} Seite(n) nur teilweise gelesen ({partial[0]['note']})")
            message = "; ".join(notes) or None
            incomplete = int(bool(doc["vision_incomplete"] or skipped or partial))
            if any(r["retry"] for r in partial):  # e.g. a component missing: the file is read again later
                attempts = (attempts or 0) + 1
                retry_after = _sqlite_time(_utcnow() + retry_delay(attempts))
            limit = next((r["size_limit"] for r in skipped + partial if r["size_limit"]), None)
            if limit:  # read again once that limit of the worker is raised (documents.reprocess_due)
                profile = f"{(profile or '').partition(LIMIT_MARK)[0]}{LIMIT_MARK}{limit}"
        status = "ok" if chunks else ("skipped" if waiting or skipped else "empty")
        if status == "empty" and not message:
            message = "kein Text gefunden"
        method = "vision" if read or doc["method"] == "vision" else doc["method"]
        doc_date = doc["doc_date"]
        if doc["mail_id"] is None:  # a date in the text the model read (mail attachments: the mail's date)
            doc_date = document_date(doc["filename"], "\n\n".join(p.text for p in pages), doc["mtime"])
        self.conn.execute(
            "UPDATE documents SET status = ?, message = ?, method = ?, incomplete = ?, vision_pending = ?, doc_date = ?, "
            "pages = MAX(COALESCE(pages, 0), ?), extract_profile = ?, attempts = ?, retry_after = ?, "
            "indexed_at = datetime('now') WHERE id = ?",
            (status, message, method, incomplete, waiting, doc_date, max((p.number for p in pages), default=0),
             profile, attempts, retry_after, doc_id))
        if not waiting:
            self.conn.execute("DELETE FROM page_texts WHERE doc_id = ?", (doc_id,))
            self.stats["documents"] += 1

    # -- older PDFs: scan pages with a text layer ---------------------------------------------------------
    def _recheck_one(self) -> bool:
        """Check one complete PDF read by an earlier version without the model for scan pages with a text
        layer (their handwriting is only in the image): found, they wait for the model like those of a new
        file – its sections stay as they are until then. Returns False if there is none to check."""
        while True:
            if not self._recheck:
                if self._recheck_done:
                    return False
                rows = self.conn.execute(
                    f"SELECT id, project, path, rel_path, filename, ext, sha256, cache_path, mail_id FROM documents "
                    f"WHERE {_RECHECK_WHERE} {_ORDER} LIMIT ?", (_RECHECK_BATCH + len(self._recheck_seen),)).fetchall()
                fresh = [dict(r) for r in rows if r["id"] not in self._recheck_seen]
                if not fresh:
                    self._recheck_done = True
                    return False
                self._recheck.extend(fresh)
            row = self._recheck.popleft()
            self._recheck_seen.add(row["id"])
            if self.order is not None and self.order.skipped(row["project"], row["mail_id"]):
                continue  # a file of a folder switched off: not read
            data = self._read(row)
            if data is None:
                continue
            break
        self._check_stop()
        result = extract_worker.extract_file(row["filename"], data, self.llm, self.extract, defer_vision=True,
                                             recheck=True, worker=self.worker)
        self.stats["checked"] += 1
        profile = stored_profile(self.profile, result)
        self._begin()
        if self.conn.execute(f"SELECT 1 FROM documents WHERE id = ? AND sha256 = ? AND {_RECHECK_WHERE}",
                             (row["id"], row["sha256"])).fetchone() is None:
            self.conn.rollback()  # read anew by the documents pass meanwhile: its own result stands
            return True
        if result.vision_pages and result.status in ("ok", "skipped"):
            store_page_texts(self.conn, row["id"], result.pages, result.vision_pages)
            self.conn.execute(
                "UPDATE documents SET extract_profile = ?, incomplete = 1, vision_pending = ?, vision_note = ?, "
                "vision_incomplete = ?, message = ? WHERE id = ?",
                (profile, len(result.vision_pages), result.message, int(result.incomplete),
                 pending_message(result.message, len(result.vision_pages)), row["id"]))
            self.stats["found"] += 1
            self._project[row["id"]] = (row["project"], row["mail_id"])
            self._ids.append(row["id"])  # read in this pass as well, after the documents already waiting
            log.info("Bildauswertung: %s – %d gescannte Seite(n) mit Textebene gefunden", printable(row["rel_path"]),
                     len(result.vision_pages))
        elif result.status in (UNCHANGED, "ok", "empty"):
            self.conn.execute("UPDATE documents SET extract_profile = ? WHERE id = ?", (self.profile, row["id"]))
        self.conn.commit()  # anything else (worker failure): checked again in a later pass
        return True


class _Pool:
    """Model calls in daemon threads, at most as many at a time as the caller submits (it keeps to
    VISION_CONCURRENCY). Daemon threads, unlike a ThreadPoolExecutor's: an answer still on its way when the
    app shuts down does not hold up its end."""

    def submit(self, fn, *args) -> Future:
        future: Future = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args)
            except BaseException as exc:  # noqa: BLE001 – handed to the caller
                future.set_exception(exc)
            else:
                future.set_result(result)

        threading.Thread(target=run, name="bildauswertung", daemon=True).start()
        return future


_PASS_LOCK = threading.Lock()  # one run of the Bildauswertung at a time in this process (its concurrency cap)


def _order(conn, settings) -> reihenfolge.VisionOrder:
    return reihenfolge.VisionOrder(conn, getattr(settings, "projects_dir", None),
                                   getattr(settings, "index_folder_order", ()))


def run_vision(conn, llm, settings, *, progress=None) -> dict:
    """The phase "Bildauswertung" at the end of an indexing pass without the app's background worker (see
    the module docstring); nothing without vision, nothing while the background worker reads pages."""
    if llm is None or not getattr(llm, "vision", False):
        counts = pending_counts(conn)
        return {"note": "ausgeschaltet (LLM_VISION=0)", "pending_pages": counts["pages"],
                "pending_documents": counts["documents"]}
    if not _PASS_LOCK.acquire(blocking=False):
        return {"note": "läuft im Hintergrund"}
    try:
        return VisionPass(conn, llm, seconds=getattr(settings, "vision_pass_seconds", 1800.0),
                          concurrency=getattr(settings, "vision_concurrency", 3),
                          projects_dir=getattr(settings, "projects_dir", None), progress=progress,
                          order=_order(conn, settings)).run()
    finally:
        _PASS_LOCK.release()


# -- in the background, beside the documents pass ----------------------------------------------------------
class VisionLive:
    """What the background Bildauswertung does right now, for the status page (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.enabled = False  # a background worker runs (LLM_VISION=1, the app)
            self.concurrency = 0
            self.busy = False  # a run with pages to read is going on
            self._project: dict | None = None
            self.in_flight = 0  # pages with the model right now
            self.document: dict | None = None  # file and page last handed to the model
            self.pages = 0  # read since the app started
            self.pending_pages: int | None = None  # waiting, without those of folders switched off
            self.off_pages = 0  # waiting in folders switched off (not read)
            self.since: float | None = None

    def enable(self, concurrency: int) -> None:
        with self._lock:
            self.enabled, self.concurrency, self.since = True, concurrency, time.time()

    def set_busy(self, busy: bool) -> None:
        with self._lock:
            self.busy = busy
            if not busy:
                self._project = self.document = None
                self.in_flight = 0

    def calls(self, n: int, name: str | None = None, page: int | None = None) -> None:
        with self._lock:
            self.in_flight = n
            if name is not None:
                self.document = {"name": name, "page": page}

    def project(self, number: str | None, folder: str) -> None:
        with self._lock:
            self._project = {"number": number, "folder": folder} if number else None

    def pending(self, pages: int, off: int) -> None:
        with self._lock:
            self.pending_pages, self.off_pages = pages, off

    def page_done(self, seconds: float | None) -> None:
        with self._lock:
            if seconds is not None:
                self.pages += 1
            if self.pending_pages:
                self.pending_pages -= 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "concurrency": self.concurrency, "busy": self.busy,
                    "project": dict(self._project) if self._project else None, "pages": self.pages,
                    "in_flight": self.in_flight, "document": dict(self.document) if self.document else None,
                    "pending_pages": self.pending_pages, "off_pages": self.off_pages, "since": self.since}


LIVE = VisionLive()


def _worked(stats: dict) -> bool:
    return any(stats.get(k) for k in ("pages", "failed", "not_readable", "documents", "checked", "discarded"))


class VisionWorker:
    """The Bildauswertung of the app: a thread that reads the scan pages waiting for the model, beside the
    documents pass and between the indexing passes (see the module docstring). Started only with
    LLM_VISION=1. Runs of at most VISION_PASS_SECONDS one after the other while pages wait; with nothing to
    do it sleeps until the documents pass stores a document with scan pages (documents.VISION_WAITING) or
    for IDLE_SECONDS. Its own database connection (short write transactions, the busy timeout of db.connect)
    and its own extraction worker process for the page images (extract_worker.own_worker)."""

    IDLE_SECONDS = 60.0  # nothing to do (or only pages whose retry is not due yet): look again after this
    REFRESH_SECONDS = 60.0  # during a run, the documents waiting are read again this often
    ERROR_SECONDS = 60.0  # after an unexpected error

    def __init__(self, settings, llm, *, live: VisionLive | None = None):
        self.settings, self.llm = settings, llm
        self.live = live or LIVE
        self.concurrency = max(1, int(getattr(settings, "vision_concurrency", 3)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker = None  # extract_worker.ExtractWorker of its own, or None (in this process)
        self.runs = 0
        self.last_error: str | None = None

    @staticmethod
    def wanted(llm) -> bool:
        return llm is not None and bool(getattr(llm, "vision", False))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._worker = extract_worker.own_worker(self.settings)
        self.live.reset()
        self.live.enable(self.concurrency)
        self._thread = threading.Thread(target=self._loop, name="bildauswertung", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Ends at once: answers of the model on their way are dropped (those pages are read after the next
        start), a page image being prepared is abandoned (its worker process ends)."""
        self._stop.set()
        documents.VISION_WAITING.set()
        if self._worker is not None:
            self._worker.close()
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
                documents.VISION_WAITING.clear()
                try:
                    if conn is None:
                        db.init_db(self.settings.db_path)
                        conn = db.connect(self.settings.db_path)
                    stats = self.run_once(conn)
                    self.last_error = None
                    pause = 0.0 if _worked(stats) else self.IDLE_SECONDS
                except extract_worker.ExtractionAborted:
                    if self._stop.is_set():
                        break
                    pause = self.ERROR_SECONDS
                except Exception as exc:  # noqa: BLE001 – the thread must survive any single failure
                    log.exception("Bildauswertung im Hintergrund fehlgeschlagen")
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    if conn is not None:
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                    pause = self.ERROR_SECONDS
                finally:
                    self.live.set_busy(False)
                if pause and not self._stop.is_set():
                    documents.VISION_WAITING.wait(pause)
        finally:
            if conn is not None:
                conn.close()

    def run_once(self, conn) -> dict:
        """One run (at most VISION_PASS_SECONDS); waits while another one runs in this process."""
        while not _PASS_LOCK.acquire(timeout=1.0):
            if self._stop.is_set():
                raise extract_worker.ExtractionAborted("Bildauswertung wird beendet")
        try:
            self.runs += 1
            self.live.set_busy(True)
            return VisionPass(conn, self.llm, seconds=getattr(self.settings, "vision_pass_seconds", 1800.0),
                              concurrency=self.concurrency, projects_dir=getattr(self.settings, "projects_dir", None),
                              order=_order(conn, self.settings), worker=self._worker, stop=self._stop,
                              refresh=self.REFRESH_SECONDS, live=self.live).run()
        finally:
            _PASS_LOCK.release()
