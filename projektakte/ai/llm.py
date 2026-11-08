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


@dataclass
class ChatResult:
    text: str
    raw_text: str
    usage: dict
    model: str
    latency_ms: float


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
        self._headers = bearer(api_key)
        self.extra_body = dict(extra_body or {})
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_error_at: float | None = None

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
            raw = _extract_content(data)
        except AIServiceError as exc:
            self._note_error(str(exc))
            raise
        return ChatResult(
            text=strip_think(raw),
            raw_text=raw,
            usage=data.get("usage") or {},
            model=data.get("model") or self.model,
            latency_ms=t.ms,
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
        """Reachability and format check for /health/deps (lists models, sends a 1-token chat)."""
        info: dict = {"url": self.url, "model": self.model, "vision": self.vision}
        try:
            with Timer() as t:
                models = get_json(f"{self.url}/models", timeout=timeout, headers=self._headers)
                ids = [m.get("id") for m in (models.get("data") or []) if isinstance(m, dict)]
                info["models_available"] = ids
                info["model_listed"] = self.model in ids
                res = self.chat(
                    [{"role": "user", "content": "Sag OK."}], max_tokens=8, timeout=timeout
                )
            info.update(ok=True, format="openai-chat", latency_ms=t.ms, sample=res.text[:40])
        except AIServiceError as exc:
            info.update(ok=False, format=None, error=str(exc))
        except (AttributeError, TypeError) as exc:
            info.update(ok=False, format=None, error=f"unerwartetes Antwortformat: {exc}")
        return info


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
