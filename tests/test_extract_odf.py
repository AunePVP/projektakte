"""OpenDocument (.odt, .ods, .odp, templates) and OpenOffice.org 1.x (.sxw, .sxc, .sxi) files, built here from
ZIP and XML strings: their text comes out like that of a Word file – headings, paragraphs, lists, tables as
rows, spreadsheets by sheet, slides as pages – and they are read only within bounds: zip bombs, many members,
millions of repeated cells, deep nesting, DTDs with entities and password-protected files are refused or cut
short with a note, never retried; broken files are recorded like other broken files. They are read in the
extraction worker, and an .odt in a project (or attached to a mail) is found by a question."""
import io
import os
import sqlite3
import time
import zipfile
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from projektakte import extract as extract_mod
from projektakte import documents, extract_worker
from projektakte.ai.services import AIServices
from projektakte.ask import ask
from projektakte.chunking import chunk_pages
from projektakte.extract import (
    ODF_DTD_TEXT,
    ODF_ENCRYPTED_TEXT,
    ODF_EXT,
    STRUCTURED_EXT,
    SUPPORTED_EXT,
    Page,
    extract,
)
from projektakte.extract_worker import ExtractWorker, Limits
from projektakte.indexer import run_index
from projektakte.search import hybrid_search

from .conftest import fake_settings

ODF = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "officeooo": "http://openoffice.org/2009/office",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xlink": "http://www.w3.org/1999/xlink",
}
OOO = {  # OpenOffice.org 1.x / StarOffice 6–7
    "office": "http://openoffice.org/2000/office",
    "style": "http://openoffice.org/2000/style",
    "text": "http://openoffice.org/2000/text",
    "table": "http://openoffice.org/2000/table",
    "draw": "http://openoffice.org/2000/drawing",
    "presentation": "http://openoffice.org/2000/presentation",
    "svg": "http://www.w3.org/2000/svg",
    "dc": "http://purl.org/dc/elements/1.1/",
}
MIMETYPES = {"text": "application/vnd.oasis.opendocument.text",
             "spreadsheet": "application/vnd.oasis.opendocument.spreadsheet",
             "presentation": "application/vnd.oasis.opendocument.presentation"}
MANIFEST_NS = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"


def content_xml(body: str, kind: str = "text", styles: str = "", prolog: str = "") -> str:
    decl = " ".join(f'xmlns:{p}="{u}"' for p, u in ODF.items())
    return (f'<?xml version="1.0" encoding="UTF-8"?>{prolog}<office:document-content {decl} office:version="1.3">'
            f"<office:automatic-styles>{styles}</office:automatic-styles>"
            f"<office:body><office:{kind}>{body}</office:{kind}></office:body></office:document-content>")


def legacy_xml(body: str, kind: str = "text") -> str:
    """content.xml as OpenOffice.org 1.x wrote it: other namespaces, an external DTD, the kind on the root."""
    decl = " ".join(f'xmlns:{p}="{u}"' for p, u in OOO.items())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE office:document-content PUBLIC '
            '"-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "office.dtd">\n'
            f'<office:document-content {decl} office:class="{kind}" office:version="1.0">'
            f"<office:automatic-styles/><office:body>{body}</office:body></office:document-content>")


def manifest(*entries: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><manifest:manifest xmlns:manifest="{MANIFEST_NS}" '
            'manifest:version="1.3"><manifest:file-entry manifest:full-path="/" '
            f'manifest:media-type="application/vnd.oasis.opendocument.text"/>{"".join(entries)}</manifest:manifest>')


def entry(path: str, encrypted: bool = False) -> str:
    data = ('<manifest:encryption-data manifest:checksum-type="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0'
            '#sha256-1k" manifest:checksum="AAAA"><manifest:algorithm manifest:algorithm-name="http://www.w3.org/'
            '2001/04/xmlenc#aes256-cbc" manifest:initialisation-vector="AAAA"/><manifest:key-derivation '
            'manifest:key-derivation-name="PBKDF2" manifest:key-size="32" manifest:iteration-count="100000" '
            'manifest:salt="AAAA"/></manifest:encryption-data>') if encrypted else ""
    return (f'<manifest:file-entry manifest:full-path="{path}" manifest:media-type="text/xml">{data}'
            "</manifest:file-entry>")


