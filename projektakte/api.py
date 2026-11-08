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
