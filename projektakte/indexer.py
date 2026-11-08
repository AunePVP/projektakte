"""Indexing pipeline: project register → documents → mails → assignment.

One call of :func:`run_index` processes everything that is new or changed; it is used by the
background worker, the CLI (``python -m projektakte.index``) and the tests.
"""
from __future__ import annotations

import logging
import time

from . import db
from .ai.services import AIServices
from .config import Settings
from .registry import scan_projects, sync_projects

log = logging.getLogger("projektakte.indexer")


def run_index(settings: Settings, ai: AIServices | None = None) -> dict:
    started = time.time()
    db.init_db(settings.db_path)
    stats: dict = {}
    with db.session(settings.db_path) as conn:
        folders = scan_projects(settings.projects_dir, settings.project_number_pattern)
        stats["projects"] = sync_projects(conn, folders)
    stats["seconds"] = round(time.time() - started, 2)
    log.info("Indexierung fertig: %s", stats)
    return stats
