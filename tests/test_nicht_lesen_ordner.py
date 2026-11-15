"""SKIP_READ_FOLDERS: files in folders such as 304000-CAD are listed and open in the viewer, but are not read – no
text, no Tesseract, no model. Files read before are put aside at the start of the next pass."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from projektakte import db
from projektakte.documents import UNREAD_TEXT, index_all_documents
from projektakte.main import create_app
from projektakte.registry import sync_from_folder

from .conftest import fake_settings
from .test_bildauswertung_stufen import digital_with_logo, plain_scan

PATTERN = r"\d{6}"


def _share(root: Path) -> None:
    p = root / "304000_Feuerwehrplan"
    (p / "304000-CAD").mkdir(parents=True)
    (p / "304000-CAD" / "Grundriss EG.pdf").write_bytes(digital_with_logo())
    (p / "304000-CAD" / "Bestand").mkdir()
    (p / "304000-CAD" / "Bestand" / "Scan.pdf").write_bytes(plain_scan())
    (p / "304000-Text").mkdir()
    (p / "304000-Text" / "Brief.pdf").write_bytes(digital_with_logo())


def _db(tmp_path):
    root = tmp_path / "projekte"
    _share(root)
    path = tmp_path / "x.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    sync_from_folder(conn, root, PATTERN)
    conn.commit()
    return conn, root


def _docs(conn) -> dict:
    return {Path(r["rel_path"]).name: dict(r) for r in conn.execute("SELECT * FROM documents")}


def _chunks(conn, name: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE d.filename = ?",
                        (name,)).fetchone()[0]


def test_files_in_a_cad_folder_are_listed_not_read(tmp_path):
    conn, root = _db(tmp_path)
    stats = index_all_documents(conn, projects_dir=root, skip_read=("CAD",))
    docs = _docs(conn)
    for name in ("Grundriss EG.pdf", "Scan.pdf"):  # also in a folder below
        assert (docs[name]["status"], docs[name]["message"], docs[name]["vision_pending"]) == (
            "skipped", UNREAD_TEXT, 0)
        assert _chunks(conn, name) == 0 and docs[name]["sha256"]
    assert docs["Brief.pdf"]["status"] == "ok" and _chunks(conn, "Brief.pdf") > 0  # other folders as before
    assert stats["not_read"] == 2
    again = index_all_documents(conn, projects_dir=root, skip_read=("CAD",))
    assert again.get("not_read", 0) == 0 and again["unchanged"] == 3
    # the folder taken out of the setting: read like an incomplete file
    index_all_documents(conn, projects_dir=root)
    assert _docs(conn)["Grundriss EG.pdf"]["status"] == "ok" and _chunks(conn, "Grundriss EG.pdf") > 0


def test_files_read_before_are_put_aside_at_once(tmp_path):
    conn, root = _db(tmp_path)
    index_all_documents(conn, projects_dir=root)
    assert _chunks(conn, "Grundriss EG.pdf") > 0
    stats = index_all_documents(conn, projects_dir=root, skip_read=("CAD",))
    docs = _docs(conn)
    assert stats["not_read"] == 2
    assert docs["Grundriss EG.pdf"]["message"] == UNREAD_TEXT and _chunks(conn, "Grundriss EG.pdf") == 0
    assert docs["Scan.pdf"]["vision_pending"] == 0
    assert not conn.execute("SELECT 1 FROM page_texts WHERE doc_id = ?", (docs["Scan.pdf"]["id"],)).fetchone()
    assert _chunks(conn, "Brief.pdf") > 0


def test_the_file_still_opens_in_the_viewer(tmp_path, fake_ai):
    root = tmp_path / "projekte"
    _share(root)
    s = fake_settings(fake_ai, tmp_path / "data", PROJECTS_DIR=root, PROJECT_NUMBER_PATTERN=PATTERN,
                      INDEX_INTERVAL=0, SKIP_READ_FOLDERS="CAD")
    assert s.skip_read_folders == ("CAD",)
    from projektakte.indexer import run_index

    run_index(s, None, vision=False)
    conn = db.connect(s.db_path)
    doc_id = conn.execute("SELECT id FROM documents WHERE filename = 'Grundriss EG.pdf'").fetchone()[0]
    conn.close()
    with TestClient(create_app(s, background=False)) as client:
        r = client.get(f"/dokumente/{doc_id}/datei")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert "Grundriss EG.pdf" in client.get("/projekte/304000").text
