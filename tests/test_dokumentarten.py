"""Document types (dokumentarten.py): what a document is, from its content (the file name only a weak hint),
not limited to a fixed list; the search ranks the documents of a type named in the query first – newest
first, related types next, mere mentions after them, nothing left out; the language model is asked about
unclear documents in the background (structured answer, "unklar" allowed), never with the fake AI."""
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte import db, dokumentarten as da
from projektakte.ai.services import AIServices
from projektakte.documents import write_chunks
from projektakte.chunking import Chunk
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.search import TYPES_CANDIDATES, TYPES_OFF, TYPES_TOP, hybrid_search

from .conftest import fake_settings

KITA, GARAGE = "BS-2031-0001", "BS-2031-0002"

# file name → (text, date): the names say little or something wrong on purpose
FILES = {
    f"{KITA} Kita Sonnenschein": {
        "Scan_0815.txt": ("Brandschutzkonzept\nfür den Neubau der Kita Sonnenschein\nStand: 03.02.2023\n\n"
                          "Der erste Rettungsweg führt über den Flur.", "2023-02-03"),
        "Dok_17_final.txt": ("Ingenieurbüro Funkenflug GmbH\nTel. 0000 123\n\n"
                             "Brandschutzkonzept – Fortschreibung Index B\nKita Sonnenschein\n"
                             "Stand: 10.09.2024\n\nDie Rettungswege sind neu bemessen.", "2024-09-10"),
        "Brandschutzkonzept_alt.txt": ("Brandschutztechnische Stellungnahme\nzur Nutzungsänderung im Obergeschoss\n"
                                       "Datum: 01.03.2024\n\nDie Brandschutzkonzept-Annahmen bleiben gültig.",
                                       "2024-03-01"),
        "BS-Nachweis.txt": ("Brandschutznachweis\nKita Sonnenschein, Anbau\nDatum: 01.05.2022\n\n"
                            "Nachweis der Rettungswege.", "2022-05-01"),
        "Notiz Bauleitung.txt": ("Notiz zur Baubesprechung\nDatum: 12.12.2024\n\nGemäß Brandschutzkonzept ist "
                                 "die Tür T30. Das Brandschutzkonzept fordert Rauchmelder. Laut Brandschutzkonzept "
                                 "bleibt der Flur frei. Brandschutzkonzept Brandschutzkonzept.", "2024-12-12"),
        "Dok_99.txt": ("Löschwasserberechnung\nfür die Kita Sonnenschein\nDatum: 05.05.2024\n\n"
                       "Bedarf 96 m³/h über zwei Stunden.", "2024-05-05"),
        "Protokoll 3.txt": ("Begehungsprotokoll Nr. 3\nDatum: 01.06.2024\n\nMängel an den Türen.", "2024-06-01"),
    },
    f"{GARAGE} Tiefgarage Lindenplatz": {
        "Konzept.txt": ("Brandschutzkonzept\nTiefgarage Lindenplatz\nStand: 01.01.2025\n\n"
                        "Die Zufahrt der Feuerwehr erfolgt über die Rampe.", "2025-01-01"),
        "Anschreiben.txt": ("Betreff: Brandschutzkonzept Tiefgarage – Übersendung\n\nSehr geehrte Damen und "
                            "Herren,\nanbei das Brandschutzkonzept.\nMit freundlichen Grüßen", "2025-01-02"),
    },
}


def _write_projects(root: Path) -> None:
    for folder, files in FILES.items():
        for name, (text, day) in files.items():
            path = root / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            ts = time.mktime(time.strptime(day, "%Y-%m-%d"))
            os.utime(path, (ts, ts))


