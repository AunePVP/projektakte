"""Indexing pipeline: project register → documents → mails → assignment.

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
from .documents import index_all_documents
from .mails.eml_source import sync_eml_folder
from .mails.jmap_source import JMAPClient, sync_plmail
from .registry import scan_projects, sync_projects
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


def _run(settings: Settings, ai: AIServices | None, phase) -> dict:
    started = time.time()
    db.init_db(settings.db_path)
    stats: dict = {}
    with db.session(settings.db_path) as conn:
        phase("Projekte")
        folders = scan_projects(settings.projects_dir, settings.project_number_pattern)
        stats["projects"] = sync_projects(conn, folders)
        conn.commit()
        llm = ai.llm if ai else None
        phase("Dokumente")
        stats["documents"] = index_all_documents(conn, llm=llm)
        phase("Mails")
        stats["mails"] = sync_eml_folder(conn, settings.mail_dir, cache_dir=settings.cache_dir, llm=llm)
        if settings.plmail_enabled:
            phase("plMail")
            stats["plmail"] = sync_plmail(conn, plmail_client(settings), cache_dir=settings.cache_dir, llm=llm)
        if ai is not None:
            phase("Embeddings")
            stats["embeddings"] = embed_pending(conn, ai.embed)
        phase("Zuordnung")
        stats["assignment"] = assign_mails(conn, settings.project_number_pattern,
                                           min_share=settings.assign_min_share,
                                           min_lead=settings.assign_min_lead,
                                           vector_key=str(settings.db_path))
    stats["seconds"] = round(time.time() - started, 2)
    log.info("Indexierung fertig: %s", stats)
    return stats
