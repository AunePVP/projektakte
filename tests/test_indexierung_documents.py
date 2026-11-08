"""Indexing of project files: incomplete results are processed again (vision switched on, model
back after an outage), short write transactions, links leaving PROJECTS_DIR, renamed/vanished
project folders."""
import os
import sqlite3
from datetime import timedelta

import pytest

from projektakte import db, documents
from projektakte.demo.writers import png_bytes
from projektakte.documents import index_all_documents, index_project_documents, reprocess_attachments
from projektakte.mails.model import Attachment, MailRecord
from projektakte.mails.store import store_mail
from projektakte.registry import scan_projects, sync_projects

PATTERN = r"BS-\d{4}-\d{4}"


class _Result:
    def __init__(self, text):
        self.text = text


class VisionLLM:
    vision = True

    def __init__(self, model="vision-a", text="Grundriss EG mit Brandwand und Fluchtweg"):
        self.model = model
        self.text = text
        self.calls = 0

    def describe_image(self, data, mime, prompt):
        self.calls += 1
        return _Result(self.text)


class DownLLM(VisionLLM):
    def describe_image(self, data, mime, prompt):
        self.calls += 1
        raise RuntimeError("LLM timeout")


@pytest.fixture()
def store(tmp_path):
    root = tmp_path / "projekte"
    proj = root / "BS-2024-0001 Testhalle"
    (proj / "05 Pläne").mkdir(parents=True)
    (proj / "05 Pläne" / "Plan_EG.png").write_bytes(png_bytes())
    (proj / "Notiz.txt").write_text("Rettungsweg über Treppenraum Nord.", encoding="utf-8")
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_projects(conn, scan_projects(root, PATTERN))
    conn.commit()
    yield conn, root, proj
    conn.close()


def _png_row(conn):
    return conn.execute("SELECT id, status, message, method, incomplete, attempts, retry_after, extract_profile "
                        "FROM documents WHERE ext = '.png'").fetchone()


def _index(conn, root, llm=None):
    return index_all_documents(conn, llm=llm, projects_dir=root)


def test_skipped_scan_is_read_once_vision_is_switched_on(store):
    conn, root, _ = store
    _index(conn, root)
    assert (_png_row(conn)["status"], _png_row(conn)["incomplete"]) == ("skipped", 1)
    assert _index(conn, root)["unchanged"] == 2  # same profile: nothing to do

    llm = VisionLLM()
    stats = _index(conn, root, llm)
    assert stats["reprocessed"] == 1 and stats["unchanged"] == 1
    row = _png_row(conn)
    assert (row["status"], row["method"], row["incomplete"]) == ("ok", "vision", 0)
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'brandwand'").fetchone()[0] == 1
    doc_id = row["id"]
    # complete now: neither the same nor another model reads it again
    assert _index(conn, root, llm)["unchanged"] == 2
    assert _index(conn, root, VisionLLM(model="vision-b"))["unchanged"] == 2
    assert llm.calls == 1 and _png_row(conn)["id"] == doc_id


def test_empty_vision_answer_is_retried_with_another_model(store):
    conn, root, _ = store
    _index(conn, root, VisionLLM(text="  "))
    assert (_png_row(conn)["status"], _png_row(conn)["incomplete"]) == ("empty", 1)
    assert _index(conn, root, VisionLLM(text="  "))["unchanged"] == 2
    assert _index(conn, root, VisionLLM(model="vision-b"))["reprocessed"] == 1
    assert _png_row(conn)["status"] == "ok"


def test_failed_extraction_is_retried_after_backoff(store, monkeypatch):
    conn, root, _ = store
    t0 = documents._utcnow()
    monkeypatch.setattr(documents, "_utcnow", lambda: t0)
    down = DownLLM()
    _index(conn, root, down)
    row = _png_row(conn)
    assert (row["status"], row["incomplete"], row["attempts"]) == ("error", 1, 1)
    assert row["retry_after"] == documents._sqlite_time(t0 + timedelta(minutes=15))

    # before the retry time nothing happens, even though the model is back
    up = VisionLLM()
    monkeypatch.setattr(documents, "_utcnow", lambda: t0 + timedelta(minutes=10))
    assert _index(conn, root, up)["unchanged"] == 2 and up.calls == 0

    # still down at the first retry: the back-off doubles
    monkeypatch.setattr(documents, "_utcnow", lambda: t0 + timedelta(minutes=16))
    assert _index(conn, root, down)["reprocessed"] == 1
    row = _png_row(conn)
    assert row["attempts"] == 2
    assert row["retry_after"] == documents._sqlite_time(t0 + timedelta(minutes=16 + 30))

    monkeypatch.setattr(documents, "_utcnow", lambda: t0 + timedelta(minutes=50))
    assert _index(conn, root, up)["reprocessed"] == 1
    row = _png_row(conn)
    assert (row["status"], row["incomplete"], row["attempts"], row["retry_after"]) == ("ok", 0, 0, None)


