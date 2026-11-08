"""``python -m projektakte.demo`` – write the demo data set to ``$DATA_DIR/demo``."""
from __future__ import annotations

import argparse
from pathlib import Path

from ..config import load_settings
from .generate import SEED, generate, write_eval


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Demo-Datensatz für Projektakte erzeugen")
    ap.add_argument("--target", type=Path, help="Zielordner (Standard: $DATA_DIR/demo)")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--eval", type=Path, metavar="DATEI",
                    help="zusätzlich den Evaluationssatz schreiben (z. B. tests/eval.json)")
    a = ap.parse_args(argv)
    if a.eval:
        write_eval(a.eval)
        print(f"Evaluationssatz geschrieben: {a.eval}")
    target = a.target or load_settings().demo_dir
    manifest = generate(target, seed=a.seed)
    print(f"Demo-Datensatz in {target}: {len(manifest['projects'])} Projekte, "
          f"{len(manifest['mails'])} Mails, {len(manifest['files'])} Dateien")


if __name__ == "__main__":
    main()
