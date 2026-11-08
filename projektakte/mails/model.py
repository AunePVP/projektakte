"""Source-independent mail record and the .eml parser."""
from __future__ import annotations

import email
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.utils import getaddresses, parsedate_to_datetime

from ..extract import decode_text, html_to_text


@dataclass
class Attachment:
    filename: str
    data: bytes
    mime: str = "application/octet-stream"


@dataclass
class MailRecord:
    source: str  # "eml" | "jmap"
    source_key: str  # unique within Projektakte, e.g. "eml:2024/x.eml" or "jmap:7:M123"
    subject: str
    sender: str  # e-mail address
    sender_name: str
    recipients: str
    date: str | None  # ISO 8601, UTC
    body: str
    message_id: str | None = None
    in_reply_to: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    content_hash: str = ""

    def compute_hash(self) -> str:
        h = hashlib.sha256()
        for part in (self.subject, self.sender, self.recipients, self.date or "", self.body):
            h.update(part.encode("utf-8", "replace") + b"\0")
        for a in self.attachments:
            h.update(a.filename.encode("utf-8", "replace") + b"\0" + hashlib.sha256(a.data).digest())
        self.content_hash = h.hexdigest()
        return self.content_hash


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _addresses(value) -> list[tuple[str, str]]:
    return [(n, a) for n, a in getaddresses([str(value)]) if a] if value else []


def parse_eml(data: bytes, source_key: str, fallback_date: datetime | None = None) -> MailRecord:
    msg = email.message_from_bytes(data, policy=policy.default)
    senders = _addresses(msg.get("From"))
    sender_name, sender = (senders[0] if senders else ("", ""))
    recipients = ", ".join(a for _, a in _addresses(msg.get("To")) + _addresses(msg.get("Cc")))
    date = None
    if msg.get("Date"):
        try:
            date = iso_utc(parsedate_to_datetime(str(msg["Date"])))
        except (TypeError, ValueError):
            date = None
    if date is None and fallback_date is not None:
        date = iso_utc(fallback_date)
    body_part = msg.get_body(("plain", "html"))
    body = ""
    if body_part is not None:
        try:
            body = body_part.get_content()
        except (LookupError, UnicodeDecodeError):
            body = decode_text(body_part.get_payload(decode=True) or b"")
        if body_part.get_content_type() == "text/html":
            body = html_to_text(body)
    attachments = []
    for part in msg.iter_attachments():
        name = part.get_filename()
        if not name:
            continue
        try:
            payload = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        elif isinstance(payload, email.message.Message):
            payload = payload.as_bytes()
        attachments.append(Attachment(name, payload, part.get_content_type()))
    rec = MailRecord(
        source="eml", source_key=source_key, subject=str(msg.get("Subject", "") or "").strip(),
        sender=sender.lower(), sender_name=sender_name, recipients=recipients, date=date,
        body=body.strip(), message_id=str(msg.get("Message-ID") or "") or None,
        in_reply_to=str(msg.get("In-Reply-To") or "") or None, attachments=attachments)
    rec.compute_hash()
    return rec
