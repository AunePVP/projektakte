"""Timeline layout (zeitleiste.build) and the timeline, chosen entry and tabs of the project page."""
from __future__ import annotations

import random
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from projektakte import db, web, zeitleiste
from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app

from .conftest import fake_settings

TODAY = date(2026, 11, 10)
HX = {"HX-Request": "true"}


def _entry(i: int, day: date | None, kind: str = "doc") -> dict:
    return {"key": f"{'d' if kind == 'doc' else 'm'}{i}", "kind": kind, "title": f"Eintrag {i}", "full": f"Ordner/Eintrag {i}.pdf",
            "day": day, "sort": day.isoformat() if day else ""}


def _big(count: int = 500, seed: int = 7) -> list[dict]:
    """Entries over twelve years like a long project: mostly old and spread, the last weeks busy."""
    rnd = random.Random(seed)
    out = []
    for i in range(count):
        age = int(rnd.random() * 4400) if i % 5 else int(rnd.random() * rnd.random() * 60)
        out.append(_entry(i, TODAY - timedelta(days=age), "mail" if i % 4 == 0 else "doc"))
    return out


def test_empty_and_undated():
    assert zeitleiste.build([], TODAY)["empty"] is True
    only_undated = zeitleiste.build([_entry(1, None), _entry(2, None)], TODAY)
    assert only_undated["undated"] == 2 and only_undated["height"] == 0 and not only_undated["empty"]
    mixed = zeitleiste.build([_entry(1, None), _entry(2, TODAY)], TODAY)
    assert mixed["undated"] == 1 and len(mixed["labels"]) == 1 and mixed["count"] == 2


def test_a_small_project_labels_every_entry_and_grows():
    entries = [_entry(i, TODAY - timedelta(days=40 + 3 * i)) for i in range(25)]  # all within a short stretch
    tl = zeitleiste.build(entries, TODAY)
    assert len(tl["labels"]) == 25 and not tl["chips"]
    ys = [float(label["y"]) for label in tl["labels"]]
    assert all(b - a >= zeitleiste.GAP - 0.2 for a, b in zip(ys, ys[1:]))
    assert tl["height"] >= ys[-1] + zeitleiste.PAD  # the timeline never ends above its last line


def test_a_big_project_sums_up_with_chips_and_stays_light():
    tl = zeitleiste.build(_big(), TODAY)
    assert tl["chips"] and tl["labels"]
    assert tl["height"] <= zeitleiste.height_for(500) + 60
    ys = sorted([float(x["y"]) for x in tl["labels"]] + [float(c["y"]) for c in tl["chips"]])
    assert all(b - a >= zeitleiste.GAP - 0.5 for a, b in zip(ys, ys[1:])), "lines overlap"
    assert len(tl["dots"]) <= 500 and len(tl["leaders"]) <= len(tl["labels"]) + zeitleiste.MAX_CHIP_LEADERS * len(tl["chips"])
    assert re.fullmatch(r"\d+ Einträge · [A-Z][a-zä]{2} \d\d( – [A-Z][a-zä]{2} \d\d)?", tl["chips"][0]["text"])
    assert tl["chips"][0]["key"].startswith("z") and "_" in tl["chips"][0]["key"]


def test_recent_time_gets_more_room_than_old_time():
    tl = zeitleiste.build(_big(), TODAY)
    by_text = {t["text"]: float(t["y"]) for t in tl["ticks"]}
    last_month = by_text["−4 Wo"] - by_text["heute"]  # four weeks
    years = sorted((float(t["y"]) for t in tl["ticks"] if t["text"].isdigit()))
    one_old_year = years[1] - years[0]  # a whole year of the old part
    assert last_month > one_old_year / 2, (last_month, one_old_year)  # four weeks ≈ half an old year of axis
    assert tl["ticks"][0]["text"] == "heute" and tl["ticks"][0]["show"]
    texts = {t["text"] for t in tl["ticks"]}
    assert {"2025", "2020"} <= texts and {"−1 Wo", "−2 Wo"} <= texts


def test_reference_is_the_newest_entry_not_a_date_in_the_future_or_the_long_past():
    old = zeitleiste.build([_entry(1, date(2024, 6, 1)), _entry(2, date(2024, 1, 5))], TODAY)
    assert old["ticks"][0]["text"] == "01.06.24" and float(old["labels"][0]["y"]) < 40  # starts at the top
    future = zeitleiste.build([_entry(1, TODAY + timedelta(days=30)), _entry(2, TODAY - timedelta(days=10))], TODAY)
    assert future["ticks"][0]["text"] == "heute" and float(future["labels"][0]["y"]) < 40


