"""Deterministic stand-ins for the three AI services (pure functions, no I/O).

They are good enough for the whole pipeline to work end-to-end and behave like sensible (if simple)
models, not like a lookup table for an evaluation set:

* embeddings: signed feature hashing of stemmed words (+ a little character n-gram signal for
  German compounds) onto 256 dimensions, L2-normalised – lexically similar texts end up close;
* reranker: weighted share of the question's topic words found in the document, with word forms,
  compounds (by their head: a "Löschwasserteich" is a Teich, "Löschwasser" alone counts half) and
  fire-safety synonyms/abbreviations (BMA ↔ Brandmeldeanlage, plus SYNONYMS_FILE); filler words such
  as "Projekt" or "vorgesehen" do not count, and a matching number alone is no relevance
  ("<script>alert(1)</script>" scores 0). A query that consists of reference codes (project number,
  file number) is judged by whether the document contains them literally;
* chat: an extractive reader that fails closed. It splits the numbered sources into sentences
  (headings, greetings, courtesy phrases, signatures, role lines, participant lists, questions and
  the premise of a question are never quoted as facts, table rows are read as "Spalte: Wert") and
  scores them by the question's topic words (IDF-weighted over the given sources; project names and
  numbers do not count; contractions such as "fürs" are no topic, "Funk-Rauchwarnmelder" matches
  "funkvernetzte Rauchwarnmelder", "vertritt" matches "vertreten"); a place the sentence does not name
  ("im Sicherheitstreppenraum") counts half when another sentence of the project puts the sentence's
  subject there ("Die Druckbelüftungsanlage hält im Sicherheitstreppenraum …"). It answers with the
  best sentence(s) as "Laut <Quelle>: … [n]" (a sentence starting with a pronoun takes the one before
  it); a signature says who signed and who someone is ("Wer ist bei der Feuerwehr … zuständig?"). A question that asks for a value (date, amount,
  number, cost, duration, name, norm, class, colour, …) is only answered by a sentence that contains
  such a value for the asked topic – a length must be the asked dimension of the asked thing ("Wie
  breit …?" is not answered by "in 60 cm Höhe"), "Wann wurde … abgenommen?" needs the date of the
  event, not an obligation or a plan; otherwise – and whenever no sentence covers the topic – it says
  "Nicht gefunden.". A question naming a project is answered from that project only. Questions about
  the latest state use the newest dated source, "Was hat X geschrieben?" the sources written by X,
  decision questions ("Hat … zugestimmt?") the sources that state the decision, "Wer hat …
  unterschrieben?" the signature, overview questions several sources, and questions over several
  projects get one finding per project.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field, replace
from datetime import date

from ..textutil import (
    QUERY_FILLER,
    STOPWORDS,
    TEMPORAL_WORDS,
    ROLE_WORDS,
    QueryTerm,
    Synonyms,
    attribute_head,
    content_terms,
    find_identifiers,
    fold,
    identifier_in,
    is_enumeration_question,
    is_project_question,
    is_summary_question,
    is_temporal_question,
    load_synonyms,
    SUMMARY_WORDS,
    query_term_credit,
    query_term_in,
    query_terms,
    role_key,
    signature_start,
    split_sentences,
    split_signature,
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


# --------------------------------------------------------------------------- synonyms
def active_synonyms(path: str | None = None) -> Synonyms:
    """Built-in synonyms plus the synonym file – ``path`` or SYNONYMS_FILE, the same file the app's
    full-text search uses (``AI_MODE=fake`` runs in the app's process; ``python -m projektakte.fake_ai``
    takes ``--synonyms-file``). Re-read when the file changes."""
    return load_synonyms(path if path is not None else (os.environ.get("SYNONYMS_FILE") or None))


# --------------------------------------------------------------------------- question words
# a preposition fused with "das" ("fürs Pflegeheim", "aufs Dach", "ans Netz") is a function word, no topic
_CONTRACTIONS = frozenset("furs ans aufs ums ubers durchs vors hinters unters".split())
# the first part of a compound written with a hyphen ("Funk-Rauchwarnmelder", "Brandschutz-Konzept")
_HYPHEN_PART = re.compile(r"(?<![\w-])([^\W\d_]{3,})-(?=[^\W\d_])")
# strong verbs change their vowel in the present tense: "vertritt" ↔ vertreten/Vertreter, "hilft" ↔ helfen
_STRONG_PRESENT = (("tritt", "tret"), ("gibt", "geb"), ("hilft", "helf"), ("spricht", "sprech"), ("sieht", "seh"),
                   ("liest", "les"), ("wirft", "werf"), ("misst", "mess"), ("bricht", "brech"), ("trifft", "treff"),
                   ("empfiehlt", "empfehl"), ("befiehlt", "befehl"), ("stirbt", "sterb"), ("wirbt", "werb"),
                   ("vergisst", "vergess"), ("erlischt", "erlosch"), ("schmilzt", "schmelz"))


def question_terms(text: str, synonyms: Synonyms, *, drop: frozenset[str] = frozenset()) -> list[QueryTerm]:
    """:func:`query_terms` plus what typed questions need on top: a contraction ("fürs") is no topic,
    the first part of a hyphenated compound starts compounds as a whole word does ("Funk-Rauchwarnmelder"
    ↔ "funkvernetzte Rauchwarnmelder"), and a strong verb in the present matches its other forms
    ("vertritt" ↔ "vertreten durch")."""
    modifiers = {fold(m.group(1)) for m in _HYPHEN_PART.finditer(text)}
    out = []
    for t in query_terms(text, synonyms, drop=drop | _CONTRACTIONS):
        extra = [t.term] if t.word in modifiers and len(t.term) >= 4 else []
        extra += [t.word[: -len(present)] + base for present, base in _STRONG_PRESENT if t.word.endswith(present)]
        extra = [f for f in dict.fromkeys(extra) if f not in t.forms]
        if extra:
            syns = dict.fromkeys(t.syns)
            for f in extra:
                if f != t.term:
                    syns.update(dict.fromkeys(a for a in synonyms.expanded(f) if a != (t.term,)))
            t = replace(t, forms=t.forms + tuple(extra), syns=tuple(syns))
        out.append(t)
    return out


# --------------------------------------------------------------------------- reranker
IDENTIFIER_BONUS = 0.1  # a question with topic words: the named project/file number only breaks ties
_MARKUP = re.compile(r"<[^<>]{0,200}>|\bjavascript:", re.I)


def rerank_score(query: str, doc: str, synonyms: Synonyms | None = None) -> float:
    """Weighted share of the question's topic words in the document (+ a small density bonus).

    Like a cross-encoder it judges the topic. Reference codes (project numbers, file numbers) are
    no topic: with topic words they only add a small bonus; a query of reference codes alone
    ("BS-2024-0233", "63-0137/24") is scored by the share of codes the document contains literally.
    A number is no topic either: when none of the query's words matches, a matching digit does not
    make the document relevant ("<script>alert(1)</script>" scores 0 – markup is ignored)."""
    idents = find_identifiers(query)
    ident_share = sum(1 for i in idents if identifier_in(i, doc)) / len(idents) if idents else 0.0
    syn = synonyms if synonyms is not None else active_synonyms()
    q = question_terms(strip_identifiers(_MARKUP.sub(" ", query)), syn, drop=TEMPORAL_WORDS)  # "letzter Stand" shapes
    # a short number only counts next to real topic words ("Auflage 7"), never on its own ("1")
    if all(t.word.isdigit() and len(t.word) <= 2 for t in q):
        q = []
    if not q:
        return round(ident_share, 6)
    doc_terms = text_terms(doc)
    total = sum(t.weight for t in q)
    credits = [query_term_credit(t, doc_terms) for t in q]
    words_ = [(t, c) for t, c in zip(q, credits) if not t.word.isdigit()]
    if words_ and not any(c > 0 for t, c in words_ if len(t.word) >= 3):
        # no topic word matched – a matching number alone ("alert(1)" ↔ "Prüfbericht Nr. 1") is no relevance
        return round(IDENTIFIER_BONUS * ident_share, 6)
    coverage = sum(t.weight * c for t, c in zip(q, credits)) / total
    hits = sum(1 for c in credits if c >= 1.0)
    dens = min(1.0, hits / (1 + len(doc_terms) / 40))  # tie-breaker: denser documents win
    return round(coverage + 0.05 * dens + IDENTIFIER_BONUS * ident_share, 6)


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
    signature: str = ""  # lines after the closing of a mail ("Prüfingenieurin für Brandschutz")

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


# --------------------------------------------------------------------------- chat: values in sentences
_MONTHS = "Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
_MONTH_NO = {fold(m): i for i, m in enumerate(_MONTHS.split("|"), start=1)}
_NUMWORDS = ("ein", "eine", "einen", "einem", "einer", "zwei", "drei", "vier", "fünf", "sechs", "sieben",
             "acht", "neun", "zehn", "elf", "zwölf", "zwanzig", "dreißig", "hundert", "beide", "beiden")
_NUMWORD_RX = "|".join(sorted(_NUMWORDS, key=len, reverse=True))
_NUMWORD_SET = frozenset(fold(w) for w in _NUMWORDS[5:])
_COUNTWORD_RX = "|".join(sorted(_NUMWORDS[5:], key=len, reverse=True))  # numbers without the articles "ein(e)"
_UNIT = (r"(?:m²|m³/h|m³|qm|m|cm|mm|km|Pa|bar|N|kN|°\s?C|%|kW|W|lx|Lux|dB|kg|t|Tonnen|Stunden|Std\.?|h|"
         r"Minuten|min|Sekunden|Tage[n]?|Wochen|Monate[n]?|Jahre[n]?|Euro|EUR|€)(?![\w²³])")
VALUE_PATTERNS = {
    "date": re.compile(r"\b\d{1,2}\.\d{1,2}\.(\d{2,4})?(?!\d)|\b\d{1,2}\.\s*(" + _MONTHS + r")\b|\bKW\s?\d{1,2}\b|"
                       r"\b(" + _MONTHS + r")\b|\b(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)\b|"
                       r"\b(diese[rn]?|nächste[rn]?|kommende[rn]?)\s+Woche\b|\b(Anfang|Mitte|Ende)\s+(des\s+Monats|"
                       r"der\s+Woche|des\s+Jahres)\b|\bQuartal\b|\b(19|20)\d{2}\b"),
    "duration": re.compile(r"\d+(?:[.,]\d+)?\s*(Stunden|Std\.?|h|Minuten|min|Sekunden|Tage[n]?|Wochen|Monate[n]?|"
                           r"Jahre[n]?)(?![\w²³/])|\b(" + _NUMWORD_RX + r")\s+(Stunden|Minuten|Tage|Wochen|Monate|"
                           r"Jahre)\b", re.I),
    "measure": re.compile(r"\d+(?:[.,]\d+)?\s*(?:[–-]\s*\d+(?:[.,]\d+)?\s*)?(?:bis\s+\d+(?:[.,]\d+)?\s*)?"
                          r"(m²|m³/h|m³|qm|m|cm|mm|km|Pa|bar|N|kN|°\s?C|%|kW|lx|Lux|dB)(?![\w²³])"),
    "multiple": re.compile(r"\b\d+\s*-?\s*fach\w*\b|\b(doppelt|dreifach|zweifach|zweifacher)\w*\b", re.I),
    "money": re.compile(r"\d[\d.,]*\s*(€|Euro\b|EUR\b|Mio\.?\s*(€|Euro)|Tsd\.?\s*(€|Euro))|(€|EUR)\s*\d", re.I),
    "frequency": re.compile(r"\b(\w+mal|jährlich|halbjährlich|vierteljährlich|monatlich|wöchentlich|täglich|"
                            r"je\s+(Halbjahr|Schuljahr|Jahr|Monat|Quartal|Woche)|pro\s+(Halbjahr|Schuljahr|Jahr|Monat|"
                            r"Quartal|Woche)|alle\s+\w+\s+(Jahre|Monate|Wochen|Tage)|wiederkehrend|regelmäßig)\b",
                            re.I),
    "standard": re.compile(r"\b(DIN|EN|ISO|VDE|VDI|VdS|ASR|DGUV|TRBS|MLAR|MBO|BauO)\b(\s+(EN|ISO|VDE))*"
                           r"(\s*[A-Z]?\d[\d.\-/]*)?|\bMuster-[\w-]+|\w+[Rr]ichtlinie\b|\w+[Vv]erordnung\b"),
    "class": re.compile(r"\bKategorie\s*\d|\b(Gebäude|Risiko|Baustoff|Feuerwiderstands)?[Kk]lasse\s*\w+|"
                        r"\b[TFREIW]{1,3}\s?\d{2,3}(-RS)?\b|\bTyp\s+[A-Z0-9]\b|\b(feuerbeständig|hochfeuerhemmend|"
                        r"feuerhemmend|nichtbrennbar|schwerentflammbar|normalentflammbar|Sonderbau)\w*", re.I),
    "color": re.compile(r"\b(rot|grün|blau|gelb|weiß|schwarz|grau|orange|braun|silbern|RAL\s?\d{4})(e[nrms]?)?\b",
                        re.I),
    "weight": re.compile(r"\d+(?:[.,]\d+)?\s*(kg|t|Tonnen|kN|kN/m²)(?![\w²³])"),
    "temperature": re.compile(r"\d+\s*°\s?C|\b\d+\s*Grad\b"),
    "language": re.compile(r"\b(deutsch|englisch|französisch|italienisch|spanisch|türkisch|polnisch|russisch|"
                           r"arabisch|zweisprachig|mehrsprachig)\w*", re.I),
    # a time given by an event: "vor Nutzungsaufnahme", "vor der Abnahme", "sobald …", "wenn innerhalb …"
    "relative": re.compile(r"\b(vor|nach|bei|während|bis\s+zur|bis\s+zum)\s+(der\s+|dem\s+|den\s+)?(ersten\s+)?"
                           r"(Nutzungsaufnahme|Inbetriebnahme|Abnahme|Fertigstellung|Eröffnung|Baubeginn|Bauzeit|Bezug|"
                           r"Beginn|Ende)\b|\b(sobald|nachdem|bevor|wenn\s+innerhalb|innerhalb\s+von)\b", re.I),
    "reason": re.compile(r"\b(weil|da\s|wegen|aufgrund|daher|deshalb|deswegen|als\s+Ersatz|Ersatz\s+für|damit|"
                         r"abgelehnt|Voraussetzung|Kompensation|reicht\s+nicht|um\s+zu)\b", re.I),
}
_ORG = re.compile(r"\b(GmbH|gGmbH|mbH|AG|eG|KG|e\.\s?V\.)\b|\b(Ingenieurbüro|Büro|Firma|Stadt|Amt|Jugendamt|"
                  r"Feuerwehr|Atelier|Akademie|Fachverband)\s+[A-ZÄÖÜ]\w+")
_TITLE = re.compile(r"\b(Dipl\.-Ing\.|Dr\.|Prof\.|Herr|Herrn|Frau)\s+[A-ZÄÖÜ]")
_ROLE_NOUN = re.compile(r"\w*(sachverständig|sachkundig|fachkraft|fachfirma|fachbetrieb|hersteller|errichter|"
                        r"betreiber|bauherr|architekt|ingenieur|planer|feuerwehr|prüfer|portier|hausmeister|"
                        r"personal|leitung|beauftragt|verfasser|bauaufsicht)\w*", re.I)
_AGENT_PHRASE = re.compile(r"\b(durch|von)\s+(einen|eine|einem|einer|den|die|dem|der)\s+[A-ZÄÖÜ]\w+")
_TOKEN = re.compile(r"\d+(?:[.,]\d+)?|[^\W\d_]+")
_MEASURE_UNIT = re.compile(r"^" + _UNIT)
QUANTITY_TYPES = ("count", "measure", "money", "duration", "weight", "temperature", "multiple")


def _has_type(kind: str, u: "Unit", q: "Question", names: frozenset[str]) -> bool:
    text = f"{u.label or ''} {u.text}" if u.label and u.table else u.text
    if kind == "count":
        return _count_near_topic(u, q)
    if kind == "quantity":
        return any(_has_type(k, u, q, names) for k in QUANTITY_TYPES) and _value_near_topic(u, q)
    if kind == "measure":
        return bool(VALUE_PATTERNS[kind].search(text)) and _value_near_topic(u, q) and _dimension_ok(u, q)
    if kind in ("duration", "money", "weight", "temperature", "multiple"):
        return bool(VALUE_PATTERNS[kind].search(text)) and _value_near_topic(u, q)
    if kind == "date":
        if q.past_event and _NOT_DONE.search(u.text):
            return False  # "… ist vor der Abnahme vorzulegen", "… ist für den 18.06. geplant" – not done
        return bool(VALUE_PATTERNS["date"].search(strip_identifiers(text)))
    if kind == "relative":
        return _relative_time(u, q)
    if kind == "name":
        return _has_name(text, names)
    if kind == "agent":
        return _has_name(text, names) or bool(_ROLE_NOUN.search(text) or _AGENT_PHRASE.search(text))
    return bool(VALUE_PATTERNS[kind].search(text))


def _has_name(text: str, names: frozenset[str]) -> bool:
    if _ORG.search(text) or _TITLE.search(text):
        return True
    return bool(names & set(words(text)))


def _topic_positions(u: "Unit", q: "Question", tokens: list[str], *, attrs: bool = False) -> set[int]:
    """Positions of tokens that are (or contain) a topic word (or, with ``attrs``, an attribute word)."""
    out = set()
    terms = [t for t in q.terms + (q.attrs if attrs else []) if not t.term.isdigit()]
    for i, tok in enumerate(tokens):
        if tok[:1].isdigit():
            continue
        tt = text_terms(tok)
        if any(query_term_credit(t, tt) >= 1.0 for t in terms):
            out.add(i)
    return out


def _label_matches_topic(u: "Unit", q: "Question") -> bool:
    return bool(u.label) and any(query_term_in(t, u.label_terms) for t in q.terms if not t.term.isdigit())


def _is_number(tok: str) -> bool:
    return tok[:1].isdigit() or fold(tok) in _NUMWORD_SET


_NOT_A_COUNT_BEFORE = frozenset("din en iso nr auflage position pos top kw vom am bis abs teil teile".split())


_PIECES = re.compile(r"\b(Stück|Stk\.?)\s*(\d+)\b|\b(\d+)\s*(Stück|Stk)\b")
_NOT_A_COUNT_AFTER = re.compile(r"^\s*(Jahre|Jahren|Monate|Monaten|Wochen|Tage|Tagen|Stunden|Minuten|mal\b)")


def _plain_counts(text: str) -> list[str]:
    """Numbers in a text that can be a count: no date, measure, frequency ('alle zwei Jahre'), norm."""
    text = VALUE_PATTERNS["date"].sub(" ", strip_identifiers(text))
    out = []
    spans = [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]
    for i, (tok, start, end) in enumerate(spans):
        if not _is_number(tok) or (i > 0 and fold(spans[i - 1][0]) in _NOT_A_COUNT_BEFORE | {"alle", "je"}):
            continue
        if tok[:1].isdigit() and start > 0 and text[start - 1].isalpha():
            continue  # part of a code: "ASR A2.2", "T30", "F90"
        rest = text[end:].lstrip()
        if (tok[:1].isdigit() and ((_MEASURE_UNIT.match(rest) and not rest.startswith("%")) or text[end:end + 1] in "-/")) \
                or _NOT_A_COUNT_AFTER.match(rest):
            continue  # "2,10 m", "alle zwei Jahre" – but "5 % der Beschäftigten" is a share
        out.append(tok)
    return out


def _count_near_topic(u: "Unit", q: "Question") -> bool:
    """A number counting the asked thing: '240 Einstellplätzen', 'fünf Gruppen', 'Kinder: 95',
    'insgesamt 95 Kindern', 'dreigeschossiger Bau', 'Rauchwarnmelder …, Stück 24' – not a measure
    ('2,10 m'), a norm ('DIN EN 81-72'), a frequency ('alle zwei Jahre'), a date or a number next to
    something else."""
    if _label_matches_topic(u, q) and _plain_counts(u.text):
        return True
    text = VALUE_PATTERNS["date"].sub(" ", strip_identifiers(u.text))
    spans = [(m.group(0), m.end()) for m in _TOKEN.finditer(text)]
    tokens = [t for t, _ in spans]
    near = _topic_positions(u, q, tokens)
    if not near:
        return False
    for clause in re.split(r";|\s–\s", text):  # "Funkvernetzter Rauchwarnmelder nach DIN 14676, Stück 24."
        if _PIECES.search(clause) and _topic_positions(u, q, _TOKEN.findall(clause)):
            return True
    for i, (tok, end) in enumerate(spans):
        low = fold(tok)
        if not tok[:1].isdigit():
            for w in _NUMWORDS[5:-2]:  # "dreigeschossig", "fünfgruppig", "achtgeschossigen"
                if low.startswith(fold(w)) and len(low) > len(w) + 3 and i in near:
                    return True
        if not _is_number(tok) or (i > 0 and fold(tokens[i - 1]) in _NOT_A_COUNT_BEFORE):
            continue
        if tok[:1].isdigit() and (_MEASURE_UNIT.match(text[end:].lstrip()) or text[end:end + 1] in "-/"):
            continue  # "2,10 m", "30 m", "8 Stunden", "81-72" are no counts
        for j in range(i + 1, min(len(tokens), i + 4)):  # adjectives may stand between: "95 neue Kinder"
            if j in near:
                return True
            if not tokens[j][:1].islower():
                break
    return False


def _value_near_topic(u: "Unit", q: "Question") -> bool:
    """A value belongs to the asked topic when it stands close to a topic word (or the row/label
    names the topic); 'Die Einfahrtshöhe von 2,10 m …' does not answer a question about parking."""
    if not [t for t in q.terms if not t.term.isdigit()]:
        return True  # only the attribute was asked ("Wie hoch ist das Gebäude?")
    if _label_matches_topic(u, q) or u.section_topic:
        return True
    for clause in re.split(r";|\s–\s", u.text):
        tokens = _TOKEN.findall(clause)
        near = _topic_positions(u, q, tokens, attrs=True)  # "Nutzbare Breite 1,0 m." under the topic
        if not near:
            continue
        for i, tok in enumerate(tokens):
            if (tok[:1].isdigit() or fold(tok) in _NUMWORD_SET) and any(abs(i - j) <= 12 for j in near):
                return True
    return False


def _relative_time(u: "Unit", q: "Question") -> bool:
    """'vor Nutzungsaufnahme', 'sobald …' – a time given by another event; never by the asked event
    itself ('Die Laufkarten sind vor der Abnahme vorzulegen' does not say when the Abnahme is)."""
    for m in VALUE_PATTERNS["relative"].finditer(u.text):
        event = m.group(4)
        if event and any(query_term_in(t, text_terms(event)) for t in q.terms if not t.term.isdigit()):
            continue
        return True
    return False


def _dimension_ok(u: "Unit", q: "Question") -> bool:
    """For 'Wie breit/hoch/lang/tief …?': a length in the sentence must be that dimension of the asked
    thing – 'in 60 cm Höhe' is no width, 'Rauchabschnitte von höchstens 30 m Länge' no length of the
    corridor, 'Stichflur 3. OG: 17 m' says no width. Other measures (Pa, m³/h, m²) are left alone."""
    if not q.dimension:
        return True
    text = u.text
    lengths = list(_LENGTH_VALUE.finditer(text))
    if not lengths:  # "Wie tief ist der Teich?" is not answered by "200 m³"; "Wie hoch ist der Überdruck?" by Pa
        return q.dimension == "hohe"
    rest = _LENGTH_VALUE.sub(" ", text)
    if VALUE_PATTERNS["measure"].search(rest):
        return True  # the sentence also has another measure ("15 bis 50 Pa")
    tokens = [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]
    label_dim = _dimension_of(fold(u.label or "").split()[-1]) if u.label else None
    in_sentence = {d for tok, _, _ in tokens if not tok[:1].isdigit()
                   for d in [_dimension_of(fold(tok), noun=tok[:1].isupper())] if d}
    if label_dim:
        in_sentence.add(label_dim)
    for m in lengths:
        dims, owner = set(), None
        after = [t for t in tokens if t[1] >= m.end()][:4]
        for j, (tok, _, _) in enumerate(after):
            f = fold(tok)
            if f == "uber":  # "0,5 m über Dach"
                if j + 1 < len(after) and fold(after[j + 1][0]) in _ABOVE:
                    dims.add("hohe")
                break
            if f in _DIM_SKIP:
                continue
            d = _dimension_of(f, noun=tok[:1].isupper())
            if d:
                dims.add(d)
            break
        before = [t for t in tokens if t[2] <= m.start()]
        for tok, start, end in reversed(before[-4:]):
            if re.search(r"[,;()]", text[end:m.start()]) and not re.fullmatch(r"\s*:?\s*", text[end:m.start()]):
                break
            f = fold(tok)
            if f in _DIM_SKIP:
                continue
            d = _dimension_of(f, noun=tok[:1].isupper())
            if d:
                dims.add(d)
                if f not in _DIM_WORDS:  # compound: "Einfahrtshöhe von 2,10 m" belongs to the Einfahrt
                    head = next(h for h in _DIM_NOUNS if f.endswith(h))
                    owner = f[: -len(head)]
                    owner = owner[:-1] if owner.endswith("s") and len(owner) > 4 else owner
            break
        if not dims and label_dim:
            dims.add(label_dim)
            label = fold(u.label or "").split()[-1]
            if label not in _DIM_WORDS:
                head = next((h for h in _DIM_NOUNS if label.endswith(h)), None)
                owner = label[: -len(head)] if head else None
        if dims and owner is None:  # "Rauchabschnitte von höchstens 30 m Länge" belongs to the Rauchabschnitte
            prev = [t for t in before if fold(t[0]) not in _DIM_SKIP or fold(t[0]) == "von"][-2:]
            if len(prev) == 2 and fold(prev[1][0]) == "von" and prev[0][0][:1].isupper():
                owner = fold(prev[0][0])
        if owner and owner in _MEASURE_NOUNS:
            owner = None
        if dims:
            if q.dimension in dims and (owner is None or _owner_is_topic(owner, q)):
                return True
        elif q.dimension in in_sentence:  # "Länge des notwendigen Flurs im Obergeschoss (38 m statt 30 m)"
            return True
    return False


_ABOVE = frozenset("dach gelande gelandeoberflache fussboden fussbodenoberkante okf oberkante".split())


def _owner_is_topic(owner: str, q: "Question") -> bool:
    topic = [t for t in q.terms if not t.term.isdigit()] or q.project_words
    return not topic or any(query_term_in(t, text_terms(owner)) for t in topic)


# --------------------------------------------------------------------------- chat: reading sources
_MAIL_HEADER = re.compile(r"^(Betreff|Von|An|Cc|Datum|Gesendet):\s", re.I)
_SALUTATION = re.compile(r"^(sehr\s+geehrte[rs]?|hallo|guten\s+(tag|morgen|abend)|liebe[rs]?|hi|moin|servus)\b"
                         r"[^,:!.]{0,60}[,:!]\s*", re.I)
_CLOSING = re.compile(r"^(mit\s+freundlichen\s+grüßen|freundliche\s+grüße|viele\s+grüße|beste\s+grüße|"
                      r"herzliche\s+grüße|liebe\s+grüße|gruß\b|grüße\b|mfg\b|im\s+auftrag\b|i\.\s?a\.)", re.I)
_LIST_MARK = re.compile(r"^\s*(?:[-•*–]\s+|\d{1,2}[.)]\s+)")
_LABEL = re.compile(r"^([A-ZÄÖÜ][^:]{0,60}?):\s+(?=\S)")
_SECTION_NO = re.compile(r"^\d+(\.\d+)*\s+")
_META_LABELS = frozenset({"teilnehmer", "aufgestellt", "verteiler", "projekt", "ihr zeichen", "aktenzeichen"})
_INDIRECT_QUESTION = re.compile(
    r"\b(fragt|fragen|gefragt|anfrage|rückfrage|möchte\s+wissen|wissen,|klären|unklar|ist\s+(noch\s+)?offen)\b"
    r"[^.]{0,80}?\b(ob|welche[rsnm]?|wie|wann|wo|was|wer)\b", re.I)
_REQUEST = re.compile(r"^(bitte|können\s+sie|könnten\s+sie|wir\s+bitten|ich\s+bitte)\b", re.I)
# formal lead-ins that carry no content: "Zum Bauvorhaben … nehmen wir wie folgt Stellung: …"
_PREAMBLE = re.compile(r"^[^:]{0,120}?\b(nehmen|nimmt)\s+\w+\s+wie\s+folgt\s+Stellung\s*:\s*|"
                       r"^[^:]{0,80}?\bwie\s+folgt\s*:\s*|"
                       r"^(zum|zu|für|betrifft|betreff|bzgl\.?|bezüglich)\s+(dem\s+|das\s+)?(projekt|bauvorhaben\s+)?\s*"
                       r"[A-Z]{1,4}-\d{4}-\d{3,4}\b[^:]{0,60}:\s*", re.I)


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
    label_terms: frozenset[str] = field(default_factory=frozenset)
    table: bool = False
    kind: str = "fact"  # fact | meta (participant lists …) | question (indirect) | request
    section_topic: bool = False  # set while scoring: the question's topic is this unit's section


_ORDINAL_BREAK = re.compile(r"\b(im|am|vom|zum|zur|bis|ab|der|des|dem|den|die|das|ins|beim)\s+\d{1,2}\.$", re.I)


# PDF lines with several fields in columns: "Aktenzeichen: BO-233/24    Ihr Zeichen: BS-2024-0233"
_COLUMN_GAP = re.compile(r"[ \t]{3,}(?=[A-ZÄÖÜ][^:\n]{0,40}:\s)")


def _lines(text: str) -> list[str]:
    """Lines with PDF line breaks after ordinals rejoined ('im 3.' + 'Obergeschoss …'), fields in
    columns split, runs of spaces collapsed."""
    out: list[str] = []
    for raw in _COLUMN_GAP.sub("\n", text).split("\n"):
        line = re.sub(r"[ \t]{2,}", " ", raw.strip())
        if not line:
            continue
        if out and (_ORDINAL_BREAK.search(out[-1]) or out[-1].endswith(("-", ","))) and line[:1].isalpha():
            out[-1] = f"{out[-1]} {line}" if not out[-1].endswith("-") else out[-1] + line
        else:
            out.append(line)
    return out


# a short line with a value is data, not a heading: "Gesamtbetrag 312,40 Euro", "Toner schwarz, 2 Stück"
_DATA_LINE = re.compile(r"\d[\d.,]*\s*(€|Euro\b|EUR\b|Stück\b|Stk\b|Pakete\b|m²|m³|%)|(€|EUR)\s*\d")


def _is_heading_like(line: str) -> bool:
    return len(line.split()) <= 6 and not re.search(r"[.!?:;]$", line) and ":" not in line \
        and not _DATA_LINE.search(line)


def _table_row(line: str) -> list[str] | None:
    s = line.strip()
    if not (s.startswith("|") or " | " in s):
        return None
    cells = [c.strip() for c in s.strip("|").split("|")]
    return cells if len(cells) >= 2 else None


def _header_row(cells: list[str]) -> bool:
    return all(c and len(c) <= 30 and not any(ch.isdigit() for ch in c) for c in cells)


# phrases of politeness that say nothing about the project ("Danke für die Nachfrage.", "Ich sehe mir das
# bis Ende der Woche an und melde mich …") – never quoted as an answer
_COURTESY = re.compile(
    r"^(vielen\s+|herzlichen\s+|besten\s+)?dank\w*(\s+(für|ihnen|dir|euch)\b[^,;:]*)?[.!]?$|"
    r"\b(ich|wir)\s+(sehe|sehen|schaue|schauen)\s+(mir|uns)\s+"
    r"(das|die\s+sache)\b|\bmelde\w*\s+(mich|uns)\s+(\w+\s+){0,4}?(zurück|wieder|bei\s+ihnen|mit\b|sobald|bis)|"
    r"\bfür\s+(rück)?fragen\s+(stehe|stehen)\b|\bstehe\w*\s+(ich|wir)?\s*(gern|gerne)?\s*zur\s+verfügung\b|"
    r"^(ich|wir)\s+hoffe\w*\b|\bschönes\s+wochenende\b|^(bis\s+bald|bis\s+dann)\b", re.I)
# "… ist nur 5,5 m² groß. Reicht das?" – the sentence before such a question is its premise, no fact
_PREMISE_QUESTION = re.compile(r"^(reicht|reichen|passt|passen|ist|sind|geht|genügt|genügen|funktioniert|klappt|kann|"
                               r"können|darf|dürfen|spricht|wäre|wären|müssen|muss|brauchen|braucht)\b.*\b(das|dies|es|so)\b",
                               re.I)


def read_units(src: Source) -> list[Unit]:
    """Quotable sentences of a source (see module docstring for what is left out).

    The signature (lines after the closing, or a name/organisation ending the text) is no statement;
    it becomes one unit of kind "signature" that only answers "Wer hat … unterschrieben?"."""
    units: list[Unit] = []
    context = ""
    lines = _lines(src.text)
    if src.kind == "mail":
        lines = [ln for ln in lines if not _MAIL_HEADER.match(ln)]
    sig_from = signature_start(lines)
    signature = lines[sig_from:]
    in_table = False
    for li, line in enumerate(lines[:sig_from]):
        if _CLOSING.match(line):
            if src.kind != "mail" and re.match(r"(im\s+auftrag|i\.\s?a\.)", line, re.I) and li + 1 < len(lines):
                signer = lines[li + 1].strip()  # "Im Auftrag / Jens Pfeiffer" under a permit
                if re.fullmatch(_PERSON, signer):
                    units.append(Unit(src, len(units), li + 1, signer, "Unterzeichnet im Auftrag", context))
            break
        if line.startswith("#"):
            context = _SECTION_NO.sub("", line.lstrip("#").strip())
            in_table = False
            continue
        cells = _table_row(line)
        if cells is not None:  # table row: header row skipped, others read as "label: value"
            first = not in_table
            in_table = True
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            nxt = _table_row(lines[li + 1]) if li + 1 < len(lines) else None
            if first and _header_row(cells) and (nxt is not None or li > 0):
                continue
            label, text = cells[0], ", ".join(c for c in cells[1:] if c)
            if text and label:
                units.append(Unit(src, len(units), li, text, label, context, table=True))
            continue
        in_table = False
        line = _SALUTATION.sub("", line).strip()
        line = _LIST_MARK.sub("", line).strip()
        line = _PREAMBLE.sub("", line).strip()
        if len(line) <= 3:
            continue
        if _is_heading_like(line):
            context = _SECTION_NO.sub("", line)
            continue
        label = None
        m = _LABEL.match(line)
        if m and len(m.group(1).split()) <= 5:
            label, line = m.group(1).strip(), line[m.end():].strip()
        sentences = split_sentences(line)
        if li == sig_from - 1:  # "… behalten. Benedikt Sauer, Wohnbau Stadtpark eG"
            sentences, own_signature = split_signature(sentences)
            if own_signature:
                signature = [own_signature] + signature
        line_units: list[Unit] = []
        for sent in sentences:
            if len(sent) <= 2:
                continue
            sent = sent[0].upper() + sent[1:]
            lab = label
            if lab is None and len(sent.split()) <= 3 and context:  # "Keine." under "Abweichungen"
                lab = context
            kind = "fact"
            if lab and fold(lab) in _META_LABELS:
                kind = "meta"
            elif lab and fold(lab) in ("bauvorhaben", "objekt", "grundstuck") and (
                    fold(lab) == "grundstuck" or (src.project_name and fold(sent).startswith(fold(src.project_name)))):
                kind = "meta"  # "Bauvorhaben: Industriehalle Nordring, Nordring 88" names, it does not say
            elif lab and _is_role_label(lab) and re.match(_PERSON + r"|[A-ZÄÖÜ]\w+.*\b(GmbH|eG|AG|mbH|KG)\b", sent):
                kind = "role"  # "Bauherr: Wohnbau Stadtpark eG, vertreten durch …" says who, not what
            elif sent.endswith("?") or _INDIRECT_QUESTION.search(sent):
                kind = "question"  # a question in a source is no answer (unless one asks what was asked)
            elif _COURTESY.search(sent):
                kind = "courtesy"
            elif _REQUEST.match(sent):
                kind = "request"
            line_units.append(Unit(src, len(units) + len(line_units), li, sent, lab, context, kind=kind))
        for a, b in zip(line_units, line_units[1:]):
            if a.kind == "fact" and b.kind == "question" and _PREMISE_QUESTION.match(b.text):
                a.kind = "premise"
        units += line_units
    signature = [x for x in signature if not _CLOSING.match(x)]
    if signature:
        src.signature = " ".join(signature)  # never quoted as a statement, but names the sender's role
        units.append(Unit(src, len(units), sig_from, ", ".join(signature[:2]), "Unterschrift", context,
                          kind="signature"))
    if src.kind == "mail" and src.name:  # the subject: evidence that someone wrote in this project
        subject = re.sub(r"^((AW|RE|WG|FW|Fwd)\s*:\s*)+", "", src.name, flags=re.I)
        if src.project:
            subject = re.sub(re.escape(src.project) + r"\s*", "", subject)
        if src.project_name:
            subject = re.sub(re.escape(src.project_name), "", subject)
        subject = re.sub(r"^[\s:–\-()]+|[\s:–\-()]+$", "", subject)
        if len(subject) > 3:
            units.append(Unit(src, len(units), -1, subject, "Betreff", "", kind="subject"))
    for u in units:
        u.terms = text_terms(f"{u.label or ''} {u.text}")
        u.ctx_terms = text_terms(f"{src.header} {u.context}")
        u.label_terms = text_terms(u.label or "")
    return units


def _is_role_label(label: str) -> bool:
    folded = fold(label.split()[0]) if label.split() else ""
    return any(folded.startswith(r) for r in ROLE_WORDS) or folded.startswith("brandschutzplanung")


# --------------------------------------------------------------------------- chat: the question
_WIE_ADJ = re.compile(r"\bwie\s*(lange?|hoch|groß|gross|breit|tief|weit|schwer|viele?|oft|häufig|heiß|"
                      r"heiss|warm|kalt|stark|dick|alt|teuer|klein|niedrig|schmal)\b", re.I)
_NEED_RULES = (
    (("date", "duration", "relative"), re.compile(r"\b(wann|bis\s+wann|seit\s+wann|ab\s+wann|für\s+wann)\b", re.I)),
    (("date",), re.compile(r"\ban\s+welchem\s+tag\b|\bwelche[nmrs]?\s+(datum|termin|zeitpunkt|tag|monat|woche)\b", re.I)),
    (("frequency",), re.compile(r"\bwie\s+(oft|häufig|regelmäßig)\b|\bin\s+welchen\s+abständen\b", re.I)),
    (("money",), re.compile(r"\b(kostet|kosten|teuer|preis\w*|honorar\w*|miete|mieten|mietpreis\w*|gebühr\w*|"
                            r"\w+kosten|\w+miete|\w*betrag|\w*beträge|\w*summe)\b", re.I)),
    (("count",), re.compile(r"\bwie\s*viele\b|\b(anzahl|zahl)\b", re.I)),
    (("quantity",), re.compile(r"\bwie\s*viel\b(?!e)", re.I)),
    (("duration", "measure"), re.compile(r"\bwie\s+lange?\b|\bdauer\b", re.I)),
    (("temperature",), re.compile(r"\bwie\s+(heiß|heiss|warm|kalt)\b|\btemperatur\w*\b", re.I)),
    (("weight", "measure"), re.compile(r"\bwie\s+schwer\b|\bgewicht\w*\b|\b(trägt|tragen|tragfähig\w*|"
                                       r"belastbar\w*)\b", re.I)),
    (("quantity",), re.compile(r"\bwie\s+(hoch|groß|gross|breit|tief|weit|dick|stark|alt|klein|niedrig|schmal)\b",
                               re.I)),
    (("name",), re.compile(r"\bwelche[rsnm]?\s+(hersteller|firma|unternehmen|lieferant\w*|anbieter|fachfirma|"
                           r"büro)\b|\bwie\s+hei(ß|ss)t\b|\bname[n]?\b", re.I)),
    (("standard",), re.compile(r"\b(norm|normen|din|richtlinie|vorschrift)\b", re.I)),
    (("class",), re.compile(r"\b(kategorie|klasse|feuerwiderstand\w*|baustoffklasse|risikoklasse)\b", re.I)),
    (("color",), re.compile(r"\bfarbe\w*\b", re.I)),
    (("language",), re.compile(r"\bsprache[n]?\b", re.I)),
    (("name",), re.compile(r"^\s*wer\s+(ist|sind|war|waren)\s+(der|die|das|unser\w*|ihr\w*)?\s*[A-ZÄÖÜ]", re.I)),
    # "Welcher Prüfsachverständige nimmt … ab?" asks for the person, not for the role word
    (("name",), re.compile(r"\bwelche[rsnm]?\s+(\w+\s+)?(prüfsachverständig\w*|sachverständig\w*|sachkundig\w*|"
                           r"prüfingenieur\w*|prüfer\w*|errichter\w*|architekt\w*|fachplaner\w*|planer\w*|"
                           r"ingenieur\w*|person\w*|mitarbeiter\w*|ansprechpartner\w*|brandschutzbeauftragt\w*|"
                           r"bauleiter\w*)\b", re.I)),
    (("agent",), re.compile(r"^\s*(wer|von\s+wem|durch\s+wen)\b|\b(von|durch)\s+wem\b|\bwen\b", re.I)),
)
# words that name the asked attribute; they are answered by a value, not by the word itself
_ATTRIBUTE_WORDS = frozenset(stem(fold(w)) for w in """
    kosten preis preise honorar miete gebühr gebühren anzahl zahl datum termin zeitpunkt temperatur gewicht
    farbe hersteller firma unternehmen lieferant anbieter norm normen richtlinie vorschrift kategorie klasse
    sprache sprachen name höhe breite länge tiefe größe dauer menge fläche betrag summe
    """.split())
# "Wann wurde die BMA abgenommen?" asks when something happened: a date of the event, not an obligation
# ("ist vor der Abnahme vorzulegen") or a plan ("ist für März vorgesehen")
_PAST_AUX = re.compile(r"\b(wurde|wurden|war|waren|worden)\b", re.I)
_PERFECT_AUX = re.compile(r"\b(hat|haben|hatte|hatten|ist|sind)\b", re.I)
_FUTURE_Q = re.compile(r"\b(wird|werden|soll|sollen|sollte|muss|müssen|geplant|vorgesehen|voraussichtlich|"
                       r"bald|demnächst|künftig)\b", re.I)
_SEP_FOLDED = r"(?:ab|an|auf|aus|bei|dar|durch|ein|fest|frei|her|hin|mit|nach|nieder|sicher|bereit|vor|weg|zuruck|zusammen|um|uber)"
_ZU_INFINITIVE = re.compile(r"^" + _SEP_FOLDED + r"zu\w{3,}en$")
_PARTICIPLE_FORM = re.compile(r"^" + _SEP_FOLDED + r"?ge\w{2,}(t|et|en)$|^\w{3,}iert$|"
                              r"^(be|er|ver|zer|ent|emp|uber|unter|miss|hinter|wider)\w{2,}(t|en)$")
_WHEN_Q = re.compile(r"^\s*(wann|seit\s+wann|an\s+welchem\s+(tag|datum)|welche[nmrs]?\s+(datum|tag|zeitpunkt))\b", re.I)
_SEP = r"(?:ab|an|auf|aus|bei|dar|durch|ein|fest|frei|her|hin|mit|nach|nieder|sicher|bereit|vor|weg|zurück|zusammen|um|über)"
# not done (yet): an obligation, a plan or an announcement
_NOT_DONE = re.compile(
    r"\b(muss|müssen|soll|sollen|sollte|sollten|geplant|vorgesehen|voraussichtlich|beabsichtigt|angesetzt|terminiert|"
    r"anvisiert|erforderlich|notwendig|vorzusehen|vorzulegen)\b|"
    r"\b(ist|sind|war|waren)\b(?:\s+\S+){0,14}?\s+(?:zu\s+[a-zäöüß]\w{2,}(?:en|ern|eln)|" + _SEP + r"zu[a-zäöüß]{3,}(?:en|ern|eln))\b|"
    r"\b(wird|werden)\s+(?:\S+\s+){0,6}?(am|im|ab|bis|zum|zur|in\s+KW|nach)\s+(\d|KW|Januar|Februar|März|April|Mai|"
    r"Juni|Juli|August|September|Oktober|November|Dezember)", re.I)
# dimensions of a length – "Wie breit ist die Treppe?" is not answered by "in 60 cm Höhe"
_DIM_ADJ = {"breit": "breite", "schmal": "breite", "hoch": "hohe", "hoh": "hohe", "niedrig": "hohe", "lang": "lange",
            "lange": "lange", "tief": "tiefe"}
_DIM_WORDS = {"breite": "breite", "breit": "breite", "schmal": "breite", "hohe": "hohe", "hoch": "hohe",
              "lange": "lange", "lang": "lange", "entfernung": "lange", "weite": "lange", "abstand": "lange",
              "tiefe": "tiefe", "tief": "tiefe"}
_DIM_NOUNS = ("breite", "hohe", "lange", "tiefe", "entfernung")
_DIM_SKIP = frozenset("""mindestens hochstens maximal max ca etwa rund je jeweils nur insgesamt circa knapp genau lichte lichten
    lichter lichtes nutzbare nutzbaren nutzbarer freie freien freier gesamte gesamten von mit einer einem eines ein eine
    der die das des dem den bei auf""".split())
_MEASURE_NOUNS = frozenset("mass masse mindestmass hochstmass wert werte grenzwert hochstwert mindestwert".split())
_LENGTH_VALUE = re.compile(r"\d+(?:[.,]\d+)?(?:\s*(?:[–-]|bis)\s*\d+(?:[.,]\d+)?)?\s*(?:m|cm|mm|km)(?![\w²³/])")
_CIRCUMSTANCE = re.compile(r"\b(bei|im)\s+(einem\s+|einer\s+)?(Brand|Feuer|Brandfall|Notfall|Ernstfall|Alarm|Feueralarm|"
                           r"Brandalarm|Alarmfall)\b", re.I)
_SIGNER_Q = re.compile(r"\b(unterschrieben|unterschreibt|unterzeichnet\w*|signiert|gezeichnet|unterschrift\w*)\b|"
                       r"\bvon\s+wem\s+(stammt|stammen|ist|kommt)\b|\bwer\s+hat\b.*\b(verfasst|aufgestellt|erstellt)\b", re.I)
_DECISION_Q = re.compile(r"\b(zugestimmt|zustimm\w*|stimmt\w*\b.*\bzu\b|zustimmung|abgelehnt|ablehn\w*|lehnt\w*\b.*\bab\b|"
                         r"ablehnung|genehmigt|genehmigen|freigegeben|freigeben|freigabe|zugelassen|zulassen|"
                         r"einverstanden|akzeptiert|akzeptier\w*|mitgetragen|erlaubt|entschieden|entscheid\w*)\b", re.I)
# "Ist der Mangel behoben?", "Wurde die Anlage abgenommen?": asks for the current state
_COMPLETION = re.compile(r"\b(behoben|beseitigt|erledigt|abgeschlossen|umgesetzt|nachgeholt|fertig\w*|erfolgt|"
                         r"abgenommen|eingebaut|installiert|erteilt|vergeben|bestellt|beauftragt)\b", re.I)
_DECISION_STATED = re.compile(r"\b(stimme\w*\b.*\bzu\b|zugestimmt|zustimmung|abgelehnt|lehne\w*\b.*\bab\b|ablehnung|"
                              r"nicht\s+zustimm\w*|genehmigt|freigegeben|freigabe|mitgetragen|einverstanden|akzeptiert|"
                              r"erteilt|zugelassen|keine\s+bedenken|unzulässig|zulässig)\b", re.I)
_CONDITIONAL = re.compile(r"\b(wenn|sofern|falls|vorbehaltlich|nach\s+vorlage|entschieden|beantragt|beantragen|"
                          r"vorbereiten|vorgeschlagen)\b", re.I)
_COMMUNICATION = re.compile(r"\b(geschrieben|schreibt|schrieb|gesagt|sagt|sagte|mitgeteilt|teilt|gemeldet|meldet|"
                            r"angemerkt|merkt|gefordert|fordert|verlangt|verlangen|geantwortet|antwortet|berichtet|"
                            r"geäußert|äußert|stellung|meint|gemeint|empfiehlt|empfohlen|rückmeldung|geschickt|"
                            r"schickt|gesendet|verschickt|wünscht|möchte|will|wollte|gefragt|fragt|fragte|bittet|"
                            r"bat|gebeten|stammt|stammen|vorgeschlagen|schlägt)\b", re.I)
# words for the source itself ("Von wem stammt die Mail …?") – answered by the kind of source
_SOURCE_WORDS = {"mail": ("mail", "email", "nachricht", "schreib", "brief", "anschreib"),
                 "attachment": ("anhang", "anlag", "datei")}
_W_QUESTION = re.compile(r"^\s*(welche\w*|wie|was|wann|wer|wem|wen|wo|wohin|woher|womit|wofür|woraus|worauf|"
                         r"warum|weshalb|wieso|in\s+welche\w*|bis\s+wann|seit\s+wann|nach\s+welche\w*|"
                         r"an\s+welche\w*|für\s+welche\w*|bei\s+welche\w*|von\s+wem|durch\s+wen|über\s+welche\w*)\b",
                         re.I)
_OPEN_POINTS = re.compile(r"\b(offen\w*|ungeklärt|unklar|klären|rückfrage\w*|frage[n]?|gefragt|fragt\w*|wissen|"
                          r"anfrage\w*|nachfrage\w*)\b", re.I)
_PARTICIPANTS = re.compile(r"\b(teilnehm\w*|teilgenommen|nahm\w*\b.*\bteil|nehmen\b.*\bteil|anwesend|dabei|"
                           r"beteiligt\w*)\b", re.I)
# documents written by a role (for "Was hat der Prüfingenieur festgestellt?")
_DOC_ROLES = {"prufbericht": "prufingenieur", "baugenehmigung": "bauaufsicht", "stellungnahm": "feuerwehr"}
CONTEXT_CREDIT = 0.8  # a word found only in the source header/section counts a little less
MIN_COVERAGE = 0.7  # share of the (weighted) topic words a sentence must cover
TABLE_PENALTY = 0.3  # a terse table cell ("8 h") is used only if no sentence says it as well
MAX_SENTENCES = 3
MAX_PROJECTS = 10


@dataclass
class Question:
    text: str
    terms: list[QueryTerm]  # topic words (without project names and attribute words)
    types: set[str]  # value types of which the answer must contain one ("date", "count", …)
    temporal: bool
    enumeration: bool
    project_question: bool
    projects: set[str]  # projects named in the question
    attrs: list[QueryTerm] = field(default_factory=list)  # words naming the asked attribute
    soft: set[str] = field(default_factory=set)  # preferred value types ("reason")
    decision: bool = False
    roles: list[QueryTerm] = field(default_factory=list)  # role words of an authorship question
    authorship: bool = False
    summary: bool = False
    open_points: bool = False
    participants: bool = False
    communication: bool = False  # "Was hat X geschrieben/gesagt/gefordert?", "Wer hat … geschickt?"
    prefer: str | None = None  # preferred value type when several are accepted ("wie lange" → duration)
    manner: bool = False  # "Wie wird/werden … <verb>?" – asks for the way something is done
    yesno: bool = False  # "Hat …?", "Ist …?", "Wurde …?"
    past_event: bool = False  # "Wann wurde … abgenommen?" – the date it happened
    completion: bool = False  # "Ist die Anlage schon abgenommen?" – was it done?
    dimension: str | None = None  # asked dimension of a length ("breite", "hohe", "lange", "tiefe")
    signer: bool = False  # "Wer hat … unterschrieben?"
    project_words: list[QueryTerm] = field(default_factory=list)  # words naming the project ("Tiefgarage")
    located: set[str] = field(default_factory=set)  # topic nouns named as a place ("im Sicherheitstreppenraum")


TYPE_WORD_WEIGHT = 0.5  # the building type in front of a project's own name


def _project_names(sources: list[Source]) -> dict[str, dict[str, float]]:
    """Stems of the project names (without numbers) per project, from the source headers, with their
    weight as evidence for the project: the building type in front of the name ("Tiefgarage"
    Marktplatz, "Wohnhochhaus" Am Stadtpark) is an ordinary word of other projects too and counts
    half; the name itself ("Marktplatz", "Hafenkante") counts fully."""
    out: dict[str, dict[str, float]] = {}
    for s in sources:
        if s.project:
            stems = [w for w in content_terms(s.project_name or "") if not w.isdigit()]
            names = out.setdefault(s.project, {})
            for i, w in enumerate(stems):
                weight = TYPE_WORD_WEIGHT if i == 0 and len(stems) > 1 else 1.0
                names[w] = max(names.get(w, 0.0), weight)
    return out


def _names_project(term: QueryTerm, names: dict[str, float]) -> float:
    """How strongly a question word names the project ("Halle" → Industriehalle, "Heim" → Pflegeheim,
    "Hochhauses" → Wohnhochhaus); 0 for a word that only shares a stem ("Schüler" ≠ Grundschule)."""
    if term.term.isdigit():
        return 0.0
    inflected = term.word[len(term.term):] in ("", "e", "es", "s", "n", "en", "ern")
    best = 0.0
    for w, weight in names.items():
        if term.term == w or term.word == w or (
                len(term.term) >= 4 and inflected and (w.endswith(term.term) or w.startswith(term.term))):
            best = max(best, weight)
    return best


def _is_role(t: QueryTerm) -> bool:
    return role_key(t) is not None


def analyse_question(question: str, sources: list[Source], synonyms: Synonyms | None = None) -> Question:
    shaping = {stem(fold(m.group(1))) for m in _WIE_ADJ.finditer(question)}  # "wie lange", "wie hoch"
    all_terms = question_terms(question, synonyms if synonyms is not None else active_synonyms(),
                               drop=frozenset(TEMPORAL_WORDS))
    # "bei einem Brand", "im Brandfall": the circumstance, not the topic, of a fire-safety question
    circumstance = {stem(fold(m.group(3))) for m in _CIRCUMSTANCE.finditer(question)}
    all_terms = [replace(t, weak=True) if t.term in circumstance else t for t in all_terms]
    enumeration = is_enumeration_question(question)
    folded_q = fold(question)
    named: set[str] = set()
    project_terms: set[str] = set()
    matches: dict[str, tuple[float, set[str]]] = {}
    for project, names in _project_names(sources).items():
        number_words = set(words(project))
        score, hits = 0.0, set()
        if fold(project) in folded_q:  # the full project number, e.g. "BS-2024-0137"
            score += 10
            hits.update(t.term for t in all_terms if t.term in number_words or t.word in number_words)
        weights = {t.term: _names_project(t, names) for t in all_terms}
        found = {term for term, weight in weights.items() if weight}
        score += sum(weights.values())
        if score:
            matches[project] = (score, hits | found)
    if matches:  # "die Tiefgarage der Hafenkante": Hafenkante (a name), not "Tiefgarage Marktplatz" (a type)
        top = max(score for score, _ in matches.values())
        named = {p for p, (score, _) in matches.items() if score == top}
        if len(named) == 1:
            project_terms.update(matches[next(iter(named))][1])
        # several projects named equally ("die Tiefgarage im Wohnhochhaus"): which word names the project
        # and which the thing asked about is open – the words stay topic words, an answer must contain them
    types: set[str] = set()
    prefer = None
    for kinds, rx in _NEED_RULES:
        if rx.search(question):
            types.update(kinds)
            prefer = prefer or kinds[0]
    if "money" in types:
        types.discard("quantity")
    if "name" in types:
        types.discard("agent")  # "Wer ist der Hausmeister?" asks for a name, not for a role word
    if "count" in types and re.search(r"\bwie\s*viele\b", question, re.I):
        types.discard("quantity")
    terms: list[QueryTerm] = []
    attrs: list[QueryTerm] = []
    # numbers of a value in a yes/no question ("Reicht ein Vorraum von 5,5 m²?") are the premise to check, not
    # the topic (in "In welchem Projekt ist der Teich 200 m³ groß?" they are)
    value_digits = {d for k in ("measure", "money", "duration", "weight", "temperature")
                    for m in VALUE_PATTERNS[k].finditer(question) for d in re.findall(r"\d+", m.group(0))}
    for t in all_terms:
        if not enumeration and t.term in project_terms:
            continue  # project names identify the project – they are no answer to the question
        if t.term.isdigit() and t.term in value_digits and not _W_QUESTION.match(question):
            continue
        head = attribute_head(t.word) if t.noun else None
        if t.term in shaping or t.word in shaping:
            attrs.append(replace(t, weak=True))
        elif t.term in _ATTRIBUTE_WORDS or (head and not head[0]):
            attrs.append(t)
            if head:
                types.add(head[1])
        else:
            if t.attr and _W_QUESTION.match(question):  # "Wie hoch ist die Löschwassermenge?" – not
                types.add(t.attr)  # "Hat sie der längeren Flurlänge zugestimmt?"
            terms.append(t)
    if "money" in types:
        types.discard("quantity")
    roles = [t for t in terms if _is_role(t)]
    temporal = is_temporal_question(question) or (not _W_QUESTION.match(question) and bool(_COMPLETION.search(question)))
    from_role = any(re.search(r"\b(von|vom)\s+(der|dem|den|unserem|unserer|einem|einer)?\s*" + re.escape(t.word),
                              folded_q) for t in roles)
    authorship = bool(roles) and bool(_COMMUNICATION.search(question) or temporal or from_role)
    decision = bool(_DECISION_Q.search(question))
    summary = is_summary_question(question) and not types
    if is_project_question(question):  # "Welches Gebäude …", "Bei welcher Baustelle …": the kind of project
        terms = [t for t in terms if t.term not in ("gebaud", "baustell", "objekt", "vorhab")]
    if terms and all(t.attributive for t in terms):  # "Welche offenen Punkte …?": the adjective of a
        terms = []  # filler noun is no topic of its own
    if summary:  # "Fasse … zusammen", "Worum geht es …": the words only shape the question
        terms = [t for t in terms if not SUMMARY_WORDS.match(t.word) and not SUMMARY_WORDS.match(t.term)]
    soft = {"reason"} if re.search(r"\b(warum|weshalb|wieso|wozu|aus\s+welchem\s+grund)\b", question, re.I) else set()
    past_event = _past_event_question(question)
    if past_event:  # "Wann wurde … abgenommen?" – a date, not "vor der Abnahme" or a duration
        types -= {"duration", "relative"}
        types.add("date")
        prefer = "date"
    yesno = not _W_QUESTION.match(question)
    dimension = None
    m = _WIE_ADJ.search(question)
    if m and fold(m.group(1)) in _DIM_ADJ:
        dimension = _DIM_ADJ[fold(m.group(1))]
    for t in attrs:  # "Welche Breite hat …?", "Flurlänge"
        dim = _dimension_of(t.word, noun=t.noun)
        if dim and not dimension:
            dimension = dim
    return Question(question, terms, types, temporal, enumeration, is_project_question(question),
                    named if not enumeration else set(), attrs=attrs, soft=soft, decision=decision,
                    roles=roles, authorship=authorship, summary=summary,
                    open_points=bool(_OPEN_POINTS.search(question)), participants=bool(_PARTICIPANTS.search(question)),
                    communication=bool(_COMMUNICATION.search(question) or re.search(r"\bvorgeschlagen\b|\bschlägt\b",
                                                                                     question, re.I)),
                    prefer=prefer if len(types) > 1 else None,
                    manner=bool(re.match(r"\s*wie\s+(wird|werden|wurde|wurden)\b", question, re.I)) and not types,
                    yesno=yesno, past_event=past_event,
                    completion=yesno and bool(_COMPLETION.search(question)) and not _FUTURE_Q.search(question),
                    dimension=dimension, signer=bool(_SIGNER_Q.search(question)),
                    project_words=[t for t in all_terms if t.term in project_terms and not t.term.isdigit()],
                    located=_located(question, terms))


_PLACE_PREP = frozenset("im in ins am an beim bei auf aus vom von zum zur unter uber neben hinter".split())
_ARTICLES = frozenset("der die das den dem des ein eine einen einem einer eines".split())


def _located(question: str, terms: list[QueryTerm]) -> set[str]:
    """Topic nouns the question names as the place or owner of the asked thing: "im
    Sicherheitstreppenraum", "in der obersten Etage", "beim Feuerwehraufzug"."""
    toks = words(question)
    out: set[str] = set()
    for i, w in enumerate(toks):
        j = i - 1
        while j >= 0 and i - j <= 3 and (toks[j] in _ARTICLES or toks[j].endswith(("e", "en", "er", "es", "em"))) \
                and toks[j] not in _PLACE_PREP:
            j -= 1  # article and adjectives between preposition and noun
        if j < 0 or toks[j] not in _PLACE_PREP:
            continue
        out.update(t.term for t in terms if t.noun and not t.term.isdigit() and (w == t.word or stem(w) == t.term))
    return out


