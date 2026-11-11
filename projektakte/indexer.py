"""Indexing pipeline: project register → documents → mails → embeddings → assignment → Bildauswertung.

One call of :func:`run_index` processes everything that is new or changed; it is used by the
background worker, the CLI (``python -m projektakte.index``) and the tests. In the app the Bildauswertung
runs beside it in a thread of its own (vision.VisionWorker, ``vision=False`` here); the CLI and the tests
keep it as the last phase of the pass.
"""
from __future__ import annotations

import logging
import threading
import time

from . import db, documents, dokumentarten, extract_worker, reihenfolge, structure
from .ai.services import AIServices
from .assign import assign_mails
from .config import Settings
from .demo_cleanup import demo_left_over, purge_demo_data, remember_demo
from .documents import index_all_documents, reprocess_attachments
from .mails.eml_source import sync_eml_folder
from .mails.jmap_source import JMAPClient, sync_plmail
from .mails.store import sweep_cache
from .registry import ENCODING_HINT, active_count, sync_from_folder, unchanged_stats
from .vectors import DEFAULT_VECTOR_SEARCH, VECTOR_INDEX, embed_pending
from .vision import run_vision

log = logging.getLogger("projektakte.indexer")


_RUN_LOCK = threading.Lock()


def plmail_client(settings: Settings) -> JMAPClient:
    if not settings.plmail_token.startswith("plmail_"):
        log.warning("PLMAIL_TOKEN hat nicht die erwartete Form plmail_<64 Hex-Zeichen>")
    return JMAPClient(settings.plmail_url, settings.plmail_token, timeout=settings.plmail_timeout,
                      page_size=settings.plmail_page_size)


def run_index(settings: Settings, ai: AIServices | None = None, progress=None, *, vision: bool = True,
              embeddings: bool = True, doc_types: bool = True) -> dict:
    """One pass over all sources. ``progress(phase)`` is called with the current phase. ``vision`` False: no
    phase "Bildauswertung" (the app's background worker reads the scan pages meanwhile); ``embeddings`` False:
    no phase "Embeddings" (the app's background embedding makes the vectors meanwhile, embed_worker.py);
    ``doc_types`` False: documents indexed before document types existed are typed by the app's worker
    (dokumentarten.DokumentartWorker), not at the end of the pass."""
    def phase(name: str) -> None:
        reihenfolge.LIVE.set_phase(name)  # status page: "Wird gerade gelesen"
        if progress:
            progress(name)

    # never two passes at the same time (background worker + CLI/test); files are read in the extraction
    # worker process if the settings say so (EXTRACT_ISOLATION) – it is kept for the next pass – and in two
    # stages: the model reads scan pages in the phase "Bildauswertung" at the end (vision.py)
    VECTOR_INDEX.configure(getattr(settings, "vector_search", DEFAULT_VECTOR_SEARCH))  # mail assignment searches
    with _RUN_LOCK, extract_worker.isolation(settings), documents.vision_later():
        reihenfolge.LIVE.begin()
        try:
            return _run(settings, ai, phase, vision, embeddings, doc_types)
        finally:
            reihenfolge.LIVE.end()


def _demo_data(conn, settings: Settings) -> dict | None:
    """DEMO_MODE=1: remember that the demo data set is indexed. DEMO_MODE=0: remove demo projects and
    demo mails left over from such runs, so invented content never reaches search and answers."""
    if settings.demo_mode:
        remember_demo(conn, settings.demo_dir)
        conn.commit()
        return None
    if not demo_left_over(conn, settings):
        return None
    result = purge_demo_data(conn, settings)
    return result if result["projects"] or result["mails"] else None


def _paused(layout: structure.Layout) -> str:
    """Why projects, documents and the assignment wait (German, short)."""
    if layout.state == "pending":
        return structure.PENDING_TEXT
    if layout.state in ("rejected", "none", "unchecked"):
        return "kein bestätigtes Projektnummern-Schema"
    return "kein gültiges Projektnummern-Schema"


def _index_documents(conn, llm, settings: Settings) -> dict:
    """The files of all active projects. File names that are no valid UTF-8 (a share mounted without
    iocharset=utf8) are skipped and counted by the documents pass itself, a project it cannot store
    is skipped there; should anything else of that kind slip through, the documents of this pass stop
    with a warning – the mails, embeddings and the assignment still run – instead of the whole pass
    failing."""
    try:
        return index_all_documents(conn, llm=llm, projects_dir=settings.projects_dir,
                                   skip_photos=settings.vision_skip_folders,
                                   folder_order=getattr(settings, "index_folder_order", ()))
    except UnicodeError as exc:
        conn.rollback()
        log.warning("Dokumente: ein Datei- oder Ordnername ist kein gültiges UTF-8 (%s) – %s", exc, ENCODING_HINT)
        return {"warning": f"Dateien nicht vollständig gelesen: ein Datei- oder Ordnername hat eine falsche "
                           f"Zeichenkodierung (meist eine Freigabe ohne iocharset=utf8) – {ENCODING_HINT}"}


