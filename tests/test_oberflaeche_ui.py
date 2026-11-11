"""Web interface (package "oberflaeche"): answer status contract, search threshold, inbox paging and
badge, project attachments, local dates, status page, German wording and the CSP-safe templates."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte import db
from projektakte.ai.services import AIServices
from projektakte.ask import AskResult, cited_numbers
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.web import (answer_view, cite_links, de_date, de_num, highlight, local_day, plural,
                             resolve_project_input, router, safe_back, search_view, templates)

from .conftest import fake_settings

HX = {"HX-Request": "true"}
HTML = {"Accept": "text/html"}
TEMPLATES = Path(__file__).resolve().parent.parent / "projektakte" / "templates"
CLOSED_URL = "http://127.0.0.1:9"  # discard port: connection refused at once


@pytest.fixture(scope="module")
def demo(tmp_path_factory, fake_ai):
    """Indexed demo data with fake AI services – shared by the read-only tests of this module."""
    root = tmp_path_factory.mktemp("ui")
    generate(root / "demo")
    s = fake_settings(fake_ai, root, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    return s


@pytest.fixture(scope="module")
def ro(demo):
    return TestClient(create_app(demo, background=False))


@pytest.fixture()
def rw(tmp_path, fake_ai):
    """A private demo installation for tests that change data."""
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    return TestClient(create_app(s, background=False))


def _src(n: int, kind: str = "document", text: str = "Auflage 4: Betriebsdauer mindestens 8 Stunden.") -> dict:
    return {"n": n, "kind": kind, "doc_id": 10 + n, "mail_id": 20 + n, "chunk_id": 30 + n, "project": "BS-1",
            "label": f"Quelle-{n}.pdf", "snippet": text[:400], "text": text}


def _result(status: str, answer: str, *, sources=(), context=(), notes=()) -> AskResult:
    return AskResult(question="Wie lange muss das Notlicht funktionieren?", project="BS-1", answer=answer,
                     found=status == "answered", status=status, sources=list(sources), context=list(context),
                     notes=list(notes))


def _render_answer(result: AskResult) -> str:
    return templates.get_template("_answer.html").render(result=result, question=result.question)


def _section(html: str, start: str, end: str = "</ol>") -> str:
    i = html.index(start)
    return html[i: html.index(end, i)]


# --------------------------------------------------------------------------- BUG-13 citations
def test_cite_links_resolves_ranges_like_cited_numbers():
    html = str(cite_links("Breite 1,20 m [1–3].", "q-src", 5))
    assert re.findall(r'href="#q-src-(\d)"', html) == ["1", "2", "3"]
    assert [int(n) for n in re.findall(r'href="#q-src-(\d)"', html)] == cited_numbers("Breite 1,20 m [1–3].", 5)
    assert "[<a" in html and "</a>]." in html
    # numbers without a shown source stay plain text; other bracket forms are linked too
    html = str(cite_links("Siehe [2, 9] und [Quelle 4].", "q-src", 6))
    assert re.findall(r'href="#q-src-(\d)"', html) == ["2", "4"] and ", 9]" in html
    # text is escaped
    assert "&lt;b&gt;" in str(cite_links("<b>[1]</b>", "q-src", 1))


# --------------------------------------------------------------------------- A2 / DEMO-3 / spec-04 / DEMO-11
def test_answered_shows_cited_sources_and_collapses_the_rest():
    ctx = [_src(n) for n in range(1, 6)]
    html = _render_answer(_result("answered", "Die Betriebsdauer beträgt 8 Stunden [3].", sources=[ctx[2]],
                                  context=ctx))
    quellen = _section(html, "<h3>Quellen</h3>")
    assert 'id="q-src-3" value="3"' in quellen and "[3]" in quellen and "q-src-1" not in quellen
    more = _section(html, '<details class="more-sources">', "</details>")
    assert "Weitere Fundstellen (4, in der Antwort nicht zitiert)" in more
    assert all(f'id="q-src-{n}"' in more for n in (1, 2, 4, 5))
    assert 'href="#q-src-3"' in html and "Mögliche Fundstellen" not in html
    assert "Alle Treffer in der Suche ansehen" in html


def test_no_citation_is_marked_and_shows_possible_locations():
    ctx = [_src(n) for n in range(1, 6)]
    html = _render_answer(_result("no_citation", "Das Notlicht muss 3 Stunden halten [9].", context=ctx,
                                  notes=["Die Antwort nennt keine Quelle (unbelegt)."]))
    assert "Antwort ohne Quellenangabe – bitte prüfen" in html
    assert 'class="answer-text unverified"' in html and 'href="#q-src-9"' not in html
    assert "<h3>Quellen</h3>" not in html
    possible = _section(html, "<h3>Mögliche Fundstellen</h3>")
    assert [int(n) for n in re.findall(r'id="q-src-(\d)"', possible)] == [1, 2, 3]
    assert "Weitere Fundstellen (2)" in html
    assert ('href="/suche?q=Wie+lange+muss+das+Notlicht+funktionieren%3F&amp;project=BS-1&amp;weak=1"' in html
            and "In der Suche öffnen" in html)


def test_legacy_no_citation_sources_are_not_presented_as_evidence():
    ctx = [_src(n) for n in range(1, 4)]
    html = _render_answer(_result("no_citation", "Antwort ohne Zitat.", sources=ctx, context=ctx))
    assert "<h3>Quellen</h3>" not in html and "Mögliche Fundstellen" in html


@pytest.mark.parametrize("status, answer", [
    ("not_found", "Dazu habe ich in den Unterlagen nichts gefunden."),
    ("llm_unavailable", "KI-Dienst nicht erreichbar – hier die gefundenen Stellen zum Nachlesen."),
    ("truncated", "Das Sprachmodell hat keine vollständige Antwort geliefert."),
])
def test_miss_statuses_show_top3_context_and_search_link(status, answer):
    ctx = [_src(n) for n in range(1, 7)]
    html = _render_answer(_result(status, answer, context=ctx))
    assert answer in html and f'class="answer {status}"' in html
    possible = _section(html, "<h3>Mögliche Fundstellen</h3>")
    assert re.findall(r'id="q-src-(\d)"', possible) == ["1", "2", "3"]
    assert 'href="/dokumente/11#c31"' in possible  # linked to the passage
    assert "Weitere Fundstellen (3)" in html and "In der Suche öffnen" in html
    if status == "truncated":
        assert "Antwort unvollständig" in html


def test_not_found_without_context_still_offers_the_search():
    html = _render_answer(_result("not_found", "Dazu habe ich in den Unterlagen nichts gefunden."))
    assert "Mögliche Fundstellen" not in html and "In der Suche öffnen" in html


def test_truncated_partial_answer_keeps_its_cited_sources():
    ctx = [_src(n) for n in range(1, 6)]
    html = _render_answer(_result("truncated", "Laut Genehmigung 8 Stunden [2] …", sources=[ctx[1]], context=ctx))
    assert "Antwort unvollständig" in html and 'href="#q-src-2"' in html
    assert re.findall(r'id="q-src-(\d)"', _section(html, "<h3>Quellen</h3>")) == ["2"]
    assert re.findall(r'id="q-src-(\d)"', _section(html, "<h3>Mögliche Fundstellen</h3>")) == ["1", "3", "4"]
    assert "Weitere Fundstellen (1, in der Antwort nicht zitiert)" in html and "In der Suche öffnen" in html


def test_answer_view_contract():
    ctx = [_src(n) for n in range(1, 6)]
    v = answer_view(_result("not_found", "x", context=ctx))
    assert [s["n"] for s in v["possible"]] == [1, 2, 3] and [s["n"] for s in v["more"]] == [4, 5]
    assert v["message_only"] and v["search_prominent"] and not v["cited"]
    v = answer_view(_result("answered", "x [4] [1]", sources=[ctx[3], ctx[0]], context=ctx))
    assert [s["n"] for s in v["cited"]] == [1, 4] and not v["possible"] and not v["search_prominent"]
    # also works on the JSON form of a result (AskResult.to_dict())
    assert answer_view(_result("no_citation", "x", context=ctx).to_dict())["title"].startswith("Antwort ohne")


def test_not_found_in_the_app_shows_possible_locations_and_search_link(ro):
    data = {"question": "Welche Kantinenpreise gelten im Sommer?", "project": "BS-2024-0137"}
    for headers in (HX, HTML):
        html = ro.post("/fragen", data=data, headers=headers).text
        assert "nichts gefunden" in html and "Mögliche Fundstellen" in html
        assert re.search(r'<li id="q-src-1" value="1">.*?href="/(dokumente|mails)/\d+', html, re.S)
        assert ('href="/suche?q=Welche+Kantinenpreise+gelten+im+Sommer%3F&amp;project=BS-2024-0137&amp;weak=1"'
                in html)


def test_llm_down_shows_found_passages(demo):
    client = TestClient(create_app(replace(demo, llm_url=f"{CLOSED_URL}/v1"), background=False))
    html = client.post("/fragen", data={"question": "Was fordert Auflage 7 der Baugenehmigung?",
                                        "project": "BS-2024-0137"}, headers=HX).text
    assert 'class="answer llm_unavailable"' in html and "Mögliche Fundstellen" in html
    assert "Baugenehmigung.pdf" in html and "In der Suche öffnen" in html


# --------------------------------------------------------------------------- DEMO-4 search
def test_search_hides_weak_hits_and_offers_them(ro):
    html = ro.get("/suche", params={"q": "xyzzy"}).text
    assert "Keine passenden Treffer für „xyzzy“" in html and '<ol class="hits">' not in html
    assert 'href="/suche?q=xyzzy&amp;weak=1"' in html and "trotzdem schwache Treffer zeigen" in html
    weak = ro.get("/suche", params={"q": "xyzzy", "weak": "1"}).text
    assert '<ol class="hits weak">' in weak and weak.count('<li class="weak">') > 0
    assert "schwach passend" in weak and "Wieder ausblenden" in weak


def test_search_shows_matching_hits_with_highlight(ro):
    html = ro.get("/suche", params={"q": "Löschwasserteich"}).text
    hits = _section(html, '<ol class="hits">')
    assert "Industriehalle Nordring" in hits and "<mark>Löschwasserteich</mark>" in hits
    assert "passende Treffer" in html


def test_search_threshold_comes_from_settings(demo):
    client = TestClient(create_app(replace(demo, search_min_rerank=0.0), background=False))
    assert '<ol class="hits">' in client.get("/suche", params={"q": "xyzzy"}).text


def test_search_without_reranker_hides_nothing(demo):
    client = TestClient(create_app(replace(demo, rerank_url=f"{CLOSED_URL}/rerank"), background=False))
    html = client.get("/suche", params={"q": "xyzzy"}).text
    assert "Reranker nicht erreichbar" in html and '<ol class="hits">' in html


def test_search_view_and_highlight_units():
    class H:
        def __init__(self, r):
            self.rerank = r

    class R:
        def __init__(self, reranked):
            self.reranked, self.hits = reranked, [H(0.9), H(0.04), H(None)]

    v = search_view(R(True), min_score=0.05, show_weak=False)
    assert [h.rerank for h in v["strong"]] == [0.9] and len(v["weak"]) == 2
    assert len(search_view(R(False), min_score=0.05, show_weak=False)["strong"]) == 3
    out = str(highlight("Zwei Rettungswege <b>über</b> die Treppe.", "Welche Rettungsweg gibt es?"))
    assert "<mark>Rettungswege</mark>" in out and "&lt;b&gt;" in out and "<mark>gibt" not in out


# --------------------------------------------------------------------------- DEMO-7 inbox actions
def _badge(html: str) -> str | None:
    m = re.search(r'<span id="inbox-badge"[^>]*hx-swap-oob="true"[^>]*>(\d*)</span>', html)
    return m.group(1) if m else None


def _card_ids(html: str) -> list[str]:
    return re.findall(r'<article class="card inbox-item" id="mail-(\d+)"', html)


def test_inbox_actions_update_badge_and_keep_rejected_mail_actionable(rw):
    page = rw.get("/eingang").text
    count = int(re.search(r'id="inbox-badge" class="badge">(\d+)<', page).group(1))
    suggested = re.findall(r'id="mail-(\d+)">(?:(?!</article>).)*?value="confirm"', page, re.S)
    n_suggested = int(re.search(r'id="inbox-count-suggested">(\d+)<', page).group(1))
    r = rw.post(f"/eingang/{suggested[0]}", data={"action": "confirm"}, headers=HX).text
    assert 'class="card inbox-item done"' in r and "Zugeordnet zu" in r and _badge(r) == str(count - 1)
    # the filter counts are updated out of band as well
    assert f'id="inbox-count-suggested" hx-swap-oob="true">{n_suggested - 1}<' in r
    assert f'id="inbox-count-all" hx-swap-oob="true">{count - 1}<' in r
    # reject: neutral card that still has the buttons, the mail stays in the inbox
    r = rw.post(f"/eingang/{suggested[1]}", data={"action": "reject"}, headers=HX).text
    assert "inbox-item done" not in r and 'class="card inbox-item"' in r
    assert 'value="assign"' in r and 'value="none"' in r and 'value="confirm"' not in r
    assert 'class="note info"' in r and "Vorschlag verworfen" in r and _badge(r) == str(count - 1)
    # a failed decision is not green either and keeps the buttons
    r = rw.post(f"/eingang/{suggested[1]}", data={"action": "assign", "project": "BS-0000-0000"}, headers=HX).text
    assert "inbox-item done" not in r and 'class="note warn"' in r and "Unbekanntes Projekt" in r
    # assign by name (free-text field with the shared project list), then "no project"
    r = rw.post(f"/eingang/{suggested[1]}", data={"action": "assign", "project": "Hotel Seeblick"}, headers=HX).text
    assert "BS-2024-0233" in r and "inbox-item done" in r and _badge(r) == str(count - 2)
    r = rw.post(f"/eingang/{suggested[2]}", data={"action": "none"}, headers=HX).text
    assert "ohne Projektbezug" in r and _badge(r) == str(count - 3)
    assert f'class="badge">{count - 3}<' in rw.get("/eingang").text


def test_inbox_without_javascript_redirects_back_or_explains_the_error(rw):
    ids = _card_ids(rw.get("/eingang").text)
    r = rw.post(f"/eingang/{ids[0]}", data={"action": "none", "back": "/eingang?page=2"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/eingang?page=2"
    for evil in ("//evil.example", "/\\evil.example", "https://evil.example"):
        r = rw.post(f"/eingang/{ids[1]}", data={"action": "reject", "back": evil}, follow_redirects=False)
        assert r.headers["location"] == "/eingang"
    r = rw.post(f"/eingang/{ids[2]}", data={"action": "assign", "project": "zzz", "back": "/eingang"},
                headers=HTML)
    assert r.status_code == 400 and "Unbekanntes Projekt: zzz" in r.text and 'href="/eingang"' in r.text


def test_resolve_project_input(demo):
    with db.session(demo.db_path) as conn:
        assert resolve_project_input(conn, "bs-2024-0233") == "BS-2024-0233"
        assert resolve_project_input(conn, "BS-2024-0233 Hotel Seeblick") == "BS-2024-0233"
        assert resolve_project_input(conn, "seeblick") == "BS-2024-0233"
        assert resolve_project_input(conn, "Gibt es nicht") == "Gibt es nicht"  # decide() rejects it
        with pytest.raises(Exception, match="Mehrere Projekte"):
            resolve_project_input(conn, "BS-2024")
    assert safe_back("/eingang?status=suggested") == "/eingang?status=suggested"
    assert safe_back("/\\x") == safe_back("//x") == safe_back("javascript:x") == "/eingang"


# --------------------------------------------------------------------------- database busy
@pytest.fixture()
def short_busy_timeout(monkeypatch):
    original = db.connect

    def connect(path):
        conn = original(path)
        conn.execute("PRAGMA busy_timeout = 100")
        return conn

    monkeypatch.setattr(db, "connect", connect)


def test_decisions_while_the_database_is_locked(rw, short_busy_timeout):
    s = rw.app.state.settings
    ids = _card_ids(rw.get("/eingang").text)
    locker = sqlite3.connect(str(s.db_path), isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")  # holds the write lock like a long indexing transaction
    try:
        r = rw.post(f"/eingang/{ids[0]}", data={"action": "none"}, headers=HX)
        assert r.status_code == 200 and "Datenbank ist gerade belegt" in r.text
        assert 'class="note warn"' in r.text and 'value="none"' in r.text  # still actionable
        r = rw.post(f"/eingang/{ids[0]}", data={"action": "none"}, headers=HTML)
        assert r.status_code == 503 and r.headers["retry-after"] == "5" and "erneut versuchen" in r.text
        r = rw.post(f"/api/mails/{ids[0]}/assign", json={"action": "none"})
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
        assert "erneut versuchen" in r.json()["detail"]
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert rw.post(f"/eingang/{ids[0]}", data={"action": "none"}, headers=HX).status_code == 200


def test_other_database_errors_are_not_hidden(rw, monkeypatch):
    def broken(*_a, **_k):
        raise sqlite3.OperationalError("no such table: assign_decisions")

    monkeypatch.setattr("projektakte.web.decide", broken)
    ids = _card_ids(rw.get("/eingang").text)
    with pytest.raises(sqlite3.OperationalError):
        rw.post(f"/eingang/{ids[0]}", data={"action": "none"}, headers=HX)


# --------------------------------------------------------------------------- ops-02 paging
def _add_inbox_mails(s, n: int) -> None:
    with db.session(s.db_path) as conn:
        conn.executemany(
            "INSERT INTO mails(source, source_key, subject, sender, date, preview, assign_status) "
            "VALUES('eml', ?, ?, 'x@example.org', ?, 'Text', 'unassigned')",
            [(f"eml:test-{i}", f"Testmail {i:03d}", f"2020-01-01T{i // 60:02d}:{i % 60:02d}:00+00:00")
             for i in range(n)])


def test_inbox_is_paged_filtered_and_has_one_project_list(rw):
    s = rw.app.state.settings
    before = rw.get("/api/inbox").json()["total"]
    _add_inbox_mails(s, 120)
    total = before + 120
    page = rw.get("/eingang").text
    assert len(_card_ids(page)) == 50 and f"1–50 von {total} Mails" in page
    assert page.count("<datalist") == 1 and "<select" not in page.split("<main>")[1]
    n_projects = len(rw.get("/api/projects").json()["projects"])
    assert page.split("<main>")[1].count("<option") == n_projects
    assert re.search(r'href="/eingang\?ab=[^"]+" rel="next">Weiter', page) and 'href="/eingang?ende=1"' in page
    # suggestions first, then the newest
    statuses = [m["assign_status"] for m in rw.get("/api/inbox").json()["mails"]]
    assert statuses == sorted(statuses, key=lambda st: st != "suggested")
    last = rw.get("/eingang", params={"ende": "1"}).text
    assert len(_card_ids(last)) == 50 and "Testmail 000" in last and f"{total - 49}–{total} von {total}" in last
    # old page numbers still work
    old = rw.get("/eingang", params={"page": "999"}).text
    assert len(_card_ids(old)) == total - 50 * (-(-total // 50) - 1) and "Testmail 000" in old
    assert len(_card_ids(rw.get("/eingang", params={"page": "abc"}).text)) == 50
    assert len(_card_ids(rw.get("/eingang", params={"ab": "kaputt", "vor": "1~x"}).text)) == 50
    # filter
    sugg = rw.get("/eingang", params={"status": "suggested"}).text
    assert 'class="active" aria-current="page">Mit Vorschlag' in sugg and "Testmail" not in sugg
    assert all('value="confirm"' in a for a in re.findall(r"<article.*?</article>", sugg, re.S))
    # the non-JS forms return to the same page (its first mail as cursor)
    first = rw.get("/eingang", params={"status": "unassigned"}).text
    nxt = re.search(r'href="(/eingang\?status=unassigned&amp;ab=[^"]+)" rel="next"', first).group(1)
    p2 = rw.get(nxt.replace("&amp;", "&")).text
    assert f'name="back" value="{nxt}"' in p2
    # the page stays small: no project list per card
    assert len(page) < 120_000


def test_api_inbox_paging(rw):
    _add_inbox_mails(rw.app.state.settings, 30)
    full = rw.get("/api/inbox").json()
    assert full["total"] == full["count"] >= 30 and "suggested_project_name" in full["mails"][0]
    part = rw.get("/api/inbox", params={"limit": 10, "offset": 5}).json()
    assert part["count"] == 10 and part["total"] == full["total"]
    assert [m["id"] for m in part["mails"]] == [m["id"] for m in full["mails"][5:15]]
    only = rw.get("/api/inbox", params={"status": "unassigned"}).json()
    assert {m["assign_status"] for m in only["mails"]} == {"unassigned"}
    assert rw.get("/api/inbox", params={"status": "assigned"}).status_code == 400


# --------------------------------------------------------------------------- DEMO-9 attachments
def test_project_page_lists_mail_attachments(ro):
    html = ro.get("/projekte/BS-2024-0137", params={"tab": "anhaenge"}).text
    section = _section(html, '<div id="inhalt">', "</table>")
    assert "Anhänge aus Mails" in section
    doc_id = re.search(r'<a href="/dokumente/(\d+)" data-betrachter="/dokumente/\1/ansicht">Stellungnahme Feuerwehr.pdf</a>',
                       section).group(1)
    mail_id = re.search(r'<a href="/mails/(\d+)">Stellungnahme Brandschutzdienststelle', section).group(1)
    assert re.search(r'<span class="muted">\d\d\.\d\d\.\d{4}</span>', section)
    assert ro.get(f"/dokumente/{doc_id}").status_code == 200
    # chosen in the timeline, the mail names its attachment
    page = ro.get("/projekte/BS-2024-0137", params={"auswahl": f"m{mail_id}"}).text
    card = _section(page, '<div id="auswahl"', "</section>")
    assert (f'<a href="/dokumente/{doc_id}" data-betrachter="/dokumente/{doc_id}/ansicht">Stellungnahme Feuerwehr.pdf'
            '</a>') in card
    assert "Stellungnahme Brandschutzdienststelle" in card


# --------------------------------------------------------------------------- BUG-11 local dates
def test_dates_use_local_time():
    assert de_date("2024-06-17T22:30:00+00:00") == "18.06.2024"
    assert de_date("2024-06-17T22:30:00+00:00", True) == "18.06.2024 00:30"
    assert de_date("2024-12-31T23:30:00Z") == "01.01.2025"
    assert de_date("2024-06-18") == "18.06.2024" and de_date(None) == "–" and de_date("kaputt") == "kaputt"
    assert str(local_day("2024-06-17T22:30:00+00:00")) == "2024-06-18"


def test_project_list_and_timeline_use_the_local_day(rw):
    s = rw.app.state.settings
    with db.session(s.db_path) as conn:
        conn.execute("INSERT INTO mails(source, source_key, subject, sender, date, project, assign_status) "
                     "VALUES('eml', 'eml:silvester', 'Silvester-Mail', 'x@example.org', "
                     "'2025-12-31T23:30:00+00:00', 'BS-2024-0137', 'assigned')")
    rows = rw.get("/", params={"q": "BS-2024-0137"}, headers=HX).text
    assert "01.01.2026" in rows and "31.12.2025" not in rows
    page = rw.get("/projekte/BS-2024-0137").text
    assert re.findall(r'<span class="d">([\d.]+)</span>', page)[0] == "01.01.26"  # the newest label of the timeline


# --------------------------------------------------------------------------- DEMO-10 status page
def _deps(ai_mode: str) -> str:
    deps = {"ai_mode": ai_mode, "services": {
        "llm": {"ok": True, "format": "openai-chat", "latency_ms": 7.83, "url": "http://127.0.0.1:1/v1",
                "model": "qwen3.8-flash-next"},
        "embeddings": {"ok": True, "format": "openai", "latency_ms": 1234.5, "url": "http://127.0.0.1:1",
                       "model": "qwen3-embedding-4b", "dim": 256},
        "reranker": {"ok": False, "format": None, "url": "http://127.0.0.1:1/rerank", "error": "weg",
                     "fallback": "Suche ohne Reranker"}},
        "plmail": {"ok": True, "format": "jmap", "latency_ms": 12.0, "url": "https://plmail", "accounts": 1}}
    return templates.get_template("_deps.html").render(deps=deps)


def test_deps_in_fake_mode_say_simulated_and_numbers_are_german():
    fake = _deps("fake")
    assert "simuliert" in fake and "qwen" not in fake and "127.0.0.1" not in fake
    assert "7,8" in fake and "1.234,5" in fake and "7.8" not in fake
    assert "1 Konto" in fake and "Konto/Konten" not in fake
    real = _deps("real")
    assert "qwen3.8-flash-next" in real and "simuliert" not in real


def test_status_page_with_fake_ai_mode(tmp_path):
    from projektakte.config import load_settings

    s = load_settings({"DATA_DIR": str(tmp_path), "AI_MODE": "fake", "AUTH_DISABLED": "1", "APP_ENV": "test",
                       "HEALTH_TIMEOUT": "2"})
    client = TestClient(create_app(s, background=False))
    part = client.get("/status/dienste").text
    assert "simuliert" in part and "qwen" not in part
    assert "KI-Dienste simuliert" in client.get("/status").text


# --------------------------------------------------------------------------- DEMO-13 wording
def test_german_numbers_and_plurals():
    assert de_num(1234) == "1.234" and de_num(7.83, 1) == "7,8" and de_num(None) == "–"
    assert plural(1, "Mail", "Mails") == "1 Mail" and plural(2, "Mail", "Mails") == "2 Mails"
    assert plural(0, "Seite", "Seiten") == "0 Seiten" and plural(1500, "Seite", "Seiten") == "1.500 Seiten"


def test_no_makeshift_plurals_in_templates_and_pills_do_not_wrap():
    for f in TEMPLATES.glob("*.html"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"\w\((s|n|e|en)\)|Konto/Konten", text), f.name
    css = (TEMPLATES.parent / "static" / "app.css").read_text(encoding="utf-8")
    pill = re.search(r"\.pill \{[^}]*\}", css).group(0)
    assert "white-space: nowrap" in pill and "text-overflow: ellipsis" in pill


def test_project_header_uses_proper_plurals(ro):
    html = ro.get("/projekte/BS-2024-0137").text
    assert "Mail(s)" not in html and re.search(r"\d+ Dokumente · \d+ Mails", html)
    assert re.search(r"\b1 Mail im Eingang vorgeschlagen|\b\d+ Mails im Eingang vorgeschlagen", html)


# --------------------------------------------------------------------------- SEC-07 banner
def test_banner_when_access_control_is_off(ro):
    html = ro.get("/").text
    assert "Zugangsschutz ist ausgeschaltet (nur Entwicklung)" in html
    # also on error pages rendered outside render()
    assert "Zugangsschutz ist ausgeschaltet" in ro.get("/projekte/BS-1999-0001", headers=HTML).text


def test_no_banner_when_logged_in(tmp_path, fake_ai):
    from projektakte.auth import COOKIE, hash_password

    users = f"anna:{hash_password('pw-12345678', iterations=1000)}"
    s = fake_settings(fake_ai, tmp_path, AUTH_DISABLED=0, AUTH_USERS=users, SECRET_KEY="k" * 48)
    app = create_app(s, background=False)
    client = TestClient(app)
    client.cookies.set(COOKIE, app.state.auth.make_cookie("anna"))
    html = client.get("/").text
    assert "Angemeldet: anna" in html and "Zugangsschutz ist ausgeschaltet" not in html


# --------------------------------------------------------------------------- CSP-safe templates
def test_templates_have_no_inline_code_or_styles():
    for f in TEMPLATES.glob("*.html"):
        if f.name == "login.html":  # owned by auth_web.py, checked there
            continue
        text = re.sub(r"\{#.*?#\}", "", f.read_text(encoding="utf-8"), flags=re.S)
        assert "<style" not in text and " style=" not in text, f.name
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), f.name
        assert not re.search(r"\son[a-z]+=|hx-on|javascript:", text), f.name


def test_pages_configure_htmx_for_the_csp(ro):
    html = ro.get("/").text
    assert '<meta name="htmx-config"' in html and '"includeIndicatorStyles": false' in html


def test_login_routes_moved_out_of_web_module():
    assert not [r for r in router.routes if getattr(r, "path", "") in ("/login", "/logout")]
