import json
import sqlite3
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte import db
from projektakte.ai.services import AIServices
from projektakte.assign import assign_mails, decide
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.mails import eml_source, store
from projektakte.mails.eml_source import sync_eml_folder
from projektakte.mails.model import parse_eml
from projektakte.mails.store import bulk_markers, safe_filename, upgrade_mail_texts
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


# --------------------------------------------------------------------------- robustness (review findings)
PATTERN = r"BS-\d{4}-\d{4}"


def write_eml(path, subject, body, *, date="Tue, 18 Jun 2024 10:30:00 +0200", attachments=(), headers=(),
              msgid=None, sender="Anna Bauer <anna@bauamt.example>"):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "buero@ib.example"
    m["Subject"] = subject
    m["Date"] = date
    m["Message-ID"] = msgid or f"<{abs(hash((subject, body)))}@bauamt.example>"
    for k, v in headers:
        m[k] = v
    m.set_content(body)
    for name, data in attachments:
        m.add_attachment(data, maintype="text", subtype="plain", filename=name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(m))
    return path


@pytest.fixture()
def maildb(tmp_path):
    path = tmp_path / "x.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO projects(number, name, path) VALUES('BS-2024-0001', 'Testhalle', ?)",
                 (str(tmp_path / "p"),))
    conn.commit()
    yield conn, tmp_path / "mails", tmp_path / "cache", path
    conn.close()


def sync(conn, mail_dir, cache, **kw):
    return sync_eml_folder(conn, mail_dir, cache_dir=cache, **kw)


def test_safe_filename_counts_bytes_and_keeps_the_extension():
    long_name = "Prüfbericht_" + "設計図面の確認" * 15 + ".pdf"
    safe = safe_filename(long_name)
    assert len(safe.encode()) <= 180 and safe.endswith(".pdf") and safe.startswith("Prüfbericht_")
    assert safe_filename("../../etc/passwd") == "_.._etc_passwd"
    assert safe_filename("..") == "anhang" and safe_filename("") == "anhang"
    assert safe_filename("a\x00b\nc.txt") == "a_b_c.txt"
    odd = safe_filename("x." + "y" * 300)  # absurd "extension" is cut like the rest of the name
    assert len(odd.encode()) <= 180


def test_long_attachment_name_is_stored(maildb):
    """BUG-3/BUG-4: an attachment name of more than 255 bytes is stored (cache name cut in bytes)."""
    conn, mail_dir, cache, _ = maildb
    name = "Prüfbericht_" + "設計図面の確認" * 15 + ".txt"
    write_eml(mail_dir / "lang.eml", "Rückfrage BS-2024-0001", "Bitte prüfen",
              attachments=[(name, "Rettungsweg".encode())])
    assert sync(conn, mail_dir, cache)["errors"] == 0
    filename, cache_path = conn.execute("SELECT filename, cache_path FROM documents").fetchone()
    assert filename == name and Path(cache_path).read_bytes().startswith(b"Rettungsweg")


def test_failed_mail_leaves_nothing_behind_and_is_retried(maildb, monkeypatch):
    """BUG-4: a failure after the mail row was written rolls everything back; the next run stores
    the mail completely (with its attachment) instead of reporting it as unchanged."""
    conn, mail_dir, cache, _ = maildb
    write_eml(mail_dir / "ok.eml", "Erste Mail", "Text")
    write_eml(mail_dir / "anhang.eml", "Prüfbericht", "Anbei", attachments=[("Bericht.txt", b"Rettungsweg frei")])
    real = store.write_document

    def boom(*args, **kwargs):
        raise OSError("Platte voll")
    monkeypatch.setattr(store, "write_document", boom)
    stats = sync(conn, mail_dir, cache)
    assert (stats["new"], stats["errors"]) == (1, 1)
    assert [r[0] for r in conn.execute("SELECT subject FROM mails")] == ["Erste Mail"]
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1  # only the first mail's body
    monkeypatch.setattr(store, "write_document", real)
    stats = sync(conn, mail_dir, cache)
    assert (stats["new"], stats["errors"], stats["unchanged"]) == (1, 0, 1)
    assert [r[0] for r in conn.execute("SELECT d.filename FROM documents d JOIN mails m ON m.id = d.mail_id "
                                       "WHERE m.subject = 'Prüfbericht'")] == ["Bericht.txt"]