def package(content: str | bytes | None, *, kind: str = "text", manifest_xml: str | None = None,
            extra: dict[str, bytes | str] | None = None) -> bytes:
    """An OpenDocument package: mimetype first and stored, then content.xml, the manifest and further members."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), MIMETYPES[kind], compress_type=zipfile.ZIP_STORED)
        if content is not None:
            z.writestr("content.xml", content, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("META-INF/manifest.xml", manifest_xml or manifest(entry("content.xml")),
                   compress_type=zipfile.ZIP_DEFLATED)
        for name, data in (extra or {}).items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def odt(body: str, styles: str = "") -> bytes:
    return package(content_xml(body, styles=styles))


def ods(body: str) -> bytes:
    return package(content_xml(body, "spreadsheet"), kind="spreadsheet")


def odp(body: str) -> bytes:
    return package(content_xml(body, "presentation"), kind="presentation")


def p(text: str, style: str | None = None) -> str:
    return f'<text:p text:style-name="{style}">{text}</text:p>' if style else f"<text:p>{text}</text:p>"


def h(text: str, level: int = 1) -> str:
    return f'<text:h text:outline-level="{level}">{text}</text:h>'


def cell(text: str | None = None, repeat: int | None = None, attrs: str = "") -> str:
    rep = f' table:number-columns-repeated="{repeat}"' if repeat else ""
    inner = p(text) if text is not None else ""
    return f"<table:table-cell{rep}{attrs}>{inner}</table:table-cell>" if inner else f"<table:table-cell{rep}{attrs}/>"


def row(*cells: str, repeat: int | None = None) -> str:
    rep = f' table:number-rows-repeated="{repeat}"' if repeat else ""
    return f"<table:table-row{rep}>{''.join(cells)}</table:table-row>"


def table(name: str, *rows: str) -> str:
    return f'<table:table table:name="{name}">{"".join(rows)}</table:table>'


def note(citation: str, text: str, cls: str = "footnote") -> str:
    return (f'<text:note text:id="n{citation}" text:note-class="{cls}"><text:note-citation>{citation}'
            f"</text:note-citation><text:note-body>{p(text)}</text:note-body></text:note>")


def read(name: str, data: bytes):
    return extract(Path(name), data)


TITLE_STYLE = '<style:style style:name="P1" style:family="paragraph" style:parent-style-name="Title"/>'
CONCEPT = "".join([
    # deleted with change tracking: kept by the file, not part of the text
    '<text:tracked-changes><text:changed-region text:id="c1"><text:deletion><office:change-info>'
    "<dc:creator>Anna</dc:creator><dc:date>2024-03-01T10:00:00</dc:date></office:change-info>"
    f'{p("Eine Sprinkleranlage ist vorgesehen.")}</text:deletion></text:changed-region></text:tracked-changes>',
    '<text:sequence-decls><text:sequence-decl text:display-outline-level="0" text:name="Abbildung"/>'
    "</text:sequence-decls>",
    p("Brandschutzkonzept Musterschule", "P1"),  # "Title" via an automatic style
    # a table of contents repeats the headings: left out
    '<text:table-of-content text:name="Inhaltsverzeichnis1"><text:table-of-content-source/><text:index-body>'
    f'<text:index-title>{p("Inhaltsverzeichnis")}</text:index-title>{p("4 Löschwasser<text:tab/>7")}'
    "</text:index-body></text:table-of-content>",
    h("4 Löschwasser"),
    p("Der Löschwasserbedarf beträgt <text:span>96 m³/h</text:span> über 2 Stunden."
      + note("1", "Nach DVGW W 405.")),
    h("4.1 Hydranten", 2),
    # the deletion mark and an insertion: the text after the changes; a comment is left out
    p('Zwei Hydranten<text:change text:change-id="c1"/> im Abstand von <text:change-start text:change-id="c2"/>'
      'höchstens <text:change-end text:change-id="c2"/>75 m.<office:annotation><dc:creator>Bernd</dc:creator>'
      f'{p("Mit der Feuerwehr klären")}</office:annotation>'),
    '<text:list text:style-name="L1"><text:list-item>', p("Erste Auflage"),
    "<text:list><text:list-item>", p("Unterpunkt a"), "</text:list-item></text:list></text:list-item>",
    "<text:list-item>", p("Zweite Auflage"), p("mit Erläuterung"), "</text:list-item></text:list>",
    '<table:table table:name="Tabelle1"><table:table-column table:number-columns-repeated="2"/>',
    "<table:table-header-rows>", row(cell("Bauteil"), cell("Anforderung")), "</table:table-header-rows>",
    row(cell("Treppenraumwand"), f"<table:table-cell>{p('F 90')}{p('(feuerbeständig)')}</table:table-cell>"),
    row(f'<table:table-cell table:number-columns-spanned="2">{p("Gesamt")}</table:table-cell>',
        "<table:covered-table-cell/>"),
    row(cell(), cell()),  # empty row: nothing
    "</table:table>",
    p("Ende<text:tab/>Seite<text:s text:c=\"3\"/>2<text:line-break/>Zweite Zeile"),
])
CONCEPT_TEXT = ("# Brandschutzkonzept Musterschule\n\n"
                "# 4 Löschwasser\n"
                "Der Löschwasserbedarf beträgt 96 m³/h über 2 Stunden.\n"
                "Fußnote 1: Nach DVGW W 405.\n\n"
                "## 4.1 Hydranten\n"
                "Zwei Hydranten im Abstand von höchstens 75 m.\n"
                "- Erste Auflage\n"
                "- Unterpunkt a\n"
                "- Zweite Auflage\n"
                "mit Erläuterung\n"
                "Bauteil | Anforderung\n"
                "Treppenraumwand | F 90 (feuerbeständig)\n"
                "Gesamt\n"
                "Ende\tSeite   2\nZweite Zeile")


# -- formats ------------------------------------------------------------------------------------------------------
def test_formats_are_supported_and_read_in_the_worker():
    assert {".odt", ".ott", ".ods", ".odp", ".sxw", ".sxc", ".sxi"} <= ODF_EXT
    assert ODF_EXT <= SUPPORTED_EXT and ODF_EXT <= STRUCTURED_EXT
    assert ".sdw" not in SUPPORTED_EXT  # StarOffice 5 binary: another format
    worker = SimpleNamespace()  # _takes only looks at the extension and the model
    assert extract_worker._takes(worker, "Konzept.ODT", None) and extract_worker._takes(worker, "a.sxc", None)


def test_text_document_reads_like_a_word_file():
    r = read("Brandschutzkonzept.odt", odt(CONCEPT, TITLE_STYLE))
    assert (r.status, r.message, r.incomplete, r.retry) == ("ok", None, False, False)
    assert [p.number for p in r.pages] == [None]
    assert r.text == CONCEPT_TEXT
    for left_out in ("Sprinkler", "Inhaltsverzeichnis", "Feuerwehr klären", "\t7"):
        assert left_out not in r.text
    # headings start sections, also below the outline level 1
    assert [c.heading for c in chunk_pages(r.pages)] == ["4 Löschwasser", "4.1 Hydranten"]
    # a template is the same format
    assert read("Vorlage.ott", odt(CONCEPT, TITLE_STYLE)).text == CONCEPT_TEXT


def test_white_space_soft_hyphens_and_breaks():
    body = (
        "<text:p>\n    Rettungs­weg  über\n    <text:span> den </text:span> Flur&#173;bereich\n</text:p>"
        "<text:p>Breite:<text:s/>1,20<text:s text:c=\"2\"/>m<text:tab/>(lichte Breite)</text:p>"
        "<text:p>Zeile eins<text:line-break/>Zeile zwei</text:p>"
        '<text:p>Viele<text:s text:c="1000000"/>Leerzeichen</text:p>'  # capped, never a megabyte of spaces
        "<text:p>  <text:soft-page-break/>  </text:p>"  # nothing but white space: no line
        '<text:h text:outline-level="2">Kapitel<text:tab/>3<text:line-break/>Brandabschnitte</text:h>'
    )
    r = read("a.odt", odt(body))
    assert r.text.split("\n") == [
        "Rettungsweg über den Flurbereich", "Breite: 1,20  m\t(lichte Breite)", "Zeile eins", "Zeile zwei",
        "Viele" + " " * 10 + "Leerzeichen", "", "## Kapitel 3 Brandabschnitte"]


def test_footnotes_endnotes_and_frames_follow_their_paragraph():
    body = (
        h(f"Rettungswege{note('*', 'Stand der MBO 2019.')}")
        + p(f"Die Länge beträgt 35 m{note('2', 'Gemessen in der Lauflinie.')} bis zum Ausgang"
            f"{note('i', 'Siehe Anhang.', 'endnote')}.")
        # a text frame anchored in the paragraph (a caption): a line of its own after the paragraph
        + p('Siehe Plan.<draw:frame draw:name="Bild1"><svg:title>Alternativtext</svg:title><draw:text-box>'
            '<text:p>Abbildung 1: Fluchtwegplan EG<draw:frame><draw:image xlink:href="Pictures/1.png">'
            "<office:binary-data>QUJD</office:binary-data></draw:image></draw:frame></text:p>"
            "</draw:text-box></draw:frame>")
    )
    r = read("a.odt", odt(body))
    assert r.text == ("# Rettungswege\nFußnote *: Stand der MBO 2019.\n"
                      "Die Länge beträgt 35 m bis zum Ausgang.\nFußnote 2: Gemessen in der Lauflinie.\n"
                      "Endnote i: Siehe Anhang.\nSiehe Plan.\nAbbildung 1: Fluchtwegplan EG")
    assert "m2" not in r.text and "Alternativtext" not in r.text and "QUJD" not in r.text


def test_lists_with_stated_numbers_headers_and_numbered_paragraphs():
    body = (
        "<text:list><text:list-header>" + p("Auflagen der Genehmigung:") + "</text:list-header>"
        "<text:list-item><text:number>1.</text:number>" + p("Rauchmelder in allen Räumen") + "</text:list-item>"
        "<text:list-item><text:list><text:list-item>" + p("Ebene zwei ohne Text davor") + "</text:list-item>"
        "</text:list>" + p("weiter in Ebene eins") + "</text:list-item></text:list>"
        "<text:numbered-paragraph><text:number>3)</text:number>" + p("Nummerierter Absatz")
        + "</text:numbered-paragraph>"
        '<text:h text:outline-level="1"><text:number>5.</text:number>Technik</text:h>'
    )
    assert read("a.odt", odt(body)).text == ("Auflagen der Genehmigung:\n1. Rauchmelder in allen Räumen\n"
                                             "- Ebene zwei ohne Text davor\nweiter in Ebene eins\n"
                                             "3) Nummerierter Absatz\n\n# 5. Technik")


def test_tables_nested_and_in_lists():
    inner = table("Innen", row(cell("a"), cell("b")))
    body = ("<text:list><text:list-item>" + p("Punkt") + table("T", row(cell("x"), cell("y"))) + "</text:list-item>"
            "</text:list>" + table("Aussen", row(f"<table:table-cell>{inner}</table:table-cell>", cell("c"))))
    assert read("a.odt", odt(body)).text == "- Punkt\nx | y\na | b | c"


# -- spreadsheets -----------------------------------------------------------------------------------------------
def test_spreadsheet_sheets_rows_values_and_repeated_empty_cells():
    full = 16384  # columns of a LibreOffice sheet; the rest of a sheet is one empty row repeated
    body = (
        table("Löschwasser",
              '<table:table-column table:number-columns-repeated="16384"/>',
              row(cell("Bedarf", attrs=' office:value-type="string"'),
                  cell("96", attrs=' office:value-type="float" office:value="96" table:formula="of:=48*2"'),
                  cell(repeat=full - 2)),
              row(cell(repeat=full), repeat=3),
              # values without displayed text (written by other programs): the value
              row(cell(attrs=' office:value-type="percentage" office:value="0.15"'),
                  cell(attrs=' office:value-type="date" office:date-value="2024-03-12"'),
                  cell(attrs=' office:value-type="boolean" office:boolean-value="true"'),
                  "<table:covered-table-cell><text:p>verdeckt</text:p></table:covered-table-cell>",
                  cell(repeat=full - 4)),
              row(cell(repeat=full), repeat=1_048_570))
        + table("Leer", row(cell(repeat=full), repeat=1_048_576))
        + table("Prüfung", '<table:shapes><draw:frame><draw:text-box><text:p>Hinweisfeld</text:p></draw:text-box>'
                           "</draw:frame></table:shapes>",
                row(cell("Mängel"), "<table:table-cell>" + p(
                    "keine<office:annotation>" + p("Kommentar im Feld") + "</office:annotation>")
                    + "</table:table-cell>"))
        + "<table:named-expressions><table:named-range table:name=\"Summe\"/></table:named-expressions>"
        "<table:content-validations><table:content-validation table:name=\"v1\"><table:help-message>"
        f"{p('Nur Zahlen eingeben')}</table:help-message></table:content-validation></table:content-validations>"
    )
    started = time.monotonic()
    r = read("Berechnung.ods", ods(body))
    assert time.monotonic() - started < 2
    assert r.status == "ok" and r.pages[0].number is None
    assert r.text == ("# Löschwasser\nBedarf | 96\n0.15 | 2024-03-12 | true\n\n"
                      "# Prüfung\nHinweisfeld\nMängel | keine")
    assert [c.heading for c in chunk_pages(r.pages)] == ["Löschwasser", "Prüfung"]


def test_repeated_non_empty_cells_and_rows_are_capped():
    started = time.monotonic()
    r = read("a.ods", ods(table("T", row(cell("x", repeat=16384), cell("y")),
                                row(cell("z", repeat=3), repeat=1_048_576))))
    assert time.monotonic() - started < 2
    lines = r.text.split("\n")
    assert lines[0] == "# T" and lines[1] == " | ".join(["x"] * 100 + ["y"])
    assert lines[2:] == ["z | z | z"] * 100 and not r.incomplete


def test_text_beyond_the_limit_is_cut_short_with_a_note(monkeypatch):
    monkeypatch.setattr(extract_mod, "MAX_ODF_TEXT_CHARS", 5000)
    rows = "".join(row(cell(f"Zeile {i} mit etwas Text")) for i in range(2000))
    r = read("a.ods", ods(table("Viel", rows)))
    assert r.status == "ok" and r.incomplete and not r.retry
    assert r.message == "Text nach 5.000 Zeichen gekürzt (Grenze je Datei)"
    assert r.text.startswith("# Viel\nZeile 0 mit etwas Text") and len(r.text) <= 5000
    # repetitions are counted before they are expanded
    huge = read("b.ods", ods(table("T", row(cell("x" * 400, repeat=100), repeat=100))))
    assert huge.incomplete and len(huge.text) <= 5000


# -- presentations ----------------------------------------------------------------------------------------------
def test_presentation_slides_are_pages_with_their_number():
    body = (
        '<draw:page draw:name="Folie1">'
        '<draw:frame presentation:class="title"><draw:text-box>' + p("Rettungswege") + "</draw:text-box></draw:frame>"
        '<draw:frame presentation:class="outline"><draw:text-box><text:list><text:list-item>'
        + p("Zwei bauliche Rettungswege") + "</text:list-item></text:list></draw:text-box></draw:frame>"
        "<presentation:notes><draw:page-thumbnail/><draw:frame presentation:class=\"notes\"><draw:text-box>"
        + p("Auf den Treppenraum hinweisen.") + "</draw:text-box></draw:frame></presentation:notes></draw:page>"
        '<draw:page draw:name="Folie2"><draw:frame presentation:class="title"><draw:text-box/></draw:frame></draw:page>'
        '<draw:page draw:name="Folie3"><draw:custom-shape>' + p("Sammelstelle Parkplatz") + "</draw:custom-shape>"
        "<draw:frame>" + table("T", row(cell("A"), cell("B"))) + "</draw:frame>"
        "<officeooo:annotation><dc:creator>C</dc:creator>" + p("Kommentar") + "</officeooo:annotation></draw:page>"
    )
    r = read("Schulung.odp", odp(body))
    assert r.status == "ok"
    assert r.pages == [
        Page(1, "# Rettungswege\n- Zwei bauliche Rettungswege\nNotizen:\nAuf den Treppenraum hinweisen."),
        Page(3, "Sammelstelle Parkplatz\nA | B")]
    chunks = chunk_pages(r.pages)
    assert [(c.page, c.heading) for c in chunks] == [(1, "Rettungswege"), (3, "Rettungswege")]


# -- OpenOffice.org 1.x -----------------------------------------------------------------------------------------
def test_openoffice_1_files_are_read_the_same_way():
    text_body = (
        '<text:sequence-decls/><text:h text:level="2">Rettungswege</text:h>'
        "<text:p>Breite<text:tab-stop/>1,20 m<text:footnote text:id=\"ftn1\"><text:footnote-citation>1"
        "</text:footnote-citation><text:footnote-body>" + p("MBO § 35")
        + "</text:footnote-body></text:footnote></text:p>"
        "<text:ordered-list><text:list-item>" + p("Punkt eins") + "</text:list-item></text:ordered-list>"
        "<text:unordered-list><text:list-item>" + p("Punkt zwei") + "</text:list-item></text:unordered-list>"
        + table("T", row(cell("3", attrs=' table:value-type="float" table:value="3"'), cell("Ausgänge")))
        + "<text:p>Undeklariert: &nbsp;Ende</text:p>"  # declared in the external DTD only: left out
    )
    sxw = package(legacy_xml(text_body))
    assert read("Schreiben.sxw", sxw).text == ("## Rettungswege\nBreite\t1,20 m\nFußnote 1: MBO § 35\n- Punkt eins\n"
                                               "- Punkt zwei\n3 | Ausgänge\nUndeklariert: Ende")
    assert read("Vorlage.stw", sxw).status == "ok"
    sheet = table("Tabelle1", row(cell(attrs=' table:value-type="float" table:value="12.5"'), cell("m³")),
                  row(cell(repeat=256), repeat=31999))
    assert read("Liste.sxc", package(legacy_xml(sheet, "spreadsheet"))).text == "# Tabelle1\n12.5 | m³"
    slides = ('<draw:page draw:name="Seite 1"><draw:text-box presentation:class="title">' + p("Brandschutz")
              + "</draw:text-box></draw:page>")
    assert read("Vortrag.sxi", package(legacy_xml(slides, "presentation"))).pages == [Page(1, "# Brandschutz")]


# -- bounds and refusals ----------------------------------------------------------------------------------------
def test_zip_bomb_is_decompressed_only_up_to_the_bound(monkeypatch):
    monkeypatch.setattr(extract_mod, "MAX_ODF_XML_BYTES", 1_000_000)
    bomb = content_xml(p(" " * 20_000_000))  # 20 MB of spaces: about 20 KB compressed
    data = package(bomb)
    assert len(data) < 100_000
    decompressed = []
    real_read = zipfile.ZipExtFile.read
    monkeypatch.setattr(zipfile.ZipExtFile, "read", lambda self, *a: decompressed.append(len(
        chunk := real_read(self, *a))) or chunk)
    r = read("bombe.odt", data)
    assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
    assert r.message == "Datei zu groß zum Lesen (Inhalt entpackt über 1 MB)"  # no text before the bound
    assert sum(decompressed) <= 1_000_000 + extract_mod._ODF_CHUNK + 10_000  # (+ the manifest)


def test_a_big_spreadsheet_is_read_up_to_the_bound(monkeypatch):
    """Lists with many rows (Raumbuch, LV, exports) beyond MAX_ODF_XML_BYTES: the rows before the bound are
    read, marked incomplete – not the whole file refused."""
    monkeypatch.setattr(extract_mod, "MAX_ODF_XML_BYTES", 200_000)
    rows = "".join(row(cell(f"Raum {i}"), cell("T30-RS")) for i in range(5000))
    r = read("Raumbuch.ods", ods(table("Räume", rows)))
    assert (r.status, r.incomplete, r.retry) == ("ok", True, False)
    assert r.message == "Inhalt entpackt über 200 KB – nur der Anfang gelesen"
    lines = r.text.splitlines()
    assert lines[:3] == ["# Räume", "Raum 0 | T30-RS", "Raum 1 | T30-RS"] and 100 < len(lines) < 5000


def test_compression_methods_other_than_odfs_are_refused():
    """bzip2/LZMA members: zipfile (3.12) decompresses a whole block of any size at once – not read."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text", zipfile.ZIP_STORED)
        z.writestr("content.xml", content_xml(p("Text")), zipfile.ZIP_BZIP2)
    r = read("a.odt", data.getvalue())
    assert (r.status, r.incomplete, r.retry) == ("skipped", False, False)
    assert r.message == "Datei nicht gelesen (Inhalt mit einer Kompression, die OpenDocument nicht kennt)"


