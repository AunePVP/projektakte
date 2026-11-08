"""Deterministic stand-ins for the three AI services (pure functions, no I/O).

They are good enough for the whole pipeline to work end-to-end and behave like sensible (if simple)
models, not like a lookup table for the evaluation set:

* embeddings: signed feature hashing of stemmed words (+ a little character n-gram signal for
  German compounds) onto 256 dimensions, L2-normalised – lexically similar texts end up close;
* reranker: weighted share of the question's topic words found in the document, with word forms,
  compounds and fire-safety synonyms/abbreviations (BMA ↔ Brandmeldeanlage); filler words such as
  "Projekt" or "vorgesehen" do not count;
* chat: an extractive reader. It splits the numbered sources into sentences (headings, greetings,
  signatures and questions are never quoted, table rows are read as "label: value"), scores them by
  the question's topic words (IDF-weighted over the given sources, project names and numbers do not
  count), answers with the best sentence(s) as "Laut <Quelle>: … [n]" and says "Nicht gefunden." when
  no sentence covers the question. Questions about the latest state use the newest dated source;
  questions over several projects get one finding per project.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field, replace
from datetime import date

from ..textutil import (
    TEMPORAL_WORDS,
    QueryTerm,
    content_terms,
    fold,
    is_enumeration_question,
    is_project_question,
    query_term_in,
    query_terms,
    stem,
    strip_identifiers,
    text_terms,
    words,
)

DIM = 256
NOT_FOUND = "Nicht gefunden."
_SOURCE_HEADER = re.compile(r"^\[(\d+)\]\s*(.*)$", re.MULTILINE)


# --------------------------------------------------------------------------- embeddings
def _bucket(feature: str) -> tuple[int, float]:
    h = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    idx = int.from_bytes(h[:4], "little") % DIM
    sign = 1.0 if h[4] & 1 else -1.0
    return idx, sign


def embed_text(text: str) -> list[float]:
    if "Query:" in text:  # query instruction prefix (see docs/ki-dienste.md) carries no content
        text = text.rsplit("Query:", 1)[1]
    vec = [0.0] * DIM
    for term in content_terms(text):
        idx, sign = _bucket("w:" + term)
        vec[idx] += sign
        if len(term) >= 8:  # compounds: Rauchableitung ~ Rauchabzug ~ Rauchabschnitt
            grams = [term[i : i + 5] for i in range(len(term) - 4)]
            w = 0.35 / math.sqrt(len(grams))
            for g in grams:
                idx, sign = _bucket("g:" + g)
                vec[idx] += sign * w
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        vec[0] = 1.0
        return vec
    return [x / norm for x in vec]


# --------------------------------------------------------------------------- reranker
def rerank_score(query: str, doc: str) -> float:
    """Weighted share of the question's topic words in the document (+ a small density bonus).

    Like a cross-encoder it judges the topic: reference codes such as project numbers in the question
    do not count."""
    q = query_terms(strip_identifiers(query))
    if not q:
        return 0.0
    doc_terms = text_terms(doc)
    total = sum(t.weight for t in q)
    hits = [t for t in q if query_term_in(t, doc_terms)]
    coverage = sum(t.weight for t in hits) / total
    dens = min(1.0, len(hits) / (1 + len(doc_terms) / 40))  # tie-breaker: denser documents win
    return round(coverage + 0.05 * dens, 6)


# --------------------------------------------------------------------------- chat: prompt parsing
@dataclass
class Source:
    n: int
    header: str
    text: str
    kind: str = "document"  # document | mail | attachment
    project: str | None = None
    project_name: str | None = None
    date: date | None = None
    sender: str | None = None
    name: str = ""  # file name or subject
    page: int | None = None

    @property
    def header_terms(self) -> frozenset[str]:
        return text_terms(self.header)

    def describe(self, *, with_date: bool = False) -> str:
        """How the answer refers to this source: 'Baugenehmigung (S. 2)', 'Mail von X vom …'."""
        d = self.date.strftime("%d.%m.%Y") if self.date else None
        if self.kind == "mail":
            return f"Mail von {self.sender} vom {d}" if self.sender else f"Mail vom {d}"
        if self.kind == "attachment":
            return f"Anhang „{self.name}“ (Mail vom {d})"
        base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", self.name.rsplit("/", 1)[-1])
        base = re.sub(r"^\d{4}-\d{2}-\d{2}[ _]+", "", base)
        extra = [f"S. {self.page}"] if self.page else []
        if with_date and d:
            extra.append(d)
        return f"{base} ({', '.join(extra)})" if extra else base


_H_PROJECT = re.compile(r"\s+–\s+Projekt\s+(\S+)\s+(.+)$")
_H_MAIL = re.compile(r"^Mail vom (\d{2}\.\d{2}\.\d{4}) \((.*?)\):\s*(.*)$")
_H_ATTACHMENT = re.compile(r"^(.*?) \(Anhang einer Mail vom (\d{2}\.\d{2}\.\d{4})\)")
_H_PAGE = re.compile(r", S\. (\d+)")
_H_STAND = re.compile(r"\(Stand (\d{2}\.\d{2}\.\d{4})\)")


def _de(d: str) -> date | None:
    try:
        day, month, year = (int(x) for x in d.split("."))
        return date(year, month, day)
    except ValueError:
        return None


def _parse_header(n: int, header: str, text: str) -> Source:
    s = Source(n=n, header=header, text=text)
    rest = header
    m = _H_PROJECT.search(rest)
    if m:
        s.project, s.project_name = m.group(1), m.group(2).strip()
        rest = rest[: m.start()]
    m = _H_PAGE.search(rest)
    if m:
        s.page = int(m.group(1))
    if (m := _H_MAIL.match(rest)):
        s.kind, s.date, s.sender, s.name = "mail", _de(m.group(1)), m.group(2), m.group(3)
    elif (m := _H_ATTACHMENT.match(rest)):
        s.kind, s.name, s.date = "attachment", m.group(1), _de(m.group(2))
    else:
        m = _H_STAND.search(rest)
        s.date = _de(m.group(1)) if m else None
        s.name = re.split(r", S\. \d+| \(Stand ", rest)[0].strip()
    return s


def parse_sources(prompt: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Split a prompt into question and numbered sources [(n, header, text)]."""
    question = ""
    body = prompt
    m = re.search(r"^Frage:\s*(.*)\Z", prompt, re.MULTILINE | re.DOTALL)
    if m:
        question = m.group(1).strip()
        body = prompt[: m.start()]
    sources = []
    headers = list(_SOURCE_HEADER.finditer(body))
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        sources.append((int(h.group(1)), h.group(2).strip(), body[h.end() : end].strip()))
    return question, sources