def test_no_write_lock_while_extracting_attachments(maildb, monkeypatch):
    """BUG-2: attachments are extracted before the write transaction; earlier mails are committed."""
    conn, mail_dir, cache, path = maildb
    for i in range(3):
        write_eml(mail_dir / f"m{i}.eml", f"Mail {i}", "Text", attachments=[(f"A{i}.txt", f"Inhalt {i}".encode())])
    results = []
    real = store.prepare_document

    def extract_and_write(**kwargs):
        other = sqlite3.connect(path, timeout=0)  # fails at once if the database is locked
        try:
            other.execute("INSERT INTO meta(key, value) VALUES('probe', 'x') ON CONFLICT(key) DO UPDATE "
                          "SET value = excluded.value")
            other.commit()
            results.append("ok")
        except sqlite3.OperationalError as exc:
            results.append(str(exc))
        finally:
            other.close()
        return real(**kwargs)

    monkeypatch.setattr(store, "prepare_document", extract_and_write)
    assert sync(conn, mail_dir, cache)["new"] == 3
    assert results == ["ok"] * 3


def test_moved_eml_keeps_mail_and_human_decision(maildb):
    """BUG-9: moving/renaming a file inside MAIL_DIR keeps the mail id, its sections and the decision."""
    conn, mail_dir, cache, _ = maildb
    src = write_eml(mail_dir / "2024-06-18.eml", "Frage zur Halle", "Wie breit wird das Tor?")
    sync(conn, mail_dir, cache)
    assign_mails(conn, PATTERN, min_share=0.5, min_lead=0.2, vector_key="k")
    mail_id = conn.execute("SELECT id FROM mails").fetchone()[0]
    chunks = conn.execute("SELECT id FROM chunks WHERE mail_id = ?", (mail_id,)).fetchall()
    decide(conn, mail_id, "assign", "BS-2024-0001", user="anna")
    (mail_dir / "archiv").mkdir()
    src.rename(mail_dir / "archiv" / "umbenannt.eml")
    stats = sync(conn, mail_dir, cache)
    assert (stats["moved"], stats["new"], stats["removed"]) == (1, 0, 0)
    row = conn.execute("SELECT id, source_key, project, assign_status FROM mails").fetchone()
    assert tuple(row) == (mail_id, "eml:archiv/umbenannt.eml", "BS-2024-0001", "confirmed")
    assert conn.execute("SELECT id FROM chunks WHERE mail_id = ?", (mail_id,)).fetchall() == chunks
    assign_mails(conn, PATTERN, min_share=0.5, min_lead=0.2, vector_key="k")
    assert tuple(conn.execute("SELECT assign_status, assign_reason FROM mails").fetchone()) == \
        ("confirmed", "Von anna zugeordnet.")


def test_decision_survives_removal_and_reimport_under_another_name(maildb):
    """BUG-9: the decision is also stored by Message-ID – a re-exported file with a new name gets it."""
    conn, mail_dir, cache, _ = maildb
    src = write_eml(mail_dir / "a.eml", "Frage zur Halle", "Wie breit wird das Tor?", msgid="<abc@x.example>")
    sync(conn, mail_dir, cache)
    assign_mails(conn, PATTERN, min_share=0.5, min_lead=0.2, vector_key="k")
    decide(conn, conn.execute("SELECT id FROM mails").fetchone()[0], "none", user="ben")
    src.unlink()
    write_eml(mail_dir / "dummy.eml", "Andere Mail", "Text")  # MAIL_DIR is not empty
    assert sync(conn, mail_dir, cache)["removed"] == 1
    write_eml(mail_dir / "neu" / "export.eml", "Frage zur Halle", "Wie breit wird das Tor? (neu exportiert)",
              msgid="<abc@x.example>")
    sync(conn, mail_dir, cache)
    assign_mails(conn, PATTERN, min_share=0.5, min_lead=0.2, vector_key="k")
    row = conn.execute("SELECT assign_status, assign_reason FROM mails WHERE source_key = 'eml:neu/export.eml'"
                       ).fetchone()
    assert tuple(row) == ("none", "Von ben als ohne Projektbezug markiert.")