def _past_event_question(question: str) -> bool:
    """'Wann wurde/wurden …?', 'Wann ist … abgenommen worden?', 'Wann hat … erteilt?' – not 'Wann wird …?',
    'Wann ist die Abnahme?' or 'Wann soll … ?'."""
    if not _WHEN_Q.match(question) or _FUTURE_Q.search(question):
        return False
    if _PAST_AUX.search(question):
        return True
    raw = re.findall(r"[^\W\d_]+", question)
    last = fold(raw[-1]) if raw and raw[-1][:1].islower() else ""
    if not last or re.search(r"\bzu\s+\w+\W*$", question) or _ZU_INFINITIVE.match(last):
        return False  # "Wann sind die Pläne zu übergeben / vorzulegen?" – an obligation
    return bool(_PERFECT_AUX.search(question)) and bool(_PARTICIPLE_FORM.match(last))


def _dimension_of(word: str, *, noun: bool = True) -> str | None:
    """Dimension a folded word names: 'breite'/'breit' → breite, 'einfahrtshohe' → hohe (compound head)."""
    if word in _DIM_WORDS:
        return _DIM_WORDS[word]
    if noun:
        for head in _DIM_NOUNS:
            if word.endswith(head) and len(word) - len(head) >= 3:
                return _DIM_WORDS[head]
    return None


