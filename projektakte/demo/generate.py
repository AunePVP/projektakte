"""Deterministic generator for the demo data set (fixed seed, no timestamps in files)."""
from __future__ import annotations

import json
import os
import random
from datetime import date, datetime, timezone
from pathlib import Path

from . import data
from .documents import project_files
from .eval import EVAL
from .mails import render_all

SEED = 4711
# bump when the generated content changes, so DEMO_MODE regenerates outdated demo data
DEMO_VERSION = "5"  # 5: escape plan with ISO 7010 signs and legend


def _mtime(d: date) -> float:
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc).timestamp()


def _write(path: Path, content: bytes, when: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)
    ts = _mtime(when)
    os.utime(path, (ts, ts))


def generate(demo_dir: Path, seed: int = SEED) -> dict:
    """Write the demo data set to ``demo_dir`` and return the manifest."""
    demo_dir = Path(demo_dir)
    rng = random.Random(seed)
    written: list[str] = []
    projects_root = demo_dir / "projekte"
    for p in data.PROJECTS:
        folder = projects_root / data.folder_name(p)
        for rel, content, when in project_files(p, rng):
            target = folder / rel
            _write(target, content, when)
            written.append(str(target.relative_to(demo_dir)))

    mails_meta = []
    for fname, raw, m in render_all(rng):
        target = demo_dir / "mails" / fname
        _write(target, raw, m["date"].date())
        rel = str(target.relative_to(demo_dir))
        written.append(rel)
        mails_meta.append({
            "file": rel, "category": m["category"], "project": m["project"],
            "subject": m["subject"], "attachments": [a[0] for a in m["attachments"]],
        })

    # remove files from older generator versions (only inside our own demo folder)
    keep = set(written) | {"manifest.json"}
    for f in sorted(demo_dir.rglob("*"), reverse=True):
        rel = str(f.relative_to(demo_dir))
        if f.is_file() and rel not in keep:
            f.unlink()
        elif f.is_dir() and not any(f.iterdir()):
            f.rmdir()

    manifest = {
        "version": DEMO_VERSION,
        "seed": seed,
        "projects": [{"number": p["number"], "name": p["name"], "kind": p["kind"]}
                     for p in data.PROJECTS],
        "files": sorted(written),
        "mails": mails_meta,
    }
    (demo_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    return manifest


def mail_message_ids(seed: int = SEED) -> dict[str, str]:
    """File name below ``demo/mails`` → Message-ID of every demo mail, computed without writing
    anything (lets the index drop demo mails even when the demo folder was deleted meanwhile)."""
    rng = random.Random(seed)
    for p in data.PROJECTS:  # the same random sequence as generate()
        project_files(p, rng)
    return {fname: m["message_id"] for fname, _raw, m in render_all(rng)}


def is_current(demo_dir: Path) -> bool:
    try:
        manifest = json.loads((Path(demo_dir) / "manifest.json").read_text())
    except (OSError, ValueError):
        return False
    return manifest.get("version") == DEMO_VERSION and all(
        (Path(demo_dir) / f).exists() for f in manifest.get("files", []))


def ensure(demo_dir: Path) -> bool:
    """Generate the demo data if it is missing or outdated. Returns True if it was (re)written."""
    if is_current(demo_dir):
        return False
    generate(demo_dir)
    return True


def eval_set() -> list[dict]:
    return EVAL


def write_eval(path: Path) -> None:
    Path(path).write_text(json.dumps(EVAL, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
