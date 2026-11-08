"""Integration API for plMail (docs/plmail-integration.md).

plMail calls these endpoints server-side with ``Authorization: Bearer $INTEGRATION_TOKEN`` while a
human reads a mail: it shows Projektakte's projects (and a suggestion) and stores the human's choice
in plMail itself. Projektakte then reads that choice via JMAP. Nothing here changes an assignment.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import __version__, db
from .assign import suggest_for_text
from .mails.jmap_source import PROJECT_CAP, PROJECT_PROP

router = APIRouter(prefix="/api/integration", tags=["integration"])
MAX_TEXT = 20_000


def _base_url(request: Request) -> str:
    s = request.app.state.settings
    return s.public_url or str(request.base_url).rstrip("/")


@router.get("/info")
def info(request: Request) -> dict:
    """What plMail needs to know about this Projektakte (for its own status/health display)."""
    s = request.app.state.settings
    with db.session(s.db_path) as conn:
        projects = conn.execute("SELECT COUNT(*) FROM projects WHERE active = 1").fetchone()[0]
    return {"name": "Projektakte", "version": __version__, "assign_mode": s.assign_mode,
            "projects": projects, "jmap": {"capability": PROJECT_CAP, "property": PROJECT_PROP},
            "url": _base_url(request)}


@router.get("/projects")
def projects(request: Request, q: str = "", limit: int = 500) -> dict:
    """Active projects for plMail's project picker (number, name, link to the project page)."""
    s = request.app.state.settings
    sql, args = "SELECT number, name FROM projects WHERE active = 1", []
    if q.strip():
        sql += " AND (number LIKE ? OR name LIKE ?)"
        args += [f"%{q.strip()}%"] * 2
    sql += " ORDER BY number DESC LIMIT ?"
    args.append(max(1, min(limit, 5000)))
    base = _base_url(request)
    with db.session(s.db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return {"count": len(rows), "projects": [
        {"number": r["number"], "name": r["name"], "url": f"{base}/projekte/{r['number']}"} for r in rows]}


class SuggestRequest(BaseModel):
    subject: str = ""
    body: str = ""
    sender: str = Field("", alias="from")
    attachments: list[str] = []
    limit: int = 3
    # optional mail headers; only newsletter markers are used (List-Unsubscribe, Precedence)
    headers: dict[str, str] = {}

    model_config = {"populate_by_name": True}


@router.post("/suggest")
def suggest(request: Request, req: SuggestRequest) -> dict:
    """Project suggestions for the mail a human is reading. Stores and assigns nothing.

    Newsletters and advertising (by header, sender address or wording) get no content suggestion."""
    s = request.app.state.settings
    if not (req.subject.strip() or req.body.strip() or req.attachments):
        raise HTTPException(400, "Betreff, Text oder Anhangnamen fehlen")
    base = _base_url(request)
    with db.session(s.db_path) as conn:
        items = suggest_for_text(
            conn, request.app.state.ai, str(s.db_path), s.project_number_pattern,
            subject=req.subject[:1000], body=req.body[:MAX_TEXT], attachments=[a[:300] for a in req.attachments[:50]],
            sender=req.sender[:300], limit=max(1, min(req.limit, 10)),
            min_share=s.assign_min_share, min_lead=s.assign_min_lead,
            min_similarity=getattr(s, "assign_min_similarity", 0.0),
            headers={str(k)[:100]: str(v)[:1000] for k, v in list(req.headers.items())[:50]})
    for it in items:
        it["url"] = f"{base}/projekte/{it['number']}"
    return {"suggestions": items}


@router.post("/notify", status_code=202)
def notify(request: Request) -> dict:
    """plMail calls this after a human (re)assigned a mail, so Projektakte syncs right away instead of
    waiting for the next INDEX_INTERVAL. The body is ignored; the change itself is read via JMAP."""
    request.app.state.indexer.trigger()
    return {"status": "angestoßen"}
