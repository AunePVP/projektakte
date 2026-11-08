"""Small German text helpers shared by search, assignment and the fake AI services."""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("projektakte.textutil")

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

# Filler words of typed questions ("In welchen Projekten ist … vorgesehen?"): light verbs, generic
# nouns and forms of address. They carry no topic, so question-side matching (full-text expansion,
# fake reranker and fake chat) ignores them. Not used for mails/documents (assignment).
QUERY_FILLER = frozenset(
    """
    projekt projekte projekten projekts projektes bauvorhaben bauvorhabens vorhaben objekt objekte
    objekten unterlagen dokument dokumente dokumenten quelle quellen akte information informationen
    vorgesehen vorsehen sieht geplant planen plant gefordert fordert fordern verlangt gibt geben
    gegeben steht stehen genannt erwahnt beschrieben drin denn eigentlich genau jeweils bitte uns
    braucht brauchen benotigt benotigen notig bekommt bekommen erhalt erhalten kommt kommen bauen baut
    gebaut errichtet errichten
    geht gehen sagt sagen gesagt wissen weiss gilt gelten funktionieren funktioniert durchhalten
    beachten beachtet beachtung tun macht machen gemacht lassen worden wird werden sein seien hatte
    art sorte typ frau herr herrn dr dipl ing jemand irgendwas irgendwelche
    ihren ihrem ihrer ihres seinen seinem seiner seines unsere unseren unserem unserer unseres meinen
    meinem meiner meines eure euren eurem
    """.split()
)

# Words asking for the latest state ("Was ist der letzte Stand …?", "Wurde … schon …?").
TEMPORAL_WORDS = frozenset(
    """
    letzt letzte letzter letzten letztes zuletzt aktuell aktuelle aktueller aktuellen aktuelles
    neueste neuester neuesten neuestes neuste neusten neuigkeit neuigkeiten stand stande inzwischen
    mittlerweile schon bereits derzeit momentan gerade jetzt update
    """.split()
)


def fold(text: str) -> str:
    """Lowercase and strip diacritics (ä→a, ß→ss) – matches FTS5 'remove_diacritics'."""
    text = text.lower().replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return _WORD.findall(fold(text))


_SUFFIXES = ("ungen", "ern", "em", "en", "er", "es", "e", "n", "s")
# separable verbs with "zu" infix: abzunehmen → abnehmen, aufzuschalten → aufschalten
_ZU_INFIX = re.compile(r"^(ab|an|auf|aus|bei|ein|fest|frei|her|hin|mit|nach|vor|weg|zuruck|zusammen)zu(\w{3,}n)$")


def stem(word: str) -> str:
    """Very light German stemmer: enough to match Rettungsweg/Rettungswege/Rettungswegs."""
    if word.isdigit():
        return word
    m = _ZU_INFIX.match(word)
    if m:
        word = m.group(1) + m.group(2)
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
        if (len(w) < 2 and not w.isdigit()) or w in STOPWORDS or w in extra_stop:
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


# --------------------------------------------------------------------------- dates
try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Berlin")
except Exception:  # noqa: BLE001 – no tz database available: fall back to UTC
    LOCAL_TZ = timezone.utc


