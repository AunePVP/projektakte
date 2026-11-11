"""Phase "Bildauswertung": the multimodal model reads the scan pages of the documents (stage 2).

In an indexing pass a file is read in two stages (see extract.py, ``defer_vision``):

1. The documents and mail passes store it at once – its text layers, and for scan pages without one
   Tesseract's text (if installed) – so it is searchable right away. Its scan pages are listed as
   pending (``documents.vision_pending``, ``page_texts``): "Bildauswertung ausstehend".
2. This phase, after documents, mails and embeddings (nothing waits on it), lets the model read them:
   newest documents first, up to VISION_CONCURRENCY calls at a time (the AI server takes a few at once and
   answers the questions as well, so one of its places stays free), for at most VISION_PASS_SECONDS per
   indexing pass – then the pass ends, new files and mails are read, and the next pass goes on where
   this one stopped. The model gets the page image *and* the page's text (OCR layer or Tesseract), its
   answer replaces that text; the document's sections are written again (only that document's), and
   their vectors are made by the next embedding run.

The page images are decoded and prepared by the extraction worker under its limits (untrusted PDF data is
never parsed in the app process during a pass); the app only sends the finished JPEG/PNG bytes to the
model, from a small thread pool. While the model works, the phase also checks complete PDFs read by an
earlier version without the model for scan pages with a text layer (``recheck``) – their handwriting is
only in the image.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field
from pathlib import Path

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
    """One run of the phase within its time budget (see the module docstring)."""

    def __init__(self, conn, llm, *, seconds: float = 1800.0, concurrency: int = 3, projects_dir=None,
                 progress=None, clock=time.monotonic, prepare=None, extract=None, order=None):
        self.conn, self.llm = conn, llm
        self.seconds = float(seconds)
        self.concurrency = max(1, int(concurrency))
        self.boundary = Path(projects_dir).resolve() if projects_dir else None
        self.progress = progress
        self.order = order  # reihenfolge.VisionOrder: pinned projects and the folder order first
        self._project: dict[int, str | None] = {}  # document id → project
        self.clock = clock
        self.prepare = prepare or extract_mod.page_images
        self.extract = extract or extract_mod.extract
        self.profile = extraction_profile(llm)
        self.stats = {"pages": 0, "failed": 0, "not_readable": 0, "documents": 0, "checked": 0, "found": 0,
                      "stopped": False}
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
            return {**self.stats, "pending_pages": 0, "pending_documents": 0, "recheck": 0}
        log.info("Bildauswertung: %d Seite(n) in %d Dokument(en) ausstehend%s – bis zu %d gleichzeitig, Zeitbudget %s",
                 counts["pages"], counts["documents"],
                 f", {counts['recheck']} ältere PDF-Datei(en) zu prüfen" if counts["recheck"] else "",
                 self.concurrency, f"{seconds_text(self.seconds)} s" if self.seconds > 0 else "unbegrenzt")
        self._end = self.clock() + self.seconds if self.seconds > 0 else float("inf")
        rows = self.conn.execute(f"SELECT id, project FROM documents WHERE vision_pending > 0 {_ORDER}").fetchall()
        self._project = {r[0]: r[1] for r in rows}
        self._ids = deque(self.order.sort([(r[0], r[1]) for r in rows]) if self.order else (r[0] for r in rows))
        pool = _Pool()  # (app shutdown: answers still on their way are not waited for – daemon threads)
        inflight: dict[Future, _Task] = {}
        while True:
            self._check_stop()
            while len(inflight) < self.concurrency and self._time_left():
                task = self._next_task()
                if task is None:
                    break
                inflight[pool.submit(self._call, task)] = task
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

    def _time_left(self) -> bool:
        return self.clock() < self._end

    def _check_stop(self) -> None:
        worker = extract_worker.current()
        if worker is not None and worker.closed:  # the app shuts down
            raise extract_worker.ExtractionAborted("Leseprozess wurde beendet")

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
            if not self._ids:
                return None
            if self.order is not None and self.order.stale():  # the order was changed on the status page
                self._ids = deque(self.order.sort([(i, self._project.get(i)) for i in self._ids]))
            doc_id = self._ids.popleft()
            if doc_id in self._seen:
                continue
            self._seen.add(doc_id)
            row = self.conn.execute(
                "SELECT id, path, rel_path, filename, ext, sha256, cache_path, mail_id, vision_pending FROM documents "
                "WHERE id = ?", (doc_id,)).fetchone()
            if row is None or not row["vision_pending"]:
                continue
            due = deque(self.conn.execute(
                "SELECT page, text FROM page_texts WHERE doc_id = ? AND (state = 'pending' OR (state = 'failed' AND "
                "(retry_after IS NULL OR retry_after <= ?))) ORDER BY page", (doc_id, now)).fetchall())
            if not due:
                continue
            data = self._read(dict(row))
            if data is None:
                continue
            doc = _Doc(dict(row), data, deque((r["page"], r["text"]) for r in due))
            if self.order is not None:
                number = self._project.get(doc_id)
                reihenfolge.LIVE.vision_project(number, self.order.project_of(number))
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
            self._queue(doc, _Task(row["id"], number, name, known, [(IMAGE_EXT[row["ext"]], doc.data)]))
            doc.due.clear()
            return
        batch = [doc.due.popleft() for _ in range(min(BATCH_PAGES, len(doc.due)))]
        known = dict(batch)
        prepared = extract_worker.page_images(row["filename"], doc.data, [n for n, _t in batch], self.prepare)
        for page in prepared:
            if page.images:
                self._queue(doc, _Task(row["id"], page.number, name, known.get(page.number, ""), page.images,
                                       page.partial, page.retry, page.limit))
            else:
                log.warning("Bildauswertung: %s Seite %d nicht auswertbar (%s)", name, page.number, page.problem)
                self._store(row["id"], page.number, problem=page.problem or "kein Bild", retry=page.retry,
                            limit=page.limit)

    def _queue(self, doc: _Doc, task: _Task) -> None:
        doc.open += 1
        self._tasks.append(task)

    # -- results -----------------------------------------------------------------------------------------
    def _finish(self, task: _Task, future: Future) -> None:
        try:
            text, seconds = future.result()
        except Exception as exc:  # noqa: BLE001 – the model failed (timeout, server down): later again
            log.warning("Bildauswertung: %s Seite %d fehlgeschlagen (%s)", task.name, task.page, exc)
            self._store(task.doc_id, task.page, error=str(exc))
        else:
            log.info("Bildauswertung: %s Seite %d gelesen (%.0f s)%s", task.name, task.page, seconds,
                     "" if text else " – kein Text")
            self._store(task.doc_id, task.page, text=text, partial=task.partial, retry=task.retry, limit=task.limit)
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
               limit: str | None = None) -> None:
        """Record the outcome of one page (committed at once: an interrupted pass loses no page read)."""
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
        else:
            note = partial or (None if text else NO_TEXT)
            self.conn.execute(
                "UPDATE page_texts SET state = 'done', text = CASE WHEN ? != '' THEN ? ELSE text END, note = ?, "
                "partial = ?, retry = ?, size_limit = ? WHERE doc_id = ? AND page = ?",
                (text or "", text or "", note, int(partial is not None), int(partial is not None and retry), limit,
                 doc_id, page))
            self.stats["pages"] += 1
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
        doc = self.conn.execute(
            "SELECT d.*, p.name AS project_name FROM documents d LEFT JOIN projects p ON p.number = d.project "
            "WHERE d.id = ?", (doc_id,)).fetchone()
        if doc is None:
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
        self.conn.commit()

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
                    f"SELECT id, path, rel_path, filename, ext, sha256, cache_path, mail_id FROM documents "
                    f"WHERE {_RECHECK_WHERE} {_ORDER} LIMIT ?", (_RECHECK_BATCH + len(self._recheck_seen),)).fetchall()
                fresh = [dict(r) for r in rows if r["id"] not in self._recheck_seen]
                if not fresh:
                    self._recheck_done = True
                    return False
                self._recheck.extend(fresh)
            row = self._recheck.popleft()
            self._recheck_seen.add(row["id"])
            data = self._read(row)
            if data is None:
                continue
            break
        self._check_stop()
        result = extract_worker.extract_file(row["filename"], data, self.llm, self.extract, defer_vision=True,
                                             recheck=True)
        self.stats["checked"] += 1
        profile = stored_profile(self.profile, result)
        if result.vision_pages and result.status in ("ok", "skipped"):
            store_page_texts(self.conn, row["id"], result.pages, result.vision_pages)
            self.conn.execute(
                "UPDATE documents SET extract_profile = ?, incomplete = 1, vision_pending = ?, vision_note = ?, "
                "vision_incomplete = ?, message = ? WHERE id = ?",
                (profile, len(result.vision_pages), result.message, int(result.incomplete),
                 pending_message(result.message, len(result.vision_pages)), row["id"]))
            self.stats["found"] += 1
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


def run_vision(conn, llm, settings, *, progress=None) -> dict:
    """The phase "Bildauswertung" of an indexing pass (see the module docstring); nothing without vision."""
    if llm is None or not getattr(llm, "vision", False):
        counts = pending_counts(conn)
        return {"note": "ausgeschaltet (LLM_VISION=0)", "pending_pages": counts["pages"],
                "pending_documents": counts["documents"]}
    return VisionPass(conn, llm, seconds=getattr(settings, "vision_pass_seconds", 1800.0),
                      concurrency=getattr(settings, "vision_concurrency", 3),
                      projects_dir=getattr(settings, "projects_dir", None), progress=progress,
                      order=reihenfolge.VisionOrder(conn, getattr(settings, "projects_dir", None),
                                                    getattr(settings, "index_folder_order", ()))).run()
