"""Matching of question words against texts (round 2): German compounds by their head, no prefix
false positives, word forms, synonyms of everyday words, reference codes and clean passages."""
from projektakte.textutil import (
    PARTIAL_CREDIT,
    find_identifiers,
    identifier_in,
    is_summary_question,
    is_temporal_question,
    load_synonyms,
    plain_lines,
    query_term_credit,
    query_term_in,
    query_terms,
    relevant_passage,
    stem,
    term_in,
    text_terms,
    word_credit,
)


def _term(question: str, word: str):
    return next(t for t in query_terms(question) if t.word == word)


def test_compounds_match_by_their_head_not_by_their_first_part():
    """A Löschwasserteich is a Teich, a Baubesprechung a Besprechung, "Projektbezug" no Projekt."""
    assert term_in("baubesprechung", {"besprechung"})
    assert not term_in("loschwasserteich", {"loschwass"})
    assert not term_in("projektbezug", {"projekt"})
    assert term_in("rauchableitung", {"rauchableitungsoffnung"})  # the word starting a longer one
    assert word_credit("loschwasserteich", {"loschwasser"}) == PARTIAL_CREDIT
    assert word_credit("baubesprechung", {"besprechung"}) == 1.0


def test_short_stems_do_not_start_compounds_unless_inflected():
    """'Lüfter' (stem luft) is no 'Luftwechsel'; 'offen' is no 'Offenhalten'; 'Kinder' → kindgerecht."""
    assert not query_term_in(_term("Wie lange müssen die Lüfter laufen?", "lufter"), text_terms("Luftwechsel 12-fach"))
    assert not query_term_in(_term("Was ist noch offen?", "offen"), text_terms("Keile zum Offenhalten"))
    assert query_term_in(_term("Wie hoch ist der Handlauf für die Kinder?", "kinder"),
                         text_terms("Der kindgerechte Handlauf"))


def test_compound_written_apart_needs_both_parts_as_words():
    """'Stellplatz' is not 'Stellung' + 'Marktplatz'; 'Flurlänge' is 'Länge des Flurs'."""
    q = _term("Wie viele Stellplätze hat die Tiefgarage?", "stellplatze")
    assert not query_term_in(q, text_terms("Zum Marktplatz nehmen wir wie folgt Stellung: 2,10 m"))
    assert query_term_in(q, text_terms("Großgarage mit 240 Einstellplätzen"))
    assert query_term_in(_term("Wie lang ist die Flurlänge?", "flurlange"), text_terms("Länge des Flurs: 38 m"))


def test_everyday_synonyms_and_word_forms():
    pairs = [
        ("Wie viele Stockwerke hat das Haus?", "stockwerke", "Das Wohnhochhaus hat 22 Geschosse."),
        ("Wann beginnt der Ausbau im 4. Stock?", "stock", "Der Ausbau im 4. OG beginnt im Februar."),
        ("Muss die Fassade nichtbrennbar sein?", "fassade", "Die Außenwandbekleidung ist nichtbrennbar."),
        ("Wie heiß werden die Lüfter?", "lufter", "Die Ventilatoren der Entrauchung sind für 300 °C auszulegen."),
        ("Wer ist die Ansprechpartnerin?", "ansprechpartnerin", "Bauherr: Stadt, vertreten durch Petra Lindqvist"),
        ("Wer prüft die Anlage?", "pruft", "durch einen Prüfsachverständigen abzunehmen"),
        ("Was hat er bemängelt?", "bemangelt", "Mangel: Die Feststellanlage wurde ohne Abnahme betrieben."),
        ("Wie viele WE hat das Hochhaus?", "we", "Wohnungen: 140"),
        ("Wie hoch ist das Gebäude?", "hoch", "Gebäudehöhe 21,5 m"),
        ("Wann wird die Anlage gesichert?", "gesichert", "Die Zufahrt ist mit Pollern zu sichern."),
    ]
    for question, word, text in pairs:
        assert query_term_in(_term(question, word), text_terms(text)), (question, text)


