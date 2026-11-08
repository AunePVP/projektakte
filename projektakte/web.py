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
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import __version__, db, inbox
from .ask import ask as answer_question, cited_numbers
from .assign import INBOX_STATUSES, DecisionError, decide
from .registry import get_project, list_projects
from .search import hybrid_search
from .webtext import (breakable_path, highlight, matcher, parse_blocks, passage, render_html, same_heading,
                      snippet, trim_overlap)

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


def search_url(query: str, project: str | None = None, **extra) -> str:
    params = {"q": query}
    if project:
        params["project"] = project
    params.update({k: v for k, v in extra.items() if v})
    return "/suche?" + urlencode(params)


def file_url(doc_id: int, filename: str | None = None) -> str:
    """Address of an original file. It ends with the file name, so the browser's PDF viewer names
    the tab and the save dialog after the file instead of "datei"."""
    name = (filename or "").strip()
    return f"/dokumente/{doc_id}/datei/{quote(name, safe='')}" if name else f"/dokumente/{doc_id}/datei"


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

    The search link always shows the weak hits as well: a question rarely reaches the relevance
    threshold of the search page, and "In der Suche öffnen" must never lead to an empty page.
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
        "search_url": search_url(field("question") or "", field("project"), weak="1"),
        "search_prominent": status != "answered",
    }


# ------------------------------------------------------------------------------------- search
def literal_identifier_hit(h, query: str) -> bool:
    """Does a hit literally contain an identifier of the query (project number, file number, "T90")?
    Rerankers – the fake one on purpose, real cross-encoders in practice – score a bare identifier
    low, but such a hit is exactly what was searched for."""
    return matcher(query).has_identifier(getattr(h, "text", ""), getattr(h, "title", ""),
                                         getattr(h, "rel_path", ""), getattr(h, "label", ""))


def search_view(res, *, min_score: float, show_weak: bool, query: str = "") -> dict:
    """Split search hits into matching and weak ones. Only the reranker gives a comparable score;
    without it (fallback) every hit counts as matching. A hit that literally contains an identifier
    of the query is never weak."""
    if res.reranked:
        strong, weak = [], []
        for h in res.hits:
            ok = (h.rerank or 0.0) >= min_score or (query and literal_identifier_hit(h, query))
            (strong if ok else weak).append(h)
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
DOC_STATUS = {"ok": "", "skipped": "übersprungen", "error": "Fehler", "empty": "ohne Text",
              "pending": "Anhang ausstehend"}
INBOX_FILTERS = {"": "Alle", "suggested": "Mit Vorschlag", "unassigned": "Ohne Vorschlag"}


def auth_disabled_for(request: Request) -> bool:
    auth = getattr(request.app.state, "auth", None)
    return bool(getattr(auth, "disabled", False))


templates.env.filters.update(de_date=de_date, de_num=de_num, plural=plural, filesize=filesize,
                             cite_links=cite_links, highlight=highlight, snippet=snippet,
                             path=breakable_path)
templates.env.globals.update(ASSIGN_LABELS=ASSIGN_LABELS, METHOD_LABELS=METHOD_LABELS, DOC_STATUS=DOC_STATUS,
                             INBOX_FILTERS=INBOX_FILTERS, version=__version__, answer_view=answer_view,
                             auth_disabled_for=auth_disabled_for, passage=passage, file_url=file_url)


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
def ask_page(request: Request, project: str | None = None, q: str = ""):
    """Question form; ``q`` fills in a question typed elsewhere."""
    with db.session(_settings(request).db_path) as conn:
        projects = list_projects(conn)
    return render(request, "ask.html", projects=projects, project=project, result=None,
                  question=q.strip()[:MAX_QUERY])