def test_filter_and_selection_marks():
    entries = _big(120)
    mails = zeitleiste.build(entries, TODAY, art="mail")
    assert mails["count"] == 30 and all(label["kind"] == "mail" for label in mails["labels"])
    one = next(e["key"] for e in entries if e["kind"] == "doc")
    marked = zeitleiste.build(entries, TODAY, selected=one)
    assert [label["key"] for label in marked["labels"] if label["sel"]] in ([one], [])
    chip = zeitleiste.build(_big(), TODAY)["chips"][0]
    assert [c["key"] for c in zeitleiste.build(_big(), TODAY, selected=chip["key"])["chips"] if c["sel"]] == [chip["key"]]
    assert zeitleiste.group_key(date(2024, 1, 2), date(2024, 3, 4)) == "z2024-01-02_2024-03-04"


# --------------------------------------------------------------------------- the project page
@pytest.fixture()
def client(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    return TestClient(create_app(s, background=False))


def _ids(client, number="BS-2024-0137") -> dict:
    with db.session(client.app.state.settings.db_path) as conn:
        doc = conn.execute("SELECT id FROM documents WHERE project = ? AND filename = 'Baugenehmigung.pdf'", (number,)).fetchone()
        mail = conn.execute("SELECT id FROM mails WHERE project = ? AND subject LIKE 'Stellungnahme Brandschutz%'", (number,)).fetchone()
    return {"doc": doc[0], "mail": mail[0]}


def test_timeline_is_drawn_without_inline_styles(client):
    html = client.get("/projekte/BS-2024-0137").text
    svg = html.split('<svg id="tl-svg"', 1)[1].split("</svg>", 1)[0]
    assert "<foreignObject" in svg and 'class="tl-lab doc' in svg and 'class="tl-lab mail' in svg
    assert " style=" not in svg and "<style" not in svg
    assert 'class="proj-layout"' in html and '<aside class="proj-side card" id="zeitleiste"' in html
    assert '<main class="wide">' in html and 'id="auswahl"' in html
    assert "13" in re.search(r'<a href="[^"]*" hx-get[^>]*class="on"[^>]*>Alle <span class="n">(\d+)</span>', html).group(1)


def test_choosing_an_entry_shows_its_card_and_marks_it(client):
    ids = _ids(client)
    page = client.get("/projekte/BS-2024-0137", params={"auswahl": f"d{ids['doc']}"}).text
    card = page.split('<div id="auswahl"', 1)[1].split("</section>", 1)[0]
    assert "Baugenehmigung.pdf" in card and f'href="/dokumente/{ids["doc"]}"' in card and "Im Ordner zeigen" in card
    assert "01 Genehmigung" in card and 'tl-lab doc sel' in page and 'aria-current="true"' in page
    mail = client.get("/projekte/BS-2024-0137", params={"auswahl": f"m{ids['mail']}"}).text
    mail_card = mail.split('<div id="auswahl"', 1)[1].split("</section>", 1)[0]
    assert "Stellungnahme Brandschutzdienststelle" in mail_card and f'href="/mails/{ids["mail"]}"' in mail_card
    assert "Stellungnahme Feuerwehr.pdf" in mail_card  # its attachment


@pytest.mark.parametrize("auswahl", ["", "d999999", "m0", "x12", "d12; drop", "z2024-13-45_2024-14-01", "z2024-01-01", "../../etc", "d" + "9" * 30])
def test_unusable_choices_show_no_card(client, auswahl):
    page = client.get("/projekte/BS-2024-0137", params={"auswahl": auswahl})
    assert page.status_code == 200 and 'class="card auswahl"' not in page.text and "Traceback" not in page.text


def test_choosing_a_stretch_lists_its_entries(client):
    page = client.get("/projekte/BS-2024-0137", params={"auswahl": "z2024-03-01_2024-03-31"}).text
    card = page.split('<div id="auswahl"', 1)[1].split("</section>", 1)[0]
    assert "01.03.2024 – 31.03.2024" in card and "2 Einträge" in card
    assert "Baugenehmigung.pdf" in card and "Baugenehmigung 63-0137" in card
    only_mail = client.get("/projekte/BS-2024-0137", params={"auswahl": "z2024-03-01_2024-03-31", "art": "mail"}).text
    assert "1 Eintrag" in only_mail.split('<div id="auswahl"', 1)[1].split("</section>", 1)[0]
    assert "Baugenehmigung.pdf</a>" not in only_mail.split('<div id="auswahl"', 1)[1].split("</section>", 1)[0]


def test_entries_without_a_date_have_their_own_link(client):
    with db.session(client.app.state.settings.db_path) as conn:
        conn.execute("INSERT INTO documents(path, project, rel_path, filename, ext, size, status) VALUES "
                     "('/x/ohne.pdf', 'BS-2024-0137', 'Ablage/ohne.pdf', 'ohne.pdf', '.pdf', 1, 'ok')")
    page = client.get("/projekte/BS-2024-0137").text
    assert "1 Eintrag ohne Datum" in page and 'auswahl=zohne' in page
    card = client.get("/projekte/BS-2024-0137", params={"auswahl": "zohne"}).text
    assert "Einträge ohne Datum" in card and "ohne.pdf" in card.split('<div id="auswahl"', 1)[1].split("</section>", 1)[0]


def test_timeline_filter_and_partials(client):
    ids = _ids(client)
    mails = client.get("/projekte/BS-2024-0137", params={"art": "mail"}).text
    svg = mails.split('<svg id="tl-svg"', 1)[1].split("</svg>", 1)[0]
    assert 'tl-lab mail' in svg and 'tl-lab doc' not in svg
    # htmx: the drawing alone, the card out of band – no page around it
    part = client.get("/projekte/BS-2024-0137/zeitleiste", params={"auswahl": f"d{ids['doc']}", "nur": "svg"}, headers=HX)
    assert part.status_code == 200 and "<html" not in part.text and part.text.lstrip().startswith("<svg id=\"tl-svg\"")
    assert 'hx-swap-oob="outerHTML"' in part.text and "Baugenehmigung.pdf" in part.text.split('id="auswahl"', 1)[1]
    whole = client.get("/projekte/BS-2024-0137/zeitleiste", params={"art": "doc"}, headers=HX).text
    assert whole.lstrip().startswith('<div id="tl">') and "Dokumente <span" in whole and 'tl-lab mail' not in whole
    # without htmx the address leads to the page
    plain = client.get("/projekte/BS-2024-0137/zeitleiste", params={"art": "doc", "auswahl": f"d{ids['doc']}"}, follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"] == f"/projekte/BS-2024-0137?art=doc&auswahl=d{ids['doc']}#zeitleiste"
    assert client.get("/projekte/BS-1999-0001/zeitleiste", headers={**HX, "Accept": "text/html"}).status_code == 404


def test_tabs_documents_attachments_mails(client):
    docs = client.get("/projekte/BS-2024-0137").text.split('<div id="inhalt">', 1)[1]
    assert 'aria-current="page"' in docs and "01 Genehmigung" in docs and "Aus der Mail" not in docs
    mails = client.get("/projekte/BS-2024-0137", params={"tab": "mails"}).text.split('<div id="inhalt">', 1)[1]
    assert "Zuordnung" in mails and "Zustimmung zur Abweichung" in mails and "dok-browser" not in mails
    atts = client.get("/projekte/BS-2024-0137", params={"tab": "anhaenge"}).text.split('<div id="inhalt">', 1)[1]
    assert "Stellungnahme Feuerwehr.pdf" in atts
    assert 'id="dok-browser"' in client.get("/projekte/BS-2024-0137", params={"tab": "unsinn"}).text
    part = client.get("/projekte/BS-2024-0137/inhalt", params={"tab": "mails"}, headers=HX)
    assert part.status_code == 200 and part.text.lstrip().startswith('<div id="inhalt">') and "<html" not in part.text
    plain = client.get("/projekte/BS-2024-0137/inhalt", params={"tab": "mails"}, follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"] == "/projekte/BS-2024-0137?tab=mails#inhalt"
    assert client.get("/projekte/BS-1999-0001/inhalt", headers={**HX, "Accept": "text/html"}).status_code == 404


def test_long_mail_and_attachment_lists_are_cut(client, monkeypatch):
    monkeypatch.setattr(web, "LIST_LIMIT", 3)
    cut = client.get("/projekte/BS-2024-0137", params={"tab": "mails"}).text
    assert "3 von 8" in cut and "alle zeigen" in cut and cut.count('<td class="date">') == 3
    everything = client.get("/projekte/BS-2024-0137", params={"tab": "mails", "alle": "1"}).text
    assert everything.count('<td class="date">') == 8 and "alle zeigen" not in everything
