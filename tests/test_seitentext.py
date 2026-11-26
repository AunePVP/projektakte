"""Raw text of the page model (blocks with coordinates and a class) → the text of a page, and the rule for
headers and footers that come back on the pages of a document. Pure functions, no service."""
from projektakte.seitentext import Block, document_texts, page_text, parse_blocks, repeated_keys


def block(text: str, klasse: str = "Text", y: float = 0.25) -> str:
    return f"<x_0.0918><y_{y}>{text}<x_0.8906><y_{round(y + 0.0328, 4)}><class_{klasse}>"


def page(*blocks: str) -> str:
    return "\n\n".join(blocks)


def test_blocks_in_order_with_class_and_box():
    raw = page("<x_0.0><y_0.0>Musterbüro GmbH · Beispielstadt<x_0.999><y_0.0164><class_Page-header>",
               "<x_0.0918><y_0.2516>Die Löschwassermenge beträgt 96 m³/h.<x_0.8906><y_0.2844><class_Text>")
    blocks = parse_blocks(raw)
    assert [b.klasse for b in blocks] == ["Page-header", "Text"]
    assert blocks[0].text == "Musterbüro GmbH · Beispielstadt"
    assert blocks[1].box == (0.0918, 0.2516, 0.8906, 0.2844)


def test_text_outside_blocks_is_kept():
    blocks = parse_blocks("lose Zeile\n\n<x_0.1><y_0.1>A<x_0.2><y_0.2><class_Text>")
    assert blocks[0] == Block("Text", "lose Zeile", None)
    assert blocks[1].text == "A"


def test_cut_off_last_block_is_kept():
    blocks = parse_blocks(block("Erster Absatz.") + "\n\n<x_0.1><y_0.5>abgeschnittener Sat")
    assert blocks[-1].text == "abgeschnittener Sat"
    assert blocks[-1].box is None


def test_block_may_span_lines():
    blocks = parse_blocks(block("Zeile eins\nZeile zwei"))
    assert blocks[0].text == "Zeile eins\nZeile zwei"


def test_page_text_strips_marks_keeps_markdown():
    raw = page(block("# Prüfbericht Brandschutz", "Section-header", 0.1), block("Das ist **fett** gesetzt."))
    text = page_text(parse_blocks(raw))
    assert text == "# Prüfbericht Brandschutz\n\nDas ist **fett** gesetzt."
    assert "<x_" not in text and "<class_" not in text


def test_unescapes_angle_brackets():
    text = page_text(parse_blocks(block("An Max \\<m@buero.example>")))
    assert text == "An Max <m@buero.example>"


def test_empty_picture_dropped():
    raw = page(block("Davor."), block("", "Picture", 0.4), block("Danach.", y=0.6))
    assert page_text(parse_blocks(raw)) == "Davor.\n\nDanach."


def test_loop_note_removed_from_text():
    raw = block("Zeile A\nZeile A\n[Schleife des Modells: 37× wiederholt, Rest entfernt]")
    text = page_text(parse_blocks(raw))
    assert "Schleife des Modells" not in text
    assert text == "Zeile A\nZeile A"


def test_table_block_unchanged():
    table = "\\begin{tabular}{ll}\nRaum & Fläche \\\\\nT2 & 12 m² \\\\\n\\end{tabular}"
    assert page_text(parse_blocks(block(table, "Table"))) == table


def test_empty_raw_gives_no_text():
    assert parse_blocks("") == []
    assert page_text([]) == ""


def _report(pages: int) -> dict[int, str]:
    return {n: page(block("Musterbüro GmbH", "Page-header", 0.0), block(f"Inhalt der Seite {n}."),
                    block(f"Seite {n} von {pages}", "Page-footer", 0.95))
            for n in range(1, pages + 1)}


def test_repeated_header_kept_once():
    texts = document_texts(_report(4))
    assert texts[1] == "Musterbüro GmbH\n\nInhalt der Seite 1.\n\nSeite 1 von 4"
    for n in (2, 3, 4):
        assert texts[n] == f"Inhalt der Seite {n}."


def test_two_pages_nothing_removed():
    texts = document_texts(_report(2))
    assert texts[2] == "Musterbüro GmbH\n\nInhalt der Seite 2.\n\nSeite 2 von 2"


def test_header_on_two_of_six_pages_stays():
    raws = {n: page(block(f"Inhalt {n}.")) for n in range(1, 7)}
    for n in (2, 5):
        raws[n] = page(block("Anlage Lageplan", "Page-header", 0.0), raws[n])
    texts = document_texts(raws)
    assert texts[2].startswith("Anlage Lageplan") and texts[5].startswith("Anlage Lageplan")


def test_header_on_three_of_ten_pages_stays():
    raws = {n: page(block(f"Inhalt {n}.")) for n in range(1, 11)}
    for n in (1, 2, 3):
        raws[n] = page(block("Anhang A", "Page-header", 0.0), raws[n])
    assert all(document_texts(raws)[n].startswith("Anhang A") for n in (1, 2, 3))


def test_only_header_and_footer_classes_count():
    raws = {n: page(block("Vertraulich"), block(f"Inhalt {n}.", y=0.5)) for n in range(1, 5)}
    assert all(t.startswith("Vertraulich") for t in document_texts(raws).values())


def test_repeated_keys_ignore_digits_case_and_spacing():
    pages = [parse_blocks(block(text, "Page-footer")) for text in
             ("Seite 1 von 12", "SEITE 2  von 12", "Seite 10 von 12")]
    assert repeated_keys(pages) == {"seite # von #"}


def test_first_occurrence_is_on_first_page_that_has_it():
    raws = _report(4)
    raws[1] = page(block("Deckblatt."))
    texts = document_texts(raws)
    assert texts[1] == "Deckblatt."
    assert texts[2].startswith("Musterbüro GmbH")
    assert texts[3] == "Inhalt der Seite 3."