@pytest.fixture(scope="module")
def env(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("dokumentarten")
    _write_projects(tmp / "projekte")
    s = fake_settings(fake_ai, tmp, PROJECTS_DIR=str(tmp / "projekte"))
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return s, ai, conn


def _types(conn) -> dict[str, sqlite3.Row]:
    return {r["filename"]: r for r in conn.execute("SELECT * FROM documents")}


def _names(res) -> list[str]:
    return [h.title for h in res.hits]


# --------------------------------------------------------------------------- detection
@pytest.mark.parametrize("text, filename, expected", [
    ("Brandschutzkonzept\nfür den Neubau", "Scan_0001.pdf", "Brandschutzkonzept"),
    ("# Prüfbericht Nr. 2 zum Brandschutznachweis\n| Projekt | X |", "a.md", "Prüfbericht"),
    ("Brandschutztechnische Stellungnahme zum Umbau", "Brandschutzkonzept_v2.pdf", "Stellungnahme"),
    ("Entwurf des Brandschutzkonzepts\nfür die Kita", "Notiz.pdf", "Brandschutzkonzept"),
    ("Untere Bauaufsicht Musterstadt\nMusterstadt, den 01.02.2024\nAktenzeichen: 63-1/24\nBaugenehmigung\n",
     "Scan.pdf", "Baugenehmigung"),
    ("Flucht- und Rettungsplan\nErdgeschoss", "EG.pdf", "Flucht- und Rettungsplan"),
    ("Abschlagsrechnung Nr. 4\nLeistungen bis März", "x.pdf", "Rechnung"),
    ("Honorarangebot\nfür die Fachplanung Brandschutz", "x.pdf", "Angebot"),
    ("Leistungsverzeichnis Rauchwarnmelder\nPosition 1", "x.txt", "Leistungsverzeichnis"),
    ("Übereinstimmungserklärung\ndes Errichters", "x.pdf", "Übereinstimmungserklärung"),
    ("Sehr geehrte Damen und Herren,\nanbei das Brandschutzkonzept.\nMit freundlichen Grüßen", "x.pdf", "Schreiben"),
    ("Betreff: Brandschutzkonzept Kita – Übersendung\nSehr geehrte Frau Muster,", "x.pdf", "Schreiben"),
    ("Ingenieurbüro Muster GmbH\nTel. 0000 1\nLöschwasserberechnung\nfür das Objekt", "Dok1.pdf",
     "Löschwasserberechnung"),
    ("Plan & Bau GmbH\nMusterweg 1\n\nDie Leitung wird verlegt.", "x.pdf", None),
    ("Gemäß Brandschutzkonzept ist die Tür T30 auszuführen.", "Notiz.pdf", None),
])
def test_type_comes_from_the_content_not_the_file_name(text, filename, expected):
    det = da.detect(text, filename, Path(filename).suffix)
    assert det.name == expected
    if expected:
        assert det.method == da.INHALT and det.clear


def test_file_name_is_only_a_weak_hint():
    det = da.detect("Die Anlage wurde gemäß Brandschutzkonzept errichtet.", "BSK_Kita_v3.pdf", ".pdf")
    assert (det.name, det.method) == ("Brandschutzkonzept", da.DATEINAME) and not det.clear
    agree = da.detect("Brandschutzkonzept\nKita", "Brandschutzkonzept.pdf", ".pdf")
    alone = da.detect("Brandschutzkonzept\nKita", "Scan.pdf", ".pdf")
    assert agree.confidence > alone.confidence >= da.CLEAR
    assert da.detect("", "IMG_2031.jpg", ".jpg").name == "Foto"
    assert da.detect("Von: a@b.example", "Mail.eml", ".eml").name == "Mail"
    assert da.detect("", "Dokument.pdf", ".pdf").name is None


def test_new_types_are_free_text_in_the_singular():
    assert da.normalize("Löschwasserberechnungen") == ("Löschwasserberechnung", "loschwasserberechnung")
    assert da.normalize("brandschutzkonzept") == ("Brandschutzkonzept", "brandschutzkonzept")
    assert da.normalize("BS-Nachweis") == ("Brandschutznachweis", "brandschutznachweis")
    assert da.normalize("Ein ganzer Satz, der keine Art ist und viel zu lang wird") is None
    assert da.normalize("") is None and da.normalize("<b>x</b>") is None


# --------------------------------------------------------------------------- query words
@pytest.mark.parametrize("query, expected", [
    ("Brandschutzkonzept", ["brandschutzkonzept"]),
    ("Brandschutzkonzepte", ["brandschutzkonzept"]),
    ("brandschutzkonzepten Kita", ["brandschutzkonzept"]),
    ("BS-Konzept", ["brandschutzkonzept"]),
    ("Brandschutzkozept", ["brandschutzkonzept"]),
    ("Branschutzkonzept", ["brandschutzkonzept"]),
    ("Kitabrandschutzkonzept", ["brandschutzkonzept"]),
    ("Prüfberichte Hotel", ["prufbericht"]),
    ("Fluchtpläne", ["fluchtundrettungsplan"]),
    ("LV Rauchwarnmelder", ["leistungsverzeichnis"]),
    ("Rettungsweg", []),
    ("Mail vom Bauherrn", []),
    ("wer hat das schreiben", []),
    ("Brandschutz", []),
])
def test_query_names_a_type_in_any_form(query, expected):
    assert da.query_types(None, query).exact == expected


def test_a_family_head_names_all_its_types():
    tq = da.query_types(None, "Protokolle")
    assert {"protokoll", "besprechungsprotokoll", "begehungsprotokoll", "abnahmeprotokoll"} <= set(tq.exact)
    tq = da.query_types(None, "Brandschutzkonzept Kita")
    assert tq.related == ["brandschutznachweis"] and tq.rest == "Kita"


def test_synonym_groups_count_as_aliases(tmp_path):
    from projektakte.textutil import load_synonyms
    f = tmp_path / "syn.txt"
    f.write_text("Löschwassernachweis = Löschwasserberechnung\n", encoding="utf-8")
    types = {**da.seed_types(), "loschwasserberechnung": da.DocType("Löschwasserberechnung", "loschwasserberechnung")}
    da.KNOWN._cache[":memory:"] = (time.monotonic() + 3600, types)
    try:
        conn = sqlite3.connect(":memory:")
        assert da.query_types(conn, "Löschwassernachweis", load_synonyms(str(f))).exact == ["loschwasserberechnung"]
    finally:
        da.KNOWN.invalidate()


# --------------------------------------------------------------------------- indexing and search
def test_documents_are_typed_when_indexed(env):
    _, _, conn = env
    t = _types(conn)
    assert t["Scan_0815.txt"]["doc_type"] == "Brandschutzkonzept"
    assert t["Dok_17_final.txt"]["doc_type"] == "Brandschutzkonzept"  # letterhead lines are skipped
    assert t["Brandschutzkonzept_alt.txt"]["doc_type"] == "Stellungnahme"  # the name is misleading
    assert t["BS-Nachweis.txt"]["doc_type"] == "Brandschutznachweis"
    assert t["Notiz Bauleitung.txt"]["doc_type"] is None  # a mention is no title
    assert t["Dok_99.txt"]["doc_type"] == "Löschwasserberechnung"  # not in the seed list
    assert t["Protokoll 3.txt"]["doc_type"] == "Begehungsprotokoll"
    assert t["Anschreiben.txt"]["doc_type"] == "Schreiben"
    assert t["Scan_0815.txt"]["doc_type_method"] == da.INHALT
    assert t["Scan_0815.txt"]["doc_type_title"].startswith("Brandschutzkonzept")
    # unclear documents wait for the model (asked only with real AI services)
    assert t["Notiz Bauleitung.txt"]["doc_type_ai"] == da.AI_OPEN


@pytest.mark.parametrize("query", ["Brandschutzkonzept", "Brandschutzkonzepte", "Branschutzkonzept",
                                   "Brandschutzkozept", "BS-Konzept"])
def test_type_documents_come_first_newest_first(env, query):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), query, project=KITA, k=10, doc_types=TYPES_TOP)
    names = _names(res)
    assert names[:2] == ["Dok_17_final.txt", "Scan_0815.txt"], names  # the newest version first
    assert names[2] == "BS-Nachweis.txt"  # a related type ("named differently") next
    assert "Notiz Bauleitung.txt" in names  # a mere mention is still found, after them
    assert names.index("Notiz Bauleitung.txt") > 2
    assert [h.type_match for h in res.hits[:3]] == ["art", "art", "verwandt"]
    assert res.doc_types == ["Brandschutzkonzept"]


