"""``python -m projektakte.index`` – run one indexing pass in the foreground.

``--purge-demo`` instead removes the demo projects and demo mails from the index (see
demo_cleanup); a run with DEMO_MODE=0 does that by itself after demo data was active.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import db, demo
from .ai.services import AIServices, resolve_settings
from .config import load_settings
from .demo_cleanup import purge_demo_data
from .indexer import run_index

log = logging.getLogger("projektakte.index")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m projektakte.index",
                                 description="Einen Indexierungslauf im Vordergrund ausführen")
    ap.add_argument("--purge-demo", action="store_true",
                    help="nur die Demo-Projekte und Demo-Mails aus dem Index entfernen (kein Indexierungslauf)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = resolve_settings(load_settings())
    if args.purge_demo:
        if settings.demo_mode:
            print("DEMO_MODE=1 ist gesetzt – die Demo-Daten würden beim nächsten Lauf wieder eingelesen. "
                  "Erst DEMO_MODE=0 setzen.", file=sys.stderr)
            return 2
        db.init_db(settings.db_path)
        with db.session(settings.db_path) as conn:
            stats = purge_demo_data(conn, settings)
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        return 0
    if settings.demo_mode and demo.ensure(settings.demo_dir):  # same as the app's start-up
        log.info("DEMO_MODE: Demo-Datensatz in %s erzeugt", settings.demo_dir)
    stats = run_index(settings, AIServices.from_settings(settings))
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
