"""Round 5 of the verification (package "oberflaeche").

* A project number typed with spaces is joined only into the configured shape of a project number
  (PROJECT_NUMBER_PATTERN): norms, abbreviations with numbers, counts and a number that is written out
  already keep their spaces – and their project link, marks and literal hits.
* A compound part is only marked where the word really splits there ("Rauchableitung" does not mark
  "Leitung").
* Inbox paging: no address hides an open mail. A rejected card that an earlier page still shows and
  counts is shown again – marked – on the page it now sorts onto, without being counted twice; the
  ``gezeigt`` value is signed, so a crafted link neither marks cards nor moves the numbers.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from projektakte import db, web
from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.webtext import QueryMatcher, highlight, join_spaced_codes, matcher

from .conftest import fake_settings

HX = {"HX-Request": "true"}
CROSS_SITE = {"Sec-Fetch-Site": "cross-site"}
ORDER = "Vorschläge zuerst, dann die neuesten"
AGAIN = "dazu 1 Mail von einer vorigen Seite"
ONLY_AGAIN = "Auf dieser Seite nur noch 1 Mail von einer vorigen Seite"


def _indexed_demo(root, fake_ai) -> TestClient:
    generate(root / "demo")
    s = fake_settings(fake_ai, root, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    return TestClient(create_app(s, background=False))


@pytest.fixture(scope="module")
def ro(tmp_path_factory, fake_ai):
    return _indexed_demo(tmp_path_factory.mktemp("r5ui"), fake_ai)


@pytest.fixture()
def rw(tmp_path, fake_ai):
    return _indexed_demo(tmp_path, fake_ai)


def _marks(html) -> list[str]:
    return re.findall(r"<mark>(.*?)</mark>", str(html))


# --------------------------------------------------------------------------- project numbers typed with spaces
@pytest.mark.parametrize("query", [
    "BS-2024-0233 2025", "Mails BS-2024-0233 12 2024", "BS-2024-0233 Stand 2025",  # written out already
    "DIN VDE 0100-560", "DIN 14095 2007", "DIN EN 1838 2019", "Feuerwehrpläne DIN 14095 14096",  # norms
    "Prüfberichte BMA 2022 2023", "Wartung RWA 2023 2024", "KW 12 13", "EG 12 13",  # abbreviations
    "ca 30 40", "max 30 40", "je 10 20", "pro 10 20", "Seite 12 13", "ab 2023 2024", "2024 0233",  # counts
    "BS 2024", "BS 2024 02", "XY 2024 0233", "BA 412/2023",  # no project number of the configured shape
])
def test_ordinary_text_and_written_numbers_keep_their_spaces(query):
    """review: 'BS-2024-0233 2025' became 'BS-2024-0233-2025', 'DIN VDE 0100-560' 'DIN VDE-0100-560',
    'ca 30 40' 'ca-30-40' – project link, marks and literal hits were lost."""
    assert join_spaced_codes(query) == query


@pytest.mark.parametrize("query, joined", [
    ("BS 2024 0233", "BS-2024-0233"),
    ("bs 2023 0412 Brandwand", "bs-2023-0412 Brandwand"),
    ("BS2024 0233", "BS-2024-0233"),
    ("BS-2024 0233", "BS-2024-0233"),
    ("BS 2024-0233 und BS 2024 0311", "BS-2024-0233 und BS-2024-0311"),
    ("BS 2024 0233 2025", "BS-2024-0233 2025"),  # the number, then a year
    ("BS-2024-0233 oder BS 2024 0311", "BS-2024-0233 oder BS-2024-0311"),
])
def test_spaced_project_numbers_are_joined(query, joined):
    assert join_spaced_codes(query) == joined


def test_joining_follows_the_configured_number_shape():
    assert join_spaced_codes("Plan P 12345", r"P\d{5}") == "Plan P12345"
    assert join_spaced_codes("PRJ 24 117 Brandwand", r"PRJ-\d{2}/\d{3}") == "PRJ-24/117 Brandwand"
    assert join_spaced_codes("BS 2024 0233", r"P\d{5}") == "BS 2024 0233"  # not a number of this office
    assert join_spaced_codes("BS 2024 0233", "([") == "BS 2024 0233"  # unusable pattern: nothing joined
    assert QueryMatcher("P 12345", r"P\d{5}").strong_identifiers == [("p12345",)]


@pytest.mark.parametrize("text, query, expected, literal", [
    ("Brandschutzkonzept BS-2024-0233 Hotel Seeblick, Stand 2025", "BS-2024-0233 2025", ["BS-2024-0233", "2025"],
     True),
    ("Brandschutzkonzept BS-2024-0233 Hotel Seeblick, Stand 2025", "Mails BS-2024-0233 12 2024", ["BS-2024-0233"],
     True),
    ("Die Anlage entspricht DIN VDE 0100-560.", "DIN VDE 0100-560", ["DIN", "VDE", "0100-560"], True),
    ("Feuerwehrplan nach DIN 14095 2007 erstellt.", "DIN 14095 2007", ["DIN", "14095", "2007"], True),
    ("Feuerwehrpläne nach DIN 14095 und DIN 14096", "Feuerwehrpläne DIN 14095 14096",
     ["Feuerwehrpläne", "DIN", "14095", "DIN", "14096"], True),
    ("Wartung der RWA 2023 und 2024 erfolgt.", "Wartung RWA 2023 2024", ["Wartung", "RWA", "2023", "2024"], True),
    ("Abstand 30 bis 40 m", "ca 30 40", ["30", "40"], False),  # two digits are no identifier
    ("Brandschutzkonzept BS-2024-0233 vom 09.01.2024, Projekt 2024", "BS 2024 0233", ["BS-2024-0233"], True),
])
def test_marks_and_literal_hits_of_numbers_in_a_query(text, query, expected, literal):
    assert _marks(highlight(text, query)) == expected
    assert matcher(query).has_identifier(text) is literal


def test_project_link_and_marks_for_a_written_number_with_more_numbers(ro):
    html = ro.get("/suche", params={"q": "BS-2024-0233 2025"}).text
    assert 'href="/projekte/BS-2024-0233">Zum Projekt BS-2024-0233 ' in html
    assert "<mark>BS-2024-0233</mark>" in html
    html = ro.get("/suche", params={"q": "Feuerwehrpläne DIN 14095 14096"}).text
    assert {"14095", "14096", "DIN"} <= set(_marks(html))
    with db.session(ro.app.state.settings.db_path) as conn:
        for query in ("BS-2024-0233 2025", "Mails BS-2024-0233 12 2024", "BS 2024 0233 2025"):
            assert [p["number"] for p in web.projects_named_in(conn, query)] == ["BS-2024-0233"], query


def test_template_filters_use_the_configured_number_shape():
    """The filters read PROJECT_NUMBER_PATTERN of the app that renders the page."""
    settings = SimpleNamespace(project_number_pattern=r"P\d{5}")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    template = web.templates.env.from_string("{{ t|highlight(q) }}|{{ passage('', t, q) }}")
    out = template.render(request=request, t="Plan P12345 vom Mai", q="P 12345")
    assert out.startswith("Plan <mark>P12345</mark> vom Mai|")
    assert web.templates.env.from_string("{{ t|highlight(q) }}").render(t="Plan P12345", q="P 12345") == "Plan P12345"


# --------------------------------------------------------------------------- compound parts
def test_question_does_not_mark_a_word_that_only_ends_like_its_compound():
    """retest: 'Welche Auflagen zur Rauchableitung …' marked 'Leitung' in '… im Büro der Leitung auslösen'."""
    text = ("Auflage 5: Rauchwarnmelder lösen zusätzlich einen Alarm im Büro der Leitung aus. Auflage 4: "
            "Öffnung zur Rauchableitung.")
    question = "Welche Auflagen zur Rauchableitung stehen in der Baugenehmigung?"
    assert _marks(highlight(text, question)) == ["Auflage", "Auflage", "Rauchableitung"]


@pytest.mark.parametrize("query, word, matches", [
    # the text names the head of a compound of the query
    ("Rauchableitung", "Leitung", False),  # Rauch-Ableitung
    ("Brandabschnitt", "Schnitt", False),  # Brand-Abschnitt
    ("Treppenhausbelüftung", "Lüftung", False),  # Treppenhaus-Belüftung
    ("Treppenhausbelüftung", "Belüftung", True),
    ("ausgeführt", "geführt", False),  # a derivation, no compound
    ("stattfindet", "findet", False),
    ("Baubesprechung", "Besprechung", True),
    ("Hochhausrichtlinie", "Richtlinie", True),  # Hoch-haus ends like the prefix "aus"
    ("Feuerlöscherwartung", "Wartung", True),  # Feuer-löscher ends like "er"
    ("Rettungswegkennzeichnung", "Kennzeichnung", True),
    ("Steigleitung", "Leitung", True),
    ("Übergabeprotokoll", "Protokoll", True),
    ("Probebetrieb", "Betrieb", True),
    # the query names the head of a compound of the text
    ("Leitung", "Rauchableitung", False),
    ("Leitung", "Ableitung", False),
    ("Fahrt", "Zufahrt", False),
    ("Leitung", "Steigleitung", True),
    ("Genehmigung", "Baugenehmigung", True),
    ("Prüfung", "Abnahmeprüfung", True),
])
def test_compound_parts_are_marked_only_where_the_word_splits(query, word, matches):
    assert QueryMatcher(query).word_matches(word) is matches


def test_project_question_does_not_mark_leitung(ro):
    data = {"question": "Welche Auflagen zur Rauchableitung stehen in der Baugenehmigung?", "project": "BS-2025-0019"}
    html = ro.post("/fragen", data=data).text
    assert "Büro der Leitung" in html and "<mark>Rauchableitung</mark>" in html
    assert "<mark>Leitung</mark>" not in html


# --------------------------------------------------------------------------- inbox: carried cards
def _fill_inbox(client, n_suggested: int, n_open: int) -> None:
    """Only these mails in the inbox: suggestions with old dates (rejected, they sort far back)."""
    with db.session(client.app.state.settings.db_path) as conn:
        conn.execute("UPDATE mails SET assign_status = 'none' WHERE assign_status IN ('suggested', 'unassigned')")
        rows = [(f"eml:r5s-{i}", f"Vorschlag {i:03d}", f"2019-01-01T{i // 60:02d}:{i % 60:02d}:00+00:00", "suggested",
                 "BS-2024-0137") for i in range(n_suggested)]
        rows += [(f"eml:r5o-{i}", f"Offen {i:03d}", f"2021-01-01T{i // 60:02d}:{i % 60:02d}:00+00:00", "unassigned",
                  None) for i in range(n_open)]
        conn.executemany(
            "INSERT INTO mails(source, source_key, subject, sender, date, preview, assign_status, suggested_project) "
            "VALUES('eml', ?, ?, 'x@example.org', ?, 'Text', ?, ?)", rows)


def _cards(page: str) -> list[tuple[int, bool]]:
    """(id, shown once more) of the cards of a page."""
    return [(int(i), "repeated" in cls.split())
            for cls, i in re.findall(r'<article class="(card inbox-item[^"]*)" id="mail-(\d+)"', page)]


def _ids(page: str) -> list[int]:
    return [i for i, _ in _cards(page)]


def _counted(page: str) -> list[int]:
    return [i for i, again in _cards(page) if not again]


def _repeated(page: str) -> list[int]:
    return [i for i, again in _cards(page) if again]


def _page_state(page: str) -> dict:
    return json.loads(html_lib.unescape(re.search(r"hx-vals='([^']+)'", page).group(1)))


def _summary(html: str) -> str:
    m = re.search(r'<p class="muted small" id="inbox-summary"[^>]*>(.*?)</p>', html, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def _link(page: str, rel: str) -> str | None:
    m = re.search(rf'href="([^"]+)" rel="{rel}"', page)
    return html_lib.unescape(m.group(1)) if m else None


def _post(client, mail_id: int, page: str, action: str, **extra) -> str:
    data = {"action": action, "back": "/eingang", **_page_state(page), **extra}
    r = client.post(f"/eingang/{mail_id}", data=data, headers=HX)
    assert r.status_code == 200
    return r.text


def _open_ids(client, status: str = "") -> list[int]:
    return [m["id"] for m in client.get("/api/inbox", params={"status": status} if status else {}).json()["mails"]]


def test_a_crafted_gezeigt_link_hides_no_mail_and_moves_no_number(rw):
    """review: '/eingang?gezeigt=70,64,…' (a cross-site GET) showed one card of 15 and '15–15 von 15 Mails';
    '?status=suggested&gezeigt=…' claimed '8–13 von 13 Mails' for a filter of 6."""
    _fill_inbox(rw, 8, 7)
    plain = rw.get("/eingang").text
    ids = _ids(plain)
    assert len(ids) == 15 and _summary(plain) == "15 Mails · Vorschläge zuerst, dann die neuesten"
    others = ",".join(map(str, ids[1:]))
    for url in (f"/eingang?gezeigt={others}", f"/eingang?gezeigt={others}.AAAAAAAAAAAAAAAAAAAAAA"):
        page = rw.get(url, headers=CROSS_SITE)
        assert page.status_code == 200
        assert _ids(page.text) == ids and not _repeated(page.text) and _summary(page.text) == _summary(plain)
    unassigned = ",".join(map(str, _open_ids(rw, "unassigned")))
    page = rw.get(f"/eingang?status=suggested&gezeigt={unassigned}", headers=CROSS_SITE).text
    assert _summary(page) == "8 Mails · Vorschläge zuerst, dann die neuesten" and len(_ids(page)) == 8
    # with a cursor, too: only the application's own (signed) value counts
    page = rw.get(f"/eingang?ab=1~999999~2099-01-01&gezeigt={others}", headers=CROSS_SITE).text
    assert _ids(page) == ids and not _repeated(page) and _summary(page) == _summary(plain)


@pytest.mark.parametrize("n_open", [60, 140])  # the rejected mail sorts onto page 2 / page 3
def test_rejected_card_is_shown_again_marked_and_counted_once(rw, n_open):
    _fill_inbox(rw, 10, n_open)
    total = 10 + n_open
    page1 = rw.get("/eingang").text
    ids = _ids(page1)
    _post(rw, ids[0], page1, "reject")
    for mid in ids[1:13]:
        r = _post(rw, mid, page1, "none")
    r = _post(rw, ids[13], page1, "assign", project="BS-2024-0137")
    left = total - 13
    assert _summary(r).startswith(f"1–37 von {left} Mails"), _summary(r)
    url, start, counted, repeated_on = _link(r, "next"), 38, [ids[0]] + ids[14:], []
    assert re.search(rf"gezeigt={ids[0]}\.[\w-]{{22}}$", url), url  # signed
    while url:
        page = rw.get(url).text
        cards = _counted(page)
        if _repeated(page):
            repeated_on.append(len(repeated_on))
            assert _repeated(page) == [ids[0]] and "Bereits auf einer vorigen Seite gezeigt" in page
        if cards:
            assert _summary(page).startswith(f"{start}–{start + len(cards) - 1} von {left} Mails"), _summary(page)
            assert _summary(page).endswith(f"· {AGAIN}") is bool(_repeated(page))
        else:  # n_open=140: the rejected mail sorts behind the 150th place – a page of its own
            assert _summary(page) == f"{ONLY_AGAIN} · noch {left} Mails im Eingang"
        counted += cards
        start += len(cards)
        url = _link(page, "next")
    assert start - 1 == left and repeated_on == [0], "shown again exactly once, on the page it sorts onto"
    assert sorted(counted) == sorted(_open_ids(rw)) and len(counted) == len(set(counted)), \
        "nothing skipped, nothing counted twice"


def test_weiter_after_the_rest_was_decided_shows_the_open_card(rw):
    """review: on page 1 one card rejected, the other 49 decided, the first mail of page 2 decided
    elsewhere – the renewed "Weiter" link showed no card and 'Alles zugeordnet – der Eingang ist leer.'
    while the rejected mail was still open."""
    _fill_inbox(rw, 51, 0)
    page1 = rw.get("/eingang").text
    ids = _ids(page1)
    _post(rw, ids[0], page1, "reject")
    for mid in ids[1:]:
        r = _post(rw, mid, page1, "none")
    nxt = _link(r, "next")
    with db.session(rw.app.state.settings.db_path) as conn:  # another user decides the 51st mail
        conn.execute("UPDATE mails SET assign_status = 'none' WHERE assign_status = 'suggested'")
    assert _open_ids(rw) == [ids[0]]
    for url in (nxt, re.sub(r"ab=[^&]+", "ab=0~1~2000-01-01", nxt)):  # also: cursor behind the last mail
        page = rw.get(url).text
        assert _cards(page) == [(ids[0], True)], url
        assert "der Eingang ist leer" not in page
        assert _summary(page) == f"{ONLY_AGAIN} · noch 1 Mail im Eingang"
        assert 'id="inbox-pager-info">nur Mails einer vorigen Seite</span>' in page
    # deciding it here empties the inbox
    page = rw.get(nxt).text
    r = _post(rw, ids[0], page, "none")
    assert "✓" in r and _summary(r).startswith("Alle Mails dieser Seite sind bearbeitet · noch 0 Mails")


def test_decisions_on_a_page_with_a_card_shown_again(rw):
    _fill_inbox(rw, 10, 60)
    page1 = rw.get("/eingang").text
    ids = _ids(page1)
    _post(rw, ids[0], page1, "reject")
    for mid in ids[1:5]:
        r = _post(rw, mid, page1, "none")
    page2 = rw.get(_link(r, "next")).text
    assert _repeated(page2) == [ids[0]] and len(_counted(page2)) == 20
    assert _summary(page2) == f"47–66 von 66 Mails · {ORDER} · {AGAIN}"
    state = _page_state(page2)
    assert state["seite_ids"] == ",".join(map(str, _counted(page2))) and state["seite_wieder"] == str(ids[0])
    # a counted card decided: the card shown again is still named, not counted
    r = _post(rw, _counted(page2)[0], page2, "none")
    assert _summary(r) == f"47–65 von 65 Mails · {ORDER} · {AGAIN}"
    # a failed decision keeps the card's mark
    r = _post(rw, ids[0], page2, "assign", project="gibt-es-nicht")
    assert re.search(rf'<article class="card inbox-item repeated" id="mail-{ids[0]}"', r)
    # the card shown again decided here: everything after page 1 moves up by one
    r = _post(rw, ids[0], page2, "none")
    assert _summary(r) == f"46–64 von 64 Mails · {ORDER}", _summary(r)


def test_without_javascript_the_page_comes_back_with_the_card_marked(rw):
    _fill_inbox(rw, 10, 60)
    page1 = rw.get("/eingang").text
    ids = _ids(page1)
    _post(rw, ids[0], page1, "reject")
    for mid in ids[1:5]:
        r = _post(rw, mid, page1, "none")
    page2 = rw.get(_link(r, "next")).text
    back = html_lib.unescape(re.search(r'name="back" value="([^"]+)"', page2).group(1))
    assert re.search(rf"gezeigt={ids[0]}\.[\w-]{{22}}", back)
    r = rw.post(f"/eingang/{_counted(page2)[0]}", data={"action": "none", "back": back}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == back
    again = rw.get(back).text
    assert _repeated(again) == [ids[0]] and _summary(again).startswith("47–65 von 65 Mails"), _summary(again)


def test_signed_value_of_another_filter_is_ignored(rw):
    _fill_inbox(rw, 10, 20)
    page1 = rw.get("/eingang").text
    ids = _ids(page1)
    _post(rw, ids[0], page1, "reject")
    request = SimpleNamespace(app=rw.app)
    signed = web.sign_carried(request, "", [ids[0]])
    assert web.read_carried(request, "", signed) == [ids[0]]
    assert web.read_carried(request, "unassigned", signed) == []
    assert web.read_carried(request, "", signed.replace(str(ids[0]), str(ids[1]), 1)) == []
    page = rw.get(f"/eingang?status=unassigned&ab=0~999999~2099-01-01&gezeigt={signed}").text
    assert ids[0] in _ids(page) and not _repeated(page)
    assert _summary(page) == "21 Mails · Vorschläge zuerst, dann die neuesten", _summary(page)


def test_an_empty_page_never_claims_the_inbox_is_empty(rw, monkeypatch):
    """If the mails of a page are decided while it is read, the page says what is left."""
    _fill_inbox(rw, 3, 2)
    monkeypatch.setattr(web, "_inbox_page_rows", lambda *a, **k: [])
    page = rw.get("/eingang").text
    assert "der Eingang ist leer" not in page
    assert "Auf dieser Seite ist keine offene Mail mehr – noch 5 Mails im Eingang." in page