def test_missing_or_empty_mail_dir_deletes_nothing(maildb):
    """ops-06: an unmounted network drive (missing or empty MAIL_DIR) must not empty the index."""
    conn, mail_dir, cache, _ = maildb
    for i in range(3):
        write_eml(mail_dir / f"m{i}.eml", f"Mail {i}", "Text")
    assert sync(conn, mail_dir, cache)["new"] == 3
    mail_dir.rename(mail_dir.with_name("weg"))
    stats = sync(conn, mail_dir, cache)
    assert stats["removed"] == 0 and "nicht erreichbar" in stats["warning"]
    mail_dir.mkdir()  # mount point without the mount
    stats = sync(conn, mail_dir, cache)
    assert stats["removed"] == 0 and "keine .eml-Dateien mehr (bisher 3)" in stats["warning"]
    assert conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0] == 3
    # emptied on purpose
    assert sync(conn, mail_dir, cache, allow_empty=True)["removed"] == 3


def test_unreadable_subfolder_deletes_nothing(maildb, monkeypatch):
    conn, mail_dir, cache, _ = maildb
    write_eml(mail_dir / "a.eml", "Mail a", "Text")
    write_eml(mail_dir / "sub" / "b.eml", "Mail b", "Text")
    assert sync(conn, mail_dir, cache)["new"] == 2
    real_walk = eml_source.os.walk

    def walk_without_sub(top, onerror=None):
        for dirpath, dirnames, filenames in real_walk(top):
            if "sub" in dirnames:
                dirnames.remove("sub")
                onerror(PermissionError(13, "Keine Berechtigung", str(Path(dirpath) / "sub")))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(eml_source.os, "walk", walk_without_sub)
    stats = sync(conn, mail_dir, cache)
    assert stats["removed"] == 0 and "nicht lesbar" in stats["warning"]
    assert conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0] == 2


def test_section_header_and_attachment_date_use_local_time(maildb):
    """DEMO-5/BUG-11: 00:30 in Berlin is 22:30 UTC the day before – shown as on the mail page."""
    conn, mail_dir, cache, _ = maildb
    write_eml(mail_dir / "nacht.eml", "Nachtmail", "Text", date="Tue, 18 Jun 2024 00:30:00 +0200",
              attachments=[("Notiz.txt", b"Rettungsweg")])
    sync(conn, mail_dir, cache)
    assert conn.execute("SELECT date FROM mails").fetchone()[0] == "2024-06-17T22:30:00+00:00"  # stored in UTC
    text = conn.execute("SELECT text FROM chunks WHERE doc_id IS NULL").fetchone()[0]
    assert "Datum: 18.06.2024 00:30" in text and "T22:30" not in text
    assert conn.execute("SELECT doc_date FROM documents").fetchone()[0] == "2024-06-18"


def test_old_iso_header_is_rewritten_once(maildb):
    """DEMO-5: sections indexed by older versions get the local German date (in place, vectors stay)."""
    conn, mail_dir, cache, _ = maildb
    write_eml(mail_dir / "a.eml", "Mail", "Text", date="Wed, 26 Feb 2025 16:58:00 +0100")
    sync(conn, mail_dir, cache)
    cid, text = conn.execute("SELECT id, text FROM chunks WHERE doc_id IS NULL").fetchone()
    old = text.replace("Datum: 26.02.2025 16:58", "Datum: 2025-02-26T15:58:00+00:00")
    conn.execute("UPDATE chunks SET text = ?, embedding = x'00' WHERE id = ?", (old, cid))
    conn.execute("UPDATE chunks_fts SET body = ? WHERE rowid = ?", (old.lower(), cid))
    conn.execute("DELETE FROM meta WHERE key = 'mail_header_format'")
    conn.commit()
    assert upgrade_mail_texts(conn) == 1 and upgrade_mail_texts(conn) == 0
    text, emb = conn.execute("SELECT text, embedding FROM chunks WHERE id = ?", (cid,)).fetchone()
    assert "Datum: 26.02.2025 16:58" in text and emb == b"\x00"
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH '\"26.02.2025\"'").fetchone()[0] == 1


