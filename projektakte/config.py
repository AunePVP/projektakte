"""Settings from environment variables (no secrets in code).

Every address, model name and timeout comes from the environment so that switching from the
fake AI services (development, tests) to the real ones (staging, production) needs no code change.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_PROJECT_NUMBER_PATTERN = r"BS-\d{4}-\d{4}"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _float(value: str | None, default: float) -> float:
    """A number from the environment; empty, malformed, NaN and infinite values give the default."""
    try:
        number = float(value) if value not in (None, "") else default
    except ValueError:
        return default
    return number if math.isfinite(number) else default


def _number_warnings(env: Mapping[str, str], names: tuple[str, ...]) -> list[str]:
    """Settings given as something other than a finite number (they fall back to their default)."""
    out = []
    for name in names:
        raw = (env.get(name) or "").strip()
        try:
            ok = not raw or math.isfinite(float(raw))
        except ValueError:
            ok = False
        if not ok:
            out.append(f"{name}={raw} ist keine gültige Zahl – es gilt der Standardwert")
    return out


def _timeout(env: Mapping[str, str], name: str, default: float, warnings: list[str]) -> float:
    """A service timeout in seconds: it must be a positive number – 0 or a negative value would make
    every call fail at once (there is no "no timeout": a hanging service must not hold a question)."""
    raw = (env.get(name) or "").strip()
    value = _float(raw, default)
    if raw and (value <= 0 or value != _float(raw, -1.0)):
        warnings.append(f"{name}={raw} ist kein gültiges Zeitlimit (Sekunden größer 0) – verwendet werden "
                        f"{default:g} s")
        return default
    return value


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _assign_mode(value: str | None, plmail: bool) -> str:
    value = (value or "").strip().lower()
    if value in ("auto", "manual"):
        return value
    return "manual" if plmail else "auto"


@dataclass(frozen=True)
class Settings:
    port: int
    data_dir: Path
    app_env: str
    # sources
    projects_dir: Path
    mail_dir: Path
    project_number_pattern: str
    demo_mode: bool
    # AI services
    ai_mode: str  # "real" (use the URLs) or "fake" (start the fakes inside this process)
    llm_url: str
    llm_model: str
    llm_vision: bool
    llm_timeout: float
    llm_max_tokens: int
    llm_extra_body: dict
    embed_url: str
    embed_model: str
    embed_format: str  # auto | openai | tei
    embed_timeout: float
    embed_batch: int
    embed_query_instruction: str
    rerank_url: str
    rerank_model: str
    rerank_format: str  # auto | tei | cohere
    rerank_timeout: float
    rerank_top_k: int
    health_timeout: float
    ask_top_k: int
    ask_context_chars: int
    ask_min_rerank: float
    # indexing / assignment
    index_interval: float
    assign_min_share: float
    assign_min_lead: float
    # access control
    auth_users: str = field(default="", repr=False)
    auth_users_file: str = ""
    auth_disabled: bool = False
    secret_key: str = field(default="", repr=False)
    session_hours: float = 12.0
    cookie_secure: bool | None = None  # None = not set: on in APP_ENV=production, otherwise off
    # plMail (JMAP)
    plmail_url: str = ""
    plmail_token: str = field(default="", repr=False)
    plmail_timeout: float = 30.0
    plmail_page_size: int = 100
    plmail_web_url: str = ""  # link template to open a mail in plMail: …{accountId}…{emailId}…
    # assignment: "auto" (project number → assigned) or "manual" (humans decide, in plMail for its mails)
    assign_mode: str = "auto"
    # plMail → Projektakte integration API (Bearer token) and the public base URL for links
    integration_token: str = field(default="", repr=False)
    public_url: str = ""
    # search page: hide hits whose reranker score is below this (only when the reranker answered)
    search_min_rerank: float = 0.05
    # optional file with abbreviations/synonyms for the full-text query ("BMA = Brandmeldeanlage")
    synonyms_file: str = ""
    # minimum absolute similarity for a mail suggestion (in addition to share/lead); 0 = off
    assign_min_similarity: float = 0.0
    # reverse proxy: comma-separated IPs whose X-Forwarded-* headers are trusted (uvicorn)
    forwarded_allow_ips: str = ""
    log_level: str = "INFO"
    # allow an empty/missing MAIL_DIR to remove all eml mails (otherwise protected, see ops-06)
    mail_allow_empty: bool = False
    # overall time budget of one question in seconds (all searches + language model); 0 = off.
    # Values below ASK_DEADLINE_MIN are raised to it (startup warning); the configured value is kept
    # in ask_deadline_requested for that warning.
    ask_deadline: float = 180.0
    ask_deadline_requested: float = 180.0
    # problems found while reading the environment (logged as warnings at start-up)
    config_warnings: tuple[str, ...] = ()
    # optional API keys for the AI services (secrets)
    llm_api_key: str = field(default="", repr=False)
    embed_api_key: str = field(default="", repr=False)
    rerank_api_key: str = field(default="", repr=False)

    @property
    def plmail_enabled(self) -> bool:
        return bool(self.plmail_url and self.plmail_token)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "projektakte.sqlite3"

    @property
    def demo_dir(self) -> Path:
        return self.data_dir / "demo"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


DEFAULT_QUERY_INSTRUCTION = (
    "Instruct: Finde Abschnitte aus Brandschutz-Projektunterlagen, die die Frage beantworten\nQuery: "
)


# below this, searches and the language model share too little time to be of use (half of the budget
# is kept for the language model, an AI call needs at least a second – search.MIN_CALL_SECONDS)
ASK_DEADLINE_MIN = 10.0


def _ask_deadline(raw: float) -> float:
    if raw <= 0:
        return 0.0
    return max(raw, ASK_DEADLINE_MIN)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = dict(os.environ if env is None else env)
    warnings: list[str] = []
    data_dir = Path(e.get("DATA_DIR", "./data"))
    demo_mode = _bool(e.get("DEMO_MODE"))
    # With DEMO_MODE the generated demo data is the default source; otherwise conventional folders.
    default_projects = data_dir / "demo" / "projekte" if demo_mode else data_dir / "projekte"
    default_mails = data_dir / "demo" / "mails" if demo_mode else data_dir / "mails"
    try:
        extra_body = json.loads(e.get("LLM_EXTRA_BODY", "") or "{}")
        if not isinstance(extra_body, dict):
            extra_body = {}
    except json.JSONDecodeError:
        extra_body = {}
    return Settings(
        port=_int(e.get("PORT"), 8000),
        data_dir=data_dir,
        app_env=(e.get("APP_ENV") or "dev").strip().lower(),
        projects_dir=Path(e.get("PROJECTS_DIR") or default_projects),
        mail_dir=Path(e.get("MAIL_DIR") or default_mails),
        project_number_pattern=e.get("PROJECT_NUMBER_PATTERN") or DEFAULT_PROJECT_NUMBER_PATTERN,
        demo_mode=demo_mode,
        ai_mode=(e.get("AI_MODE") or "real").strip().lower(),
        llm_url=(e.get("LLM_URL") or "http://192.168.178.62:8000/v1").rstrip("/"),
        llm_model=e.get("LLM_MODEL") or "qwen3.8-flash-next",
        # the demo with fake services reads its plans and scans by default (deterministic transcript)
        llm_vision=(_bool(e.get("LLM_VISION")) if (e.get("LLM_VISION") or "").strip()
                    else demo_mode and (e.get("AI_MODE") or "").strip().lower() == "fake"),
        llm_timeout=_timeout(e, "LLM_TIMEOUT", 120.0, warnings),
        llm_max_tokens=_int(e.get("LLM_MAX_TOKENS"), 800),
        llm_extra_body=extra_body,
        embed_url=(e.get("EMBED_URL") or "http://192.168.178.62:8001").rstrip("/"),
        embed_model=e.get("EMBED_MODEL") or "qwen3-embedding-4b",
        embed_format=(e.get("EMBED_FORMAT") or "auto").lower(),
        embed_timeout=_timeout(e, "EMBED_TIMEOUT", 60.0, warnings),
        embed_batch=max(1, _int(e.get("EMBED_BATCH"), 32)),
        embed_query_instruction=e.get("EMBED_QUERY_INSTRUCTION", DEFAULT_QUERY_INSTRUCTION),
        rerank_url=(e.get("RERANK_URL") or "http://192.168.178.62:4343/rerank").rstrip("/"),
        rerank_model=e.get("RERANK_MODEL", ""),
        rerank_format=(e.get("RERANK_FORMAT") or "auto").lower(),
        rerank_timeout=_timeout(e, "RERANK_TIMEOUT", 30.0, warnings),
        rerank_top_k=max(1, _int(e.get("RERANK_TOP_K"), 30)),
        health_timeout=_timeout(e, "HEALTH_TIMEOUT", 3.0, warnings),
        ask_top_k=max(1, _int(e.get("ASK_TOP_K"), 6)),
        ask_context_chars=max(1000, _int(e.get("ASK_CONTEXT_CHARS"), 8000)),
        ask_min_rerank=_float(e.get("ASK_MIN_RERANK"), 0.01),
        index_interval=_float(e.get("INDEX_INTERVAL"), 300.0),
        assign_min_share=_float(e.get("ASSIGN_MIN_SHARE"), 0.5),
        assign_min_lead=_float(e.get("ASSIGN_MIN_LEAD"), 0.2),
        auth_users=e.get("AUTH_USERS", ""),
        auth_users_file=e.get("AUTH_USERS_FILE", ""),
        auth_disabled=_bool(e.get("AUTH_DISABLED")),
        secret_key=e.get("SECRET_KEY", ""),
        session_hours=_float(e.get("SESSION_HOURS"), 12.0),
        cookie_secure=_bool(e.get("COOKIE_SECURE")) if (e.get("COOKIE_SECURE") or "").strip() else None,
        log_level=(e.get("LOG_LEVEL") or "INFO").strip().upper(),
        mail_allow_empty=_bool(e.get("MAIL_ALLOW_EMPTY")),
        ask_deadline=_ask_deadline(_float(e.get("ASK_DEADLINE"), 180.0)),
        ask_deadline_requested=max(0.0, _float(e.get("ASK_DEADLINE"), 180.0)),
        config_warnings=tuple(warnings + _number_warnings(e, ("ASK_DEADLINE",))),
        plmail_url=(e.get("PLMAIL_URL") or "").rstrip("/"),
        plmail_token=e.get("PLMAIL_TOKEN", ""),
        plmail_timeout=_timeout(e, "PLMAIL_TIMEOUT", 30.0, warnings),
        plmail_page_size=max(1, _int(e.get("PLMAIL_PAGE_SIZE"), 100)),
        plmail_web_url=e.get("PLMAIL_WEB_URL", ""),
        # with plMail connected, mails are assigned by humans in plMail unless configured otherwise
        assign_mode=_assign_mode(e.get("ASSIGN_MODE"), bool(e.get("PLMAIL_URL") and e.get("PLMAIL_TOKEN"))),
        integration_token=e.get("INTEGRATION_TOKEN", ""),
        public_url=(e.get("PUBLIC_URL") or "").rstrip("/"),
        search_min_rerank=_float(e.get("SEARCH_MIN_RERANK"), 0.05),
        synonyms_file=e.get("SYNONYMS_FILE", ""),
        assign_min_similarity=_float(e.get("ASSIGN_MIN_SIMILARITY"), 0.0),
        forwarded_allow_ips=e.get("FORWARDED_ALLOW_IPS", ""),
        llm_api_key=e.get("LLM_API_KEY", ""),
        embed_api_key=e.get("EMBED_API_KEY", ""),
        rerank_api_key=e.get("RERANK_API_KEY", ""),
    )