@router.post("/fragen", response_class=HTMLResponse)
def ask_submit(request: Request, question: str = Form(""), project: str = Form(""), q: str = Form("")):
    """Answer a question. ``q`` is the field of the search box on the start page, whose button
    "Frage stellen" sends the typed text here."""
    s = _settings(request)
    question = (question.strip() or q.strip())[:MAX_QUERY]
    with db.session(s.db_path) as conn:
        p = get_project(conn, project) if project else None
        result = None
        if question:
            result = answer_question(conn, request.app.state.ai, str(s.db_path), question,
                                     project=p["number"] if p else None, top_k=s.ask_top_k,
                                     context_chars=s.ask_context_chars, min_rerank=s.ask_min_rerank,
                                     rerank_top_k=s.rerank_top_k, synonyms_file=s.synonyms_file,
                                     deadline_s=s.ask_deadline)
        projects = list_projects(conn)
    if is_htmx(request):
        return templates.TemplateResponse(request, "_answer.html", {"result": result, "question": question})
    return render(request, "ask.html", projects=projects, project=p["number"] if p else None,
                  result=result, question=question)


_IDENTIFIER_TOKEN = re.compile(r"[^\W_]+(?:[-/.][^\W_]+)*")
MAX_PROJECT_LINKS = 3


def projects_named_in(conn, query: str) -> list[dict]:
    """Projects whose number appears in a search query ('BS-2024-0233' → link to the project page)."""
    found: dict[str, dict] = {}
    for token in _IDENTIFIER_TOKEN.findall(query or ""):
        if any(c.isdigit() for c in token) and "-" in token:
            p = get_project(conn, token)
            if p:
                found.setdefault(p["number"], p)
        if len(found) >= MAX_PROJECT_LINKS:
            break
    return list(found.values())