def test_a_huge_unit_is_bounded(monkeypatch):
    """A paragraph of millions of empty elements (a few bytes of XML each) would be held in memory as a tree
    until it ends: the reading stops at MAX_ODF_UNIT_ITEMS, the text before is kept."""
    monkeypatch.setattr(extract_mod, "MAX_ODF_UNIT_ITEMS", 1000)
    r = read("a.odt", odt(p("Vorher") + p("<a/>" * 5000) + p("Nachher")))
    assert (r.status, r.text, r.incomplete, r.retry) == ("ok", "Vorher", True, False)
    assert r.message == ("Text ab einem Absatz bzw. einer Zeile oder Folie mit über 1.000 Elementen nicht gelesen "
                         "(Grenze je Abschnitt)")
    alone = read("b.odt", odt(p("<text:s/>" * 5000)))
    assert (alone.status, alone.incomplete, alone.retry) == ("skipped", True, False)
    assert alone.message == "Datei zu groß zum Lesen (ein Absatz bzw. eine Zeile oder Folie mit über 1.000 Elementen)"
    assert read("c.odt", odt(p("<text:span>x</text:span>" * 400))).text == "x" * 400  # 800 items: fine


def test_many_different_names_are_refused(monkeypatch):
    """The XML parser keeps every element and attribute name it meets for the whole file: different names
    are bounded (MAX_ODF_NAMES), also in left-out elements and outside paragraphs."""
    monkeypatch.setattr(extract_mod, "MAX_ODF_NAMES", 200)
    assert read("a.odt", odt(p("Text"))).text == "Text"
    for body in ("".join(f"<e{i}/>" for i in range(300)),  # outside paragraphs
                 p("".join(f'<text:span a{i}="">x</text:span>' for i in range(300))),  # attributes
                 "<office:annotation>" + "".join(f"<e{i}/>" for i in range(300)) + "</office:annotation>"):
        r = read("b.odt", odt(body))
        assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
        assert r.message == "Datei nicht gelesen (mehr als 200 verschiedene Element- und Attributnamen)"