# --------------------------------------------------------------------------- chat: reading sources
_MAIL_HEADER = re.compile(r"^(Betreff|Von|An|Cc|Datum|Gesendet):\s", re.I)
_SALUTATION = re.compile(r"^(sehr\s+geehrte[rs]?|hallo|guten\s+(tag|morgen|abend)|liebe[rs]?|hi|moin|servus)\b"
                         r"[^,:!.]{0,60}[,:!]\s*", re.I)
_CLOSING = re.compile(r"^(mit\s+freundlichen\s+grüßen|freundliche\s+grüße|viele\s+grüße|beste\s+grüße|"
                      r"herzliche\s+grüße|liebe\s+grüße|gruß\b|grüße\b|mfg\b|im\s+auftrag\b|i\.\s?a\.)", re.I)
_LIST_MARK = re.compile(r"^\s*(?:[-•*–]\s+|\d{1,2}[.)]\s+)")
_LABEL = re.compile(r"^([A-ZÄÖÜ][^:]{0,60}?):\s+(?=\S)")
_SECTION_NO = re.compile(r"^\d+(\.\d+)*\s+")
_ABBREV_END = re.compile(r"(\b(Nr|Abs|ca|bzw|ggf|inkl|vgl|Dipl|Ing|Str|Tel|Dr|Prof|gem|max|min|mind|S|u|z|d|e)"
                         r"|\b\d{1,2}|\b[A-ZÄÖÜ])\.$")


@dataclass
class Unit:
    """A quotable sentence with its label ('Auflage 7 (Rettungswege)') and section context."""

    source: Source
    pos: int
    line: int
    text: str
    label: str | None
    context: str
    terms: frozenset[str] = field(default_factory=frozenset)
    ctx_terms: frozenset[str] = field(default_factory=frozenset)
    table: bool = False


