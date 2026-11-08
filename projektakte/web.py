"""Server-rendered web interface (Jinja2 + HTMX, German). Every form also works without JavaScript.

The templates contain no inline scripts, style blocks or style attributes (Content Security Policy
``default-src 'self'``); all styles live in static/app.css. Login and logout are in auth_web.py.
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import __version__, db, inbox
from .ask import ask as answer_question, cited_numbers
from .assign import INBOX_STATUSES, DecisionError, decide
from .registry import get_project, list_projects
from .search import hybrid_search
from .textutil import QUESTION_WORDS, content_terms, fold, stem

log = logging.getLogger("projektakte.web")

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Berlin")
except Exception:  # noqa: BLE001 – no tz database: show UTC
    LOCAL_TZ = timezone.utc

MAX_QUERY = 2000  # characters of a question or search query
SEARCH_HITS = 20
MISS_SOURCES = 3  # "Mögliche Fundstellen" shown when there is no cited answer
BUSY_TEXT = ("Die Datenbank ist gerade belegt (die Indexierung schreibt) – die Entscheidung wurde nicht "
             "gespeichert. Bitte in einigen Sekunden erneut versuchen.")
BUSY_RETRY_AFTER = 5  # seconds


# ------------------------------------------------------------------------------------- formatting
def local_datetime(value: str | None) -> datetime | date | None:
    """ISO date/datetime as shown to people: datetimes with a time zone (mails are stored in UTC) in
    local time (Europe/Berlin), plain dates unchanged. None if missing or unreadable."""
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if len(text) <= 10:
        return dt.date()
    return dt.astimezone(LOCAL_TZ) if dt.tzinfo is not None else dt


def local_day(value: str | None) -> date | None:
    """Local calendar day of an ISO date/datetime (a mail at 00:30 in Berlin belongs to that day)."""
    dt = local_datetime(value)
    return dt.date() if isinstance(dt, datetime) else dt


def local_sort_key(value: str | None) -> str:
    """Sortable local wall-clock time ('2024-06-18' < '2024-06-18T00:30:00'); '' if missing."""
    dt = local_datetime(value)
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=None).isoformat()
    return dt.isoformat()


def de_date(value: str | None, with_time: bool = False) -> str:
    """TT.MM.JJJJ (optionally with HH:MM) in local time; '–' if missing."""
    if not value:
        return "–"
    dt = local_datetime(value)
    if dt is None:
        return str(value)
    if with_time and isinstance(dt, datetime):
        return dt.strftime("%d.%m.%Y %H:%M")
    return dt.strftime("%d.%m.%Y")


def de_num(value, digits: int = 0) -> str:
    """German number format: 1.234 and 7,8; '–' if missing."""
    if value is None or value == "":
        return "–"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{x:,.{digits}f}"
    return text.replace(",", "\0").replace(".", ",").replace("\0", ".")


def plural(n: int | None, singular: str, plural_form: str) -> str:
    """'1 Mail', '3 Mails', '1.234 Dokumente'."""
    n = n or 0
    return f"{de_num(n)} {singular if n == 1 else plural_form}"


def filesize(n: int | None) -> str:
    if not n:
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return str(n)


# Bracketed citations as the answer pipeline writes or understands them: [2], [1, 3], [1–3], [Quelle 2].
_CITE_GROUP = re.compile(r"\[\^?\s*(?:Quellen?\s*)?(\d+(?:\s*(?:[-–,;]|und|bis)\s*\d+)*)\s*\]", re.I)
_NO_LIMIT = 10**6


def cite_links(text: str, prefix: str = "src", n_sources: int | None = None) -> Markup:
    """Escape an answer and turn citations into links to the source list.

    Ranges are resolved exactly like ``ask.cited_numbers`` ([1–3] → links 1, 2, 3). With
    ``n_sources`` only numbers 1…n_sources become links; other numbers stay plain text, so no link
    points to a source that is not shown."""
    def repl(m: re.Match) -> str:
        nums = cited_numbers(f"[{m.group(1)}]", _NO_LIMIT)
        if not nums:
            return m.group(0)
        parts = [f'<a href="#{prefix}-{n}" class="cite">{n}</a>'
                 if n_sources is None or 1 <= n <= n_sources else str(n) for n in nums]
        return "[" + ", ".join(parts) + "]"
    return Markup(_CITE_GROUP.sub(repl, str(escape(text))))


_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def highlight(text: str, query: str) -> Markup:
    """Escape ``text`` and mark the words of the search query (<mark>), matching like the search does:
    case and accents ignored, word forms and compounds by prefix in both directions (Rettungsweg →
    Rettungswege, Löschwasserteich → Löschwasser; cf. ``textutil.term_in``)."""
    terms = [t for t in content_terms(query or "", QUESTION_WORDS) if len(t) >= 3 or t.isdigit()]
    if not terms:
        return Markup(escape(text))

    def matches(word: str) -> bool:
        folded = fold(word)
        stemmed = stem(folded)
        return any(stemmed == t or folded == t or (len(t) >= 4 and folded.startswith(t))
                   or (len(folded) >= 6 and t.startswith(folded)) for t in terms)

    out, pos = [], 0
    for m in _TOKEN.finditer(text):
        if matches(m.group(0)):
            out.append(escape(text[pos:m.start()]))
            out.append(Markup("<mark>") + escape(m.group(0)) + Markup("</mark>"))
            pos = m.end()
    out.append(escape(text[pos:]))
    return Markup("").join(out)


def search_url(query: str, project: str | None = None, **extra) -> str:
    params = {"q": query}
    if project:
        params["project"] = project
    params.update({k: v for k, v in extra.items() if v})
    return "/suche?" + urlencode(params)


# ------------------------------------------------------------------------------------- answers
STATUS_TITLES = {
    "no_citation": "Antwort ohne Quellenangabe – bitte prüfen",
    "truncated": "Antwort unvollständig (abgebrochen) – bitte prüfen",
}


def answer_view(result) -> dict:
    """What the answer partial shows for an ``ask.AskResult`` (status contract in ask.py).

    * answered: answer with links, the cited sources under "Quellen" (numbered as in the text) and the
      uncited context collapsed under "Weitere Fundstellen" – so a list starting at [3] is explained.
    * every other status: the message or the marked text, the best (uncited) context passages as
      "Mögliche Fundstellen" (rest collapsed) and a link to the search page with the same question; a
      cut-off answer ("truncated") also keeps its cited sources under "Quellen".
    """
    field = result.get if isinstance(result, dict) else (lambda name: getattr(result, name, None))
    status = field("status")
    context = sorted(field("context") or [], key=lambda s: s["n"])
    sources = field("sources") or []
    cited = sorted(sources, key=lambda s: s["n"]) if status in ("answered", "truncated") else []
    cited_ns = {s["n"] for s in cited}
    rest = [s for s in context if s["n"] not in cited_ns]
    if status == "answered":
        possible, more = [], rest
    else:
        possible, more = rest[:MISS_SOURCES], rest[MISS_SOURCES:]
    return {
        "title": STATUS_TITLES.get(status),
        "message_only": status in ("not_found", "llm_unavailable"),
        "unverified": status == "no_citation",
        "cited": cited,
        "possible": possible,
        "more": more,
        "n_sources": len(context),
        "search_url": search_url(field("question") or "", field("project")),
        "search_prominent": status != "answered",
    }


# ------------------------------------------------------------------------------------- search
def search_view(res, *, min_score: float, show_weak: bool) -> dict:
    """Split search hits into matching and weak ones. Only the reranker gives a comparable score;
    without it (fallback) every hit counts as matching."""
    if res.reranked:
        strong = [h for h in res.hits if (h.rerank or 0.0) >= min_score]
        weak = [h for h in res.hits if (h.rerank or 0.0) < min_score]
    else:
        strong, weak = list(res.hits), []
    return {"strong": strong, "weak": weak, "show_weak": show_weak}


# ------------------------------------------------------------------------------------- labels
ASSIGN_LABELS = {
    "assigned": "sicher zugeordnet", "suggested": "Vorschlag", "unassigned": "offen",
    "confirmed": "bestätigt", "none": "ohne Projektbezug", "pending": "wird verarbeitet",
    "plmail_open": "in plMail offen",
}
METHOD_LABELS = {"subject": "Nummer im Betreff", "body": "Nummer im Text", "attachment": "Nummer im Anhangnamen",
                 "manual": "von Hand", "similarity": "inhaltlich", "plmail": "in plMail"}
DOC_STATUS = {"ok": "", "skipped": "übersprungen", "error": "Fehler", "empty": "ohne Text"}
INBOX_FILTERS = {"": "Alle", "suggested": "Mit Vorschlag", "unassigned": "Ohne Vorschlag"}


def auth_disabled_for(request: Request) -> bool:
    auth = getattr(request.app.state, "auth", None)
    return bool(getattr(auth, "disabled", False))


templates.env.filters.update(de_date=de_date, de_num=de_num, plural=plural, filesize=filesize,
                             cite_links=cite_links, highlight=highlight)
templates.env.globals.update(ASSIGN_LABELS=ASSIGN_LABELS, METHOD_LABELS=METHOD_LABELS, DOC_STATUS=DOC_STATUS,
                             INBOX_FILTERS=INBOX_FILTERS, version=__version__, answer_view=answer_view,
                             auth_disabled_for=auth_disabled_for)


# ------------------------------------------------------------------------------------- helpers
def _settings(request: Request):
    return request.app.state.settings


def _inbox_count(s) -> int | None:
    try:
        with db.session(s.db_path) as conn:
            return inbox.count_open(conn)
    except sqlite3.OperationalError as exc:  # the page must still render while the database is busy
        log.warning("Zähler des Zuordnungs-Eingangs nicht lesbar: %s", exc)
        return None


def render(request: Request, name: str, status_code: int = 200, *, headers: dict | None = None,
           **ctx) -> HTMLResponse:
    s = _settings(request)
    ctx.update(request=request, inbox_count=_inbox_count(s), user=getattr(request.state, "user", None),
               indexing=request.app.state.indexer.status(), demo_mode=s.demo_mode, ai_mode=s.ai_mode,
               auth_disabled=auth_disabled_for(request))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def safe_back(target: str | None, default: str = "/eingang") -> str:
    """Only local targets for redirects after a form (no //host, no backslash, no control chars)."""
    if (not target or not target.startswith("/") or target.startswith("//") or "\\" in target
            or any(ord(c) < 32 for c in target)):
        return default
    return target


def _int(value: str | int | None, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _project_rows(conn, q: str) -> list[dict]:
    """Projects with counts and the last activity (local day of the newest document or mail)."""
    projects = list_projects(conn, q)
    docs = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT project, COUNT(*), MAX(doc_date) FROM documents WHERE mail_id IS NULL AND project IS NOT NULL "
        "GROUP BY project")}
    # mail dates are stored uniformly as UTC ISO strings, so MAX() picks the newest mail
    mails = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT project, COUNT(*), MAX(date) FROM mails WHERE deleted = 0 AND project IS NOT NULL "
        "GROUP BY project")}
    for p in projects:
        n_docs, last_doc = docs.get(p["number"], (0, None))
        n_mails, last_mail = mails.get(p["number"], (0, None))
        days = [d for d in (local_day(last_doc), local_day(last_mail)) if d]
        p["docs"], p["mails"] = n_docs, n_mails
        p["last"] = max(days).isoformat() if days else None
    return projects