# --------------------------------------------------------------------------- chat: people and roles
_PERSON = r"(?:(?:Dipl\.-Ing\.|Dr\.|Prof\.)\s+)?[A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß\-]+){1,2}"
_ROLE_PAREN = re.compile(r"(" + _PERSON + r")\s+\(([A-ZÄÖÜ][\w\- ]{2,40})\)")
_REPRESENTED = re.compile(r"vertreten\s+durch\s+(" + _PERSON + r")")


def role_aliases(units: dict[int, list[Unit]]) -> dict[str, set[str]]:
    """Surnames per role stem, read from the sources: 'Renate Albers (Bauherr)', 'Bauherr: …,
    vertreten durch Renate Albers', 'Prüfingenieur: Dipl.-Ing. Sabine Wendehals, …'."""
    out: dict[str, set[str]] = {}

    def add(role: str, person: str) -> None:
        r = stem(fold(role.split()[0]))
        if r.startswith("brandschutzdienststell"):
            r = "feuerwehr"
        if any(r.startswith(x) for x in ROLE_WORDS):
            out.setdefault(r, set()).add(fold(person.split()[-1]))

    seen: set[int] = set()
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
    for source_units in units.values():
        src = source_units[0].source if source_units else None
        if src is None or not src.sender or src.n in seen:
            continue
        seen.add(src.n)
        person = [w for w in src.sender.split() if w[:1].isupper() and not w.endswith(".")]
        if not person:
            continue
        for w in words(f"{src.sender} {src.signature}"):  # "Prüfingenieurin für Brandschutz", "Feuerwehr …"
            role = "feuerwehr" if _FIRE_RANK.match(w) else stem(w)
            add(role, person[-1])
    return out


