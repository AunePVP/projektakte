"""The report of numbers about the index (python -m projektakte.bericht): what was read how, where pages failed,
how well text layers agree with what the model saw – counts only, nothing of the documents' contents."""
from __future__ import annotations

from projektakte import bericht

from .seitenhilfe import docs as _docs
from .test_seitentyp_suche import index  # noqa: F401 – fixture


def test_report_counts_documents_pages_and_sections(index):
    conn, _root = index
    data = bericht.collect(conn)
    assert data["dokumente"]["gesamt"] == 3
    assert data["dokumente"]["status"] == {"ok": 3}
    assert data["dokumente"]["seiten_offen"] == 0
    assert data["seiten"]["typ"] == {"Text": 2, "Plan": 1, "Unsicher": 1}
    assert data["seiten"]["schleife"] == 1 and data["seiten"]["von_hand"] == 0
    assert data["seiten"]["textquelle"] == {"modell": 2, "stufe1": 2}
    assert data["abschnitte"]["gesamt"] == 5 and data["abschnitte"]["ohne_vektor"] == 5
    assert data["abschnitte"]["zeichen_mittel"] > 10


def test_report_groups_problems_without_naming_files(index):
    conn, _root = index
    doc = _docs(conn)["Scan.pdf"]
    conn.execute("UPDATE documents SET status = 'error', incomplete = 1, "
                 "message = 'Datei nicht lesbar (PdfiumError: Failed to load document 17)' WHERE id = ?", (doc["id"],))
    conn.execute("INSERT INTO page_texts(doc_id, page, text, state, note) VALUES(?, 7, '', 'failed', "
                 "'HTTP 504 von http://192.168.32.121:8767/v1/chat/completions: Zeitlimit')", (doc["id"],))
    conn.commit()
    data = bericht.collect(conn)
    assert data["dokumente"]["meldungen"][0] == ("Datei nicht lesbar (PdfiumError: Failed to load document #)", 1)
    assert data["seiten"]["wartend"] == {"failed": 1}
    assert data["seiten"]["fehler"][0][1] == 1 and "HTTP #" in data["seiten"]["fehler"][0][0]
    text = bericht.render(data)
    assert "Scan.pdf" not in text and "Rauchabzug" not in text  # counts only: safe to copy into a chat
    assert "Dokumente" in text and "Seiten" in text and "Abschnitte" in text and "Datei nicht lesbar" in text


def test_report_shows_how_well_text_layers_agree(index):
    conn, _root = index
    doc = _docs(conn)["Scan.pdf"]
    for page, share, origin in ((11, 0.99, "ebene"), (12, 0.97, "ebene"), (13, 0.7, "ebene"), (14, 0.2, "modell")):
        conn.execute("INSERT INTO page_types(doc_id, page, typ, layer, deckung, quelle) VALUES(?,?,?,1,?,?)",
                     (doc["id"], page, "Text", share, origin))
    conn.commit()
    data = bericht.collect(conn)
    assert data["seiten"]["deckung"] == {"ab 95 %": 2, "80–95 %": 0, "50–80 %": 1, "unter 50 %": 1}
    assert "Textebene" in bericht.render(data)


def test_main_prints_the_report(index, tmp_path, capsys, monkeypatch):
    conn, _root = index
    conn.commit()
    assert bericht.main(["--db", str(tmp_path / "x.sqlite3")]) == 0
    out = capsys.readouterr().out
    assert "Dokumente: 3" in out
