"""Document types (Dokumentart): what a document *is* – Brandschutzkonzept, Baugenehmigung, Prüfbericht, …

People search for "das Brandschutzkonzept" whatever the file is called. So every document gets a type, taken
mainly from its content (the title area of the first page: the first lines and headings), the file name only
as a weak hint:

* :func:`detect` – cheap and deterministic: the first lines are read like a person reads a title page. A line
  that *starts* with a type word ("Brandschutzkonzept", "Brandschutztechnische Stellungnahme", "Prüfbericht
  Nr. 1 zum Brandschutznachweis", "Entwurf des Brandschutzkonzepts") names the type; a word in running text
  ("gemäß Brandschutzkonzept …") does not. The list :data:`SEED` is a starting point, not a closed set: a
  title word ending in a known head ("…berechnung", "…protokoll", "…nachweis") is a type of its own
  ("Löschwasserberechnung"), stored as free text with a folded key.
* :class:`DokumentartWorker` – in the app, a thread of its own: it types the documents indexed before this
  existed (backfill, with progress on the status page) and asks the language model about the documents whose
  type stayed unclear (background, one call at a time, never while a question runs, not with the fake AI –
  the fake would have to invent). The model answers JSON; "unklar" is a valid answer and keeps the weak
  guess.
* :func:`classify_written` – called whenever the sections of a document are written (documents.write_chunks:
  new files, files read anew, attachments, the Bildauswertung replacing the text): a changed beginning of the
  text is typed anew, an unchanged one keeps its type (also one given by the model).

The search (search.py) recognises a type named in a query (:func:`query_types`: inflections, plurals,
compounds, aliases, typos, families such as "Protokolle") and ranks the documents of that type first.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache

from .pause import PAUSE
from .textutil import STOPWORDS, Synonyms, fold, stem

log = logging.getLogger("projektakte.dokumentarten")

HEAD_CHARS = 2000  # beginning of the text that decides the type (title area / first page)
AI_CHARS = 1500  # … of which the language model sees this much
CLEAR = 0.6  # confidence from which a type counts as clear (below: the model is asked, if available)
MIN_RANKED = 0.4  # types with less confidence are shown but never ranked first
MAX_LINES = 25  # lines of the title area looked at
MAX_TITLE_LINE = 160  # longer lines are running text

# The seed list: display name, aliases (folded, written together), related types (ranked right after the
# type itself – "named differently": a Brandschutznachweis is what other offices call the concept).
SEED: tuple[tuple[str, str, str], ...] = (
    ("Brandschutzkonzept", "bskonzept brandschutzkonzeption brandschutzgesamtkonzept", "Brandschutznachweis"),
    ("Brandschutznachweis", "bsnachweis bsn", "Brandschutzkonzept"),
    ("Baugenehmigung", "baugenehmigungsbescheid genehmigungsbescheid bauschein", "Bescheid"),
    ("Bescheid", "", "Baugenehmigung"),
    ("Bauantrag", "", ""),
    ("Prüfbericht", "prufungsbericht", "Prüfbescheinigung"),
    ("Prüfbescheinigung", "", "Prüfbericht"),
    ("Bericht", "", ""),
    ("Stellungnahme", "", ""),
    ("Gutachten", "brandschutzgutachten sachverstandigengutachten", ""),
    ("Plan", "zeichnung", ""),
    ("Grundriss", "grundrissplan", ""),
    ("Flucht- und Rettungsplan", "fluchtundrettungsplan fluchtplan rettungsplan fluchtwegplan rettungswegplan "
                                 "fluchtwegeplan", ""),
    ("Feuerwehrplan", "fwp feuerwehreinsatzplan", ""),
    ("Protokoll", "niederschrift", ""),
    ("Besprechungsprotokoll", "", ""),
    ("Begehungsprotokoll", "", ""),
    ("Abnahmeprotokoll", "", ""),
    ("Angebot", "honorarangebot", ""),
    ("Rechnung", "schlussrechnung abschlagsrechnung teilrechnung honorarrechnung", ""),
    ("Auftrag", "auftragsbestatigung beauftragung", ""),
    ("Leistungsverzeichnis", "lv", ""),
    ("Übereinstimmungserklärung", "ubereinstimmungsbestatigung", ""),
    ("Brandschutzordnung", "bso", ""),
    ("Aktenvermerk", "vermerk aktennotiz", ""),
    ("Schreiben", "brief anschreiben", ""),
    ("Mail", "email", ""),
    ("Foto", "bild fotodokumentation", ""),
)
# Heads of type words: a title word ending in one of them is a type even if the list does not know it
# ("Löschwasserberechnung"), and a query naming the head alone means the whole family ("Protokolle" →
# Besprechungs-, Begehungs-, Abnahmeprotokoll).
HEADS = ("konzept", "nachweis", "bericht", "gutachten", "plan", "protokoll", "bescheinigung", "erklarung",
         "bescheid", "stellungnahme", "angebot", "rechnung", "berechnung", "verzeichnis", "vermerk", "antrag",
         "auftrag", "liste", "beschreibung", "zeichnung", "genehmigung", "konzeption", "analyse", "studie")
# words before the type word that keep the line a title ("Entwurf des Brandschutzkonzepts")
_QUALIFIERS = frozenset("entwurf fortschreibung nachtrag erganzung anlage anhang teil version aktualisierung "
                        "uberarbeitung revision vorabzug kopie abschrift auszug".split())
_LINKS = frozenset("des der dem den zum zur zu fur uber".split())
_COMPANY = re.compile(r"\b(gmbh|mbh|ag|kg|gbr|tel|telefon|fax|www)\b|@")
_ADDRESS = re.compile(r"(ingenieur|buro\b|strasse\b|\bstr\b|postfach)")
_LETTER = re.compile(r"\b(sehr geehrte|mit freundlichen grussen|freundliche grusse)\b")
_SUBJECT = re.compile(r"^(betreff|betr|bezug|gegenstand|titel|dokument|inhalt)\b\.?\s*:?\s*", re.IGNORECASE)
_ADJECTIVE = re.compile(r"^[a-z]{3,}(isch|lich|ig|et|t|nd|en|er|al|iv|ar|ell|ur)(e|er|es|en)$")
_TOKEN = re.compile(r"[^\W_]+(?:-[^\W_]+)*-?", re.UNICODE)
_PHOTO_EXT = frozenset({".jpg", ".jpeg", ".heic", ".webp"})
_MAIL_EXT = frozenset({".eml", ".msg"})

# methods stored in documents.doc_type_method (German, shown on the document page)
INHALT, DATEINAME, DATEITYP, KI = "inhalt", "dateiname", "dateityp", "ki"
METHOD_LABELS = {INHALT: "aus dem Inhalt", DATEINAME: "aus dem Dateinamen", DATEITYP: "aus dem Dateityp",
                 KI: "vom Sprachmodell"}
# documents.doc_type_ai: the model is to be asked ('offen'), has answered ('fertig') or found it unclear
AI_OPEN, AI_DONE, AI_UNCLEAR = "offen", "fertig", "unklar"


def key_of(name: str) -> str:
    """The folded key of a type name ("Flucht- und Rettungsplan" → "fluchtundrettungsplan")."""
    return re.sub(r"[^a-z0-9]", "", fold(name))


@dataclass(frozen=True)
class DocType:
    name: str
    key: str
    aliases: tuple[str, ...] = ()
    related: tuple[str, ...] = ()  # keys

    @property
    def stems(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(stem(k) for k in (self.key, *self.aliases)))


@lru_cache(maxsize=1)
def seed_types() -> dict[str, DocType]:
    return {key_of(n): DocType(n, key_of(n), tuple(a.split()), tuple(key_of(r) for r in rel.split(",") if r))
            for n, a, rel in SEED}


@lru_cache(maxsize=1)
def _lexicon() -> dict[str, str]:
    """stem of every seed key and alias → type key."""
    out: dict[str, str] = {}
    for t in seed_types().values():
        for s in t.stems:
            out.setdefault(s, t.key)
    return out


def _head_of(word: str) -> str | None:
    for h in sorted(HEADS, key=len, reverse=True):
        if word.endswith(h) or stem(word).endswith(stem(h)):
            return h
    return None


def normalize(name: str) -> tuple[str, str] | None:
    """(display name, key) of a type named in free text – a seed type under its own name, any other as
    written (singular, first letter capital). None for something that is no plausible type name."""
    name = re.sub(r"\s+", " ", (name or "").strip().strip(".:;,\"'„“"))
    if not name or len(name) > 60 or len(name.split()) > 5 or not re.search(r"[^\W\d_]{3}", name):
        return None
    if re.search(r"[^\w\s\-/&.äöüÄÖÜß]", name):
        return None
    key = key_of(name)
    seed = _match_seed(key)
    if seed:
        return seed.name, seed.key
    # a plural of a head ("Löschwasserberechnungen") is stored in the singular
    folded = fold(name)
    for h in sorted(HEADS, key=len, reverse=True):
        for suffix in ("en", "e", "n", "s"):
            if folded.endswith(h + suffix):
                name = name[: len(name) - len(suffix)]
                break
        else:
            continue
        break
    name = name[:1].upper() + name[1:]
    return name, key_of(name)


def _match_seed(word: str) -> DocType | None:
    """The seed type a single folded word names: exactly, as alias or inflected (by stem), or as the end of
    a compound naming a specific type (≥ 8 letters: "Teilbrandschutzkonzept", "Abschlagsrechnung") – unless
    a longer generic head fits better ("Löschwasserberechnung" is no Rechnung)."""
    if not word:
        return None
    lex = _lexicon()
    s = stem(word)
    key = lex.get(word) or lex.get(s)
    if key:
        return seed_types()[key]
    best, best_len = None, 0
    for ls, k in lex.items():
        if len(ls) >= 8 and len(ls) > best_len and s.endswith(ls) and len(s) - len(ls) >= 3:
            best, best_len = k, len(ls)
    if best is None:
        return None
    head = _head_of(word)
    if head and len(head) > best_len:
        return None
    return seed_types()[best]


# --------------------------------------------------------------------------- detection
@dataclass
class Detection:
    name: str | None = None
    key: str | None = None
    confidence: float = 0.0
    method: str = INHALT
    title: str | None = None  # the line that named the type

    @property
    def clear(self) -> bool:
        return self.name is not None and self.confidence >= CLEAR


def _clean_line(line: str) -> str:
    line = line.strip().strip("|").strip()
    line = re.sub(r"^#+\s*", "", line)
    line = re.sub(r"^(?:[-*•]\s+|\d+(?:\.\d+)*\.?\s+)", "", line)
    return line.strip()


def _title_word(line: str) -> tuple[str, str] | None:
    """(original word, folded word) of the type word a title line starts with – after adjectives
    ("Brandschutztechnische Stellungnahme") and qualifiers ("Entwurf des …"); joins "BS-Konzept" and
    "Flucht- und Rettungsplan"."""
    tokens = _TOKEN.findall(line)[:8]
    i = 0
    skipped = 0
    while i < len(tokens) and skipped < 3:
        f = fold(tokens[i]).replace("-", "")
        if f in _QUALIFIERS or (f in _LINKS and i > 0):
            i += 1
            skipped += 1
            continue
        if _ADJECTIVE.match(f) and i + 1 < len(tokens) and tokens[i + 1][:1].isupper() and not _type_word(f):
            i += 1
            skipped += 1
            continue
        break
    if i >= len(tokens):
        return None
    tok = tokens[i]
    if tok.endswith("-") and i + 2 < len(tokens) and fold(tokens[i + 1]) in ("und", "oder"):
        tok = tokens[i + 2]  # "Flucht- und Rettungsplan" → Rettungsplan (an alias of the whole)
    if not tok[:1].isupper() and not tok.isupper():
        return None  # titles start with a noun
    return tok, fold(tok).replace("-", "")


def _type_word(word: str) -> tuple[str, str] | None:
    """(name, key) of the seed type a folded title word names, None otherwise."""
    seed = _match_seed(word)
    return (seed.name, seed.key) if seed else None


def _new_type(original: str, word: str) -> tuple[str, str] | None:
    """A type the seed list does not know: a title word ending in a head ("Löschwasserberechnung")."""
    head = _head_of(word)
    if not head or len(word) < len(head) + 3:  # a bare head is in the seed list or no type
        return None
    return normalize(original)


def _filename_words(filename: str) -> list[str]:
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename or "")
    base = re.sub(r"(?<=[a-zäöü])(?=[A-ZÄÖÜ])", " ", base)  # CamelCase
    raw = [w for w in re.split(r"[\W_]+", base) if w]
    out = [fold(w) for w in raw]
    out += [fold(a + b) for a, b in zip(raw, raw[1:])]  # "BS-Konzept", "BS_Nachweis"
    return out


def _from_filename(filename: str) -> tuple[str, str] | None:
    for w in _filename_words(filename):
        if w == "bsk":  # in file names nearly always the concept (in the text: Brandschutzklappe)
            return "Brandschutzkonzept", "brandschutzkonzept"
        if len(w) < 2 or w.isdigit() or w in STOPWORDS:
            continue
        seed = _match_seed(w)
        if seed and (len(w) >= 4 or w in seed.aliases):
            return seed.name, seed.key
    return None


def detect(text: str, filename: str = "", ext: str = "") -> Detection:
    """The type of a document from the beginning of its text (``HEAD_CHARS``) and its file name."""
    ext = (ext or "").lower()
    head = (text or "")[:HEAD_CHARS]
    by_name = _from_filename(filename)
    if ext in _MAIL_EXT:
        return Detection("Mail", "mail", 0.9, DATEITYP)
    found: Detection | None = None
    subject: Detection | None = None
    for n, raw in enumerate([ln for ln in head.split("\n") if ln.strip()][:MAX_LINES]):
        line = _clean_line(raw)
        if not line or len(line) > MAX_TITLE_LINE:
            continue
        low = fold(line)
        if _COMPANY.search(low):
            continue
        candidates = [(line, 0.0, False)]
        m = _SUBJECT.match(line)
        if m:
            candidates = [(line[m.end():], 0.1, True)]
        elif ": " in line[:40]:  # "Fake-Transkription (…): Fluchtwegplan …", "Projekt: …"
            candidates.append((line.split(": ", 1)[1], 0.1, False))
        for cand, penalty, is_subject in candidates:
            tw = _title_word(cand)
            if not tw:
                continue
            original, word = tw
            hit = _type_word(word)
            if hit:
                name, key = hit
                conf = 0.9 if n < 8 else 0.8
            else:
                new = _new_type(original, word)
                if not new or _ADDRESS.search(low):  # "Ingenieurbüro für Brandschutzplanung"
                    continue
                name, key = new
                conf = 0.7
            conf -= penalty + (0.15 if len(cand) > 100 else 0.0)
            det = Detection(name, key, round(conf, 2), INHALT, cand.strip()[:200])
            if is_subject:
                subject = subject or det
                continue
            found = det
            break
        if found:
            break
    if found is None and _LETTER.search(fold(head)):
        found = Detection("Schreiben", "schreiben", 0.7 if subject else 0.6, INHALT, subject.title if subject else None)
    if found is None and subject is not None:
        found = subject
    if found is not None:
        if by_name and by_name[1] == found.key:
            found.confidence = min(0.95, round(found.confidence + 0.05, 2))
        return found
    if by_name:
        return Detection(by_name[0], by_name[1], 0.45, DATEINAME)
    if ext in _PHOTO_EXT and len(head.strip()) < 200:
        return Detection("Foto", "foto", 0.5, DATEITYP)
    return Detection()


# --------------------------------------------------------------------------- storage
def text_hash(text: str, filename: str) -> str:
    return hashlib.sha1(f"{filename}\n{(text or '')[:HEAD_CHARS]}".encode("utf-8", "replace")).hexdigest()[:16]


def head_text(chunks) -> str:
    """The beginning of a document's text from its sections (in order): the text without the overlap
    the sections share is not needed – the first ``HEAD_CHARS`` decide."""
    out, size = [], 0
    for c in chunks:
        t = getattr(c, "text", None) if not isinstance(c, str) else c
        if t is None:
            t = c["text"]
        out.append(t)
        size += len(t)
        if size >= HEAD_CHARS:
            break
    return "\n".join(out)[:HEAD_CHARS]


def store(conn, doc_id: int, det: Detection, digest: str, *, ask_ai: bool) -> None:
    conn.execute(
        "UPDATE documents SET doc_type = ?, doc_type_key = ?, doc_type_method = ?, doc_type_confidence = ?, "
        "doc_type_title = ?, doc_type_hash = ?, doc_type_ai = ? WHERE id = ?",
        (det.name, det.key, det.method if det.name else None, det.confidence if det.name else None, det.title,
         digest, AI_OPEN if ask_ai else None, doc_id))
    KNOWN.invalidate()


def wants_ai(det: Detection, text: str) -> bool:
    """Unclear and with enough text for the model to judge (a photo without text is a photo)."""
    return not det.clear and len((text or "").strip()) >= 40


def classify_written(conn, doc_id: int, chunks, filename: str) -> None:
    """Type a document whose sections were just written – only if the beginning of its text changed (the
    type, also one given by the model, stays otherwise). Fast: pure string work and one UPDATE."""
    text = head_text(chunks)
    digest = text_hash(text, filename)
    row = conn.execute("SELECT doc_type_hash FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None or row[0] == digest:
        return
    ext = re.search(r"\.[^.]+$", filename or "")
    det = detect(text, filename, ext.group(0) if ext else "")
    store(conn, doc_id, det, digest, ask_ai=wants_ai(det, text))
    if wants_ai(det, text):
        WAITING.set()


def _document_head(conn, doc_id: int) -> str:
    rows = conn.execute("SELECT text FROM chunks WHERE doc_id = ? ORDER BY ord LIMIT 8", (doc_id,)).fetchall()
    return head_text([r[0] for r in rows])


def backfill(conn, limit: int = 200) -> int:
    """Type up to ``limit`` documents that have no type yet (indexed before types existed). Returns the
    number typed; the caller commits."""
    rows = conn.execute("SELECT id, filename, ext FROM documents WHERE doc_type_hash IS NULL ORDER BY id LIMIT ?",
                        (limit,)).fetchall()
    for r in rows:
        text = _document_head(conn, r[0])
        det = detect(text, r[1], r[2])
        store(conn, r[0], det, text_hash(text, r[1]), ask_ai=wants_ai(det, text))
    return len(rows)


def backfill_all(conn) -> int:
    """All documents without a type (CLI pass without the app's worker), in short transactions."""
    total = 0
    while True:
        n = backfill(conn)
        conn.commit()
        total += n
        if n == 0:
            return total


def counts(conn) -> dict:
    """For the status page: documents still without a type, waiting for the model, typed."""
    todo = conn.execute("SELECT COUNT(*) FROM documents WHERE doc_type_hash IS NULL").fetchone()[0]
    ai = conn.execute("SELECT COUNT(*) FROM documents WHERE doc_type_ai = ?", (AI_OPEN,)).fetchone()[0]
    return {"todo": todo, "ai_open": ai}


# --------------------------------------------------------------------------- known types
class KnownTypes:
    """The seed types plus every type stored in the database (cached for a few seconds)."""

    TTL = 30.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, DocType]]] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def get(self, conn) -> dict[str, DocType]:
        path = _db_path(conn)
        with self._lock:
            hit = self._cache.get(path)
            if hit and time.monotonic() - hit[0] < self.TTL:
                return hit[1]
        types = dict(seed_types())
        try:
            for name, key in conn.execute(
                    "SELECT doc_type, doc_type_key FROM documents WHERE doc_type_key IS NOT NULL "
                    "GROUP BY doc_type_key"):
                if key and key not in types:
                    types[key] = DocType(name, key)
        except Exception:  # noqa: BLE001 – an old database without the columns: seed types only
            pass
        with self._lock:
            self._cache[path] = (time.monotonic(), types)
        return types


