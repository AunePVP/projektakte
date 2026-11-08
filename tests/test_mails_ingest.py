import json
import sqlite3
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.mails.model import parse_eml
from projektakte.main import create_app

from .conftest import fake_settings


def test_parse_eml_headers_body_attachments():
    m = EmailMessage()
    m["From"] = "Dipl.-Ing. Sabine Wendehals <Pruefung@Wendehals-PI.example>"
    m["To"] = "Tobias <t@x.example>"
    m["Cc"] = "Lea <l@x.example>"
    m["Subject"] = "Prüfbericht"
    m["Date"] = "Wed, 19 Apr 2023 17:21:00 +0100"
    m.set_content("Text mit Ümlaut")
    m.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="Bericht Nr. 1.pdf")
    rec = parse_eml(m.as_bytes(), "eml:a.eml")
    assert rec.sender == "pruefung@wendehals-pi.example" and rec.sender_name == "Dipl.-Ing. Sabine Wendehals"
    assert rec.recipients == "t@x.example, l@x.example"
    assert rec.date == "2023-04-19T16:21:00+00:00"
    assert rec.body == "Text mit Ümlaut"
    assert [(a.filename, a.mime) for a in rec.attachments] == [("Bericht Nr. 1.pdf", "application/pdf")]
    assert len(rec.content_hash) == 64


@pytest.fixture()
def indexed(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    stats = run_index(s, AIServices.from_settings(s))
    return s, stats


def test_demo_mails_are_imported(indexed):
    s, stats = indexed
    manifest = json.loads((s.demo_dir / "manifest.json").read_text())
    assert stats["mails"]["new"] == len(manifest["mails"]) and stats["mails"]["errors"] == 0
    conn = sqlite3.connect(s.db_path)
    assert conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0] == len(manifest["mails"])
    n_att = sum(len(m["attachments"]) for m in manifest["mails"])
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE mail_id IS NOT NULL").fetchone()[0] == n_att
    # attachments are indexed like documents (chunks + FTS + vectors)
    hits = conn.execute("SELECT d.filename FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                        "JOIN documents d ON d.id = c.doc_id WHERE chunks_fts MATCH 'saugstelle' "
                        "AND d.mail_id IS NOT NULL").fetchall()
    assert ("Stellungnahme Feuerwehr.pdf",) in hits
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0] == 0
    # mail bodies are searchable
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts f JOIN chunks c ON c.id=f.rowid "
                        "WHERE chunks_fts MATCH 'stichflurs' AND c.doc_id IS NULL").fetchone()[0] >= 1
    # HTML newsletter and ISO-8859-1 mail decoded
    body = conn.execute("SELECT body FROM mails WHERE subject LIKE 'Newsletter%'").fetchone()[0]
    assert "<p>" not in body and "Rauchableitung" in body
    assert conn.execute("SELECT COUNT(*) FROM mails WHERE body LIKE '%Garage unter dem Marktplatz%'"
                        ).fetchone()[0] == 1
    # attachment copies live in our own cache, the mail folder stays untouched
    path = conn.execute("SELECT cache_path FROM documents WHERE mail_id IS NOT NULL LIMIT 1").fetchone()[0]
    assert str(s.cache_dir) in path


def test_mail_import_is_incremental(indexed):
    s, _ = indexed
    stats = run_index(s, AIServices.from_settings(s))["mails"]
    assert stats["new"] == stats["changed"] == 0
    victim = sorted((s.mail_dir).glob("*.eml"))[0]
    victim.unlink()
    assert run_index(s, AIServices.from_settings(s))["mails"]["removed"] == 1


def test_mails_api(indexed):
    s, _ = indexed
    client = TestClient(create_app(s))
    data = client.get("/api/mails", params={"limit": 5}).json()
    assert data["total"] >= 60 and data["count"] == 5
    dates = [m["date"] for m in data["mails"]]
    assert dates == sorted(dates, reverse=True)
    found = client.get("/api/mails", params={"q": "Stichflur"}).json()["mails"]
    assert any("Abweichungsantrag" in m["subject"] for m in found)
    with_att = next(m for m in client.get("/api/mails", params={"limit": 200}).json()["mails"]
                    if m["attachments"])
    detail = client.get(f"/api/mails/{with_att['id']}").json()
    assert detail["body"] and detail["attachments"][0]["chunks"] >= 1
    assert client.get("/api/mails/999999").status_code == 404