# ranks of the fire service in a sender's name: Brandoberinspektor, Brandamtsrätin, Stadtbrandmeister
_FIRE_RANK = re.compile(r"^(brand(ober|haupt|amts|meister|inspektor|rat|direktor)|\w*brandmeister|wehrfuhrer)")


def known_names(sources: list[Source], units: dict[int, list[Unit]]) -> frozenset[str]:
    """Surnames of the people in the sources (senders, participants, representatives)."""
    names: set[str] = set()
    for s in sources:
        if s.sender:
            parts = [w for w in words(s.sender) if len(w) >= 3]
            if parts:
                names.add(parts[-1])
    for us in units.values():
        for u in us:
            for m in _ROLE_PAREN.finditer(u.text):
                names.add(fold(m.group(1).split()[-1]))
            for m in _REPRESENTED.finditer(u.text):
                names.add(fold(m.group(1).split()[-1]))
    return frozenset(n for n in names if len(n) >= 3)


def _authored(role: QueryTerm, src: Source, aliases: dict[str, set[str]]) -> bool:
    """Was the source written by the role or person ('Mail von Renate Albers' for the Bauherr,
    'Stellungnahme Brandschutzdienststelle' for the Feuerwehr, a Prüfbericht for the Prüfingenieur,
    a mail by Sabine Wendehals for "Frau Wendehals")?"""
    key = role_key(role) or role.term
    if src.sender:
        sender = set(words(src.sender))
        if role.word in sender or role.term in sender:
            return True
        for r, names in aliases.items():
            if (r.startswith(key) or key.startswith(r)) and names & sender:
                return True
    name_terms = text_terms(f"{src.name} {src.sender or ''}")
    if query_term_in(role, name_terms):
        return True
    folded = fold(src.name)
    return any(doc in folded and (key.startswith(r) or r.startswith(key)) for doc, r in _DOC_ROLES.items())


