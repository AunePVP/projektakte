"""plMail via JMAP against a small fake server: session, paging, reordering, attachments,
incremental Email/changes, read-only, error handling."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from projektakte import db
from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.demo.writers import pdf_bytes
from projektakte.indexer import run_index
from projektakte.mails.jmap_source import JMAPClient, JMAPError, sync_plmail
from projektakte.main import create_app

from .conftest import fake_settings
from .fake_jmap import TOKEN, start_fake_plmail

READ_ONLY = {"Email/query", "Email/get", "Email/changes"}


@pytest.fixture()
def plmail():
    server, fake = start_fake_plmail()
    yield f"http://127.0.0.1:{server.server_address[1]}", fake
    server.shutdown()
    server.server_close()


def fill(fake):
    ids = {}
    ids["a1"] = fake.add("7", "BS-2024-0137 Rückfrage Außentreppe", "Bitte Breite der Außentreppe prüfen.",
                         received="2025-03-01T08:00:00Z")
    ids["a2"] = fake.add("7", "Prüfbericht", "Anbei der Bericht.", received="2025-03-03T08:00:00Z",
                         attachments=[("BS-2023-0412_Pruefbericht.pdf",
                                       pdf_bytes(["Prüfbericht Pflegeheim: Feststellanlage abgenommen."]),
                                       "application/pdf")])
    ids["a3"] = fake.add("7", "Newsletter", "<p>Neuigkeiten <b>aus dem Verband</b></p>",
                         received="2025-03-02T08:00:00Z", html=True)
    ids["b1"] = fake.add("9", "Termin BS-2022-0901", "Begehung der Tiefgarage am Montag.",
                         received="2025-02-20T08:00:00Z")
    return ids


@pytest.fixture()
def env(tmp_path, fake_ai, plmail):
    url, fake = plmail
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, PLMAIL_URL=url, PLMAIL_TOKEN=TOKEN, PLMAIL_PAGE_SIZE=2)
    return s, AIServices.from_settings(s), fake


def jmap_mails(s):
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    return {r["source_key"]: dict(r) for r in conn.execute("SELECT * FROM mails WHERE source = 'jmap'")}


def test_initial_sync_all_accounts_with_paging(env):
    s, ai, fake = env
    ids = fill(fake)
    stats = run_index(s, ai)["plmail"]
    assert stats["error"] is None
    assert stats["accounts"]["7"]["new"] == 3 and stats["accounts"]["7"]["mode"] == "full"
    assert stats["accounts"]["9"]["new"] == 1
    mails = jmap_mails(s)
    assert len(mails) == 4
    m = mails[f"jmap:7:{ids['a1']}"]
    assert m["project"] == "BS-2024-0137" and m["assign_status"] == "assigned"
    assert m["date"] == "2025-03-01T08:00:00+00:00" and m["sender"] == "bauamt@x.example"
    # attachment downloaded, indexed, and its file name assigns the project
    a2 = mails[f"jmap:7:{ids['a2']}"]
    assert a2["project"] == "BS-2023-0412" and a2["assign_method"] == "attachment"
    conn = sqlite3.connect(s.db_path)
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'feststellanlage' AND rowid IN "
                        "(SELECT c.id FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE d.mail_id = ?)",
                        (a2["id"],)).fetchone()[0] == 1
    # HTML-only mail converted to text
    assert mails[f"jmap:7:{ids['a3']}"]["body"].startswith("Neuigkeiten aus dem Verband")
    # paging with limit 2 → two query pages for account 7; never a write method
    assert fake.methods.count("Email/query") == 3
    assert set(fake.methods) <= READ_ONLY


def test_incremental_sync_uses_changes(env):
    s, ai, fake = env
    ids = fill(fake)
    run_index(s, ai)
    fake.methods.clear()
    new = fake.add("7", "BS-2025-0019 Rauchwarnmelder", "Funkvernetzte Melder bestellt.",
                   received="2025-03-05T08:00:00Z")
    fake.flag("7", ids["a1"])  # only a flag: no re-import
    fake.destroy("7", ids["a3"])
    stats = run_index(s, ai)["plmail"]["accounts"]["7"]
    assert stats["mode"] == "changes"
    assert (stats["new"], stats["unchanged"], stats["removed"]) == (1, 1, 1)
    assert "Email/query" not in fake.methods and set(fake.methods) <= READ_ONLY
    mails = jmap_mails(s)
    assert f"jmap:7:{new}" in mails and f"jmap:7:{ids['a3']}" not in mails
    assert mails[f"jmap:7:{new}"]["project"] == "BS-2025-0019"
    # nothing new → just one Email/changes per account
    fake.methods.clear()
    run_index(s, ai)
    assert fake.methods == ["Email/changes", "Email/changes"]


def test_many_changes_are_paged(env):
    s, ai, fake = env
    fill(fake)
    run_index(s, ai)
    for i in range(5):
        fake.add("9", f"Mail {i}", "Text", received=f"2025-04-0{i + 1}T08:00:00Z")
    stats = run_index(s, ai)["plmail"]["accounts"]["9"]
    assert stats["new"] == 5 and fake.methods.count("Email/changes") >= 3  # maxChanges = page size 2


def test_cannot_calculate_changes_falls_back_to_full_sync(env):
    s, ai, fake = env
    fill(fake)
    run_index(s, ai)
    fake.forget_history = True
    stats = run_index(s, ai)["plmail"]["accounts"]["7"]
    assert stats["mode"] == "full" and stats["unchanged"] == 3


def test_session_fallback_and_reordering(plmail, tmp_path):
    url, fake = plmail
    fake.well_known = False
    ids = fill(fake)
    client = JMAPClient(url, TOKEN, page_size=10)
    assert client.account_ids() == ["7", "9"]
    emails, state, query_ids = client.query_page("7", 0)
    assert [e["id"] for e in emails] == query_ids == [ids["a2"], ids["a3"], ids["a1"]]  # newest first
    assert state == "3"


def test_wrong_token_and_bad_requests(plmail, tmp_path):
    url, fake = plmail
    fill(fake)
    bad = JMAPClient(url, "plmail_" + "00" * 32)
    with pytest.raises(JMAPError, match="App-Passwort"):
        bad.account_ids()
    db.init_db(tmp_path / "x.sqlite3")
    with db.session(tmp_path / "x.sqlite3") as conn:
        result = sync_plmail(conn, bad, cache_dir=tmp_path)
    assert "App-Passwort" in result["error"]
    good = JMAPClient(url, TOKEN)
    with pytest.raises(JMAPError, match="invalidArguments"):
        good.call([["Email/get", {"accountId": 7, "ids": []}, "g"]])  # number instead of string
    # request-level errors come as application/problem+json with status 400
    from projektakte.ai.http import HTTPStatusError, request_json
    with pytest.raises(HTTPStatusError) as info:
        request_json("POST", good.session()["apiUrl"], payload={"kaputt": True},
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert info.value.status == 400 and "notRequest" in info.value.body


def test_problem_json_detail_is_reported(plmail, monkeypatch):
    url, fake = plmail
    fill(fake)
    client = JMAPClient(url, TOKEN)
    client.session()
    from projektakte.ai.http import HTTPStatusError
    from projektakte.mails import jmap_source

    def boom(*a, **k):
        raise HTTPStatusError(400, '{"type": "urn:ietf:params:jmap:error:limit", "detail": "zu viele"}', url)
    monkeypatch.setattr(jmap_source, "request_json", boom)
    with pytest.raises(JMAPError, match="HTTP 400.*zu viele"):
        client.call([["Email/query", {"accountId": "7"}, "q"]])


def test_plmail_down_does_not_break_indexing(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, PLMAIL_URL="http://127.0.0.1:9", PLMAIL_TOKEN=TOKEN)
    stats = run_index(s, AIServices.from_settings(s))
    assert "nicht erreichbar" in stats["plmail"]["error"]
    assert stats["mails"]["new"] >= 60  # the eml source still works


def test_health_deps_reports_plmail(env):
    s, _, fake = env
    fill(fake)
    data = TestClient(create_app(s, background=False)).get("/health/deps").json()
    assert data["plmail"]["ok"] and data["plmail"]["accounts"] == 2 and data["plmail"]["format"] == "jmap"
    assert TOKEN not in str(data)
