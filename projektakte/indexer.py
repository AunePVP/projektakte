"""Indexing pipeline: project register → documents → mails → embeddings → assignment.

One call of :func:`run_index` processes everything that is new or changed; it is used by the
background worker, the CLI (``python -m projektakte.index``) and the tests.
"""
from __future__ import annotations

import logging
import threading
import time

from . import db
from .ai.services import AIServices
from .assign import assign_mails
from .config import Settings
from .demo_cleanup import demo_left_over, purge_demo_data, remember_demo
from .documents import index_all_documents, reprocess_attachments
from .mails.eml_source import sync_eml_folder
from .mails.jmap_source import JMAPClient, sync_plmail
from .mails.store import sweep_cache
from .registry import sync_from_folder
from .vectors import embed_pending

log = logging.getLogger("projektakte.indexer")


_RUN_LOCK = threading.Lock()


def plmail_client(settings: Settings) -> JMAPClient:
    if not settings.plmail_token.startswith("plmail_"):
        log.warning("PLMAIL_TOKEN hat nicht die erwartete Form plmail_<64 Hex-Zeichen>")
    return JMAPClient(settings.plmail_url, settings.plmail_token, timeout=settings.plmail_timeout,
                      page_size=settings.plmail_page_size)


def run_index(settings: Settings, ai: AIServices | None = None, progress=None) -> dict:
    """One pass over all sources. ``progress(phase)`` is called with the current phase."""
    def phase(name: str) -> None:
        if progress:
            progress(name)

    with _RUN_LOCK:  # never two passes at the same time (background worker + CLI/test)
        return _run(settings, ai, phase)


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


def _run(settings: Settings, ai: AIServices | None, phase) -> dict:
    started = time.time()
    db.init_db(settings.db_path)
    stats: dict = {}
    with db.session(settings.db_path) as conn:
        if (demo := _demo_data(conn, settings)) is not None:
            stats["demo_purge"] = demo
        phase("Projekte")
        stats["projects"] = sync_from_folder(conn, settings.projects_dir, settings.project_number_pattern)
        conn.commit()
        llm = ai.llm if ai else None
        phase("Dokumente")
        if stats["projects"].get("warning"):  # PROJECTS_DIR unavailable: change nothing
            stats["documents"] = {"note": "nicht abgeglichen – Projektordner nicht verfügbar"}
        else:
            stats["documents"] = index_all_documents(conn, llm=llm, projects_dir=settings.projects_dir)
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
        if ai is not None:
            phase("Embeddings")
            stats["embeddings"] = embed_pending(conn, ai.embed)
        phase("Zuordnung")
        stats["assignment"] = assign_mails(conn, settings.project_number_pattern,
                                           min_share=settings.assign_min_share,
                                           min_lead=settings.assign_min_lead,
                                           vector_key=str(settings.db_path),
                                           mode=settings.assign_mode,
                                           min_similarity=settings.assign_min_similarity)
    stats["seconds"] = round(time.time() - started, 2)
    log.info("Indexierung fertig: %s", stats)
    return stats