# --------------------------------------------------------------------------- chat: scoring
@dataclass
class Scored:
    unit: Unit
    coverage: float
    inline: bool
    score: float
    matched: frozenset[str]
    types: frozenset[str]
    authored: bool = False
    answerable: bool = False


BRIDGE_CREDIT = 0.5  # a place the sentence does not name, but another sentence ties its subject to
BRIDGE_GAP = 3  # at most this many words between the thing and the place ("Druckbelüftungsanlage hält im STR")


def _unmatched_place(q: Question, credit: dict[str, float]) -> QueryTerm | None:
    """The one topic noun a sentence misses when it is a place of the question ("im
    Sicherheitstreppenraum") and every other topic noun is in the sentence itself."""
    nouns = [t for t in q.terms if t.noun and not t.weak and not t.term.isdigit()]
    missing = [t for t in nouns if t.term not in credit]
    if len(missing) != 1 or len(nouns) < 2 or missing[0].term not in q.located:
        return None
    return missing[0] if all(credit[t.term] >= 1.0 for t in nouns if t is not missing[0]) else None


def _bridged(place: QueryTerm, u: Unit, q: Question, pool: list[Unit]) -> bool:
    """Does another sentence of the same project put a thing named in ``u`` at the asked place? "Die
    Auslegung der Druckbelüftungsanlage ist nachvollziehbar; die Türöffnungskraft von 100 N wird
    eingehalten." is about the Sicherheitstreppenraum when the concept says "Die Druckbelüftungsanlage
    hält im Sicherheitstreppenraum …" or a permit "Sicherheitstreppenraum mit Druckbelüftungsanlage"."""
    nouns = words(" ".join(re.findall(r"\b[A-ZÄÖÜ][^\W\d_]{5,}", u.text)))  # "Druckbelüftungsanlage", not "N"
    things = {stem(w) for w in nouns if w not in STOPWORDS and w not in QUERY_FILLER
              and not any(query_term_in(t, text_terms(w)) for t in q.terms)}  # not the asked thing itself
    if not things:
        return False
    for v in pool:
        if v is u or v.kind not in ("fact", "role") or _key(v.text) == _key(u.text):
            continue
        toks = words(v.text)
        at_place = [i for i, w in enumerate(toks) if query_term_in(place, text_terms(w))]
        if not at_place:
            continue
        at_thing = [i for i, w in enumerate(toks) if stem(w) in things]
        if any(abs(i - j) <= BRIDGE_GAP + 1 for i in at_place for j in at_thing):
            return True
    return False


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


