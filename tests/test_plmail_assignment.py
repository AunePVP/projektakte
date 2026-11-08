"""Assignment in plMail (ASSIGN_MODE=manual): a human picks the project while reading the mail in
plMail, Projektakte only reads that choice via JMAP. Plus the integration API plMail calls."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from projektakte.ai.services import AIServices
from projektakte.config import load_settings
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.mails.jmap_source import PROJECT_CAP, PROJECT_PROP, parse_assignment
from projektakte.main import create_app

from .conftest import fake_env, fake_settings
from .fake_jmap import TOKEN, start_fake_plmail

INTEGRATION = "integrations-token-0123456789"


@pytest.fixture()
def plmail():
    server, fake = start_fake_plmail()
    yield f"http://127.0.0.1:{server.server_address[1]}", fake
    server.shutdown()
    server.server_close()


@pytest.fixture()
def env(tmp_path, fake_ai, plmail):
    url, fake = plmail
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, PLMAIL_URL=url, PLMAIL_TOKEN=TOKEN,
                      INTEGRATION_TOKEN=INTEGRATION, PLMAIL_WEB_URL="https://mail.example/#/{accountId}/{emailId}")
    ids = {
        "num": fake.add("7", "BS-2024-0137 Rückfrage Außentreppe", "Bitte Breite prüfen."),
        "content": fake.add("7", "Lindenhofweg 4 – Muster", "Gitterroststufen für die Außentreppe der Grundschule."),
        "news": fake.add("7", "Newsletter", "Neuigkeiten aus dem Verband."),
    }
    return s, AIServices.from_settings(s), fake, ids


def row(s, key):
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM mails WHERE source_key = ?", (key,)).fetchone()


def test_mode_defaults_to_manual_when_plmail_is_connected(tmp_path):
    base = {"DATA_DIR": str(tmp_path)}
    assert load_settings(base).assign_mode == "auto"
    assert load_settings({**base, "PLMAIL_URL": "http://x", "PLMAIL_TOKEN": "t"}).assign_mode == "manual"
    assert load_settings({**base, "PLMAIL_URL": "http://x", "PLMAIL_TOKEN": "t",
                          "ASSIGN_MODE": "auto"}).assign_mode == "auto"


def test_nothing_is_assigned_automatically(env):
    s, ai, fake, ids = env
    stats = run_index(s, ai)["assignment"]
    for key in ids.values():
        r = row(s, f"jmap:7:{key}")
        assert r["project"] is None and r["assign_status"] == "plmail_open"  # even with a project number
    # eml mails (demo) are not assigned automatically either – a number only proposes
    conn = sqlite3.connect(s.db_path)
    assert conn.execute("SELECT COUNT(*) FROM mails WHERE project IS NOT NULL").fetchone()[0] == 0
    assert stats["assigned"] == 0 and stats["suggested"] >= 60
    assert conn.execute("SELECT COUNT(*) FROM mails WHERE assign_method = 'subject' AND "
                        "assign_status = 'suggested'").fetchone()[0] >= 40
    # the extension is requested only because plMail advertises it
    assert all(PROJECT_CAP in u for u in fake.usings)


def test_assignment_in_plmail_is_taken_over(env):
    s, ai, fake, ids = env
    run_index(s, ai)
    fake.assign("7", ids["content"], "BS-2024-0137", name="Grundschule Am Lindenhof", user="anna")
    fake.assign("7", ids["news"], None, status="none", user="ben")
    run_index(s, ai)
    r = row(s, f"jmap:7:{ids['content']}")
    assert (r["project"], r["assign_status"], r["assign_method"]) == ("BS-2024-0137", "confirmed", "plmail")
    assert "in plMail von anna am 10.03.2025 zugeordnet" == r["assign_reason"]
    conn = sqlite3.connect(s.db_path)
    # its sections now belong to the project → found by project search/questions
    assert {p for (p,) in conn.execute("SELECT project FROM chunks WHERE mail_id = ?", (r["id"],))} == \
        {"BS-2024-0137"}
    n = row(s, f"jmap:7:{ids['news']}")
    assert (n["project"], n["assign_status"]) == (None, "none") and "ben" in n["assign_reason"]

    # re-assign, then remove the assignment again in plMail
    fake.assign("7", ids["content"], "BS-2024-0058")
    run_index(s, ai)
    assert row(s, f"jmap:7:{ids['content']}")["project"] == "BS-2024-0058"
    fake.assign("7", ids["content"], None)
    run_index(s, ai)
    r = row(s, f"jmap:7:{ids['content']}")
    assert (r["project"], r["assign_status"]) == (None, "plmail_open")
    # only reading methods, an assignment change only re-reads the mail via Email/changes
    assert set(fake.methods) <= {"Email/query", "Email/get", "Email/changes"}


def test_unknown_project_from_plmail_is_reported(env):
    s, ai, fake, ids = env
    fake.assign("7", ids["num"], "BS-2099-0001")
    run_index(s, ai)
    r = row(s, f"jmap:7:{ids['num']}")
    assert r["project"] is None and r["assign_status"] == "plmail_open" and "unbekannt" in r["assign_reason"]


def test_projektakte_cannot_override_plmail(env):
    s, ai, fake, ids = env
    run_index(s, ai)
    client = TestClient(create_app(s, background=False))
    mid = row(s, f"jmap:7:{ids['num']}")["id"]
    r = client.post(f"/api/mails/{mid}/assign", json={"action": "assign", "project": "BS-2024-0137"})
    assert r.status_code == 400 and "plMail" in r.json()["detail"]
    page = client.get(f"/mails/{mid}").text
    assert "wird in plMail zugeordnet" in page and f"https://mail.example/#/7/{ids['num']}" in page
    assert "Zuordnung ändern" not in page
    # jmap mails are not in Projektakte's inbox; eml mails (demo) are, with the number as suggestion
    inbox = client.get("/api/inbox").json()["mails"]
    assert all(m["source"] == "eml" for m in inbox) and inbox
    st = client.get("/api/status").json()
    assert st["assign_mode"] == "manual" and st["counts"]["plmail_open"] == 3


def test_auto_mode_plmail_choice_beats_project_number(tmp_path, fake_ai, plmail):
    url, fake = plmail
    s = fake_settings(fake_ai, tmp_path, PLMAIL_URL=url, PLMAIL_TOKEN=TOKEN, ASSIGN_MODE="auto",
                      PROJECTS_DIR=str(tmp_path / "demo" / "projekte"))
    generate(tmp_path / "demo")
    eid = fake.add("7", "BS-2024-0137 Frage", "Text")
    run_index(s, AIServices.from_settings(s))
    assert row(s, f"jmap:7:{eid}")["assign_status"] == "assigned"
    fake.assign("7", eid, "BS-2023-0412")
    run_index(s, AIServices.from_settings(s))
    r = row(s, f"jmap:7:{eid}")
    assert (r["project"], r["assign_method"]) == ("BS-2023-0412", "plmail")


def test_plmail_without_extension_still_works(env):
    s, ai, fake, ids = env
    fake.project_extension = False
    stats = run_index(s, ai)["plmail"]
    assert stats["error"] is None and stats["accounts"]["7"]["new"] == 3
    assert all(PROJECT_CAP not in u for u in fake.usings)


def test_parse_assignment_is_strict():
    assert parse_assignment(None) is None
    assert parse_assignment({"status": "assigned"}) is None
    assert parse_assignment({"status": "kaputt", "project": "X"}) is None
    assert parse_assignment({"status": "assigned", "project": " bs-2024-0137 "})["project"] == "BS-2024-0137"
    assert parse_assignment({"status": "none"})["status"] == "none"


# --------------------------------------------------------------------------- integration API
@pytest.fixture()
def api(env):
    s, ai, fake, ids = env
    run_index(s, ai)
    return TestClient(create_app(s, background=False)), s


H = {"Authorization": f"Bearer {INTEGRATION}"}


def test_integration_needs_token(api, tmp_path, fake_ai):
    client, s = api
    s2 = load_settings({**fake_env(fake_ai, tmp_path), "AUTH_DISABLED": "0", "AUTH_USERS": "",
                        "INTEGRATION_TOKEN": INTEGRATION, "DEMO_MODE": "1", "DATA_DIR": str(s.data_dir)})
    protected = TestClient(create_app(s2, background=False))
    assert protected.get("/api/integration/projects").status_code == 401
    assert protected.get("/api/integration/projects", headers={"Authorization": "Bearer falsch"}).status_code == 401
    assert protected.get("/api/integration/projects", headers=H).status_code == 200
    # the integration token opens only the integration API
    assert protected.get("/api/projects", headers=H).status_code == 401
    assert protected.get("/api/mails", headers=H).status_code == 401


def test_integration_projects_and_info(api):
    client, _ = api
    data = client.get("/api/integration/projects", headers=H).json()
    assert data["count"] == 8
    p = next(x for x in data["projects"] if x["number"] == "BS-2024-0137")
    assert p["name"] == "Grundschule Am Lindenhof" and p["url"].endswith("/projekte/BS-2024-0137")
    assert client.get("/api/integration/projects", params={"q": "Kita"}, headers=H).json()["count"] == 1
    info = client.get("/api/integration/info", headers=H).json()
    assert info["jmap"] == {"capability": PROJECT_CAP, "property": PROJECT_PROP}
    assert info["assign_mode"] == "manual"


def test_integration_suggest(api):
    client, s = api
    by_number = client.post("/api/integration/suggest", headers=H, json={
        "subject": "AW: BS-2023-0412 Feststellanlage", "body": "Abnahme erfolgt.",
        "from": "kortmann@sonnenhang-senioren.example", "attachments": []}).json()["suggestions"]
    assert by_number[0]["number"] == "BS-2023-0412" and by_number[0]["confidence"] == "sicher"
    by_content = client.post("/api/integration/suggest", headers=H, json={
        "subject": "Löschwasserteich", "body": "Für unsere Logistikhalle am Nordring 88 ist der Bagger für den "
        "Löschwasserteich bestellt. Muss die Saugstelle vor der Abnahme fertig sein?",
        "from": "holt@nordring-logistik.example"}).json()["suggestions"]
    assert by_content[0]["number"] == "BS-2023-0777" and by_content[0]["confidence"] == "hoch"
    assert len(by_content) <= 3
    unrelated = client.post("/api/integration/suggest", headers=H, json={
        "subject": "Wartung der Kaffeemaschine", "body": "Am Freitag wird die Kaffeemaschine entkalkt."}).json()
    assert all(x["confidence"] != "sicher" for x in unrelated["suggestions"])
    assert client.post("/api/integration/suggest", headers=H, json={}).status_code == 400
    # suggesting never assigns anything
    conn = sqlite3.connect(s.db_path)
    assert conn.execute("SELECT COUNT(*) FROM mails WHERE project IS NOT NULL").fetchone()[0] == 0


def test_integration_notify_triggers_sync(env):
    s, ai, fake, ids = env
    import time
    with TestClient(create_app(s)) as client:
        def runs():
            return client.get("/api/status").json()["indexing"]
        deadline = time.time() + 20
        while (runs()["runs"] < 1 or runs()["running"]) and time.time() < deadline:
            time.sleep(0.05)
        fake.assign("7", ids["num"], "BS-2024-0137")
        assert client.post("/api/integration/notify", headers=H).status_code == 202
        deadline = time.time() + 20
        while time.time() < deadline and row(s, f"jmap:7:{ids['num']}")["assign_status"] != "confirmed":
            time.sleep(0.05)
        assert row(s, f"jmap:7:{ids['num']}")["project"] == "BS-2024-0137"