def _run(settings: Settings, ai: AIServices | None, phase, vision: bool = True, embeddings: bool = True,
         doc_types: bool = True) -> dict:
    started = time.time()
    db.init_db(settings.db_path)
    stats: dict = {}
    with db.session(settings.db_path) as conn:
        if (demo := _demo_data(conn, settings)) is not None:
            stats["demo_purge"] = demo
        phase("Projekte")
        # the effective layout: PROJECT_NUMBER_PATTERN, or with "auto" the confirmed scheme (a detection
        # made here is only a proposal until a person confirms it); without a usable scheme nothing is
        # synced (like an unmounted share)
        layout, structure_info = structure.layout_for_index(conn, settings)
        if structure_info:
            stats["structure"] = structure_info
        if layout.pattern is None:
            log.warning("%s – Projekte und Dokumente bleiben unverändert", layout.problem)
            stats["projects"] = unchanged_stats(active_count(conn), layout.problem)
        else:
            stats["projects"] = sync_from_folder(conn, settings.projects_dir, layout.pattern,
                                                 max_depth=layout.depth,
                                                 parent_digits=getattr(settings, "project_parent_digits", 0))
        conn.commit()
        llm = ai.llm if ai else None
        phase("Dokumente")
        if layout.pattern is None:
            stats["documents"] = {"note": f"nicht abgeglichen – {_paused(layout)}"}
        elif stats["projects"].get("warning"):  # PROJECTS_DIR unavailable: change nothing
            stats["documents"] = {"note": "nicht abgeglichen – Projektordner nicht verfügbar"}
        else:
            stats["documents"] = _index_documents(conn, llm, settings)
        if layout.pattern is not None:  # what the scans noticed (guard, names in a wrong encoding)
            structure.record_scan_notes(conn, settings, layout, stats["projects"], stats["documents"])
            conn.commit()
        phase("Mails")
        # MAIL_ALLOW_EMPTY=1: an empty MAIL_DIR is intended – remove its mails (normally a missing
        # mount, so nothing is removed then)
        stats["mails"] = sync_eml_folder(conn, settings.mail_dir, cache_dir=settings.cache_dir, llm=llm,
                                         allow_empty=bool(getattr(settings, "mail_allow_empty", False)))
        if settings.plmail_enabled:
            phase("plMail")
            stats["plmail"] = sync_plmail(conn, plmail_client(settings), cache_dir=settings.cache_dir, llm=llm)
        # incomplete attachments (scan without vision, model down) of unchanged mails
        stats["attachments"] = reprocess_attachments(conn, llm=llm)
        # attachment files no document uses any more (once a day; no import runs meanwhile)
        if (swept := sweep_cache(conn, settings.cache_dir)) is not None:
            stats["cache_sweep"] = {"removed": swept}
        if ai is not None and not embeddings:
            stats["embeddings"] = {"note": "laufen im Hintergrund"}
        elif ai is not None:
            phase("Embeddings")
            stats["embeddings"] = embed_pending(conn, ai.embed)
        phase("Zuordnung")
        if layout.pattern is None:  # mails wait (stay "pending") until project numbers can be recognised
            stats["assignment"] = {"note": f"nicht ausgeführt – {_paused(layout)}"}
        else:
            stats["assignment"] = assign_mails(conn, layout.pattern,
                                               min_share=settings.assign_min_share,
                                               min_lead=settings.assign_min_lead,
                                               vector_key=str(settings.db_path),
                                               mode=settings.assign_mode,
                                               min_similarity=settings.assign_min_similarity)
        if not vision:  # the app's background worker reads the scan pages meanwhile (vision.VisionWorker)
            stats["vision"] = {"note": "läuft im Hintergrund"}
        else:  # last: the model reads the scan pages within its time budget; the next pass goes on
            phase("Bildauswertung")
            stats["vision"] = run_vision(conn, llm, settings, progress=phase)
            if embeddings and ai is not None and (stats["vision"].get("pages") or stats["vision"].get("documents")):
                stats["vision_embeddings"] = embed_pending(conn, ai.embed)  # the sections written anew
        if doc_types:  # without the app: the documents indexed before types existed (deterministic, fast)
            typed = dokumentarten.backfill_all(conn)
            if typed:
                stats["doc_types"] = {"typed": typed}
    stats["seconds"] = round(time.time() - started, 2)
    log.info("Indexierung fertig: %s", stats)
    return stats
