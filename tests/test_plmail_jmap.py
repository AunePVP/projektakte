"""plMail via JMAP against a small fake server: session, paging, reordering, attachments,
incremental Email/changes, read-only, error handling."""
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

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
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, PLMAIL_URL=url, PLMAIL_TOKEN=TOKEN, PLMAIL_PAGE_SIZE=2,
                      ASSIGN_MODE="auto")
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
    # paging with limit 2 and one mail overlap → three query pages for account 7, one for account 9,
    # plus one ids-only listing per account (reconciliation); never a write method
    assert fake.methods.count("Email/query") == 3 + 1 + 2
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


# --------------------------------------------------------------------------- robustness (review findings)
def _sync(s):
    """Only the plMail phase (fast): sync into the index database of ``s``."""
    from projektakte.indexer import plmail_client

    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        return sync_plmail(conn, plmail_client(s), cache_dir=s.cache_dir)


def _keys(s):
    return sorted(k.rsplit(":", 1)[1] for k in jmap_mails(s))


def test_deletion_while_paging_loses_no_mail(env):
    """BUG-8: a mail deleted on an already read page shifts the list – no other mail may be lost."""
    s, ai, fake = env
    ids = [fake.add("7", f"Mail {i}", f"Text {i}", received=f"2025-03-0{i}T08:00:00Z") for i in range(1, 8)]
    state = {"done": False}

    def delete_newest_after_first_page(fake_, account, page):
        if not state["done"] and account == "7":
            state["done"] = True
            fake_.destroy("7", page[0])  # the newest mail, already read

    fake.after_query.append(delete_newest_after_first_page)
    stats = _sync(s)["accounts"]["7"]
    assert stats["mode"] == "full" and stats["errors"] == 0
    assert _keys(s) == sorted(ids[:-1])  # everything that still exists, nothing more
    # the next run (Email/changes) keeps it that way
    fake.after_query.clear()
    assert _sync(s)["accounts"]["7"]["mode"] == "changes"
    assert _keys(s) == sorted(ids[:-1])


def test_several_deletions_while_paging_are_recovered_by_the_id_list(env):
    """BUG-8: more deletions than the page overlap – the ids-only reconciliation fetches the rest."""
    s, ai, fake = env
    ids = [fake.add("7", f"Mail {i}", f"Text {i}", received=f"2025-03-{i:02d}T08:00:00Z") for i in range(1, 13)]
    calls = {"n": 0}

    def delete_three_read_mails(fake_, account, page):
        calls["n"] += 1
        if calls["n"] == 2:  # after the second page: three already read mails disappear
            for eid in ids[-3:]:
                fake_.destroy("7", eid)

    fake.after_query.append(delete_three_read_mails)
    stats = _sync(s)["accounts"]["7"]
    assert _keys(s) == sorted(ids[:-3])
    assert stats["recovered"] >= 1  # skipped while paging, found by the id list


def test_known_mail_is_only_removed_when_plmail_confirms(env, monkeypatch):
    """A mail missing from an (incomplete) id list is removed only after Email/get says notFound."""
    s, ai, fake = env
    ids = [fake.add("7", f"Mail {i}", f"Text {i}", received=f"2025-03-0{i}T08:00:00Z") for i in range(1, 4)]
    _sync(s)
    with db.session(s.db_path) as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'jmap_state:%'")  # force a full sync
    original = JMAPClient.query_ids

    def incomplete(self, account_id, position, limit):
        found, qstate, applied = original(self, account_id, position, limit)
        return [i for i in found if i != ids[0]], qstate, applied

    monkeypatch.setattr(JMAPClient, "query_ids", incomplete)
    stats = _sync(s)["accounts"]["7"]
    assert stats["removed"] == 0 and _keys(s) == sorted(ids)
    fake.destroy("7", ids[1])
    with db.session(s.db_path) as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'jmap_state:%'")
    assert _sync(s)["accounts"]["7"]["removed"] == 1 and _keys(s) == sorted([ids[0], ids[2]])


def test_server_side_limit_does_not_end_paging_early(env):
    """plMail may cap the page size (reports ``limit``); a short page is not the end of the mailbox."""
    s, ai, fake = env
    s = replace(s, plmail_page_size=5)
    fake.max_query_limit = 2
    ids = [fake.add("7", f"Mail {i}", f"Text {i}", received=f"2025-03-0{i}T08:00:00Z") for i in range(1, 8)]
    stats = _sync(s)["accounts"]["7"]
    assert stats["new"] == 7 and stats.get("recovered", 0) == 0 and _keys(s) == sorted(ids)


def _make_retries_due(s, account: str) -> None:
    with db.session(s.db_path) as conn:
        key = f"jmap_retry:{account}"
        items = json.loads(conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()[0])
        for item in items.values():
            item["next"] = "2000-01-01T00:00:00+00:00"
        conn.execute("UPDATE meta SET value = ? WHERE key = ?", (json.dumps(items), key))


