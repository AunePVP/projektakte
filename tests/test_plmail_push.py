"""Push from plMail (docs/plmail-push.md): plMail sends the mails a human assigned to a project, changes of
the assignment and deletions; Projektakte needs no access to the mailbox."""
import json
import sqlite3
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.demo.writers import pdf_bytes
from projektakte.indexer import run_index
from projektakte.main import create_app

from .conftest import fake_settings

TOKEN = "integrations-token-0123456789"
H = {"Authorization": f"Bearer {TOKEN}"}
ASSIGNED = {"status": "assigned", "project": "BS-2024-0137", "projectName": "Grundschule",
            "assignedBy": "anna", "assignedAt": "2026-11-18T09:00:00Z"}


def eml(subject="Rückfrage Außentreppe", attachment=True) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Bauamt <bauamt@x.example>", "buero@x.example", subject
    m["Date"] = "Tue, 18 Nov 2026 09:00:00 +0100"
    m.set_content("Bitte die Breite der Außentreppe prüfen.")
    if attachment:
        m.add_attachment(pdf_bytes(["Prüfbericht Treppe"]), maintype="application", subtype="pdf",
                         filename="Pruefbericht.pdf")
    return m.as_bytes()


@pytest.fixture()
def env(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, INTEGRATION_TOKEN=TOKEN)
    ai = AIServices.from_settings(s)
    run_index(s, ai)  # projects of the demo data
    with TestClient(create_app(s, background=False)) as client:
        yield s, ai, client


def mail(s, eid="M1"):
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM mails WHERE source_key = ?", (f"jmap:7:{eid}",)).fetchone()
    docs = conn.execute("SELECT COUNT(*) FROM documents WHERE mail_id = ?", (row["id"],)).fetchone()[0] if row else 0
    return row, docs


def put(client, eid="M1", assignment=ASSIGNED, body=None):
    headers = {**H, "Content-Type": "message/rfc822"}
    if assignment is not None:
        headers["X-Projektakte-Zuordnung"] = json.dumps(assignment)
    return client.put(f"/api/integration/mails/7/{eid}", content=eml() if body is None else body, headers=headers)


def test_pushed_mail_is_imported_with_its_assignment(env):
    s, ai, client = env
    r = put(client)
    assert r.status_code == 202 and r.json()["status"] == "angenommen"
    assert client.get("/api/integration/mails/7/M1", headers=H).json()["wartet"] is True
    run_index(s, ai)
    row, docs = mail(s)
    assert row["source"] == "jmap" and row["project"] == "BS-2024-0137" and docs == 1
    assert json.loads(row["external_assignment"])["assignedBy"] == "anna"
    assert not list((s.data_dir / "plmail-eingang").glob("*.eml"))
    state = client.get("/api/integration/mails/7/M1", headers=H).json()
    assert state["vorhanden"] is True and state["wartet"] is False


def test_assignment_change_and_deletion(env):
    s, ai, client = env
    put(client)
    run_index(s, ai)
    r = client.put("/api/integration/mails/7/M1/zuordnung", headers=H,
                   json={**ASSIGNED, "project": "BS-2023-0412"})
    assert r.json()["status"] == "geändert"
    run_index(s, ai)
    assert mail(s)[0]["project"] == "BS-2023-0412"
    r = client.put("/api/integration/mails/7/M1/zuordnung", headers=H, json={"status": "none"})
    assert r.json()["status"] == "gelöscht" and mail(s) == (None, 0)
    assert client.delete("/api/integration/mails/7/M1", headers=H).json()["status"] == "nicht vorhanden"
    put(client, "M2")
    assert client.delete("/api/integration/mails/7/M2", headers=H).json()["status"] == "gelöscht"  # still waiting
    run_index(s, ai)
    assert mail(s, "M2") == (None, 0)


def test_only_assigned_mails_are_accepted(env):
    s, ai, client = env
    assert put(client, assignment=None).status_code == 400
    assert put(client, assignment={"status": "none"}).status_code == 422
    assert put(client, body=b"").status_code == 400
    assert client.put("/api/integration/mails/7/..%2Fx", headers=H, content=eml()).status_code in (400, 404)
    r = client.put("/api/integration/mails/7/M9/zuordnung", headers=H, json=ASSIGNED)
    assert r.status_code == 404  # unknown: plMail sends the mail itself first
    assert not list((s.data_dir / "plmail-eingang").glob("*.eml"))