def local_date(value: str | None) -> date | None:
    """Calendar date of an ISO date/datetime in the office's time zone (Europe/Berlin).

    Mails are stored in UTC ("2024-06-17T22:30:00+00:00"); shown as a date they must use the local
    day (18.06.2024). Plain dates ("2024-06-18") are returned unchanged.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(LOCAL_TZ)
    return dt.date()


def de_date(value: str | None, default: str = "?") -> str:
    """German date (TT.MM.JJJJ) of an ISO date/datetime, in local time."""
    d = local_date(value)
    return d.strftime("%d.%m.%Y") if d else default


# --------------------------------------------------------------------------- word variants
# Separable verb prefixes: "abgenommen" → "ab" + "genommen", "einzubauen" → "ein" + "zu" + "bauen".
_SEP_PREFIX = ("zuruck", "zusammen", "durch", "fest", "frei", "nach", "uber", "auf", "aus", "bei", "ein",
               "her", "hin", "mit", "vor", "weg", "ab", "an", "um", "zu")
_PARTICIPLE = re.compile(r"^(%s)?ge(\w{2,}?)(t|et|en)$" % "|".join(_SEP_PREFIX))
_ZU_ANY = re.compile(r"^(%s)zu(\w{3,}en)$" % "|".join(_SEP_PREFIX))
_WEAK_PARTICIPLE = re.compile(r"^((be|ge|er|ver|zer|ent|emp)\w{3,}t|\w{3,}(iert|igt))$")
# Ablaut series of strong verbs common in project files (Abnahme/abnehmen/abgenommen …).
_ABLAUT = (("nehm", "nahm", "nomm", "nimm"), ("schliess", "schloss"), ("zieh", "zog", "zug"),
           ("find", "fand", "fund"), ("steh", "stand"), ("halt", "hielt"), ("geb", "gab"),
           ("schreib", "schrieb"), ("treff", "traf", "troff"))


@lru_cache(maxsize=65536)
def word_variants(word: str) -> frozenset[str]:
    """Stems of a folded word plus base forms of participles and strong-verb forms.

    ``abgenommen`` → {abgenomm, abnomm, abnehm, abnahm}; ``eingebaut`` → {eingebaut, einbau};
    ``Abnahme`` → {abnahm, abnehm, abnomm}. Enough to match questions and texts that use different
    word forms of the same verb or noun.
    """
    return frozenset({stem(word)} | set(verb_bases(word)))


@lru_cache(maxsize=65536)
def verb_bases(word: str) -> tuple[str, ...]:
    """Verb stems hidden in a folded word: participles, zu-infinitives and ablaut forms."""
    bases: set[str] = set()
    m = _PARTICIPLE.match(word)
    if m and len(word) >= 5:
        bases.add((m.group(1) or "") + m.group(2))
    m = _ZU_ANY.match(word)
    if m:
        bases.add(stem(m.group(1) + m.group(2)))
    if _WEAK_PARTICIPLE.match(word):  # genehmigt → genehmig, beantragt → beantrag, installiert → installier
        bases.add(word[:-1])
    plain = {stem(word)} | ({word[:-1]} if word.endswith("t") and len(word) >= 5 else set())  # nimmt → nimm
    for b in list(bases or plain):
        for series in _ABLAUT:  # only as the last morpheme: Abnahme, abgenommen – not Gebäude
            form = next((f for f in series if b.endswith(f)), None)
            if form:
                bases.update(b[: -len(form)] + other for other in series)
    bases.discard(stem(word))
    return tuple(sorted(b for b in bases if len(b) >= 2))


def verbal_noun(word: str) -> str | None:
    """Nominalisation of a verb form: 'prüfen' → 'prufung', 'geübt' → 'ubung', 'evakuieren' →
    'evakuierung'. Only for infinitives and participles."""
    m = _PARTICIPLE.match(word)
    if m and len(word) >= 5:
        if m.group(3) == "en":  # strong verbs change their stem (genommen ≠ Nahme)
            return None
        base = (m.group(1) or "") + m.group(2)
    elif word.endswith("en") and len(word) >= 6:
        base = word[:-2]
    else:
        return None
    if base.endswith(("ung", "e")) or len(base) < 2:
        return None
    return base + "ung"


# --------------------------------------------------------------------------- synonyms
BUILTIN_SYNONYMS = """
# Abkürzungen und Fachsynonyme aus dem Brandschutz. Eine Gruppe je Zeile:
#   "A = B = C"  gleichbedeutend (in beide Richtungen)
#   "A -> B"     A wird zu B erweitert, aber nicht umgekehrt (für mehrdeutige Kürzel)
BMA = Brandmeldeanlage
BMZ = Brandmelderzentrale
RWA = Rauch- und Wärmeabzugsanlage = Rauch- und Wärmeabzug = Rauchabzugsanlage = Rauchabzug = Rauchableitung = Entrauchung = Entrauchungsanlage
SAA = Sprachalarmanlage = ELA
SiBe = Sicherheitsbeleuchtung = Notbeleuchtung = Notlicht
RDA = Druckbelüftungsanlage = Druckbelüftung = Überdruckanlage = Überdruckbelüftung = Rauchschutzdruckanlage
FSA = Feststellanlage
BSK = Brandschutzklappe
RWM = Rauchwarnmelder = Rauchmelder
FSD = Feuerwehrschlüsseldepot
FBF = Feuerwehrbedienfeld
FAT = Feuerwehranzeigetableau
Treppenraum = Treppenhaus
Rettungsweg = Fluchtweg
OG = Obergeschoss
DG = Dachgeschoss
TG = Tiefgarage
GK = Gebäudeklasse
EG -> Erdgeschoss
UG -> Untergeschoss
KG -> Kellergeschoss
PI -> Prüfingenieur
PSV -> Prüfsachverständiger
BSB -> Brandschutzbeauftragter
Feuerwehr = Brandschutzdienststelle
Bauaufsicht = Bauaufsichtsbehörde = Bauamt = Bauordnungsamt = Baubehörde
Drehleiter = Hubrettungsfahrzeug
Bedenken = Einwände = Einwand = Einwendungen
Räumung = Evakuierung
Material = Baustoff
Löschwasserbedarf = Löschwassermenge
Neuigkeiten = Sachstand = aktueller Stand
"""


@dataclass(frozen=True)
class Synonyms:
    """Synonym groups as phrases of folded words: ('rauch', 'warmeabzug') for 'Rauch- und Wärmeabzug'."""

    groups: tuple[tuple[tuple[str, ...], ...], ...] = ()
    one_way: tuple[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]], ...] = ()

    def alternatives(self, term: str) -> list[tuple[str, ...]]:
        """Other phrasings of a stemmed term as tuples of stems (a phrase has several).

        A term matches a group word exactly or as the first part of a compound; then the rest of the
        compound is kept: ``entrauchungsoffnung`` → (``rauchableitung``, ``offnung``), …
        """
        out: list[tuple[str, ...]] = []

        def rest_of(key: tuple[str, ...]) -> str | None:
            """'' for an exact match, the remaining compound part, or None."""
            if len(key) != 1:
                return None
            word = key[0]
            if term == word or term == stem(word):
                return ""
            for c in sorted({word, stem(word)}, key=len, reverse=True):
                if len(c) >= 5 and term.startswith(c) and len(term) - len(c) >= 4:
                    rest = term[len(c):]
                    return rest[1:] if rest.startswith("s") and len(rest) > 4 else rest
            return None

        def stems(key: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(stem(w) for w in key)

        for group in self.groups:
            for key in group:
                rest = rest_of(key)
                if rest is None:
                    continue
                for other in group:
                    if other is key:
                        continue
                    out.append(stems(other) + ((rest,) if rest else ()))
                break
        for key, targets in self.one_way:
            rest = rest_of(key)
            if rest is not None:
                out.extend(stems(t) + ((rest,) if rest else ()) for t in targets)
        return [k for k in dict.fromkeys(out) if k != (term,)]


def _phrase_key(phrase: str) -> tuple[str, ...]:
    return tuple(w for w in words(phrase) if w not in STOPWORDS and (len(w) >= 2 or w.isdigit()))


def parse_synonyms(text: str) -> Synonyms:
    """Parse the synonym format (see BUILTIN_SYNONYMS). Lines without a group are ignored."""
    groups, one_way = [], []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "->" in line:
            left, right = line.split("->", 1)
            key = _phrase_key(left)
            targets = tuple(k for k in (_phrase_key(x) for x in re.split(r"[=,;]", right)) if k)
            if key and targets:
                one_way.append((key, targets))
            continue
        keys = tuple(dict.fromkeys(k for k in (_phrase_key(p) for p in re.split(r"[=,;]", line)) if k))
        if len(keys) >= 2:
            groups.append(keys)
    return Synonyms(tuple(groups), tuple(one_way))


def merge_synonyms(*items: Synonyms) -> Synonyms:
    return Synonyms(tuple(g for s in items for g in s.groups), tuple(o for s in items for o in s.one_way))


@lru_cache(maxsize=1)
def builtin_synonyms() -> Synonyms:
    return parse_synonyms(BUILTIN_SYNONYMS)


@lru_cache(maxsize=16)
def _file_synonyms(path: str, mtime: float) -> Synonyms:
    try:
        return parse_synonyms(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("Synonymdatei nicht lesbar (%s): %s", path, exc)
        return Synonyms()


def load_synonyms(path: str | None = None) -> Synonyms:
    """Built-in list plus an optional file (SYNONYMS_FILE, same format); re-read when it changes."""
    base = builtin_synonyms()
    if not path:
        return base
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        log.warning("Synonymdatei nicht gefunden: %s", path)
        return base
    return merge_synonyms(base, _file_synonyms(path, mtime))


# --------------------------------------------------------------------------- question analysis
_WORD_CASED = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class QueryTerm:
    """One content word of a question with everything it may match."""

    term: str  # stem of the folded word
    word: str  # folded word as typed
    noun: bool  # capitalised (or an abbreviation/number) – nouns carry the topic in German
    syns: tuple[tuple[str, ...], ...] = ()  # abbreviations/synonyms (phrases as tuples of stems)
    forms: tuple[str, ...] = ()  # other word forms (participle bases, ablaut, verbal noun)
    attributive: bool = False  # lower-case word right before a noun ("nasse Steigleitung")
    weak: bool = False  # set by callers for words that mainly shape the question ("wie lange")

    @property
    def weight(self) -> float:
        """Nouns carry the topic; attributes narrow it; verbs and adverbs vary most between a
        question and the text that answers it."""
        if self.weak:
            return 0.25
        if self.noun:
            return 1.0
        return 0.75 if self.attributive else 0.4

    @property
    def alts(self) -> tuple[tuple[str, ...], ...]:
        return self.syns + tuple((f,) for f in self.forms)


def query_terms(text: str, synonyms: Synonyms | None = None, *,
                drop: frozenset[str] = frozenset()) -> list[QueryTerm]:
    """Content words of a question with word-form variants and synonyms.

    Question words, filler words and words in ``drop`` (folded) are left out. A word typed with a
    capital letter counts as noun unless the question is written entirely in lower case.
    """
    syn = synonyms if synonyms is not None else builtin_synonyms()
    raw = _WORD_CASED.findall(text)
    any_upper = any(w[:1].isupper() for w in raw[1:])
    verb_at, particle = _separable_verb(raw)
    out: dict[str, QueryTerm] = {}
    for i, w in enumerate(raw):
        f = fold(w)
        if i == verb_at:  # "Wer nimmt die Anlage ab?" → abnimmt (matches Abnahme, abnehmen)
            f = particle + f
        if (len(f) < 2 and not f.isdigit()) or f in STOPWORDS or f in QUESTION_WORDS or f in QUERY_FILLER \
                or f in drop:
            continue
        t = stem(f)
        if t in out:
            continue
        noun = (w[:1].isupper() and (i > 0 or w.isupper())) or not any_upper or f.isdigit() \
            or (len(w) >= 2 and w.isupper())
        attributive = not noun and i + 1 < len(raw) and raw[i + 1][:1].isupper()
        forms = list(verb_bases(f))
        vn = None if noun else verbal_noun(f)  # verbs only: "geprüft" also finds "Prüfung"
        if vn and vn not in forms:
            forms.append(vn)
        syns: dict[tuple[str, ...], None] = {}
        for base in [t, *forms]:
            for a in syn.alternatives(base):
                if a != (t,) and not (len(a) == 1 and a[0] in forms):
                    syns.setdefault(a, None)
        out[t] = QueryTerm(t, f, noun, tuple(syns), tuple(x for x in forms if x != t), attributive)
    return list(out.values())


def _separable_verb(raw: list[str]) -> tuple[int | None, str]:
    """Position of the finite verb whose separable particle ends the question, and the particle.

    German questions put the verb second (W-questions) or first (yes/no questions) and the particle
    last: 'Wer nimmt die Druckbelüftung ab?', 'Hält die Feuerwehr die Fläche frei?'."""
    if len(raw) < 3:
        return None, ""
    particle = fold(raw[-1])
    if particle not in _SEP_PREFIX:
        return None, ""
    at = 1 if fold(raw[0]) in QUESTION_WORDS or fold(raw[0]) in ("wer", "wie", "was", "wann", "wo") else 0
    verb = raw[at]
    if not verb[:1].islower() and at == 1:
        return None, ""
    return at, particle


@lru_cache(maxsize=4096)
def text_terms(text: str) -> frozenset[str]:
    """All stems and word-form variants of a text (the document side of matching)."""
    terms: set[str] = set()
    for w in set(words(text)):
        if len(w) < 2 and not w.isdigit():
            continue
        terms |= word_variants(w)
    return frozenset(terms)


def _alt_in(a: str, doc: frozenset[str] | set[str], reverse: bool = True) -> bool:
    if a in doc:
        return True
    if a.isdigit():
        return False
    for d in doc:
        if len(a) >= 4 and d.startswith(a):  # Rauchableitung → Rauchableitungsöffnung
            return True
        if reverse and len(a) >= 6 and len(d) >= 6 and a.startswith(d):  # Abweichungsantrag → Abweichung
            return True
        if len(a) >= 5 and d.endswith(a):  # Genehmigung → Baugenehmigung (compound head)
            return True
        if len(a) >= 6 and a in d:  # Wasser → Löschwasserbedarf
            return True
    return False


def _phrase_in(phrase: tuple[str, ...], doc: frozenset[str] | set[str]) -> bool:
    """A synonym/word form – only exact, prefix and compound-head matches (a synonym such as
    'Brandschutzdienststelle' must not match its mere beginning 'Brandschutz')."""
    return all(_alt_in(p, doc, reverse=False) for p in phrase)


def _decompound(t: str, doc: frozenset[str] | set[str], synonyms: Synonyms) -> bool:
    """Compound written together in the question, apart in the text:
    'Entrauchungsventilatoren' ↔ 'Ventilatoren der Entrauchung', 'Drehleiterfläche' ↔
    'Aufstellfläche für das Hubrettungsfahrzeug'."""
    if len(t) < 9:
        return False
    for i in range(4, len(t) - 3):
        left, right = t[:i], t[i:]
        lefts = {left, left[:-1]} if left.endswith("s") and len(left) > 5 else {left}
        if not _alt_in(right, doc) and not any(_phrase_in(a, doc) for a in synonyms.alternatives(right)):
            continue
        for lf in lefts:
            if len(lf) >= 4 and (_alt_in(lf, doc) or any(_phrase_in(a, doc) for a in synonyms.alternatives(lf))):
                return True
    return False


@lru_cache(maxsize=65536)
def query_term_in(q: QueryTerm, doc: frozenset[str], synonyms: Synonyms | None = None) -> bool:
    """Does a question word (or one of its variants/synonyms) occur in a text's term set?"""
    if _alt_in(q.term, doc) or any(_phrase_in(a, doc) for a in q.alts):
        return True
    if q.word != q.term and len(q.word) >= 6 and _alt_in(q.word, doc):  # stem too short: wass(er)
        return True
    return _decompound(q.term, doc, synonyms if synonyms is not None else builtin_synonyms())