def _lines(text: str) -> list[str]:
    """Lines with PDF line breaks after ordinals rejoined ('im 3.' + 'Obergeschoss …')."""
    out: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if out and (re.search(r"\b\d{1,2}\.$", out[-1]) or out[-1].endswith(("-", ","))) and line[:1].isalpha():
            out[-1] = f"{out[-1]} {line}" if not out[-1].endswith("-") else out[-1] + line
        else:
            out.append(line)
    return out


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for piece in re.split(r"(?<=[.!?])\s+", text):
        buf = f"{buf} {piece}".strip()
        if not _ABBREV_END.search(buf):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _is_heading_like(line: str) -> bool:
    return len(line.split()) <= 6 and not re.search(r"[.!?:;]$", line) and ":" not in line


def read_units(src: Source) -> list[Unit]:
    """Quotable sentences of a source (see module docstring for what is left out)."""
    units: list[Unit] = []
    context = ""
    table_row = 0
    for li, line in enumerate(_lines(src.text)):
        if src.kind == "mail" and _MAIL_HEADER.match(line):
            continue
        if _CLOSING.match(line):
            break  # signature follows
        if line.startswith("#"):
            context = _SECTION_NO.sub("", line.lstrip("#").strip())
            table_row = 0
            continue
        if "|" in line:  # table row: header row skipped, others read as "label: value"
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            table_row += 1
            if table_row == 1 or len(cells) < 2:
                continue
            label, text = cells[0], ", ".join(c for c in cells[1:] if c)
            if text:
                units.append(Unit(src, len(units), li, text, label, context, table=True))
            continue
        table_row = 0
        line = _SALUTATION.sub("", line).strip()
        line = _LIST_MARK.sub("", line).strip()
        if len(line) <= 3:
            continue
        if _is_heading_like(line):
            context = _SECTION_NO.sub("", line)
            continue
        label = None
        m = _LABEL.match(line)
        if m and len(m.group(1).split()) <= 5:
            label, line = m.group(1).strip(), line[m.end():].strip()
        for sent in _sentences(line):
            if sent.endswith("?") or len(sent) <= 2:
                continue  # questions in a source are no answer
            sent = sent[0].upper() + sent[1:]
            lab = label
            if lab is None and len(sent.split()) <= 3 and context:  # "Keine." under "Abweichungen"
                lab = context
            units.append(Unit(src, len(units), li, sent, lab, context))
    for u in units:
        u.terms = text_terms(f"{u.label or ''} {u.text}")
        u.ctx_terms = text_terms(f"{src.header} {u.context}")
    return units


# --------------------------------------------------------------------------- chat: the question
_WIE_ADJ = re.compile(r"\bwie\s+(lange?|hoch|groß|gross|breit|tief|weit|schwer|viele?|oft|häufig|heiß|"
                      r"heiss|warm|stark|dick|alt|teuer)\b", re.I)
