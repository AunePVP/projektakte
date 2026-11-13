"""Repetition loops in model answers: the model writes the same line or the same few words again and again
until it runs out of tokens (small vision models transcribing forms, tables and plans fall into this).

:func:`find_loop` finds the place where such a loop starts. A stretch counts as a loop if a sequence of 1 to
MAX_PERIOD words follows itself at least MIN_REPEATS times in a row and the stretch has at least MIN_CHARS
characters – a real page repeats a table head or "[handschriftlich: ✓]" a few times, not six times in a row
over hundreds of characters. Words are compared exactly (case aside): rows that differ in a number are no loop.
"""
from __future__ import annotations

import re

MIN_REPEATS = 6
MIN_CHARS = 400
MAX_PERIOD = 80  # words in the repeated unit (a long table row or a short paragraph)
STREAM_WINDOW = 600  # words looked at while an answer streams in (the newest ones)

_WORD = re.compile(r"\S+")


def find_loop(text: str, *, window: int | None = None) -> int | None:
    """Character position where the first repetition loop of ``text`` starts, or None.

    ``window``: only the last that many words are looked at (cheap enough to run while an answer streams
    in; a loop always grows at the end)."""
    if not text:
        return None
    words = [(m.start(), m.end(), m.group().lower()) for m in _WORD.finditer(text)]
    if window is not None and len(words) > window:
        words = words[-window:]
    n = len(words)
    toks = [w[2] for w in words]
    best: int | None = None
    for p in range(1, min(MAX_PERIOD, n // MIN_REPEATS) + 1):
        run = 0  # words in a row equal to the word p before
        for i in range(p, n):
            if toks[i] != toks[i - p]:
                run = 0
                continue
            run += 1
            if run + p < p * MIN_REPEATS:
                continue
            start = i - run - p + 1
            if words[i][1] - words[start][0] >= MIN_CHARS:
                if best is None or words[start][0] < best:
                    best = words[start][0]
                break
    return best


def before_loop(text: str) -> str:
    """What the model wrote before it fell into its first loop (the whole text if there is none)."""
    start = find_loop(text)
    return text if start is None else text[:start].rstrip()
