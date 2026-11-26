"""The page type of every page the page model saw, kept for good (table page_types, migration 20)."""
import sqlite3

import pytest

from projektakte import db, seitentypen


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO documents(id, path, rel_path, filename, ext, sha256) "
                 "VALUES(1, '/p/a.pdf', 'a.pdf', 'a.pdf', '.pdf', 'x')")
    yield conn
    conn.close()


def test_migration_20_adds_the_table_and_the_layer_column(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 20
    columns = [r[1] for r in conn.execute("PRAGMA table_info(page_types)")]
    assert columns == ["doc_id", "page", "typ", "kopf", "sicherheit", "schleife", "finish", "source", "layer", "raw"]
    assert "layer" in [r[1] for r in conn.execute("PRAGMA table_info(page_texts)")]


def test_store_replaces_the_page(conn):
    seitentypen.store(conn, 1, 3, typ="Unsicher", kopf="Plan", sicherheit=0.4)
    seitentypen.store(conn, 1, 3, typ="Text", kopf="Text", sicherheit=0.99, raw="<x_0.1>…", layer=True,
                      schleife=True, finish="stop")
    rows = conn.execute("SELECT * FROM page_types").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["typ"], row["kopf"], row["sicherheit"], row["schleife"], row["finish"], row["source"], row["layer"],
            row["raw"]) == ("Text", "Text", 0.99, 1, "stop", "modell", 1, "<x_0.1>…")


def test_known_and_clear(conn):
    seitentypen.store(conn, 1, 1, typ="Text")
    seitentypen.store(conn, 1, 2, typ="Plan", source="cad")
    known = seitentypen.known(conn, 1)
    assert sorted(known) == [1, 2] and known[2]["source"] == "cad"
    seitentypen.clear(conn, 1)
    assert seitentypen.known(conn, 1) == {}


def test_rows_go_with_the_document(conn):
    seitentypen.store(conn, 1, 1, typ="Text")
    conn.execute("DELETE FROM documents WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM page_types").fetchone()[0] == 0


def test_unknown_type_is_refused(conn):
    with pytest.raises(ValueError):
        seitentypen.store(conn, 1, 1, typ="Tabelle")


def test_counts_and_summary(conn):
    for page, typ in enumerate(["Text"] * 12 + ["Plan"] * 3 + ["Foto"] + ["Leer"] * 2 + ["Unsicher"], start=1):
        seitentypen.store(conn, 1, page, typ=typ)
    seitentypen.store(conn, 1, 1, typ="Text", schleife=True)
    counts = seitentypen.counts(conn, 1)
    assert counts == {"Text": 12, "Plan": 3, "Foto": 1, "Leer": 2, "Unsicher": 1, "Schleife": 1}
    assert seitentypen.summary(counts) == "12 Text, 3 Plan, 1 Foto, 2 leer, 1 unsicher, 1 Schleife"


def test_summary_leaves_out_what_is_not_there():
    assert seitentypen.summary({"Text": 1, "Plan": 1}) == "1 Text, 1 Plan"
    assert seitentypen.summary({}) == ""


def test_labels():
    assert seitentypen.label("Text") is None
    assert seitentypen.label("Plan") == "Plan"
    assert seitentypen.label("Foto") == "Foto"
    assert seitentypen.label("Leer") == "leer"
    assert seitentypen.label("Unsicher", kopf="Plan") == "Typ unsicher (vermutlich Plan)"
    assert seitentypen.label("Unsicher") == "Typ unsicher"
    assert seitentypen.label("Unsicher", kopf="Unsicher") == "Typ unsicher"
    assert seitentypen.label("Text", schleife=True) == "Schleife"
    assert seitentypen.label(None) is None
