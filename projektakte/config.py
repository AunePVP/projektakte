"""Settings from environment variables (no secrets in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    port: int
    data_dir: Path
    app_env: str


def load_settings() -> Settings:
    return Settings(
        port=int(os.environ.get("PORT", "8000")),
        data_dir=Path(os.environ.get("DATA_DIR", "./data")),
        app_env=os.environ.get("APP_ENV", "dev"),
    )