_TYPE_RULES = (
    ("date", re.compile(r"\b(wann|bis\s+wann|für\s+wann|seit\s+wann|an\s+welchem\s+tag|welche[ns]?\s+"
                        r"(datum|termin|zeitpunkt))\b", re.I)),
    ("frequency", re.compile(r"\bwie\s+(oft|häufig)\b|\bin\s+welchen\s+abständen\b", re.I)),
    ("quantity", re.compile(r"\bwie\s+(lange?|hoch|groß|gross|breit|tief|weit|schwer|viele?|heiß|heiss|warm|"
                            r"stark|dick|alt|teuer)\b|\bwelche[nrs]?\s+(betriebsdauer|breite|höhe|länge|fläche|"
                            r"größe|menge|anzahl|dauer|temperatur|überdruck|druck|luftwechsel|querschnitt|"
                            r"löschwassermenge)\b", re.I)),
    ("agent", re.compile(r"^\s*wer\b|\b(durch|von)\s+wem\b|\bwen\b", re.I)),
    ("reason", re.compile(r"\b(warum|weshalb|wieso|aus\s+welchem\s+grund)\b", re.I)),
)
_MONTHS = "Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
_TYPE_PATTERNS = {
    "date": re.compile(r"\b\d{1,2}\.\d{1,2}\.(\d{2,4})?(?!\d)|\bKW\s?\d{1,2}\b|\b(" + _MONTHS + r")\b|"
                       r"\b(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)\b"),
    "quantity": re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|m²|m³/h|m³|m\b|cm\b|mm\b|km\b|Pa\b|N\b|°C|Stunden|Std\b|h\b|"
                           r"Minuten|min\b|Tage|Wochen|Monate|Jahre|Personen|Plätze|Geschosse|Wohnungen|Kinder|"
                           r"Gastbetten|Stück|kg\b|kW\b|Lux|lx\b|Euro|€)|\b\d+-fach\b|\b(ein|zwei|drei|vier|fünf|"
                           r"sechs|acht|zehn|zwölf)\w*\s+(Stunden|Jahre|Minuten|Meter)\b"),
    "frequency": re.compile(r"\b(\w+mal|jährlich|halbjährlich|monatlich|wöchentlich|täglich|je\s+(Halbjahr|"
                            r"Schuljahr|Jahr|Monat|Quartal)|alle\s+\w+\s+(Jahre|Monate|Wochen|Tage))\b", re.I),
    "agent": re.compile(r"\b(durch|von)\s+(einen|eine|einem|einer|den|die|dem|der)\s+[A-ZÄÖÜ]\w+|"
                        r"\b(Prüfsachverständig|Prüfingenieur|Sachkundig|Fachkraft|Hersteller|Errichter|Betreiber|"
                        r"Türhersteller)\w*|\b(Dipl\.-Ing\.|Dr\.|Herr|Frau)\s+[A-ZÄÖÜ]|\b(GmbH|AG|eG|gGmbH|mbH)\b"),
    "reason": re.compile(r"\b(weil|da\s|wegen|aufgrund|daher|deshalb|deswegen|als\s+Ersatz|Ersatz\s+für|damit|"
                         r"abgelehnt|Voraussetzung|Kompensation|reicht\s+nicht)\b", re.I),
}
CONTEXT_CREDIT = 0.8  # a word found only in the source header/section counts a little less
MIN_COVERAGE = 0.7  # share of the (weighted) topic words a sentence must cover
TABLE_PENALTY = 0.6  # a terse table cell ("8 h") is used only if no sentence says it as well
MAX_SENTENCES = 3
MAX_PROJECTS = 10


@dataclass
class Question:
    text: str
    terms: list[QueryTerm]
    types: set[str]
    temporal: bool
    enumeration: bool
    project_question: bool
    projects: set[str]  # projects named in the question


def _project_names(sources: list[Source]) -> dict[str, set[str]]:
    """Stems of the project names (without numbers) per project, from the source headers."""
    out: dict[str, set[str]] = {}
    for s in sources:
        if s.project:
            out.setdefault(s.project, set()).update(
                w for w in content_terms(s.project_name or "") if not w.isdigit())
    return out


def _names_project(term: QueryTerm, word_set: set[str]) -> bool:
    if term.term.isdigit():
        return False
    for w in word_set:
        if term.term == w or term.word == w:
            return True
        if len(term.term) >= 4 and (w.endswith(term.term) or w.startswith(term.term)):
            return True  # "Halle" → Industriehalle, "Heim" → Pflegeheim
    return False


def analyse_question(question: str, sources: list[Source]) -> Question:
    shaping = {stem(fold(m.group(1))) for m in _WIE_ADJ.finditer(question)}  # "wie lange", "wie hoch"
    terms = [replace(t, weak=True) if t.term in shaping else t
             for t in query_terms(question, drop=frozenset(TEMPORAL_WORDS))]
    enumeration = is_enumeration_question(question)
    folded_q = fold(question)
    named: set[str] = set()
    project_terms: set[str] = set()
    for project, names in _project_names(sources).items():
        number_words = set(words(project))
        if fold(project) in folded_q:  # the full project number, e.g. "BS-2024-0137"
            named.add(project)
            project_terms.update(t.term for t in terms if t.term in number_words or t.word in number_words)
        hits = [t.term for t in terms if _names_project(t, names)]
        if hits:
            named.add(project)
            project_terms.update(hits)
    if not enumeration:  # project names identify the project – they are no answer to the question
        terms = [t for t in terms if t.term not in project_terms]
    types = {name for name, rx in _TYPE_RULES if rx.search(question)}
    return Question(question, terms, types, any(w in TEMPORAL_WORDS for w in words(question)),
                    enumeration, is_project_question(question), named if not enumeration else set())