def test_type_plus_other_words(env):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), "Brandschutzkonzept Tiefgarage", k=10, doc_types=TYPES_TOP)
    names = _names(res)
    assert names[0] == "Konzept.txt"  # the concept of the garage, not the newer concepts of other projects
    assert {"Dok_17_final.txt", "Scan_0815.txt"} <= set(names)  # nothing is left out
    res = hybrid_search(conn, ai, str(s.db_path), "Brandschutzkonzept Kita", k=10, doc_types=TYPES_TOP)
    assert _names(res)[:2] == ["Dok_17_final.txt", "Scan_0815.txt"]


def test_new_type_is_searchable(env):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), "Löschwasserberechnungen", k=5, doc_types=TYPES_TOP)
    assert _names(res)[0] == "Dok_99.txt" and res.hits[0].doc_type == "Löschwasserberechnung"
    res = hybrid_search(conn, ai, str(s.db_path), "Protokolle Kita", k=5, doc_types=TYPES_TOP)
    assert _names(res)[0] == "Protokoll 3.txt"


def test_without_type_ranking_the_order_is_the_old_one(env):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), "Brandschutzkonzept", project=KITA, k=10, doc_types=TYPES_OFF)
    assert not res.doc_types and all(h.type_match is None for h in res.hits)


def test_questions_only_add_candidates(env):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), "Was steht im Brandschutzkonzept zur Zufahrt?", k=10,
                        doc_types=TYPES_CANDIDATES)
    assert "Konzept.txt" in _names(res)[:3]
    assert all(h.type_match is None for h in res.hits)  # the reranker decides the order


