"""Answer questions: retrieval → prompt with numbered sources → answer with source references.

The model may only use the given sources and must cite them as [n]. If nothing fits, the answer is
"nicht gefunden" – no invented facts. If the LLM is down, the found passages are returned instead.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field

from .ai import AIServiceError
from .search import Hit, hybrid_search

log = logging.getLogger("projektakte.ask")

SYSTEM_PROMPT = (
    "Du bist der Projektassistent eines Ingenieurbüros für Brandschutz. Beantworte die Frage "
    "ausschließlich anhand der nummerierten Quellen. Belege jede Aussage mit der Nummer der Quelle in "
    "eckigen Klammern, z. B. [2]. Erfinde nichts und ergänze kein Allgemeinwissen. Wenn die Quellen die "
    "Frage nicht beantworten, antworte nur: Nicht gefunden. Antworte auf Deutsch, knapp und sachlich; "
    "nenne Zahlen, Daten und Auflagen-Nummern genau so, wie sie in den Quellen stehen."
)
NOT_FOUND_TEXT = "Dazu habe ich in den Unterlagen nichts gefunden."
LLM_DOWN_TEXT = "KI-Dienst nicht erreichbar – hier die gefundenen Stellen zum Nachlesen."
_CITATION = re.compile(r"\[(\d+(?:\s*[-–,;]\s*\d+)*)\]")
_NOT_FOUND = re.compile(r"^\W*(nicht gefunden|keine (angaben|informationen)|dazu (steht|finde))", re.I)


@dataclass
class AskResult:
    question: str
    project: str | None
    answer: str
    found: bool | None  # None = could not be decided (LLM down)
    status: str  # answered | not_found | llm_unavailable | no_citation
    sources: list[dict] = field(default_factory=list)  # cited sources (with their number n)
    context: list[dict] = field(default_factory=list)  # all sources given to the model
    notes: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    latency_ms: float | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def source_header(n: int, h: Hit) -> str:
    project = f" – Projekt {h.project} {h.project_name}" if h.project else ""
    return f"[{n}] {h.label}{project}"


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


def cited_numbers(answer: str, n_sources: int) -> list[int]:
    nums: list[int] = []
    for m in _CITATION.finditer(answer):
        for part in re.split(r"\s*[,;]\s*", m.group(1)):
            if re.search(r"[-–]", part):
                a, b = (int(x) for x in re.split(r"\s*[-–]\s*", part))
                rng = range(a, b + 1) if 0 < b - a < 10 else [a, b]
            else:
                rng = [int(part)]
            for n in rng:
                if 1 <= n <= n_sources and n not in nums:
                    nums.append(n)
    return nums


def _source_dict(n: int, h: Hit) -> dict:
    d = h.to_dict()
    d["n"] = n
    d["snippet"] = h.text[:400]
    return d


def ask(conn: sqlite3.Connection, ai, vector_key: str, question: str, *, project: str | None = None,
        top_k: int = 6, context_chars: int = 8000, min_rerank: float = 0.01,
        rerank_top_k: int = 30) -> AskResult:
    res = hybrid_search(conn, ai, vector_key, question, project=project, k=top_k, rerank_top_k=rerank_top_k)
    result = AskResult(question=question, project=project, answer="", found=False, status="not_found",
                       notes=list(res.notes))
    hits = res.hits
    if res.reranked:
        hits = [h for h in hits if (h.rerank or 0) >= min_rerank]
    if not hits:
        result.answer = NOT_FOUND_TEXT
        return result
    prompt, used = build_prompt(question, hits, context_chars)
    result.context = [_source_dict(i, h) for i, h in enumerate(used, start=1)]
    try:
        chat = ai.llm.chat([{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt}])
    except AIServiceError as exc:
        log.warning("Sprachmodell nicht erreichbar: %s", exc)
        result.answer, result.found, result.status = LLM_DOWN_TEXT, None, "llm_unavailable"
        result.sources = result.context
        result.notes.append(str(exc))
        return result
    answer = chat.text.strip()
    result.usage, result.latency_ms = chat.usage, chat.latency_ms
    if not answer or (_NOT_FOUND.search(answer) and len(answer) < 160):
        result.answer = NOT_FOUND_TEXT
        return result
    nums = cited_numbers(answer, len(used))
    result.answer, result.found = answer, True
    if nums:
        result.status = "answered"
        result.sources = [result.context[n - 1] for n in nums]
    else:
        result.status = "no_citation"
        result.sources = result.context
        result.notes.append("Die Antwort nennt keine Quelle – bitte anhand der Fundstellen prüfen.")
    return result