def test_newsletter_header_is_remembered(maildb):
    conn, mail_dir, cache, _ = maildb
    write_eml(mail_dir / "n.eml", "Fachartikel", "Text", headers=[("List-Unsubscribe", "<mailto:x@y.example>")])
    write_eml(mail_dir / "p.eml", "Rundschreiben", "Text", headers=[("Precedence", "bulk")])
    write_eml(mail_dir / "m.eml", "Normale Mail", "Text")
    sync(conn, mail_dir, cache)
    assert bulk_markers(conn) == {"eml:n.eml": "List-Unsubscribe", "eml:p.eml": "Precedence: bulk"}
    (mail_dir / "n.eml").unlink()
    sync(conn, mail_dir, cache)
    assert set(bulk_markers(conn)) == {"eml:p.eml"}


# --------------------------------------------------------------------------- round 2
def test_links_in_mail_dir_bring_nothing_from_outside(maildb, tmp_path):
    """Like SEC-02 for MAIL_DIR: a link to a file outside is skipped (also a broken one); a link to a
    file inside MAIL_DIR is fine."""
    conn, mail_dir, cache, _ = maildb
    write_eml(mail_dir / "a.eml", "Echte Mail", "Text")
    secret = write_eml(tmp_path / "outside" / "geheim.eml", "Gehaltsliste", "STRENG GEHEIM Zebrafink")
    (mail_dir / "zz_link.eml").symlink_to(secret)
    (mail_dir / "kaputt.eml").symlink_to(tmp_path / "gibt-es-nicht.eml")
    (mail_dir / "archiv").mkdir()
    (mail_dir / "archiv" / "kopie.eml").symlink_to(mail_dir / "a.eml")
    stats = sync(conn, mail_dir, cache)
    assert (stats["new"], stats["errors"]) == (2, 0)
    assert sorted(r[0] for r in conn.execute("SELECT source_key FROM mails")) == ["eml:a.eml", "eml:archiv/kopie.eml"]
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE text LIKE '%Zebrafink%'").fetchone()[0] == 0
    # a mail that got in through such a link earlier (older version) leaves the index
    rec = parse_eml(secret.read_bytes(), "eml:zz_link.eml")
    store.write_mail(conn, store.prepare_mail(conn, rec, cache_dir=cache))
    conn.commit()
    assert sync(conn, mail_dir, cache)["removed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE text LIKE '%Zebrafink%'").fetchone()[0] == 0


def test_file_swapped_for_a_link_is_not_read(maildb, tmp_path):
    """Between the scan and the read a file is replaced by a link to outside: refused."""
    conn, mail_dir, cache, _ = maildb
    write_eml(mail_dir / "a.eml", "Echte Mail", "Text")
    secret = write_eml(tmp_path / "outside" / "geheim.eml", "Gehaltsliste", "STRENG GEHEIM")
    files, _ = eml_source._scan(mail_dir, mail_dir.resolve())
    (mail_dir / "a.eml").unlink()
    (mail_dir / "a.eml").symlink_to(secret)
    with pytest.raises(PermissionError):
        eml_source._read_inside(files[0][1], mail_dir.resolve())


def test_newsletter_headers_of_older_imports_are_read_once(maildb):
    """Mails imported before newsletter headers were kept get their marker from the file (headers
    only, once per database); a mail waiting in the inbox is evaluated again."""
    conn, mail_dir, cache, _ = maildb
    write_eml(mail_dir / "n.eml", "Fachartikel", "Text", headers=[("List-Unsubscribe", "<mailto:x@y.example>")])
    write_eml(mail_dir / "m.eml", "Normale Mail", "Text")
    sync(conn, mail_dir, cache)
    conn.execute("UPDATE mails SET assign_status = 'unassigned', suggest_done = 1")
    conn.execute("DELETE FROM meta WHERE key LIKE 'mail\\_bulk%' ESCAPE '\\'")  # state of the older version
    conn.commit()
    assert sync(conn, mail_dir, cache)["unchanged"] == 2
    assert bulk_markers(conn) == {"eml:n.eml": "List-Unsubscribe"}
    assert dict(conn.execute("SELECT source_key, suggest_done FROM mails").fetchall()) == \
        {"eml:n.eml": 0, "eml:m.eml": 1}
    conn.execute("DELETE FROM meta WHERE key = 'mail_bulk:eml:n.eml'")
    conn.commit()
    sync(conn, mail_dir, cache)  # done once – not read again on every run
    assert bulk_markers(conn) == {}