# --------------------------------------------------------------------------- question intent
_ENUMERATION = re.compile(
    r"\b(in\s+welchen|welche[n]?|alle[n]?|wie\s+viele[n]?|in\s+wie\s+vielen)\s+(\w+\s+)?"
    r"(projekte[n]?|bauvorhaben|vorhaben|objekte[n]?|gebäude[n]?|baustellen)\b"
    r"|\bwo\s+(überall|alles)\b", re.I)
_PROJECT_QUESTION = re.compile(
    r"\b(welche[mrsn]?|in\s+welche[mrn]?|für\s+welche[sn]?|bei\s+welche[mrn]?)\s+(\w+\s+)?"
    r"(projekt\w*|bauvorhaben\w*|vorhaben\w*|objekt\w*|gebäude\w*|baustelle\w*)\b|^\s*wo\b", re.I)


def is_enumeration_question(question: str) -> bool:
    """'In welchen Projekten …?', 'Welche Projekte …?' – the answer is a list over projects."""
    return bool(_ENUMERATION.search(question))


def is_project_question(question: str) -> bool:
    """Asks which project(s) something belongs to ('In welchem Projekt …?', 'Wo wird …?')."""
    return bool(_PROJECT_QUESTION.search(question)) or is_enumeration_question(question)


def is_temporal_question(question: str) -> bool:
    """Asks for the latest state ('letzter Stand', 'inzwischen', 'schon', 'aktuell')."""
    return any(w in TEMPORAL_WORDS for w in words(question))


# Reference codes such as project numbers ("BS-2024-0137"), file numbers ("63-0137/24"): they pick a
# project or file but say nothing about the topic of a question.
_IDENTIFIER = re.compile(r"\b(?:[A-Za-zÄÖÜäöü]{1,6}[-/ ]?)?\d{2,6}(?:[-/]\d{1,6}){1,3}\b")


def strip_identifiers(text: str) -> str:
    return _IDENTIFIER.sub(" ", text)