# ------------------------------------------------------------------------------------- pages
@router.get("/", response_class=HTMLResponse)
def projects_page(request: Request, q: str = ""):
    with db.session(_settings(request).db_path) as conn:
        projects = _project_rows(conn, q)
    if is_htmx(request):
        return templates.TemplateResponse(request, "_project_rows.html", {"projects": projects, "q": q})
    return render(request, "projects.html", projects=projects, q=q)


def _project_attachments(conn, number: str) -> list[dict]:
    """Attachments of the mails assigned to a project (newest mail first)."""
    return [dict(r) for r in conn.execute(
        "SELECT d.id, d.filename, d.ext, d.size, d.status, d.message, d.method, m.id AS mail_id, "
        "m.subject, m.date FROM documents d JOIN mails m ON m.id = d.mail_id "
        "WHERE m.project = ? AND m.deleted = 0 ORDER BY m.date DESC, d.id", (number,))]


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
        attachments = _project_attachments(conn, p["number"])
        suggested = conn.execute("SELECT COUNT(*) FROM mails WHERE suggested_project = ? AND deleted = 0 "
                                 "AND assign_status = 'suggested'", (p["number"],)).fetchone()[0]
    atts_by_mail: dict[int, list[dict]] = {}
    for a in attachments:
        atts_by_mail.setdefault(a["mail_id"], []).append(a)
    timeline = sorted(
        [{"kind": "doc", "date": d["doc_date"], "title": d["rel_path"], "id": d["id"], "sub": d["ext"][1:].upper(),
          "atts": []} for d in docs]
        + [{"kind": "mail", "date": m["date"], "title": m["subject"], "id": m["id"],
            "sub": m["sender_name"] or m["sender"], "atts": atts_by_mail.get(m["id"], [])} for m in mails],
        key=lambda e: local_sort_key(e["date"]), reverse=True)
    try:
        p["rel"] = str(Path(p["path"]).relative_to(s.projects_dir))
    except ValueError:
        p["rel"] = p["path"]
    return render(request, "project.html", p=p, docs=docs, mails=mails, attachments=attachments,
                  timeline=timeline, suggested=suggested)


