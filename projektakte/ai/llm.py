"""Client for an OpenAI-compatible Chat-Completions server (vLLM, llama.cpp, …)."""
from __future__ import annotations

import base64
import re
import threading
import time
from dataclasses import dataclass

from .http import AIServiceError, Timer, bearer, get_json, post_json

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

    @property
    def truncated(self) -> bool:
        """The model ran out of tokens or produced only reasoning – the answer is incomplete."""
        return (self.finish_reason == "length" or has_unterminated_think(self.raw_text)
                or (not self.text and bool(self.reasoning or self.raw_text)))


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
    ):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.vision = vision
        self._api_key = api_key
        self._headers = bearer(api_key)
        self.extra_body = dict(extra_body or {})
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_error_at: float | None = None

    def config(self) -> dict:
        """What this client was built with (plain data, contains the API key): the extraction worker
        process builds the same client from it with :meth:`from_config`."""
        return {"url": self.url, "model": self.model, "timeout": self.timeout, "max_tokens": self.max_tokens,
                "vision": self.vision, "api_key": self._api_key, "extra_body": dict(self.extra_body)}

    @classmethod
    def from_config(cls, config: dict) -> "LLMClient":
        return cls(config["url"], config["model"], timeout=config["timeout"], max_tokens=config["max_tokens"],
                   vision=config["vision"], api_key=config["api_key"], extra_body=config["extra_body"])

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
        """Read a scan/plan with the multimodal model. Only allowed with LLM_VISION=1."""
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
        return self.chat(messages, max_tokens=max(self.max_tokens, 1500))

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
