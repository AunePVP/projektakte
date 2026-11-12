"""Server-rendered web interface (Jinja2 + HTMX, German). Every form also works without JavaScript.

The templates contain no inline scripts, style blocks or style attributes (Content Security Policy
``default-src 'self'``); all styles live in static/app.css. Login and logout are in auth_web.py.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import math
import re
import secrets
import sqlite3
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup, escape

from . import (__version__, betrachter, db, dokumentarten, embed_worker, inbox, reihenfolge, structure, tippfehler,
               vision, zeiten, zeitleiste)
from .ask import ask as answer_question, cited_numbers
from .assign import INBOX_STATUSES, DecisionError, decide
from .config import DEFAULT_PROJECT_NUMBER_PATTERN
from .db import BAUVORHABEN
from .extract import extraction_profile
from .registry import as_tree, children, get_project, list_projects, number_regex, project_folders
from .search import TYPES_TOP, hybrid_search
from .webtext import (SNIPPET_CHARS, breakable_path, highlight, join_spaced_codes, matcher, parse_blocks, passage,
                      render_html, same_heading, snippet, trim_overlap)

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


def viewer_url(doc_id: int, page: int | None = None, chunk: int | None = None) -> str:
    """Address of the viewer panel of a document (loaded by static/betrachter.js; the link itself keeps
    the document page as its href, so it works without JavaScript). ``page``: the PDF opens there."""
    params = {}
    if page:
        params["seite"] = int(page)
    if chunk:
        params["c"] = int(chunk)
    return f"/dokumente/{int(doc_id)}/ansicht" + (f"?{urlencode(params)}" if params else "")


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
def literal_identifier_hit(h, query: str, code_pattern: str | None = None) -> bool:
    """Does a hit literally contain an identifier of the query (project number, file number, "T90")?
    Rerankers – the fake one on purpose, real cross-encoders in practice – score a bare identifier
    low, but such a hit is exactly what was searched for. ``code_pattern``: PROJECT_NUMBER_PATTERN
    (a project number typed with spaces counts as written)."""
    return matcher(query, code_pattern).has_identifier(getattr(h, "text", ""), getattr(h, "title", ""),
                                                       getattr(h, "rel_path", ""), getattr(h, "label", ""))


def search_view(res, *, min_score: float, show_weak: bool, query: str = "", code_pattern: str | None = None) -> dict:
    """Split search hits into matching and weak ones. Only the reranker gives a comparable score;
    without it (fallback) every hit counts as matching. A hit that literally contains an identifier
    of the query is never weak."""
    if res.reranked:
        strong, weak = [], []
        for h in res.hits:
            ok = ((h.rerank or 0.0) >= min_score or bool(getattr(h, "type_match", None))
                  or (query and literal_identifier_hit(h, query, code_pattern)))
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


def code_pattern_of(request: Request | None) -> str | None:
    """The effective shape of project numbers (PROJECT_NUMBER_PATTERN, with "auto" the stored detection;
    structure.NO_MATCH while none is known): what a number typed with spaces ("BS 2024 0233") is joined
    into for search links and highlighting. Looked up once per request."""
    settings = getattr(getattr(getattr(request, "app", None), "state", None), "settings", None)
    if settings is None:
        return None
    state = getattr(request, "state", None)
    cached = getattr(state, "project_number_shape", None) if state is not None else None
    if cached is not None:
        return cached
    pattern = structure.number_pattern(settings)
    if state is not None:
        try:
            state.project_number_shape = pattern
        except AttributeError:
            pass
    return pattern


@pass_context
def _highlight_filter(ctx, text: str, query: str) -> Markup:
    return highlight(text, query, code_pattern_of(ctx.get("request")))


@pass_context
def _snippet_filter(ctx, text: str, query: str = "", width: int = SNIPPET_CHARS) -> str:
    return snippet(text, query, width, code_pattern_of(ctx.get("request")))


@pass_context
def _passage_global(ctx, snippet_text: str | None, full_text: str | None, query: str = "",
                    width: int = SNIPPET_CHARS) -> str:
    return passage(snippet_text, full_text, query, width, code_pattern_of(ctx.get("request")))


templates.env.filters.update(dauer=zeiten.dauer, etwa=zeiten.etwa, je=zeiten.je)
templates.env.filters.update(de_date=de_date, de_num=de_num, plural=plural, filesize=filesize,
                             cite_links=cite_links, highlight=_highlight_filter, snippet=_snippet_filter,
                             path=breakable_path)
templates.env.globals.update(ASSIGN_LABELS=ASSIGN_LABELS, METHOD_LABELS=METHOD_LABELS, DOC_STATUS=DOC_STATUS,
                             INBOX_FILTERS=INBOX_FILTERS, version=__version__, answer_view=answer_view,
                             auth_disabled_for=auth_disabled_for, passage=_passage_global, file_url=file_url,
                             viewer_url=viewer_url)


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


def _activity(conn, today: date | None = None) -> dict[str, tuple[int, int, date | None]]:
    """Per project number: (documents, mails, last activity – the local day of the newest document or
    mail that is not in the future). A document's date often comes from its text (a deadline, a planned
    opening "29.06.2030"): dates after ``today`` (default: the local day now) never count as activity."""
    today = today or datetime.now(LOCAL_TZ).date()
    day_end = datetime.combine(today, datetime.max.time(), LOCAL_TZ).astimezone(timezone.utc)
    docs = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT project, COUNT(*), MAX(CASE WHEN substr(doc_date, 1, 10) <= ? THEN doc_date END) FROM documents "
        "WHERE mail_id IS NULL AND project IS NOT NULL GROUP BY project", (today.isoformat(),))}
    # mail dates are stored uniformly as UTC ISO strings ("…T…+00:00"), so text order is time order
    mails = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT project, COUNT(*), MAX(CASE WHEN date <= ? THEN date END) FROM mails WHERE deleted = 0 "
        "AND project IS NOT NULL GROUP BY project", (day_end.strftime("%Y-%m-%dT%H:%M:%S+00:00"),))}
    out: dict[str, tuple[int, int, date | None]] = {}
    for number in docs.keys() | mails.keys():
        n_docs, last_doc = docs.get(number, (0, None))
        n_mails, last_mail = mails.get(number, (0, None))
        days = [d for d in (local_day(last_doc), local_day(last_mail)) if d and d <= today]
        out[number] = (n_docs, n_mails, max(days) if days else None)
    return out


def _with_activity(rows: list[dict], activity: dict) -> list[dict]:
    """Counts and last activity on each row; a site (Bauvorhaben) shows them with all its orders."""
    for p in rows:
        n_docs, n_mails, last = activity.get(p["number"], (0, 0, None))
        p["own_docs"], p["own_mails"] = n_docs, n_mails
        if p.get("kind") == BAUVORHABEN:
            # all its orders, also those a filter hides: the site's figures are the whole site's
            for c in p.get("subprojects", p.get("children")) or ():
                c_docs, c_mails, c_last = activity.get(c["number"], (0, 0, None))
                n_docs, n_mails = n_docs + c_docs, n_mails + c_mails
                last = max(d for d in (last, c_last) if d) if (last or c_last) else None
        p["docs"], p["mails"] = n_docs, n_mails
        p["last"] = last.isoformat() if last else None
    return rows


def _project_rows(conn, q: str, today: date | None = None) -> list[dict]:
    """Projects with counts and the last activity, as a tree: each site (Bauvorhaben) with its orders
    (Unterprojekte) indented beneath it – the site's counts cover them all. A filter that matches an
    order shows its site above it, one that matches a site all its orders."""
    rows = as_tree(list_projects(conn, q), list_projects(conn) if q.strip() else None)
    return _with_activity(rows, _activity(conn, today))


# ------------------------------------------------------------------------------------- pages
# -- project overview: three views, chosen per person on /einstellungen --------------------------------
ANSICHTEN = {"liste": "Liste (alle Projekte untereinander)",
             "kacheln": "Kacheln (Ordner 1–9, dann 20–29, dann die Projekte)",
             "spalten": "Spalten (Ordner, Unterordner und Projekte nebeneinander)"}
DEFAULT_ANSICHT = "liste"
PAGE_ROWS = 100  # projects shown at once in the folder views
# A project is a row that is not an order of a site: sites and projects without a site (orders are part of
# their site, they are never counted as projects of their own).
_TOP_LEVEL = "active = 1 AND (kind = 'bauvorhaben' OR parent IS NULL OR parent = '')"


def _pref_key(user: str | None) -> str:
    return f"ansicht:{user or ''}"


def user_ansicht(conn, user: str | None) -> str:
    value = db.get_meta(conn, _pref_key(user), DEFAULT_ANSICHT)
    return value if value in ANSICHTEN else DEFAULT_ANSICHT


def _prefix(value: str, length: int) -> str:
    """A folder prefix from the address: ``length`` characters, no wildcard characters."""
    value = re.sub(r"[^0-9A-Za-z]", "", value or "")
    return value[:length] if len(value) >= length else ""


def folder_counts(conn, length: int, under: str = "") -> list[dict]:
    """Projects (not orders) per folder of ``length`` characters of the number, below ``under``. One grouped
    query – no project is loaded."""
    rows = conn.execute(f"SELECT substr(number, 1, ?) AS k, COUNT(*) AS n FROM projects WHERE {_TOP_LEVEL} "
                        "AND number LIKE ? GROUP BY k ORDER BY k", (length, under + "%")).fetchall()
    return [{"key": r["k"], "count": r["n"]} for r in rows]


def folder_projects(conn, prefix: str, limit: int, today: date | None = None) -> tuple[list[dict], int]:
    """The projects of one folder with their orders (as a tree, like the list), at most ``limit`` projects;
    plus the number of all projects in the folder."""
    total = conn.execute(f"SELECT COUNT(*) FROM projects WHERE {_TOP_LEVEL} AND number LIKE ?", (prefix + "%",)).fetchone()[0]
    sites = [r[0] for r in conn.execute(f"SELECT number FROM projects WHERE {_TOP_LEVEL} AND number LIKE ? "
                                        "ORDER BY number DESC LIMIT ?", (prefix + "%", limit))]
    if not sites:
        return [], total
    marks = ",".join("?" * len(sites))
    rows = [dict(r) for r in conn.execute(
        "SELECT number, name, path, active, parent, kind FROM projects WHERE active = 1 AND "
        f"(number IN ({marks}) OR parent IN ({marks}))", sites + sites)]
    rows.sort(key=lambda r: r["number"], reverse=True)
    return _with_activity(as_tree(rows), _activity(conn, today)), total


def _overview(request: Request, conn, ansicht: str, o: str, p: str, sel: str) -> dict:
    """What the folder views show: level 1 (folders 1–9), level 2 (20–29) or the projects of a folder."""
    o1 = _prefix(o, 1)
    o2 = _prefix(p or o, 2)
    if o2 and o1 and not o2.startswith(o1):
        o2 = ""
    ctx: dict = {"ansicht": ansicht, "o1": o1, "o2": o2, "sel": sel}
    ctx["level1"] = folder_counts(conn, 1)
    if o1:
        ctx["level2"] = folder_counts(conn, 2, o1)
    if o2:
        ctx["projects"], ctx["total"] = folder_projects(conn, o2, PAGE_ROWS if not request.query_params.get("alle") else 10**6)
        ctx["more"] = ctx["total"] - sum(1 for x in ctx["projects"] if x["depth"] == 0)
    return ctx


@router.get("/", response_class=HTMLResponse)
def projects_page(request: Request, q: str = "", o: str = "", p: str = "", sel: str = ""):
    user = getattr(request.state, "user", None)
    with db.session(_settings(request).db_path) as conn:
        ansicht = user_ansicht(conn, user)
        if ansicht != "liste" and not q.strip():
            ctx = _overview(request, conn, ansicht, o, p, sel)
            return render(request, "projects_ordner.html", q=q, **ctx)
        projects = _project_rows(conn, q)
    if is_htmx(request):
        return templates.TemplateResponse(request, "_project_rows.html", {"projects": projects, "q": q})
    return render(request, "projects.html", projects=projects, q=q, ansicht=ansicht)


@router.get("/einstellungen", response_class=HTMLResponse)
def settings_page(request: Request, gespeichert: str = ""):
    with db.session(_settings(request).db_path) as conn:
        current = user_ansicht(conn, getattr(request.state, "user", None))
    return render(request, "einstellungen.html", ansichten=ANSICHTEN, current=current, saved=bool(gespeichert))


@router.post("/einstellungen")
def settings_save(request: Request, ansicht: str = Form("")):
    if ansicht not in ANSICHTEN:
        raise HTTPException(status_code=400, detail="Unbekannte Ansicht.")
    with db.session(_settings(request).db_path) as conn:
        db.set_meta(conn, _pref_key(getattr(request.state, "user", None)), ansicht)
    return RedirectResponse("/einstellungen?gespeichert=1", status_code=303)


def _project_attachments(conn, number: str) -> list[dict]:
    """Attachments of the mails assigned to a project (newest mail first)."""
    return [dict(r) for r in conn.execute(
        "SELECT d.id, d.filename, d.ext, d.size, d.status, d.message, d.method, m.id AS mail_id, "
        "m.subject, m.date FROM documents d JOIN mails m ON m.id = d.mail_id "
        "WHERE m.project = ? AND m.deleted = 0 ORDER BY m.date DESC, d.id", (number,))]


def _project_mails(conn, number: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, subject, sender, sender_name, date, assign_status, assign_method, has_attachments "
        "FROM mails WHERE project = ? AND deleted = 0 ORDER BY date DESC", (number,))]


TABS = (("dokumente", "Dokumente"), ("anhaenge", "Anhänge aus Mails"), ("mails", "Mails"))
ARTS = ("alle", "doc", "mail")
LIST_LIMIT = 200  # rows of the mail and attachment tabs before "alle zeigen"
GROUP_LIMIT = 60  # entries of a chosen stretch of the timeline
_SEL_ENTRY = re.compile(r"[dm]\d{1,12}")
_SEL_GROUP = re.compile(r"z(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})")


def tab_url(number: str, tab: str = "dokumente", folder: str = "", partial: bool = False,
            everything: bool = False) -> str:
    """The project page at a tab (of documents: at a folder) – or, with ``partial``, just that content (HTMX)."""
    base = f"/projekte/{quote(number, safe='')}"
    query = {}
    if tab != "dokumente":
        query["tab"] = tab
    if folder:
        query["ordner"] = folder
    if everything:
        query["alle"] = "1"
    qs = f"?{urlencode(query)}" if query else ""
    return f"{base}/inhalt{qs}" if partial else f"{base}{qs}#inhalt"


def timeline_url(number: str, art: str = "alle", selected: str = "", partial: bool = False,
                 only_svg: bool = False) -> str:
    """The project page with the timeline filtered by ``art`` and ``selected`` marked – or, with ``partial``,
    just the timeline (``only_svg``: its drawing only, so a scrolled timeline stays where it is) (HTMX)."""
    base = f"/projekte/{quote(number, safe='')}"
    query = {}
    if art in ARTS and art != "alle":
        query["art"] = art
    if selected:
        query["auswahl"] = selected
    if partial and only_svg:
        query["nur"] = "svg"
    qs = f"?{urlencode(query)}" if query else ""
    return f"{base}/zeitleiste{qs}" if partial else f"{base}{qs}#zeitleiste"


templates.env.globals.update(tab_url=tab_url, timeline_url=timeline_url)


def timeline_entries(docs: list[dict], mails: list[dict], attachments: list[dict]) -> list[dict]:
    """Documents and mails of a project as timeline entries, newest first; a mail carries its attachments."""
    atts_by_mail: dict[int, list[dict]] = {}
    for a in attachments:
        atts_by_mail.setdefault(a["mail_id"], []).append(a)
    entries = []
    for d in docs:
        rel = (d["rel_path"] or "").replace("\\", "/")
        entries.append({"key": f"d{d['id']}", "kind": "doc", "id": d["id"], "title": d["filename"] or rel,
                        "full": rel, "date": d["doc_date"], "day": local_day(d["doc_date"]),
                        "sort": local_sort_key(d["doc_date"]), "sub": (d["ext"] or "")[1:].upper(),
                        "folder": rel.rsplit("/", 1)[0] if "/" in rel else "", "status": d["status"],
                        "message": d["message"], "method": d["method"], "atts": []})
    for m in mails:
        entries.append({"key": f"m{m['id']}", "kind": "mail", "id": m["id"], "title": m["subject"] or "(ohne Betreff)",
                        "full": m["subject"] or "(ohne Betreff)", "date": m["date"], "day": local_day(m["date"]),
                        "sort": local_sort_key(m["date"]), "sub": m["sender_name"] or m["sender"],
                        "atts": atts_by_mail.get(m["id"], [])})
    return sorted(entries, key=lambda e: e["sort"], reverse=True)


def timeline_selection(entries: list[dict], selected: str, art: str) -> dict | None:
    """What a ``auswahl`` value names: one entry ("d12", "m5"), a stretch of the timeline
    ("z2024-01-01_2024-03-31", the entries of those days) or the undated entries ("zohne") – or None."""
    if _SEL_ENTRY.fullmatch(selected or ""):
        found = next((e for e in entries if e["key"] == selected), None)
        return {"type": "entry", "key": selected, "entry": found} if found else None
    fits = [e for e in entries if art == "alle" or e["kind"] == art]
    if selected == "zohne":
        members = [e for e in fits if e["day"] is None]
        title = "Einträge ohne Datum"
    elif m := _SEL_GROUP.fullmatch(selected or ""):
        try:
            first, last = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        except ValueError:
            return None
        members = [e for e in fits if e["day"] is not None and first <= e["day"] <= last]
        title = f"{de_date(first.isoformat())}" + ("" if first == last else f" – {de_date(last.isoformat())}")
    else:
        return None
    return {"type": "group", "key": selected, "title": title, "total": len(members),
            "members": members[:GROUP_LIMIT]}


def _load_project(conn, number: str) -> tuple[dict, list[dict], list[dict], list[dict]]:
    p = get_project(conn, number)
    if not p:
        raise HTTPException(404, f"Projekt {number} nicht gefunden")
    return p, _project_docs(conn, p["number"]), _project_mails(conn, p["number"]), _project_attachments(conn, p["number"])


def _timeline_context(entries: list[dict], art: str, auswahl: str, today: date | None = None) -> dict:
    art = art if art in ARTS else "alle"
    selection = timeline_selection(entries, auswahl, art)
    selected = selection["key"] if selection else ""
    return {"art": art, "tl": zeitleiste.build(entries, today or datetime.now(LOCAL_TZ).date(), art, selected),
            "sel": selection, "selected": selected,
            "counts": {"alle": len(entries), "doc": sum(e["kind"] == "doc" for e in entries),
                       "mail": sum(e["kind"] == "mail" for e in entries)}}


def _content_context(p: dict, docs: list[dict], mails: list[dict], attachments: list[dict], tab: str, folder: str,
                     everything: bool) -> dict:
    """What the tabs below the question show (documents as a file browser, attachments, mails)."""
    tab = tab if tab in dict(TABS) else "dokumente"
    if folder:
        tab = "dokumente"
    shown_mails = mails if everything else mails[:LIST_LIMIT]
    shown_atts = attachments if everything else attachments[:LIST_LIMIT]
    return {"tab": tab, "tabs": [(k, label, n) for (k, label), n in zip(TABS, (len(docs), len(attachments), len(mails)))],
            "browser": folder_view(docs, folder) if tab == "dokumente" else None,
            "mails": shown_mails, "attachments": shown_atts, "everything": everything,
            "mails_cut": len(mails) - len(shown_mails), "atts_cut": len(attachments) - len(shown_atts)}


@router.get("/projekte/{number}/zeitleiste", response_class=HTMLResponse)
def project_timeline(request: Request, number: str, art: str = "alle", auswahl: str = "", nur: str = ""):
    """The timeline of a project (and the card of what is chosen in it): the HTMX partial, else the page."""
    if not is_htmx(request):
        return RedirectResponse(timeline_url(number, art, auswahl), status_code=303)
    s = _settings(request)
    with db.session(s.db_path) as conn:
        p, docs, mails, attachments = _load_project(conn, number)
    ctx = _timeline_context(timeline_entries(docs, mails, attachments), art, auswahl)
    return render(request, "_zeitleiste_antwort.html", p=p, only_svg=nur == "svg", **ctx)


@router.get("/projekte/{number}/inhalt", response_class=HTMLResponse)
def project_content(request: Request, number: str, tab: str = "dokumente", ordner: str = "", alle: str = ""):
    """The tabs of a project page (documents, attachments, mails): the HTMX partial, else the page."""
    folder = clean_folder(ordner)
    if not is_htmx(request):
        return RedirectResponse(tab_url(number, tab, folder, everything=bool(alle)), status_code=303)
    s = _settings(request)
    with db.session(s.db_path) as conn:
        p, docs, mails, attachments = _load_project(conn, number)
    return render(request, "_inhalt.html", p=p, is_site=p.get("kind") == BAUVORHABEN,
                  **_content_context(p, docs, mails, attachments, tab, folder, bool(alle)))


@router.get("/projekte/{number}", response_class=HTMLResponse)
def project_page(request: Request, number: str, art: str = "alle", auswahl: str = "", tab: str = "dokumente",
                 ordner: str = "", alle: str = ""):
    s = _settings(request)
    with db.session(s.db_path) as conn:
        p, docs, mails, attachments = _load_project(conn, number)
        # a site (Bauvorhaben): its own files (outside every order) and its orders (Unterprojekte);
        # an order: a link to its site
        site = get_project(conn, p["parent"]) if p.get("parent") else None
        subprojects = (_with_activity(children(conn, p["number"]), _activity(conn))
                       if p.get("kind") == BAUVORHABEN else [])
        suggested = conn.execute("SELECT COUNT(*) FROM mails WHERE suggested_project = ? AND deleted = 0 "
                                 "AND assign_status = 'suggested'", (p["number"],)).fetchone()[0]
        folders = project_folders(conn, p["number"])
    entries = timeline_entries(docs, mails, attachments)
    # all folders of the project (several folders with its number are read as one project), primary first
    p["folders"] = [_below_projects_dir(f, s.projects_dir) for f in folders or [p["path"]]]
    p["rel"] = p["folders"][0]
    return render(request, "project.html", p=p, docs=docs, mails_all=mails, attachments_all=attachments,
                  suggested=suggested, site=site, subprojects=subprojects, wide=True,
                  is_site=p.get("kind") == BAUVORHABEN, only_svg=False,
                  **_timeline_context(entries, art, auswahl),
                  **_content_context(p, docs, mails, attachments, tab, clean_folder(ordner), bool(alle)))


def clean_folder(folder: str | None) -> str:
    """A folder inside a project as given in a URL: '/'-separated, without empty, '.' and '..' parts."""
    parts = [p for p in (folder or "").replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def folder_url(number: str, folder: str = "", partial: bool = False) -> str:
    """The project page opened at ``folder`` – or, with ``partial``, just its file browser (HTMX)."""
    base = f"/projekte/{quote(number, safe='')}"
    if partial:
        return f"{base}/ordner" + (f"?{urlencode({'pfad': folder})}" if folder else "")
    return base + (f"?{urlencode({'ordner': folder})}" if folder else "") + "#inhalt"


templates.env.globals["folder_url"] = folder_url


def folder_view(docs: list[dict], folder: str) -> dict:
    """One level of the project's documents as a file browser: the folders and files directly in
    ``folder`` (a path inside the project, '' = top level), folders first, both by name. A folder
    counts every document below it and the ones that were not read completely. The tree is built from
    the indexed documents (their path inside the project), not from the share – it shows what
    Projektakte has read, and works while the share is not reachable."""
    folder = clean_folder(folder)
    depth = len(folder.split("/")) if folder else 0
    prefix = f"{folder}/" if folder else ""
    folders: dict[str, dict] = {}
    files: list[dict] = []
    for d in docs:
        parts = (d["rel_path"] or "").replace("\\", "/").split("/")
        if not "/".join(parts).startswith(prefix) or len(parts) <= depth:
            continue
        if len(parts) == depth + 1:
            files.append(d)
            continue
        name = parts[depth]
        f = folders.setdefault(name, {"name": name, "path": prefix + name, "count": 0, "problems": 0})
        f["count"] += 1
        f["problems"] += d.get("status") != "ok"
    crumbs, acc = [], []
    for part in folder.split("/") if folder else []:
        acc.append(part)
        crumbs.append({"name": part, "path": "/".join(acc)})
    return {"folder": folder, "crumbs": crumbs,
            "parent": "/".join(folder.split("/")[:-1]) if folder else None,
            "folders": sorted(folders.values(), key=lambda f: f["name"].casefold()),
            "files": sorted(files, key=lambda d: (d["filename"] or d["rel_path"]).casefold()),
            "found": not folder or bool(folders or files)}


def doc_type_of(d: dict) -> str | None:
    """The document type shown for a document row (dokumentarten.py) – not a weak guess."""
    return d.get("doc_type") if (d.get("doc_type_confidence") or 0) >= dokumentarten.MIN_RANKED else None


def doc_type_note(d: dict) -> str | None:
    """How the type of a document was found, for the document page ("aus dem Inhalt: „Brandschutzkonzept …“")."""
    if not doc_type_of(d):
        return None
    how = dokumentarten.METHOD_LABELS.get(d.get("doc_type_method") or "", "")
    title = d.get("doc_type_title")
    return f"{how}: „{title}“" if how and title else how or None


templates.env.globals.update(doc_type_of=doc_type_of, doc_type_note=doc_type_note)


def _project_docs(conn, number: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, rel_path, filename, ext, size, doc_date, pages, status, message, method, "
        f"CASE WHEN doc_type_confidence >= {dokumentarten.MIN_RANKED} THEN doc_type END AS doc_type FROM documents "
        "WHERE project = ? AND mail_id IS NULL ORDER BY rel_path", (number,))]


@router.get("/projekte/{number}/ordner", response_class=HTMLResponse)
def project_folder(request: Request, number: str, pfad: str = ""):
    """The file browser of a project at one folder: the HTMX partial, else the whole project page."""
    if not is_htmx(request):
        return RedirectResponse(folder_url(number, clean_folder(pfad)), status_code=303)
    s = _settings(request)
    with db.session(s.db_path) as conn:
        p = get_project(conn, number)
        if not p:
            raise HTTPException(404, f"Projekt {number} nicht gefunden")
        docs = _project_docs(conn, p["number"])
    return render(request, "_doc_browser.html", p=p, browser=folder_view(docs, pfad))


def _below_projects_dir(path: str, projects_dir) -> str:
    try:
        return str(Path(path).relative_to(projects_dir))
    except ValueError:
        return path


@router.get("/fragen", response_class=HTMLResponse)
def ask_page(request: Request, project: str | None = None, q: str = ""):
    """Question form; ``q`` fills in a question typed elsewhere."""
    with db.session(_settings(request).db_path) as conn:
        projects = as_tree(list_projects(conn))
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
        projects = as_tree(list_projects(conn))
    if is_htmx(request):
        return templates.TemplateResponse(request, "_answer.html", {"result": result, "question": question})
    return render(request, "ask.html", projects=projects, project=p["number"] if p else None,
                  result=result, question=question)


_IDENTIFIER_TOKEN = re.compile(r"[^\W_]+(?:[-/.][^\W_]+)*")
MAX_PROJECT_LINKS = 3
# words before a number that make it the number of a standard or rule, never a site: DIN 4102, EN 1634,
# DIN EN ISO 13943, VdS 2095, DGUV 205-001 – in a fire-safety office these often equal a site number
_STANDARD_PREFIX = frozenset({"din", "en", "iso", "iec", "vde", "vds", "vdi", "dvgw", "dguv", "trbs", "trgs",
                              "asr", "bgv", "bgr", "bgi", "nfpa", "cen", "ts"})


@lru_cache(maxsize=16)
def _number_finder(pattern: str | None) -> re.Pattern | None:
    try:
        return number_regex(pattern or DEFAULT_PROJECT_NUMBER_PATTERN)
    except re.error:
        return None


def projects_named_in(conn, query: str, code_pattern: str | None = None) -> list[dict]:
    """Projects whose number appears in a search query ('BS-2024-0233', also typed with spaces as
    'bs 2024 0233' → link to the project page; ``code_pattern``: the effective PROJECT_NUMBER_PATTERN).
    Candidates are what has the shape of a project number (also 'P12345', '2024 017') and, as
    before, every token with digits and a hyphen."""
    joined = join_spaced_codes(query or "", code_pattern)
    finder = _number_finder(code_pattern)
    shaped = [m.group(0) for m in finder.finditer(joined)] if finder else []
    tokens = [t for t in _IDENTIFIER_TOKEN.findall(joined) if any(c.isdigit() for c in t) and "-" in t]
    # a site (Bauvorhaben) is named by its shorter number ("1015"), which is no project number: only
    # sites are looked up for plain numbers – for a link, never to assign anything
    plain, before = [], ""
    for t in _IDENTIFIER_TOKEN.findall(joined):  # not after a standard's name (DIN 1827, DIN EN 1634)
        if t.isdigit() and before not in _STANDARD_PREFIX:
            plain.append(t)
        before = t.lower()
    found: dict[str, dict] = {}
    for token in dict.fromkeys(shaped + tokens + plain):
        p = get_project(conn, token)
        if p and (token in shaped or token in tokens or p.get("kind") == BAUVORHABEN):
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
                                k=SEARCH_HITS, rerank_top_k=s.rerank_top_k, synonyms_file=s.synonyms_file,
                                doc_types=TYPES_TOP)
            named = projects_named_in(conn, query, code_pattern_of(request))
        projects = as_tree(list_projects(conn))
    if res is not None:
        view = search_view(res, min_score=getattr(s, "search_min_rerank", 0.05), show_weak=weak == "1",
                           query=query, code_pattern=code_pattern_of(request))
    return render(request, "search.html", q=query, project=number, projects=projects, res=res, view=view,
                  show_project=not number or (p or {}).get("kind") == BAUVORHABEN,
                  named_projects=named, all_projects_url=search_url(query, None, weak="1" if weak == "1" else ""),
                  weak_url=search_url(query, number, weak="1"), strong_url=search_url(query, number),
                  hq=" ".join([query, *(c.word for c in res.corrections)]) if res is not None else query,
                  corrected_url=search_url(tippfehler.corrected_query(query, res.corrections), number)
                  if res is not None and res.corrections else "")


# ------------------------------------------------------------------------------------- inbox
def inbox_url(status: str = "", page: int = 1, **cursor: str) -> str:
    """Address of an inbox page: filter plus the keyset cursor (``ab``: page starts at this mail,
    ``vor``: the page before this mail, ``ende``: last page); ``page`` is the old offset form."""
    params = {"status": status, "page": page if page > 1 else ""}
    params.update(cursor)
    params = {k: v for k, v in params.items() if v}
    return "/eingang" + ("?" + urlencode(params, safe=",") if params else "")


# Carried cards (``gezeigt``): a rejected suggestion stays actionable on its page and is counted there,
# although it now sorts further back. The "Weiter" link names it, so the page it now falls into shows
# it once more – marked, without counting it again – and positions go on after it. The value is signed:
# only the application's own links mark cards or move the count; any other value is ignored, and no
# value at all can hide a mail (every open mail of a page's key range is shown).
_PROCESS_SECRET = secrets.token_hex(32)  # without SECRET_KEY: links of this process only (like sessions)
CARRY_SIG_CHARS = 22  # 128 bits of HMAC-SHA256, URL-safe base64


@lru_cache(maxsize=8)
def _carry_key(secret: str) -> bytes:
    root = hashlib.sha256((secret or _PROCESS_SECRET).encode("utf-8")).digest()
    return hmac.new(root, b"projektakte/eingang-gezeigt/v1", hashlib.sha256).digest()


def _carry_sig(request: Request, status: str, ids_text: str) -> str:
    key = _carry_key(getattr(_settings(request), "secret_key", "") or "")
    mac = hmac.new(key, f"{status}|{ids_text}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii")[:CARRY_SIG_CHARS]


def sign_carried(request: Request, status: str, ids: list[int]) -> str:
    """``gezeigt`` value for the cards earlier pages showed and counted: '127,133.<signature>'."""
    if not ids:
        return ""
    text = inbox.encode_ids(ids)
    return f"{text}.{_carry_sig(request, status, text)}"


def read_carried(request: Request, status: str, value: str | None) -> list[int]:
    """Ids of a ``gezeigt`` value made by :func:`sign_carried` for this filter; [] for anything else."""
    text, sep, sig = (value or "").rpartition(".")
    if not sep or len(sig) != CARRY_SIG_CHARS or not sig.isascii():
        return []
    if not hmac.compare_digest(sig, _carry_sig(request, status, text)):
        return []
    return inbox.decode_ids(text)


def _inbox_page_rows(conn, status: str, total: int, *, ab: str, vor: str, ende: str, page: str) -> list[dict]:
    """One page read by key, plus the first mail of the next page if there is one (to link it).
    Every open mail of the range is part of it – also a card an earlier page still shows."""
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


def inbox_range(first: int, last: int, total: int, pager: bool = False, repeated: int = 0) -> dict:
    """What the summary line and the pager say about the shown part of the inbox: positions
    ``first``–``last`` of ``total`` open mails (``done``: nothing the page counts is left open) and
    ``repeated`` open cards of earlier pages that the page shows once more without counting them. A
    page with a pager keeps the "first–last of total" form after decisions even when the total falls
    to one page – "29 Mails" would claim that all of them are on this page."""
    return {"first": first, "last": last, "total": total, "done": last < first, "repeated": repeated,
            "paged": total > inbox.PAGE_SIZE or first > 1 or pager, "pager": pager}


@router.get("/eingang", response_class=HTMLResponse)
def inbox_page(request: Request, status: str = "", ab: str = "", vor: str = "", ende: str = "",
               page: str = "", gezeigt: str = ""):
    """``gezeigt``: cards that earlier pages showed and counted although they sort onto this page or
    behind it now (a rejected suggestion) – signed, passed on by "Weiter". This page still shows
    such a card, marked as shown before, but does not count it again."""
    s = _settings(request)
    status = status if status in INBOX_STATUSES else ""
    size = inbox.PAGE_SIZE
    # only a page after the first follows earlier pages: its "Weiter" cursor (``ab``) comes along
    earlier = read_carried(request, status, gezeigt) if ab else []
    carry: list[int] = []  # of those: the ones that count before this page …
    carry_on: list[int] = []  # … and before the next page
    with db.session(s.db_path) as conn:
        counts = inbox.counts_by_status(conn)
        total = counts[status]
        rows = _inbox_page_rows(conn, status, total, ab=ab, vor=vor, ende=ende, page=page)
        mails, following = rows[:size], rows[size:size + 1]
        first = 0
        if mails:
            first_key = inbox.sort_key(mails[0])
            if earlier:
                carry = inbox.carried(conn, earlier, behind=first_key, status=status, inclusive=True)
                carry_on = inbox.carried(conn, carry, behind=inbox.sort_key(mails[-1]), status=status)
            first = inbox.count_before(conn, first_key, status=status) + len(carry) + 1
            if carry:  # a rejected card that left the filter was counted on top of the total there
                left, in_filter = inbox.count_ids(conn, carry, status=status)
                total += left - in_filter
        projects = as_tree(list_projects(conn))
    for m in mails:
        m["repeated"] = m["id"] in carry
    counted = [m for m in mails if not m["repeated"]]
    repeated = [m for m in mails if m["repeated"]]
    first_key = inbox.sort_key(mails[0]) if mails else None
    last_key = inbox.sort_key(mails[-1]) if mails else None
    last = first + len(counted) - 1 if mails else 0
    next_key = inbox.sort_key(following[0]) if following else None
    back = (inbox_url(status, ab=inbox.encode_key(first_key), gezeigt=sign_carried(request, status, carry))
            if mails and first > 1 else inbox_url(status))
    shown = inbox_range(first, last, total, repeated=len(repeated))
    page_state = None
    if mails:
        page_state = {"seite_status": status, "seite_von": inbox.encode_key(first_key),
                      "seite_bis": inbox.encode_key(last_key), "seite_pager": "1" if shown["paged"] else "",
                      "seite_ids": inbox.encode_ids(counted)}
        if carry:
            page_state["seite_gezeigt"] = sign_carried(request, status, carry)
        if repeated:
            page_state["seite_wieder"] = inbox.encode_ids(repeated)
        if next_key:  # a decision renews the "Weiter" link (rejected cards that moved behind the page)
            page_state["seite_weiter"] = inbox.encode_key(next_key)
    return render(request, "inbox.html", mails=mails, projects=projects, assign_mode=s.assign_mode,
                  counts=counts, status=status, total=total, back=back, shown=shown, page_state=page_state,
                  filter_urls={k: inbox_url(k) for k in INBOX_FILTERS},
                  start_url=inbox_url(status) if first > 1 else None,
                  prev_url=inbox_url(status, vor=inbox.encode_key(first_key)) if first > 1 else None,
                  next_url=(inbox_url(status, ab=inbox.encode_key(next_key),
                                      gezeigt=sign_carried(request, status, carry_on)) if next_key else None),
                  end_url=inbox_url(status, ende="1") if next_key else None)


def _page_summary(request: Request, conn, state: dict) -> dict | None:
    """The summary line of the inbox page a decision came from (sent with the HTMX request as
    ``seite_*``): position of the mails still open on that page and the total of its filter.

    "Still open" are the cards the page counts (``seite_ids``) that can still be decided. A rejected
    suggestion is one of them although its sort key changed (it moves behind the suggestions, and
    in the filter "Mit Vorschlag" it no longer belongs to the filter – it is counted on top of the
    filter's total then, so the line never claims fewer open mails than the page shows). Pages
    rendered without ids fall back to the key range of the page.

    Such cards of earlier pages (``gezeigt``, signed) count before this page; the page shows those
    that fall into its range once more (``wieder``) without counting them. With ``weiter`` (the start
    of the next page) the answer also carries the new "Weiter" link (``next_url``): it passes on the
    cards that now sort behind this page, so the next page does not count them again."""
    first_key, last_key = inbox.decode_key(state.get("von")), inbox.decode_key(state.get("bis"))
    status = state.get("status") or ""
    if first_key is None or last_key is None or status not in ("", *INBOX_STATUSES):
        return None
    total = inbox.counts_by_status(conn)[status]
    before = inbox.count_before(conn, first_key, status=status)
    earlier = read_carried(request, status, state.get("gezeigt"))
    again = sorted(set(inbox.decode_ids(state.get("wieder"))) & set(earlier))
    repeated, repeated_in_filter = inbox.count_ids(conn, again, status=status)
    ids = inbox.decode_ids(state.get("ids"))
    if ids:
        left, in_filter = inbox.count_ids(conn, ids, status=status)
        total += left - in_filter
    else:  # the key range holds the cards shown once more as well
        left = max(0, inbox.count_between(conn, first_key, last_key, status=status) - repeated_in_filter)
    if earlier:
        e_left, e_in_filter = inbox.count_ids(conn, earlier, status=status)
        total += e_left - e_in_filter
        before += len(inbox.carried(conn, earlier, behind=first_key, status=status, inclusive=True))
    shown = inbox_range(before + 1, before + left, total, pager=state.get("pager") == "1", repeated=repeated)
    next_key = inbox.decode_key(state.get("weiter"))
    if next_key is not None:
        carry = inbox.carried(conn, earlier + ids, behind=last_key, status=status)
        shown["next_url"] = inbox_url(status, ab=inbox.encode_key(next_key),
                                      gezeigt=sign_carried(request, status, carry))
    return shown


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


def _decide(request: Request, conn, mail_id: int, action: str, project: str, user: str | None, mode: str,
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
            "summary": _page_summary(request, conn, page_state) if page_state else None}


def _card_context(outcome: dict, back: str, repeated: bool = False) -> dict:
    """Template and context of the inbox card after a decision.

    Real decisions (assigned, no project) become a green "done" card. A rejected suggestion or a
    failed decision keeps the mail actionable in the inbox (neutral card with the buttons; a card
    shown once more from an earlier page keeps that mark)."""
    mail, result, error = outcome["mail"], outcome["result"], outcome["error"]
    ctx = {"mail": mail, "m": mail, "result": result, "error": error, "name": outcome["name"], "back": back,
           "counts": outcome["counts"], "summary": outcome.get("summary"), "repeated": repeated}
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
                 seite_bis: str = Form(""), seite_pager: str = Form(""), seite_ids: str = Form(""),
                 seite_gezeigt: str = Form(""), seite_wieder: str = Form(""), seite_weiter: str = Form("")):
    """A decision from an inbox card or the mail page. HTMX requests carry the cursor of their page
    (``seite_*``, set on the card list) so the summary line of that page is updated as well."""
    s = _settings(request)
    back = safe_back(back)
    user = getattr(request.state, "user", None)
    page_state = ({"status": seite_status, "von": seite_von, "bis": seite_bis, "pager": seite_pager,
                   "ids": seite_ids, "gezeigt": seite_gezeigt, "wieder": seite_wieder, "weiter": seite_weiter}
                  if is_htmx(request) and seite_von else None)
    repeated = bool(page_state) and mail_id in inbox.decode_ids(seite_wieder) and mail_id in read_carried(
        request, seite_status, seite_gezeigt)
    try:
        with db.session(s.db_path) as conn:
            outcome = _decide(request, conn, mail_id, action, project, user, s.assign_mode, page_state)
    except LookupError:
        raise HTTPException(404, "Mail nicht gefunden") from None
    except sqlite3.OperationalError as exc:
        if not inbox.is_db_busy(exc):
            raise
        log.warning("Entscheidung zu Mail %s nicht gespeichert – Datenbank belegt: %s", mail_id, exc)
        return _busy_response(request, mail_id, back, project if action == "assign" else "", repeated)
    if is_htmx(request):
        return templates.TemplateResponse(request, "_inbox_update.html", _card_context(outcome, back, repeated))
    if outcome["error"]:
        return render(request, "error.html", status_code=400, title="Zuordnung nicht möglich",
                      message=outcome["error"], back=back, back_label="Zurück")
    return RedirectResponse(back, status_code=303)


def _busy_response(request: Request, mail_id: int, back: str, typed: str = "", repeated: bool = False) -> HTMLResponse:
    """The decision could not be written: friendly German message, the mail stays actionable (a card
    shown once more from an earlier page keeps that mark, like after any other failed decision)."""
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
           "name": None, "back": back, "counts": counts, "repeated": repeated}
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
        projects = as_tree(list_projects(conn))
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
def document_page(request: Request, doc_id: int, seite: str = ""):
    s = _settings(request)
    with db.session(s.db_path) as conn:
        d = conn.execute("SELECT d.*, p.name AS project_name FROM documents d LEFT JOIN projects p ON "
                         "p.number = d.project WHERE d.id = ?", (doc_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Dokument nicht gefunden")
        rows = _chunk_rows(conn, doc_id)
        mail = None
        if d["mail_id"]:
            mail = conn.execute("SELECT id, subject, date FROM mails WHERE id = ?", (d["mail_id"],)).fetchone()
    d = dict(d)
    v = viewer_view(s, d, page=_page_param(seite), mail=dict(mail) if mail else None)
    d["available"] = v["available"]
    chunks = [chunk_view(c) for c in without_overlap(rows)]
    return render(request, "document.html", d=d, chunks=chunks, mail=dict(mail) if mail else None, v=v)


def _chunk_rows(conn, doc_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, ord, page, heading, text FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,))]


def _page_param(value: str | int | None) -> int | None:
    """A page number from the address (1 … 99999), anything else is ignored."""
    try:
        n = int(str(value or "").strip())
    except ValueError:
        return None
    return n if 1 <= n <= 99_999 else None


def viewer_view(s, d: dict, *, page: int | None = None, chunk: int | None = None, mail: dict | None = None) -> dict:
    """How the viewer shows a document row (see betrachter.py): ``kind`` pdf / bild / wandeln / text, the
    file and text addresses, the page (PDF page or TIFF page), notes. A file that is not there (attachment
    still pending, file gone) is always shown as text with the existing note, never as a broken frame."""
    path = original_file(s, d)
    kind = betrachter.view_kind(d.get("ext") or Path(d.get("filename") or "").suffix)
    rel = d.get("rel_path") or ""
    folder = "" if d.get("mail_id") else str(PurePosixPath(rel).parent) if rel else ""
    v = {"id": d["id"], "filename": d.get("filename") or rel, "doc_type": doc_type_of(d), "folder": "" if folder == "." else folder,
         "mail": mail, "available": path is not None, "status": d.get("status"), "message": d.get("message"),
         "file_url": file_url(d["id"], d.get("filename")), "text_url": f"/dokumente/{d['id']}",
         "page": page, "chunk": chunk, "pages": 1, "note": "", "active": betrachter.is_active(d.get("ext"))}
    if chunk:
        v["text_url"] += f"#c{chunk}"
    if path is None:
        kind = "text"
    elif kind == "wandeln":
        try:
            v["pages"] = betrachter.image_info(path).pages
        except betrachter.PreviewError as exc:
            kind, v["note"] = "text", str(exc)
        v["page"] = min(page or 1, v["pages"])
    v["kind"] = kind
    v["frame_url"] = v["file_url"] + (f"#page={page}" if kind == "pdf" and page else "")
    v["preview_url"] = f"/dokumente/{d['id']}/vorschau" + (f"?seite={v['page']}" if (v["page"] or 1) > 1 else "")
    return v


@router.get("/dokumente/{doc_id}/ansicht", response_class=HTMLResponse)
def document_viewer(request: Request, doc_id: int, seite: str = "", c: str = ""):
    """The viewer panel of a document (a fragment for static/betrachter.js). Without the script – an
    address typed or opened in a new tab – the document page."""
    page = _page_param(seite)
    chunk = int(c.strip()) if c.strip().isdigit() and len(c.strip()) <= 12 else None
    if not is_htmx(request):
        query = f"?seite={page}" if page else ""
        return RedirectResponse(f"/dokumente/{doc_id}{query}" + (f"#c{chunk}" if chunk else ""), status_code=303)
    s = _settings(request)
    with db.session(s.db_path) as conn:
        d = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Dokument nicht gefunden")
        d = dict(d)
        mail = None
        if d["mail_id"]:
            mail = conn.execute("SELECT id, subject, date FROM mails WHERE id = ?", (d["mail_id"],)).fetchone()
        v = viewer_view(s, d, page=page, chunk=chunk, mail=dict(mail) if mail else None)
        chunks = [chunk_view(r) for r in without_overlap(_chunk_rows(conn, doc_id))] if v["kind"] == "text" else []
    if chunk and not any(ch["id"] == chunk for ch in chunks):
        v["chunk"] = None
    return templates.TemplateResponse(request, "_betrachter.html", {"v": v, "chunks": chunks})


@router.get("/dokumente/{doc_id}/vorschau")
def document_preview(request: Request, doc_id: int, seite: str = ""):
    """A TIFF or BMP page converted to PNG/JPEG for the viewer (betrachter.preview: bounded, one at a
    time). Same checks as the original file: only files known to the index, below the configured roots."""
    s = _settings(request)
    with db.session(s.db_path) as conn:
        d = conn.execute("SELECT path, cache_path, filename, ext, mail_id, status FROM documents WHERE id = ?",
                         (doc_id,)).fetchone()
    if not d:
        raise HTTPException(404, "Dokument nicht gefunden")
    path = original_file(s, d)
    if path is None or betrachter.view_kind(d["ext"] or path.suffix) != "wandeln":
        raise HTTPException(404, "Keine Vorschau verfügbar")
    try:
        data, media = betrachter.preview(path, _page_param(seite) or 1)
    except betrachter.PreviewBusy:
        raise HTTPException(503, "Die Vorschau ist gerade belegt – bitte gleich erneut versuchen.",
                            headers={"Retry-After": "5"}) from None
    except betrachter.PreviewError as exc:
        raise HTTPException(422, str(exc)) from None
    return Response(data, media_type=media)


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
    path = Path(raw)
    if "\ufffd" in raw:  # a folder name in a wrong encoding (stored with �): open it under its real name
        from .documents import real_folder

        parent = real_folder(path.parent)
        if parent is None:
            return None
        path = parent / path.name
    path = path.resolve()
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
    ext = path.suffix.lower()
    inline = ext in INLINE_EXT
    # HTML, SVG & Co. would run as active content in the application's origin: always a plain download
    media = "application/octet-stream" if betrachter.is_active(ext) or betrachter.is_active(
        Path(d["filename"] or "").suffix) else None
    return FileResponse(path, filename=d["filename"], media_type=media,
                        content_disposition_type="inline" if inline else "attachment")


INLINE_EXT = frozenset({".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".gif", ".webp"})


# ------------------------------------------------------------------------------------- status
@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request, tab: str = "", q: str = ""):
    from .api import status as api_status

    st = api_status(request)
    with db.session(_settings(request).db_path) as conn:
        rf = reihenfolge_context(request, conn, tab, q)
    return render(request, "status.html", st=st, rf=rf, eye=rf["now"]["vision"], emb=rf["now"]["embed"])


@router.get("/status/dienste", response_class=HTMLResponse)
def deps_partial(request: Request):
    from .main import health_deps_data

    return templates.TemplateResponse(request, "_deps.html", {"deps": health_deps_data(request.app)})


@router.post("/status/neu-indexieren")
def reindex_now(request: Request):
    request.app.state.indexer.trigger()
    return RedirectResponse("/status", status_code=303)


# --- project structure (PROJECT_NUMBER_PATTERN=auto): a person confirms or rejects a proposal
LAYOUT_ANCHOR = "/status#projektordner"


def _layout_decision(request: Request, action, *, reindex: bool = True):
    """Run ``action(conn, settings, user)`` on the database, then start an indexing pass (the decision
    takes effect at once) and go back to the status page. Login and the cross-site check are done
    for every POST route by the middleware (main.access_control)."""
    s = _settings(request)
    user = getattr(request.state, "user", None)
    try:
        with db.session(s.db_path) as conn:
            action(conn, s, user)
    except structure.ProposalError as exc:
        return render(request, "error.html", status_code=409, title="Vorschlag nicht mehr aktuell",
                      message=str(exc), back=LAYOUT_ANCHOR, back_label="Zur Statusseite")
    except sqlite3.OperationalError as exc:
        if not inbox.is_db_busy(exc):
            raise
        log.warning("Entscheidung zur Projektstruktur nicht gespeichert – Datenbank belegt: %s", exc)
        return render(request, "error.html", status_code=503, headers={"Retry-After": str(BUSY_RETRY_AFTER)},
                      title="Bitte gleich noch einmal",
                      message="Die Datenbank ist gerade belegt (die Indexierung schreibt) – die Entscheidung wurde "
                              "nicht gespeichert. Bitte in einigen Sekunden erneut versuchen.",
                      back=LAYOUT_ANCHOR, back_label="Zur Statusseite")
    if reindex:
        request.app.state.indexer.trigger()
    return RedirectResponse(LAYOUT_ANCHOR, status_code=303)


@router.post("/status/projektstruktur/uebernehmen")
def layout_confirm(request: Request, vorschlag: str = Form("")):
    """Confirm the pending proposal the page showed (its id): it is used from this indexing pass on."""
    return _layout_decision(request, lambda conn, s, user: structure.confirm_proposal(conn, s, vorschlag, user))


@router.post("/status/projektstruktur/ablehnen")
def layout_reject(request: Request, vorschlag: str = Form("")):
    """Reject the pending proposal: everything stays as it is; it is not offered again until the
    detection changes."""
    return _layout_decision(request, lambda conn, s, user: structure.reject_proposal(conn, s, vorschlag, user),
                            reindex=False)


@router.post("/status/projektstruktur/neu-pruefen")
def layout_recheck(request: Request, ablehnung: str = Form("")):
    """Analyse the share again at once (after folders were fixed); ``ablehnung=aufheben`` also offers a
    rejected proposal again."""
    return _layout_decision(request, lambda conn, s, user: structure.request_recheck(
        conn, s, forget_rejection=ablehnung == "aufheben"))


# --- Reihenfolge: order of the documents pass (reihenfolge.py) – folders, the project being read, the queue
RF_ANCHOR = "reihenfolge"
RF_TABS = {"warteschlange": "Warteschlange", "fertig": "Fertig", "alle": "Alle"}
RF_MAX_ROWS = 50  # rows of the project list; a number typed in the search finds any project
RF_POLL_RUNNING = 4  # seconds between two updates of "Wird gerade gelesen" while a pass runs
RF_POLL_IDLE = 30


def current_profile(request: Request) -> str:
    """The extraction profile of this app's model: a project is done only with it (reihenfolge.py)."""
    ai = getattr(request.app.state, "ai", None)
    return extraction_profile(getattr(ai, "llm", None))


def rf_view(request: Request, conn) -> reihenfolge.View:
    s = _settings(request)
    return reihenfolge.view(conn, s.projects_dir, current_profile(request), getattr(s, "index_folder_order", ()))


def rf_url(tab: str = "", q: str = "", partial: bool = False) -> str:
    params = {k: v for k, v in (("tab", tab if tab != "warteschlange" else ""), ("q", q)) if v}
    base = "/status/reihenfolge/liste" if partial else "/status"
    return base + (f"?{urlencode(params)}" if params else "") + ("" if partial else f"#{RF_ANCHOR}")


def _rf_tab(tab: str) -> str:
    return tab if tab in RF_TABS else "warteschlange"


def _rf_query(q: str) -> str:
    return (q or "").strip()[:100]


def _rf_now(conn, view: reihenfolge.View | None, request: Request) -> dict:
    """The card "Wird gerade gelesen": the project of the running pass, its files, the phase, the next three,
    the times and estimates (zeiten.py), and the Bildauswertung running beside it."""
    snap = reihenfolge.LIVE.snapshot()
    project = snap["project"]
    if project and not project.get("name"):  # Bildauswertung: only the number is known
        row = conn.execute("SELECT name FROM projects WHERE number = ?", (project["number"],)).fetchone()
        project["name"] = row[0] if row else ""
    if project:
        project["folder_label"] = reihenfolge.folder_label(project["folder"])
    if snap["running"]:  # the next projects of this pass – as sorted again after a change meanwhile
        stale = snap["pending"] and snap["version"] != reihenfolge.version(conn)
        if stale and view is None:
            view = rf_view(request, conn)
        upcoming = reihenfolge.upcoming(snap, view if stale else None)
        queue_left = snap["queue_left"]
    else:  # waiting: the head of the queue for the next pass
        view = view if view is not None else rf_view(request, conn)
        upcoming = [e["number"] for e in view.queue[:3]]
        queue_left = len(view.queue)
    phase = (snap["phase"] or "").split(" – ")[0]
    reading = snap["running"] and phase == "Dokumente" and bool(project)
    times = _rf_times(conn, snap, reading, queue_left)
    eye = _vision_now(request, conn)
    embed = _embed_now(request, conn)
    types = _doctypes_now(request, conn)
    background = bool(eye and eye["busy"]) or bool(embed and embed["busy"]) or bool(types and types["busy"])
    return {"running": snap["running"], "phase": phase, "phase_detail": snap["phase"], "project": project,
            "seen": snap["seen"], "total": snap["total"], "upcoming": upcoming, "times": times, "vision": eye,
            "embed": embed, "doctypes": types, "background": background, "active": snap["running"] or background,
            "poll": RF_POLL_RUNNING if snap["running"] or background else RF_POLL_IDLE}


def _doctypes_now(request: Request, conn) -> dict | None:
    """The document types in the background (dokumentarten.DokumentartWorker), None without it: documents
    still without a type (older ones, typed in batches) and unclear ones waiting for the language model."""
    worker = getattr(getattr(request.app.state, "indexer", None), "doctypes", None)
    if worker is None:
        return None
    live = dokumentarten.LIVE.snapshot()
    counts = dokumentarten.counts(conn)
    ai_open = counts["ai_open"] if live["ai"] else 0
    return {"alive": worker.alive, "busy": bool(worker.alive and (counts["todo"] or ai_open)), "todo": counts["todo"],
            "ai": live["ai"], "ai_open": ai_open, "typed": live["typed"], "asked": live["asked"],
            "error": live["error"]}


def _embed_now(request: Request, conn) -> dict | None:
    """The background embedding (embed_worker.EmbedWorker), None without it: sections done and waiting, the
    project and file of the current batch, rate and rough time left."""
    worker = getattr(getattr(request.app.state, "indexer", None), "embedder", None)
    if worker is None:
        return None
    live = embed_worker.LIVE.snapshot()
    pending = live["pending"]
    rate = zeiten.embed_rate(zeiten.read(conn, zeiten.EMBED_KEY), pending or 0)
    current = live["current"]
    if current and current.get("project"):
        row = conn.execute("SELECT name FROM projects WHERE number = ?", (current["project"],)).fetchone()
        current["name"] = row[0] if row else ""
    return {"busy": bool(worker.alive and (live["busy"] or pending)), "alive": worker.alive,
            "concurrency": live["concurrency"], "in_flight": live["in_flight"], "done": live["done"],
            "pending": pending, "current": current, "error": live["error"], **rate}


def _rf_times(conn, snap: dict, reading: bool, queue_left: int) -> dict:
    """Elapsed times of the pass and the project, the average per file read, and the rough estimates."""
    per_project = zeiten.read(conn, zeiten.PROJECT_KEY)
    per_file = zeiten.read(conn, zeiten.FILE_KEY)
    read, read_seconds = snap["run_read"], snap["run_read_seconds"]
    project_eta = (zeiten.project_eta(snap["seen"], snap["total"], snap["project_elapsed"] or 0)
                   if reading else None)
    queue_eta = zeiten.queue_eta(queue_left, per_project, per_file)
    if queue_eta is not None and project_eta is not None:
        queue_eta += project_eta
    return {"run_elapsed": snap["run_elapsed"] if snap["running"] else None,
            "project_elapsed": snap["project_elapsed"] if reading else None,
            "files_read": read, "files_unchanged": max(0, snap["run_seen"] - read),
            "per_file": read_seconds / read if read else per_file.value,
            "per_file_run": bool(read),
            "project_eta": project_eta, "queue_left": queue_left, "queue_eta": queue_eta,
            "per_project": per_project.value if per_project.samples >= zeiten.MIN_PROJECTS else None,
            "queue_samples": per_project.samples, "queue_min": zeiten.MIN_PROJECTS}


def _vision_now(request: Request, conn) -> dict | None:
    """The Bildauswertung in the background (vision.VisionWorker), None without it (LLM_VISION=0, no app
    worker): busy, calls at a time, its project, pages waiting, speed and the rough time left."""
    worker = getattr(getattr(request.app.state, "indexer", None), "vision", None)
    if worker is None:
        return None
    live = vision.LIVE.snapshot()
    pending = live["pending_pages"]
    rate = zeiten.vision_rate(zeiten.read(conn, zeiten.PAGE_KEY), live["concurrency"], pending or 0)
    project = live["project"]
    if project:
        row = conn.execute("SELECT name FROM projects WHERE number = ?", (project["number"],)).fetchone()
        project["name"] = row[0] if row else ""
    return {"busy": bool(worker.alive and pending), "alive": worker.alive, "concurrency": live["concurrency"],
            "project": project, "pending": pending, "off_pages": live["off_pages"], "pages": live["pages"],
            "in_flight": live["in_flight"], "document": live["document"],
            "error": worker.last_error, **rate}


def _rf_match(number: str, q: str) -> tuple[str, str, str] | None:
    """The number split around the searched part (marked in the list)."""
    i = number.casefold().find(q.casefold()) if q else -1
    return (number[:i], number[i:i + len(q)], number[i + len(q):]) if i >= 0 else None


def reihenfolge_context(request: Request, conn, tab: str = "", q: str = "", note: str = "",
                        problem: bool = False, limit: int = RF_MAX_ROWS) -> dict:
    """Everything the section "Reihenfolge" of the status page shows – one pass over the projects (one
    query, sorted in Python), file counts only for the rows shown."""
    tab, q = _rf_tab(tab), _rf_query(q)
    v = rf_view(request, conn)
    now = _rf_now(conn, v, request)
    running = now["project"]["number"] if now["running"] and now["project"] and now["phase"] == "Dokumente" else None
    queue = v.queue_groups  # a site with its orders is one project here (reihenfolge.group_entries)
    done = v.done_groups
    off = v.off_groups
    if running:  # the running order counts as its site
        running = next((e["number"] for e in v.grouped if running in e["members"]), running)
    if running:  # the project being read leads the queue (also one already done that is checked again)
        queue = [e for e in queue if e["number"] == running] + [e for e in queue if e["number"] != running]
    rows = {"warteschlange": queue, "fertig": done, "alle": queue + off + done}[tab]
    if q:
        needle = q.casefold()
        rows = [e for e in rows if any(needle in n.casefold() for n in e["members"])]
    shown = rows[:max(1, limit)]
    numbers = [n for e in shown for n in e["members"]]
    files: dict[str, int] = {}
    if numbers:
        marks = ",".join("?" * len(numbers))
        files = {r[0]: r[1] for r in conn.execute(
            f"SELECT project, COUNT(*) FROM documents WHERE mail_id IS NULL AND project IN ({marks}) GROUP BY project",
            numbers)}
    position = {e["number"]: i + 1 for i, e in enumerate(queue)}
    out = []
    for e in shown:
        state = ("run" if e["number"] == running else "done" if e["done"] and e["pinned"] is None
                 else "off" if e["off"] else "pin" if e["pinned"] is not None else "done" if e["done"] else "wait")
        out.append({"number": e["number"], "name": e["name"], "kind": e["kind"],
                    "folder": reihenfolge.folder_label(e["folder"]),
                    "files": sum(files.get(n, 0) for n in e["members"]), "orders": len(e["members"]) - (e["kind"] == "bauvorhaben"),
                    "state": state, "done_at": e["done_at"], "skipped": e["done_skipped"] or 0, "off": e["off"],
                    "position": position.get(e["number"]),
                    "match": _rf_match(e["number"], q)})
    return {"tab": tab, "q": q, "rows": out, "total": len(rows), "shown": len(out), "queue_count": len(queue),
            "done_count": len(done), "off_count": len(off), "folders": v.folders, "customized": v.customized,
            "now": now, "note": note,
            "problem": problem, "default_order": list(getattr(_settings(request), "index_folder_order", ()))}


@router.get("/status/reihenfolge/liste", response_class=HTMLResponse)
def rf_list(request: Request, tab: str = "", q: str = ""):
    """The project list (tabs, search) – HTMX swaps it; without JavaScript the status page itself."""
    if not is_htmx(request):
        return RedirectResponse(rf_url(_rf_tab(tab), _rf_query(q)), status_code=303)
    with db.session(_settings(request).db_path) as conn:
        rf = reihenfolge_context(request, conn, tab, q)
    return templates.TemplateResponse(request, "_reihenfolge_liste.html", {"rf": rf, "is_partial": True})


@router.get("/status/reihenfolge/jetzt", response_class=HTMLResponse)
def rf_now(request: Request):
    """The card "Wird gerade gelesen" (polled by HTMX: every few seconds during a pass, rarely otherwise)."""
    with db.session(_settings(request).db_path) as conn:
        now = _rf_now(conn, None, request)
    return templates.TemplateResponse(request, "_reihenfolge_jetzt.html", {"now": now})


def _rf_change(request: Request, action, tab: str, q: str):
    """Run ``action(conn, settings) -> note`` and show the section again: HTMX gets it as a fragment, a
    form without JavaScript is sent back to the status page (post/redirect/get). Login and the cross-site
    check are done for every POST route by the middleware (main.access_control)."""
    s = _settings(request)
    tab, q = _rf_tab(tab), _rf_query(q)
    try:
        with db.session(s.db_path) as conn:
            note, problem = action(conn, s), False
    except reihenfolge.OrderError as exc:
        note, problem = str(exc), True
    except sqlite3.OperationalError as exc:
        if not inbox.is_db_busy(exc):
            raise
        log.warning("Reihenfolge nicht gespeichert – Datenbank belegt: %s", exc)
        note, problem = ("Die Datenbank ist gerade belegt (die Indexierung schreibt) – die Änderung wurde nicht "
                         "gespeichert. Bitte in einigen Sekunden erneut versuchen."), True
        if not is_htmx(request):
            return render(request, "error.html", status_code=503, headers={"Retry-After": str(BUSY_RETRY_AFTER)},
                          title="Bitte gleich noch einmal", message=note, back=rf_url(tab, q),
                          back_label="Zur Statusseite")
    if not is_htmx(request):
        if problem:
            return render(request, "error.html", status_code=409, title="Reihenfolge nicht geändert", message=note,
                          back=rf_url(tab, q), back_label="Zur Statusseite")
        return RedirectResponse(rf_url(tab, q), status_code=303)
    with db.session(s.db_path) as conn:
        rf = reihenfolge_context(request, conn, tab, q, note=note, problem=problem)
    return templates.TemplateResponse(request, "_reihenfolge.html", {"rf": rf})


@router.post("/status/reihenfolge/ordner")
def rf_folder(request: Request, ordner: str = Form(""), aktion: str = Form(""), tab: str = Form(""), q: str = Form("")):
    """Move a top-level folder up or down, switch it off or on (``aktion``: hoch, runter, aus, an)."""
    return _rf_change(request, lambda conn, s: reihenfolge.change_folder(
        conn, s.projects_dir, s.index_folder_order, ordner, aktion), tab, q)


@router.post("/status/reihenfolge/standard")
def rf_reset(request: Request, tab: str = Form(""), q: str = Form("")):
    """Back to INDEX_FOLDER_ORDER and the default order."""
    def action(conn, s):
        reihenfolge.reset_folders(conn)
        return "Ordner-Reihenfolge auf den Standard zurückgesetzt."
    return _rf_change(request, action, tab, q)


@router.post("/status/reihenfolge/vorziehen")
def rf_pin(request: Request, nummer: str = Form(""), tab: str = Form(""), q: str = Form("")):
    return _rf_change(request, lambda conn, s: reihenfolge.pin(conn, nummer, s.projects_dir,
                                                               getattr(s, "index_folder_order", ())), tab, q)


@router.post("/status/reihenfolge/zurueck")
def rf_unpin(request: Request, nummer: str = Form(""), tab: str = Form(""), q: str = Form("")):
    return _rf_change(request, lambda conn, s: reihenfolge.unpin(conn, nummer), tab, q)


templates.env.globals.update(rf_url=rf_url, RF_TABS=RF_TABS)
