"""Second mail source: plMail via its JMAP API (RFC 8620/8621), strictly read-only.

See docs/plmail-jmap.md. Paths come from the session object; every JMAP account (one per connected
mailbox) is synchronised. First run: ``Email/query`` + ``Email/get`` page by page (position/limit,
pages overlap), then an ids-only ``Email/query`` reconciliation: mails skipped because the list
shifted while paging (a mail deleted meanwhile) are fetched, mails gone from plMail are removed only
after ``Email/get`` confirms it. Afterwards only ``Email/changes`` from the stored ``state``.
Methods that change anything (``Email/set``, ``EmailSubmission/set``) are never used.

Every mail is downloaded and extracted before it is written and committed on its own. A mail that
fails (cache not writable, extraction crashed, …) is logged, counted and retried later with a
back-off (meta ``jmap_retry:<account>``) – it never stops the run or the other mails.

An attachment plMail does not deliver right now (timeout, server error) does not hold the mail back:
the mail is stored at once – text, search and assignment work – and the attachment appears as a
document with the status "Anhang ausstehend". It is loaded again from a separate retry list with a
back-off (meta ``jmap_att_retry:<account>``, 15 min doubled up to a day); after
``ATTACHMENT_ATTEMPTS`` attempts it stays as a document with an error message. An attachment plMail
no longer has (404/410) or one that is too large is shown the same way, without further attempts.

Newsletter headers: plMail's ``Email/get`` offers no ``header:*`` properties (docs/plmail-jmap.md,
"Vorschläge an plMail"), so List-Unsubscribe/Precedence are not known for plMail mails – the
assignment recognises newsletters and advertising there by sender address and wording only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..ai.http import AIServiceError, HTTPStatusError, Timer, request, request_json
from ..extract import html_to_text
from .model import Attachment, MailRecord, iso_utc
from .store import delete_mail, prepare_mail, retry_locked, write_mail

log = logging.getLogger("projektakte.mails.jmap")

USING = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
MAIL_CAP = "urn:ietf:params:jmap:mail"
PROPERTIES = ["id", "threadId", "subject", "from", "to", "cc", "receivedAt", "preview", "hasAttachment",
              "attachments", "bodyValues", "textBody", "htmlBody"]
# plMail extension (docs/plmail-integration.md): project assignment made by a human in plMail
PROJECT_CAP = "urn:plmail:params:jmap:projektakte"
PROJECT_PROP = "plmail:projektakte"
MAX_ATTACHMENT_BYTES = 80 * 1024 * 1024
IDS_PAGE_SIZE = 1000  # ids-only Email/query of the reconciliation (the server may cap it)
RETRY_BASE = timedelta(minutes=15)  # first retry of a failed mail or attachment, doubled per attempt
RETRY_MAX = timedelta(days=1)
# attempts to load an attachment plMail does not deliver (15 min … 1 day apart, about 4 days in all);
# afterwards it stays as a document with an error message
ATTACHMENT_ATTEMPTS = 10


class JMAPError(Exception):
    def __init__(self, message: str, type_: str | None = None):
        super().__init__(message)
        self.type = type_


class JMAPClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0, page_size: int = 100):
        self.base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}  # never logged
        self.timeout = timeout
        self.page_size = page_size
        self._session: dict | None = None
        self.calls: list[str] = []  # method names used (read-only check in tests)

    @property
    def has_project_extension(self) -> bool:
        """Does this plMail expose the project assignment (capability in the session)?"""
        return PROJECT_CAP in (self.session().get("capabilities") or {})

    def _using(self) -> list[str]:
        return USING + [PROJECT_CAP] if self.has_project_extension else USING

    def _properties(self) -> list[str]:
        return PROPERTIES + [PROJECT_PROP] if self.has_project_extension else PROPERTIES

    # -- session ---------------------------------------------------------------------------------
    def session(self, refresh: bool = False) -> dict:
        if self._session is not None and not refresh:
            return self._session
        last: Exception | None = None
        for path in ("/.well-known/jmap", "/jmap/session"):
            try:
                data = request_json("GET", self.base + path, timeout=self.timeout, headers=self._headers)
            except HTTPStatusError as exc:
                if exc.status in (401, 403):
                    raise JMAPError("plMail lehnt das App-Passwort ab (PLMAIL_TOKEN prüfen)") from None
                last = exc
                continue
            except AIServiceError as exc:
                raise JMAPError(f"plMail nicht erreichbar: {exc}") from None
            if not isinstance(data, dict) or "apiUrl" not in data:
                last = JMAPError("Antwort ist kein JMAP-Session-Objekt")
                continue
            self._session = data
            return data
        raise JMAPError(f"keine JMAP-Session gefunden ({last})")

    def account_ids(self) -> list[str]:
        accounts = self.session().get("accounts") or {}
        ids = []
        for acc_id, acc in accounts.items():
            caps = (acc or {}).get("accountCapabilities")
            if caps is None or MAIL_CAP in caps:
                ids.append(str(acc_id))
        return sorted(ids)

    # -- API calls -------------------------------------------------------------------------------
    def call(self, method_calls: list) -> list:
        for name, *_ in method_calls:
            self.calls.append(name)
        api_url = self.session()["apiUrl"]
        try:
            data = request_json("POST", api_url, payload={"using": self._using(), "methodCalls": method_calls},
                                timeout=self.timeout, headers=self._headers)
        except HTTPStatusError as exc:
            detail = exc.body
            try:
                problem = json.loads(exc.body)
                detail = problem.get("detail") or problem.get("title") or problem.get("type") or exc.body
            except ValueError:
                pass
            raise JMAPError(f"JMAP-Anfrage abgelehnt (HTTP {exc.status}): {detail}"[:300]) from None
        except AIServiceError as exc:
            raise JMAPError(f"plMail nicht erreichbar: {exc}") from None
        responses = data.get("methodResponses") if isinstance(data, dict) else None
        if not isinstance(responses, list):
            raise JMAPError("ungültige JMAP-Antwort")
        for name, args, _ in responses:
            if name == "error":
                raise JMAPError(f"JMAP-Fehler: {args.get('type')} {args.get('description', '')}".strip(),
                                args.get("type"))
        return responses

    def download(self, account_id: str, blob_id: str, name: str, mime: str) -> bytes:
        template = self.session().get("downloadUrl") or ""
        url = (template.replace("{accountId}", urllib.parse.quote(account_id, safe=""))
               .replace("{blobId}", urllib.parse.quote(blob_id, safe=""))
               .replace("{name}", urllib.parse.quote(name or "anhang", safe=""))
               .replace("{type}", urllib.parse.quote(mime or "application/octet-stream", safe="")))
        if url.startswith("/"):
            url = self.base + url
        body, _ = request("GET", url, timeout=self.timeout,
                          headers={**self._headers, "Accept": mime or "*/*"})
        return body

    # -- convenience -------------------------------------------------------------------------------
    def query_page(self, account_id: str, position: int) -> tuple[list[dict], str | None, list[str]]:
        responses = self.call([
            ["Email/query", {"accountId": account_id,
                             "sort": [{"property": "receivedAt", "isAscending": False}],
                             "position": position, "limit": self.page_size, "calculateTotal": False}, "q0"],
            ["Email/get", {"accountId": account_id,
                           "#ids": {"resultOf": "q0", "name": "Email/query", "path": "/ids"},
                           "properties": self._properties(), "fetchTextBodyValues": True,
                           "fetchHTMLBodyValues": True}, "g0"],
        ])
        query = next(a for n, a, c in responses if c == "q0")
        self._adopt_limit(query)
        ids = query.get("ids") or []
        got = next(a for n, a, c in responses if c == "g0")
        return _in_order(got.get("list") or [], ids), got.get("state"), ids

    def _adopt_limit(self, query: dict) -> None:
        """A server may enforce a smaller limit than requested (it then returns ``limit``); paging must
        use it, or a short page would be taken for the end of the mailbox."""
        limit = query.get("limit")
        if isinstance(limit, int) and 0 < limit < self.page_size:
            log.info("plMail begrenzt Email/query auf %d Mails je Seite", limit)
            self.page_size = limit

    def query_ids(self, account_id: str, position: int, limit: int) -> tuple[list[str], str | None, int]:
        """Only ids (newest first): (ids, queryState, limit the server applied)."""
        responses = self.call([
            ["Email/query", {"accountId": account_id,
                             "sort": [{"property": "receivedAt", "isAscending": False}],
                             "position": position, "limit": limit, "calculateTotal": False}, "q0"]])
        query = responses[0][1]
        applied = query.get("limit")
        applied = applied if isinstance(applied, int) and 0 < applied < limit else limit
        return [str(i) for i in query.get("ids") or []], query.get("queryState"), applied

    def existing_ids(self, account_id: str, ids: list[str]) -> set[str]:
        """Which of these mails still exist in plMail (Email/get answers notFound for the others)."""
        found: set[str] = set()
        for i in range(0, len(ids), self.page_size):
            responses = self.call([["Email/get", {"accountId": account_id, "ids": ids[i : i + self.page_size],
                                                  "properties": ["id"]}, "g0"]])
            found |= {str(e.get("id")) for e in responses[0][1].get("list") or []}
        return found

    def get(self, account_id: str, ids: list[str]) -> tuple[list[dict], str | None]:
        responses = self.call([["Email/get", {"accountId": account_id, "ids": ids, "properties": self._properties(),
                                              "fetchTextBodyValues": True, "fetchHTMLBodyValues": True}, "g0"]])
        got = responses[0][1]
        return _in_order(got.get("list") or [], ids), got.get("state")

    def changes(self, account_id: str, since: str) -> dict:
        return self.call([["Email/changes", {"accountId": account_id, "sinceState": since,
                                             "maxChanges": self.page_size}, "c0"]])[0][1]

    def probe(self) -> dict:
        info: dict = {"url": self.base}
        try:
            with Timer() as t:
                self.session(refresh=True)
                accounts = self.account_ids()
            info.update(ok=True, format="jmap", accounts=len(accounts), latency_ms=t.ms,
                        project_extension=self.has_project_extension)
        except JMAPError as exc:
            info.update(ok=False, format=None, error=str(exc))
        return info


def _in_order(items: list[dict], ids: list[str]) -> list[dict]:
    """plMail returns Email/get in database order – restore the order of the requested ids."""
    pos = {i: n for n, i in enumerate(ids)}
    return sorted(items, key=lambda e: pos.get(e.get("id"), len(pos)))


def _addr(v) -> list[tuple[str, str]]:
    return [((a or {}).get("name") or "", ((a or {}).get("email") or "").lower()) for a in (v or [])]


def _body(email: dict) -> str:
    values = email.get("bodyValues") or {}
    text = "\n".join((values.get(p.get("partId")) or {}).get("value", "") for p in email.get("textBody") or [])
    if text.strip():
        return text.strip()
    html = "\n".join((values.get(p.get("partId")) or {}).get("value", "") for p in email.get("htmlBody") or [])
    return html_to_text(html) if html.strip() else (email.get("preview") or "")


def fingerprint(email: dict) -> str:
    """Cheap fingerprint before downloading attachments (flag changes do not count, a project
    assignment made in plMail does)."""
    parts = [email.get("subject") or "", json.dumps(email.get("from") or [], sort_keys=True),
             email.get("receivedAt") or "", _body(email),
             ",".join(sorted((a or {}).get("blobId", "") for a in email.get("attachments") or [])),
             json.dumps(email.get(PROJECT_PROP), sort_keys=True)]
    return hashlib.sha256("\0".join(parts).encode("utf-8", "replace")).hexdigest()


def _not_loaded(att: dict, name: str, reason: str, state: str) -> Attachment:
    """An attachment without content: shown as a document with ``reason``; "pending" ones are
    loaded again later, "gone" ones not."""
    return Attachment(name, b"", att.get("type") or "application/octet-stream", unavailable=reason, state=state,
                      ref=str(att.get("blobId") or att.get("partId") or ""), size=att.get("size"))


def to_record(client: JMAPClient, account_id: str, email: dict, *, give_up: bool = False) -> MailRecord:
    """Mail record incl. downloaded attachments. An attachment that cannot be downloaded right now
    becomes a placeholder ("Anhang ausstehend", loaded again later); with ``give_up`` (last attempt)
    or when plMail no longer has it (404/410) the placeholder carries an error message instead.
    The mail itself is never held back by an attachment."""
    senders = _addr(email.get("from"))
    sender_name, sender = senders[0] if senders else ("", "")
    recipients = ", ".join(a for _, a in _addr(email.get("to")) + _addr(email.get("cc")) if a)
    date = None
    if email.get("receivedAt"):
        try:
            date = iso_utc(datetime.fromisoformat(str(email["receivedAt"]).replace("Z", "+00:00")))
        except ValueError:
            date = None
    attachments = []
    for att in email.get("attachments") or []:
        if not att.get("blobId"):
            continue
        name = att.get("name") or f"anhang-{att.get('partId', '')}"
        if (att.get("size") or 0) > MAX_ATTACHMENT_BYTES:
            attachments.append(_not_loaded(att, name, f"Anhang nicht übernommen: größer als "
                                                      f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.", "gone"))
            continue
        try:
            data = client.download(account_id, att["blobId"], name, att.get("type") or "")
        except (HTTPStatusError, AIServiceError) as exc:
            status = exc.status if isinstance(exc, HTTPStatusError) else None
            detail = f"HTTP {status}" if status else str(exc)[:120]
            if status in (404, 410):
                reason, state = f"Anhang ist in plMail nicht mehr vorhanden ({detail}).", "gone"
            elif give_up:
                reason, state = (f"Anhang konnte von plMail nicht geladen werden ({detail}), auch nach "
                                 f"{ATTACHMENT_ATTEMPTS} Versuchen – bitte in plMail öffnen."), "gone"
            else:
                reason, state = (f"Anhang ausstehend: plMail hat ihn noch nicht geliefert ({detail}). "
                                 "Er wird automatisch nachgeladen."), "pending"
            log.warning("Anhang %s nicht ladbar (%s) – Mail ohne ihn übernommen%s", name, detail,
                        ", wird nachgeladen" if state == "pending" else "")
            attachments.append(_not_loaded(att, name, reason, state))
            continue
        attachments.append(Attachment(name, data, att.get("type") or "application/octet-stream"))
    rec = MailRecord(source="jmap", source_key=f"jmap:{account_id}:{email['id']}",
                     subject=(email.get("subject") or "").strip(), sender=sender, sender_name=sender_name,
                     recipients=recipients, date=date, body=_body(email), attachments=attachments,
                     external_assignment=parse_assignment(email.get(PROJECT_PROP)))
    rec.compute_hash()
    return rec


def parse_assignment(value) -> dict | None:
    """Validate the plMail property; anything unexpected counts as 'not assigned yet'."""
    if not isinstance(value, dict) or value.get("status") not in ("assigned", "none"):
        return None
    out = {"status": value["status"],
           "project": str(value.get("project") or "").strip().upper() or None,
           "projectName": str(value.get("projectName") or "")[:200],
           "assignedBy": str(value.get("assignedBy") or "")[:100],
           "assignedAt": str(value.get("assignedAt") or "")[:40]}
    if out["status"] == "assigned" and not out["project"]:
        return None
    return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Retries:
    """Mails of one account to fetch again later, with a back-off, because ``Email/changes`` would
    never report them again: mails that failed (meta ``jmap_retry:<account>``) and mails whose
    attachments are still missing (``jmap_att_retry:<account>``)."""

    def __init__(self, conn: sqlite3.Connection, account_id: str, prefix: str = "jmap_retry"):
        self.conn, self.key = conn, f"{prefix}:{account_id}"
        self.reload()

    def reload(self) -> None:
        """Read the list again (after a rollback the in-memory state may be ahead of the database)."""
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (self.key,)).fetchone()
        try:
            data = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            data = {}
        self.items: dict[str, dict] = data if isinstance(data, dict) else {}

    def attempts(self, email_id: str) -> int:
        return int((self.items.get(email_id) or {}).get("attempts") or 0)

    def due(self, now: datetime | None = None) -> list[str]:
        now_s = (now or _utcnow()).isoformat(timespec="seconds")
        return sorted(i for i, r in self.items.items() if str(r.get("next") or "") <= now_s)

    def failed(self, email_id: str, error: str, attempts: int | None = None) -> None:
        """Remember a failed attempt (``attempts`` = its number; given explicitly the call can be
        repeated after a rollback without counting twice)."""
        n = attempts or self.attempts(email_id) + 1
        delay = min(RETRY_BASE * (2 ** min(n - 1, 16)), RETRY_MAX)
        self.items[email_id] = {"attempts": n, "next": (_utcnow() + delay).isoformat(timespec="seconds"),
                                "error": error[:200]}
        self._save()

    def done(self, email_id: str) -> None:
        if self.items.pop(email_id, None) is not None:
            self._save()

    def _save(self) -> None:
        if self.items:
            self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                              "value = excluded.value", (self.key, json.dumps(self.items, sort_keys=True)))
        else:
            self.conn.execute("DELETE FROM meta WHERE key = ?", (self.key,))


def _reloader(*lists: _Retries):
    """After a rollback the in-memory retry lists may be ahead of the database: read them again."""
    return lambda: [r.reload() for r in lists]


def _commit_after(conn: sqlite3.Connection, change, *lists: _Retries) -> None:
    """Run a small change of retry ``lists`` and commit, repeated while the database is locked."""
    def write() -> None:
        change()
        conn.commit()
    retry_locked(conn, write, on_retry=_reloader(*lists))


def _store(conn: sqlite3.Connection, client: JMAPClient, account_id: str, email: dict, stats: dict,
           cache_dir: Path, llm, retries: _Retries, att_retries: _Retries, *, force: bool = False) -> bool:
    """Download, extract and store one mail, then commit. Errors are logged, counted and remembered
    for a later retry – they never stop the run. Attachments plMail does not deliver go on the
    attachment retry list. ``force`` stores the mail even if its fingerprint is unchanged (to load
    missing attachments). Returns True when the mail is stored."""
    eid = str(email.get("id") or "")
    if not eid:
        stats["errors"] += 1
        log.warning("plMail-Konto %s: Mail ohne id übersprungen", account_id)
        return False
    key = f"jmap:{account_id}:{eid}"
    fp = fingerprint(email)
    if not force:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"jmap_fp:{key}",)).fetchone()
        exists = conn.execute("SELECT 1 FROM mails WHERE source_key = ? AND deleted = 0", (key,)).fetchone()
        if row and row[0] == fp and exists:
            stats["unchanged"] += 1
            if eid in retries.items:
                _commit_after(conn, lambda: retries.done(eid), retries)
            return True
    try:
        attempt = att_retries.attempts(eid) + 1  # if attachments are missing again
        rec = to_record(client, account_id, email, give_up=attempt > ATTACHMENT_ATTEMPTS)
        prepared = prepare_mail(conn, rec, cache_dir=cache_dir, llm=llm)  # slow part, no write lock
        pending = [a for a in rec.attachments if a.unavailable and a.state == "pending"]

        def write() -> str:
            _, result = write_mail(conn, prepared)
            conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                         "value = excluded.value", (f"jmap_fp:{key}", fp))
            retries.done(eid)
            if pending:
                att_retries.failed(eid, pending[0].unavailable or "", attempts=attempt)
            else:
                att_retries.done(eid)
            conn.commit()
            return result
        status = retry_locked(conn, write, on_retry=_reloader(retries, att_retries))
    except JMAPError:
        conn.rollback()
        retries.reload()
        att_retries.reload()
        raise  # plMail itself is unavailable – handled per account
    except Exception as exc:  # noqa: BLE001 – one bad mail must not stop the run
        conn.rollback()
        retries.reload()
        att_retries.reload()
        log.warning("plMail-Mail %s nicht übernommen (wird später erneut versucht): %s", key, exc)
        stats["errors"] += 1
        n, error = retries.attempts(eid) + 1, str(exc)
        _commit_after(conn, lambda: retries.failed(eid, error, attempts=n), retries)
        return False
    stats[status] += 1
    return True


def _set_state(conn: sqlite3.Connection, account_id: str, state: str | None) -> None:
    if state:
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value", (f"jmap_state:{account_id}", state))


def _stored_ids(conn: sqlite3.Connection, account_id: str) -> dict[str, int]:
    """plMail email id → mail id of everything stored for this account."""
    prefix = f"jmap:{account_id}:"
    return {r[1][len(prefix):]: r[0] for r in conn.execute(
        "SELECT id, source_key FROM mails WHERE substr(source_key, 1, ?) = ?", (len(prefix), prefix))}


def _delete_ids(conn: sqlite3.Connection, account_id: str, email_ids) -> int:
    """Remove these mails (uncommitted); returns how many were stored."""
    stored = _stored_ids(conn, account_id)
    removed = 0
    for eid in email_ids:
        if eid in stored:
            delete_mail(conn, stored[eid])
            conn.execute("DELETE FROM meta WHERE key = ?", (f"jmap_fp:jmap:{account_id}:{eid}",))
            removed += 1
    return removed


def _refetch(conn, client, account_id, stats, cache_dir, llm, retries: _Retries, att_retries: _Retries,
             todo: _Retries, *, force: bool) -> None:
    """Fetch the mails of ``todo`` (one of the two retry lists) that are due again and store them;
    mails plMail no longer has leave the list (their deletion comes via Email/changes)."""
    due = todo.due()
    for i in range(0, len(due), client.page_size):
        batch = due[i : i + client.page_size]
        emails, _ = client.get(account_id, batch)
        found = {str(e.get("id")) for e in emails}
        gone = [eid for eid in batch if eid not in found]
        if gone:
            _commit_after(conn, lambda: [todo.done(eid) for eid in gone], todo)
        for e in emails:
            _store(conn, client, account_id, e, stats, cache_dir, llm, retries, att_retries, force=force)


def _sync_changes(conn, client, account_id, state, stats, cache_dir, llm, retries, att_retries) -> None:
    while True:
        ch = client.changes(account_id, state)
        ids = list(dict.fromkeys((ch.get("created") or []) + (ch.get("updated") or [])))
        for i in range(0, len(ids), client.page_size):
            emails, _ = client.get(account_id, ids[i : i + client.page_size])
            for e in emails:
                _store(conn, client, account_id, e, stats, cache_dir, llm, retries, att_retries)
        destroyed = [str(x) for x in ch.get("destroyed") or []]
        state = ch.get("newState") or state

        def apply(destroyed=destroyed, state=state) -> int:
            removed = _delete_ids(conn, account_id, destroyed)
            for eid in destroyed:
                retries.done(eid)
                att_retries.done(eid)
            _set_state(conn, account_id, state)
            conn.commit()
            return removed
        stats["removed"] += retry_locked(conn, apply, on_retry=_reloader(retries, att_retries))
        if not ch.get("hasMoreChanges"):
            return


def _overlap(page_size: int) -> int:
    """Mails each page repeats from the previous one: a deletion on an earlier page shifts the list,
    the overlap still catches the mail that moved up."""
    return max(1, page_size // 10) if page_size > 1 else 0


def _all_ids(client: JMAPClient, account_id: str, attempts: int = 3) -> list[str]:
    """All email ids (ids only, cheap). Repeated when the mailbox changed while listing (queryState
    changed or the overlapping id did not come back where expected)."""
    ids: list[str] = []
    for _ in range(attempts):
        ids, seen, position, first_state, stable = [], set(), 0, None, True
        limit = IDS_PAGE_SIZE
        while True:
            page, qstate, limit = client.query_ids(account_id, position, limit)
            if position == 0:
                first_state = qstate
            elif qstate != first_state or (ids and page[:1] != ids[-1:]):
                stable = False
            for eid in page:
                if eid not in seen:
                    seen.add(eid)
                    ids.append(eid)
            if len(page) < limit or len(page) <= 1:
                break
            position += len(page) - 1  # one id overlap: shows whether the list shifted
        if stable:
            return ids
    return ids  # still changing – good enough: deletions are confirmed with Email/get anyway


def _sync_full(conn, client, account_id, stats, cache_dir, llm, retries, att_retries) -> None:
    stats["mode"] = "full"
    stats["recovered"] = 0
    position, state, seen = 0, None, set()
    while True:
        emails, page_state, ids = client.query_page(account_id, position)
        state = state or page_state  # state of the first page: later changes come via Email/changes
        fresh = [e for e in emails if str(e.get("id")) not in seen]
        for e in fresh:
            seen.add(str(e.get("id")))
            _store(conn, client, account_id, e, stats, cache_dir, llm, retries, att_retries)
        if len(ids) < client.page_size or not ids:
            break
        if not fresh:  # a full page without anything new: the server ignores the position
            log.warning("plMail-Konto %s: Blättern liefert nichts Neues – Abgleich über die ID-Liste", account_id)
            break
        position += max(1, len(ids) - _overlap(client.page_size))
    # reconciliation over the plain id list: nothing skipped, nothing removed by mistake
    current = _all_ids(client, account_id)
    missing = [i for i in current if i not in seen]
    for i in range(0, len(missing), client.page_size):
        emails, _ = client.get(account_id, missing[i : i + client.page_size])
        for e in emails:
            seen.add(str(e.get("id")))
            if _store(conn, client, account_id, e, stats, cache_dir, llm, retries, att_retries):
                stats["recovered"] += 1
    listed = set(current)
    candidates = [eid for eid in _stored_ids(conn, account_id) if eid not in listed]
    gone = []
    if candidates:
        still_there = client.existing_ids(account_id, candidates)
        gone = [c for c in candidates if c not in still_there]

    def apply() -> int:
        removed = _delete_ids(conn, account_id, gone)
        for eid in gone:
            retries.done(eid)
            att_retries.done(eid)
        _set_state(conn, account_id, state)
        conn.commit()
        return removed
    stats["removed"] += retry_locked(conn, apply, on_retry=_reloader(retries, att_retries))


def sync_account(conn: sqlite3.Connection, client: JMAPClient, account_id: str, *, cache_dir: Path,
                 llm=None) -> dict:
    stats = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0, "errors": 0, "mode": "changes"}
    retries = _Retries(conn, account_id)
    att_retries = _Retries(conn, account_id, "jmap_att_retry")
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"jmap_state:{account_id}",)).fetchone()
    state = row[0] if row else None
    done = False
    if state:
        try:
            _refetch(conn, client, account_id, stats, cache_dir, llm, retries, att_retries, retries, force=False)
            _refetch(conn, client, account_id, stats, cache_dir, llm, retries, att_retries, att_retries,
                     force=True)
            _sync_changes(conn, client, account_id, state, stats, cache_dir, llm, retries, att_retries)
            done = True
        except JMAPError as exc:
            if exc.type != "cannotCalculateChanges":
                raise
            log.warning("plMail-Konto %s: Änderungen nicht berechenbar – vollständiger Abgleich", account_id)
    if not done:
        _sync_full(conn, client, account_id, stats, cache_dir, llm, retries, att_retries)
    stats["attachments_pending"] = len(att_retries.items)  # mails still waiting for an attachment
    return stats


def sync_plmail(conn: sqlite3.Connection, client: JMAPClient, *, cache_dir: Path, llm=None) -> dict:
    """Synchronise all accounts; problems are reported in the result, never raised."""
    result: dict = {"accounts": {}, "error": None}
    try:
        accounts = client.account_ids()
    except JMAPError as exc:
        log.warning("plMail: %s", exc)
        result["error"] = str(exc)
        return result
    for acc in accounts:
        try:
            result["accounts"][acc] = sync_account(conn, client, acc, cache_dir=cache_dir, llm=llm)
        except JMAPError as exc:
            log.warning("plMail-Konto %s: %s", acc, exc)
            conn.rollback()
            result["accounts"][acc] = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 – an unexpected answer must not stop the other sources
            log.exception("plMail-Konto %s: unerwarteter Fehler", acc)
            conn.rollback()
            result["accounts"][acc] = {"error": f"unerwarteter Fehler: {exc}"[:300]}
    return result