def test_repeated_cells_have_a_budget_per_file(monkeypatch):
    """Repetitions multiply (a cell 100 times in a row repeated 100 times): the copies of one file stay
    within _ODF_REPEAT_CHARS, beyond a repeated cell or row is written once."""
    monkeypatch.setattr(extract_mod, "_ODF_REPEAT_CHARS", 10_000)
    big = "x" * 1000
    sheet = table("T", row(cell(big, repeat=100), repeat=100) + row(cell("ab", repeat=3)))
    r = read("a.ods", ods(sheet))
    assert (r.status, r.incomplete) == ("ok", False)
    assert r.text.splitlines()[1:] == [big, "ab | ab | ab"]  # one copy each; the small one still repeated


def test_size_is_also_checked_while_decompressing():
    """Should a package understate a member's size (zipfile itself stops at the stated size), the bytes
    actually decompressed count."""
    class Lying:
        def open(self, info):
            return io.BytesIO(b"<a>" + b" " * 5000 + b"</a>")

    parser = extract_mod._odf_parser(lambda *a: None)
    with pytest.raises(extract_mod._OdfRefused) as refused:
        extract_mod._parse_member(Lying(), SimpleNamespace(file_size=10), 1000, "Inhalt", parser)
    assert refused.value.message == "Datei zu groß zum Lesen (Inhalt entpackt über 1 KB)"