def test_attachment_dates_of_older_imports_become_the_local_day(maildb):
    """BUG-11 for existing databases: older versions dated attachments with the UTC day."""
    conn, mail_dir, cache, _ = maildb
    write_eml(mail_dir / "nacht.eml", "Nachtmail", "Text", date="Tue, 18 Jun 2024 00:30:00 +0200",
              attachments=[("Notiz.txt", b"Rettungsweg")])
    write_eml(mail_dir / "tag.eml", "Tagmail", "Text", date="Tue, 18 Jun 2024 10:30:00 +0200",
              attachments=[("Bericht_2023-01-05.txt", b"Rettungsweg")])
    sync(conn, mail_dir, cache)
    conn.execute("UPDATE documents SET doc_date = '2024-06-17' WHERE filename = 'Notiz.txt'")  # old: UTC day
    conn.execute("UPDATE documents SET doc_date = '2023-01-05' WHERE filename LIKE 'Bericht%'")
    conn.execute("DELETE FROM meta WHERE key = 'mail_attachment_dates'")
    conn.commit()
    assert store.upgrade_attachment_dates(conn) == 1 and store.upgrade_attachment_dates(conn) == 0
    assert dict(conn.execute("SELECT filename, doc_date FROM documents").fetchall()) == \
        {"Notiz.txt": "2024-06-18", "Bericht_2023-01-05.txt": "2023-01-05"}


def test_import_waits_for_the_lock_instead_of_failing(maildb):
    """SQLITE_BUSY_SNAPSHOT: another connection commits between the import's first read and its
    first write. With BEGIN IMMEDIATE the import holds the lock from the start – the other side
    waits, the mail is stored (before: 'database is locked' at once, mail counted as error)."""
    conn, mail_dir, cache, path = maildb
    write_eml(mail_dir / "a.eml", "Mail a", "Text")
    real = store._write_row
    others = []

    def write_from_elsewhere_first(*args, **kwargs):
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute("INSERT INTO meta(key, value) VALUES('probe', 'x') ON CONFLICT(key) DO UPDATE "
                          "SET value = excluded.value")
            other.commit()
            others.append("written")
        except sqlite3.OperationalError as exc:
            others.append(str(exc))
        finally:
            other.close()
        return real(*args, **kwargs)

    store._write_row = write_from_elsewhere_first
    try:
        stats = sync(conn, mail_dir, cache)
    finally:
        store._write_row = real
    assert (stats["new"], stats["errors"]) == (1, 0)
    assert others == ["database is locked"]  # the other connection had to wait (timeout 0 here)


def test_import_survives_a_busy_inbox(maildb):
    """Many short writes of another connection (clicks in the inbox) during a mail import: no mail
    fails because of the lock."""
    import threading
    import time

    conn, mail_dir, cache, path = maildb
    for i in range(30):
        write_eml(mail_dir / f"m{i:02d}.eml", f"Mail {i}", f"Text {i}")
    stop = threading.Event()
    failures = []

    def inbox():
        other = sqlite3.connect(path, timeout=5)
        try:
            while not stop.is_set():
                try:
                    other.execute("INSERT INTO meta(key, value) VALUES('probe', ?) ON CONFLICT(key) DO UPDATE "
                                  "SET value = excluded.value", (str(time.time()),))
                    other.commit()
                except sqlite3.OperationalError as exc:
                    failures.append(str(exc))
                time.sleep(0.001)
        finally:
            other.close()

    t = threading.Thread(target=inbox)
    t.start()
    try:
        stats = sync(conn, mail_dir, cache)
    finally:
        stop.set()
        t.join()
    assert (stats["new"], stats["errors"]) == (30, 0) and failures == []


