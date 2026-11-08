"""Second mail source: plMail via its JMAP API (RFC 8620/8621), strictly read-only.

See docs/plmail-jmap.md. Paths come from the session object; every JMAP account (one per connected
mailbox) is synchronised. First run: ``Email/query`` + ``Email/get`` page by page (position/limit);
afterwards only ``Email/changes`` from the stored ``state``. Methods that change anything
(``Email/set``, ``EmailSubmission/set``) are never used.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import urllib.parse
from datetime import datetime
from pathlib import Path

from ..ai.http import AIServiceError, HTTPStatusError, Timer, request, request_json
from ..extract import html_to_text
from .model import Attachment, MailRecord, iso_utc
from .store import delete_mail, store_mail

log = logging.getLogger("projektakte.mails.jmap")

USING = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
MAIL_CAP = "urn:ietf:params:jmap:mail"
PROPERTIES = ["id", "threadId", "subject", "from", "to", "cc", "receivedAt", "preview", "hasAttachment",
              "attachments", "bodyValues", "textBody", "htmlBody"]
MAX_ATTACHMENT_BYTES = 80 * 1024 * 1024


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
            data = request_json("POST", api_url, payload={"using": USING, "methodCalls": method_calls},
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
                           "properties": PROPERTIES, "fetchTextBodyValues": True,
                           "fetchHTMLBodyValues": True}, "g0"],
        ])
        ids = next(a for n, a, c in responses if c == "q0").get("ids") or []
        got = next(a for n, a, c in responses if c == "g0")
        return _in_order(got.get("list") or [], ids), got.get("state"), ids

    def get(self, account_id: str, ids: list[str]) -> tuple[list[dict], str | None]:
        responses = self.call([["Email/get", {"accountId": account_id, "ids": ids, "properties": PROPERTIES,
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
            info.update(ok=True, format="jmap", accounts=len(accounts), latency_ms=t.ms)
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
    """Cheap content fingerprint before downloading attachments (flag changes do not count)."""
    parts = [email.get("subject") or "", json.dumps(email.get("from") or [], sort_keys=True),
             email.get("receivedAt") or "", _body(email),
             ",".join(sorted((a or {}).get("blobId", "") for a in email.get("attachments") or []))]
    return hashlib.sha256("\0".join(parts).encode("utf-8", "replace")).hexdigest()


def to_record(client: JMAPClient, account_id: str, email: dict) -> MailRecord:
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
        if not att.get("blobId") or (att.get("size") or 0) > MAX_ATTACHMENT_BYTES:
            continue
        name = att.get("name") or f"anhang-{att.get('partId', '')}"
        try:
            data = client.download(account_id, att["blobId"], name, att.get("type") or "")
        except AIServiceError as exc:
            log.warning("Anhang %s nicht ladbar: %s", name, exc)
            continue
        attachments.append(Attachment(name, data, att.get("type") or "application/octet-stream"))
    rec = MailRecord(source="jmap", source_key=f"jmap:{account_id}:{email['id']}",
                     subject=(email.get("subject") or "").strip(), sender=sender, sender_name=sender_name,
                     recipients=recipients, date=date, body=_body(email), attachments=attachments)
    rec.compute_hash()
    return rec


def _store(conn, client, account_id, email, stats, cache_dir: Path, llm) -> None:
    key = f"jmap:{account_id}:{email['id']}"
    fp = fingerprint(email)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"jmap_fp:{key}",)).fetchone()
    exists = conn.execute("SELECT 1 FROM mails WHERE source_key = ? AND deleted = 0", (key,)).fetchone()
    if row and row[0] == fp and exists:
        stats["unchanged"] += 1
        return
    _, status = store_mail(conn, to_record(client, account_id, email), cache_dir=cache_dir, llm=llm)
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (f"jmap_fp:{key}", fp))
    stats[status] += 1


def _set_state(conn: sqlite3.Connection, account_id: str, state: str | None) -> None:
    if state:
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value", (f"jmap_state:{account_id}", state))


def sync_account(conn: sqlite3.Connection, client: JMAPClient, account_id: str, *, cache_dir: Path,
                 llm=None) -> dict:
    stats = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0, "mode": "changes"}
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"jmap_state:{account_id}",)).fetchone()
    state = row[0] if row else None
    if state:
        try:
            while True:
                ch = client.changes(account_id, state)
                ids = list(dict.fromkeys((ch.get("created") or []) + (ch.get("updated") or [])))
                for i in range(0, len(ids), client.page_size):
                    emails, _ = client.get(account_id, ids[i : i + client.page_size])
                    for e in emails:
                        _store(conn, client, account_id, e, stats, cache_dir, llm)
                for gone in ch.get("destroyed") or []:
                    r = conn.execute("SELECT id FROM mails WHERE source_key = ?",
                                     (f"jmap:{account_id}:{gone}",)).fetchone()
                    if r:
                        delete_mail(conn, r[0])
                        stats["removed"] += 1
                state = ch.get("newState") or state
                _set_state(conn, account_id, state)
                conn.commit()
                if not ch.get("hasMoreChanges"):
                    return stats
        except JMAPError as exc:
            if exc.type != "cannotCalculateChanges":
                raise
            log.warning("plMail-Konto %s: Änderungen nicht berechenbar – vollständiger Abgleich", account_id)
    # full synchronisation, page by page (position + limit; no anchors in plMail)
    stats["mode"] = "full"
    position, new_state, seen = 0, None, set()
    while True:
        emails, page_state, ids = client.query_page(account_id, position)
        new_state = new_state or page_state  # state of the first page: later arrivals come via changes
        for e in emails:
            seen.add(f"jmap:{account_id}:{e['id']}")
            _store(conn, client, account_id, e, stats, cache_dir, llm)
        conn.commit()
        if len(ids) < client.page_size:
            break
        position += len(ids)
    for r in conn.execute("SELECT id, source_key FROM mails WHERE source_key LIKE ?",
                          (f"jmap:{account_id}:%",)).fetchall():
        if r["source_key"] not in seen:
            delete_mail(conn, r["id"])
            stats["removed"] += 1
    _set_state(conn, account_id, new_state)
    conn.commit()
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
    return result
