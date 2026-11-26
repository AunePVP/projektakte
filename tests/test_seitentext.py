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


# -- pages with a real text layer: the exact text of the layer in the model's blocks ----------------------------
from projektakte.seitentext import compose_layer, coverage, join_lines, layer_blocks  # noqa: E402


def seg(x0, y0, x1, y1, text):
    return [x0, y0, x1, y1, text]


def mblock(text: str, klasse: str, box) -> str:
    x0, y0, x1, y1 = box
    return f"<x_{x0}><y_{y0}>{text}<x_{x1}><y_{y1}><class_{klasse}>"


def test_join_lines_makes_a_paragraph():
    assert join_lines(["Laut Leitfaden der LUBW ist die", "äußere Böschung zu wählen."]) == \
        "Laut Leitfaden der LUBW ist die äußere Böschung zu wählen."
    assert join_lines(["Zuständig ist die Brandschutz-", "Dienststelle der Stadt."]) == \
        "Zuständig ist die Brandschutz-Dienststelle der Stadt."
    assert join_lines(["Werte von 3 -", "5 m sind üblich."]) == "Werte von 3 - 5 m sind üblich."
    assert join_lines([]) == ""


def _page():
    """A page as the text layer gives it (lines, exact) and as the model saw it (blocks, one word misread)."""
    segments = [
        seg(0.10, 0.02, 0.60, 0.04, "Musterbüro GmbH · Beispielstadt"),
        seg(0.10, 0.10, 0.50, 0.13, "Gewässerrandstreifen"),
        seg(0.10, 0.16, 0.14, 0.18, "8."),
        seg(0.16, 0.16, 0.90, 0.18, "Laut Leitfaden der LUBW ist im Falle mehrerer Böschungsoberkanten"),
        seg(0.16, 0.18, 0.90, 0.20, "diejenige zu wählen, die am nächsten liegt (§ 38 Abs. 4 WHG)."),
        seg(0.10, 0.22, 0.14, 0.24, "9."),
        seg(0.16, 0.22, 0.90, 0.24, "Vor Beginn der Baumaßnahme ist der Gewässerrandstreifen abzumessen."),
        seg(0.10, 0.95, 0.30, 0.97, "Seite 13 von 184"),
    ]
    raw = "\n\n".join([
        mblock("Musterbüro GmbH · Beispielstadt", "Page-header", (0.09, 0.015, 0.61, 0.045)),
        mblock("## Gewässerrandstreifen", "Section-header", (0.09, 0.095, 0.51, 0.135)),
        mblock("8. Laut Leitfaden der LUBW ist im Falle mehrerer Böschungsoberkanten diejenige zu wählen, die am "
               "nächsten liegt (§ 3B Abs. 4 WHG).", "List-item", (0.09, 0.155, 0.91, 0.205)),
        mblock("9. Vor Beginn der Baumaßnahme ist der Gewässerrandstreifen abzumessen.", "List-item",
               (0.09, 0.215, 0.91, 0.245)),
        mblock("Seite 13 von 184", "Page-footer", (0.09, 0.945, 0.31, 0.975)),
    ])
    return segments, raw


def test_layer_blocks_take_the_exact_text_in_the_models_blocks():
    segments, raw = _page()
    blocks = layer_blocks(segments, parse_blocks(raw))
    assert [b.klasse for b in blocks] == ["Page-header", "Section-header", "List-item", "List-item", "Page-footer"]
    assert blocks[1].text == "## Gewässerrandstreifen"  # the model's heading mark, the layer's words
    assert blocks[2].text == ("8. Laut Leitfaden der LUBW ist im Falle mehrerer Böschungsoberkanten diejenige zu "
                              "wählen, die am nächsten liegt (§ 38 Abs. 4 WHG).")  # exact: § 38, not the misread § 3B
    assert blocks[3].text == "9. Vor Beginn der Baumaßnahme ist der Gewässerrandstreifen abzumessen."


def test_columns_are_put_together_by_block():
    """The text layer gives two columns line by line across; the model's blocks put each column together."""
    segments = [seg(0.10, 0.90, 0.40, 0.92, "Bankverbindung: Sparkasse"), seg(0.55, 0.90, 0.85, 0.92, "Servicezeiten:"),
                seg(0.10, 0.92, 0.40, 0.94, "IBAN: DE00 0000 0000"), seg(0.55, 0.92, 0.85, 0.94, "Montag: 08:30 Uhr")]
    raw = "\n\n".join([mblock("Bankverbindung: Sparkasse IBAN: DE00 0000 0000", "Page-footer", (0.09, 0.89, 0.41, 0.95)),
                       mblock("Servicezeiten: Montag: 08:30 Uhr", "Page-footer", (0.54, 0.89, 0.86, 0.95))])
    blocks = layer_blocks(segments, parse_blocks(raw))
    assert [b.text for b in blocks] == ["Bankverbindung: Sparkasse IBAN: DE00 0000 0000",
                                       "Servicezeiten: Montag: 08:30 Uhr"]