@router.get("/suche", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", project: str = "", weak: str = ""):
    s = _settings(request)
    query = q.strip()[:MAX_QUERY]
    res = view = None
    named: list[dict] = []
    with db.session(s.db_path) as conn:
        p = get_project(conn, project) if project else None
        number = p["number"] if p else ""
        if query:
            res = hybrid_search(conn, request.app.state.ai, str(s.db_path), query, project=number or None,
                                k=SEARCH_HITS, rerank_top_k=s.rerank_top_k, synonyms_file=s.synonyms_file)
            named = projects_named_in(conn, query)
        projects = list_projects(conn)
    if res is not None:
        view = search_view(res, min_score=getattr(s, "search_min_rerank", 0.05), show_weak=weak == "1",
                           query=query)
    return render(request, "search.html", q=query, project=number, projects=projects, res=res, view=view,
                  named_projects=named, all_projects_url=search_url(query, None, weak="1" if weak == "1" else ""),
                  weak_url=search_url(query, number, weak="1"), strong_url=search_url(query, number))


# ------------------------------------------------------------------------------------- inbox
def inbox_url(status: str = "", page: int = 1, **cursor: str) -> str:
    """Address of an inbox page: filter plus the keyset cursor (``ab``: page starts at this mail,
    ``vor``: the page before this mail, ``ende``: last page); ``page`` is the old offset form."""
    params = {"status": status, "page": page if page > 1 else ""}
    params.update(cursor)
    params = {k: v for k, v in params.items() if v}
    return "/eingang" + ("?" + urlencode(params) if params else "")


def _inbox_page_rows(conn, status: str, total: int, *, ab: str, vor: str, ende: str,
                     page: str) -> list[dict]:
    """One page read by key, plus the first mail of the next page if there is one (to link it)."""
    size = inbox.PAGE_SIZE
    start, before = inbox.decode_key(ab), inbox.decode_key(vor)
    if before is not None:
        rows = inbox.page_rows(conn, status=status, limit=size, before=before)
        if len(rows) == size:  # fewer: the page before reaches the beginning – show the first page
            following = inbox.page_rows(conn, status=status, limit=2, start=inbox.sort_key(rows[-1]))
            return rows + following[1:]
    elif start is not None:
        rows = inbox.page_rows(conn, status=status, limit=size + 1, start=start)
        if rows or not total:
            return rows
        ende = "1"  # the cursor lies behind the last open mail (decided meanwhile)
    if ende == "1":
        return inbox.page_rows(conn, status=status, limit=size, last=True)
    if page:  # old links with a page number
        pages = max(1, math.ceil(total / size))
        offset = (min(max(1, _int(page, 1)), pages) - 1) * size
        return inbox.page_rows(conn, status=status, limit=size + 1, offset=offset)
    return inbox.page_rows(conn, status=status, limit=size + 1)


def inbox_range(first: int, last: int, total: int, pager: bool = False) -> dict:
    """What the summary line and the pager say about the shown part of the inbox: positions
    ``first``–``last`` of ``total`` open mails (``done``: nothing of the page is left open). A page
    with a pager keeps the "first–last of total" form after decisions even when the total falls to
    one page – "29 Mails" would claim that all of them are on this page."""
    return {"first": first, "last": last, "total": total, "done": last < first,
            "paged": total > inbox.PAGE_SIZE or first > 1 or pager, "pager": pager}


@router.get("/eingang", response_class=HTMLResponse)
def inbox_page(request: Request, status: str = "", ab: str = "", vor: str = "", ende: str = "",
               page: str = ""):
    s = _settings(request)
    status = status if status in INBOX_STATUSES else ""
    size = inbox.PAGE_SIZE
    with db.session(s.db_path) as conn:
        counts = inbox.counts_by_status(conn)
        total = counts[status]
        rows = _inbox_page_rows(conn, status, total, ab=ab, vor=vor, ende=ende, page=page)
        mails, following = rows[:size], rows[size:size + 1]
        first = inbox.count_before(conn, inbox.sort_key(mails[0]), status=status) + 1 if mails else 0
        projects = list_projects(conn)
    first_key = inbox.sort_key(mails[0]) if mails else None
    last_key = inbox.sort_key(mails[-1]) if mails else None
    last = first + len(mails) - 1 if mails else 0
    next_key = inbox.sort_key(following[0]) if following else None
    back = inbox_url(status, ab=inbox.encode_key(first_key)) if mails and first > 1 else inbox_url(status)
    shown = inbox_range(first, last, total)
    page_state = None
    if mails:
        page_state = {"seite_status": status, "seite_von": inbox.encode_key(first_key),
                      "seite_bis": inbox.encode_key(last_key), "seite_pager": "1" if shown["paged"] else "",
                      "seite_ids": inbox.encode_ids(mails)}
    return render(request, "inbox.html", mails=mails, projects=projects, assign_mode=s.assign_mode,
                  counts=counts, status=status, total=total, back=back, shown=shown, page_state=page_state,
                  filter_urls={k: inbox_url(k) for k in INBOX_FILTERS},
                  start_url=inbox_url(status) if first > 1 else None,
                  prev_url=inbox_url(status, vor=inbox.encode_key(first_key)) if first > 1 else None,
                  next_url=inbox_url(status, ab=inbox.encode_key(next_key)) if next_key else None,
                  end_url=inbox_url(status, ende="1") if next_key else None)


def _page_summary(conn, state: dict) -> dict | None:
    """The summary line of the inbox page a decision came from (sent with the HTMX request as
    ``seite_*``): position of the mails still open on that page and the total of its filter.

    "Still open" are the cards of the page (``seite_ids``) that can still be decided. A rejected
    suggestion is one of them although its sort key changed (it moves behind the suggestions, and
    in the filter "Mit Vorschlag" it no longer belongs to the filter – it is counted on top of the
    filter's total then, so the line never claims fewer open mails than the page shows). Pages
    rendered without ids fall back to the key range of the page."""
    first_key, last_key = inbox.decode_key(state.get("von")), inbox.decode_key(state.get("bis"))
    status = state.get("status") or ""
    if first_key is None or last_key is None or status not in ("", *INBOX_STATUSES):
        return None
    total = inbox.counts_by_status(conn)[status]
    before = inbox.count_before(conn, first_key, status=status)
    ids = inbox.decode_ids(state.get("ids"))
    if ids:
        left, in_filter = inbox.count_ids(conn, ids, status=status)
        total += left - in_filter
    else:
        left = inbox.count_between(conn, first_key, last_key, status=status)
    return inbox_range(before + 1, before + left, total, pager=state.get("pager") == "1")


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


def _decide(conn, mail_id: int, action: str, project: str, user: str | None, mode: str,
            page_state: dict | None = None) -> dict:
    """Apply a decision; DecisionError becomes ``error``. LookupError (mail gone) propagates.
    ``page_state`` (cursor of the page the decision came from) yields its new summary line."""
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
            "counts": inbox.counts_by_status(conn), "typed": project if action == "assign" else "",
            "summary": _page_summary(conn, page_state) if page_state else None}