def test_too_many_members_are_refused_before_zipfile_reads_them(monkeypatch):
    monkeypatch.setattr(extract_mod, "MAX_ODF_MEMBERS", 5)
    many = package(content_xml(p("Text")), extra={f"Pictures/{i}.png": b"x" for i in range(10)})
    # 5 members, but a directory of more than 5 × 1 KB
    long_names = package(content_xml(p("Text")), extra={f"P{i}/" + "n" * 3000: b"x" for i in range(2)})
    assert read("a.odt", package(content_xml(p("Text")))).text == "Text"  # 3 members: fine
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: pytest.fail("zipfile darf das Verzeichnis nicht lesen"))
    # the end record followed by the longest trailing data zipfile still looks through (64 KB)
    far = many + b"\0" * (1 << 16)
    for data in (many, long_names, zip64_claiming(many, entries=50), far):
        r = read("a.odt", data)
        assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
        assert r.message == "Datei zu groß zum Lesen (mehr als 5 Bestandteile)"


def zip64_claiming(data: bytes, *, entries: int) -> bytes:
    """``data`` with a ZIP64 end record (which zipfile would use) before its end record, stating ``entries``."""
    end = data.rfind(b"PK\x05\x06")
    record = extract_mod._EOCD64.pack(b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, 100, 0)
    locator = b"PK\x06\x07" + (0).to_bytes(4, "little") + end.to_bytes(8, "little") + (1).to_bytes(4, "little")
    return data[:end] + record + locator + data[end:]