def test_retry_locked_repeats_only_lock_errors(maildb, monkeypatch):
    conn, *_ = maildb
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"
    assert store.retry_locked(conn, flaky) == "ok" and len(calls) == 3
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        store.retry_locked(conn, lambda: conn.execute("SELECT * FROM fehlt"))
    calls.clear()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        store.retry_locked(conn, lambda: calls.append(1) or (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")))
    assert len(calls) == store.LOCK_RETRIES


def _decided(conn, mail_dir, cache, name, subject, body, msgid, *, date="Tue, 18 Jun 2024 10:30:00 +0200"):
    write_eml(mail_dir / name, subject, body, msgid=msgid, date=date)
    sync(conn, mail_dir, cache)
    assign_mails(conn, PATTERN, min_share=0.5, min_lead=0.2, vector_key="k")
    mail_id = conn.execute("SELECT id FROM mails WHERE source_key = ?", ("eml:" + name,)).fetchone()[0]
    decide(conn, mail_id, "assign", "BS-2024-0001", user="anna")
    return mail_id


def _state(conn, name):
    return tuple(conn.execute("SELECT project, assign_status, assign_reason FROM mails WHERE source_key = ?",
                              ("eml:" + name,)).fetchone())


def test_message_id_decision_is_not_given_to_another_mail(maildb):
    """A device with a constant Message-ID: a decision for scan 1 is not transferred to scan 2 – not
    while scan 1 is there, and not after it is gone either (different content, subject and date)."""
    conn, mail_dir, cache, _ = maildb
    _decided(conn, mail_dir, cache, "scan1.eml", "Scan", "Begehungsprotokoll Schule", "<0@scanner.local>")
    write_eml(mail_dir / "scan2.eml", "Scan vom Gerät", "Rechnung Nr. 4711 der Druckerei", msgid="<0@scanner.local>",
              date="Wed, 19 Jun 2024 09:00:00 +0200")
    sync(conn, mail_dir, cache)
    assign_mails(conn, PATTERN, min_share=0.5, min_lead=0.2, vector_key="k")
    assert _state(conn, "scan2.eml")[:2] == (None, "unassigned")
    (mail_dir / "scan1.eml").unlink()
    write_eml(mail_dir / "scan3.eml", "Scan vom Gerät", "Lieferschein Papier", msgid="<0@scanner.local>",
              date="Thu, 20 Jun 2024 09:00:00 +0200")
    assert sync(conn, mail_dir, cache)["moved"] == 0  # same id, other subject and date: no move either
    assign_mails(conn, PATTERN, min_share=0.5, min_lead=0.2, vector_key="k")
    assert _state(conn, "scan3.eml")[:2] == (None, "unassigned")
    assert "anna" not in (_state(conn, "scan2.eml")[2] + _state(conn, "scan3.eml")[2])


def test_copy_of_a_decided_mail_does_not_inherit_the_decision(maildb):
    conn, mail_dir, cache, _ = maildb
    _decided(conn, mail_dir, cache, "a.eml", "Frage zur Halle", "Wie breit wird das Tor?", "<abc@x.example>")
    (mail_dir / "kopie.eml").write_bytes((mail_dir / "a.eml").read_bytes())
    sync(conn, mail_dir, cache)
    assign_mails(conn, PATTERN, min_share=0.5, min_lead=0.2, vector_key="k")
    assert _state(conn, "a.eml") == ("BS-2024-0001", "confirmed", "Von anna zugeordnet.")
    assert _state(conn, "kopie.eml")[:2] == (None, "unassigned")


def test_placeholder_message_ids_are_ignored(maildb):
    conn, mail_dir, cache, _ = maildb
    _decided(conn, mail_dir, cache, "a.eml", "Frage", "Text eins", "<>")
    assert [r[0] for r in conn.execute("SELECT source_key FROM assign_decisions")] == ["eml:a.eml"]
    (mail_dir / "a.eml").unlink()
    write_eml(mail_dir / "b.eml", "Frage", "Text zwei", msgid="<>")
    assert sync(conn, mail_dir, cache)["moved"] == 0


def test_text_attachment_without_charset_keeps_umlauts():
    """A text/plain attachment without charset parameter (base64 UTF-8) must not lose its umlauts."""
    import base64
    raw = ("From: a@x.example\nTo: b@x.example\nSubject: LV\nMIME-Version: 1.0\n"
           "Content-Type: multipart/mixed; boundary=XX\n\n--XX\nContent-Type: text/plain\n\nHallo\n"
           "--XX\nContent-Type: text/plain\nContent-Disposition: attachment; filename=LV.txt\n"
           "Content-Transfer-Encoding: base64\n\n" + base64.b64encode("Stück 24, Leitungsbüro".encode()).decode()
           + "\n--XX--\n").encode()
    rec = parse_eml(raw, "eml:lv.eml")
    from projektakte.extract import decode_text
    assert decode_text(rec.attachments[0].data) == "Stück 24, Leitungsbüro"