LONG_NAME = "Prüfbericht_" + "設計図面の確認" * 15 + ".txt"  # 121 characters, > 255 bytes


def test_bad_mail_is_isolated_and_retried(env):
    """BUG-3: an attachment with a file name longer than 255 bytes and a failing download neither
    stop the run nor the other mails; the failed mail is retried later and then stored."""
    s, ai, fake = env
    good = fake.add("7", "BS-2024-0137 Rückfrage", "Bitte prüfen.", received="2025-03-01T08:00:00Z")
    long_name = fake.add("7", "Langer Name", "Anhang mit langem Namen.", received="2025-03-02T08:00:00Z",
                         attachments=[(LONG_NAME, "Rettungsweg im Prüfbericht".encode(), "text/plain")])
    broken = fake.add("7", "Defekter Anhang", "Anhang kommt nicht.", received="2025-03-03T08:00:00Z",
                      attachments=[("Plan.txt", b"Plan Brandabschnitte", "text/plain")])
    fake.failing_blobs[fake.blob_ids("7", broken)[0]] = 503
    stats = run_index(s, ai)  # the whole run goes through: embeddings and assignment, too
    acc = stats["plmail"]["accounts"]["7"]
    assert stats["plmail"]["error"] is None and acc["errors"] == 1 and acc["new"] == 2
    assert stats["assignment"]["assigned"] >= 1
    mails = jmap_mails(s)
    assert f"jmap:7:{broken}" not in mails and mails[f"jmap:7:{good}"]["project"] == "BS-2024-0137"
    conn = sqlite3.connect(s.db_path)
    path = conn.execute("SELECT d.cache_path FROM documents d JOIN mails m ON m.id = d.mail_id "
                        "WHERE m.source_key = ?", (f"jmap:7:{long_name}",)).fetchone()[0]
    assert len(Path(path).name.encode()) <= 255 and Path(path).read_bytes().startswith(b"Rettungsweg")
    assert conn.execute("SELECT filename FROM documents WHERE path LIKE ?", (f"%{LONG_NAME}",)).fetchone()[0] \
        == LONG_NAME  # the original name stays the download name

    # Email/changes reports nothing new – the failed mail is retried from the retry list once due
    fake.failing_blobs.clear()
    _make_retries_due(s, "7")
    acc = run_index(s, ai)["plmail"]["accounts"]["7"]
    assert acc["new"] == 1 and acc["errors"] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents d JOIN mails m ON m.id = d.mail_id "
                        "WHERE m.source_key = ?", (f"jmap:7:{broken}",)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM meta WHERE key = 'jmap_retry:7'").fetchone()[0] == 0


def test_retry_waits_for_its_time(env):
    s, ai, fake = env
    broken = fake.add("7", "Defekter Anhang", "Anhang kommt nicht.",
                      attachments=[("Plan.txt", b"Plan", "text/plain")])
    fake.failing_blobs[fake.blob_ids("7", broken)[0]] = 500
    assert _sync(s)["accounts"]["7"]["errors"] == 1
    fake.downloads.clear()
    stats = _sync(s)["accounts"]["7"]  # not due yet: no new download attempt
    assert stats["errors"] == 0 and fake.downloads == []


def test_missing_attachment_in_plmail_does_not_block_the_mail(env):
    """404 = plMail no longer has the attachment: the mail is stored without it (not retried forever)."""
    s, ai, fake = env
    eid = fake.add("7", "Anhang weg", "Text", attachments=[("Plan.txt", b"Plan", "text/plain")])
    fake.failing_blobs[fake.blob_ids("7", eid)[0]] = 404
    assert _sync(s)["accounts"]["7"]["new"] == 1


def test_no_write_lock_while_downloading(env, monkeypatch):
    """BUG-2: while an attachment is downloaded, another connection (a decision in the inbox) can
    write at once – earlier mails are already committed."""
    s, ai, fake = env
    for i in range(3):
        fake.add("7", f"Mail {i}", "Text", received=f"2025-03-0{i + 1}T08:00:00Z",
                 attachments=[(f"Plan{i}.txt", f"Plan {i}".encode(), "text/plain")])
    results = []
    original = JMAPClient.download

    def download_and_write(self, *args, **kwargs):
        other = sqlite3.connect(s.db_path, timeout=0)
        try:
            other.execute("INSERT INTO meta(key, value) VALUES('probe', 'x') ON CONFLICT(key) DO UPDATE "
                          "SET value = excluded.value")
            other.commit()
            results.append("ok")
        except sqlite3.OperationalError as exc:
            results.append(str(exc))
        finally:
            other.close()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(JMAPClient, "download", download_and_write)
    _sync(s)
    assert results == ["ok"] * 3
