"""Small German text helpers shared by search, assignment and the fake AI services."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

STOPWORDS = frozenset(
    """
    a ab aber alle allem allen aller alles als also am an ander andere anderen auch auf aus bei beim
    bin bis bitte bzw da dabei damit dann das dass dein deine dem den denn der deren des dessen die
    dies diese diesem diesen dieser dieses doch dort du durch ein eine einem einen einer eines einige
    er es etwa etwas euch euer fur gegen gibt hab habe haben hat hatte hatten hier hin hinter ich ihm
    ihn ihnen ihr ihre im in ins ist ja jede jedem jeden jeder jedes jetzt kann kein keine konnen
    konnte machen man mehr mein meine mit muss mussen nach nicht nichts noch nun nur ob oder ohne sehr
    sein seine sich sie sind so soll sollen sondern sowie uber um und uns unser unter vom von vor
    wann war waren warum was weil welche welchem welchen welcher welches wenn wer werden wie wieder
    will wir wird wo wurde wurden zu zum zur zwar zwischen
    gilt gelten steht stehen gibt geben laut bitte sowie hallo guten tag viele grusse freundlichen
    mfg sehr geehrte geehrter damen herren liebe lieber
    """.split()
)

# Words that only shape a question and carry no content (used by the fake LLM and "not found").
QUESTION_WORDS = frozenset(
    "welche welcher welches welchen wer wie was wann wo warum wieso weshalb gibt es ist sind hat "
    "haben wurde wurden werden kann muss mussen soll sollen darf durfen".split()
)


def fold(text: str) -> str:
    """Lowercase and strip diacritics (ä→a, ß→ss) – matches FTS5 'remove_diacritics'."""
    text = text.lower().replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return _WORD.findall(fold(text))


_SUFFIXES = ("ungen", "ern", "em", "en", "er", "es", "e", "n", "s")


def stem(word: str) -> str:
    """Very light German stemmer: enough to match Rettungsweg/Rettungswege/Rettungswegs."""
    if word.isdigit():
        return word
    for suf in _SUFFIXES:
        if len(word) - len(suf) >= 4 and word.endswith(suf):
            if suf == "ungen":
                return word[: -len(suf)] + "ung"
            return word[: -len(suf)]
    return word


def content_terms(text: str, extra_stop: frozenset[str] = frozenset()) -> list[str]:
    """Stemmed content words without stop words, in order, duplicates removed."""
    seen: dict[str, None] = {}
    for w in words(text):
        if len(w) < 2 or w in STOPWORDS or w in extra_stop:
            continue
        seen.setdefault(stem(w), None)
    return list(seen)


def term_in(term: str, doc_terms: set[str], doc_text_folded: str | None = None) -> bool:
    """Does a (stemmed) query term occur in a document?

    Accepts exact stems, prefixes (Rauchableitung → Rauchableitungsöffnung) and, for longer words,
    the reverse direction (Abweichungsantrag ↔ Abweichung) – German compounds are common.
    """
    if term in doc_terms:
        return True
    if len(term) >= 5:
        for t in doc_terms:
            if t.startswith(term) or (len(t) >= 6 and term.startswith(t)):
                return True
    return False


_DATE_DE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def find_date(text: str) -> date | None:
    """First plausible date in a text (German 12.03.2024 or ISO 2024-03-12)."""
    candidates = []
    for m in _DATE_ISO.finditer(text):
        candidates.append((m.start(), int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in _DATE_DE.finditer(text):
        candidates.append((m.start(), int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for _, y, mo, d in sorted(candidates):
        try:
            if 1990 <= y <= 2100:
                return date(y, mo, d)
        except ValueError:
            continue
    return None
