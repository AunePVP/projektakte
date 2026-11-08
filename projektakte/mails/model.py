"""Source-independent mail record and the .eml parser."""
from __future__ import annotations

import email
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.utils import getaddresses, parsedate_to_datetime

from ..extract import decode_text, html_to_text

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Berlin")  # the office's time zone (dates shown to people)
except Exception:  # noqa: BLE001 – no tz database available: fall back to UTC
    LOCAL_TZ = timezone.utc


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
    # assignment made by a human in plMail (JMAP property "plmail:projektakte"); not part of the hash
    external_assignment: dict | None = None
    # header that marks a newsletter/mass mailing ("List-Unsubscribe", "Precedence: bulk"); not hashed
    bulk: str | None = None

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


def local_time(iso: str | None) -> datetime | None:
    """A stored ISO timestamp (UTC) in the office's local time; None if missing or unreadable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def local_day(iso: str | None) -> str | None:
    """Local calendar day (YYYY-MM-DD) of a stored UTC timestamp – a mail at 00:30 in Berlin belongs
    to that day, not to the UTC day before."""
    dt = local_time(iso)
    return dt.date().isoformat() if dt else None


def de_datetime(iso: str | None) -> str:
    """German local date and time ("26.02.2025 16:58") as shown on the mail page; "" if unknown."""
    dt = local_time(iso)
    return dt.strftime("%d.%m.%Y %H:%M") if dt else ""


BULK_PRECEDENCE = ("bulk", "junk")


def bulk_marker(msg) -> str | None:
    """Header that identifies a newsletter or mass mailing, or None.

    ``List-Unsubscribe`` is what newsletter senders must set; ``Precedence: bulk/junk`` marks mass
    mailings. ``List-Id`` alone is not used – project teams may use mailing lists."""
    if msg.get("List-Unsubscribe"):
        return "List-Unsubscribe"
    precedence = str(msg.get("Precedence") or "").strip().lower()
    if precedence in BULK_PRECEDENCE:
        return f"Precedence: {precedence}"
    return None


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
        body=body.strip(), message_id=str(msg.get("Message-ID") or "").strip() or None,
        in_reply_to=str(msg.get("In-Reply-To") or "").strip() or None, attachments=attachments,
        bulk=bulk_marker(msg))
    rec.compute_hash()
    return rec
