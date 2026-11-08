"""Quality criteria from VISION.md section 9 on the demo data set, plus the inbox workflow."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from projektakte.ai.services import AIServices
from projektakte.assign import certain_project
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.registry import number_regex

from .conftest import fake_settings

RX = number_regex(r"BS-\d{4}-\d{4}")
KNOWN = {"bs-2024-0137": "BS-2024-0137", "bs-2023-0412": "BS-2023-0412"}


def test_certain_rules_order_and_unknown_numbers():
    assert certain_project("BS-2024-0137 Frage", "BS-2023-0412", [], RX, KNOWN)[:2] == ("BS-2024-0137", "subject")
    assert certain_project("Frage", "zu bs-2023-0412", [], RX, KNOWN)[:2] == ("BS-2023-0412", "body")
    assert certain_project("Frage", "", ["BS-2024-0137_Plan.pdf"], RX, KNOWN)[:2] == ("BS-2024-0137", "attachment")
    assert certain_project("BS-2024-0137 und BS-2023-0412", "", [], RX, KNOWN)[:2] == (None, "ambiguous")
    assert certain_project("BS-2099-0001", "", [], RX, KNOWN)[:2] == (None, "unknown")
    assert certain_project("Hallo", "", [], RX, KNOWN)[:2] == (None, None)


@pytest.fixture()
def demo(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    manifest = json.loads((s.demo_dir / "manifest.json").read_text())
    expected = {"eml:" + m["file"].split("/", 1)[1]: m for m in manifest["mails"]}
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    rows = {r["source_key"]: r for r in conn.execute("SELECT * FROM mails")}
    return s, expected, rows


def test_all_mails_with_number_assigned_correctly(demo):
    _, expected, rows = demo
    with_number = [k for k, m in expected.items() if m["category"] in ("subject", "body", "attachment")]
    assert len(with_number) >= 55
    for key in with_number:
        r = rows[key]
        assert (r["project"], r["assign_status"]) == (expected[key]["project"], "assigned"), r["subject"]
        assert r["assign_method"] == expected[key]["category"]


def test_unrelated_mails_never_assigned(demo):
    _, expected, rows = demo
    for key, m in expected.items():
        if m["category"] == "unrelated":
            r = rows[key]
            assert r["project"] is None and r["assign_status"] == "unassigned", r["subject"]
            assert r["suggested_project"] is None, r["subject"]


def test_content_suggestions_mostly_right_and_never_automatic(demo):
    _, expected, rows = demo
    content = [k for k, m in expected.items() if m["category"] == "content"]
    right = 0
    for key in content:
        r = rows[key]
        assert r["project"] is None and r["assign_status"] in ("suggested", "unassigned")
        right += r["suggested_project"] == expected[key]["project"]
    assert right / len(content) > 0.5
    assert right >= 6  # current quality on the demo data: 8 of 8


def test_inbox_and_decisions(demo):
    s, expected, rows = demo
    client = TestClient(create_app(s))
    inbox = client.get("/api/inbox").json()
    keys = {k for k, m in expected.items() if m["category"] in ("content", "unrelated")}
    assert inbox["count"] == len(keys)
    first = inbox["mails"][0]
    assert first["assign_status"] == "suggested" and first["suggested_project_name"]

    # confirm a suggestion
    r = client.post(f"/api/mails/{first['id']}/assign", json={"action": "confirm"}).json()
    assert r["assign_status"] == "confirmed" and r["project"] == first["suggested_project"]
    # its sections now belong to the project
    conn = sqlite3.connect(s.db_path)
    assert {p for (p,) in conn.execute("SELECT project FROM chunks WHERE mail_id = ?", (first["id"],))} == \
        {first["suggested_project"]}

    unrelated = next(m for m in inbox["mails"] if m["subject"].startswith("Rechnung"))
    assert client.post(f"/api/mails/{unrelated['id']}/assign", json={"action": "none"}).json()[
        "assign_status"] == "none"
    other = next(m for m in inbox["mails"] if m["assign_status"] == "suggested" and m["id"] != first["id"])
    r = client.post(f"/api/mails/{other['id']}/assign", json={"action": "reject"}).json()
    assert r["assign_status"] == "unassigned" and r["project"] is None
    manual = client.post(f"/api/mails/{other['id']}/assign",
                         json={"action": "assign", "project": "bs-2024-0137"}).json()
    assert manual["project"] == "BS-2024-0137"

    assert client.get("/api/inbox").json()["count"] == len(keys) - 3
    # errors
    assert client.post(f"/api/mails/{other['id']}/assign",
                       json={"action": "assign", "project": "BS-1999-0001"}).status_code == 400
    assert client.post("/api/mails/999999/assign", json={"action": "none"}).status_code == 404
    assert client.post(f"/api/mails/{other['id']}/assign", json={"action": "kaputt"}).status_code == 400

    # human decisions survive a complete re-import of the mails
    conn.execute("DELETE FROM chunks WHERE mail_id IS NOT NULL")
    conn.execute("DELETE FROM documents WHERE mail_id IS NOT NULL")
    conn.execute("DELETE FROM mails")
    conn.execute("DELETE FROM meta WHERE key LIKE 'eml_stamp:%'")
    conn.commit()
    run_index(s, AIServices.from_settings(s))
    after = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT subject, project, assign_status FROM mails")}
    assert after[first["subject"]] == (first["suggested_project"], "confirmed")
    assert after[unrelated["subject"]] == (None, "none")
    assert after[other["subject"]] == ("BS-2024-0137", "confirmed")


def test_without_embeddings_mails_wait_for_suggestions(tmp_path, fresh_fake_ai):
    generate(tmp_path / "demo")
    fresh_fake_ai.options["fail"] = ["embed"]
    s = fake_settings(fresh_fake_ai, tmp_path, DEMO_MODE=1)
    stats = run_index(s, AIServices.from_settings(s))["assignment"]
    assert stats["assigned"] >= 55 and stats["suggested"] == 0
    fresh_fake_ai.options["fail"] = []
    stats = run_index(s, AIServices.from_settings(s))["assignment"]
    assert stats["suggested"] >= 6
