"""Repetition loops in model answers: the model writes the same line or the same few words again and again
until it runs out of tokens (small vision models transcribing forms, tables and plans fall into this).

:func:`find_loop` finds the place where such a loop starts. A stretch counts as a loop if a sequence of 1 to
MAX_PERIOD words follows itself in a row – at least MIN_REPEATS times for a short unit (a line), at least
LONG_REPEATS times for a unit of LONG_UNIT_CHARS or more (a block of lines: no real page repeats a paragraph three
times in a row) – and the stretch has at least MIN_CHARS characters. A real page repeats a table head or
"[handschriftlich: ✓]" a few times, not six times in a row over hundreds of characters. Words are compared exactly
(case aside): rows that differ in a number are no loop.

A page with more than MAX_STAMPS "[Stempel: …]" counts as running away as well: seen with a small model, which
summed up the page as invented stamps ("[Stempel: „Teeküche vorhanden“]") and repeated that block to the limit. No
real page carries that many stamps; the answer is cut before the first one too many.
"""
from __future__ import annotations

import re

MIN_REPEATS = 6
LONG_REPEATS = 3
LONG_UNIT_CHARS = 200
MIN_CHARS = 400
MAX_PERIOD = 400  # words in the repeated unit (a long table row up to a block of twenty stamps)
STREAM_WINDOW = 1500  # words looked at while an answer streams in (the newest ones)
MAX_STAMPS = 10

_WORD = re.compile(r"\S+")
_STAMP = re.compile(r"\[Stempel:", re.IGNORECASE)


def find_loop(text: str, *, window: int | None = None, stamps: bool = True) -> int | None:
    """Character position where the first repetition loop of ``text`` starts, or None (see the module docstring;
    also the place of the stamp one too many).

    ``window``: only the last that many words are looked at (cheap enough to run while an answer streams
    in; a loop always grows at the end). ``stamps`` False: repetitions only (text put together from overlapping
    sections repeats a few stamps where they overlap)."""
    if not text:
        return None
    best = _too_many_stamps(text) if stamps else None
    words = [(m.start(), m.end(), m.group().lower()) for m in _WORD.finditer(text)]
    if window is not None and len(words) > window:
        words = words[-window:]
    n = len(words)
    toks = [w[2] for w in words]
    for p in range(1, min(MAX_PERIOD, n // LONG_REPEATS) + 1):
        run = 0  # words in a row equal to the word p before
        for i in range(p, n):
            if toks[i] != toks[i - p]:
                run = 0
                continue
            run += 1
            repeats = (run + p) // p
            if repeats < LONG_REPEATS:
                continue
            start = i - run - p + 1
            span = words[i][1] - words[start][0]
            unit = span / repeats
            if span >= MIN_CHARS and (repeats >= MIN_REPEATS or unit >= LONG_UNIT_CHARS):
                if best is None or words[start][0] < best:
                    best = words[start][0]
                break
    return best


def _too_many_stamps(text: str) -> int | None:
    stamps = [m.start() for m in _STAMP.finditer(text)]
    return stamps[MAX_STAMPS] if len(stamps) > MAX_STAMPS else None


def before_loop(text: str) -> str:
    """What the model wrote before it fell into its first loop (the whole text if there is none)."""
    start = find_loop(text)
    return text if start is None else text[:start].rstrip()
