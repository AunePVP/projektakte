"""Round 4 of the verification (package "oberflaeche"): a project number typed with spaces
("BS 2024 0233") links the project and is marked as a whole like "BS-2024-0233", and a rejected
suggestion that stays on its inbox page is not counted again on the next page. Since round 5 the next
page shows it once more where it now sorts – marked as already shown and counted ("repeated") – so no
page hides an open mail; the counts stay the same."""
from __future__ import annotations

import html as html_lib
import json
import re

import pytest
from fastapi.testclient import TestClient

from projektakte import db
from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.webtext import QueryMatcher, highlight, join_spaced_codes

from .conftest import fake_settings

HX = {"HX-Request": "true"}


def _indexed_demo(root, fake_ai) -> TestClient:
    generate(root / "demo")
    s = fake_settings(fake_ai, root, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    return TestClient(create_app(s, background=False))


@pytest.fixture(scope="module")
def ro(tmp_path_factory, fake_ai):
    return _indexed_demo(tmp_path_factory.mktemp("r4ui"), fake_ai)


@pytest.fixture()
def rw(tmp_path, fake_ai):
    return _indexed_demo(tmp_path, fake_ai)


# --------------------------------------------------------------------------- project number with spaces
def _hits(html: str) -> str:
    m = re.search(r'<ol class="hits">(.*?)</ol>', html, re.S)
    return m.group(1) if m else ""


@pytest.mark.parametrize("query, number", [
    ("BS 2024 0233", "BS-2024-0233"),
    ("bs 2023 0412", "BS-2023-0412"),
    ("BS2024 0233", "BS-2024-0233"),
    ("BS 2024-0233", "BS-2024-0233"),
    ("Brandschutzkonzept BS 2024 0233", "BS-2024-0233"),
])
def test_project_number_with_spaces_links_the_project_and_marks_it_as_a_whole(ro, query, number):
    """verify4: "BS 2024 0233" found the hits but showed no "Zum Projekt" button, marked "2024" inside
    "BS-2024-0233" (not "0233") and took "BS" for Brandschutz (Brandschutzkonzept, -nachweis …)."""
    html = ro.get("/suche", params={"q": query}).text
    assert f'href="/projekte/{number}">Zum Projekt {number} ' in html
    hits = _hits(html)
    assert f"<mark>{number}</mark>" in hits
    marks = set(re.findall(r"<mark>(.*?)</mark>", hits))
    assert not {m for m in marks if re.fullmatch(r"\d+", m)}, marks  # no part of the number alone
    if "Brandschutzkonzept" not in query:
        assert not {m for m in marks if m.lower().startswith("brandschutz")}, marks
    assert f'value="{query}"' in html  # the search field keeps what was typed


def test_join_spaced_codes():
    assert join_spaced_codes("BS 2024 0233") == "BS-2024-0233"
    assert join_spaced_codes("bs 2023 0412 Brandwand") == "bs-2023-0412 Brandwand"
    assert join_spaced_codes("BS2024 0233") == "BS-2024-0233"
    assert join_spaced_codes("BS 2024-0233 und BS 2024 0311") == "BS-2024-0233 und BS-2024-0311"
    # only the configured shape of a project number is joined (round 5); a file number keeps its space
    for unchanged in ("BS-2024-0233", "BA-412/2023", "BA 412/2023", "DIN 4102", "DIN 4102 4", "Seite 12 13",
                      "ab 2023 2024", "Notlicht 8 Stunden", "2024 0233", "BS 2024", "T30 RS"):
        assert join_spaced_codes(unchanged) == unchanged


def test_highlight_of_a_number_typed_with_spaces():
    text = "Brandschutzkonzept BS-2024-0233 vom 09.01.2024, Projekt 2024"
    expected = "Brandschutzkonzept <mark>BS-2024-0233</mark> vom 09.01.2024, Projekt 2024"
    assert str(highlight(text, "BS 2024 0233")) == expected
    assert str(highlight(text, "bs 2024 0233")) == expected
    assert str(highlight(text, "BS-2024-0233")) == expected
    assert QueryMatcher("bs 2023 0412").strong_identifiers == [("bs", "2023", "0412")]
    assert QueryMatcher("Brandwand BS 2023 0777").has_identifier("Detail zu BS-2023-0777_Brandwand.pdf")
    assert "<mark>4102</mark>" in str(highlight("Bauteile nach DIN 4102", "DIN 4102"))  # unchanged


# --------------------------------------------------------------------------- inbox: rejected card and "Weiter"
def _fill_inbox(client, n_suggested: int, n_open: int) -> None:
    """Only these mails in the inbox: suggestions with old dates (rejected, they sort far back)."""
    with db.session(client.app.state.settings.db_path) as conn:
        conn.execute("UPDATE mails SET assign_status = 'none' WHERE assign_status IN ('suggested', 'unassigned')")
        rows = [(f"eml:r4s-{i}", f"Vorschlag {i:03d}", f"2019-01-01T{i // 60:02d}:{i % 60:02d}:00+00:00", "suggested",
                 "BS-2024-0137") for i in range(n_suggested)]
        rows += [(f"eml:r4o-{i}", f"Offen {i:03d}", f"2021-01-01T{i // 60:02d}:{i % 60:02d}:00+00:00", "unassigned",
                  None) for i in range(n_open)]
        conn.executemany(
            "INSERT INTO mails(source, source_key, subject, sender, date, preview, assign_status, suggested_project) "
            "VALUES('eml', ?, ?, 'x@example.org', ?, 'Text', ?, ?)", rows)


def _ids(page: str) -> list[int]:
    return [int(x) for x in re.findall(r'<article class="card inbox-item[^"]*" id="mail-(\d+)"', page)]


def _repeated(page: str) -> list[int]:
    """Cards an earlier page showed and counted already (round 5: shown again, marked, not counted)."""
    return [int(x) for x in re.findall(r'<article class="card inbox-item repeated" id="mail-(\d+)"', page)]


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


@pytest.mark.parametrize("n_open", [60, 140])  # the rejected mail moves to page 2 / to page 3
def test_rejected_card_is_counted_once_across_pages(rw, n_open):
    """verify4: 70 open mails; on page 1 one "Verwerfen", 12 × "Kein Projektbezug", one assignment →
    "1–37 von 57", but "Weiter" showed "37–57 von 57", counting the rejected mail a second time. Now
    the next pages go on with 38 and show the rejected mail once more, marked, outside the count."""
    _fill_inbox(rw, 10, n_open)
    total = 10 + n_open
    page1 = rw.get("/eingang").text
    ids = _ids(page1)
    assert _summary(page1).startswith(f"1–50 von {total} Mails") and len(ids) == 50
    stale_next = _link(page1, "next")
    r = _post(rw, ids[0], page1, "reject")
    assert "Vorschlag verworfen" in r
    for mid in ids[1:13]:
        r = _post(rw, mid, page1, "none")
    r = _post(rw, ids[13], page1, "assign", project="BS-2024-0137")
    left = total - 13
    assert _summary(r).startswith(f"1–37 von {left} Mails"), _summary(r)
    # the answer renews the "Weiter" link: it passes on the rejected card that still is on page 1
    nxt = _link(r, "next")
    assert 'id="inbox-next"' in r and nxt and f"gezeigt={ids[0]}" in nxt
    seen, url, start, pages, again = [ids[0]] + ids[14:], nxt, 38, [], []
    while url:
        page = rw.get(url).text
        again += _repeated(page)  # shown once more where it sorts now, but not counted again
        cards = [i for i in _ids(page) if i not in _repeated(page)]
        if cards:
            assert _summary(page).startswith(f"{start}–{start + len(cards) - 1} von {left} Mails"), _summary(page)
        seen += cards
        pages.append(cards)
        start += len(cards)
        url = _link(page, "next")
    assert again == [ids[0]]
    assert start - 1 == left and len([p for p in pages if p]) == (1 if n_open == 60 else 2)
    assert sorted(seen) == sorted(_open_ids(rw)) and len(seen) == len(set(seen)), "nothing skipped, nothing twice"
    # the link as first rendered (no renewal, e.g. a second tab) skips nothing either
    assert set(pages[0]) <= set(_ids(rw.get(stale_next).text))


def test_decisions_on_the_next_page_keep_counting_from_the_carried_card(rw):
    _fill_inbox(rw, 10, 60)
    page1 = rw.get("/eingang").text
    ids = _ids(page1)
    r = _post(rw, ids[0], page1, "reject")
    for mid in ids[1:5]:
        r = _post(rw, mid, page1, "none")
    assert _summary(r).startswith("1–46 von 66 Mails")
    page2_url = _link(r, "next")
    page2 = rw.get(page2_url).text
    assert _summary(page2).startswith("47–66 von 66 Mails") and _repeated(page2) == [ids[0]]
    state = _page_state(page2)
    assert state["seite_gezeigt"].startswith(f"{ids[0]}.") and "seite_weiter" not in state  # last page
    r = _post(rw, _ids(page2)[0], page2, "none")
    assert _summary(r).startswith("47–65 von 65 Mails"), _summary(r)
    # without JavaScript the decision returns to the same page, which still shows the card marked and
    # does not count it
    back = html_lib.unescape(re.search(r'name="back" value="([^"]+)"', page2).group(1))
    assert f"gezeigt={ids[0]}" in back
    r = rw.post(f"/eingang/{_ids(page2)[1]}", data={"action": "none", "back": back}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == back
    again = rw.get(back).text
    assert _repeated(again) == [ids[0]] and _summary(again).startswith("47–64 von 64 Mails"), _summary(again)
    # once the rejected card is decided on page 1, the renewed link no longer carries it
    r = _post(rw, ids[0], page1, "none")
    assert "gezeigt" not in (_link(r, "next") or "")


def test_rejected_card_in_the_suggestion_filter_is_counted_once_across_pages(rw):
    """In "Mit Vorschlag" the rejected mail leaves the filter but stays on page 1, counted on top of
    the total there; the next page goes on after it instead of counting place 50 twice."""
    _fill_inbox(rw, 70, 0)
    page1 = rw.get("/eingang", params={"status": "suggested"}).text
    ids = _ids(page1)
    r = _post(rw, ids[0], page1, "reject")
    assert _summary(r).startswith("1–50 von 70 Mails")
    nxt = _link(r, "next")
    assert nxt.startswith("/eingang?status=suggested&ab=") and f"gezeigt={ids[0]}" in nxt
    page2 = rw.get(nxt).text
    assert _summary(page2).startswith("51–70 von 70 Mails"), _summary(page2)
    assert sorted(ids + _ids(page2)) == sorted(_open_ids(rw, "suggested") + [ids[0]])