def _attr_in(t: QueryTerm, u: Unit) -> bool:
    """An attribute word in the sentence or its label ('Höhe: 68 m', 'Gebäudehöhe 21,5 m')."""
    return query_term_in(t, u.terms)


def _quotable(q: Question, u: Unit) -> bool:
    if u.kind == "meta":
        return (q.participants and fold(u.label or "") in ("teilnehmer", "verteiler")) or any(
            query_term_in(t, u.label_terms) for t in q.terms + q.attrs)
    if u.kind == "question":
        return q.open_points
    if u.kind == "courtesy":
        return False  # "Danke für die Nachfrage." answers nothing
    if u.kind == "signature":  # "Brandoberinspektor Uwe Kessler, Feuerwehr Musterstadt, Vorbeugender Brandschutz"
        return q.signer or bool(q.types & {"name", "agent"})  # says who signed – and who someone is
    if u.kind == "role":  # "Bauherr: …, vertreten durch …" – who someone is
        return bool(q.types & {"name", "agent"}) or q.participants or q.signer
    if u.kind == "premise":  # "… nur 5,5 m² groß. Reicht das?" – what was asked, not a fact
        return q.open_points or q.authorship or q.communication
    if u.kind == "subject":
        return q.enumeration  # only as evidence of an authored mail (see score_units)
    if u.kind == "request":  # "Bitte … so planen, dass die Rettungswege frei bleiben."
        return q.authorship or q.temporal or q.communication or (
            bool(q.terms) and all(query_term_in(t, u.terms) for t in q.terms if t.noun))
    return True