def test_search_page_shows_type_and_correction(env):
    s, _, _ = env
    with TestClient(create_app(s, background=False)) as client:
        html = client.get("/suche", params={"q": "Branschutzkonzept", "project": KITA}).text
        assert "Dokumentart erkannt" in html and "doc-type match" in html
        assert "meinten Sie" in html and "„Brandschutzkonzept“" in html
        assert html.index("Dok_17_final.txt") < html.index("Notiz Bauleitung.txt")
        data = client.get("/api/search", params={"q": "Brandschutzkonzepte", "project": KITA}).json()
        assert data["doc_types"] == ["Brandschutzkonzept"] and data["hits"][0]["type_match"] == "art"
        doc_id = data["hits"][0]["doc_id"]
        page = client.get(f"/dokumente/{doc_id}").text
        assert "Dokumentart" in page and "aus dem Inhalt" in page
        folder = client.get(f"/projekte/{KITA}").text
        assert 'class="pill doc-type"' in folder


# --------------------------------------------------------------------------- re-classification
def _doc_with_text(conn, text: str, filename: str = "x.txt") -> int:
    doc_id = conn.execute("INSERT INTO documents(path, project, rel_path, filename, ext) VALUES(?,?,?,?,?)",
                          (f"/p/{filename}", None, filename, filename, Path(filename).suffix)).lastrowid
    write_chunks(conn, doc_id, [Chunk(0, 1, None, text)], filename=filename, project=None, project_name=None,
                 mail_id=None)
    return doc_id


def _row(conn, doc_id):
    return conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()


def test_type_follows_a_changed_text(tmp_path):
    p = tmp_path / "t.sqlite3"
    db.init_db(p)
    conn = db.connect(p)
    doc_id = _doc_with_text(conn, "Seite ohne Textebene, nur ein Stempel und eine Unterschrift", "Scan.pdf")
    assert _row(conn, doc_id)["doc_type"] is None and _row(conn, doc_id)["doc_type_ai"] == da.AI_OPEN
    # the Bildauswertung replaced the text: typed anew
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    write_chunks(conn, doc_id, [Chunk(0, 1, None, "Prüfbericht Nr. 4\nzur Brandmeldeanlage")], filename="Scan.pdf",
                 project=None, project_name=None, mail_id=None)
    assert _row(conn, doc_id)["doc_type"] == "Prüfbericht" and _row(conn, doc_id)["doc_type_ai"] is None
    # a type the model gave stays while the text stays the same
    conn.execute("UPDATE documents SET doc_type = 'Gutachten', doc_type_method = 'ki' WHERE id = ?", (doc_id,))
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    write_chunks(conn, doc_id, [Chunk(0, 1, None, "Prüfbericht Nr. 4\nzur Brandmeldeanlage")], filename="Scan.pdf",
                 project=None, project_name=None, mail_id=None)
    assert _row(conn, doc_id)["doc_type"] == "Gutachten"