def test_deep_nesting_is_refused():
    deep = "<text:span>" * 150 + "tief" + "</text:span>" * 150
    r = read("a.odt", odt(p(deep)))
    assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
    assert r.message == "Datei nicht gelesen (mehr als 100 Ebenen verschachtelt)"
    fine = "<text:span>" * 90 + "noch lesbar" + "</text:span>" * 90
    assert read("b.odt", odt(p(fine))).text == "noch lesbar"


@pytest.mark.parametrize("prolog", [
    # billion laughs
    '<!DOCTYPE office:document-content [<!ENTITY a "lol"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
    '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;"><!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">]>',
    # an external entity (a file of the server)
    '<!DOCTYPE office:document-content [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
    # an internal subset without entities: LibreOffice never writes one
    '<!DOCTYPE office:document-content [<!ELEMENT office:document-content ANY>]>',
])
def test_dtd_with_entities_is_refused(prolog):
    body = p("Text &d;") if "&a;" in prolog else p("Text &xxe;") if "xxe" in prolog else p("Text")
    started = time.monotonic()
    r = read("a.odt", package(content_xml(body, prolog=prolog)))
    assert time.monotonic() - started < 1
    assert (r.status, r.message, r.incomplete, r.retry) == ("skipped", ODF_DTD_TEXT, False, False)


