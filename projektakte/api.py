"""JSON API."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import db
from .assign import INBOX_STATUSES, DecisionError, decide
from .search import hybrid_search
from .registry import get_project, list_projects

router = APIRouter(prefix="/api")


def _settings(request: Request):
    return request.app.state.settings


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path


@router.get("/projects")
def projects(request: Request, q: str = "") -> dict:
    s = _settings(request)
    with db.session(s.db_path) as conn:
        items = list_projects(conn, q)
    for p in items:
        p["path"] = _rel(p["path"], s.projects_dir)
    return {"count": len(items), "projects": items}


@router.get("/projects/{number}")
def project(request: Request, number: str) -> dict:
    s = _settings(request)
    with db.session(s.db_path) as conn:
        p = get_project(conn, number)
    if not p:
        raise HTTPException(404, f"Projekt {number} nicht gefunden")
    p["path"] = _rel(p["path"], s.projects_dir)
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
        for m in items:
            m["attachments"] = [r[0] for r in conn.execute(
                "SELECT filename FROM documents WHERE mail_id = ? ORDER BY id", (m["id"],))]
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
def inbox(request: Request) -> dict:
    """Assignment inbox: mails without a certain project – with or without a suggestion."""
    s = _settings(request)
    with db.session(s.db_path) as conn:
        rows = conn.execute(
            f"SELECT {MAIL_COLUMNS} FROM mails WHERE deleted = 0 AND assign_status IN (?, ?) "
            "ORDER BY (assign_status = 'suggested') DESC, date DESC", INBOX_STATUSES).fetchall()
        items = [dict(r) for r in rows]
        names = dict(conn.execute("SELECT number, name FROM projects").fetchall())
        for m in items:
            m["suggested_project_name"] = names.get(m["suggested_project"])
            m["attachments"] = [r[0] for r in conn.execute(
                "SELECT filename FROM documents WHERE mail_id = ? ORDER BY id", (m["id"],))]
    return {"count": len(items), "mails": items}


class AssignRequest(BaseModel):
    action: str = "assign"  # confirm | assign | reject | none
    project: str | None = None


@router.post("/mails/{mail_id}/assign")
def assign(request: Request, mail_id: int, body: AssignRequest) -> dict:
    s = _settings(request)
    user = getattr(request.state, "user", None)
    with db.session(s.db_path) as conn:
        try:
            return decide(conn, mail_id, body.action, body.project, user=user)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except DecisionError as exc:
            raise HTTPException(400, str(exc)) from None


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
                            k=max(1, min(k, 50)), rerank_top_k=s.rerank_top_k)
    return {"query": res.query, "project": res.project, "reranked": res.reranked, "notes": res.notes,
            "hits": [h.to_dict() for h in res.hits]}
