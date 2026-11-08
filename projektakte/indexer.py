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
from .documents import index_all_documents, reprocess_attachments
from .mails.eml_source import sync_eml_folder
from .mails.jmap_source import JMAPClient, sync_plmail
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


# ------------------------------------------------------------------------------------------------
# WIRING (mails package): extra keyword arguments for the mail and assignment phases, e.g. a guard
# against an empty MAIL_DIR or a minimum similarity for suggestions. Add an entry only once the
# called function accepts it; everything else in this module stays as it is.
def _extra_kwargs(settings: Settings, step: str) -> dict:
    """Optional keyword arguments for ``step`` in ("eml", "plmail", "assign")."""
    if step == "assign":
        return {"min_similarity": settings.assign_min_similarity}
    return {}
# ------------------------------------------------------------------------------------------------


def _run(settings: Settings, ai: AIServices | None, phase) -> dict:
    started = time.time()
    db.init_db(settings.db_path)
    stats: dict = {}
    with db.session(settings.db_path) as conn:
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
        stats["mails"] = sync_eml_folder(conn, settings.mail_dir, cache_dir=settings.cache_dir, llm=llm,
                                         **_extra_kwargs(settings, "eml"))
        if settings.plmail_enabled:
            phase("plMail")
            stats["plmail"] = sync_plmail(conn, plmail_client(settings), cache_dir=settings.cache_dir, llm=llm,
                                          **_extra_kwargs(settings, "plmail"))
        # incomplete attachments (scan without vision, model down) of unchanged mails
        stats["attachments"] = reprocess_attachments(conn, llm=llm)
        if ai is not None:
            phase("Embeddings")
            stats["embeddings"] = embed_pending(conn, ai.embed)
        phase("Zuordnung")
        stats["assignment"] = assign_mails(conn, settings.project_number_pattern,
                                           min_share=settings.assign_min_share,
                                           min_lead=settings.assign_min_lead,
                                           vector_key=str(settings.db_path),
                                           mode=settings.assign_mode,
                                           **_extra_kwargs(settings, "assign"))
    stats["seconds"] = round(time.time() - started, 2)
    log.info("Indexierung fertig: %s", stats)
    return stats