def test_lines_outside_every_block_are_never_lost():
    segments, raw = _page()
    segments.insert(7, seg(0.10, 0.50, 0.80, 0.52, "Dieser Absatz liegt in keinem Block des Modells."))
    blocks = layer_blocks(segments, parse_blocks(raw))
    texts = [b.text for b in blocks]
    assert "Dieser Absatz liegt in keinem Block des Modells." in texts
    assert texts.index("Dieser Absatz liegt in keinem Block des Modells.") == 4  # after the block before it
    assert sum(len(t) for t in texts) >= sum(len(s[4]) for s in segments)  # every line is in the text


def test_text_only_the_image_shows_comes_from_the_model():
    """A stamp or a scanned insert on a digital page: the model read it, the text layer does not have it."""
    segments, raw = _page()
    raw += "\n\n" + mblock("Geprüft Bauaufsicht Musterstadt 12.03.2024", "Text", (0.55, 0.60, 0.90, 0.66))
    texts = [b.text for b in layer_blocks(segments, parse_blocks(raw))]
    assert "Geprüft Bauaufsicht Musterstadt 12.03.2024" in texts


def test_model_block_whose_text_the_layer_has_elsewhere_is_not_doubled():
    """The model's box missed its lines: they stay as loose lines, the model's copy is left out."""
    segments, raw = _page()
    raw = raw.replace("<x_0.09><y_0.215>", "<x_0.09><y_0.300>").replace("<x_0.91><y_0.245>", "<x_0.91><y_0.330>")
    texts = [b.text for b in layer_blocks(segments, parse_blocks(raw))]
    assert sum("Vor Beginn der Baumaßnahme" in t for t in texts) == 1


def test_table_rows_stay_rows():
    segments = [seg(0.10, 0.30, 0.30, 0.32, "Raum"), seg(0.50, 0.30, 0.70, 0.32, "Fläche"),
                seg(0.10, 0.32, 0.30, 0.34, "T2"), seg(0.50, 0.32, 0.70, 0.34, "12 m²")]
    raw = mblock("irgendeine Tabellenschreibweise", "Table", (0.09, 0.29, 0.71, 0.35))
    blocks = layer_blocks(segments, parse_blocks(raw))
    assert blocks[0].klasse == "Table" and blocks[0].text == "Raum | Fläche\nT2 | 12 m²"


def test_coverage_is_the_share_of_the_models_words_the_layer_has():
    layer = "Laut Leitfaden der LUBW ist die äußere Böschung zu wählen und abzumessen"
    assert coverage("Laut Leitfaden der LUBW ist die äußere Böschung zu wählen und abzumessen", layer) == 1.0
    assert coverage("**Laut** Leitfaden der LUBW ist die äussere Boschung zu wählen und abzumessen", layer) >= 0.8
    assert coverage("Ganz anderer Text über Stellplätze Garagen Zufahrten Feuerwehr Aufstellflächen", layer) < 0.2
    assert coverage("zu kurz", layer) is None  # too few words to tell


def test_compose_layer_uses_the_layer_when_it_shows_what_the_model_saw():
    segments, raw = _page()
    blocks, deckung, quelle = compose_layer(segments, raw)
    assert quelle == "ebene" and deckung >= 0.9
    assert "§ 38 Abs. 4 WHG" in page_text(blocks) and "§ 3B" not in page_text(blocks)


def test_compose_layer_uses_the_model_when_the_layer_is_not_what_the_page_shows():
    """A text layer of nonsense (a font without a usable character map) or hidden text: the model's text."""
    _segments, raw = _page()
    garbled = [seg(0.10, 0.16, 0.90, 0.18, "Kdxw Khlwidghq ghu OXEZ lvw lp Idooh phkuhuhu Eövfkxqjvrehundqwhq"),
               seg(0.10, 0.22, 0.90, 0.24, "Yru Ehjlqq ghu Edxpdßqdkph lvw ghu Jhzävvhuudqgvwuhlihq")]
    blocks, deckung, quelle = compose_layer(garbled, raw)
    assert quelle == "modell" and deckung < 0.2
    assert "Laut Leitfaden der LUBW" in page_text(blocks)


def test_compose_layer_keeps_the_layer_when_the_models_answer_is_not_whole():
    _segments, raw = _page()
    other = [seg(0.10, 0.16, 0.90, 0.18, "Ein ganz anderer, aber einwandfreier Text der Textebene über Stellplätze.")]
    for flags in ({"schleife": True}, {"finish": "length"}):
        blocks, _deckung, quelle = compose_layer(other, raw, **flags)
        assert quelle == "ebene" and "Stellplätze" in page_text(blocks)


def test_compose_layer_without_model_blocks_is_the_plain_layer():
    segments, _raw = _page()
    blocks, deckung, quelle = compose_layer(segments, "")
    assert (deckung, quelle) == (None, "ebene")
    assert page_text(blocks).split("\n")[1] == "Gewässerrandstreifen"
