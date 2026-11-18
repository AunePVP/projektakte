"""Integration API for plMail (docs/plmail-integration.md, docs/plmail-push.md).

plMail calls these endpoints server-side with ``Authorization: Bearer $INTEGRATION_TOKEN`` while a
human reads a mail: it shows Projektakte's projects (and a suggestion) and stores the human's choice
in plMail itself. With push (``/mails/…``) plMail also sends the mails a human assigned to a project –
Projektakte then needs no access to the mailbox.
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import __version__, db, structure
from .assign import suggest_for_text
from .mails import push
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
        # "projects" stays the number of projects (orders); sites (Bauvorhaben) are counted on their own
        projects, sites = conn.execute(
            "SELECT COALESCE(SUM(kind != 'bauvorhaben'), 0), COALESCE(SUM(kind = 'bauvorhaben'), 0) FROM projects "
            "WHERE active = 1").fetchone()
    return {"name": "Projektakte", "version": __version__, "assign_mode": s.assign_mode,
            "projects": projects, "bauvorhaben": sites, "jmap": {"capability": PROJECT_CAP, "property": PROJECT_PROP},
            "url": _base_url(request)}


@router.get("/projects")
def projects(request: Request, q: str = "", limit: int = 500) -> dict:
    """Active projects for plMail's project picker (number, name, link to the project page); ``kind``
    'bauvorhaben' marks a site (a person may assign a mail to it), ``parent`` the site of an order."""
    s = request.app.state.settings
    sql, args = "SELECT number, name, kind, parent FROM projects WHERE active = 1", []
    if q.strip():
        sql += " AND (number LIKE ? OR name LIKE ?)"
        args += [f"%{q.strip()}%"] * 2
    sql += " ORDER BY number DESC LIMIT ?"
    args.append(max(1, min(limit, 5000)))
    base = _base_url(request)
    with db.session(s.db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return {"count": len(rows), "projects": [
        {"number": r["number"], "name": r["name"], "kind": r["kind"], "parent": r["parent"],
         "url": f"{base}/projekte/{r['number']}"} for r in rows]}


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
        # numbers in the mail are found with the effective scheme (configured, or detected with "auto");
        # while there is none, only the content vote suggests
        items = suggest_for_text(
            conn, request.app.state.ai, str(s.db_path), structure.number_pattern(s, conn),
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


# -- push: plMail sends the mails a human assigned (docs/plmail-push.md) ----------------------------------
def _ids(account_id: str, email_id: str) -> tuple[str, str]:
    try:
        return push.check_id(account_id, "accountId"), push.check_id(email_id, "emailId")
    except push.PushError as exc:
        raise HTTPException(400, str(exc)) from None


@router.put("/mails/{account_id}/{email_id}", status_code=202)
async def put_mail(request: Request, account_id: str, email_id: str) -> dict:
    """The raw mail (body, ``Content-Type: message/rfc822``) with its assignment (header
    ``X-Projektakte-Zuordnung``, JSON like the JMAP property ``plmail:projektakte``). Only mails assigned to a
    project are taken. Waits in DATA_DIR/plmail-eingang and is imported by the indexer right after."""
    account_id, email_id = _ids(account_id, email_id)
    s = request.app.state.settings
    try:
        assignment = push.assignment_or_error(json.loads(request.headers.get("x-projektakte-zuordnung") or "null"))
    except (ValueError, push.PushError) as exc:
        raise HTTPException(400, str(exc) if isinstance(exc, push.PushError) else
                            "X-Projektakte-Zuordnung ist kein JSON") from None
    if assignment["status"] != "assigned":
        raise HTTPException(422, "Nur Mails mit Projektzuordnung senden – für „kein Projektbezug“ DELETE verwenden")
    upload = push.new_upload(s.data_dir)
    size = 0
    try:
        with upload.open("wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > push.MAX_BYTES:
                    raise HTTPException(413, f"Mail größer als {push.MAX_BYTES // (1024 * 1024)} MB")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "Leerer Inhalt – die Mail als .eml (message/rfc822) senden")
        push.spool(s.data_dir, account_id, email_id, upload, assignment)
    finally:
        if upload.exists():
            os.unlink(upload)
    request.app.state.indexer.trigger()
    return {"status": "angenommen", "bytes": size}


class Zuordnung(BaseModel):
    status: str
    project: str | None = None
    projectName: str = ""  # noqa: N815 – the JMAP property's field names
    assignedBy: str = ""  # noqa: N815
    assignedAt: str = ""  # noqa: N815


@router.put("/mails/{account_id}/{email_id}/zuordnung")
def put_assignment(request: Request, account_id: str, email_id: str, body: Zuordnung) -> dict:
    """New assignment of a mail sent before. "assigned" updates it; "none" (kein Projektbezug) deletes it.
    ``unbekannt``: Projektakte does not have the mail – send it with PUT /mails/… first."""
    account_id, email_id = _ids(account_id, email_id)
    s = request.app.state.settings
    try:
        assignment = push.assignment_or_error(body.model_dump())
    except push.PushError as exc:
        raise HTTPException(400, str(exc)) from None
    with db.session(s.db_path) as conn:
        result = push.reassign(conn, s.data_dir, s.cache_dir, account_id, email_id, assignment)
    if result == "unbekannt":
        raise HTTPException(404, "Mail nicht vorhanden – zuerst die Mail senden (PUT /api/integration/mails/…)")
    return {"status": result}


@router.delete("/mails/{account_id}/{email_id}")
def delete_mail(request: Request, account_id: str, email_id: str) -> dict:
    """Assignment taken back, "kein Projektbezug" or mail deleted in plMail: remove the copy with its
    attachments. Answers 200 also when Projektakte did not have it (repeating is harmless)."""
    account_id, email_id = _ids(account_id, email_id)
    s = request.app.state.settings
    with db.session(s.db_path) as conn:
        removed = push.delete(conn, s.data_dir, s.cache_dir, account_id, email_id)
    return {"status": "gelöscht" if removed else "nicht vorhanden"}


@router.get("/mails/{account_id}/{email_id}")
def get_mail(request: Request, account_id: str, email_id: str) -> dict:
    """Does Projektakte have this mail (stored or waiting)? For plMail's resend after an outage."""
    account_id, email_id = _ids(account_id, email_id)
    s = request.app.state.settings
    with db.session(s.db_path) as conn:
        return push.present(conn, s.data_dir, account_id, email_id)
