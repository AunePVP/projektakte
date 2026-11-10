"""Layout of the vertical timeline on a project page.

The timeline is one SVG: the newest entry is at the top, time runs downwards on a root curve, so recent
weeks get much room and old years little (not fully logarithmic). Every entry is a dot on the axis; where
there is room it also gets a one-line label joined to its dot by a thin curve, where there is not, the
entries of a stretch are summed up in a chip ("23 Einträge · Mär 18 – Okt 19"). Ticks name days, weeks,
months and years where they fit.

All positions are computed here (pure functions, no database), the template only prints them – the
templates may not use inline styles (Content Security Policy), SVG attributes are fine.
"""
from __future__ import annotations

from datetime import date, timedelta

WIDTH = 350  # viewBox width: ticks | axis | labels
AXIS_X = 62
LABEL_X = 84
LABEL_W = WIDTH - LABEL_X - 4
PAD = 14  # space above the newest and below the oldest entry
CURVE = 0.45  # position = (age / oldest age) ** CURVE: < 1 spreads the recent time out
GAP = 19  # least distance of two labels
DRIFT = 26  # how far below its dot a label may sit before the entry goes into a chip
CLUSTER_SPAN = 76  # a chip sums up the entries of about this much axis
TITLE_CHARS = 27
MIN_SPAN_DAYS = 30
EVERY_LABEL = 30  # up to this many entries every one gets its own line (the timeline grows), no chips
MON = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")


def _n(value: float) -> str:
    return f"{value:.1f}"


def short_date(day: date) -> str:
    return f"{day.day:02d}.{day.month:02d}.{day.year % 100:02d}"


def month_text(day: date) -> str:
    return f"{MON[day.month - 1]} {day.year % 100:02d}"


def _cut(text: str, limit: int = TITLE_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


MAX_CHIP_LEADERS = 6  # lines from the dots of a chip to it (more would only be noise)


def _sample(values: list[float], limit: int) -> list[float]:
    """At most ``limit`` of ``values``, spread evenly (first and last included)."""
    if len(values) <= limit:
        return values
    return [values[round(i * (len(values) - 1) / (limit - 1))] for i in range(limit)]


def height_for(count: int) -> int:
    """Pixel height of the timeline: enough for the labels of the recent entries, never endless."""
    return max(460, min(2400, 400 + 8 * count))


def group_key(first: date, last: date) -> str:
    return f"z{first.isoformat()}_{last.isoformat()}"


def build(entries: list[dict], today: date, art: str = "alle", selected: str = "") -> dict:
    """Layout of ``entries`` (dicts with ``key``, ``kind`` 'doc'/'mail', ``title``, ``full``, ``day`` (a date
    or None), ``sort``) – newest first after sorting here. ``art``: 'alle', 'doc' or 'mail'. ``selected`` is
    the key of the entry (or group, see :func:`group_key`) to mark. Entries without a date are only counted."""
    use = [e for e in entries if art == "alle" or e["kind"] == art]
    dated = sorted((e for e in use if e["day"] is not None), key=lambda e: e["sort"], reverse=True)
    out = {"width": WIDTH, "axis": AXIS_X, "label_x": LABEL_X, "label_w": LABEL_W, "height": 0, "ticks": [],
           "dots": [], "labels": [], "chips": [], "leaders": [], "undated": len(use) - len(dated),
           "count": len(use), "empty": not use}
    if not dated:
        return out
    newest = dated[0]["day"]
    ref = min(newest, today)  # an entry dated in the future is no reference for "now"
    oldest = dated[-1]["day"]
    span = max(MIN_SPAN_DAYS, (ref - oldest).days)
    height = height_for(len(dated))
    usable = height - 2 * PAD

    def y_of(day: date) -> float:
        age = max(0, (ref - day).days)
        return PAD + usable * (min(1.0, age / span) ** CURVE)

    # ticks: where the scale is finest the labels are days and weeks, then months, then years
    marks: dict[date, tuple[str, bool]] = {}

    def add(day: date, text: str, major: bool = False) -> None:
        if 0 <= (ref - day).days <= span:
            marks.setdefault(day, (text, major))

    add(ref, "heute" if ref == today else short_date(ref), True)
    for w in range(1, 5):
        add(ref - timedelta(days=7 * w), f"−{w} Wo")
    first = date(ref.year, ref.month, 1)
    for _ in range(14):
        if (ref - first).days > 25:
            add(first, str(first.year) if first.month == 1 else MON[first.month - 1], first.month == 1)
        first = (first - timedelta(days=1)).replace(day=1)
    for year in range(ref.year - 1, oldest.year - 1, -1):
        add(date(year, 1, 1), str(year), True)
    last_shown = -99.0
    for day, (text, major) in sorted(marks.items(), key=lambda kv: kv[0], reverse=True):
        y = y_of(day)
        show = y - last_shown >= 15
        if show:
            last_shown = y
        out["ticks"].append({"y": _n(y), "text": text, "major": major, "show": show})

    # dots, labels and chips, newest first
    def look(e: dict) -> str:
        return e["kind"]

    seen_dots: set[tuple[int, str]] = set()
    for e in dated:  # dots on the same spot would be one: a project of thousands stays a light page
        y = y_of(e["day"])
        if (spot := (round(y * 2), look(e))) not in seen_dots:
            seen_dots.add(spot)
            out["dots"].append({"y": _n(y), "kind": look(e)})
    last = -float(GAP)
    drift = float("inf") if len(dated) <= EVERY_LABEL else DRIFT
    cluster: list[dict] = []

    def close() -> None:
        nonlocal last, cluster
        if not cluster:
            return
        ys = [y_of(e["day"]) for e in cluster]
        y = max(last + GAP + 2, sum(ys) / len(ys))
        newest_day, oldest_day = cluster[0]["day"], cluster[-1]["day"]
        a, b = month_text(oldest_day), month_text(newest_day)
        count = len(cluster)
        text = f"{count} {'Eintrag' if count == 1 else 'Einträge'} · {a if a == b else a + ' – ' + b}"
        key = group_key(oldest_day, newest_day)
        out["chips"].append({"y": _n(y), "y_top": _n(y - 10), "y_text": _n(y + 4), "text": text, "key": key,
                             "width": min(LABEL_W, int(26 + 6.3 * len(text))), "sel": key == selected})
        for ey in _sample(ys, MAX_CHIP_LEADERS):
            out["leaders"].append({"d": f"M{AXIS_X + 4} {_n(ey)} C {AXIS_X + 12} {_n(ey)}, {AXIS_X + 12} {_n(y)}, "
                                        f"{LABEL_X} {_n(y)}"})
        last = y + 3
        cluster = []

    for i, e in enumerate(dated):
        y = y_of(e["day"])
        want = max(y, last + GAP)
        if not cluster and want - y <= drift:
            out["labels"].append({
                "key": e["key"], "kind": e["kind"], "y": _n(want), "y_top": _n(want - 9), "y_text": _n(want + 4),
                "date": short_date(e["day"]), "title": _cut(e["title"], 120), "full": e["full"], "sel": e["key"] == selected})
            out["leaders"].append({"d": f"M{AXIS_X + 4} {_n(y)} C {AXIS_X + 12} {_n(y)}, {AXIS_X + 12} {_n(want)}, "
                                        f"{LABEL_X} {_n(want)}"})
            last = want
        else:
            cluster.append(e)
            following = dated[i + 1] if i + 1 < len(dated) else None
            if following is None or y_of(following["day"]) - last > CLUSTER_SPAN:
                close()
    close()
    out["height"] = int(max(height, last + PAD + 12))  # never end above the last line
    return out