# --------------------------------------------------------------------------- chat: aliases
_ROLE_WORDS = ("bauherr", "architekt", "prufingenieur", "fachplaner", "bauleitung", "errichter", "betreiber",
               "brandschutzplanung", "sachverstandig")
_PERSON = r"(?:(?:Dipl\.-Ing\.|Dr\.|Prof\.)\s+)?[A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß\-]+){1,2}"
_ROLE_PAREN = re.compile(r"(" + _PERSON + r")\s+\(([A-ZÄÖÜ][\w\- ]{2,40})\)")
_REPRESENTED = re.compile(r"vertreten\s+durch\s+(" + _PERSON + r")")


def role_aliases(units: dict[int, list[Unit]]) -> dict[str, set[str]]:
    """Surnames per role stem, read from the sources: 'Renate Albers (Bauherr)', 'Bauherr: …,
    vertreten durch Renate Albers', 'Prüfingenieur: Dipl.-Ing. Sabine Wendehals, …'."""
    out: dict[str, set[str]] = {}

    def add(role: str, person: str) -> None:
        r = stem(fold(role.split()[0]))
        if any(r.startswith(x) or x.startswith(r) for x in _ROLE_WORDS):
            out.setdefault(r, set()).add(fold(person.split()[-1]))

    for source_units in units.values():
        for u in source_units:
            if u.label:
                m = re.match(_PERSON, u.text)
                if m:
                    add(u.label, m.group(0))
                for m in _REPRESENTED.finditer(u.text):
                    add(u.label, m.group(1))
            for m in _ROLE_PAREN.finditer(u.text):
                add(m.group(2), m.group(1))
    return out


def _alias_hit(term: QueryTerm, src: Source, aliases: dict[str, set[str]]) -> bool:
    if not src.sender:
        return False
    sender = set(words(src.sender))
    for role, names in aliases.items():
        if (role.startswith(term.term) or term.term.startswith(role)) and names & sender:
            return True
    return False


# --------------------------------------------------------------------------- chat: scoring
@dataclass
class Scored:
    unit: Unit
    coverage: float
    inline: bool
    score: float
    matched: frozenset[str]
    types: frozenset[str]


def _idf(q: Question, sources: list[Source], units: dict[int, list[Unit]]) -> dict[str, float]:
    n = len(sources)
    out = {}
    for t in q.terms:
        df = sum(1 for s in sources
                 if query_term_in(t, s.header_terms) or any(query_term_in(t, u.terms) for u in units[s.n]))
        out[t.term] = math.log((n + 1) / (df + 0.5)) / math.log((n + 1) / 0.5)
    return out


def _bigram_bonus(q: Question, u: Unit) -> float:
    """Adjacent question words that are adjacent in the sentence too ('Auflage 7')."""
    seq = [stem(w) for w in words(f"{u.label or ''} {u.text}")]
    pairs = set(zip(seq, seq[1:]))
    qs = [t.term for t in q.terms]
    return 0.5 * sum(1 for a, b in zip(qs, qs[1:]) if (a, b) in pairs)


def score_units(q: Question, sources: list[Source], units: dict[int, list[Unit]]) -> list[Scored]:
    aliases = role_aliases(units)
    idf = _idf(q, sources, units)
    total = sum(t.weight for t in q.terms)
    out: list[Scored] = []
    for s in sources:
        if q.projects and s.project not in q.projects:
            continue
        for u in units[s.n]:
            types = frozenset(name for name in q.types if _TYPE_PATTERNS[name].search(f"{u.label or ''} {u.text}"))
            credit: dict[str, float] = {}
            inline = False
            for t in q.terms:
                if query_term_in(t, u.terms):
                    credit[t.term] = 1.0
                    inline = True
                elif t.weak and types:  # "wie oft" is answered by "alle zwei Jahre"
                    credit[t.term] = 1.0
                elif query_term_in(t, u.ctx_terms) or _alias_hit(t, s, aliases):
                    credit[t.term] = CONTEXT_CREDIT
            weights = {t.term: t.weight for t in q.terms}
            coverage = sum(weights[k] * c for k, c in credit.items()) / total if total else 0.0
            label_terms = text_terms(u.label or "")
            score = sum(weights[k] * c * (1 + idf[k]) for k, c in credit.items())
            score += 0.8 * len(types) + _bigram_bonus(q, u)
            score += 0.5 * sum(1 for t in q.terms if u.label and query_term_in(t, label_terms))
            score -= 0.001 * len(u.text) + (TABLE_PENALTY if u.table else 0.0)  # among equals: precise prose
            out.append(Scored(u, coverage, inline, score, frozenset(credit), types))
    return out