def test_retry_delay_grows_and_is_capped():
    assert documents.retry_delay(1) == timedelta(minutes=15)
    assert documents.retry_delay(3) == timedelta(hours=1)
    assert documents.retry_delay(50) == timedelta(days=7)


def test_changed_file_resets_attempts(store, monkeypatch):
    conn, root, proj = store
    _index(conn, root, DownLLM())
    _index(conn, root, DownLLM())  # not due yet
    assert _png_row(conn)["attempts"] == 1
    (proj / "05 Pläne" / "Plan_EG.png").write_bytes(png_bytes(seed=7))
    assert _index(conn, root, DownLLM())["changed"] == 1
    assert _png_row(conn)["attempts"] == 1  # new content, new count


def test_rows_from_before_the_migration_are_processed_once(tmp_path):
    dbp = tmp_path / "old.sqlite3"
    conn = db.connect(dbp)
    for i, script in enumerate(db.MIGRATIONS[:5], start=1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    rows = [("a.png", ".png", "skipped", "Bild – Bildauswertung ausgeschaltet (LLM_VISION=0)"),
            ("b.pdf", ".pdf", "error", "PdfReadError: kaputt"),
            ("c.pdf", ".pdf", "ok", "2 Seite(n) ohne Textebene"),
            ("d.dwg", ".dwg", "skipped", "Format .dwg nicht unterstützt"),
            ("e.txt", ".txt", "ok", None)]
    for name, ext, status, message in rows:
        conn.execute("INSERT INTO documents(path, project, rel_path, filename, ext, status, message) "
                     "VALUES(?, 'BS-2024-0001', ?, ?, ?, ?, ?)", (f"/p/{name}", name, name, ext, status, message))
    conn.commit()
    conn.close()
    db.init_db(dbp)
    conn = sqlite3.connect(dbp)
    got = dict(conn.execute("SELECT filename, incomplete FROM documents").fetchall())
    assert got == {"a.png": 1, "b.pdf": 1, "c.pdf": 1, "d.dwg": 0, "e.txt": 0}
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE extract_profile IS NOT NULL").fetchone()[0] == 0


def _mail(attachments):
    rec = MailRecord(source="eml", source_key="eml:test.eml", subject="Plan EG BS-2024-0001",
                     sender="a@example.org", sender_name="A", recipients="b@example.org",
                     date="2024-05-02T10:00:00Z", body="Anbei der Plan.", attachments=attachments)
    rec.compute_hash()
    return rec


def test_mail_attachment_is_read_once_vision_is_switched_on(store, tmp_path):
    conn, _, _ = store
    rec = _mail([Attachment("Plan.png", png_bytes(seed=3), "image/png")])
    mail_id, status = store_mail(conn, rec, cache_dir=tmp_path / "cache")
    conn.commit()
    assert status == "new"
    att = conn.execute("SELECT status, incomplete FROM documents WHERE mail_id = ?", (mail_id,)).fetchone()
    assert tuple(att) == ("skipped", 1)
    # the mail itself is unchanged – the mail import would not look at it again
    assert store_mail(conn, rec, cache_dir=tmp_path / "cache")[1] == "unchanged"
    assert reprocess_attachments(conn, llm=None)["reprocessed"] == 0
    assert reprocess_attachments(conn, llm=VisionLLM())["reprocessed"] == 1
    att = conn.execute("SELECT id, status, method, cache_path FROM documents WHERE mail_id = ?", (mail_id,)).fetchone()
    assert (att["status"], att["method"]) == ("ok", "vision") and att["cache_path"]
    chunk = conn.execute("SELECT mail_id, project FROM chunks WHERE doc_id = ?", (att["id"],)).fetchone()
    assert chunk["mail_id"] == mail_id
    assert reprocess_attachments(conn, llm=VisionLLM())["reprocessed"] == 0


def test_no_write_transaction_is_held_while_extracting(store, monkeypatch, tmp_path):
    """BUG-2: another connection (a decision in the inbox) can write while files are extracted."""
    conn, root, proj = store
    _index(conn, root)
    for i in range(4):
        (proj / f"Neu_{i}.txt").write_text(f"Neue Notiz {i} zur Brandwand.", encoding="utf-8")
    other = sqlite3.connect(tmp_path / "x.sqlite3", timeout=0)  # fail at once if locked
    results = []
    real_extract = documents.extract

    def extract_and_write(*args, **kwargs):
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("INSERT INTO meta(key, value) VALUES('probe', '1') "
                          "ON CONFLICT(key) DO UPDATE SET value = value + 1")
            other.commit()
            results.append("ok")
        except sqlite3.OperationalError as exc:
            other.rollback()
            results.append(str(exc))
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(documents, "extract", extract_and_write)
    assert _index(conn, root)["new"] == 4
    other.close()
    assert results == ["ok"] * 4


def test_links_leaving_projects_dir_are_not_indexed(store, tmp_path):
    """SEC-02: neither linked files nor linked folders pointing outside PROJECTS_DIR are read."""
    conn, root, proj = store
    outside = tmp_path / "outside"
    (outside / "geheim").mkdir(parents=True)
    (outside / "geheim.txt").write_text("STRENG GEHEIM Gehaltsliste Zebrafink", encoding="utf-8")
    (outside / "geheim" / "liste.txt").write_text("Zebrafink im Ordner", encoding="utf-8")
    (proj / "link-datei.txt").symlink_to(outside / "geheim.txt")
    (proj / "link-ordner").symlink_to(outside / "geheim", target_is_directory=True)
    (proj / "intern.txt").symlink_to(proj / "Notiz.txt")  # stays inside: allowed
    stats = _index(conn, root)
    names = {r[0] for r in conn.execute("SELECT rel_path FROM documents")}
    assert "link-datei.txt" not in names and not any(n.startswith("link-ordner") for n in names)
    assert "intern.txt" in names and stats["new"] == 3
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'zebrafink'").fetchone()[0] == 0


def test_file_replaced_by_outside_link_is_dropped(store, tmp_path):
    conn, root, proj = store
    _index(conn, root)
    secret = tmp_path / "secret.txt"
    secret.write_text("Passwort Zebrafink", encoding="utf-8")
    (proj / "Notiz.txt").unlink()
    (proj / "Notiz.txt").symlink_to(secret)
    assert _index(conn, root)["removed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'zebrafink'").fetchone()[0] == 0


def test_read_inside_refuses_targets_outside(tmp_path):
    inside = tmp_path / "p"
    inside.mkdir()
    (tmp_path / "s.txt").write_text("x")
    (inside / "l.txt").symlink_to(tmp_path / "s.txt")
    with pytest.raises(PermissionError):
        documents._read_inside(inside / "l.txt", inside.resolve())


def test_project_folder_linked_outside_is_ignored(tmp_path):
    root = tmp_path / "projekte"
    root.mkdir()
    elsewhere = tmp_path / "anderswo" / "BS-2024-0009 Fremd"
    elsewhere.mkdir(parents=True)
    (root / "BS-2024-0009 Fremd").symlink_to(elsewhere, target_is_directory=True)
    (root / "2024").symlink_to(tmp_path / "anderswo", target_is_directory=True)
    (root / "BS-2024-0001 Echt").mkdir()
    assert [p.number for p in scan_projects(root, PATTERN)] == ["BS-2024-0001"]
    # defence in depth: a project row pointing outside is not read
    dbp = tmp_path / "y.sqlite3"
    db.init_db(dbp)
    with db.session(dbp) as conn:
        (elsewhere / "a.txt").write_text("fremder Inhalt", encoding="utf-8")
        p = {"number": "BS-2024-0009", "name": "Fremd", "path": str(root / "BS-2024-0009 Fremd")}
        assert index_project_documents(conn, p, projects_dir=root)["new"] == 0


def test_renamed_project_folder_keeps_documents_and_refreshes_titles(store):
    """BUG-6: same number, new name/place – documents are moved, not extracted again."""
    conn, root, proj = store
    rec = _mail([])
    mail_id, _ = store_mail(conn, rec, cache_dir=root.parent / "cache")
    from projektakte.assign import set_mail_project
    set_mail_project(conn, mail_id, "BS-2024-0001")
    _index(conn, root)
    before = dict(conn.execute("SELECT rel_path, id FROM documents WHERE mail_id IS NULL").fetchall())
    chunks_before = {r[0] for r in conn.execute("SELECT id FROM chunks")}

    target = root / "2025" / "BS-2024-0001 Lagerhalle Nordring"
    target.parent.mkdir()
    proj.rename(target)
    stats = sync_projects(conn, scan_projects(root, PATTERN))
    assert stats["updated"] == 1 and stats["deactivated"] == 0
    dstats = _index(conn, root)
    assert dstats["moved"] == 2 and dstats["new"] == 0 and dstats["removed"] == 0
    after = dict(conn.execute("SELECT rel_path, id FROM documents WHERE mail_id IS NULL").fetchall())
    assert after == before
    assert {r[0] for r in conn.execute("SELECT id FROM chunks")} == chunks_before  # vectors stay
    paths = [r[0] for r in conn.execute("SELECT path FROM documents WHERE mail_id IS NULL")]
    assert all(p.startswith(str(target)) for p in paths)
    titles = [r[0] for r in conn.execute("SELECT title FROM chunks_fts")]
    assert titles and all("lagerhalle nordring" in t and "testhalle" not in t for t in titles)
    mail_title = conn.execute("SELECT f.title FROM chunks c JOIN chunks_fts f ON f.rowid = c.id "
                              "WHERE c.mail_id = ? AND c.doc_id IS NULL", (mail_id,)).fetchone()[0]
    assert mail_title == documents.mail_fts_title("BS-2024-0001", "Lagerhalle Nordring", rec.subject, "A")


def test_documents_of_vanished_projects_leave_search_at_once(store):
    """BUG-6/ops-05: renumbered or removed folder – its files leave search and answers at once; they
    are deleted after the grace period (test_indexierung_grace.py)."""
    conn, root, proj = store
    rec = _mail([])
    mail_id, _ = store_mail(conn, rec, cache_dir=root.parent / "cache")
    _index(conn, root)
    proj.rename(root / "BS-2024-0002 Testhalle")
    stats = sync_projects(conn, scan_projects(root, PATTERN))
    assert (stats["added"], stats["deactivated"], stats["documents_hidden"], stats["documents_removed"]) == \
        (1, 1, 2, 0)
    assert _index(conn, root)["new"] == 2
    found = {r[0] for r in conn.execute("SELECT DISTINCT d.project FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                                        "JOIN documents d ON d.id = c.doc_id WHERE d.mail_id IS NULL")}
    assert found == {"BS-2024-0002"}
    # the project stays known (inactive), the mail stays
    assert conn.execute("SELECT active FROM projects WHERE number = 'BS-2024-0001'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM mails WHERE id = ?", (mail_id,)).fetchone()[0] == 1


def test_leftover_documents_of_inactive_projects_are_purged(store):
    conn, root, _ = store
    _index(conn, root)
    conn.execute("UPDATE projects SET active = 0")  # state left by an older version
    conn.commit()
    assert _index(conn, root)["removed"] == 2
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0


def test_unlisted_folder_keeps_its_documents(store, monkeypatch):
    """A folder that cannot be listed this time is no proof that its files are gone."""
    conn, root, proj = store
    _index(conn, root)
    real_walk = os.walk
    plans = proj / "05 Pläne"

    def walk(top, onerror=None, followlinks=False):
        for dirpath, dirnames, filenames in real_walk(top, onerror=onerror, followlinks=followlinks):
            if dirpath == str(plans):
                if onerror:
                    onerror(PermissionError(13, "Permission denied", str(plans)))
                continue
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(documents.os, "walk", walk)
    stats = _index(conn, root)
    assert stats["removed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE ext = '.png'").fetchone()[0] == 1


def test_missing_project_folder_keeps_documents(store):
    conn, root, proj = store
    _index(conn, root)
    p = {"number": "BS-2024-0001", "name": "Testhalle", "path": str(root / "gibt-es-nicht")}
    assert index_project_documents(conn, p, projects_dir=root)["removed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2


def test_attachment_missing_from_cache_is_not_retried_every_pass(store, tmp_path):
    conn, _, _ = store
    rec = _mail([Attachment("Plan.png", png_bytes(seed=4), "image/png")])
    mail_id, _ = store_mail(conn, rec, cache_dir=tmp_path / "cache")
    conn.execute("UPDATE documents SET cache_path = ? WHERE mail_id = ?", (str(tmp_path / "weg.png"), mail_id))
    conn.commit()
    assert reprocess_attachments(conn, llm=VisionLLM())["errors"] == 1
    assert reprocess_attachments(conn, llm=VisionLLM()) == {"reprocessed": 0, "errors": 0}
