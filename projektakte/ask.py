"""Answer questions: retrieval → prompt with numbered sources → answer with source references.

The model may only use the given sources and must cite them as [n]. If nothing fits, the answer is
"nicht gefunden" – no invented facts. If the LLM is down or its output is cut off, the found passages
are returned instead.

Status contract (rendered by the web interface):

* ``answered``        – answer with valid [n] citations; ``sources`` = the cited sources
* ``no_citation``     – the model answered without any valid citation: the text is kept but must be
  shown as "unbelegt"; ``sources`` = []
* ``not_found``       – nothing fits; ``answer`` = German not-found text
* ``llm_unavailable`` – language model down or timed out
* ``truncated``       – output cut off (token limit) or only reasoning without an answer

For every status except ``answered``, ``context`` holds what the retrieval found (possibly empty).

Every source dict (in ``sources`` and ``context``) carries ``snippet``: the passage of that chunk
that matters – the sentence(s) quoted from it in the answer, else those best matching the question –
as clean text (no markdown), at most about 400 characters.

User-visible texts (``answer``, ``notes``) never contain service URLs, exception texts or setting
names; those details go to the log for administrators.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field

from .ai import AIServiceError
from .search import MIN_CALL_SECONDS, time_left, Hit, hybrid_search, load_hits, merge_adjacent
from .textutil import (
    SUMMARY_WORDS,
    content_terms,
    de_date,
    is_enumeration_question,
    is_project_question,
    is_summary_question,
    is_temporal_question,
    fold,
    named_roles,
    query_terms,
    relevant_passage,
    role_key,
    strip_identifiers,
)

log = logging.getLogger("projektakte.ask")

SYSTEM_PROMPT = (
    "Du bist der Projektassistent eines Ingenieurbüros für Brandschutz. Beantworte die Frage "
    "ausschließlich anhand der nummerierten Quellen.\n"
    "Regeln:\n"
    "1. Belege jede Aussage direkt dahinter mit der Nummer ihrer Quelle in eckigen Klammern, z. B. [2] "
    "oder [1, 3]. Schreibe nicht „Quelle 2“ und keine Fußnoten.\n"
    "2. Erfinde nichts und ergänze kein Allgemeinwissen. Nenne Zahlen, Maße, Daten und Auflagen-Nummern "
    "genau so, wie sie in den Quellen stehen.\n"
    "3. Jede Quelle trägt ihr Datum (Mail vom …, Stand …). Fragt jemand nach dem letzten oder aktuellen "
    "Stand, ob etwas inzwischen erledigt ist oder was zuletzt geschah, stütze dich auf die neueste "
    "passende Quelle und nenne ihr Datum; ältere Aussagen nur als Vorgeschichte.\n"
    "4. Stammen die Quellen aus verschiedenen Projekten, nenne bei jeder Aussage das Projekt (Nummer "
    "und Name). Bei Aufzählungen nenne jedes passende Projekt einzeln.\n"
    "5. Beantworten die Quellen die Frage nicht, antworte nur: Nicht gefunden.\n"
    "Antworte auf Deutsch, knapp und sachlich, in ganzen Sätzen.\n\n"
    "Beispiel für das Antwortformat:\n"
    "Die Brandschutztüren im Untergeschoss sind selbstschließend auszuführen (Auflage 5) [1]. Laut Mail "
    "vom 03.04.2024 sind sie inzwischen eingebaut [3]."
)
NOT_FOUND_TEXT = "Dazu habe ich in den Unterlagen nichts gefunden."
LLM_DOWN_TEXT = "KI-Dienst nicht erreichbar – hier die gefundenen Stellen zum Nachlesen."
TRUNCATED_TEXT = ("Das Sprachmodell hat keine vollständige Antwort geliefert (Ausgabe abgebrochen) – "
                  "hier die gefundenen Stellen zum Nachlesen.")
WEAK_HITS_NOTE = ("Keine Fundstelle passt gut genug für eine belegte Antwort – gezeigt werden die besten "
                  "Treffer der Suche.")
MISS_CONTEXT = 3  # passages shown as "possible locations" when nothing reaches ASK_MIN_RERANK
ENUMERATION_MAX_SOURCES = 12
ENUMERATION_RERANK_TOP_K = 60
SNIPPET_CHARS = 400
# what an overview question needs besides its own hits: what is built, the overall assessment
SUMMARY_QUERY = "Bauvorhaben Neubau Allgemeines Zusammenfassung Ergebnis"

# Citations: [2], [2, 3], [1–3], [Quelle 2], [Quellen 1 und 3], (Quelle 2), [^2] and prose "Quelle 2".
_NUMS = r"\d+(?:\s*(?:[-–,;]|und|bis)\s*\d+)*"
_CITE_BRACKETED = re.compile(r"\[\^?\s*(?:Quellen?\s*)?(" + _NUMS + r")\s*\]|\(\s*Quellen?\s+(" + _NUMS + r")\s*\)",
                             re.I)
_CITE_PROSE = re.compile(r"\bQuellen?\s+(" + _NUMS + r")\b", re.I)

# "Not found" in the wording of real models, not only the exact "Nicht gefunden.".
# _NF_SOURCE: the model says the sources do not answer ("Die Quellen enthalten keine Angaben …",
# "Das geht aus den Unterlagen nicht hervor", "Ich konnte nichts dazu finden"). A negative *fact* is
# no refusal: "Es liegen keine Mängel mehr vor [1]." or "Seitens der Feuerwehr bestehen keine
# Bedenken [3]." are answers.
_SRC = r"(?:quellen?|unterlagen|dokumente?n?|akten?|texte?n?|vorliegenden\s+informationen)"
_NF_SOURCE = re.compile(
    r"nicht\s+gefunden|nichts\s+(?:\w+\s+){0,3}?(?:gefunden|finden)|"
    r"\b(?:geht|gehen)\s+(?:\w+\s+){0,6}?nicht\s+hervor|"
    r"lässt\s+sich\s+(?:\w+\s+){0,6}?nicht\s+(?:beantworten|entnehmen|ableiten|feststellen|sagen)|"
    r"(?:kann|könnte|konnte)\s+(?:ich\s+)?(?:\w+\s+){0,6}?(?:nicht|keine\w*|nichts)\s+(?:\w+\s+){0,2}?"
    r"(?:beantworten|beantwortet|entnehmen|sagen|finden|feststellen|ermitteln|nennen)|"
    r"\b" + _SRC + r"\b[^.!?]{0,80}\b(?:kein\w*|nicht|nichts)\b[^.!?]{0,40}\b(?:erwähnt|genannt|enthalten|"
    r"aufgeführt|beschrieben|angegeben|vorhanden|thematisiert|behandelt|ersichtlich|zu\s+entnehmen|bekannt)|"
    r"\b" + _SRC + r"\s+(?:enthalten|nennen|liefern|machen|beantworten|geben|erwähnen|beschreiben|sagen)\s+"
    r"(?:\w+\s+){0,3}?(?:kein\w*|nicht|nichts)|"
    r"\b(?:enthalten|nennen|liefern|machen|geben|erwähnen|beschreiben|sagen)\s+(?:die|diese|den)\s+(?:vorliegenden\s+|"
    r"genannten\s+)?" + _SRC + r"\s+(?:\w+\s+){0,2}?(?:kein\w*|nicht|nichts)|"
    r"\b(?:in|aus)\s+den\s+(?:vorliegenden\s+|genannten\s+|angegebenen\s+)?" + _SRC + r"\b[^.!?]{0,60}\bkein\w*\b|"
    r"^\W*(?:dazu|hierzu|darüber|hierüber)\s+(?:steht|finde|habe|gibt\s+es\s+kein|liegen\s+(?:\w+\s+)?kein)",
    re.I)
# "keine Angaben/Informationen/Hinweise …": a refusal without a citation, a fact with one
# ("Der Prüfbericht enthält keine Hinweise auf Mängel [2].").
_NF_INFO = re.compile(r"\bkein\w*\s+(?:\w+\s+){0,2}?(?:angaben?|informationen?|hinweise?|aussagen?|auskunft|"
                      r"erwähnung|daten\b)", re.I)
_NF_WEAK = re.compile(
    r"\bnicht\s+(?:enthalten|erwähnt|genannt|angegeben|aufgeführt|beschrieben|ersichtlich|vorhanden|"
    r"zu\s+entnehmen|beantwortbar|bekannt)\b|\bunbekannt\b",
    re.I)
# a refusal that goes on with a fact in the same sentence: "Die Quellen nennen keinen Termin, die
# Abnahme ist aber für März vorgesehen [2]."
_CONTRAST = re.compile(r"\b(aber|jedoch|allerdings|lediglich|nur|dafür)\b", re.I)
# clauses of one sentence: "Die Abnahme ist für März vorgesehen [2]; ein genauer Tag ist nicht genannt."
_CLAUSE_SEP = re.compile(r"\s*;\s*|\s+[–—-]\s+")
_SENTENCE_END = re.compile(r"(?<!\b[Nn]r)(?<!\bz\.\sB)(?<!\bbzw)(?<!\bca)(?<!\d)[.!?](?=\s|$)")


@dataclass
class AskResult:
    question: str
    project: str | None
    answer: str
    found: bool | None  # None = could not be decided (LLM down, output cut off, no citation)
    status: str  # answered | no_citation | not_found | llm_unavailable | truncated
    sources: list[dict] = field(default_factory=list)  # cited sources (with their number n)
    context: list[dict] = field(default_factory=list)  # all sources given to the model (with n)
    notes: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    latency_ms: float | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def source_header(n: int, h: Hit) -> str:
    """'[2] 03 Prüfung/Prüfbericht Nr. 3.md (Stand 14.11.2024) – Projekt BS-… Name'.

    Mails and attachments carry their date in the label already; documents get their document date,
    so the model can order the sources in time ("letzter Stand")."""
    label = h.label
    if h.kind == "document" and h.date:
        label += f" (Stand {de_date(h.date)})"
    project = f" – Projekt {h.project} {h.project_name}" if h.project else ""
    return f"[{n}] {label}{project}"


def build_prompt(question: str, hits: list[Hit], max_chars: int) -> tuple[str, list[Hit]]:
    parts, used, size = [], [], 0
    for h in hits:
        block = f"{source_header(len(used) + 1, h)}\n{h.text.strip()}"
        if used and size + len(block) > max_chars:
            break
        parts.append(block)
        used.append(h)
        size += len(block)
    prompt = "Quellen:\n\n" + "\n\n".join(parts) + f"\n\nFrage: {question.strip()}"
    return prompt, used


def _numbers(group: str, n_sources: int) -> list[int]:
    nums: list[int] = []
    for part in re.split(r"\s*(?:[,;]|und)\s*", group):
        if not part.strip():
            continue
        bounds = [int(x) for x in re.split(r"\s*(?:[-–]|bis)\s*", part.strip()) if x.strip().isdigit()]
        if len(bounds) == 2:
            a, b = bounds
            rng = range(a, b + 1) if 0 < b - a < 10 else [a, b]
        else:
            rng = bounds
        for n in rng:
            if 1 <= n <= n_sources and n not in nums:
                nums.append(n)
    return nums


def cited_numbers(answer: str, n_sources: int) -> list[int]:
    """Source numbers the answer refers to, in order of appearance (only 1…n_sources)."""
    found: list[tuple[int, list[int]]] = []
    for m in _CITE_BRACKETED.finditer(answer):
        found.append((m.start(), _numbers(m.group(1) or m.group(2), n_sources)))
    for m in _CITE_PROSE.finditer(answer):
        found.append((m.start(), _numbers(m.group(1), n_sources)))
    nums: list[int] = []
    for _, group in sorted(found):
        nums.extend(n for n in group if n not in nums)
    return nums


def normalize_citations(answer: str, n_sources: int) -> str:
    """Write bracketed citation variants as [n] / [n, m] so the interface can link them.

    '[Quelle 2]' → '[2]', '(Quellen 1 und 3)' → '[1, 3]', '[1–3]' → '[1, 2, 3]'. Prose like
    'laut Quelle 2' stays as written (it is still counted as a citation)."""
    def repl(m: re.Match) -> str:
        nums = _numbers(m.group(1) or m.group(2), n_sources)
        return "[" + ", ".join(str(n) for n in nums) + "]" if nums else m.group(0)
    return _CITE_BRACKETED.sub(repl, answer)


def _first_sentence(text: str) -> tuple[str, str]:
    m = _SENTENCE_END.search(text)
    return (text[: m.end()], text[m.end():]) if m else (text, "")


def is_not_found(answer: str, n_sources: int) -> bool:
    """Does the model say that the sources do not answer the question?

    A refusal that names the sources ("Die Quellen enthalten keine Angaben …") in the first sentence
    counts, unless the answer goes on with cited facts (partial answer). "Keine Angaben/Hinweise …"
    and weak phrases ("… ist nicht erwähnt") only count when nothing at all is cited – a cited
    negative fact ("Es liegen keine Mängel mehr vor [1].", "Die Kosten sind in der Baugenehmigung
    nicht enthalten [2].") stays an answer."""
    text = re.sub(r"[*_`#>]+", "", answer).strip()
    if not text:
        return False
    first, rest = _first_sentence(text)
    first_cited = bool(cited_numbers(first, n_sources))
    rest_cited = bool(cited_numbers(rest, n_sources))
    m = _NF_SOURCE.search(first)
    if m:
        if rest_cited:
            return False
        if first_cited and _CONTRAST.search(first[m.end():]):
            return False
        return not _cited_clause_besides(first, m, n_sources)
    if _NF_INFO.search(first) or _NF_WEAK.search(first):
        return not (first_cited or rest_cited)
    return False


def _cited_clause_besides(sentence: str, refusal: re.Match, n_sources: int) -> bool:
    """Does a clause of the sentence other than the refusal itself carry a citation? ("Die Quellen nennen
    kein genaues Datum; laut Protokoll ist die Abnahme für März vorgesehen [2].")"""
    start = 0
    for sep in list(_CLAUSE_SEP.finditer(sentence)) + [None]:
        end = sep.start() if sep else len(sentence)
        overlaps = start < refusal.end() and refusal.start() < end
        if not overlaps and cited_numbers(sentence[start:end], n_sources):
            return True
        start = sep.end() if sep else end
    return False


def _source_dict(n: int, h: Hit, question: str = "", quoted: str = "") -> dict:
    d = h.to_dict()
    d["n"] = n
    d["snippet"] = relevant_passage(h.text, question, quoted, SNIPPET_CHARS)
    return d


def answer_segments(answer: str, n_sources: int) -> dict[int, str]:
    """The text of the answer that each cited source backs: the words before its citation (since the
    previous citation). 'A [1]. B [2, 3].' → {1: 'A', 2: 'B', 3: 'B'}."""
    marks: list[tuple[int, int, list[int]]] = []
    for m in _CITE_BRACKETED.finditer(answer):
        marks.append((m.start(), m.end(), _numbers(m.group(1) or m.group(2), n_sources)))
    for m in _CITE_PROSE.finditer(answer):
        if not any(a <= m.start() < b for a, b, _ in marks):  # "[Quelle 2]" is one citation
            marks.append((m.start(), m.end(), _numbers(m.group(1), n_sources)))
    out: dict[int, str] = {}
    pos, last = 0, ""
    for start, end, nums in sorted(marks):
        seg = answer[pos:start].strip(" .,;:") or last
        last = seg
        for n in nums:
            out[n] = f"{out[n]} {seg}" if n in out else seg
        pos = max(pos, end)
    return out


def diversify_projects(hits: list[Hit], k: int) -> list[Hit]:
    """Best hit of every project first (in rank order), then the remaining hits by rank."""
    first, rest, seen = [], [], set()
    for h in hits:
        (rest if h.project in seen else first).append(h)
        seen.add(h.project)
    return (first + rest)[:k]


def prefer_recent(hits: list[Hit], k: int, recent: int = 2, extra: list[Hit] | None = None,
                  projects: set[str | None] | None = None) -> list[Hit]:
    """For questions about the latest state: the k-``recent`` best hits plus the newest dated hits
    among the remaining candidates (a status mail may rank below older documents). ``extra`` are
    further candidates of the same project(s) that did not reach the relevance threshold – the
    newest mail ("Was gibt es Neues?") often shares no word with the question. With ``projects``
    the newest hits are taken from those projects only (the latest state of the asked project, not a
    newer document of another one)."""
    recent = min(recent, k // 2)
    ids = {h.chunk_id for h in hits}
    pool = hits + [h for h in (extra or []) if h.chunk_id not in ids]
    if recent <= 0 or len(pool) <= k:
        return pool[:k]
    chosen = hits[: k - recent]
    chosen += sorted((h for h in pool if h not in chosen and h.local_date and (not projects or h.project in projects)),
                     key=lambda h: h.local_date, reverse=True)[:recent]
    for h in pool:  # fill up by rank
        if len(chosen) >= k:
            break
        if h not in chosen:
            chosen.append(h)
    return sorted(chosen, key=pool.index)


def _main_projects(hits: list[Hit], project: str | None) -> set[str | None]:
    """The project(s) a question is about: the filter, else the project of the best hit."""
    if project:
        return {project}
    return {hits[0].project} if hits else set()


def _search_deadline(deadline: float | None, llm_reserve: float) -> float | None:
    """Searches may use the budget except the part kept for the language model."""
    return None if deadline is None else deadline - llm_reserve


def _time_for_more(deadline: float | None, llm_reserve: float) -> bool:
    """Is there time for an extra search (overview, parties) before the language model?"""
    return deadline is None or deadline - llm_reserve - time.monotonic() >= MIN_CALL_SECONDS * 5


def _context(hits: list[Hit], question: str = "") -> list[dict]:
    return [_source_dict(i, h, question) for i, h in enumerate(hits, start=1)]


def _summary_hits(conn: sqlite3.Connection, ai, vector_key: str, hits: list[Hit], project: str | None,
                  rerank_top_k: int, synonyms_file: str | None, *, topic: bool = False, top_k: int = 6,
                  deadline: float | None = None) -> list[Hit]:
    """Overview passages for "Fasse … zusammen"/"Worum geht es …": the description of the project and
    the overall assessment, put in front of the question's own hits – behind the best of them when
    the question has a topic of its own ("Fasse zusammen, was die Feuerwehr fordert")."""
    projects = _main_projects(hits, project) - {None}
    if len(projects) != 1:
        return hits
    (number,) = projects
    extra = hybrid_search(conn, ai, vector_key, SUMMARY_QUERY, project=number, k=4, rerank_top_k=rerank_top_k,
                          synonyms_file=synonyms_file, deadline=deadline).hits
    seen = {h.chunk_id for h in extra}
    own = [h for h in hits if h.chunk_id not in seen]
    if topic:
        keep = max(1, top_k // 2)
        return own[:keep] + extra + own[keep:]
    return extra + own


def has_topic(conn: sqlite3.Connection, question: str) -> bool:
    """Does an overview question name a topic besides the project ("Fasse zusammen, was die Feuerwehr
    fordert" – not "Fasse das Projekt Kita Regenbogen zusammen")?"""
    names: set[str] = set()
    try:
        for row in conn.execute("SELECT number, name FROM projects"):
            names.update(content_terms(f"{row[0]} {row[1] or ''}"))
    except sqlite3.Error:
        pass
    return any(not SUMMARY_WORDS.match(t.word) and not SUMMARY_WORDS.match(t.term) and t.term not in names
               and not t.term.isdigit() for t in query_terms(strip_identifiers(question)))


def _role_hits(conn: sqlite3.Connection, ai, vector_key: str, question: str, hits: list[Hit], project: str | None,
               rerank_top_k: int, synonyms_file: str | None, deadline: float | None = None) -> list[Hit]:
    """For a question naming a party ("Was hat die Architektin geschrieben?", "Hat der Bauherr
    zugestimmt?"): one passage of the project that says who that is ("Bauherr: …, vertreten durch",
    "Teilnehmer: Clara Meier (Architektur)", "Prüfingenieur: …") – so the model can tell whose mail is
    whose."""
    projects = _main_projects(hits, project) - {None}
    if len(projects) != 1:
        return []
    (number,) = projects
    have = {h.chunk_id for h in hits}
    out: list[Hit] = []
    communication = bool(_COMMUNICATION_Q.search(question))
    for role in named_roles(question)[:2]:
        found = hybrid_search(conn, ai, vector_key, role.word, project=number, k=3, rerank_top_k=min(10, rerank_top_k),
                              synonyms_file=synonyms_file, deadline=deadline).hits
        for h in found:
            if h.chunk_id not in have and (h.rerank is None or h.rerank > 0):
                out.append(h)
                have.add(h.chunk_id)
                break
        if communication:  # "Was wollte der Bauherr wissen?" – the party's own newest mails
            people = _role_people([h.text for h in hits + out + found], role_key(role) or role.term)
            for h in _mails_of(conn, number, people, have):
                out.append(h)
                have.add(h.chunk_id)
    return out


_COMMUNICATION_Q = re.compile(r"\b(geschrieben|schreibt|schrieb|gesagt|sagt|sagte|mitgeteilt|teilt|gefragt|fragt|fragte|"
                              r"wollte|wollten|will|möchte|wünscht|gewünscht|gefordert|fordert|verlangt|geantwortet|"
                              r"antwortet|gemeldet|meldet|berichtet|bittet|gebeten|empfiehlt|empfohlen|vorgeschlagen|"
                              r"schlägt|geschickt|schickt)\b", re.I)
_NAME = r"(?:(?:Dipl\.-Ing\.|Dr\.|Prof\.)\s+)?([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß\-]+){1,2})"
_PERSON_ROLE = re.compile(_NAME + r"\s+\(([^()\n]{2,40})\)")  # "Clara Meier (Architektur)"
_ROLE_LINE = re.compile(r"^\s*([A-ZÄÖÜ][\wäöüß\-]+)[^:\n]{0,30}:\s+([^\n]+)$", re.M)  # "Bauherr: …, vertreten durch …"


def _role_people(texts: list[str], key: str) -> set[str]:
    """Surnames of the people the passages name for a role: 'Clara Meier (Architektur)', 'Bauherr: Wohnbau
    Stadtpark eG, vertreten durch Benedikt Sauer', 'Prüfingenieur: Dipl.-Ing. Sabine Wendehals'."""
    def is_role(label: str) -> bool:
        folded = fold(label.split()[0]) if label.split() else ""
        return folded.startswith(key) or (key == "feuerwehr" and folded.startswith("brandschutzdienststell"))

    out: set[str] = set()
    for text in texts:
        for m in _PERSON_ROLE.finditer(text):
            if is_role(m.group(2)):
                out.add(m.group(1).split()[-1])
        for m in _ROLE_LINE.finditer(text):
            if not is_role(m.group(1)):
                continue
            rest = m.group(2)
            v = re.search(r"vertreten\s+durch\s+" + _NAME, rest)
            p = v or re.match(_NAME, rest.strip())
            if p and not re.search(r"\b(GmbH|gGmbH|mbH|eG|AG|KG|Stadt|Amt|Büro)\b", p.group(0)):
                out.add(p.group(1).split()[-1])
    return out


def _mails_of(conn: sqlite3.Connection, project: str, people: set[str], have: set[int], limit: int = 2) -> list[Hit]:
    """The newest mails of the project written by these people (first chunk of each)."""
    if not people:
        return []
    cond = " OR ".join("m.sender_name LIKE ?" for _ in people)
    try:
        rows = conn.execute(
            f"SELECT c.id, c.mail_id FROM chunks c JOIN mails m ON m.id = c.mail_id WHERE c.project = ? "
            f"AND c.doc_id IS NULL AND ({cond}) ORDER BY m.date DESC, c.ord ASC LIMIT 20",
            [project, *(f"%{p}%" for p in sorted(people))]).fetchall()
    except sqlite3.Error:
        return []
    ids: list[int] = []
    mails: set[int] = set()
    for chunk_id, mail_id in rows:
        if mail_id in mails or chunk_id in have:
            continue
        mails.add(mail_id)
        ids.append(chunk_id)
        if len(ids) >= limit:
            break
    found = load_hits(conn, ids)
    return [found[i] for i in ids if i in found]


def llm_error_note(exc: Exception) -> str:
    """What the user sees about an LLM failure – the URL and the raw error stay in the log."""
    text = str(exc)
    m = re.search(r"Zeitüberschreitung nach ([\d.,]+) s", text)
    if m:
        return f"Das Sprachmodell hat nicht rechtzeitig geantwortet (Zeitüberschreitung nach {m.group(1)} s)."
    m = re.search(r"HTTP (\d{3})", text)
    if m:
        return f"Das Sprachmodell hat mit einem Fehler geantwortet (HTTP {m.group(1)})."
    if "Antwortformat" in text or "JSON" in text:
        return "Das Sprachmodell hat eine unerwartete Antwort geliefert."
    return "Das Sprachmodell ist nicht erreichbar."


def ask(conn: sqlite3.Connection, ai, vector_key: str, question: str, *, project: str | None = None,
        top_k: int = 6, context_chars: int = 8000, min_rerank: float = 0.01,
        rerank_top_k: int = 30, synonyms_file: str | None = None, deadline_s: float = 0) -> AskResult:
    """``deadline_s`` (ASK_DEADLINE, 0 = off) caps the whole question: every AI call gets at most the
    remaining time, extra searches (overview, parties) only run while enough time is left for the
    language model, and without time left the found passages come back like with an LLM outage."""
    deadline = time.monotonic() + deadline_s if deadline_s and deadline_s > 0 else None
    llm_reserve = min(ai.llm.timeout, deadline_s / 2) if deadline and ai is not None else 0
    across_projects = project is None and is_project_question(question)
    enumeration = project is None and is_enumeration_question(question)
    temporal = is_temporal_question(question)
    summary = is_summary_question(question) and not across_projects
    candidates = max(rerank_top_k, ENUMERATION_RERANK_TOP_K) if across_projects else rerank_top_k
    res = hybrid_search(conn, ai, vector_key, question, project=project, k=candidates,
                        rerank_top_k=candidates, synonyms_file=synonyms_file, deadline=_search_deadline(deadline, llm_reserve))
    result = AskResult(question=question, project=project, answer="", found=False, status="not_found",
                       notes=list(res.notes))
    hits = res.hits
    if res.reranked:
        hits = [h for h in hits if (h.rerank or 0) >= min_rerank]
    weak = [h for h in res.hits if h not in hits]
    # the latest state of one project: its newest sources are the context even if they share no word
    # with the question ("Was gibt es Neues?") – see prefer_recent below
    latest_of_one_project = temporal and bool(res.hits) and bool(
        project or len({h.project for h in res.hits[:3]}) == 1)
    if not hits and not summary and not latest_of_one_project:
        result.answer = NOT_FOUND_TEXT
        result.context = _context(merge_adjacent(res.hits)[:MISS_CONTEXT], question)
        if res.hits:
            log.info("Keine Fundstelle erreicht ASK_MIN_RERANK=%s", min_rerank)
            result.notes.append(WEAK_HITS_NOTE)
        return result
    if summary and _time_for_more(deadline, llm_reserve):
        hits = _summary_hits(conn, ai, vector_key, hits or res.hits, project, rerank_top_k, synonyms_file,
                             topic=has_topic(conn, question), top_k=top_k,
                             deadline=_search_deadline(deadline, llm_reserve))
    hits = merge_adjacent(hits)
    if across_projects:
        k = max(top_k, min(2 * top_k, ENUMERATION_MAX_SOURCES))
        hits = diversify_projects(hits, k)
    elif temporal:
        main = _main_projects(hits or res.hits, project)
        extra = merge_adjacent([h for h in weak if h.project in main])
        hits = prefer_recent(hits, top_k, extra=extra, projects=main if latest_of_one_project else None)
    else:
        hits = hits[:top_k]
    if not hits:
        result.answer = NOT_FOUND_TEXT
        result.context = _context(merge_adjacent(res.hits)[:MISS_CONTEXT], question)
        return result
    if not across_projects and _time_for_more(deadline, llm_reserve):
        hits = merge_adjacent(hits + _role_hits(conn, ai, vector_key, question, hits, project, rerank_top_k,
                                                synonyms_file, deadline=_search_deadline(deadline, llm_reserve)))
    prompt, used = build_prompt(question, hits, context_chars)
    result.context = _context(used, question)
    if enumeration:
        n_projects = len({h.project for h in used if h.project})
        result.notes.append(f"Aufzählung über Projekte: berücksichtigt sind die besten Fundstellen aus "
                            f"{n_projects} Projekten – eine vollständige Liste liefert die Suche.")
    llm_timeout = time_left(deadline, ai.llm.timeout)
    if llm_timeout == 0:
        log.warning("Zeitbudget der Frage (ASK_DEADLINE) vor dem Sprachmodell erschöpft")
        result.answer, result.found, result.status = LLM_DOWN_TEXT, None, "llm_unavailable"
        result.notes.append("Die Suche hat das Zeitbudget der Frage aufgebraucht – hier die Fundstellen.")
        return result
    try:
        chat = ai.llm.chat([{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt}], timeout=llm_timeout)
    except AIServiceError as exc:
        log.warning("Sprachmodell nicht erreichbar: %s", exc)
        result.answer, result.found, result.status = LLM_DOWN_TEXT, None, "llm_unavailable"
        result.notes.append(llm_error_note(exc))
        return result
    result.usage, result.latency_ms = chat.usage, chat.latency_ms
    _interpret(result, chat, len(used))
    if result.sources:  # snippets of the cited sources: the passage the answer quotes
        segments = answer_segments(result.answer, len(used))
        for src in result.sources:
            h = used[src["n"] - 1]
            src["snippet"] = relevant_passage(h.text, question, segments.get(src["n"], ""), SNIPPET_CHARS)
    return result


def _interpret(result: AskResult, chat, n_sources: int) -> None:
    """Turn the model output into answer, status and cited sources (see module docstring)."""
    answer = (chat.text or "").strip()
    finish = getattr(chat, "finish_reason", None)
    if not answer:
        result.answer, result.found, result.status = TRUNCATED_TEXT, None, "truncated"
        why = "Längenlimit erreicht" if finish == "length" else "nur Denkschritte, keine Antwort"
        result.notes.append(f"Antwort des Sprachmodells abgebrochen ({why}) – LLM_MAX_TOKENS erhöhen oder "
                            "das Denken abschalten (LLM_EXTRA_BODY).")
        return
    if is_not_found(answer, n_sources):
        result.answer, result.found, result.status = NOT_FOUND_TEXT, False, "not_found"
        return
    nums = cited_numbers(answer, n_sources)
    result.answer = normalize_citations(answer, n_sources)
    result.sources = [result.context[n - 1] for n in nums]
    if finish == "length" or getattr(chat, "truncated", False):
        result.answer = result.answer.rstrip() + " …"
        result.found, result.status = None, "truncated"
        result.notes.append("Die Antwort wurde abgeschnitten (Längenlimit LLM_MAX_TOKENS) – bitte anhand "
                            "der Fundstellen prüfen.")
    elif nums:
        result.found, result.status = True, "answered"
    else:
        result.found, result.status = None, "no_citation"
        result.sources = []
        result.notes.append("Die Antwort nennt keine Quelle (unbelegt) – bitte anhand der Fundstellen prüfen.")