def _answerable(q: Question, sc: Scored) -> bool:
    if not q.terms:
        return False
    return sc.coverage >= MIN_COVERAGE - 1e-9 and (sc.inline or q.temporal)


def _key(text: str) -> str:
    return " ".join(words(text))


def _open_question(q: Question) -> bool:
    """'Was gilt für …?', 'Was muss … beachtet werden?' – asks for everything about a topic."""
    return not q.types and bool(re.match(r"\s*was\b", q.text, re.I))


def _select(q: Question, candidates: list[Scored]) -> list[Scored]:
    """Best sentence plus sentences that add question words or answer types not covered yet.

    Questions about the latest state take the newest source first (an equally good older one only
    as history); open 'Was …?' questions may collect statements from several sources."""
    if q.temporal:
        candidates = sorted(candidates, key=lambda c: (c.unit.source.date or date.min, c.coverage, c.inline,
                                                       -c.unit.pos), reverse=True)
    else:
        candidates = sorted(candidates, key=lambda c: c.score, reverse=True)
    best = candidates[0]
    chosen = [best]
    seen = [_key(best.unit.text)]
    covered, types = set(best.matched), set(best.types)

    def take(c: Scored) -> None:
        chosen.append(c)
        seen.append(_key(c.unit.text))
        covered.update(c.matched)
        types.update(c.types)

    same_source = sorted((c for c in candidates if c.unit.source is best.unit.source and c is not best),
                         key=lambda c: c.unit.pos)
    if not q.types and best.unit.label:  # whole numbered paragraph ("Auflage 7 …: s1 s2")
        for c in same_source:
            if c.unit.line == best.unit.line and len(chosen) < 2:
                take(c)
    elif q.temporal and not best.inline:  # status mail: its next sentence belongs to the news
        for c in same_source:
            if c.unit.pos == best.unit.pos + 1:
                take(c)
    sources = {best.unit.source.n}
    for c in candidates[1:]:
        if len(chosen) >= MAX_SENTENCES:
            break
        k = _key(c.unit.text)
        if c in chosen or any(k in s or s in k for s in seen):
            continue  # same sentence from an overlapping source
        gain = (set(c.matched) - covered) | (set(c.types) - types)
        broad = _open_question(q) and not q.temporal and c.unit.source.n not in sources and not c.unit.table \
            and c.score >= 0.7 * best.score
        if (gain and c.score >= 0.4 * best.score) or broad:
            take(c)
            sources.add(c.unit.source.n)
    return chosen


def _latest_state(sources: list[Source], units: dict[int, list[Unit]]) -> list[Unit]:
    """'Was ist der aktuelle Stand?' without a topic: the first sentences of the newest source."""
    dated = sorted((s for s in sources if s.date), key=lambda s: (s.date, s.kind != "document"), reverse=True)
    for s in dated:
        if units[s.n]:
            return units[s.n][:2]
    return []


# --------------------------------------------------------------------------- chat: wording
def _phrase(units: list[Unit], *, with_project: bool, with_date: bool) -> str:
    """'[Projekt – ]Laut <Quelle>[, <Label>]: Satz. Satz. [n]'"""
    src = units[0].source
    label = units[0].label
    texts = " ".join(u.text if u.text.endswith((".", "!", "…")) else u.text + "." for u in units)
    prefix = f"{src.project} {src.project_name} – " if with_project and src.project else ""
    head = f"{'laut' if prefix else 'Laut'} {src.describe(with_date=with_date)}"
    if label:
        head += f", {label}"
    return f"{prefix}{head}: {texts} [{src.n}]"