def test_dtd_in_the_manifest_is_refused():
    bad = manifest(entry("content.xml")).replace(
        "<manifest:manifest", '<!DOCTYPE manifest:manifest [<!ENTITY a "x">]><manifest:manifest', 1)
    r = read("a.odt", package(content_xml(p("Text")), manifest_xml=bad))
    assert (r.status, r.message) == ("skipped", ODF_DTD_TEXT)


def test_password_protected_files_are_not_read_and_not_retried():
    encrypted = os.urandom(600)  # what an encrypted content.xml looks like
    protected = package(encrypted, manifest_xml=manifest(entry("content.xml", encrypted=True),
                                                         entry("styles.xml", encrypted=True)))
    # ODF 1.4 encrypts the whole package into one member
    whole = package(None, manifest_xml=manifest(entry("encrypted-package", encrypted=True)),
                    extra={"encrypted-package": encrypted})
    for name, data in (("Geheim.odt", protected), ("Geheim.ods", whole), ("Geheim.sxw", protected)):
        r = read(name, data)
        assert (r.status, r.message, r.incomplete, r.retry, r.pages) == (
            "skipped", ODF_ENCRYPTED_TEXT, False, False, []), name
    # encrypted as a ZIP package: the flag in the local and the central header (zipfile cannot write it)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("content.xml", encrypted)
    zipped = bytearray(buf.getvalue())
    zipped[6] |= 0x1
    zipped[zipped.rfind(b"PK\x01\x02") + 8] |= 0x1
    assert read("a.odt", bytes(zipped)).message == ODF_ENCRYPTED_TEXT
    # a password only on the macros: the document is read
    macros = package(content_xml(p("Offener Text")), manifest_xml=manifest(
        entry("content.xml"), entry("Basic/Standard/Module1.xml", encrypted=True)),
        extra={"Basic/Standard/Module1.xml": encrypted})
    assert read("a.odt", macros).text == "Offener Text"


