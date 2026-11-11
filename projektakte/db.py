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
    # 9 – sites (Bauvorhaben) above the orders (registry.bauvorhaben, PROJECT_PARENT_DIGITS): a site is a
    #     row of its own (kind 'bauvorhaben', its own files: those in its folders outside every order),
    #     an order names its site in ``parent``. No foreign key: sites and orders are written in one pass
    #     in any order, and a site that goes missing must never cascade into its orders. Existing rows
    #     are orders without a site, as before.
    """
    ALTER TABLE projects ADD COLUMN parent TEXT;
    ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'projekt';
    CREATE INDEX projects_parent ON projects(parent);
    """,
    # 10 – two stages for scans (vision.py): a file is stored at once with its text layer or Tesseract's
    #      text, its scan pages wait for the model (``vision_pending`` of them). ``page_texts`` holds the text
    #      of every page of such a document until the model has read them all, so the document can be put
    #      together again; ``vision_note``/``vision_incomplete`` are message and incompleteness of the first
    #      stage, which stand once the model is done; a page read only partly notes why (``partial``, ``note``),
    #      whether that may go away (``retry``) and the worker's limit that cut it short (``size_limit``).
    #      Existing documents have nothing pending.
    """
    ALTER TABLE documents ADD COLUMN vision_pending INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE documents ADD COLUMN vision_note TEXT;
    ALTER TABLE documents ADD COLUMN vision_incomplete INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX documents_vision_pending ON documents(vision_pending) WHERE vision_pending > 0;
    CREATE TABLE page_texts (
        doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        page        INTEGER NOT NULL,
        text        TEXT NOT NULL DEFAULT '',
        state       TEXT NOT NULL DEFAULT 'text',
        attempts    INTEGER NOT NULL DEFAULT 0,
        retry_after TEXT,
        note        TEXT,
        partial     INTEGER NOT NULL DEFAULT 0,
        retry       INTEGER NOT NULL DEFAULT 0,
        size_limit  TEXT,
        PRIMARY KEY (doc_id, page)
    );
    """,
    # 11 – order of the documents pass (reihenfolge.py): per project when its last complete documents pass
    #      ended (``done_at``), with which extraction profile (``done_profile``) and how many files or folders
    #      it could not read (``done_skipped``); ``pinned`` is the position of a project a person moved to the
    #      front ("Vorziehen", cleared once it is done). ``index_folders`` is the order of the top-level folders
    #      set on the status page (no rows: INDEX_FOLDER_ORDER and the default). Existing projects are not done
    #      yet: the next pass walks them in the new order (unchanged files cost a stat) and marks them.
    """
    ALTER TABLE projects ADD COLUMN done_at TEXT;
    ALTER TABLE projects ADD COLUMN done_profile TEXT;
    ALTER TABLE projects ADD COLUMN done_skipped INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE projects ADD COLUMN pinned INTEGER;
    CREATE TABLE index_folders (
        name      TEXT PRIMARY KEY,
        position  INTEGER NOT NULL,
        enabled   INTEGER NOT NULL DEFAULT 1
    );
    """,
    # 12 – pypdf may decode up to 450 MB per image now instead of 75 MB (extract.PDF_IMAGE_BYTES: colour
    #      scans of large plans): documents whose page images were too big for the old limit are read once
    #      more (an unknown profile makes them due, see documents.reprocess_due). Run once: a file beyond the
    #      new limit keeps its note and is not read again and again.
    """
    UPDATE documents SET incomplete = 1, extract_profile = '', retry_after = NULL
    WHERE message LIKE '%zu groß zum Lesen (Speicher- oder Datengrenze überschritten)%'
       OR vision_note LIKE '%zu groß zum Lesen (Speicher- oder Datengrenze überschritten)%'
       OR id IN (SELECT doc_id FROM page_texts WHERE note LIKE '%zu groß zum Lesen%');
    """,
]

# projects.kind
PROJEKT = "projekt"  # an order (Auftrag) or any project of a share without sites
BAUVORHABEN = "bauvorhaben"  # a site holding orders (PROJECT_PARENT_DIGITS)


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


def project_scope(conn: sqlite3.Connection, number: str) -> list[str]:
    """The project numbers a question or search about ``number`` covers: a site (Bauvorhaben) covers
    itself and all its orders (Unterprojekte, found through ``projects.parent`` only – never by a number
    prefix, so 101599 copied under another site, or 101500 for a site 1015, never leaks in); an order or
    any other project covers exactly itself – never its site or its sibling orders. Matches like
    ``registry.get_project`` (case-insensitive); an unknown number covers itself."""
    row = conn.execute("SELECT number, kind FROM projects WHERE number = ? COLLATE NOCASE", (number,)).fetchone()
    if row is None:
        return [number]
    canonical, kind = row[0], row[1]
    if kind != BAUVORHABEN:
        return [canonical]
    children = [r[0] for r in conn.execute(
        "SELECT number FROM projects WHERE parent = ? AND kind = ? ORDER BY number", (canonical, PROJEKT))]
    return [canonical, *children]


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