def _card_context(outcome: dict, back: str) -> dict:
    """Template and context of the inbox card after a decision.

    Real decisions (assigned, no project) become a green "done" card. A rejected suggestion or a
    failed decision keeps the mail actionable in the inbox (neutral card with the buttons)."""
    mail, result, error = outcome["mail"], outcome["result"], outcome["error"]
    ctx = {"mail": mail, "m": mail, "result": result, "error": error, "name": outcome["name"], "back": back,
           "counts": outcome["counts"], "summary": outcome.get("summary")}
    if result and result["assign_status"] in ("confirmed", "none", "assigned"):
        return {**ctx, "card": "done"}
    if mail and mail["assign_status"] in INBOX_STATUSES:
        if error:  # the typed project stays in the field, so a typo can be corrected
            return {**ctx, "card": "item", "notice": error, "notice_kind": "warn", "typed": outcome.get("typed")}
        return {**ctx, "card": "item", "notice_kind": "info",
                "notice": "Vorschlag verworfen – die Mail bleibt im Eingang. Bitte von Hand zuordnen oder "
                          "„Kein Projektbezug“ wählen."}
    return {**ctx, "card": "done"}


@router.post("/eingang/{mail_id}", response_class=HTMLResponse)
def inbox_decide(request: Request, mail_id: int, action: str = Form(...), project: str = Form(""),
                 back: str = Form("/eingang"), seite_status: str = Form(""), seite_von: str = Form(""),
                 seite_bis: str = Form(""), seite_pager: str = Form(""), seite_ids: str = Form("")):
    """A decision from an inbox card or the mail page. HTMX requests carry the cursor of their page
    (``seite_*``, set on the card list) so the summary line of that page is updated as well."""
    s = _settings(request)
    back = safe_back(back)
    user = getattr(request.state, "user", None)
    page_state = ({"status": seite_status, "von": seite_von, "bis": seite_bis, "pager": seite_pager,
                   "ids": seite_ids} if is_htmx(request) and seite_von else None)
    try:
        with db.session(s.db_path) as conn:
            outcome = _decide(conn, mail_id, action, project, user, s.assign_mode, page_state)
    except LookupError:
        raise HTTPException(404, "Mail nicht gefunden") from None
    except sqlite3.OperationalError as exc:
        if not inbox.is_db_busy(exc):
            raise
        log.warning("Entscheidung zu Mail %s nicht gespeichert – Datenbank belegt: %s", mail_id, exc)
        return _busy_response(request, mail_id, back, project if action == "assign" else "")
    if is_htmx(request):
        return templates.TemplateResponse(request, "_inbox_update.html", _card_context(outcome, back))
    if outcome["error"]:
        return render(request, "error.html", status_code=400, title="Zuordnung nicht möglich",
                      message=outcome["error"], back=back, back_label="Zurück")
    return RedirectResponse(back, status_code=303)


def _busy_response(request: Request, mail_id: int, back: str, typed: str = "") -> HTMLResponse:
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
        ctx.update(card="item", notice=BUSY_TEXT, notice_kind="warn", typed=typed)
    else:
        ctx.update(card="done")
    return templates.TemplateResponse(request, "_inbox_update.html", ctx)


