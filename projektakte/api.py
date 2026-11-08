"""JSON API."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from . import db
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