def test_broken_files_are_recorded_like_other_broken_files():
    broken_docx = read("Bericht.docx", b"PK\x03\x04 kaputt")
    assert (broken_docx.status, broken_docx.incomplete, broken_docx.retry) == ("error", True, True)
    valid = odt(p("Text"))
    no_content = io.BytesIO()
    with zipfile.ZipFile(no_content, "w") as z:
        z.writestr("mimetype", MIMETYPES["text"])
    stored = io.BytesIO()
    with zipfile.ZipFile(stored, "w") as z:
        z.writestr("content.xml", content_xml(p("Text")))  # uncompressed: the change below hits the data
    cases = {
        "kein ZIP": b"PK\x03\x04 kaputt",
        "abgeschnitten": valid[: len(valid) // 2],
        "ohne content.xml": no_content.getvalue(),
        "kaputtes XML": package("<office:document-content><text:p>offen"),
        "falsche Prüfsumme": stored.getvalue().replace(b"<text:p>Text", b"<text:p>Test"),
    }
    for what, data in cases.items():
        r = read("Konzept.odt", data)
        assert (r.status, r.incomplete, r.retry, r.pages) == ("error", True, True, []), what
        assert r.message, what
    assert read("a.odt", no_content.getvalue()).message == "ValueError: content.xml fehlt – keine OpenDocument-Datei"
    # a broken manifest says nothing about a password: content.xml decides
    assert read("a.odt", package(content_xml(p("Text")), manifest_xml="<kaputt")).text == "Text"


def test_empty_document_is_empty():
    r = read("leer.odt", odt(p("") + "<text:p>   </text:p>"))
    assert (r.status, r.message) == ("empty", "kein Text gefunden")


# -- worker, indexing and questions -----------------------------------------------------------------------------
def test_worker_reads_the_same(tmp_path):
    worker = ExtractWorker(Limits())
    try:
        for name, data in (("Konzept.odt", odt(CONCEPT, TITLE_STYLE)), ("a.ods", ods(table("T", row(cell("x"))))),
                           ("b.odp", odp('<draw:page>' + p("Folie") + "</draw:page>")),
                           ("Geheim.odt", package(os.urandom(100), manifest_xml=manifest(entry("content.xml", True)))),
                           ("kaputt.odt", b"PK\x03\x04 kaputt")):
            assert worker.extract(name, data, None) == extract(Path(name), data), name
        assert worker.started == 1 and worker.pid != os.getpid()
    finally:
        worker.close()
        extract_worker.shutdown()


def _mail_with(name: str, data: bytes) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "Bauamt <bauamt@stadt.example>", "buero@ib.example", "Stellungnahme 284201"
    msg["Date"], msg["Message-ID"] = "Mon, 3 Mar 2025 10:00:00 +0100", "<stellungnahme-284201@stadt.example>"
    msg.set_content("Anbei unsere Stellungnahme.")
    msg.add_attachment(data, maintype="application", subtype="vnd.oasis.opendocument.text", filename=name)
    return msg.as_bytes()


def test_odt_in_a_project_answers_a_question(fake_ai, tmp_path):
    """The office's share: 2/28/2842_<Bauvorhaben>/284201_<Auftrag>/284201-Text/…; read in the extraction
    worker, found by search and by a question, also as a mail attachment."""
    share = tmp_path / "projekt"
    order = share / "2/28/2842_Musterschule/284201_Brandschutzkonzept"
    (order / "284201-Text").mkdir(parents=True)
    (order / "284201-Tab").mkdir()
    (order / "284201-Text/Brandschutzkonzept.odt").write_bytes(odt(CONCEPT, TITLE_STYLE))
    (order / "284201-Tab/Rauchabzug.ods").write_bytes(ods(table(
        "Rauchabzug", row(cell("Treppenraum Nord"), cell("Öffnungsfläche 1,0 m²")),
        row(cell(repeat=1024), repeat=10**6))))
    (order / "284201-Text/Altes Schreiben.sxw").write_bytes(
        package(legacy_xml(p("Die Feuerwehrzufahrt ist 3,5 m breit."))))
    mails = tmp_path / "mails"
    mails.mkdir()
    (mails / "stellungnahme.eml").write_bytes(_mail_with("Stellungnahme.odt", odt(
        p("Die Brandmeldeanlage ist auf die Leitstelle aufzuschalten."))))
    s = fake_settings(fake_ai, tmp_path / "data", PROJECTS_DIR=share, MAIL_DIR=mails, PROJECT_NUMBER_PATTERN=r"\d{6}",
                      PROJECT_FOLDER_DEPTH=4, EXTRACT_ISOLATION=1)
    ai = AIServices.from_settings(s)
    try:
        run_index(s, ai)
        assert extract_worker._worker is not None and extract_worker._worker.started >= 1
    finally:
        extract_worker.shutdown()
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    try:
        docs = {r["filename"]: (r["status"], r["project"])
                for r in conn.execute("SELECT filename, status, project FROM documents")}
        assert docs == {"Brandschutzkonzept.odt": ("ok", "284201"), "Rauchabzug.ods": ("ok", "284201"),
                        "Altes Schreiben.sxw": ("ok", "284201"), "Stellungnahme.odt": ("ok", "284201")}
        result = ask(conn, ai, str(s.db_path), "Wie hoch ist der Löschwasserbedarf?", project="284201")
        assert result.found and "96 m³/h" in result.answer, result.answer
        assert result.sources[0]["label"].startswith("284201-Text/Brandschutzkonzept.odt")
        for query, path in (("Öffnungsfläche Treppenraum Nord", "284201-Tab/Rauchabzug.ods"),
                            ("Feuerwehrzufahrt Breite", "284201-Text/Altes Schreiben.sxw"),
                            ("Brandmeldeanlage Leitstelle aufschalten", "Stellungnahme.odt")):
            hits = hybrid_search(conn, ai, str(s.db_path), query, project="284201").hits
            # a mail attachment has no path in a project: its name is the title
            assert hits and (hits[0].rel_path or hits[0].title) == path, (query, [h.rel_path for h in hits])
    finally:
        conn.close()


def test_attachments_stored_before_odf_was_read_are_read_now(fake_ai, tmp_path, monkeypatch):
    """A mail attachment .odt imported by the previous version was stored as "Format .odt nicht unterstützt"
    (skipped, complete); the mail itself does not change, so it is read from the attachment cache once
    the format is read."""
    mails = tmp_path / "mails"
    mails.mkdir()
    (mails / "stellungnahme.eml").write_bytes(_mail_with("Stellungnahme.odt", odt(
        p("Die Brandmeldeanlage ist auf die Leitstelle aufzuschalten."))))
    (tmp_path / "projekt/2/28/2842_Musterschule/284201_Brandschutzkonzept").mkdir(parents=True)
    s = fake_settings(fake_ai, tmp_path / "data", PROJECTS_DIR=tmp_path / "projekt", MAIL_DIR=mails,
                      PROJECT_NUMBER_PATTERN=r"\d{6}", PROJECT_FOLDER_DEPTH=4)
    ai = AIServices.from_settings(s)
    query = "SELECT filename, status, message, incomplete FROM documents WHERE mail_id IS NOT NULL"
    with monkeypatch.context() as before:  # the previous version
        before.setattr(extract_mod, "SUPPORTED_EXT", extract_mod.SUPPORTED_EXT - ODF_EXT)
        run_index(s, ai)
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [tuple(r) for r in conn.execute(query)]
        assert rows == [("Stellungnahme.odt", "skipped", "Format .odt nicht unterstützt", 0)]
        run_index(s, ai)
        assert [tuple(r) for r in conn.execute(query)] == [("Stellungnahme.odt", "ok", None, 0)]
        hits = hybrid_search(conn, ai, str(s.db_path), "Brandmeldeanlage Leitstelle aufschalten", project="284201").hits
        assert hits and hits[0].title == "Stellungnahme.odt"
        assert documents.mark_newly_readable_attachments(conn) == 0  # once only
    finally:
        conn.close()