def score_units(q: Question, sources: list[Source], units: dict[int, list[Unit]]) -> list[Scored]:
    aliases = role_aliases(units)
    names = known_names(sources, units)
    idf = _idf(q, sources, units)
    weights = {t.term: t.weight for t in q.terms}
    total = sum(weights.values())
    # parties and people named in the question must be the speaker or be named in the sentence:
    # "Hat die Feuerwehr … zugestimmt?" is not answered by the Prüfingenieur's consent
    essential = {t.term for t in q.terms if t.noun and t.word in names}
    if q.decision or q.authorship:
        essential |= {t.term for t in q.roles}
    if q.types & {"date", "agent", "name"} or q.manner:
        # "Wann wird X abgenommen?" – the date of the Abnahme; "Wer prüft X?" – the one who checks;
        # "Wie werden die Gäste alarmiert?" – the way of alarming (a communication verb is the sender's part)
        essential |= {t.term for t in q.terms if _verb_like(t)
                      and not (q.communication and _COMMUNICATION.search(t.word))}
    source_terms = {t.term: kind for t in q.terms for kind, stems in _SOURCE_WORDS.items()
                    if t.term in stems or t.word in stems}
    month_terms = {t.term: _MONTH_NO[t.word] for t in q.terms if t.word in _MONTH_NO}
    q_dates = _question_dates(q.text)
    year_terms = {t.term: int(t.term) for t in q.terms if re.fullmatch(r"(19|20)\d\d", t.term)}
    section_q = bool(q.terms) and all(t.noun for t in q.terms) and not q.types and bool(
        re.match(r"\s*(welche[nrs]?|was\s+steht|was\s+sagt|was\s+ist|wie\s+(ist|lautet|war))\b", q.text, re.I))
    project_units: dict[str, list[Unit]] = {}
    for s in sources:
        if s.project:
            project_units.setdefault(s.project, []).extend(units[s.n])
    out: list[Scored] = []
    for s in sources:
        if q.projects and s.project not in q.projects:
            continue
        authored = {t.term for t in q.roles if _authored(t, s, aliases)}
        if s.sender:  # a person named in the question wrote this mail
            sender = set(words(s.sender))
            authored |= {t.term for t in q.terms if t.noun and (t.word in sender or t.term in sender)}
        for u in units[s.n]:
            if not _quotable(q, u) or (u.kind == "subject" and not authored):
                continue
            credit: dict[str, float] = {}
            inline = False
            for t in q.terms:
                c = query_term_credit(t, u.terms)
                if t.term in authored:
                    c = 1.0  # "Was hat der Bauherr geschrieben?": the Bauherr's own mail
                elif source_terms.get(t.term) == s.kind:
                    c = 1.0  # "die Mail zur Abnahme" – this source is a mail
                elif source_terms.get(t.term) == "mail" and s.kind == "attachment":
                    c = max(c, CONTEXT_CREDIT)  # … or came with one
                elif s.date and (month_terms.get(t.term) == s.date.month or year_terms.get(t.term) == s.date.year
                                 or (t.term.isdigit() and any(s.date == d and int(t.term) in (d.day, d.month, d.year)
                                                              for d in q_dates))):
                    c = max(c, 1.0)  # "im Februar 2025", "am 15.10.2024" – the date of the source
                if c > 0:
                    credit[t.term] = c
                    if c >= 1.0 and t.weight >= 0.4:
                        inline = True
                    continue
                if q.decision and not t.noun and _DECISION_Q.search(t.word) and _DECISION_STATED.search(u.text):
                    credit[t.term] = 1.0  # "zugestimmt?" is answered by "abgelehnt" as well
                elif query_term_in(t, u.ctx_terms):
                    credit[t.term] = CONTEXT_CREDIT
            place = _unmatched_place(q, credit) if inline else None
            if place is not None and _bridged(place, u, q, project_units.get(s.project, [])):
                credit[place.term] = BRIDGE_CREDIT  # the sentence is about a thing the sources put there
            coverage = sum(weights[k] * c for k, c in credit.items()) / total if total else 0.0
            if section_q and not inline and u.context and all(query_term_in(t, u.ctx_terms) for t in q.terms) \
                    and any(query_term_in(t, text_terms(u.context)) for t in q.terms):
                u.section_topic = True  # "Welche Kennwerte hat …?" – the rows of that section
                coverage = max(coverage, MIN_COVERAGE)
            types = set(k for k in q.types if _has_type(k, u, q, names))
            if (s.sender and q.communication or u.kind == "signature") and q.types & {"agent", "name"}:
                types |= q.types & {"agent", "name"}  # "Wer hat … geschickt/unterschrieben?" – sender, signer
            if authored and s.date and s.kind != "document" and "date" in q.types:
                types.add("date")  # "Wann hat Frau Wendehals den Antrag abgelehnt?" – the date of her mail
            types = frozenset(types)
            soft = frozenset(k for k in q.soft if VALUE_PATTERNS[k].search(u.text))
            attr_hit = any(_attr_in(t, u) for t in q.attrs)
            score = sum(weights[k] * c * (1 + idf[k]) for k, c in credit.items())
            score += _type_bonus(q, types) + 0.8 * len(soft) + _bigram_bonus(q, u) + 0.4 * attr_hit
            score += 0.5 * sum(1 for t in q.terms if u.label and query_term_in(t, u.label_terms))
            if q.types & {"name", "agent"} and _has_name(u.text, names):
                score += 0.5  # a named person/company beats a role word
            if q.decision:
                score += 1.0 * bool(_DECISION_STATED.search(u.text)) - 1.0 * bool(_CONDITIONAL.search(u.text))
            if authored:
                score += 1.0
            if u.kind == "subject":
                score -= 1.5  # only when the mail says nothing else
            score -= 0.001 * len(u.text) + (TABLE_PENALTY if u.table else 0.0)  # among equals: precise prose
            sc = Scored(u, coverage, inline, score, frozenset(credit), types | soft, bool(authored))
            sc.answerable = _answerable(q, sc, attr_hit) and all(
                credit.get(t, 0) >= 1.0 for t in essential)
            out.append(sc)
    return out


def _question_dates(text: str) -> list[date]:
    out = []
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text):
        try:
            out.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            continue
    return out


_ADJECTIVE_SUFFIX = ("ig", "lich", "bar", "isch", "sam", "haft", "los", "voll", "frei", "fest")


def _verb_like(t: QueryTerm) -> bool:
    """A lower-case content word that is a verb form ("abgenommen", "prüft", "kompensieren"), not an
    adjective ("zuständig", "nichtbrennbar") – the action a question asks about."""
    if t.noun or t.attributive or t.weak or t.term.isdigit():
        return False
    return not t.word.endswith(_ADJECTIVE_SUFFIX) and bool(re.search(r"(en|ern|eln|t|et)$", t.word))


def _type_bonus(q: Question, types: frozenset[str]) -> float:
    """1.0 for a value of the preferred type ("wie lange" → a duration before a length), 0.5 for others."""
    if not types:
        return 0.0
    return 1.0 if q.prefer in types or not q.prefer else 0.5


def _answerable(q: Question, sc: Scored, attr_hit: bool) -> bool:
    """Fail closed: the sentence must cover the topic, and a question for a value must find one;
    a decision question needs a sentence that states a decision."""
    if q.types and not (sc.types & q.types):
        # two questions in one ("Warum … und wie breit …?"): a sentence may answer the other part
        if not (re.search(r"\bund\s+(wie|wann|wo|wer|was|warum|weshalb|welche\w*)\b", q.text, re.I) and sc.types & q.soft):
            return False
    if q.decision and (q.yesno or re.search(r"\bentschied|\bentscheid", q.text, re.I)) \
            and not _DECISION_STATED.search(sc.unit.text):
        return False
    if (q.completion or q.past_event) and _NOT_DONE.search(sc.unit.text) and sc.unit.kind != "signature":
        return False  # an obligation or a plan does not say that it was done
    if not q.terms:  # "Wie hoch ist das Wohnhochhaus?" – a value of the project itself, not of its sensors
        return bool(q.attrs) and attr_hit and bool(q.types) and (
            not q.project_words or any(query_term_in(t, sc.unit.terms) for t in q.project_words))
    if sc.coverage < MIN_COVERAGE - 1e-9:
        return False
    # "Nutzbare Breite 1,0 m." in the draft named "…Fluchtbalkon…": attribute and value here, topic above
    return sc.inline or sc.authored or sc.unit.section_topic or (q.temporal and not q.types) or (attr_hit and bool(sc.types))


