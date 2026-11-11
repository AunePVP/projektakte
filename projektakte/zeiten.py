"""Times shown on the status page: elapsed time, averages and rough estimates ("ETA"), in German.

Averages that should survive a restart are moving averages in ``meta`` (one row each, JSON): every new
sample counts fully, older ones fade (``DECAY`` per sample, so roughly the last 50 count) – a share that
gets faster or slower, or another model, shows in the estimate after a while. They are written where the
indexer or the Bildauswertung writes anyway (a project done, a page stored), never per file.

An estimate is only given once enough samples exist (``MIN_*``); it is labelled "grob" and rounded so that
it does not pretend a precision it has not.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass

DECAY = 0.98  # weight of the older samples per new one (about the last 50 count)
PROJECT_KEY = "eta_project_seconds"  # seconds per project of the queue (documents pass, first complete read)
FILE_KEY = "eta_file_seconds"  # seconds per file actually read (extracted and written; not unchanged ones)
PAGE_KEY = "eta_vision_page_seconds"  # seconds the model takes per scan page (Bildauswertung)
EMBED_KEY = "eta_embed_section_seconds"  # seconds per section in the background embedding (all its batches at once)
MIN_FILES = 20  # files of a project seen before its own rate gives an estimate
MIN_PROJECTS = 5  # projects done before the queue gets a (rough) estimate …
MIN_FILES_READ = 20  # … and files read
MIN_PAGES = 10  # pages read by the model before the Bildauswertung gets an estimate
MIN_SECTIONS = 100  # sections embedded before the embedding gets an estimate


@dataclass(frozen=True)
class Mean:
    """A moving average: ``value`` (None without samples) and how many samples were taken in all."""
    value: float | None
    samples: int


def read(conn: sqlite3.Connection, key: str) -> Mean:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        data = json.loads(row[0]) if row and row[0] else {}
        total, weight, n = float(data.get("sum", 0)), float(data.get("weight", 0)), int(data.get("n", 0))
    except (ValueError, TypeError, AttributeError):
        return Mean(None, 0)
    if weight <= 0 or not math.isfinite(total / weight):
        return Mean(None, n)
    return Mean(total / weight, n)


def add(conn: sqlite3.Connection, key: str, seconds: float, count: int = 1) -> None:
    """Add a sample of ``count`` items that took ``seconds`` together (no commit: the caller's transaction)."""
    if count <= 0 or not math.isfinite(seconds) or seconds < 0:
        return
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        data = json.loads(row[0]) if row and row[0] else {}
        total, weight, n = float(data.get("sum", 0)), float(data.get("weight", 0)), int(data.get("n", 0))
    except (ValueError, TypeError, AttributeError):
        total = weight = 0.0
        n = 0
    fade = DECAY ** count
    total, weight, n = total * fade + seconds, weight * fade + count, n + count
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps({"sum": total, "weight": weight, "n": n})))


# -- German formatting ---------------------------------------------------------------------------------------
def dauer(seconds: float | None) -> str:
    """An elapsed time: "45 s", "3 Min. 20 s", "12 Min.", "2 Std. 14 Min.", "3 Tage 4 Std."; "–" if unknown."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "–"
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} Min. {s} s" if m < 10 and s else f"{m} Min."
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} Std. {m} Min." if m else f"{h} Std."
    d, h = divmod(h, 24)
    return f"{d} Tage {h} Std." if h else f"{d} Tage"


def etwa(seconds: float | None) -> str:
    """A rough estimate, rounded and marked with ≈: "≈ 1 Min.", "≈ 25 Min.", "≈ 2 Std. 15 Min.", "≈ 3 Tage";
    "–" if unknown."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "–"
    if seconds < 90:
        return "≈ 1 Min." if seconds >= 30 else "unter 1 Min."
    minutes = seconds / 60
    if minutes < 60:
        return f"≈ {round(minutes)} Min."
    if minutes < 10 * 60:  # to 5 minutes
        total = int(round(minutes / 5) * 5)
        h, m = divmod(total, 60)
        return f"≈ {h} Std. {m} Min." if m else f"≈ {h} Std."
    hours = minutes / 60
    if hours < 48:
        return f"≈ {round(hours)} Std."
    days = hours / 24
    if days < 14:
        return f"≈ {_de(days, 1 if days < 10 else 0)} Tage"
    if days < 70:
        return f"≈ {round(days / 7)} Wochen"
    return f"≈ {round(days / 30)} Monate"


def _de(x: float, digits: int) -> str:
    value = round(x, digits)
    if digits and value != int(value):
        return f"{value:.{digits}f}".replace(".", ",")
    return str(int(value))


def je(seconds: float | None) -> str:
    """An average per item: "0,4 s", "4,2 s", "48 s", "2 Min. 10 s"."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "–"
    if seconds < 0.05:
        return "unter 0,1 s"
    if seconds < 10:
        return f"{_de(seconds, 1)} s"
    return dauer(seconds)


# -- estimates -----------------------------------------------------------------------------------------------
def project_eta(seen: int, total: int, elapsed: float) -> float | None:
    """Seconds left for the project being read: its own rate so far (listing, unchanged and read files
    together) times the files left – only once ``MIN_FILES`` were gone through."""
    if seen < MIN_FILES or total <= seen or elapsed <= 0:
        return 0.0 if total and seen >= total else None
    return elapsed / seen * (total - seen)


def queue_eta(projects_left: int, per_project: Mean, per_file: Mean) -> float | None:
    """Rough seconds left for the queue: projects left times the moving average per project – only once
    ``MIN_PROJECTS`` projects and ``MIN_FILES_READ`` files were read (else None)."""
    if projects_left <= 0:
        return 0.0
    if per_project.value is None or per_project.samples < MIN_PROJECTS or per_file.samples < MIN_FILES_READ:
        return None
    return projects_left * per_project.value


def vision_rate(per_page: Mean, concurrency: int, pending: int) -> dict:
    """What the status page shows for the Bildauswertung: seconds per page (one call), pages per hour with
    ``concurrency`` calls at a time, and the rough time for the pages pending – None before ``MIN_PAGES``."""
    if per_page.value is None or per_page.samples < MIN_PAGES or per_page.value <= 0:
        return {"per_page": per_page.value, "per_hour": None, "eta": None, "samples": per_page.samples}
    concurrency = max(1, int(concurrency))
    return {"per_page": per_page.value, "per_hour": 3600 * concurrency / per_page.value,
            "eta": pending * per_page.value / concurrency, "samples": per_page.samples}


def embed_rate(per_section: Mean, pending: int) -> dict:
    """The background embedding: sections per hour and the rough time for the sections waiting – None before
    ``MIN_SECTIONS`` (the average already holds its concurrency: the time of a round per section)."""
    if per_section.value is None or per_section.samples < MIN_SECTIONS or per_section.value <= 0:
        return {"per_hour": None, "eta": None}
    return {"per_hour": 3600 / per_section.value, "eta": pending * per_section.value}