@router.get("/fragen", response_class=HTMLResponse)
def ask_page(request: Request, project: str | None = None):
    with db.session(_settings(request).db_path) as conn:
        projects = list_projects(conn)
    return render(request, "ask.html", projects=projects, project=project, result=None, question="")


@router.post("/fragen", response_class=HTMLResponse)
def ask_submit(request: Request, question: str = Form(""), project: str = Form("")):
    s = _settings(request)
    question = question.strip()[:MAX_QUERY]
    with db.session(s.db_path) as conn:
        p = get_project(conn, project) if project else None
        result = None
        if question:
            result = answer_question(conn, request.app.state.ai, str(s.db_path), question,
                                     project=p["number"] if p else None, top_k=s.ask_top_k,
                                     context_chars=s.ask_context_chars, min_rerank=s.ask_min_rerank,
                                     rerank_top_k=s.rerank_top_k, synonyms_file=s.synonyms_file)
        projects = list_projects(conn)
    if is_htmx(request):
        return templates.TemplateResponse(request, "_answer.html", {"result": result, "question": question})
    return render(request, "ask.html", projects=projects, project=p["number"] if p else None,
                  result=result, question=question)


@router.get("/suche", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", project: str = "", weak: str = ""):
    s = _settings(request)
    query = q.strip()[:MAX_QUERY]
    res = view = None
    with db.session(s.db_path) as conn:
        p = get_project(conn, project) if project else None
        number = p["number"] if p else ""
        if query:
            res = hybrid_search(conn, request.app.state.ai, str(s.db_path), query, project=number or None,
                                k=SEARCH_HITS, rerank_top_k=s.rerank_top_k, synonyms_file=s.synonyms_file)
        projects = list_projects(conn)
    if res is not None:
        view = search_view(res, min_score=getattr(s, "search_min_rerank", 0.05), show_weak=weak == "1")
    return render(request, "search.html", q=query, project=number, projects=projects, res=res, view=view,
                  weak_url=search_url(query, number, weak="1"), strong_url=search_url(query, number))


# ------------------------------------------------------------------------------------- inbox
def inbox_url(status: str = "", page: int = 1) -> str:
    params = {k: v for k, v in (("status", status), ("page", page if page > 1 else "")) if v}
    return "/eingang" + ("?" + urlencode(params) if params else "")


@router.get("/eingang", response_class=HTMLResponse)
def inbox_page(request: Request, status: str = "", page: str = "1"):
    s = _settings(request)
    status = status if status in INBOX_STATUSES else ""
    with db.session(s.db_path) as conn:
        counts = inbox.counts_by_status(conn)
        total = counts[status]
        pages = max(1, math.ceil(total / inbox.PAGE_SIZE))
        current = min(max(1, _int(page, 1)), pages)
        offset = (current - 1) * inbox.PAGE_SIZE
        mails = inbox.page_rows(conn, status=status, limit=inbox.PAGE_SIZE, offset=offset)
        projects = list_projects(conn)
    back = inbox_url(status, current)
    return render(request, "inbox.html", mails=mails, projects=projects, assign_mode=s.assign_mode,
                  counts=counts, status=status, page=current, pages=pages, total=total,
                  first=offset + 1 if mails else 0, last=offset + len(mails), back=back,
                  filter_urls={k: inbox_url(k) for k in INBOX_FILTERS},
                  prev_url=inbox_url(status, current - 1) if current > 1 else None,
                  next_url=inbox_url(status, current + 1) if current < pages else None)


def resolve_project_input(conn, text: str | None) -> str | None:
    """Project number from the free-text project field of the inbox (number, 'number name' as offered
    by the list, or a unique part of the name). Unknown text is passed on and rejected by decide()."""
    text = (text or "").strip()
    if not text:
        return None
    for candidate in (text, text.split()[0]):
        p = get_project(conn, candidate)
        if p:
            return p["number"]
    matches = list_projects(conn, text)
    if len(matches) == 1:
        return matches[0]["number"]
    if len(matches) > 1:
        raise DecisionError(f"Mehrere Projekte passen zu „{text}“ – bitte die Projektnummer aus der Liste wählen.")
    return text


def _decide(conn, mail_id: int, action: str, project: str, user: str | None, mode: str) -> dict:
    """Apply a decision; DecisionError becomes ``error``. LookupError (mail gone) propagates."""
    result, error = None, None
    try:
        target = resolve_project_input(conn, project) if action == "assign" else (project or None)
        result = decide(conn, mail_id, action, target, user=user, mode=mode)
    except DecisionError as exc:
        error = str(exc)
    name = None
    if result and result.get("project"):
        row = conn.execute("SELECT name FROM projects WHERE number = ?", (result["project"],)).fetchone()
        name = row[0] if row else None
    mail = inbox.mail_row(conn, mail_id) or {"id": mail_id, "subject": "", "assign_status": None}
    return {"action": action, "result": result, "error": error, "name": name, "mail": mail,
            "counts": inbox.counts_by_status(conn)}


def _card_context(outcome: dict, back: str) -> dict:
    """Template and context of the inbox card after a decision.

    Real decisions (assigned, no project) become a green "done" card. A rejected suggestion or a
    failed decision keeps the mail actionable in the inbox (neutral card with the buttons)."""
    mail, result, error = outcome["mail"], outcome["result"], outcome["error"]
    ctx = {"mail": mail, "m": mail, "result": result, "error": error, "name": outcome["name"], "back": back,
           "counts": outcome["counts"]}
    if result and result["assign_status"] in ("confirmed", "none", "assigned"):
        return {**ctx, "card": "done"}
    if mail and mail["assign_status"] in INBOX_STATUSES:
        if error:
            return {**ctx, "card": "item", "notice": error, "notice_kind": "warn"}
        return {**ctx, "card": "item", "notice_kind": "info",
                "notice": "Vorschlag verworfen – die Mail bleibt im Eingang. Bitte von Hand zuordnen oder "
                          "„Kein Projektbezug“ wählen."}
    return {**ctx, "card": "done"}


@router.post("/eingang/{mail_id}", response_class=HTMLResponse)
def inbox_decide(request: Request, mail_id: int, action: str = Form(...), project: str = Form(""),
                 back: str = Form("/eingang")):
    s = _settings(request)
    back = safe_back(back)
    user = getattr(request.state, "user", None)
    try:
        with db.session(s.db_path) as conn:
            outcome = _decide(conn, mail_id, action, project, user, s.assign_mode)
    except LookupError:
        raise HTTPException(404, "Mail nicht gefunden") from None
    except sqlite3.OperationalError as exc:
        if not inbox.is_db_busy(exc):
            raise
        log.warning("Entscheidung zu Mail %s nicht gespeichert – Datenbank belegt: %s", mail_id, exc)
        return _busy_response(request, mail_id, back)
    if is_htmx(request):
        return templates.TemplateResponse(request, "_inbox_update.html", _card_context(outcome, back))
    if outcome["error"]:
        return render(request, "error.html", status_code=400, title="Zuordnung nicht möglich",
                      message=outcome["error"], back=back, back_label="Zurück")
    return RedirectResponse(back, status_code=303)


def _busy_response(request: Request, mail_id: int, back: str) -> HTMLResponse:
    """The decision could not be written: friendly German message, the mail stays actionable."""
    if not is_htmx(request):
        return render(request, "error.html", status_code=503, headers={"Retry-After": str(BUSY_RETRY_AFTER)},
                      title="Bitte gleich noch einmal", message=BUSY_TEXT, back=back, back_label="Zurück")
    mail, counts = None, None
    try:  # reading still works while another connection writes (WAL)
        with db.session(_settings(request).db_path) as conn:
            mail, counts = inbox.mail_row(conn, mail_id), inbox.counts_by_status(conn)
    except sqlite3.OperationalError:
        pass
    ctx = {"mail": mail or {"id": mail_id, "subject": ""}, "m": mail, "result": None, "error": BUSY_TEXT,
           "name": None, "back": back, "counts": counts}
    if mail and mail["assign_status"] in INBOX_STATUSES:
        ctx.update(card="item", notice=BUSY_TEXT, notice_kind="warn")
    else:
        ctx.update(card="done")
    return templates.TemplateResponse(request, "_inbox_update.html", ctx)


# ------------------------------------------------------------------------------------- mails, documents
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


# ------------------------------------------------------------------------------------- status
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