def _db_path(conn) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row[2] or ":memory:")
    except Exception:  # noqa: BLE001
        return ":memory:"


KNOWN = KnownTypes()


# --------------------------------------------------------------------------- queries
@dataclass
class TypeQuery:
    """The document types a query names: ``exact`` (named), ``related`` (closely related, ranked after),
    the names for the search page, and ``rest`` – the query without the type words."""

    exact: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    rest: str = ""

    def __bool__(self) -> bool:
        return bool(self.exact)


def osa_distance(a: str, b: str, limit: int = 3) -> int:
    """Optimal string alignment distance (Levenshtein plus swapped neighbours), cut off above ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def typo_limit(word: str) -> int:
    """Edits allowed for a misspelt word: none below 6 letters, 1 up to 8, 2 from 9 on."""
    n = len(word)
    return 0 if n < 6 else (1 if n < 9 else 2)


def _word_matches(word: str, t: DocType) -> int:
    """How a folded query word names type ``t``: 3 exactly (inflected, alias, misspelt) or as the family head
    ("Protokolle" for a Besprechungsprotokoll), 2 as a compound ending in it ("Kitabrandschutzkonzept"), 0 not."""
    s = stem(word)
    stems = t.stems
    if word in (t.key, *t.aliases) or s in stems:
        return 3
    if len(s) >= 6 and any(len(k) >= 8 and s.endswith(k) for k in stems):
        return 2
    if len(s) >= 4 and s in _family_stems() and stem(t.key).endswith(s):
        return 3  # the family: "Protokolle" names Besprechungs-, Begehungs- and Abnahmeprotokolle as well
    limit = typo_limit(word)
    if limit and len(t.key) >= 8:
        for k in (t.key, *[a for a in t.aliases if len(a) >= 8]):
            if osa_distance(s, stem(k), limit) <= limit or osa_distance(word, k, limit) <= limit:
                return 3
    return 0


@lru_cache(maxsize=1)
def _family_stems() -> frozenset[str]:
    """Heads that name a family in a query: the seed types that are heads themselves ("Protokolle", "Pläne",
    "Berichte") and "Konzept" – not every head ("Nachweis der Feuerwiderstandsdauer" is no document type)."""
    return frozenset(stem(key_of(n)) for n, _, _ in SEED if key_of(n) in HEADS) | {stem("konzept")}


# words a query uses for other things: "Mail" (mails are found as mails), "schreiben"/"bild" as a verb or
# in lower case ("Wer hat … geschrieben", "bild" in a file name) – a type only when written as a noun
_NOT_IN_QUERIES = frozenset({"mail", "email"})
_NOUN_ONLY = frozenset({"schreiben", "bild", "brief"})


def _query_words(query: str) -> list[tuple[str, str]]:
    """(original, folded) words of a query; "BS-Konzept" also as one word."""
    out = []
    for tok in _TOKEN.findall(query or ""):
        tok = tok.rstrip("-")
        f = fold(tok)
        if "-" in f:
            out.append((tok, f.replace("-", "")))
        else:
            out.append((tok, f))
    return out


def query_types(conn, query: str, synonyms: Synonyms | None = None) -> TypeQuery:
    """The document types a query names (see :class:`TypeQuery`). Words of two letters or less only as an
    exact alias ("LV", "BSN"); the equivalence groups of the synonyms (SYNONYMS_FILE) count as aliases."""
    types = KNOWN.get(conn) if conn is not None else seed_types()
    tq = TypeQuery()
    rest: list[str] = []
    seen: set[str] = set()
    for original, word in _query_words(query):
        if word in _NOT_IN_QUERIES or (word in _NOUN_ONLY and not original[:1].isupper()):
            rest.append(original)
            continue
        if word in STOPWORDS or (len(word) < 4 and not any(word in t.aliases for t in types.values())):
            rest.append(original)
            continue
        candidates = [word, *_synonym_words(synonyms, word)]
        best: dict[str, int] = {}
        for t in types.values():
            for c in candidates:
                score = _word_matches(c, t)
                if score > best.get(t.key, 0):
                    best[t.key] = score
        if not best:
            rest.append(original)
            continue
        top = max(best.values())
        for key, score in best.items():
            if score == top and key not in seen:
                seen.add(key)
                tq.exact.append(key)
                tq.names.append(types[key].name)
    for key in tq.exact:
        t = types.get(key)
        for r in (t.related if t else ()):
            if r not in seen:
                seen.add(r)
                tq.related.append(r)
    tq.rest = " ".join(rest)
    return tq


def _synonym_words(synonyms: Synonyms | None, word: str) -> list[str]:
    """Single words of the equivalence groups ("A = B") a word belongs to – not the one-way expansions."""
    if synonyms is None:
        return []
    s = stem(word)
    out = []
    for group in synonyms.groups:
        if any(len(k) == 1 and (k[0] == word or stem(k[0]) == s) for k in group):
            out.extend(k[0] for k in group if len(k) == 1 and k[0] != word)
    return out


def type_documents(conn, keys: list[str], *, projects: list[str] | None = None,
                   limit: int = 30) -> list[tuple[int, int, str, float]]:
    """Documents of the types ``keys`` in the scope, newest first – those whose type is only a weak guess
    (below MIN_RANKED) are left out: (doc id, chunk id of its beginning, type key, confidence). Only
    documents still searchable (a full-text row) count; the chunk is the first one of the document."""
    if not keys:
        return []
    marks = ",".join("?" * len(keys))
    scope_sql = f" AND c.project IN ({','.join('?' * len(projects))})" if projects else ""
    sql = (f"SELECT d.id, d.doc_type_key, d.doc_type_confidence, "
           f"(SELECT c.id FROM chunks c JOIN chunks_fts f ON f.rowid = c.id WHERE c.doc_id = d.id{scope_sql} "
           f" ORDER BY c.ord LIMIT 1) AS first "
           f"FROM documents d WHERE d.doc_type_key IN ({marks}) AND d.doc_type_confidence >= ? "
           f"ORDER BY d.doc_date IS NULL, d.doc_date DESC, d.id DESC")
    params = list(keys) + [MIN_RANKED]
    if projects:
        # the scope placeholders belong to the subquery, which comes first in the statement
        params = list(projects) + list(keys) + [MIN_RANKED]
    out = []
    try:
        for r in conn.execute(sql, params):
            if r[3] is not None:
                out.append((r[0], r[3], r[1], r[2] or 0.0))
                if len(out) >= limit:
                    break
    except Exception as exc:  # noqa: BLE001 – a database without the columns: no type ranking
        log.debug("Dokumentarten nicht abfragbar: %s", exc)
        return []
    return out


# --------------------------------------------------------------------------- language model
AI_SYSTEM = (
    "Du ordnest Dokumente eines Brandschutz-Ingenieurbüros einer Dokumentart zu. Antworte nur mit JSON in der "
    "Form {\"dokumentart\": \"…\", \"sicher\": true}. Die Dokumentart ist eine kurze deutsche Bezeichnung im "
    "Singular, zum Beispiel: " + ", ".join(n for n, _, _ in SEED if n not in ("Plan", "Bericht")) +
    ". Andere Arten sind erlaubt (etwa Löschwasserberechnung). Entscheide nach Titel und Inhalt, nicht nach "
    "Wörtern, die nur erwähnt werden (ein Schreiben über ein Brandschutzkonzept ist ein Schreiben). Ist die Art "
    "nicht sicher erkennbar, antworte {\"dokumentart\": null, \"sicher\": false}. Erfinde nichts.")


def ai_prompt(text: str, filename: str) -> list[dict]:
    return [{"role": "system", "content": AI_SYSTEM},
            {"role": "user", "content": f"Dateiname: {filename}\n\nAnfang des Dokuments:\n{(text or '')[:AI_CHARS]}"}]


def parse_ai_answer(answer: str) -> tuple[str, str] | None:
    """(name, key) from the model's JSON answer; None for "unklar", an unsure answer or anything else."""
    m = re.search(r"\{.*?\}", answer or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("sicher") is not True:
        return None
    name = data.get("dokumentart")
    if not isinstance(name, str) or fold(name).strip() in ("unklar", "unbekannt", "sonstiges", "keine", ""):
        return None
    return normalize(name)


def classify_with_ai(conn, llm, doc_id: int, *, timeout: float | None = None) -> str:
    """Ask the model about one document. Returns the state stored (AI_DONE or AI_UNCLEAR); raises the
    client's error when the service fails (the document stays 'offen')."""
    row = conn.execute("SELECT filename, doc_type_hash FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        return AI_UNCLEAR
    text = _document_head(conn, doc_id)
    res = llm.chat(ai_prompt(text, row[0]), max_tokens=80, temperature=0.0, timeout=timeout)
    parsed = parse_ai_answer(getattr(res, "text", "") or "")
    # the text may have changed while the model read it: then the next classification decides
    current = conn.execute("SELECT doc_type_hash FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if current is None or current[0] != row[1]:
        return AI_UNCLEAR
    if parsed is None:
        conn.execute("UPDATE documents SET doc_type_ai = ? WHERE id = ?", (AI_UNCLEAR, doc_id))
        return AI_UNCLEAR
    name, key = parsed
    conf = 0.8 if key in seed_types() else 0.65
    conn.execute("UPDATE documents SET doc_type = ?, doc_type_key = ?, doc_type_method = ?, "
                 "doc_type_confidence = ?, doc_type_ai = ? WHERE id = ?", (name, key, KI, conf, AI_DONE, doc_id))
    KNOWN.invalidate()
    return AI_DONE


# --------------------------------------------------------------------------- background worker
WAITING = threading.Event()  # set when a document waits for typing (classify_written)


class QuestionGate:
    """Questions in progress (ask.py): the model is not asked about document types meanwhile."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._n = 0
        self._idle = threading.Event()
        self._idle.set()

    def __enter__(self):
        with self._lock:
            self._n += 1
            self._idle.clear()
        return self

    def __exit__(self, *exc) -> None:
        with self._lock:
            self._n = max(0, self._n - 1)
            if self._n == 0:
                self._idle.set()

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    def wait_idle(self, timeout: float) -> bool:
        return self._idle.wait(timeout)


QUESTIONS = QuestionGate()


class Live:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.enabled = False
            self.ai = False
            self.busy = False
            self.typed = 0  # documents typed (backfill) since the app started
            self.asked = 0  # model answers since the app started
            self.error: str | None = None

    def update(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def add(self, name: str, n: int) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def snapshot(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "ai": self.ai, "busy": self.busy, "typed": self.typed,
                    "asked": self.asked, "error": self.error}


LIVE = Live()


def ai_wanted(settings, llm) -> bool:
    """DOKUMENTART_KI: 'auto' (default) asks the model with real AI services, never with the fake one (it
    would have to invent); '1' always (if there is a model), '0' never."""
    mode = str(getattr(settings, "dokumentart_ki", "auto") or "auto").lower()
    if llm is None or mode in ("0", "aus", "off", "false", "nein"):
        return False
    if mode in ("1", "an", "on", "true", "ja"):
        return True
    return getattr(settings, "ai_mode", "") != "fake"


class DokumentartWorker:
    """Backfill and model classification in a thread of its own (see the module docstring)."""

    IDLE_SECONDS = 60.0
    ERROR_SECONDS = 120.0
    BATCH = 200
    QUESTION_WAIT = 2.0  # seconds without a question before the model is asked again

    def __init__(self, settings, llm, *, live: Live | None = None):
        self.settings, self.llm = settings, llm
        self.live = live or LIVE
        self.use_ai = ai_wanted(settings, llm)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.live.reset()
        self.live.update(enabled=True, ai=self.use_ai)
        self._thread = threading.Thread(target=self._loop, name="dokumentarten", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        WAITING.set()
        if self._thread:
            self._thread.join(timeout)
        self.live.reset()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        from . import db

        conn = None
        try:
            while not self._stop.is_set():
                PAUSE.wait(self._stop)
                if self._stop.is_set():
                    break
                WAITING.clear()
                pause = 0.0
                try:
                    if conn is None:
                        db.init_db(self.settings.db_path)
                        conn = db.connect(self.settings.db_path)
                    worked = self.run_round(conn)
                    self.live.update(error=None)
                    pause = 0.0 if worked else self.IDLE_SECONDS
                except Exception as exc:  # noqa: BLE001 – the thread must survive any single failure
                    log.warning("Dokumentarten im Hintergrund: %s – neuer Versuch in %.0f s", exc, self.ERROR_SECONDS)
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.live.update(error=self.last_error, busy=False)
                    if conn is not None:
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                    pause = self.ERROR_SECONDS
                if pause and not self._stop.is_set():
                    self.live.update(busy=False)
                    WAITING.wait(pause)
        finally:
            if conn is not None:
                conn.close()

    def run_round(self, conn) -> bool:
        """Backfill one batch, else ask the model about one document. False: nothing to do."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            n = backfill(conn, self.BATCH)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if n:
            self.live.update(busy=True)
            self.live.add("typed", n)
            return True
        if not self.use_ai:
            return False
        row = conn.execute("SELECT id FROM documents WHERE doc_type_ai = ? ORDER BY doc_date IS NULL, doc_date DESC "
                           "LIMIT 1", (AI_OPEN,)).fetchone()
        if row is None:
            return False
        # low priority: never beside a question (the model's places belong to it and the Bildauswertung)
        while QUESTIONS.busy and not self._stop.is_set():
            QUESTIONS.wait_idle(self.QUESTION_WAIT)
        if self._stop.is_set():
            return False
        self.live.update(busy=True)
        state = classify_with_ai(conn, self.llm, row[0])
        conn.commit()
        if state == AI_UNCLEAR:
            conn.execute("UPDATE documents SET doc_type_ai = ? WHERE id = ? AND doc_type_ai = ?",
                         (AI_UNCLEAR, row[0], AI_OPEN))
            conn.commit()
        self.live.add("asked", 1)
        return True
