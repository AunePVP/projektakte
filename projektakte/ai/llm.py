"""Client for an OpenAI-compatible Chat-Completions server (vLLM, llama.cpp, …)."""
from __future__ import annotations

import base64
import logging
import re
import threading
import time
from dataclasses import dataclass

from .http import AIServiceError, Timer, bearer, get_json, post_json, post_stream
from .repetition import STREAM_WINDOW, before_loop, find_loop

log = logging.getLogger("projektakte.ai")

# Reading one page image (describe_image): its answer may use at most VISION_MAX_TOKENS (or LLM_MAX_TOKENS if
# higher). With the page's text so far in the prompt (between <<< and >>>, see extract.vision_prompt) the first
# call gets less, measured on the prompt: a page answer is about as long as that text plus the handwriting. A
# loop then ends early; an answer that was merely cut short is asked once more with the full amount. Without a
# text so far (a plan Tesseract found nothing on) nothing tells the length: the full amount at once.
VISION_MAX_TOKENS = 4000
PAGE_MIN_TOKENS = 2000
_KNOWN_TEXT = "\n<<<\n"
# Second attempt after a repetition loop: other sampling than the near-greedy first one, so the page is not read
# the same way again (Qwen's recommended temperature/top_p/top_k; VISION_RETRY_BODY replaces it). No presence
# penalty: measured with Qwen3.5-4B, 1.5 lost about 14 percentage points of a page's words, 0.3 still 3.
# Only that page is asked like this; all others keep LLM_EXTRA_BODY.
DEFAULT_RETRY_BODY = {"temperature": 0.7, "top_p": 0.8, "top_k": 20}
_CHECK_EVERY = 300  # characters streamed in between two looks for a loop

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Remove reasoning blocks like ``<think>…</think>`` from a model answer.

    Also handles an unterminated ``<think>`` (answer cut off while thinking → nothing usable) and a
    dangling ``</think>`` (some templates put the opening tag into the prompt).
    """
    if not text:
        return ""
    text = _THINK_BLOCK.sub("", text)
    low = text.lower()
    if "</think>" in low:
        text = text[low.rindex("</think>") + len("</think>"):]
    elif "<think>" in low:
        text = text[: low.index("<think>")]
    return text.strip()


def has_unterminated_think(text: str) -> bool:
    """True if a reasoning block was opened but never closed (output cut off while thinking)."""
    low = (text or "").lower()
    return low.rfind("<think>") > low.rfind("</think>")


@dataclass
class ChatResult:
    text: str  # answer without reasoning blocks
    raw_text: str  # content as sent by the server
    usage: dict
    model: str
    latency_ms: float
    finish_reason: str | None = None  # "stop", "length" (max_tokens reached), … as reported
    reasoning: str = ""  # separate reasoning output (vLLM reasoning parser: reasoning_content)
    loop: bool = False  # the answer fell into a repetition loop (stopped early while streaming, or found after)

    @property
    def truncated(self) -> bool:
        """The model ran out of tokens or produced only reasoning – the answer is incomplete."""
        return (self.finish_reason == "length" or has_unterminated_think(self.raw_text)
                or (not self.text and bool(self.reasoning or self.raw_text)))


class IncompleteAnswer(AIServiceError):
    """The model did not give a usable page: it fell into a repetition loop or ran out of tokens, also on
    its second attempt. ``text`` is the usable part (before the loop; all of an answer cut short)."""

    def __init__(self, message: str, text: str, loop: bool):
        super().__init__(message)
        self.text, self.loop = text, loop


class LLMClient:
    def __init__(
        self,
        url: str,
        model: str,
        *,
        timeout: float = 120.0,
        max_tokens: int = 800,
        vision: bool = False,
        api_key: str = "",
        extra_body: dict | None = None,
        retry_body: dict | None = None,
    ):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.vision = vision
        self._api_key = api_key
        self._headers = bearer(api_key)
        self.extra_body = dict(extra_body or {})
        self.retry_body = dict(DEFAULT_RETRY_BODY if retry_body is None else retry_body)
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.last_ok_at: float | None = None  # last answer to real work (the status page: busy, not down)

    def config(self) -> dict:
        """What this client was built with (plain data, contains the API key): the extraction worker
        process builds the same client from it with :meth:`from_config`."""
        return {"url": self.url, "model": self.model, "timeout": self.timeout, "max_tokens": self.max_tokens,
                "vision": self.vision, "api_key": self._api_key, "extra_body": dict(self.extra_body),
                "retry_body": dict(self.retry_body)}

    @classmethod
    def from_config(cls, config: dict) -> "LLMClient":
        return cls(config["url"], config["model"], timeout=config["timeout"], max_tokens=config["max_tokens"],
                   vision=config["vision"], api_key=config["api_key"], extra_body=config["extra_body"],
                   retry_body=config.get("retry_body"))

    def _note_error(self, msg: str) -> None:
        with self._lock:
            self.last_error, self.last_error_at = msg, time.time()

    def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
        temperature: float = 0.1,
        timeout: float | None = None,
    ) -> ChatResult:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature,
            **self.extra_body,
        }
        try:
            with Timer() as t:
                data = post_json(
                    f"{self.url}/chat/completions",
                    payload,
                    timeout=timeout or self.timeout,
                    headers=self._headers,
                )
            raw, finish, reasoning = _extract_choice(data)
        except AIServiceError as exc:
            self._note_error(str(exc))
            raise
        self.last_ok_at = time.time()
        return ChatResult(
            text=strip_think(raw),
            raw_text=raw,
            usage=data.get("usage") or {},
            model=data.get("model") or self.model,
            latency_ms=t.ms,
            finish_reason=finish,
            reasoning=reasoning,
        )

    def describe_image(self, image: bytes, mime: str, prompt: str) -> ChatResult:
        """Read a scan/plan with the multimodal model. Only allowed with LLM_VISION=1.

        The answer is streamed and watched: a repetition loop ends the call at once (the server stops
        generating when the connection closes). Such a page is asked once more with VISION_RETRY_BODY, a page
        merely cut short once more with the full token amount. Fails again: :class:`IncompleteAnswer`."""
        if not self.vision:
            raise AIServiceError("Bildverarbeitung ist ausgeschaltet (LLM_VISION=0)")
        url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
        # a whole page with its handwriting and stamps (a dense A4 page is 1.500 to 2.500 tokens)
        full = max(self.max_tokens, VISION_MAX_TOKENS)
        first = self._read_page(messages, page_tokens(prompt, full), {})
        if _usable(first):
            return first
        retry = self.retry_body if first.loop else {}
        log.info("Bildauswertung: %s (finish_reason %s, %d Zeichen, max_tokens %d) – neuer Versuch%s",
                 "Wiederholungsschleife" if first.loop else "Antwort abgeschnitten", first.finish_reason,
                 len(first.raw_text), page_tokens(prompt, full), " mit VISION_RETRY_BODY" if retry else " mit mehr Tokens")
        second = self._read_page(messages, full, retry)
        if _usable(second):
            return second
        loop = first.loop or second.loop
        if second.loop:
            text = before_loop(second.text)
        else:
            text = second.text if not has_unterminated_think(second.raw_text) else ""
        reason = "Wiederholungsschleife" if second.loop else "Antwort zu lang (abgeschnitten)"
        raise IncompleteAnswer(f"Modell-Antwort verworfen: {reason}", text, loop)

    def _read_page(self, messages: list[dict], max_tokens: int, extra: dict) -> ChatResult:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            **self.extra_body,
            **extra,
            "stream": True,
        }
        content: list[str] = []
        reasoning: list[str] = []
        finish: str | None = None
        loop = False
        try:
            with Timer() as t:
                with post_stream(f"{self.url}/chat/completions", payload, timeout=self.timeout,
                                 headers=self._headers) as answer:
                    if not answer.is_stream:  # the server ignored "stream": one JSON answer
                        raw, finish, think = _extract_choice(answer.json())
                        content, reasoning = [raw], [think]
                    else:
                        size = checked = 0
                        for event in answer.events():
                            delta, part_finish = _delta(event)
                            if delta[0]:
                                content.append(delta[0])
                                size += len(delta[0])
                            if delta[1]:
                                reasoning.append(delta[1])
                                size += len(delta[1])
                            finish = part_finish or finish
                            if size - checked >= _CHECK_EVERY:
                                checked = size
                                if (find_loop("".join(content), window=STREAM_WINDOW) is not None
                                        or find_loop("".join(reasoning), window=STREAM_WINDOW) is not None):
                                    loop, finish = True, "loop"
                                    break  # leaving the block closes the connection: the server stops
        except AIServiceError as exc:
            self._note_error(str(exc))
            raise
        self.last_ok_at = time.time()
        raw = "".join(content)
        text = strip_think(raw)
        think = "".join(reasoning)
        loop = loop or find_loop(text) is not None or find_loop(think) is not None
        return ChatResult(text=text, raw_text=raw, usage={}, model=self.model, latency_ms=t.ms,
                          finish_reason=finish, reasoning=think, loop=loop)

    def probe(self, timeout: float) -> dict:
        """Reachability and format check for /health/deps: a chat with a few tokens decides ``ok``.

        The model list (``GET /models``) is optional – some llama.cpp/proxy setups do not offer it;
        then it is only reported as a hint (``models_error``)."""
        info: dict = {"url": self.url, "model": self.model, "vision": self.vision}
        try:
            models = get_json(f"{self.url}/models", timeout=timeout, headers=self._headers)
            ids = [m.get("id") for m in (models.get("data") or []) if isinstance(m, dict)]
            info["models_available"] = ids
            info["model_listed"] = self.model in ids
            if ids and self.model not in ids:
                info["hint"] = f"Modell {self.model} steht nicht in der Modellliste des Servers"
        except AIServiceError as exc:
            info["models_error"] = str(exc)
            info["hint"] = "Modellliste (/models) nicht verfügbar – geprüft wird nur der Chat"
        except (AttributeError, TypeError) as exc:
            info["models_error"] = f"unerwartetes Format der Modellliste: {exc}"
        try:
            res = self.chat([{"role": "user", "content": "Sag OK."}], max_tokens=8, timeout=timeout)
            info.update(ok=True, format="openai-chat", latency_ms=res.latency_ms, sample=res.text[:40],
                        finish_reason=res.finish_reason)
        except AIServiceError as exc:
            info.update(ok=False, format=None, error=str(exc))
        except (AttributeError, TypeError) as exc:
            info.update(ok=False, format=None, error=f"unerwartetes Antwortformat: {exc}")
        return info


def page_tokens(prompt: str, full: int) -> int:
    """Tokens for the first reading of a page: with the page's text so far, about twice the prompt's characters in
    tokens plus room for the handwriting (German text has 3 to 4 characters per token), at least PAGE_MIN_TOKENS;
    without it ``full``."""
    if _KNOWN_TEXT not in prompt:
        return full
    return max(min(PAGE_MIN_TOKENS, full), min(full, len(prompt) // 2 + 500))


def _usable(res: ChatResult) -> bool:
    return not res.loop and res.finish_reason != "length" and not has_unterminated_think(res.raw_text)


def _delta(event) -> tuple[tuple[str, str], str | None]:
    """((content, reasoning), finish_reason) of one streamed Chat-Completions event."""
    try:
        choice = event["choices"][0]
    except (KeyError, IndexError, TypeError):
        return ("", ""), None  # e.g. a last event with usage only
    if not isinstance(choice, dict):
        return ("", ""), None
    delta = choice.get("delta") or {}
    if not isinstance(delta, dict):
        delta = {}
    content = delta.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    think = delta.get("reasoning_content") or delta.get("reasoning") or ""
    finish = choice.get("finish_reason")
    return ((content if isinstance(content, str) else ""), (think if isinstance(think, str) else "")), (
        str(finish) if finish else None)


def _extract_choice(data) -> tuple[str, str | None, str]:
    """(content, finish_reason, reasoning) of the first choice of a Chat-Completions response."""
    content = _extract_content(data)
    choice = data["choices"][0]
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    msg = (choice.get("message") or {}) if isinstance(choice, dict) else {}
    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "") if isinstance(msg, dict) else ""
    return content, (str(finish) if finish else None), (reasoning if isinstance(reasoning, str) else "")


def _extract_content(data) -> str:
    try:
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        if content is None:
            content = choice.get("text", "")
        if isinstance(content, list):  # content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return str(content or "")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise AIServiceError("unerwartetes Antwortformat des Sprachmodells") from None
