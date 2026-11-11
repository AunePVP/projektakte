"""JSON API."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import db, inbox as inbox_queries, reihenfolge, structure, vision
from .assign import INBOX_STATUSES, DecisionError, decide
from .ask import ask as answer_question
from .search import hybrid_search
from .db import BAUVORHABEN
from .registry import children, get_project, list_projects, project_folders

log = logging.getLogger("projektakte.api")
router = APIRouter(prefix="/api")
BUSY_RETRY_AFTER = 5  # seconds


def _settings(request: Request):
    return request.app.state.settings


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path


@router.get("/projects")
def projects(request: Request, q: str = "") -> dict:
    """Projects and sites as a flat list (``kind``; an order names its site in ``parent``, a site lists
    its orders in ``children``)."""
    s = _settings(request)
    with db.session(s.db_path) as conn:
        items = list_projects(conn, q)
        kids: dict[str, list[str]] = {}
        if any(p["kind"] == BAUVORHABEN for p in items):
            for c in list_projects(conn):
                if c["parent"]:
                    kids.setdefault(c["parent"], []).append(c["number"])
    for p in items:
        p["path"] = _rel(p["path"], s.projects_dir)
        if p["kind"] == BAUVORHABEN:
            p["children"] = sorted(kids.get(p["number"], []))
    return {"count": len(items), "projects": items}


@router.get("/projects/{number}")
def project(request: Request, number: str) -> dict:
    s = _settings(request)
    with db.session(s.db_path) as conn:
        p = get_project(conn, number)
        folders = project_folders(conn, number) if p else []
        site = get_project(conn, p["parent"]) if p and p["parent"] else None
        kids = children(conn, p["number"]) if p and p["kind"] == BAUVORHABEN else []
    if not p:
        raise HTTPException(404, f"Projekt {number} nicht gefunden")
    p["path"] = _rel(p["path"], s.projects_dir)
    # an order: its site (Bauvorhaben); a site: its orders (Unterprojekte), which questions and searches
    # about it cover – its documents (/documents) are only its own files outside them
    p["parent_name"] = site["name"] if site else None
    p["children"] = [{"number": c["number"], "name": c["name"], "path": _rel(c["path"], s.projects_dir)}
                     for c in kids]
    # every folder of the project (several folders with its number are one project), the primary first
    p["folders"] = [_rel(f, s.projects_dir) for f in folders]
    return p


@router.get("/projects/{number}/documents")
def project_documents(request: Request, number: str) -> dict:
    s = _settings(request)
    with db.session(s.db_path) as conn:
        p = get_project(conn, number)
        if not p:
            raise HTTPException(404, f"Projekt {number} nicht gefunden")
        rows = conn.execute(
            "SELECT d.id, d.rel_path, d.filename, d.ext, d.size, d.doc_date, d.pages, d.status, "
            "d.message, d.method, (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS chunks "
            "FROM documents d WHERE d.project = ? AND d.mail_id IS NULL ORDER BY d.rel_path",
            (p["number"],)).fetchall()
    return {"project": p["number"], "count": len(rows), "documents": [dict(r) for r in rows]}


@router.get("/documents/{doc_id}")
def document(request: Request, doc_id: int) -> dict:
    s = _settings(request)
    with db.session(s.db_path) as conn:
        row = conn.execute(
            "SELECT id, project, mail_id, rel_path, filename, ext, size, doc_date, pages, status, "
            "message, method, sha256, indexed_at FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Dokument nicht gefunden")
        chunks = conn.execute("SELECT id, ord, page, heading, text FROM chunks WHERE doc_id = ? "
                              "ORDER BY ord", (doc_id,)).fetchall()
    return {**dict(row), "chunks": [dict(c) for c in chunks]}


MAIL_COLUMNS = ("id, source, subject, sender, sender_name, recipients, date, preview, has_attachments, "
                "project, assign_status, assign_method, assign_reason, suggested_project, suggestion_score")


@router.get("/mails")
def mails(request: Request, project: str | None = None, status: str | None = None, q: str = "",
          limit: int = 100, offset: int = 0) -> dict:
    s = _settings(request)
    where, args = ["deleted = 0"], []
    if project:
        where.append("project = ? COLLATE NOCASE")
        args.append(project)
    if status:
        where.append("assign_status = ?")
        args.append(status)
    if q.strip():
        where.append("(subject LIKE ? OR body LIKE ? OR sender LIKE ? OR sender_name LIKE ?)")
        args += [f"%{q.strip()}%"] * 4
    sql_where = " WHERE " + " AND ".join(where)
    limit = max(1, min(limit, 500))
    with db.session(s.db_path) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM mails{sql_where}", args).fetchone()[0]
        rows = conn.execute(f"SELECT {MAIL_COLUMNS} FROM mails{sql_where} ORDER BY date DESC, id DESC "
                            "LIMIT ? OFFSET ?", (*args, limit, max(0, offset))).fetchall()
        items = [dict(r) for r in rows]
        inbox_queries.attachment_names(conn, items)
    return {"total": total, "count": len(items), "mails": items}


@router.get("/mails/{mail_id}")
def mail(request: Request, mail_id: int) -> dict:
    s = _settings(request)
    with db.session(s.db_path) as conn:
        row = conn.execute(f"SELECT {MAIL_COLUMNS}, body, message_id, in_reply_to, source_key FROM mails "
                           "WHERE id = ? AND deleted = 0", (mail_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Mail nicht gefunden")
        atts = conn.execute("SELECT id, filename, ext, size, status, message, "
                            "(SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS chunks "
                            "FROM documents d WHERE mail_id = ? ORDER BY id", (mail_id,)).fetchall()
    return {**dict(row), "attachments": [dict(a) for a in atts]}


@router.get("/inbox")
def inbox(request: Request, status: str | None = None, limit: int = inbox_queries.MAX_API_LIMIT,
          offset: int = 0) -> dict:
    """Assignment inbox: mails without a certain project – with or without a suggestion.

    Suggestions first, then the newest. Paged (``limit`` at most 500, ``offset``); ``total`` is the
    number of matching mails, ``count`` the number in this answer. ``status``: suggested | unassigned."""
    s = _settings(request)
    if status and status not in INBOX_STATUSES:
        raise HTTPException(400, f"Unbekannter Status: {status} (erlaubt: {', '.join(INBOX_STATUSES)})")
    limit = max(1, min(limit, inbox_queries.MAX_API_LIMIT))
    with db.session(s.db_path) as conn:
        total = inbox_queries.counts_by_status(conn)[status or ""]
        items = inbox_queries.page_rows(conn, status=status or "", limit=limit, offset=max(0, offset))
    for m in items:
        m["suggested_project_name"] = m.pop("suggested_name")
    return {"total": total, "count": len(items), "offset": max(0, offset), "mails": items}


class AssignRequest(BaseModel):
    action: str = "assign"  # confirm | assign | reject | none
    project: str | None = None


@router.post("/mails/{mail_id}/assign")
def assign(request: Request, mail_id: int, body: AssignRequest) -> dict:
    s = _settings(request)
    user = getattr(request.state, "user", None)
    try:
        with db.session(s.db_path) as conn:
            return decide(conn, mail_id, body.action, body.project, user=user, mode=s.assign_mode)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except DecisionError as exc:
        raise HTTPException(400, str(exc)) from None
    except sqlite3.OperationalError as exc:
        if not inbox_queries.is_db_busy(exc):
            raise
        log.warning("Entscheidung zu Mail %s nicht gespeichert – Datenbank belegt: %s", mail_id, exc)
        raise HTTPException(503, "Die Datenbank ist gerade belegt (Indexierung) – die Entscheidung wurde nicht "
                                 "gespeichert. Bitte in einigen Sekunden erneut versuchen.",
                            headers={"Retry-After": str(BUSY_RETRY_AFTER)}) from None


@router.get("/search")
def search(request: Request, q: str, project: str | None = None, k: int = 10) -> dict:
    s = _settings(request)
    if not q.strip():
        raise HTTPException(400, "Suchbegriff fehlt")
    with db.session(s.db_path) as conn:
        if project:
            p = get_project(conn, project)
            if not p:
                raise HTTPException(404, f"Projekt {project} nicht gefunden")
            project = p["number"]
        res = hybrid_search(conn, request.app.state.ai, str(s.db_path), q, project=project,
                            k=max(1, min(k, 50)), rerank_top_k=s.rerank_top_k,
                            synonyms_file=s.synonyms_file)
    return {"query": res.query, "project": res.project, "reranked": res.reranked, "notes": res.notes,
            "hits": [h.to_dict() for h in res.hits]}


class AskRequest(BaseModel):
    question: str
    project: str | None = None


@router.post("/ask")
def ask(request: Request, body: AskRequest) -> dict:
    s = _settings(request)
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Frage fehlt")
    if len(question) > 2000:
        raise HTTPException(400, "Frage zu lang (max. 2000 Zeichen)")
    with db.session(s.db_path) as conn:
        project = None
        if body.project:
            p = get_project(conn, body.project)
            if not p:
                raise HTTPException(404, f"Projekt {body.project} nicht gefunden")
            project = p["number"]
        result = answer_question(conn, request.app.state.ai, str(s.db_path), question, project=project,
                                 top_k=s.ask_top_k, context_chars=s.ask_context_chars,
                                 min_rerank=s.ask_min_rerank, rerank_top_k=s.rerank_top_k,
                                 synonyms_file=s.synonyms_file, deadline_s=s.ask_deadline)
    return result.to_dict()


@router.get("/status")
def status(request: Request) -> dict:
    """Indexing state and counts – cheap, no AI calls."""
    s = _settings(request)
    with db.session(s.db_path) as conn:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        counts = {
            "projects": one("SELECT COUNT(*) FROM projects WHERE active = 1 AND kind = 'projekt'"),
            "bauvorhaben": one("SELECT COUNT(*) FROM projects WHERE active = 1 AND kind = 'bauvorhaben'"),
            "documents": one("SELECT COUNT(*) FROM documents WHERE mail_id IS NULL"),
            "documents_skipped": one("SELECT COUNT(*) FROM documents WHERE status != 'ok'"),
            "mails": one("SELECT COUNT(*) FROM mails WHERE deleted = 0"),
            "inbox": one("SELECT COUNT(*) FROM mails WHERE deleted = 0 AND "
                         "assign_status IN ('suggested', 'unassigned')"),
            "plmail_open": one("SELECT COUNT(*) FROM mails WHERE deleted = 0 AND assign_status = 'plmail_open'"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "chunks_without_vector": one("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL"),
        }
        # phase "Bildauswertung": scan pages waiting for the model, older PDFs still to be checked for them
        pending = vision.pending_counts(conn)
        counts.update(vision_pending_pages=pending["pages"], vision_pending_documents=pending["documents"],
                      vision_recheck=pending["recheck"])
        # which folders count as projects: effective PROJECT_NUMBER_PATTERN and PROJECT_FOLDER_DEPTH, with
        # "auto" also a proposal waiting for confirmation and the evidence for it
        layout = structure.current_layout(s, conn).to_dict()
    # folder names that are no valid UTF-8 (in warnings, too) shown with �: the answer must stay valid JSON
    return structure.clean({"indexing": request.app.state.indexer.status(), "counts": counts,
                            "ai_mode": s.ai_mode, "demo_mode": s.demo_mode, "assign_mode": s.assign_mode,
                            "layout": layout})


@router.post("/reindex", status_code=202)
def reindex(request: Request) -> dict:
    request.app.state.indexer.trigger()
    return {"status": "angestoßen"}


# -- order of the documents pass (reihenfolge.py; the section "Reihenfolge" of the status page) ---------
RF_MAX_LIMIT = 500


def _rf_data(request: Request, tab: str = "", q: str = "", limit: int = 50) -> dict:
    from .web import reihenfolge_context

    with db.session(_settings(request).db_path) as conn:
        rf = reihenfolge_context(request, conn, tab, q, limit=max(1, min(limit, RF_MAX_LIMIT)))
    now = rf["now"]
    projects = [{k: p[k] for k in ("position", "number", "name", "kind", "folder", "files", "state", "done_at",
                                   "skipped")} for p in rf["rows"]]
    return structure.clean({
        "folders": [{"name": f["name"], "label": f["label"], "enabled": f["enabled"], "rank": f["rank"],
                     "projects": f["total"], "done": f["finished"]} for f in rf["folders"]],
        "customized": rf["customized"], "default_order": rf["default_order"],
        "current": {"running": now["running"], "phase": now["phase_detail"], "project": now["project"],
                    "files_done": now["seen"], "files_total": now["total"], "next": now["upcoming"]},
        "queue": rf["queue_count"], "done": rf["done_count"], "tab": rf["tab"], "q": rf["q"],
        "total": rf["total"], "count": len(projects), "projects": projects})


@router.get("/reihenfolge")
def order(request: Request, tab: str = "", q: str = "", limit: int = 50) -> dict:
    """Order of the documents pass: the top-level folders, what is read right now, and the projects of a
    tab (``warteschlange`` default, ``fertig``, ``alle``) – ``q`` filters by (part of) the number."""
    return _rf_data(request, tab, q, limit)


class FolderOrderRequest(BaseModel):
    order: list[str] = []  # these folders first, in this order; the others keep theirs after them
    off: list[str] = []  # switched off: their projects come last
    reset: bool = False  # back to INDEX_FOLDER_ORDER and the default


class PinRequest(BaseModel):
    number: str


def _rf_write(request: Request, action) -> dict:
    s = _settings(request)
    try:
        with db.session(s.db_path) as conn:
            action(conn, s)
    except reihenfolge.OrderError as exc:
        raise HTTPException(400, str(exc)) from None
    except sqlite3.OperationalError as exc:
        if not inbox_queries.is_db_busy(exc):
            raise
        raise HTTPException(503, "Die Datenbank ist gerade belegt (Indexierung) – die Änderung wurde nicht "
                                 "gespeichert. Bitte in einigen Sekunden erneut versuchen.",
                            headers={"Retry-After": str(BUSY_RETRY_AFTER)}) from None
    return _rf_data(request)


@router.post("/reihenfolge/ordner")
def order_folders(request: Request, body: FolderOrderRequest) -> dict:
    def action(conn, s):
        if body.reset:
            reihenfolge.reset_folders(conn)
        else:
            reihenfolge.set_folder_order(conn, s.projects_dir, s.index_folder_order, body.order, body.off)
    return _rf_write(request, action)


@router.post("/reihenfolge/vorziehen")
def order_pin(request: Request, body: PinRequest) -> dict:
    return _rf_write(request, lambda conn, s: reihenfolge.pin(conn, body.number))


@router.post("/reihenfolge/zurueck")
def order_unpin(request: Request, body: PinRequest) -> dict:
    return _rf_write(request, lambda conn, s: reihenfolge.unpin(conn, body.number))
