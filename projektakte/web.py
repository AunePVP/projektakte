"""Server-rendered web interface (Jinja2 + HTMX, German). Every form also works without JavaScript."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import __version__, db
from .ask import ask as answer_question
from .auth import COOKIE, safe_next
from .assign import INBOX_STATUSES, DecisionError, decide
from .registry import get_project, list_projects
from .search import hybrid_search

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Berlin")
except Exception:  # noqa: BLE001 – no tz database: show UTC
    LOCAL_TZ = timezone.utc


def de_date(value: str | None, with_time: bool = False) -> str:
    if not value:
        return "–"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone(LOCAL_TZ)
    return dt.strftime("%d.%m.%Y %H:%M" if with_time and len(value) > 10 else "%d.%m.%Y")


def filesize(n: int | None) -> str:
    if not n:
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return str(n)


def cite_links(text: str, prefix: str = "src") -> Markup:
    """Escape an answer and turn [1], [2, 3] into links to the source list."""
    def repl(m: re.Match) -> str:
        nums = re.findall(r"\d+", m.group(0))
        return "[" + ", ".join(f'<a href="#{prefix}-{n}" class="cite">{n}</a>' for n in nums) + "]"
    return Markup(re.sub(r"\[(\d+(?:\s*[,;–-]\s*\d+)*)\]", repl, str(escape(text))))


ASSIGN_LABELS = {
    "assigned": "sicher zugeordnet", "suggested": "Vorschlag", "unassigned": "offen",
    "confirmed": "bestätigt", "none": "ohne Projektbezug", "pending": "wird verarbeitet",
    "plmail_open": "in plMail offen",
}
METHOD_LABELS = {"subject": "Nummer im Betreff", "body": "Nummer im Text", "attachment": "Nummer im Anhangnamen",
                 "manual": "von Hand", "similarity": "inhaltlich", "plmail": "in plMail"}
DOC_STATUS = {"ok": "", "skipped": "übersprungen", "error": "Fehler", "empty": "ohne Text"}

templates.env.filters.update(de_date=de_date, filesize=filesize, cite_links=cite_links)
templates.env.globals.update(ASSIGN_LABELS=ASSIGN_LABELS, METHOD_LABELS=METHOD_LABELS, DOC_STATUS=DOC_STATUS,
                             version=__version__)


def _settings(request: Request):
    return request.app.state.settings


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    s = _settings(request)
    with db.session(s.db_path) as conn:
        inbox_count = conn.execute(
            "SELECT COUNT(*) FROM mails WHERE deleted = 0 AND assign_status IN (?, ?)", INBOX_STATUSES
        ).fetchone()[0]
    ctx.update(request=request, inbox_count=inbox_count, user=getattr(request.state, "user", None),
               indexing=request.app.state.indexer.status(), demo_mode=s.demo_mode, ai_mode=s.ai_mode)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _project_rows(conn, q: str) -> list[dict]:
    projects = list_projects(conn, q)
    stats = {r["project"]: r for r in conn.execute(
        "SELECT project, SUM(kind = 'doc') AS docs, SUM(kind = 'mail') AS mails, MAX(date) AS last FROM ("
        " SELECT project, 'doc' AS kind, doc_date AS date FROM documents WHERE mail_id IS NULL"
        " UNION ALL SELECT project, 'mail', substr(date, 1, 10) FROM mails WHERE deleted = 0"
        ") WHERE project IS NOT NULL GROUP BY project")}
    for p in projects:
        st = stats.get(p["number"])
        p["docs"], p["mails"], p["last"] = (st["docs"], st["mails"], st["last"]) if st else (0, 0, None)
    return projects


# ------------------------------------------------------------------------------------- pages
@router.get("/", response_class=HTMLResponse)
def projects_page(request: Request, q: str = ""):
    with db.session(_settings(request).db_path) as conn:
        projects = _project_rows(conn, q)
    if is_htmx(request):
        return templates.TemplateResponse(request, "_project_rows.html", {"projects": projects, "q": q})
    return render(request, "projects.html", projects=projects, q=q)


@router.get("/projekte/{number}", response_class=HTMLResponse)
def project_page(request: Request, number: str):
    s = _settings(request)
    with db.session(s.db_path) as conn:
        p = get_project(conn, number)
        if not p:
            raise HTTPException(404, f"Projekt {number} nicht gefunden")
        docs = [dict(r) for r in conn.execute(
            "SELECT id, rel_path, filename, ext, size, doc_date, pages, status, message, method FROM documents "
            "WHERE project = ? AND mail_id IS NULL ORDER BY rel_path", (p["number"],))]
        mails = [dict(r) for r in conn.execute(
            "SELECT id, subject, sender, sender_name, date, assign_status, assign_method, has_attachments "
            "FROM mails WHERE project = ? AND deleted = 0 ORDER BY date DESC", (p["number"],))]
        suggested = conn.execute("SELECT COUNT(*) FROM mails WHERE suggested_project = ? AND deleted = 0 "
                                 "AND assign_status = 'suggested'", (p["number"],)).fetchone()[0]
    timeline = sorted(
        [{"kind": "doc", "date": d["doc_date"], "title": d["rel_path"], "id": d["id"], "sub": d["ext"][1:].upper()}
         for d in docs]
        + [{"kind": "mail", "date": m["date"], "title": m["subject"], "id": m["id"],
            "sub": m["sender_name"] or m["sender"]} for m in mails],
        key=lambda e: e["date"] or "", reverse=True)
    try:
        p["rel"] = str(Path(p["path"]).relative_to(s.projects_dir))
    except ValueError:
        p["rel"] = p["path"]
    return render(request, "project.html", p=p, docs=docs, mails=mails, timeline=timeline,
                  suggested=suggested)


@router.get("/fragen", response_class=HTMLResponse)
def ask_page(request: Request, project: str | None = None):
    with db.session(_settings(request).db_path) as conn:
        projects = list_projects(conn)
    return render(request, "ask.html", projects=projects, project=project, result=None, question="")


@router.post("/fragen", response_class=HTMLResponse)
def ask_submit(request: Request, question: str = Form(""), project: str = Form("")):
    s = _settings(request)
    question = question.strip()[:2000]
    with db.session(s.db_path) as conn:
        p = get_project(conn, project) if project else None
        result = None
        if question:
            result = answer_question(conn, request.app.state.ai, str(s.db_path), question,
                                     project=p["number"] if p else None, top_k=s.ask_top_k,
                                     context_chars=s.ask_context_chars, min_rerank=s.ask_min_rerank,
                                     rerank_top_k=s.rerank_top_k)
        projects = list_projects(conn)
    if is_htmx(request):
        return templates.TemplateResponse(request, "_answer.html", {"result": result, "question": question})
    return render(request, "ask.html", projects=projects, project=p["number"] if p else None,
                  result=result, question=question)


@router.get("/suche", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", project: str = ""):
    s = _settings(request)
    res = None
    with db.session(s.db_path) as conn:
        p = get_project(conn, project) if project else None
        if q.strip():
            res = hybrid_search(conn, request.app.state.ai, str(s.db_path), q.strip(),
                                project=p["number"] if p else None, k=20, rerank_top_k=s.rerank_top_k)
        projects = list_projects(conn)
    return render(request, "search.html", q=q, project=p["number"] if p else "", projects=projects, res=res)


def _inbox_rows(conn) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT m.id, m.subject, m.sender, m.sender_name, m.date, m.preview, m.assign_status, m.assign_reason, "
        "m.suggested_project, m.suggestion_score, p.name AS suggested_name FROM mails m "
        "LEFT JOIN projects p ON p.number = m.suggested_project WHERE m.deleted = 0 AND m.assign_status IN "
        "(?, ?) ORDER BY (m.assign_status = 'suggested') DESC, m.date DESC", INBOX_STATUSES)]
    for m in rows:
        m["attachments"] = [r[0] for r in conn.execute(
            "SELECT filename FROM documents WHERE mail_id = ? ORDER BY id", (m["id"],))]
    return rows


@router.get("/eingang", response_class=HTMLResponse)
def inbox_page(request: Request):
    with db.session(_settings(request).db_path) as conn:
        mails = _inbox_rows(conn)
        projects = list_projects(conn)
    return render(request, "inbox.html", mails=mails, projects=projects,
                  assign_mode=_settings(request).assign_mode)


@router.post("/eingang/{mail_id}", response_class=HTMLResponse)
def inbox_decide(request: Request, mail_id: int, action: str = Form(...), project: str = Form(""),
                 back: str = Form("/eingang")):
    s = _settings(request)
    error = None
    with db.session(s.db_path) as conn:
        try:
            result = decide(conn, mail_id, action, project or None, user=getattr(request.state, "user", None),
                            mode=s.assign_mode)
        except LookupError:
            raise HTTPException(404, "Mail nicht gefunden") from None
        except DecisionError as exc:
            result, error = None, str(exc)
        mail = conn.execute("SELECT id, subject, sender, sender_name, date FROM mails WHERE id = ?",
                            (mail_id,)).fetchone()
        name = None
        if result and result["project"]:
            r = conn.execute("SELECT name FROM projects WHERE number = ?", (result["project"],)).fetchone()
            name = r[0] if r else None
    if is_htmx(request):
        return templates.TemplateResponse(request, "_inbox_done.html",
                                          {"mail": dict(mail), "result": result, "error": error, "name": name})
    if not back.startswith("/") or back.startswith("//"):
        back = "/eingang"
    return RedirectResponse(back, status_code=303)


@router.get("/mails/{mail_id}", response_class=HTMLResponse)
def mail_page(request: Request, mail_id: int):
    with db.session(_settings(request).db_path) as conn:
        m = conn.execute("SELECT m.*, p.name AS project_name FROM mails m LEFT JOIN projects p ON "
                         "p.number = m.project WHERE m.id = ? AND m.deleted = 0", (mail_id,)).fetchone()
        if not m:
            raise HTTPException(404, "Mail nicht gefunden")
        atts = [dict(r) for r in conn.execute(
            "SELECT id, filename, ext, size, status, message FROM documents WHERE mail_id = ? ORDER BY id",
            (mail_id,))]
        projects = list_projects(conn)
    s = _settings(request)
    m = dict(m)
    in_plmail = m["source"] == "jmap" and (s.assign_mode == "manual" or bool(m.get("external_assignment")))
    plmail_link = None
    if m["source"] == "jmap" and s.plmail_web_url:
        _, account_id, email_id = m["source_key"].split(":", 2)
        plmail_link = s.plmail_web_url.replace("{accountId}", account_id).replace("{emailId}", email_id)
    return render(request, "mail.html", m=m, atts=atts, projects=projects, in_plmail=in_plmail,
                  plmail_link=plmail_link)


@router.get("/dokumente/{doc_id}", response_class=HTMLResponse)
def document_page(request: Request, doc_id: int):
    with db.session(_settings(request).db_path) as conn:
        d = conn.execute("SELECT d.*, p.name AS project_name FROM documents d LEFT JOIN projects p ON "
                         "p.number = d.project WHERE d.id = ?", (doc_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Dokument nicht gefunden")
        chunks = [dict(r) for r in conn.execute(
            "SELECT id, ord, page, heading, text FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,))]
        mail = None
        if d["mail_id"]:
            mail = conn.execute("SELECT id, subject, date FROM mails WHERE id = ?", (d["mail_id"],)).fetchone()
    return render(request, "document.html", d=dict(d), chunks=chunks, mail=dict(mail) if mail else None)


@router.get("/dokumente/{doc_id}/datei")
def document_file(request: Request, doc_id: int):
    """Original file (read-only). Only paths known to the index and below the configured roots."""
    s = _settings(request)
    with db.session(s.db_path) as conn:
        d = conn.execute("SELECT path, cache_path, filename, mail_id FROM documents WHERE id = ?",
                         (doc_id,)).fetchone()
    if not d:
        raise HTTPException(404, "Dokument nicht gefunden")
    path = Path(d["cache_path"] if d["mail_id"] else d["path"]).resolve()
    roots = [s.projects_dir.resolve(), s.cache_dir.resolve()]
    if not any(path.is_relative_to(r) for r in roots) or not path.is_file():
        raise HTTPException(404, "Datei nicht verfügbar")
    inline = path.suffix.lower() in (".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg")
    return FileResponse(path, filename=d["filename"],
                        content_disposition_type="inline" if inline else "attachment")


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    from .api import status as api_status

    return render(request, "status.html", st=api_status(request))


@router.get("/status/dienste", response_class=HTMLResponse)
def deps_partial(request: Request):
    from .main import health_deps_data

    return templates.TemplateResponse(request, "_deps.html", {"deps": health_deps_data(request.app)})


@router.post("/status/neu-indexieren")
def reindex_now(request: Request):
    request.app.state.indexer.trigger()
    return RedirectResponse("/status", status_code=303)


# ------------------------------------------------------------------------------------- login
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):  # noqa: A002
    auth = request.app.state.auth
    if request.state.user or auth.disabled:
        return RedirectResponse(safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": None,
                                                              "no_users": not auth.users})


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                 next: str = Form("/")):  # noqa: A002
    auth = request.app.state.auth
    client = request.client.host if request.client else ""
    if auth.check_credentials(username.strip(), password, client):
        resp = RedirectResponse(safe_next(next), status_code=303)
        resp.set_cookie(COOKIE, auth.make_cookie(username.strip()), max_age=auth.session_seconds,
                        httponly=True, samesite="lax", secure=auth.cookie_secure, path="/")
        return resp
    error = ("Zu viele Fehlversuche – bitte einige Minuten warten."
             if auth.locked(f"{client}|{username.strip().lower()}") else "Benutzername oder Passwort falsch.")
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": error,
                                                              "no_users": not auth.users}, status_code=401)


@router.post("/logout")
def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp
