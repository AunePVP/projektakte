"""Text on the pages (package "oberflaeche", round 3): identifier searches show their hits, snippets
show the matching passage without markdown syntax, the document page reads like a document,
original files carry their name, start page and search page details, mobile tables."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte.ai.services import AIServices
from projektakte.ask import AskResult
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.web import chunk_view, file_url, search_view, templates
from projektakte.webtext import QueryMatcher, highlight, passage, plain_text, snippet

from .conftest import fake_settings

HX = {"HX-Request": "true"}
STATIC = Path(__file__).resolve().parent.parent / "projektakte" / "static"


@pytest.fixture(scope="module")
def ro(tmp_path_factory, fake_ai):
    root = tmp_path_factory.mktemp("texte")
    generate(root / "demo")
    s = fake_settings(fake_ai, root, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    return TestClient(create_app(s, background=False))


def _hits(html: str, cls: str = "hits") -> str:
    m = re.search(rf'<ol class="{cls}">(.*?)</ol>', html, re.S)
    return m.group(1) if m else ""


MARKDOWN_RESIDUE = re.compile(r"\|\s*-{3}|\|---|^#+ |\n#+ |\| Angabe \||Kennwert \| Wert", re.M)


# --------------------------------------------------------------------------- identifier searches
@pytest.mark.parametrize("query, expected", [
    ("BS-2024-0137", "BS-2024-0137"),  # project number
    ("BA-412/2023", "BA-412/2023"),  # file number of a building permit
    ("63-0137/24", "63-0137/24"),
])
def test_identifier_search_shows_the_literal_hits(ro, query, expected):
    html = ro.get("/suche", params={"q": query}).text
    hits = _hits(html)
    assert "Keine passenden Treffer" not in html and hits
    assert f"<mark>{expected}</mark>" in hits
    # every shown hit that is labelled "matching" really contains it
    for li in re.findall(r"<li>(.*?)</li>", hits, re.S):
        assert expected in li


def test_project_number_search_links_the_project_and_marks_it_as_a_whole(ro):
    html = ro.get("/suche", params={"q": "BS-2024-0137"}).text
    assert 'href="/projekte/BS-2024-0137">Zum Projekt BS-2024-0137 Grundschule Am Lindenhof</a>' in html
    hits = _hits(html)
    assert "<mark>2024</mark>" not in hits and "<mark>0137</mark>" not in hits
    assert re.search(r"Stand: \d\d\.\d\d\.2024", hits)  # dates stay unmarked


def test_search_view_never_hides_literal_identifier_hits():
    class H:
        def __init__(self, text, rerank):
            self.text, self.rerank, self.title, self.rel_path, self.label = text, rerank, "", "", ""

    class R:
        reranked = True
        hits = [H("Projekt BS-2024-0137 – Grundschule", 0.0), H("Stand: 09.01.2024", 0.0),
                H("Etwas ganz anderes", 0.0), H("Rettungsweg", 0.4)]

    v = search_view(R(), min_score=0.05, show_weak=False, query="BS-2024-0137")
    assert [h.text for h in v["strong"]] == ["Projekt BS-2024-0137 – Grundschule", "Rettungsweg"]
    assert len(v["weak"]) == 2
    # a lone digit is no identifier
    assert len(search_view(R(), min_score=0.05, show_weak=False, query="1")["weak"]) == 3


def test_search_link_of_an_answer_leads_to_visible_hits(ro):
    data = {"question": "Wer ist der Statiker?", "project": "BS-2024-0137"}
    answer = ro.post("/fragen", data=data, headers=HX).text
    url = re.search(r'href="(/suche\?[^"]+)">(?:In der Suche öffnen|Alle Treffer in der Suche ansehen)', answer)
    page = ro.get(url.group(1).replace("&amp;", "&")).text
    assert '<ol class="hits' in page and "<li" in _hits(page) + _hits(page, "hits weak")
    if "<ol class=\"hits\">" not in page:
        assert "Keine gut passenden Treffer" in page and "Keine passenden Treffer" not in page


def test_restricted_search_links_all_projects_and_names_no_project(ro):
    html = ro.get("/suche", params={"q": "xyzzy", "project": "BS-2024-0137"}).text
    assert 'oder <a href="/suche?q=xyzzy">in allen Projekten suchen</a>' in html
    weak = ro.get("/suche", params={"q": "Rettungsweg", "project": "BS-2024-0137"}).text
    assert 'class="muted">· <a href="/projekte/' not in _hits(weak)


def test_hit_header_does_not_repeat_the_project(ro):
    hits = _hits(ro.get("/suche", params={"q": "Wendehals"}).text)
    items = re.findall(r"<li>\s*<div>(.*?)</div>", hits, re.S)
    assert items
    for head in items:
        label = re.search(r"<a [^>]*>(.*?)</a>", head, re.S).group(1)
        number = re.search(r"BS-\d{4}-\d{4}", label)
        if number:
            assert f'href="/projekte/{number.group(0)}"' not in head, head
    assert any('· <a href="/projekte/' in head for head in items)  # documents still name it


# --------------------------------------------------------------------------- snippets
def test_search_snippet_is_the_matching_passage_without_markdown(ro):
    hits = _hits(ro.get("/suche", params={"q": "Brandschutzbeauftragter"}).text)
    assert "<mark>Brandschutzbeauftragten</mark>" in hits or "<mark>Brandschutzbeauftragter</mark>" in hits
    for q in ("Prüfingenieur Wendehals", "Einstellplätze Kennwerte", "Feststellungen Ventilatoren"):
        html = _hits(ro.get("/suche", params={"q": q, "weak": "1"}).text)
        assert html and not MARKDOWN_RESIDUE.search(html), q
    kennwerte = _hits(ro.get("/suche", params={"q": "Einstellplätze"}).text)
    assert re.search(r"<mark>Einstellplätze</mark>: \d+", kennwerte)


def test_snippet_window_centres_on_the_query():
    filler = " ".join(f"Satz {i} über die allgemeine Lage des Grundstücks." for i in range(1, 15))
    text = f"# 3 Sicherheitsbeleuchtung\n{filler}\nAuflage 4: Die Sicherheitsbeleuchtung muss mindestens " \
           f"8 Stunden betrieben werden können.\n{filler}"
    out = snippet(text, "Wie lange muss die Sicherheitsbeleuchtung betrieben werden?")
    assert "8 Stunden" in out and out.startswith("… ") and out.endswith(" …") and len(out) <= 410
    assert "#" not in out
    # without a match: the beginning, clean
    assert snippet(text, "xyzzy").startswith("3 Sicherheitsbeleuchtung\nSatz 1")
    # short texts stay whole
    assert snippet("| Angabe | Inhalt |\n|---|---|\n| Projekt | BS-1 |", "Projekt") == "Projekt: BS-1"


def test_answer_sources_show_the_relevant_passage():
    chunk = ("Auflage 1 (Brandschutzordnung): Eine Brandschutzordnung ist aufzustellen. " * 4
             + "\nAuflage 4 (Sicherheitsbeleuchtung): Die Betriebsdauer beträgt mindestens 8 Stunden.\n"
             + "Auflage 5 (Feuerlöscher): Feuerlöscher sind bereitzustellen. " * 3)
    src = {"n": 1, "kind": "document", "doc_id": 1, "mail_id": None, "chunk_id": 2, "project": "BS-1",
           "label": "Baugenehmigung.pdf, S. 2", "text": chunk,
           "snippet": "Auflage 4 (Sicherheitsbeleuchtung): Die Betriebsdauer beträgt mindestens 8 Stunden."}
    result = AskResult(question="Wie lange muss das Notlicht funktionieren?", project="BS-1",
                       answer="Mindestens 8 Stunden [1].", found=True, status="answered", sources=[src],
                       context=[src], notes=[])
    html = templates.get_template("_answer.html").render(result=result, question=result.question)
    marked = re.search(r"<blockquote>(.*?)</blockquote>", html, re.S).group(1)
    assert "<mark>Sicherheitsbeleuchtung</mark>" in marked  # synonym of "Notlicht"
    quote = re.sub(r"</?mark>", "", marked)
    assert quote.startswith("… Auflage 4 (Sicherheitsbeleuchtung)") and "8 Stunden" in quote
    assert quote.endswith(" …") and "Brandschutzordnung" not in quote
    # without a passage (older results) the snippet is cut around the question's words
    src2 = {**src, "snippet": "", "text": chunk.replace("Notlicht", "")}
    result2 = AskResult(question="Wie lange muss die Sicherheitsbeleuchtung funktionieren?", project="BS-1",
                        answer="x", found=False, status="not_found", sources=[], context=[src2], notes=[])
    html2 = templates.get_template("_answer.html").render(result=result2, question=result2.question)
    assert "<mark>Sicherheitsbeleuchtung</mark>" in html2 and "8 Stunden" in html2


def test_passage_markers():
    full = "Erster Satz.\nZweiter Satz mit 8 Stunden.\nDritter Satz."
    assert passage("Zweiter Satz mit 8 Stunden.", full) == "… Zweiter Satz mit 8 Stunden. …"
    assert passage("Erster Satz.", full) == "Erster Satz. …"
    assert passage("...Dritter Satz.", full) == "… Dritter Satz."
    assert passage("| Kennwert | Wert |\n|---|---|\n| Ebenen | 2 |", "") == "Ebenen: 2"
    assert passage("anders formuliert …", full) == "anders formuliert …"  # not found: kept as given
    assert passage(None, full, "Dritter") == full
    # a plain chunk start without the question's words (older results): the passage around them
    long = "Allgemeines zum Vorhaben. " * 20 + "Nächste Besprechung: 01.05.2025."
    assert "01.05.2025" in passage(long[:400], long, "Wann ist die nächste Besprechung?")
    assert passage(long[:400], long, "Allgemeines").startswith("Allgemeines zum Vorhaben.")


# --------------------------------------------------------------------------- highlighting
def test_highlight_identifiers_dates_and_compounds():
    out = str(highlight("Stand: 09.01.2024 – BS-2024-0137_Plan.pdf, BS-2024-0137.", "BS-2024-0137"))
    assert out.count("<mark>BS-2024-0137</mark>") == 2 and "<mark>2024</mark>" not in out
    assert "<mark>2024-0137</mark>" in str(highlight("BS-2024-0137", "2024-0137"))
    assert "<mark>" not in str(highlight("am 12.03.2024", "2024"))
    assert str(highlight("Das Jahr 2024", "2024")) == "Das Jahr <mark>2024</mark>"
    out = str(highlight("Löschwasser 48 m³/h; Löschwasserteich; Teich", "Löschwasserteich"))
    assert "<mark>Löschwasser</mark>" not in out and "<mark>Löschwasserteich</mark>" in out
    assert "<mark>" not in str(highlight("Die Anlage im Raum", "Brandmeldeanlage Treppenraum"))
    assert "<mark>Besprechung</mark>" in str(highlight("Nächste Besprechung: 01.05.2025", "Baubesprechung"))
    assert "<mark>Brandmeldeanlage</mark>" in str(highlight("Eine Brandmeldeanlage ist vorhanden.", "BMA"))
    out = str(highlight("DIN EN 62305-3 und T90-RS", "62305 T90"))
    assert "<mark>62305</mark>-3" in out and "<mark>T90</mark>-RS" in out
    assert "&lt;b&gt;" in str(highlight("<b>Treppe</b>", "Treppe"))


def test_query_matcher_identifiers():
    m = QueryMatcher("Brandwand BS-2023-0777 1")
    assert ("bs", "2023", "0777") in m.strong_identifiers and ("1",) not in m.strong_identifiers
    assert m.has_identifier("Detail zu BS-2023-0777_Brandwand.pdf")
    assert not m.has_identifier("BS-2023-07770", "Stand 2023-0777x")
    assert QueryMatcher("BA-412/2023").has_identifier("Aktenzeichen: BA-412/2023    Ihr Zeichen")


# --------------------------------------------------------------------------- clean document view
def test_plain_text_and_chunk_view_without_markdown():
    md = "# Prüfbericht Nr. 1\n| Angabe | Inhalt |\n|---|---|\n| Projekt | BS-1 Kita |\n| Datum | 17.04.2023 |"
    assert plain_text(md) == "Prüfbericht Nr. 1\nProjekt: BS-1 Kita\nDatum: 17.04.2023"
    docx = "# 2 Kennwerte\nKennwert | Wert\nEinstellplätze | 240\nEbenen | 2"
    assert plain_text(docx) == "2 Kennwerte\nEinstellplätze: 240\nEbenen: 2"
    wide = "Raum\n| Raum | Fläche | Nutzung |\n|---|---|---|\n| 1.01 | 20 m² | Büro |"
    assert plain_text(wide).splitlines()[1] == "Raum: 1.01 · Fläche: 20 m² · Nutzung: Büro"
    assert plain_text("- **Auflage 4**: siehe [Plan](plan.pdf)\n---\n> `T90`") == "• Auflage 4: siehe Plan\nT90"
    assert plain_text("Gruß\nTel. 0511 1 | Fax 0511 2") == "Gruß\nTel. 0511 1 · Fax 0511 2"  # no table
    assert plain_text("| Bauherr: | Stadt |\n| Ort | Musterstadt |") == "Bauherr: Stadt\nOrt: Musterstadt"
    view = chunk_view({"id": 1, "page": None, "heading": "2 Kennwerte", "text": docx})
    html = str(view["html"])
    assert "<h3>2 Kennwerte</h3>" in html and view["context"] == ""  # heading shown once
    assert "<th>Kennwert</th>" in html and "<td>Einstellplätze</td><td>240</td>" in html
    assert "|" not in html and "#" not in html
    cont = chunk_view({"id": 2, "page": 3, "heading": "Auflagen", "text": "Auflage 9: Türen <T90>."})
    assert cont["context"] == "Seite 3 · Auflagen" and "&lt;T90&gt;" in str(cont["html"])


def test_document_pages_render_headings_and_tables(ro):
    docs = ro.get("/api/projects/BS-2022-0901/documents").json()["documents"]
    by_ext = {d["ext"]: d for d in docs}
    docx = ro.get(f"/dokumente/{by_ext['.docx']['id']}").text
    main = docx.split("<main>", 1)[1]
    assert "<h3>1 Allgemeines</h3>" in main and "# 1 Allgemeines" not in main
    assert main.count("1 Allgemeines") == 1  # no grey duplicate of the heading
    assert '<table class="doc-table">' in main and "Kennwert | Wert" not in main
    md = ro.get(f"/dokumente/{by_ext['.md']['id']}").text.split("<main>", 1)[1]
    assert "<th>Angabe</th>" in md and "|---|" not in md and "## " not in md and "<h4>Feststellungen</h4>" in md
    pdf = ro.get(f"/dokumente/{by_ext['.pdf']['id']}").text
    assert "Seite 2" in pdf and "Auflage 7" in pdf


# --------------------------------------------------------------------------- original files
def test_original_file_address_ends_with_the_file_name(ro):
    docs = ro.get("/api/projects/BS-2024-0137/documents").json()["documents"]
    pdf = next(d for d in docs if d["ext"] == ".pdf")
    page = ro.get(f"/dokumente/{pdf['id']}").text
    url = f"/dokumente/{pdf['id']}/datei/Baugenehmigung.pdf"
    assert f'href="{url}">Originaldatei öffnen' in page
    r = ro.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert 'filename="Baugenehmigung.pdf"' in r.headers["content-disposition"]
    assert ro.get(f"/dokumente/{pdf['id']}/datei").status_code == 200  # old links keep working
    assert ro.get("/dokumente/999999/datei/x.pdf", headers={"Accept": "text/html"}).status_code == 404
    assert file_url(3, "Stellungnahme Feuerwehr.pdf") == "/dokumente/3/datei/Stellungnahme%20Feuerwehr.pdf"
    assert file_url(3, "a/b?.pdf") == "/dokumente/3/datei/a%2Fb%3F.pdf" and file_url(3, "") == "/dokumente/3/datei"
    mails = ro.get("/api/mails", params={"q": "Stellungnahme Brandschutzdienststelle", "limit": 1}).json()
    mid = mails["mails"][0]["id"]
    mail = ro.get(f"/mails/{mid}").text
    m = re.search(r'href="(/dokumente/\d+/datei/Stellungnahme%20Feuerwehr\.pdf)">Datei öffnen', mail)
    assert m and ro.get(m.group(1)).content.startswith(b"%PDF")


# --------------------------------------------------------------------------- start page, ask page
def test_ask_button_on_the_start_page_keeps_the_typed_question(ro):
    start = ro.get("/").text
    form = re.search(r'<form action="/suche".*?</form>', start, re.S).group(0)
    assert '<button class="secondary" formaction="/fragen" formmethod="post">Frage stellen</button>' in form
    question = "Was fordert Auflage 7 der Baugenehmigung?"
    page = ro.post("/fragen", data={"q": question}).text  # what the button sends
    assert f'value="{question}"' in page and 'class="answer' in page
    assert f'value="{question}"' in ro.get("/fragen", params={"q": question}).text


# --------------------------------------------------------------------------- mobile
def test_mobile_tables_keep_words_and_status_details():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r"table\.list th, table\.list td \{[^}]*overflow-wrap: break-word;[^}]*hyphens: auto", css)
    mobile = css.split("@media (max-width: 700px)", 1)[1].split("\n}\n", 1)[0]
    assert "table.list .hide-sm { display: none; }" in mobile and ".show-sm { display: block; }" in mobile
    assert "table.list.deps .keep-sm { display: table-cell; }" in mobile
    deps = templates.get_template("_deps.html").render(deps={"ai_mode": "fake", "services": {
        "llm": {"ok": True, "format": "openai-chat", "latency_ms": 2.0}}})
    details = re.search(r'<td class="small keep-sm">(.*?)</td>', deps, re.S).group(1)
    assert "simuliert" in details


def test_project_page_moves_secondary_columns_on_mobile(ro):
    mails = ro.get("/projekte/BS-2024-0137", params={"tab": "mails"}).text.split('<div id="inhalt">', 1)[1]
    assert '<th class="w-l hide-sm">Von</th>' in mails and '<span class="show-sm muted small">' in mails
    assert '<td class="date">' in mails
    atts = ro.get("/projekte/BS-2024-0137", params={"tab": "anhaenge"}).text.split('<div id="inhalt">', 1)[1]
    assert '<span class="show-sm small">aus der Mail <a href="/mails/' in atts
