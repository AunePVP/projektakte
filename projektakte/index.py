"""``python -m projektakte.index`` – run one indexing pass in the foreground."""
from __future__ import annotations

import json
import logging

from .ai.services import AIServices, resolve_settings
from .config import load_settings
from .indexer import run_index


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = resolve_settings(load_settings())
    stats = run_index(settings, AIServices.from_settings(settings))
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