def _key(text: str) -> str:
    return " ".join(words(text))


def _open_question(q: Question) -> bool:
    """'Was gilt für …?', 'Was muss … beachtet werden?' – asks for everything about a topic."""
    return not q.types and bool(re.match(r"\s*was\b", q.text, re.I))


def _select(q: Question, candidates: list[Scored]) -> list[Scored]:
    """Best sentence plus sentences that add question words or answer types not covered yet.

    Questions about the latest state (and decisions) take the newest source first (an equally good
    older one only as history); open 'Was …?' questions may collect statements from several sources."""
    if q.temporal:
        candidates = sorted(candidates, key=lambda c: (c.unit.source.date or date.min, c.authored, c.coverage,
                                                       c.inline, -c.unit.pos), reverse=True)
    elif q.decision:
        stated = [c for c in candidates if _DECISION_STATED.search(c.unit.text) and not _CONDITIONAL.search(c.unit.text)]
        if stated:
            top = max(c.score for c in stated)
            close = [c for c in stated if c.score >= top - 1.5]
            best = max(close, key=lambda c: (c.unit.source.date or date.min, c.score))
            candidates = [best] + sorted((c for c in candidates if c is not best), key=lambda c: c.score, reverse=True)
        else:
            candidates = sorted(candidates, key=lambda c: c.score, reverse=True)
    else:
        candidates = sorted(candidates, key=lambda c: c.score, reverse=True)
    best = candidates[0]
    chosen = [best]
    seen = [_key(best.unit.text)]
    covered, types = set(best.matched), set(best.types)

    def cover(found: frozenset[str]) -> None:
        types.update(found)
        if found & q.types:  # the accepted types are alternatives ("Wann?" – a date or "vor der Abnahme")
            types.update(q.types)

    cover(best.types)

    def take(c: Scored) -> None:
        chosen.append(c)
        seen.append(_key(c.unit.text))
        covered.update(c.matched)
        cover(c.types)

    same_source = sorted((c for c in candidates if c.unit.source is best.unit.source and c is not best),
                         key=lambda c: c.unit.pos)
    if not q.types and best.unit.label and not best.unit.table:  # whole numbered paragraph ("Auflage 7 …: s1 s2")
        for c in same_source:
            if c.unit.line == best.unit.line and len(chosen) < 2:
                take(c)
    elif q.temporal and (not best.inline or best.authored):  # status mail: its next sentence belongs to the news
        for c in same_source:
            if c.unit.pos == best.unit.pos + 1:
                take(c)
    if q.decision or (q.temporal and best.authored):
        return chosen
    sources = {best.unit.source.n}
    for c in candidates[1:]:
        if len(chosen) >= MAX_SENTENCES:
            break
        k = _key(c.unit.text)
        if c in chosen or any(k in s or s in k for s in seen):
            continue  # same sentence from an overlapping source
        gain = (set(c.matched) - covered) | (set(c.types) - types)
        if q.authorship and best.authored and not c.authored:
            continue  # "Was hat die Feuerwehr gesagt?" – not what a permit says about the Feuerwehr
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
        facts = [u for u in units[s.n] if u.kind in ("fact", "request")]
        if facts:
            return facts[:2]
    return []


def _section_rows(best: Scored, candidates: list[Scored]) -> list[Unit]:
    """All rows/sentences of the section a question asks for ('Welche Kennwerte …?')."""
    src, ctx = best.unit.source, best.unit.context
    rows = sorted({c.unit.pos: c.unit for c in candidates if c.unit.source is src and c.unit.context == ctx}.values(),
                  key=lambda u: u.pos)
    return rows[:6]


_SUMMARY_SECTIONS = ("allgemein", "zusammenfassung", "bauvorhaben", "beschreibung", "gegenstand")


def _summary(q: Question, sources: list[Source], units: dict[int, list[Unit]]) -> list[list[Unit]]:
    """'Fasse das Projekt … zusammen.': what is built, the overall assessment and the latest state –
    each from its own source."""
    pool = [s for s in sources if not q.projects or s.project in q.projects]
    projects = {s.project for s in pool if s.project}
    if len(projects) > 1:
        return []
    groups: list[list[Unit]] = []
    used: set[int] = set()

    def first(pred) -> None:
        for s in pool:
            if s.n in used:
                continue
            hit = [u for u in units[s.n] if u.kind == "fact" and pred(u)]
            if hit:
                groups.append(hit[:2])
                used.add(s.n)
                return

    first(lambda u: fold(u.label or "") == "bauvorhaben"
          or any(fold(u.context).startswith(x) for x in ("allgemein", "beschreibung")))
    n_groups = len(groups)
    first(lambda u: fold(u.context).startswith("zusammenfassung"))
    if len(groups) == n_groups:
        first(lambda u: fold(u.context).startswith("ergebnis"))
    latest = _latest_state([s for s in pool if s.n not in used and s.kind != "document"], units)
    if latest:
        groups.append(latest[:1])
    return groups


# --------------------------------------------------------------------------- chat: wording
def _phrase(units: list[Unit], *, with_project: bool, with_date: bool) -> str:
    """'[Projekt – ]Laut <Quelle>[, <Label>]: Satz. Satz. [n]'"""
    src = units[0].source
    label = units[0].label
    texts = " ".join(t if t.endswith((".", "!", "?", "…")) else t + "." for t in (u.text.rstrip(";,: ") for u in units))
    prefix = f"{src.project} {src.project_name} – " if with_project and src.project else ""
    head = f"{'laut' if prefix else 'Laut'} {src.describe(with_date=with_date)}"
    if label:
        head += f", {label}"
    return re.sub(r"\s{2,}", " ", f"{prefix}{head}: {texts} [{src.n}]")


# a sentence that starts with a pronoun needs the one before it: "Er wird zu 96 m³/h … gedeckt." – who is "er"?
_PRONOUN_START = re.compile(r"^(Er|Dieser|Diese|Dieses|Dies|Dort|Hierfür|Dafür|Davon|Deren|Dessen|Hierzu|Dazu)\b")


def _with_antecedents(chosen: list[Scored], units: dict[int, list[Unit]]) -> list[Unit]:
    out: list[Unit] = []
    for c in chosen:
        u = c.unit
        pronoun = _PRONOUN_START.match(u.text) or (u.source.kind == "document" and u.text.startswith("Sie "))
        if (pronoun or u.kind == "question") and u.pos > 0:
            prev = units[u.source.n][u.pos - 1]
            # "Er wird … gedeckt." needs its subject; "Reicht das?" its premise
            if prev not in out and ((pronoun and prev.kind in ("fact", "request")
                                     and (prev.line == u.line or prev.context == u.context))
                                    or (u.kind == "question" and prev.kind == "premise" and prev.line == u.line)):
                out.append(prev)
        if u not in out:
            out.append(u)
    return out


def _phrase_rows(units: list[Unit], *, with_project: bool) -> str:
    """'Laut Brandschutzkonzept, Kennwerte: Gruppen: 5; Kinder: 95. [2]'"""
    src = units[0].source
    prefix = f"{src.project} {src.project_name} – " if with_project and src.project else ""
    head = f"{'laut' if prefix else 'Laut'} {src.describe()}"
    if units[0].context:
        head += f", {units[0].context}"
    parts = [f"{u.label}: {u.text}" if u.label and u.label != units[0].context else u.text for u in units]
    return f"{prefix}{head}: {'; '.join(p.rstrip('.') for p in parts)}. [{src.n}]"


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


def answer_from_sources(question: str, raw_sources: list[tuple[int, str, str]],
                        synonyms: Synonyms | None = None) -> str:
    sources = [_parse_header(n, h, t) for n, h, t in raw_sources]
    if not sources or not question.strip():
        return NOT_FOUND
    q = analyse_question(question, sources, synonyms)
    units = {s.n: read_units(s) for s in sources}
    several_projects = len({s.project for s in sources}) > 1
    with_project = several_projects or q.project_question
    pool = [s for s in sources if not q.projects or s.project in q.projects]
    if q.summary and not q.terms:  # "Fasse zusammen, was die Feuerwehr will" is about the Feuerwehr's statement
        groups = _summary(q, sources, units)
        if groups:
            return " ".join(_phrase(g, with_project=with_project, with_date=False) for g in groups)
        return NOT_FOUND
    if q.authorship and q.temporal and not q.types and not [t for t in q.terms if t not in q.roles]:
        aliases = role_aliases(units)  # "Was gibt es Neues vom Bauherrn?": the newest source by them
        own = [s for s in pool if any(_authored(t, s, aliases) for t in q.roles)]
        latest = _latest_state(own, units)
        if latest:
            return " ".join(_phrase(g, with_project=with_project, with_date=True) for g in _groups(latest))
    if not q.terms and not q.attrs:
        if q.temporal and not q.types:
            latest = _latest_state(pool, units)
            if latest:
                return " ".join(_phrase(g, with_project=with_project, with_date=True) for g in _groups(latest))
        return NOT_FOUND
    candidates = [c for c in score_units(q, sources, units) if c.answerable]
    if not candidates:
        return NOT_FOUND
    if q.open_points and re.search(r"\b(gefragt|fragt\w*|wissen|rückfrage\w*|anfrage\w*|nachfrage\w*)\b", q.text, re.I):
        asked = [c for c in candidates if c.unit.kind in ("question", "premise")]
        candidates = asked or candidates  # "Was hat die Architektin gefragt?" – her question, not her last remark
    if q.enumeration and several_projects:
        best: dict[str | None, Scored] = {}
        for c in sorted(candidates, key=lambda c: c.score, reverse=True):
            best.setdefault(c.unit.source.project, c)
        picked = list(best.values())[:MAX_PROJECTS]
        if len(picked) > 1:
            parts = [_phrase([c.unit], with_project=True, with_date=q.temporal) for c in picked]
            return f"Gefunden in {len(picked)} Projekten: " + " ".join(parts)
    chosen = _select(q, candidates)
    top = chosen[0]
    if top.unit.section_topic and not top.inline and not q.types:
        rows = _section_rows(top, candidates)
        if len(rows) > 1:
            return _phrase_rows(rows, with_project=with_project)
    with_date = q.temporal or q.decision or "date" in q.types
    return " ".join(_phrase(g, with_project=with_project, with_date=with_date)
                    for g in _groups(_with_antecedents(chosen, units)))


CHAT_QUIRKS = ("length", "reasoning_only", "unterminated_think")


def chat_completion(payload: dict, *, think: bool = True, quirk: str | None = None,
                    synonyms: Synonyms | None = None) -> dict:
    """OpenAI chat completion. ``quirk`` imitates edge cases of real servers (see CHAT_QUIRKS);
    ``synonyms`` default to :func:`active_synonyms` (built-in list plus SYNONYMS_FILE)."""
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
            answer = answer_from_sources(question, sources, synonyms)
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
