"""Inbox paging by key (package "oberflaeche", round 3): working through the pages one after the other
never skips mails, and the summary line follows the HTMX decisions like the badge does."""
from __future__ import annotations

import html as html_lib
import json
import re

import pytest
from fastapi.testclient import TestClient

from projektakte import db, inbox
from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app

from .conftest import fake_settings

HX = {"HX-Request": "true"}


@pytest.fixture()
def rw(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    return TestClient(create_app(s, background=False))


def _add_inbox_mails(client, n: int, *, same_date: bool = False) -> None:
    with db.session(client.app.state.settings.db_path) as conn:
        conn.executemany(
            "INSERT INTO mails(source, source_key, subject, sender, date, preview, assign_status) "
            "VALUES('eml', ?, ?, 'x@example.org', ?, 'Text', 'unassigned')",
            [(f"eml:paging-{i}", f"Testmail {i:03d}",
              "2020-01-01T00:00:00+00:00" if same_date else f"2020-01-01T{i // 60:02d}:{i % 60:02d}:00+00:00")
             for i in range(n)])


def _ids(page: str) -> list[int]:
    return [int(x) for x in re.findall(r'<article class="card inbox-item[^"]*" id="mail-(\d+)"', page)]


def _link(page: str, rel: str) -> str | None:
    m = re.search(rf'href="([^"]+)" rel="{rel}"', page)
    return html_lib.unescape(m.group(1)) if m else None


def _page_state(page: str) -> dict:
    """The hx-vals of the card list – what htmx sends along with every decision of that page."""
    return json.loads(html_lib.unescape(re.search(r"hx-vals='([^']+)'", page).group(1)))


def _decide(client, mail_id: int, page: str, action: str = "none", **extra) -> str:
    data = {"action": action, "back": "/eingang", **_page_state(page), **extra}
    r = client.post(f"/eingang/{mail_id}", data=data, headers=HX)
    assert r.status_code == 200
    return r.text


def _summary(html: str) -> str:
    m = re.search(r'<p class="muted small" id="inbox-summary"[^>]*>(.*?)</p>', html, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def test_next_page_after_deciding_the_whole_page_skips_nothing(rw):
    _add_inbox_mails(rw, 160)
    order = [m["id"] for m in rw.get("/api/inbox").json()["mails"]]
    page1 = rw.get("/eingang").text
    assert _ids(page1) == order[:50]
    next_url = _link(page1, "next")
    for mid in _ids(page1):  # the whole page is worked off with HTMX, the page stays open
        _decide(rw, mid, page1)
    page2 = rw.get(next_url).text
    assert _ids(page2) == order[50:100], "the mails formerly on places 51–100 come next"
    # again: a few decisions, then "Weiter" – still no gap
    for mid in _ids(page2)[:6]:
        _decide(rw, mid, page2)
    page3 = rw.get(_link(page2, "next")).text
    assert _ids(page3)[0] == order[100] and _ids(page3) == order[100:150]


def test_paging_is_stable_for_mails_with_the_same_date(rw):
    _add_inbox_mails(rw, 130, same_date=True)  # the id decides between equal dates
    order = [m["id"] for m in rw.get("/api/inbox").json()["mails"]]
    seen, url = [], "/eingang"
    for _ in range(5):
        page = rw.get(url).text
        seen += _ids(page)
        url = _link(page, "next")
        if not url:
            break
    assert seen == order


def test_previous_first_and_last_page(rw):
    _add_inbox_mails(rw, 160)
    order = [m["id"] for m in rw.get("/api/inbox").json()["mails"]]
    total = len(order)
    page1 = rw.get("/eingang").text
    assert _link(page1, "prev") is None and "« Anfang" not in page1
    page2 = rw.get(_link(page1, "next")).text
    assert f"51–100 von {total} Mails" in _summary(page2)
    assert _ids(rw.get(_link(page2, "prev")).text) == order[:50]
    page3 = rw.get(_link(page2, "next")).text
    back = rw.get(_link(page3, "prev")).text
    assert _ids(back) == order[50:100] and 'href="/eingang">« Anfang' in back
    last = rw.get("/eingang", params={"ende": "1"}).text
    assert _ids(last) == order[-50:] and _link(last, "next") is None
    # a cursor behind the end (everything after it decided meanwhile) shows the last page
    beyond = inbox.encode_key((0, "", 0))
    assert _ids(rw.get("/eingang", params={"ab": beyond}).text) == order[-50:]
    # the page before the second page is short of mails after decisions: the first page is shown
    for mid in order[:3]:
        _decide(rw, mid, page1)
    assert _ids(rw.get(_link(page2, "prev")).text) == order[3:53]


def test_filter_keeps_its_cursor(rw):
    _add_inbox_mails(rw, 120)
    unassigned = [m["id"] for m in rw.get("/api/inbox", params={"status": "unassigned"}).json()["mails"]]
    page1 = rw.get("/eingang", params={"status": "unassigned"}).text
    nxt = _link(page1, "next")
    assert nxt.startswith("/eingang?status=unassigned&ab=")
    page2 = rw.get(nxt).text
    assert _ids(page2) == unassigned[50:100]
    assert f'name="back" value="{html_lib.escape(nxt)}"' in page2
    state = _page_state(page2)
    assert state["seite_status"] == "unassigned" and state["seite_pager"] == "1"


def test_without_javascript_the_decision_returns_to_the_same_position(rw):
    _add_inbox_mails(rw, 120)
    order = [m["id"] for m in rw.get("/api/inbox").json()["mails"]]
    page2_url = _link(rw.get("/eingang").text, "next")
    page2 = rw.get(page2_url).text
    back = html_lib.unescape(re.search(r'name="back" value="([^"]+)"', page2).group(1))
    r = rw.post(f"/eingang/{order[50]}", data={"action": "none", "back": back}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == back
    assert _ids(rw.get(back).text) == order[51:101]


def test_summary_line_follows_htmx_decisions(rw):
    _add_inbox_mails(rw, 120)
    total = rw.get("/api/inbox").json()["total"]
    page1 = rw.get("/eingang").text
    assert _summary(page1).startswith(f"1–50 von {total} Mails")
    ids = _ids(page1)
    for mid in ids[:4]:
        r = _decide(rw, mid, page1)
    assert 'id="inbox-summary" hx-swap-oob="true"' in r
    assert _summary(r).startswith(f"1–46 von {total - 4} Mails")
    assert re.search(rf'<span id="inbox-pager-info" hx-swap-oob="true">1–46 von {total - 4}</span>', r)
    assert re.search(rf'id="inbox-badge"[^>]*hx-swap-oob="true">{total - 4}<', r)
    # the next page counts from the mails still open: 4 left before it, one decided on it
    page2 = rw.get(_link(page1, "next")).text
    assert _summary(page2).startswith(f"47–96 von {total - 4} Mails")
    r = _decide(rw, _ids(page2)[0], page2)
    assert _summary(r).startswith(f"47–95 von {total - 5} Mails")
    # all of a page decided
    for mid in ids[4:]:
        r = _decide(rw, mid, page1)
    assert _summary(r).startswith(f"Alle Mails dieser Seite sind bearbeitet · noch {total - 51} Mails im Eingang")
    assert 'href="/eingang">Nächste Mails laden' in r
    assert "Seite bearbeitet" in r


def test_small_inbox_summary_without_pager(rw):
    page = rw.get("/eingang").text
    total = int(re.search(r'id="inbox-count-all">(\d+)<', page).group(1))
    assert _summary(page) == f"{total} Mails · Vorschläge zuerst, dann die neuesten"
    assert '<nav class="pager"' not in page and _page_state(page)["seite_pager"] == ""
    r = _decide(rw, _ids(page)[0], page)
    assert _summary(r) == f"{total - 1} Mails · Vorschläge zuerst, dann die neuesten"
    assert "inbox-pager-info" not in r  # no out-of-band target that the page does not have


def test_failed_assignment_keeps_the_typed_project(rw):
    page = rw.get("/eingang").text
    mid = _ids(page)[0]
    r = _decide(rw, mid, page, action="assign", project="Gibtsnicht 4711")
    assert 'class="note warn"' in r and "Unbekanntes Projekt" in r
    assert 'value="Gibtsnicht 4711"' in r
    # a decision with a malformed page cursor still works (no summary then)
    r = rw.post(f"/eingang/{mid}", data={"action": "none", "seite_von": "kaputt", "seite_bis": "x"}, headers=HX)
    assert r.status_code == 200 and "inbox-summary" not in r.text


def test_keys_round_trip_and_reject_garbage():
    key = (1, "2024-03-23T14:58:00+00:00", 4711)
    assert inbox.decode_key(inbox.encode_key(key)) == key
    assert inbox.decode_key(inbox.encode_key((0, "", 7))) == (0, "", 7)
    for bad in ("", "x", "2~1~d", "1~abc~d", "1~1~" + "x" * 65, None):
        assert inbox.decode_key(bad) is None
