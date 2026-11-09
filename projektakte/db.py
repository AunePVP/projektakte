"""SQLite storage: one database file under DATA_DIR, additive migrations via PRAGMA user_version.

Everything in the database is derived from the (read-only) sources and can be rebuilt – except
human decisions (mail assignments), which live in their own table.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS: list[str] = [
    # 1 – project register
    """
    CREATE TABLE projects (
        number      TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        path        TEXT NOT NULL,
        active      INTEGER NOT NULL DEFAULT 1,
        first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """,
    # 2 – documents, sections (chunks) and full-text index
    """
    CREATE TABLE documents (
        id          INTEGER PRIMARY KEY,
        path        TEXT NOT NULL UNIQUE,
        project     TEXT,
        mail_id     INTEGER,
        rel_path    TEXT NOT NULL,
        filename    TEXT NOT NULL,
        ext         TEXT NOT NULL,
        size        INTEGER,
        mtime       REAL,
        sha256      TEXT,
        doc_date    TEXT,
        pages       INTEGER,
        status      TEXT NOT NULL DEFAULT 'ok',
        message     TEXT,
        method      TEXT,
        indexed_at  TEXT
    );
    CREATE INDEX documents_project ON documents(project);
    CREATE INDEX documents_mail ON documents(mail_id);
    CREATE TABLE chunks (
        id          INTEGER PRIMARY KEY,
        doc_id      INTEGER REFERENCES documents(id) ON DELETE CASCADE,
        mail_id     INTEGER,
        project     TEXT,
        ord         INTEGER NOT NULL,
        page        INTEGER,
        heading     TEXT,
        text        TEXT NOT NULL,
        embedding   BLOB
    );
    CREATE INDEX chunks_doc ON chunks(doc_id);
    CREATE INDEX chunks_mail ON chunks(mail_id);
    CREATE INDEX chunks_project ON chunks(project);
    CREATE VIRTUAL TABLE chunks_fts USING fts5(
        title, body, tokenize = 'unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER chunks_after_delete AFTER DELETE ON chunks BEGIN
        DELETE FROM chunks_fts WHERE rowid = old.id;
    END;
    """,
    # 3 – mails (assignment columns are filled from step 8 on)
    """
    CREATE TABLE mails (
        id                INTEGER PRIMARY KEY,
        source            TEXT NOT NULL,
        source_key        TEXT NOT NULL UNIQUE,
        message_id        TEXT,
        in_reply_to       TEXT,
        subject           TEXT NOT NULL DEFAULT '',
        sender            TEXT NOT NULL DEFAULT '',
        sender_name       TEXT NOT NULL DEFAULT '',
        recipients        TEXT NOT NULL DEFAULT '',
        date              TEXT,
        body              TEXT NOT NULL DEFAULT '',
        preview           TEXT NOT NULL DEFAULT '',
        sha256            TEXT,
        has_attachments   INTEGER NOT NULL DEFAULT 0,
        project           TEXT,
        assign_status     TEXT NOT NULL DEFAULT 'pending',
        assign_method     TEXT,
        assign_reason     TEXT,
        suggested_project TEXT,
        suggestion_score  REAL,
        deleted           INTEGER NOT NULL DEFAULT 0,
        indexed_at        TEXT
    );
    CREATE INDEX mails_project ON mails(project);
    CREATE INDEX mails_status ON mails(assign_status);
    CREATE INDEX mails_date ON mails(date);
    ALTER TABLE documents ADD COLUMN cache_path TEXT;
    """,
    # 4 – assignment: human decisions survive re-indexing (keyed by the mail's source key)
    """
    CREATE TABLE assign_decisions (
        source_key  TEXT PRIMARY KEY,
        action      TEXT NOT NULL,
        project     TEXT,
        rejected    TEXT NOT NULL DEFAULT '',
        decided_by  TEXT,
        decided_at  TEXT
    );
    ALTER TABLE mails ADD COLUMN suggest_done INTEGER NOT NULL DEFAULT 0;
    """,
    # 5 – assignment made in plMail (JMAP property, JSON), read-only copy
    """
    ALTER TABLE mails ADD COLUMN external_assignment TEXT;
    """,
    # 6 – extraction profile per document: incomplete results (scans without vision, failed model
    #     calls, errors) are processed again when the profile changes or their retry time is due.
    #     Existing incomplete rows get no profile, so the next run processes them once.
    """
    ALTER TABLE documents ADD COLUMN extract_profile TEXT;
    ALTER TABLE documents ADD COLUMN incomplete INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE documents ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE documents ADD COLUMN retry_after TEXT;
    UPDATE documents SET incomplete = 1
     WHERE status = 'error'
        OR (status = 'skipped' AND COALESCE(message, '') NOT LIKE 'Format %')
        OR (status = 'ok' AND COALESCE(message, '') LIKE '%ohne Textebene%')
        OR (status = 'empty' AND ext IN ('.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp'));
    """,
    # 7 – a project folder that went missing: since when and in how many consecutive passes. Its files
    #     are only hidden at first and deleted after a grace period (see registry.MISSING_GRACE).
    #     Projects already inactive keep NULL: their files were removed by the older version.
    """
    ALTER TABLE projects ADD COLUMN missing_since TEXT;
    ALTER TABLE projects ADD COLUMN missing_runs INTEGER NOT NULL DEFAULT 0;
    """,
    # 8 – several folders with the same number are one project (registry.scan_projects): projects.path
    #     is its primary folder, the further folders are listed here in their order. A database of an
    #     older version has no rows – every project is its one folder, as before.
    """
    CREATE TABLE project_folders (
        project   TEXT NOT NULL REFERENCES projects(number) ON DELETE CASCADE,
        path      TEXT NOT NULL,
        position  INTEGER NOT NULL,
        PRIMARY KEY (project, path)
    );
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    finally:
        conn.close()


@contextmanager
def session(path: Path | str) -> Iterator[sqlite3.Connection]:
    """Connection for one unit of work; commits on success, rolls back on error."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
