"""Start-up checks: configuration errors stop the start with a clear German message, and the
effective (non-secret) configuration is logged once so that problems can be analysed later."""
from __future__ import annotations

import ipaddress
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("projektakte.startup")


class ConfigError(RuntimeError):
    """The configuration is unusable or unsafe: the application refuses to start.

    The message is German and meant for the operator (it ends up in the container log)."""


def _uid_hint() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid is None:
        return ""
    return (f" Die Anwendung läuft als Benutzer-ID {uid}: auf dem Host das Verzeichnis mit "
            f"'sudo chown -R {uid}:{uid} <Verzeichnis>' freigeben oder ein benanntes Docker-Volume "
            f"verwenden (-v projektakte-daten:/data).")


def check_data_dir(data_dir: Path, db_path: Path | None = None) -> None:
    """DATA_DIR must exist (or be creatable) and be writable, as must an existing database file.

    Raises ConfigError instead of the later, cryptic ``sqlite3.OperationalError``."""
    data_dir = Path(data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".schreibtest-"):
            pass
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise ConfigError(f"DATA_DIR '{data_dir}' ist nicht beschreibbar ({reason})." + _uid_hint()) from None
    for path in (db_path, *(Path(f"{db_path}{s}") for s in ("-wal", "-shm"))) if db_path else ():
        if path.exists() and not os.access(path, os.R_OK | os.W_OK):
            raise ConfigError(f"Die Datenbankdatei '{path}' ist nicht beschreibbar." + _uid_hint())


def public_url(url: str) -> str:
    """URL without user/password and query string – safe to log."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<ungültige URL>"
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def trusted_proxies(settings, *, warn: bool = False) -> str:
    """Value for uvicorn's ``forwarded_allow_ips``: FORWARDED_ALLOW_IPS or only the local host.

    Only from these addresses are X-Forwarded-For/-Proto honoured, i.e. replace the client address
    (login throttling, logs) and the scheme (https behind a TLS proxy). ``warn`` logs entries that
    uvicorn would not recognise."""
    value = (getattr(settings, "forwarded_allow_ips", "") or "").strip()
    if not value:
        return "127.0.0.1"
    if not warn:
        return value
    for entry in (e.strip() for e in value.split(",")):
        if entry == "*":
            log.warning("FORWARDED_ALLOW_IPS=* vertraut jedem Absender – nur zulässig, wenn der Port der "
                        "Anwendung ausschließlich über den Reverse-Proxy erreichbar ist")
            continue
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            log.warning("FORWARDED_ALLOW_IPS: '%s' ist keine IP-Adresse bzw. kein Netz – wird nicht erkannt",
                        entry)
    return value


# hybrid searches of one question in the worst case (ask.py): the question itself, the overview for
# a summary (_summary_hits) and up to two named parties such as "Bauherr" (_role_hits)
ASK_SEARCHES = 4
RERANK_FORMATS = ("tei", "cohere")


def ask_worst_case_seconds(settings) -> float:
    """Longest a question can take when every AI service hangs until its timeout – what a reverse
    proxy's timeout must exceed (deploy/README.md, "Zeitlimits"):
    ``searches x (EMBED_TIMEOUT + RERANK_TIMEOUT) + LLM_TIMEOUT``, capped by the overall budget of a
    question (``ASK_DEADLINE``, default 180 s) – AI calls never run past it."""
    from . import ask

    searches = getattr(ask, "MAX_SEARCHES", ASK_SEARCHES)
    # a timeout no longer triggers a second format detection (ai/rerank.py): one rerank call per search
    worst = searches * (settings.embed_timeout + settings.rerank_timeout) + settings.llm_timeout
    deadline = getattr(settings, "ask_deadline", 0) or 0
    return min(worst, deadline) if deadline > 0 else worst


def deadline_warnings(settings) -> list[str]:
    """Invalid numbers and timeouts, and an ASK_DEADLINE that does not fit the other settings – said at
    start-up (log lines with "Konfiguration:"), not found out by users."""
    from .ai.http import seconds_text
    from .config import ASK_DEADLINE_MIN

    out = list(getattr(settings, "config_warnings", ()))
    deadline = getattr(settings, "ask_deadline", 0) or 0
    requested = getattr(settings, "ask_deadline_requested", deadline)
    if 0 < requested < ASK_DEADLINE_MIN:
        out.append(f"ASK_DEADLINE={requested:g} ist zu knapp für Suche und Sprachmodell – "
                   f"verwendet werden {seconds_text(deadline)} s (0 schaltet das Zeitbudget ab)")
    if deadline > 0 and settings.llm_timeout >= deadline:
        out.append(f"LLM_TIMEOUT={seconds_text(settings.llm_timeout)} s ist nicht kleiner als ASK_DEADLINE="
                   f"{seconds_text(deadline)} s: das Sprachmodell bekommt höchstens die Restzeit der Frage "
                   f"(bei langsamer Suche {seconds_text(deadline / 2)} s) – ASK_DEADLINE erhöhen oder "
                   f"LLM_TIMEOUT senken")
    return out


def log_startup(settings, auth) -> None:
    """One INFO line with the version and the effective configuration – URLs without credentials,
    no secrets – including the effective project layout (number scheme, folder depth)."""
    from . import __version__
    from .structure import current_layout

    for warning in deadline_warnings(settings):
        log.warning("Konfiguration: %s", warning)
    layout = current_layout(settings)
    if layout.warning:  # a newer proposal next to the confirmed scheme
        log.warning("Projektordner: %s", layout.warning)
    if layout.auto and layout.pattern is None and layout.state in ("pending", "rejected"):
        log.warning("Projektordner: %s", layout.problem)
    for notice in layout.notices:
        log.warning("Projektordner: %s", notice)

    if auth.disabled:
        access = "AUS (AUTH_DISABLED=1)"
    else:
        access = f"{len(auth.users)} Benutzer, Cookies {'mit' if auth.cookie_secure else 'ohne'} Secure"
    log.info("Projektakte %s – Konfiguration: APP_ENV=%s, AI_MODE=%s, DEMO_MODE=%s, DATA_DIR=%s, "
             "PROJECTS_DIR=%s, MAIL_DIR=%s, LLM=%s (%s, Timeout %gs), Embeddings=%s (%s, Timeout %gs), "
             "Reranker=%s (Timeout %gs), längste Frage bis %gs (Proxy-Zeitlimit darüber), plMail=%s, "
             "Zuordnung=%s, Zugangsschutz=%s, Proxy-Vertrauen=%s, Projektnummern=%s",
             __version__, settings.app_env, settings.ai_mode, "an" if settings.demo_mode else "aus",
             settings.data_dir, settings.projects_dir, settings.mail_dir, public_url(settings.llm_url),
             settings.llm_model, settings.llm_timeout, public_url(settings.embed_url), settings.embed_model,
             settings.embed_timeout, public_url(settings.rerank_url), settings.rerank_timeout,
             ask_worst_case_seconds(settings), public_url(settings.plmail_url) if settings.plmail_enabled else "aus",
             settings.assign_mode, access, trusted_proxies(settings), layout.summary())
