"""Deterministic stand-ins for the three AI services (pure functions, no I/O).

They are good enough for the whole pipeline to work end-to-end:

* embeddings: signed feature hashing of stemmed words (+ a little character n-gram signal for
  German compounds) onto 256 dimensions, L2-normalised – lexically similar texts end up close;
* reranker: share of the query's content words found in the document;
* chat: picks the numbered source(s) with most question words and quotes their best sentences
  with ``[n]`` citations; says "Nicht gefunden." if no source covers the question.
"""
from __future__ import annotations

import hashlib
import math
import re

from ..textutil import QUESTION_WORDS, content_terms, fold, term_in, words

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
    q = content_terms(query, QUESTION_WORDS)
    if not q:
        return 0.0
    doc_terms = set(content_terms(doc))
    hits = [t for t in q if term_in(t, doc_terms)]
    coverage = len(hits) / len(q)
    # small tie-breaker: denser documents win
    dens = min(1.0, len(hits) / (1 + len(doc_terms) / 40))
    return round(coverage + 0.05 * dens, 6)


# --------------------------------------------------------------------------- chat
def _message_text(content) -> tuple[str, bool]:
    if isinstance(content, list):
        text = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        has_image = any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)
        return text, has_image
    return str(content or ""), False


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


_ABBREV = re.compile(r"(\b(Nr|Abs|ca|bzw|ggf|inkl|vgl|Dipl|Ing|Str|Tel|z\. ?B|d\. ?h)|\d)\.$")
_META_LINE = re.compile(r"^(#|Betreff:|Von:|Datum:|An:|\|)")


def _sentences(text: str) -> list[str]:
    """Sentences without headings and mail header lines; 'im 2. Obergeschoss' is no sentence end."""
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if len(line) <= 3 or _META_LINE.match(line):
            continue
        buf = ""
        for piece in re.split(r"(?<=[.!?])\s+", line):
            buf = f"{buf} {piece}".strip()
            if not _ABBREV.search(buf):
                out.append(buf)
                buf = ""
        if buf:
            out.append(buf)
    return [s for s in out if len(s) > 3]


def answer_from_sources(question: str, sources: list[tuple[int, str, str]]) -> str:
    keywords = content_terms(question, QUESTION_WORDS)
    if not keywords or not sources:
        return NOT_FOUND
    scored = []
    for n, header, text in sources:
        terms = set(content_terms(header + "\n" + text))
        hits = sum(1 for k in keywords if term_in(k, terms))
        scored.append((hits / len(keywords), -n, n, text))
    scored.sort(reverse=True)
    best_cov = scored[0][0]
    if best_cov < 0.5:
        return NOT_FOUND
    parts = []
    for cov, _, n, text in scored[:2]:
        if cov < max(0.5, best_cov - 0.1):
            break
        sents = _sentences(text)
        ranked = sorted(
            range(len(sents)),
            key=lambda i: (-sum(1 for k in keywords if term_in(k, set(content_terms(sents[i])))), i),
        )
        chosen = sorted(ranked[:2])
        quote = " ".join(sents[i] for i in chosen) if chosen else text[:300]
        parts.append(f"{quote} [{n}]")
    return "Laut den Unterlagen: " + " ".join(parts)


def chat_completion(payload: dict, *, think: bool = True) -> dict:
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
    prompt_tokens = sum(len(words(_message_text(m.get("content"))[0])) for m in messages)
    return {
        "id": "chatcmpl-fake-" + hashlib.sha256(fold(prompt).encode()).hexdigest()[:12],
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content},
             "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(words(content)),
            "total_tokens": prompt_tokens + len(words(content)),
        },
    }