def test_backfill_types_older_documents(tmp_path):
    p = tmp_path / "t.sqlite3"
    db.init_db(p)
    conn = db.connect(p)
    ids = [_doc_with_text(conn, "Brandschutzkonzept\nHalle 1", f"d{i}.txt") for i in range(5)]
    conn.execute("UPDATE documents SET doc_type = NULL, doc_type_key = NULL, doc_type_hash = NULL")
    assert da.counts(conn)["todo"] == 5
    assert da.backfill(conn, limit=3) == 3 and da.counts(conn)["todo"] == 2
    assert da.backfill_all(conn) == 2 and da.counts(conn)["todo"] == 0
    assert {_row(conn, i)["doc_type"] for i in ids} == {"Brandschutzkonzept"}


def test_migration_14_adds_the_columns_to_an_older_database(tmp_path):
    p = tmp_path / "alt.sqlite3"
    conn = sqlite3.connect(p)
    for i, script in enumerate(db.MIGRATIONS[:13], start=1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    conn.execute("INSERT INTO documents(path, rel_path, filename, ext) VALUES('/a', 'a.txt', 'a.txt', '.txt')")
    conn.commit()
    conn.close()
    db.init_db(p)
    conn = db.connect(p)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 14
    assert da.counts(conn)["todo"] == 1  # typed by the background worker (or the CLI pass)


# --------------------------------------------------------------------------- language model
class Chat:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def chat(self, messages, **kw):
        self.prompts.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer

        class R:
            text = answer
        return R()


@pytest.mark.parametrize("answer, expected", [
    ('{"dokumentart": "Brandschutznachweis", "sicher": true}', ("Brandschutznachweis", "brandschutznachweis")),
    ('Gern: {"dokumentart": "Löschwasserberechnungen", "sicher": true}', ("Löschwasserberechnung",
                                                                           "loschwasserberechnung")),
    ('{"dokumentart": "Gutachten", "sicher": false}', None),
    ('{"dokumentart": null, "sicher": false}', None),
    ('{"dokumentart": "unklar", "sicher": true}', None),
    ('Das ist wohl ein Brandschutzkonzept.', None),
    ('{"dokumentart": "<script>", "sicher": true}', None),
])
def test_model_answer_is_parsed_strictly(answer, expected):
    assert da.parse_ai_answer(answer) == expected


def test_model_classifies_unclear_documents_and_may_refuse(tmp_path):
    p = tmp_path / "t.sqlite3"
    db.init_db(p)
    conn = db.connect(p)
    a = _doc_with_text(conn, "Nachweis der Rettungswege für das Bürogebäude, Seite 1 von 12 ...", "a.pdf")
    b = _doc_with_text(conn, "Seite 7 von 12 – Fortsetzung der Tabelle mit Messwerten und Zahlen", "b.pdf")
    c = _doc_with_text(conn, "Brandschutzkonzept\nHalle", "c.pdf")  # clear: never asked
    conn.commit()
    llm = Chat(['{"dokumentart": "Brandschutznachweis", "sicher": true}', '{"dokumentart": null, "sicher": false}'])
    assert da.classify_with_ai(conn, llm, a) == da.AI_DONE
    assert da.classify_with_ai(conn, llm, b) == da.AI_UNCLEAR
    ra, rb = _row(conn, a), _row(conn, b)
    assert (ra["doc_type"], ra["doc_type_method"], ra["doc_type_ai"]) == ("Brandschutznachweis", da.KI, da.AI_DONE)
    assert (rb["doc_type"], rb["doc_type_ai"]) == (None, da.AI_UNCLEAR)  # nothing invented
    assert _row(conn, c)["doc_type_ai"] is None
    prompt = llm.prompts[0][1]["content"]
    assert "Dateiname: a.pdf" in prompt and "Rettungswege" in prompt and len(prompt) < da.AI_CHARS + 100


def test_worker_types_backfill_then_asks_the_model_one_at_a_time(tmp_path):
    p = tmp_path / "t.sqlite3"
    db.init_db(p)
    conn = db.connect(p)
    a = _doc_with_text(conn, "Nachweis der Rettungswege für das Bürogebäude, Seite 1 von 12 ...", "a.pdf")
    b = _doc_with_text(conn, "Brandschutzkonzept\nHalle", "b.pdf")
    conn.execute("UPDATE documents SET doc_type = NULL, doc_type_key = NULL, doc_type_hash = NULL, doc_type_ai = NULL")
    conn.commit()

    class S:
        db_path = p
        ai_mode = ""
        dokumentart_ki = "auto"
    llm = Chat(['{"dokumentart": "Brandschutznachweis", "sicher": true}'])
    live = da.Live()
    worker = da.DokumentartWorker(S(), llm, live=live)
    assert worker.use_ai
    wconn = db.connect(p)
    wconn.isolation_level = None
    assert worker.run_round(wconn)  # backfill first
    assert _row(conn, b)["doc_type"] == "Brandschutzkonzept" and _row(conn, a)["doc_type_ai"] == da.AI_OPEN
    # the model waits while a question runs
    with da.QUESTIONS:
        t = threading.Thread(target=worker.run_round, args=(wconn,))
        t.start()
        time.sleep(0.3)
        assert llm.prompts == []
    t.join(5)
    assert len(llm.prompts) == 1 and _row(conn, a)["doc_type"] == "Brandschutznachweis"
    assert not worker.run_round(wconn)  # nothing left
    assert live.snapshot()["typed"] == 2 and live.snapshot()["asked"] == 1


def test_the_fake_ai_is_never_asked():
    class S:
        ai_mode = "fake"
        dokumentart_ki = "auto"
    assert not da.ai_wanted(S(), object())
    S.dokumentart_ki = "1"
    assert da.ai_wanted(S(), object())
    S.ai_mode, S.dokumentart_ki = "", "0"
    assert not da.ai_wanted(S(), object()) and not da.ai_wanted(S(), None)


def test_setting_dokumentart_ki():
    from projektakte.config import load_settings
    assert load_settings({"APP_ENV": "test"}).dokumentart_ki == "auto"
    assert load_settings({"APP_ENV": "test", "DOKUMENTART_KI": "0"}).dokumentart_ki == "0"
    s = load_settings({"APP_ENV": "test", "DOKUMENTART_KI": "vielleicht"})
    assert s.dokumentart_ki == "auto" and any("DOKUMENTART_KI" in w for w in s.config_warnings)


# --------------------------------------------------------------------------- speed
def test_detection_and_type_lookup_stay_fast(tmp_path):
    texts = [f"Ingenieurbüro Muster GmbH\nMusterweg {i}\n\nBrandschutzkonzept\nfür Halle {i}\n" + "Text " * 300
             for i in range(500)]
    started = time.perf_counter()
    for t in texts:
        da.detect(t, "Scan.pdf", ".pdf")
    assert (time.perf_counter() - started) / len(texts) < 0.005  # well below 5 ms per document

    p = tmp_path / "gross.sqlite3"
    db.init_db(p)
    conn = db.connect(p)
    kinds = ["brandschutzkonzept", "prufbericht", "rechnung", "schreiben"]
    conn.executemany(
        "INSERT INTO documents(id, path, project, rel_path, filename, ext, doc_date, doc_type, doc_type_key, "
        "doc_type_confidence, doc_type_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(i, f"/p/{i}", f"P{i % 400}", f"{i}.pdf", f"{i}.pdf", ".pdf", f"20{10 + i % 15}-01-01",
          kinds[i % 4], kinds[i % 4], 0.9, "x") for i in range(1, 40001)])
    conn.executemany("INSERT INTO chunks(id, doc_id, project, ord, text) VALUES(?,?,?,0,'x')",
                     [(i, i, f"P{i % 400}") for i in range(1, 40001)])
    conn.executemany("INSERT INTO chunks_fts(rowid, title, body) VALUES(?, '', 'x')", [(i,) for i in range(1, 40001)])
    conn.commit()
    started = time.perf_counter()
    for _ in range(5):
        everywhere = da.type_documents(conn, ["brandschutzkonzept"], limit=20)
        one = da.type_documents(conn, ["brandschutzkonzept"], projects=["P4"], limit=20)
    seconds = (time.perf_counter() - started) / 5
    assert len(everywhere) == 20 and one and all(d[2] == "brandschutzkonzept" for d in one)
    assert seconds < 0.5, seconds
