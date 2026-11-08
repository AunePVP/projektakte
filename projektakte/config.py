"""Settings from environment variables (no secrets in code).

Every address, model name and timeout comes from the environment so that switching from the
fake AI services (development, tests) to the real ones (staging, production) needs no code change.
"""
from __future__ import annotations

import json
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
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


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
    cookie_secure: bool = False
    # plMail (JMAP)
    plmail_url: str = ""
    plmail_token: str = field(default="", repr=False)
    plmail_timeout: float = 30.0
    plmail_page_size: int = 100
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


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = dict(os.environ if env is None else env)
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
        app_env=e.get("APP_ENV", "dev"),
        projects_dir=Path(e.get("PROJECTS_DIR") or default_projects),
        mail_dir=Path(e.get("MAIL_DIR") or default_mails),
        project_number_pattern=e.get("PROJECT_NUMBER_PATTERN") or DEFAULT_PROJECT_NUMBER_PATTERN,
        demo_mode=demo_mode,
        ai_mode=(e.get("AI_MODE") or "real").strip().lower(),
        llm_url=(e.get("LLM_URL") or "http://192.168.178.62:8000/v1").rstrip("/"),
        llm_model=e.get("LLM_MODEL") or "qwen3.8-flash-next",
        llm_vision=_bool(e.get("LLM_VISION")),
        llm_timeout=_float(e.get("LLM_TIMEOUT"), 120.0),
        llm_max_tokens=_int(e.get("LLM_MAX_TOKENS"), 800),
        llm_extra_body=extra_body,
        embed_url=(e.get("EMBED_URL") or "http://192.168.178.62:8001").rstrip("/"),
        embed_model=e.get("EMBED_MODEL") or "qwen3-embedding-4b",
        embed_format=(e.get("EMBED_FORMAT") or "auto").lower(),
        embed_timeout=_float(e.get("EMBED_TIMEOUT"), 60.0),
        embed_batch=max(1, _int(e.get("EMBED_BATCH"), 32)),
        embed_query_instruction=e.get("EMBED_QUERY_INSTRUCTION", DEFAULT_QUERY_INSTRUCTION),
        rerank_url=(e.get("RERANK_URL") or "http://192.168.178.62:4343/rerank").rstrip("/"),
        rerank_model=e.get("RERANK_MODEL", ""),
        rerank_format=(e.get("RERANK_FORMAT") or "auto").lower(),
        rerank_timeout=_float(e.get("RERANK_TIMEOUT"), 30.0),
        rerank_top_k=max(1, _int(e.get("RERANK_TOP_K"), 30)),
        health_timeout=_float(e.get("HEALTH_TIMEOUT"), 3.0),
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
        cookie_secure=_bool(e.get("COOKIE_SECURE")),
        plmail_url=(e.get("PLMAIL_URL") or "").rstrip("/"),
        plmail_token=e.get("PLMAIL_TOKEN", ""),
        plmail_timeout=_float(e.get("PLMAIL_TIMEOUT"), 30.0),
        plmail_page_size=max(1, _int(e.get("PLMAIL_PAGE_SIZE"), 100)),
        llm_api_key=e.get("LLM_API_KEY", ""),
        embed_api_key=e.get("EMBED_API_KEY", ""),
        rerank_api_key=e.get("RERANK_API_KEY", ""),
    )
