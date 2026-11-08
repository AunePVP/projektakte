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


def log_startup(settings, auth) -> None:
    """One INFO line with the version and the effective configuration – URLs without credentials,
    no secrets."""
    from . import __version__

    if auth.disabled:
        access = "AUS (AUTH_DISABLED=1)"
    else:
        access = f"{len(auth.users)} Benutzer, Cookies {'mit' if auth.cookie_secure else 'ohne'} Secure"
    log.info("Projektakte %s – Konfiguration: APP_ENV=%s, AI_MODE=%s, DEMO_MODE=%s, DATA_DIR=%s, "
             "PROJECTS_DIR=%s, MAIL_DIR=%s, LLM=%s (%s, Timeout %gs), Embeddings=%s (%s, Timeout %gs), "
             "Reranker=%s (Timeout %gs), plMail=%s, Zuordnung=%s, Zugangsschutz=%s, Proxy-Vertrauen=%s",
             __version__, settings.app_env, settings.ai_mode, "an" if settings.demo_mode else "aus",
             settings.data_dir, settings.projects_dir, settings.mail_dir, public_url(settings.llm_url),
             settings.llm_model, settings.llm_timeout, public_url(settings.embed_url), settings.embed_model,
             settings.embed_timeout, public_url(settings.rerank_url), settings.rerank_timeout,
             public_url(settings.plmail_url) if settings.plmail_enabled else "aus",
             settings.assign_mode, access, trusted_proxies(settings))
