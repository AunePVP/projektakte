"""Round 3 of the verification (package "oberflaeche"): the inbox summary line counts rejected
suggestions as open, snippets never start or end inside a word and show the body of a mail instead
of its header lines, the document page shows the paragraph chunking repeats only once, and an
attachment plMail has not delivered yet offers no file link."""
from __future__ import annotations

import html as html_lib
import json
import random
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte import db, inbox
from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.web import without_overlap
from projektakte.webtext import ELLIPSIS, mail_body, matcher, plain_text, snippet, trim_overlap

from .conftest import fake_settings

HX = {"HX-Request": "true"}


def _indexed_demo(root, fake_ai) -> TestClient:
    generate(root / "demo")
    s = fake_settings(fake_ai, root, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    return TestClient(create_app(s, background=False))


@pytest.fixture(scope="module")
def ro(tmp_path_factory, fake_ai):
    return _indexed_demo(tmp_path_factory.mktemp("r3ui"), fake_ai)


@pytest.fixture()
def rw(tmp_path, fake_ai):
    return _indexed_demo(tmp_path, fake_ai)


# --------------------------------------------------------------------------- inbox summary line
def _add_mails(client, n: int, status: str, prefix: str) -> None:
    with db.session(client.app.state.settings.db_path) as conn:
        conn.executemany(
            "INSERT INTO mails(source, source_key, subject, sender, date, preview, assign_status, suggested_project) "
            "VALUES('eml', ?, ?, 'x@example.org', ?, 'Text', ?, ?)",
            [(f"eml:{prefix}-{i}", f"{prefix} {i:03d}", f"2021-01-01T{i // 60:02d}:{i % 60:02d}:00+00:00", status,
              "BS-2024-0137" if status == "suggested" else None) for i in range(n)])


def _ids(page: str) -> list[int]:
    return [int(x) for x in re.findall(r'<article class="card inbox-item[^"]*" id="mail-(\d+)"', page)]


def _page_state(page: str) -> dict:
    return json.loads(html_lib.unescape(re.search(r"hx-vals='([^']+)'", page).group(1)))


def _post(client, mail_id: int, state: dict, action: str) -> str:
    r = client.post(f"/eingang/{mail_id}", data={"action": action, "back": "/eingang", **state}, headers=HX)
    assert r.status_code == 200
    return r.text


def _summary(html: str) -> str:
    m = re.search(r'<p class="muted small" id="inbox-summary"[^>]*>(.*?)</p>', html, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def _pager_info(html: str) -> str:
    m = re.search(r'<span id="inbox-pager-info" hx-swap-oob="true">(.*?)</span>', html, re.S)
    return m.group(1).strip() if m else ""


def test_rejected_cards_stay_open_in_the_summary_line(rw):
    """verify3: after "Verwerfen" the line claimed fewer open mails than the page showed and finally
    "Alle Mails dieser Seite sind bearbeitet" while two cards still had their buttons."""
    _add_mails(rw, 80, "suggested", "Vorschlag")
    _add_mails(rw, 40, "unassigned", "Offen")
    page = rw.get("/eingang").text
    total = rw.get("/api/inbox").json()["total"]
    ids, state = _ids(page), _page_state(page)
    assert len(ids) == 50 and state["seite_ids"] == ",".join(map(str, ids))
    for mid in ids[:2]:
        r = _post(rw, mid, state, "reject")
        assert "Vorschlag verworfen" in r and 'class="card inbox-item"' in r  # the card stays actionable
        assert _summary(r).startswith(f"1–50 von {total} Mails")
    for mid in ids[2:]:
        r = _post(rw, mid, state, "none")
    assert _summary(r).startswith(f"1–2 von {total - 48} Mails"), _summary(r)
    assert "bearbeitet" not in _summary(r) and _pager_info(r) == f"1–2 von {total - 48}"
    for mid in ids[:2]:  # now the page is really done
        r = _post(rw, mid, state, "none")
    assert _summary(r).startswith(f"Alle Mails dieser Seite sind bearbeitet · noch {total - 50} Mails")
    assert _pager_info(r) == "Seite bearbeitet"


def test_rejected_card_in_the_suggestion_filter_is_counted_on_top(rw):
    """In "Mit Vorschlag" a rejected mail leaves the filter but its card is still actionable: the line
    keeps it (never fewer open mails than shown), the filter button counts it under "Ohne Vorschlag"."""
    _add_mails(rw, 70, "suggested", "Vorschlag")
    counts = rw.get("/api/inbox", params={"status": "suggested"}).json()
    n_suggested = counts["total"]
    page = rw.get("/eingang", params={"status": "suggested"}).text
    ids, state = _ids(page), _page_state(page)
    assert state["seite_status"] == "suggested"
    r = _post(rw, ids[0], state, "reject")
    assert _summary(r).startswith(f"1–50 von {n_suggested} Mails")
    assert re.search(rf'id="inbox-count-suggested" hx-swap-oob="true">{n_suggested - 1}<', r)
    for mid in ids[1:]:
        r = _post(rw, mid, state, "none")
    assert _summary(r).startswith(f"1–1 von {n_suggested - 49} Mails"), _summary(r)
    r = _post(rw, ids[0], state, "none")
    assert _summary(r).startswith("Alle Mails dieser Seite sind bearbeitet")


def test_pages_rendered_before_the_update_still_get_a_summary(rw):
    """A page opened before the update sends no ``seite_ids``: the key range of the page is used."""
    _add_mails(rw, 60, "unassigned", "Offen")
    page = rw.get("/eingang").text
    total = rw.get("/api/inbox").json()["total"]
    state = {k: v for k, v in _page_state(page).items() if k != "seite_ids"}
    r = _post(rw, _ids(page)[0], state, "none")
    assert _summary(r).startswith(f"1–49 von {total - 1} Mails")
    # garbage in seite_ids is ignored, not an error
    page = rw.get("/eingang").text
    r = _post(rw, _ids(page)[0], {**_page_state(page), "seite_ids": "x,,-3,9" * 100}, "none")
    assert _summary(r).startswith(f"1–49 von {total - 2} Mails")


def test_page_ids_round_trip_and_reject_garbage():
    assert inbox.encode_ids([{"id": 3}, {"id": 12}]) == "3,12"
    assert inbox.decode_ids("3, 12,abc,,-4,1e3," + "9" * 19 + ",7") == [3, 12, 7]
    assert inbox.decode_ids(None) == [] and inbox.decode_ids("") == []
    assert len(inbox.decode_ids(",".join(["1"] * 1000))) == inbox.MAX_PAGE_IDS


# --------------------------------------------------------------------------- snippets: word boundaries
LETTER = (
    "Bauordnungsamt Fiktivhausen\nFiktivhausen, den 02.07.2024\n"
    "Aktenzeichen: BO-233/24    Ihr Zeichen: BS-2024-0233\nBaugenehmigung\n"
    "Bauherr: Seeblick Hotelbetriebe GmbH, vertreten durch Konstantin Rehberg\n"
    "Bauvorhaben: Umbau und Erweiterung eines Hotels auf 120 Gastbetten, vier Obergeschosse, "
    "Beherbergungsstätte nach der Beherbergungsstättenverordnung.\n"
    "Grundstück: Uferpromenade 15, 31777 Fiktivhausen\n"
    "Brandschutzplanung: Ingenieurbüro Feuerfest, Projekt BS-2024-0233 Hotel Seeblick\n"
    "Sehr geehrte Damen und Herren, auf Ihren Antrag wird die Baugenehmigung für das oben genannte "
    "Bauvorhaben nach Maßgabe der geprüften Bauvorlagen erteilt. Der Brandschutznachweis ist Bestandteil "
    "dieser Genehmigung. Die Genehmigung ergeht unter den folgenden Auflagen zum Brandschutz."
)


def test_identifier_snippet_starts_at_its_line_not_inside_a_word():
    """verify3: '… nzeichen: BO-233/24 Ihr Zeichen: BS-2024-0233 …' – the start was cut mid-word."""
    s = snippet(LETTER, "BS-2024-0233")
    assert s.startswith(f"{ELLIPSIS} Aktenzeichen: BO-233/24"), s
    assert s.count("BS-2024-0233") == 2


def test_match_inside_a_long_token_does_not_cut_the_token():
    """A match inside a mail address with no room before it: the window starts at its line (or the
    token) and grows a little instead of starting with '… ung@feuerwehr…'."""
    filler = "Die Unterlagen wurden geprüft und für vollständig befunden. " * 4
    text = (filler + "\nKontakt: Nina Brück <gefahrenvorbeugung@feuerwehr-beispielstadt.example> und weitere "
            "Angaben zur Zufahrt\nDie Zufahrt für die Feuerwehr ist freizuhalten; die Aufstellflächen der "
            "Feuerwehr werden markiert und die Feuerwehrpläne werden nach DIN 14095 ergänzt, abgestimmt mit der "
            + "Abteilung Gefahrenvorbeugung " * 5 + "und die Feuerwehr.")
    s = snippet(text, "Feuerwehr")  # before: '… g@feuerwehr-beispielstadt.example> und …'
    assert s.startswith(f"{ELLIPSIS} Kontakt: Nina Brück <gefahrenvorbeugung@"), s
    body = s.removeprefix(f"{ELLIPSIS} ")
    plain = plain_text(text)
    pos = plain.find(body[:40])
    assert pos >= 0 and (pos == 0 or plain[pos - 1].isspace()), s
    assert "feuerwehr-beispielstadt" in s


def _words(rnd: random.Random) -> str:
    """Random text: query words, long tokens (mail address, URL), identifiers and unique filler words
    (so a piece of a snippet is found at one place of the text only), with line and sentence breaks."""
    vocab = ["Brandschutz", "Feuerwehr", "Rettungsweg", "T30-RS", "BS-2024-0233", "Auflage", "Treppenraum",
             "info@feuerwehr-beispielstadt.example", "https://bauportal.example/vorgang/4711/stellungnahme",
             "Rauchableitung", "DIN 4102", "m²", "Brandmeldeanlage", "1,5", "Stichflur"]
    parts = []
    for _ in range(rnd.randint(40, 160)):
        if rnd.random() < 0.5:
            parts.append(rnd.choice(vocab))
        else:
            parts.append("".join(rnd.choice("bcdfghklmnprstwz") + rnd.choice("aeiouäöü")
                                 for _ in range(rnd.randint(1, 5))))
        if rnd.random() < 0.08:
            parts.append(rnd.choice([".\n", "\n", ". ", ": "]))
    return " ".join(parts)


def _at_word_start(plain: str, piece: str) -> bool:
    """Does ``piece`` occur in ``plain`` (not at its very beginning) right after white space?"""
    return any(m.start() > 0 and plain[m.start() - 1].isspace() for m in re.finditer(re.escape(piece), plain))


def _at_word_end(plain: str, piece: str) -> bool:
    return any(m.end() == len(plain) or plain[m.end()].isspace() or piece[-1] in ".,;:!?"
               for m in re.finditer(re.escape(piece), plain))


@pytest.mark.parametrize("seed", range(6))
def test_snippets_never_start_or_end_inside_a_word(seed):
    rnd = random.Random(seed)
    for _ in range(60):
        text = _words(rnd)
        plain = mail_body(plain_text(text))
        for q in ("Feuerwehr", "BS-2024-0233", "Stichflur Rauchableitung", "beispielstadt", "vorgang"):
            s = snippet(text, q)
            core = s
            if core.startswith(f"{ELLIPSIS} "):
                core = core[2:]
                assert _at_word_start(plain, core[:40]), (q, s[:80])
            if core.endswith(f" {ELLIPSIS}"):
                core = core[:-2]
                assert _at_word_end(plain, core[-40:]), (q, s[-80:])
            if matcher(q).spans(plain):
                assert matcher(q).spans(core), (q, s)


def test_demo_snippets_never_start_or_end_inside_a_word(ro):
    """The whole demo corpus with the queries of the verification (project numbers, terms)."""
    with db.session(ro.app.state.settings.db_path) as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM chunks")]
    queries = ["BS-2024-0233", "BS-2024-0058", "BS-2023-0412", "Feuerwehr", "Rauchableitung", "Brandmeldeanlage",
               "Löschwasser", "Rettungsweg", "T30", "Aufstellfläche", "Notlicht 8 Stunden", "Abnahme BMA"]
    cut = 0
    for text in texts:
        plain = mail_body(plain_text(text))
        for q in queries:
            s = snippet(text, q)
            if s.startswith(f"{ELLIPSIS} "):
                cut += 1
                assert _at_word_start(plain, s[2:42]), (q, s[:80])
            if s.endswith(f" {ELLIPSIS}"):
                assert _at_word_end(plain, s[-42:-2]), (q, s[-80:])
    assert cut > 20  # the check really looked at cut snippets


# --------------------------------------------------------------------------- snippets of mails
MAIL = ("Betreff: BS-2024-0233 Fluchtbalkon Entwurf\nVon: Clara Meier <c.meier@planung.example>\n"
        "Datum: 27.10.2024 09:12\nGuten Tag Herr Dr. Weber,\nanbei der Entwurf für den Fluchtbalkon am "
        "Stichflur, bitte um kurze Rückmeldung.\nMit freundlichen Grüßen\nClara Meier\nPlanungsbüro Meier")


def test_mail_snippet_shows_the_body_not_the_header_lines():
    """live-demo: mail hits showed 'Betreff: … Von: … Datum: … Guten Tag, … Mit freundlichen Grüßen'."""
    assert snippet(MAIL, "BS-2024-0233") == ("anbei der Entwurf für den Fluchtbalkon am Stichflur, bitte um "
                                            "kurze Rückmeldung.")
    assert snippet(MAIL, "Fluchtbalkon").startswith("anbei der Entwurf")
    # a long mail: the window is taken from the body only
    long_mail = MAIL.replace("bitte um kurze Rückmeldung.", "bitte um kurze Rückmeldung. " + "Text folgt. " * 60
                             + "Die Rauchableitung erfolgt über den Treppenraum.")
    s = snippet(long_mail, "Rauchableitung")
    assert "Rauchableitung" in s and "Betreff:" not in s and "Von:" not in s and "Grüßen" not in s


def test_texts_that_are_no_mail_keep_their_lines():
    letter = "Betreff: Bauvorhaben Hotel Seeblick\nSehr geehrte Damen und Herren,\nanbei die Unterlagen."
    assert mail_body(letter) == letter  # a letter's subject line is content, no Von/Datum header
    assert mail_body("Von: A\nDatum: B\nText") == "Von: A\nDatum: B\nText"
    header_only = "Betreff: Plan\nVon: A <a@b.example>\nDatum: 01.01.2024 10:00"
    assert mail_body(header_only) == header_only  # nothing else to show
    assert mail_body("Betreff: X\nDatum: 1.1.2024\nHallo zusammen,\nGrüße an das Team: der Plan ist da.") == \
        "Grüße an das Team: der Plan ist da."  # the first body line is never taken for a closing


def test_search_page_mail_hits_show_no_header_lines(ro):
    html = ro.get("/suche", params={"q": "BS-2024-0233"}).text
    mail_hits = re.findall(r'<li[^>]*>\s*<div><a href="/mails/\d+">.*?<blockquote>(.*?)</blockquote>', html, re.S)
    assert mail_hits, "the project number finds mails"
    for quote in mail_hits:
        assert not re.search(r"(^|\n)\s*(Betreff|Von|Datum):", quote) and "Mit freundlichen Grüßen" not in quote


# --------------------------------------------------------------------------- document page overlap
def test_trim_overlap():
    assert trim_overlap("A\nB\nC", "C\nD") == "A\nB"
    assert trim_overlap("A\nB\nC", "B\nC\nD") == "A"
    assert trim_overlap("A\nB  x\n", " B x\nD") == "A"  # white space does not count
    assert trim_overlap("C", "C\nD") == "C"  # a section never disappears completely
    assert trim_overlap("A\nB", "C\nB") == "A\nB"
    assert trim_overlap("", "") == ""


def test_without_overlap_only_within_a_page_and_heading():
    rows = [{"id": 1, "page": 1, "heading": None, "text": "A\nB"},
            {"id": 2, "page": 1, "heading": None, "text": "B\nC"},
            {"id": 3, "page": 2, "heading": None, "text": "C\nD"},  # the next page may repeat a line
            {"id": 4, "page": 2, "heading": "2 Bau", "text": "D\nE"}]
    out = without_overlap(rows)
    assert [r["text"] for r in out] == ["A", "B\nC", "C\nD", "D\nE"]
    assert [r["id"] for r in out] == [1, 2, 3, 4] and rows[0]["text"] == "A\nB"  # input unchanged


def test_document_page_shows_overlapping_paragraphs_once(ro):
    """verify3: /dokumente/5 showed 'Auflage 4 (Rauchableitung) …' at the end of the second and the
    beginning of the third section. Every section keeps its anchor."""
    docs = ro.get("/api/projects/BS-2023-0412/documents").json()["documents"]
    permit = next(d for d in docs if d["filename"] == "Baugenehmigung.pdf")
    with db.session(ro.app.state.settings.db_path) as conn:
        chunks = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id = ? ORDER BY ord", (permit["id"],))]
    main = ro.get(f"/dokumente/{permit['id']}").text.split("<main>", 1)[1]
    for label in ("Auflage 4 (Rauchableitung)", "Auflage 7 (Brandschutzbeauftragter)"):
        assert main.count(label) == 1, label
    for cid in chunks:
        assert f'id="c{cid}"' in main
    # a search hit on the section that starts with the overlap lands right on its text
    with db.session(ro.app.state.settings.db_path) as conn:
        cid = conn.execute("SELECT id FROM chunks WHERE doc_id = ? AND text LIKE 'Auflage 4 (Rauchableitung)%'",
                           (permit["id"],)).fetchone()[0]
    section = re.search(rf'<section class="chunk" id="c{cid}">(.*?)</section>', main, re.S).group(1)
    assert "Auflage 4 (Rauchableitung)" in section


# --------------------------------------------------------------------------- attachments not delivered
def _pending_mail(client) -> tuple[int, int, int]:
    s = client.app.state.settings
    s.cache_dir.mkdir(parents=True, exist_ok=True)
    cached = s.cache_dir / "r3-plan.txt"
    cached.write_text("Plan Brandabschnitte", encoding="utf-8")
    with db.session(s.db_path) as conn:
        mid = conn.execute(
            "INSERT INTO mails(source, source_key, subject, sender, sender_name, date, body, preview, project, "
            "assign_status, has_attachments) VALUES('jmap', 'jmap:7:r3', 'Plan und Notiz', 'a@b.example', 'Anna', "
            "'2025-03-03T08:00:00+00:00', 'Anbei.', 'Anbei.', 'BS-2024-0137', 'assigned', 1)").lastrowid
        ids = []
        for i, (name, status, cache, message) in enumerate([
                ("Notiz.txt", "pending", "", "Anhang ausstehend: plMail hat ihn noch nicht geliefert (HTTP 503). "
                                             "Er wird automatisch nachgeladen."),
                ("Plan.txt", "ok", str(cached), None)]):
            ids.append(conn.execute(
                "INSERT INTO documents(path, project, mail_id, rel_path, filename, ext, size, status, message, "
                "cache_path) VALUES(?, 'BS-2024-0137', ?, ?, ?, '.txt', 20, ?, ?, ?)",
                (f"mail:{mid}:{i}:{name}", mid, name, name, status, message, cache)).lastrowid)
    return mid, ids[0], ids[1]


def test_pending_attachment_offers_no_file_link(rw):
    """daten: /mails/77 showed 'Datei öffnen' for an attachment plMail had not delivered (404)."""
    mid, pending, ok = _pending_mail(rw)
    mail = rw.get(f"/mails/{mid}").text
    assert f"/dokumente/{pending}/datei" not in mail
    assert re.search(rf'href="/dokumente/{ok}/datei/Plan\.txt">Datei öffnen</a>', mail)
    item = re.search(rf'<li><a href="/dokumente/{pending}">Notiz\.txt</a>.*?</li>', mail, re.S).group(0)
    assert "Anhang ausstehend" in item and "wird automatisch nachgeladen" in item and "Datei öffnen" not in item

    doc = rw.get(f"/dokumente/{pending}").text
    assert "Originaldatei öffnen" not in doc and f"/dokumente/{pending}/datei" not in doc
    assert '<span class="pill warn">Anhang ausstehend</span>' in doc
    assert "Originaldatei noch nicht da – sie wird automatisch nachgeladen" in doc
    assert rw.get(f"/dokumente/{pending}/datei").status_code == 404
    assert "Originaldatei öffnen" in rw.get(f"/dokumente/{ok}").text

    project = rw.get("/projekte/BS-2024-0137", params={"auswahl": f"m{mid}"}).text  # the card of the mail in the timeline
    assert f"/dokumente/{pending}/datei" not in project
    assert re.search(rf'<a href="/dokumente/{pending}">Notiz\.txt</a> <span class="muted small">\(Anhang ausstehend\)',
                     project)


def test_missing_original_file_offers_no_link(rw):
    """A project file that is gone from disk (index not yet updated): a note instead of a 404 link."""
    docs = rw.get("/api/projects/BS-2024-0137/documents").json()["documents"]
    doc = next(d for d in docs if d["ext"] == ".pdf")
    with db.session(rw.app.state.settings.db_path) as conn:
        path = conn.execute("SELECT path FROM documents WHERE id = ?", (doc["id"],)).fetchone()[0]
    Path(path).unlink()
    page = rw.get(f"/dokumente/{doc['id']}").text
    assert "Originaldatei öffnen" not in page and "Die Originaldatei ist nicht verfügbar." in page