def _groups(units: list[Unit]) -> list[list[Unit]]:
    """Consecutive sentences of one source (and label) are quoted together."""
    groups: list[list[Unit]] = []
    for u in units:
        if groups and groups[-1][0].source is u.source and groups[-1][0].label == u.label:
            groups[-1].append(u)
        else:
            groups.append([u])
    for g in groups:
        g.sort(key=lambda u: u.pos)
    return groups


def answer_from_sources(question: str, raw_sources: list[tuple[int, str, str]]) -> str:
    sources = [_parse_header(n, h, t) for n, h, t in raw_sources]
    if not sources or not question.strip():
        return NOT_FOUND
    q = analyse_question(question, sources)
    units = {s.n: read_units(s) for s in sources}
    several_projects = len({s.project for s in sources}) > 1
    with_project = several_projects or q.project_question
    if not q.terms:
        if q.temporal:
            latest = _latest_state([s for s in sources if not q.projects or s.project in q.projects], units)
            if latest:
                return " ".join(_phrase(g, with_project=with_project, with_date=True) for g in _groups(latest))
        return NOT_FOUND
    candidates = [c for c in score_units(q, sources, units) if _answerable(q, c)]
    if not candidates:
        return NOT_FOUND
    if q.enumeration and several_projects:
        best: dict[str | None, Scored] = {}
        for c in sorted(candidates, key=lambda c: c.score, reverse=True):
            best.setdefault(c.unit.source.project, c)
        picked = list(best.values())[:MAX_PROJECTS]
        if len(picked) > 1:
            parts = [_phrase([c.unit], with_project=True, with_date=q.temporal) for c in picked]
            return f"Gefunden in {len(picked)} Projekten: " + " ".join(parts)
    chosen = _select(q, candidates)
    return " ".join(_phrase(g, with_project=with_project, with_date=q.temporal)
                    for g in _groups([c.unit for c in chosen]))


CHAT_QUIRKS = ("length", "reasoning_only", "unterminated_think")


def chat_completion(payload: dict, *, think: bool = True, quirk: str | None = None) -> dict:
    """OpenAI chat completion. ``quirk`` imitates edge cases of real servers (see CHAT_QUIRKS)."""
    messages = payload.get("messages") or []
    model = payload.get("model") or "fake-llm"
    user_texts, has_image = [], False
    for m in messages:
        text, img = _message_text(m.get("content"))
        has_image = has_image or img
        if m.get("role") == "user":
            user_texts.append(text)
    prompt = user_texts[-1] if user_texts else ""
    if has_image:
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        answer = (
            f"Fake-Transkription ({digest}): Fluchtwegplan Erdgeschoss. Notausgang Ost, "
            "Sammelplatz Parkplatz Nord, Feuerlöscher neben Treppenraum 1."
        )
    else:
        question, sources = parse_sources(prompt)
        if sources or question:
            answer = answer_from_sources(question, sources)
        else:
            answer = "OK"
    content = f"<think>Ich prüfe die Quellen.</think>\n{answer}" if think else answer
    message: dict = {"role": "assistant", "content": content}
    finish = "stop"
    if quirk == "length":  # max_tokens reached in the middle of the answer
        message["content"] = content[: max(1, len(content) * 2 // 3)]
        finish = "length"
    elif quirk == "reasoning_only":  # vLLM reasoning parser: the token budget went into reasoning
        message = {"role": "assistant", "content": None, "reasoning_content": "Ich prüfe die Quellen … " * 20}
        finish = "length"
    elif quirk == "unterminated_think":
        message["content"] = "<think>Ich prüfe die Quellen und überlege, welche passt " + "… " * 40
        finish = "length"
    prompt_tokens = sum(len(words(_message_text(m.get("content"))[0])) for m in messages)
    return {
        "id": "chatcmpl-fake-" + hashlib.sha256(fold(prompt).encode()).hexdigest()[:12],
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(words(content)),
            "total_tokens": prompt_tokens + len(words(content)),
        },
    }


def _message_text(content) -> tuple[str, bool]:
    if isinstance(content, list):
        text = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        has_image = any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)
        return text, has_image
    return str(content or ""), False