# ------------------------------------------------------------------------------------- mails, documents
@router.get("/mails/{mail_id}", response_class=HTMLResponse)
def mail_page(request: Request, mail_id: int):
    s = _settings(request)
    with db.session(s.db_path) as conn:
        m = conn.execute("SELECT m.*, p.name AS project_name FROM mails m LEFT JOIN projects p ON "
                         "p.number = m.project WHERE m.id = ? AND m.deleted = 0", (mail_id,)).fetchone()
        if not m:
            raise HTTPException(404, "Mail nicht gefunden")
        atts = [dict(r) for r in conn.execute(
            "SELECT id, filename, ext, size, status, message, path, cache_path, mail_id FROM documents "
            "WHERE mail_id = ? ORDER BY id", (mail_id,))]
        projects = list_projects(conn)
    for a in atts:  # "Datei öffnen" only for a file that is there (not for "Anhang ausstehend")
        a["available"] = original_file(s, a) is not None
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
    s = _settings(request)
    with db.session(s.db_path) as conn:
        d = conn.execute("SELECT d.*, p.name AS project_name FROM documents d LEFT JOIN projects p ON "
                         "p.number = d.project WHERE d.id = ?", (doc_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Dokument nicht gefunden")
        rows = [dict(r) for r in conn.execute(
            "SELECT id, ord, page, heading, text FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,))]
        mail = None
        if d["mail_id"]:
            mail = conn.execute("SELECT id, subject, date FROM mails WHERE id = ?", (d["mail_id"],)).fetchone()
    d = dict(d)
    d["available"] = original_file(s, d) is not None
    chunks = [chunk_view(c) for c in without_overlap(rows)]
    return render(request, "document.html", d=d, chunks=chunks, mail=dict(mail) if mail else None)


def without_overlap(chunks: list[dict]) -> list[dict]:
    """The sections of a document as one continuous text: chunking repeats the last paragraph of a
    section at the beginning of the next one (same page and heading); the page shows it only once.

    The repeated lines are left out at the end of the earlier section, not at the beginning of the
    later one, so every section (anchor ``#c<id>`` of search hits and answer sources) still starts
    with its own complete text and a passage found in the overlap is right below the anchor."""
    out = [dict(c) for c in chunks]
    for cur, nxt in zip(out, out[1:]):
        if cur.get("page") == nxt.get("page") and (cur.get("heading") or "") == (nxt.get("heading") or ""):
            cur["text"] = trim_overlap(cur.get("text") or "", nxt.get("text") or "")
    return out


def chunk_view(chunk: dict) -> dict:
    """A chunk as the document page shows it: the text without markdown syntax (headings as
    headings, tables as tables) and a context line with the page and – unless the text starts with
    it – the heading of the section it belongs to."""
    blocks = parse_blocks(chunk.get("text") or "")
    starts_with_heading = bool(blocks) and blocks[0].kind == "heading" and (
        not chunk.get("heading") or same_heading(blocks[0].text, chunk["heading"]))
    context = [f"Seite {chunk['page']}"] if chunk.get("page") else []
    if chunk.get("heading") and not starts_with_heading:
        context.append(chunk["heading"])
    return {**chunk, "context": " · ".join(context), "html": render_html(blocks)}


def original_file(s, d) -> Path | None:
    """The original file of a document row (``path``, ``cache_path``, ``mail_id``, ``status``) if it
    can be opened: known to the index, below the configured roots and present. A mail attachment
    that the source has not delivered yet ("Anhang ausstehend") has no file."""
    if d["mail_id"]:
        if d["status"] == "pending" or not d["cache_path"]:
            return None
        raw = d["cache_path"]
    else:
        raw = d["path"]
    if not raw:
        return None
    path = Path(raw).resolve()
    roots = [s.projects_dir.resolve(), s.cache_dir.resolve()]
    if not any(path.is_relative_to(r) for r in roots) or not path.is_file():
        return None
    return path


@router.get("/dokumente/{doc_id}/datei")
@router.get("/dokumente/{doc_id}/datei/{filename:path}")
def document_file(request: Request, doc_id: int, filename: str = ""):
    """Original file (read-only). Only paths known to the index and below the configured roots.
    The file name at the end of the address only names the browser tab; ``doc_id`` decides."""
    s = _settings(request)
    with db.session(s.db_path) as conn:
        d = conn.execute("SELECT path, cache_path, filename, mail_id, status FROM documents WHERE id = ?",
                         (doc_id,)).fetchone()
    if not d:
        raise HTTPException(404, "Dokument nicht gefunden")
    path = original_file(s, d)
    if path is None:
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
