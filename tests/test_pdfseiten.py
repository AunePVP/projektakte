"""The PDF reader on PDFium alone (pdfseiten.py): the text of a page as a viewer's text selection gives it, its
lines with their place on the rendered page, whether the page is a scan – and the page image, from the same
engine."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from projektakte import pdfseiten

from .pdfbau import Pdf, line, runs, scan_image, scan_pdf

A = "Die schadlose Direkteinleitung von Dachflächenwass"
B = "er in die Alte Dreisam ist der Gemeinde anzuzeigen."
# Helvetica: widths of the glyphs of A in 1/1000 em (to place the second run exactly where the first ends)
_W = {" ": 278, "D": 722, "i": 222, "e": 556, "s": 500, "c": 500, "h": 556, "a": 556, "d": 556, "l": 222, "o": 556,
      "r": 333, "k": 500, "t": 278, "n": 556, "u": 556, "g": 556, "v": 500, "f": 278, "ä": 556, "w": 722}


def _end(text: str, x: float, size: float, tc: float) -> float:
    return x + sum(_W[c] * size / 1000 + tc for c in text)


def read(data: bytes):
    result = pdfseiten.read(data, "test.pdf")
    assert result.problem is None
    return result.pages


def test_runs_inside_a_word_give_no_space():
    """Justified text: a line written as two runs, the second placed where the first ends, inside a word (the old
    reader gave "Dachflächenwass er")."""
    data = Pdf().page(runs(700, [(70, A), (_end(A, 70, 11, 0.6), B)], extra=b"0.6 Tc")).build()
    assert read(data)[0].text == A + B


def test_lines_and_paragraph_text():
    data = Pdf().page(line(70, 760, "Gewässerrandstreifen", size=14, bold=True),
                      line(70, 730, "8. Laut Leitfaden der LUBW ist die äußere Böschung zu wählen."),
                      line(70, 716, "Das gilt auch am Mühlbach (§ 38 Abs. 4 WHG).")).build()
    page = read(data)[0]
    assert page.text == ("Gewässerrandstreifen\n8. Laut Leitfaden der LUBW ist die äußere Böschung zu wählen.\n"
                         "Das gilt auch am Mühlbach (§ 38 Abs. 4 WHG).")
    assert [s.text for s in page.segments] == page.text.split("\n")
    assert page.scan is False


def test_hyphen_at_the_line_end_joins_the_word():
    data = Pdf().page(line(70, 730, "Im Falle mehrerer Böschungs-"), line(70, 716, "oberkanten gilt die äußere."),
                      line(70, 690, "Zuständig ist die Brandschutz-"), line(70, 676, "Dienststelle der Stadt.")).build()
    lines = read(data)[0].text.split(chr(10))
    assert lines == ["Im Falle mehrerer Böschungsoberkanten", "gilt die äußere.",
                     "Zuständig ist die Brandschutz-", "Dienststelle der Stadt."]  # a hyphen before a capital stays


def test_columns_on_one_line_are_separate_pieces():
    data = Pdf().page(line(70, 40, "Bankverbindung: Sparkasse", size=8), line(330, 40, "Servicezeiten: Montag", size=8),
                      line(70, 30, "IBAN: DE00 0000 0000", size=8), line(330, 30, "Dienstag: 08:30 Uhr", size=8)).build()
    page = read(data)[0]
    assert [s.text for s in page.segments] == ["Bankverbindung: Sparkasse", "Servicezeiten: Montag",
                                               "IBAN: DE00 0000 0000", "Dienstag: 08:30 Uhr"]
    left, right = page.segments[0], page.segments[1]
    assert left.x1 < 0.5 < right.x0 and abs(left.y0 - right.y0) < 0.005


def _ink_box(image: bytes) -> tuple[float, float, float, float]:
    """Where the rendered page is not white, relative to its size (left, top, right, bottom)."""
    img = Image.open(io.BytesIO(image)).convert("L")
    box = img.point(lambda v: 255 if v < 128 else 0).getbbox()
    assert box is not None
    w, h = img.size
    return box[0] / w, box[1] / h, box[2] / w, box[3] / h


@pytest.mark.parametrize("rotate, crop", [(0, None), (90, None), (180, None), (270, None),
                                          (0, (50, 100, 545, 742)), (90, (50, 100, 545, 742))])
def test_segment_boxes_match_the_rendered_page(rotate, crop):
    """The place of a line is given as on the page image the model gets: turned by /Rotate, inside the crop box."""
    data = Pdf().page(line(120, 600, "Brandschutzkonzept Musterstadt", size=20, bold=True),
                      rotate=rotate, crop=crop).build()
    seg = read(data)[0].segments[0]
    images = pdfseiten.render(data, [1], "test.pdf")
    assert images[0].problem is None
    ink = _ink_box(images[0].images[0][1])
    for got, want in zip((seg.x0, seg.y0, seg.x1, seg.y1), ink):
        assert abs(got - want) < 0.012, (rotate, crop, (seg.x0, seg.y0, seg.x1, seg.y1), ink)


def test_scan_with_invisible_text_layer_is_a_scan():
    pages = read(scan_pdf(2, layer="Pruefprotokoll Brandschutzklappe BSK 12 Wartung bestanden"))
    assert [p.scan for p in pages] == [True, True]
    assert "Brandschutzklappe" in pages[0].text  # the OCR layer is the text for now


def test_scan_without_text_layer():
    pages = read(scan_pdf(1))
    assert pages[0].scan is True and pages[0].text == "" and pages[0].segments == []


def test_digital_page_with_a_logo_is_no_scan():
    logo = scan_image((300, 120), "Logo")
    data = Pdf().page(line(70, 700, "Brandschutzkonzept für die Grundschule Am Lindenhof in Musterstadt."),
                      image=(logo, (300, 120), (400, 760, 150, 60))).build()
    page = read(data)[0]
    assert page.scan is False and "Grundschule" in page.text


def test_page_without_anything_has_no_text_and_is_no_scan():
    page = read(Pdf().page().build())[0]
    assert (page.text, page.segments, page.scan) == ("", [], False)


def test_page_sizes_and_numbers():
    data = Pdf().page(line(70, 700, "Erste Seite mit etwas Text darauf.")).page(
        line(70, 700, "Zweite Seite im Querformat."), size=(842, 595)).build()
    pages = read(data)
    assert [p.number for p in pages] == [1, 2]
    assert (round(pages[1].width), round(pages[1].height)) == (842, 595)


def test_broken_file_is_a_problem_not_an_exception():
    result = pdfseiten.read(b"%PDF-1.4\nkein PDF", "kaputt.pdf")
    assert result.pages == [] and result.problem and not result.encrypted
    images = pdfseiten.render(b"%PDF-1.4\nkein PDF", [1], "kaputt.pdf")
    assert images[0].images == [] and images[0].problem


def test_render_gives_the_page_at_vision_dpi(monkeypatch):
    monkeypatch.setenv("VISION_DPI", "175")
    data = Pdf().page(line(70, 700, "Seite eins.")).page().build()
    first, second, missing = pdfseiten.render(data, [1, 2, 7], "test.pdf")
    mime, image = first.images[0]
    assert mime in ("image/jpeg", "image/png")
    assert Image.open(io.BytesIO(image)).size == (1446, 2046)  # A4 at 175 dpi
    assert second.images == [] and second.problem == pdfseiten.BLANK
    assert missing.problem == "Seite nicht vorhanden"


def test_garbled_text_layer_counts_as_none():
    assert pdfseiten.usable("Brandschutzkonzept für die Grundschule") is True
    assert pdfseiten.usable("\ufffd\ufffd\ufffd\ufffd \ue001\ue002\ue003 \ufffd\ufffd ab") is False
    assert pdfseiten.usable("kurz") is False  # fewer than MIN_TEXT characters: no text layer to speak of