def test_stemmer_keeps_words_with_a_final_s_and_feminine_forms():
    assert stem("hochhaus") == stem("hochhauses") == "hochhaus"
    assert stem("umbaus") == "umbau"  # genitive -s is still removed
    assert stem("prufingenieurin") == stem("prufingenieur")
    assert stem("ansprechpartnerin") == stem("ansprechpartner")


def test_temporal_and_summary_questions():
    assert is_temporal_question("Was gibt es Neues vom Bauherrn?")
    assert is_temporal_question("Was hat der Bauherr zuletzt geschrieben?")
    assert not is_temporal_question("Wie breit ist die neue Außentreppe?")
    assert is_summary_question("Fasse das Projekt Kita Regenbogen zusammen.")
    assert is_summary_question("Worum geht es bei der Industriehalle?")
    assert not is_summary_question("Muss die Fassade nichtbrennbar sein?")


def test_reference_codes():
    assert find_identifiers("BS-2024-0233, 63-0137/24 und BA-019/2025 am 2024-03-12") == \
        ["BS-2024-0233", "63-0137/24", "BA-019/2025"]
    assert identifier_in("BS-2024-0233", "Projekt bs 2024-0233 Hotel")
    assert not identifier_in("2024-0233", "Nr. 2024-02331")


def test_plain_lines_read_markdown_as_text():
    text = ("# 2 Kennwerte\nKennwert | Wert\nEinstellplätze | 240\n## Prüfbericht\n| Angabe | Inhalt |\n|---|---|\n"
            "| Prüfingenieur | Dipl.-Ing. Eva Muster |\n1. **Die** Länge ist zu groß.")
    assert plain_lines(text) == ["2 Kennwerte", "Einstellplätze: 240", "Prüfbericht",
                                 "Prüfingenieur: Dipl.-Ing. Eva Muster", "Die Länge ist zu groß."]


def test_relevant_passage_shows_the_quoted_sentence_not_the_chunk_start():
    text = ("Auflagen zum Brandschutz – Hotel\n" + "Auflage 1 (Feuerwehrzufahrt): Zufahrten sind freizuhalten. " * 6
            + "\nAuflage 4 (Sicherheitsbeleuchtung): In allen Fluren ist eine Sicherheitsbeleuchtung mit einer "
              "Betriebsdauer von mindestens 8 Stunden vorzusehen.\nAuflage 5 (Rauchableitung): 1,0 m².")
    passage = relevant_passage(text, "Wie lange muss das Notlicht funktionieren?",
                               "Laut Baugenehmigung, Auflage 4: Betriebsdauer von mindestens 8 Stunden [1]")
    assert "8 Stunden" in passage and passage.startswith("… ") and len(passage) <= 402
    md = relevant_passage("# 2 Kennwerte\nKennwert | Wert\nLöschwasser | 48 m³/h", "Wie viel Löschwasser?")
    assert "|" not in md and "#" not in md and "Löschwasser: 48 m³/h" in md
    mail = ("Betreff: Stand\nVon: Anna <a@b.example>\nDatum: 01.02.2025 10:00\nGuten Tag,\n"
            "Die Abnahme ist am 12.12.2024 erfolgt.")
    assert relevant_passage(mail, "Wann war die Abnahme?").endswith("Die Abnahme ist am 12.12.2024 erfolgt.")


def test_synonyms_file_works_for_matching_too(tmp_path):
    """A9: SYNONYMS_FILE entries reach the question analysis as well, not only the full-text query."""
    f = tmp_path / "syn.txt"
    f.write_text("Pförtner = Nachtportier\n", encoding="utf-8")
    syn = load_synonyms(str(f))
    q = next(t for t in query_terms("Wer ist der Pförtner?", syn) if t.word == "pfortner")
    assert query_term_credit(q, text_terms("Nachtportier als Brandschutzhelfer"), syn) == 1.0
