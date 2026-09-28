"""Groq LLM wrapper with retries, model fallback and tolerant JSON parsing."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response (handles <think> blocks and code fences)."""
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", _THINK_RE.sub("", text)).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(cleaned[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


_RETRY_IN_RE = re.compile(r"try again in ([\d.]+)s")


def _retry_after(exc: Exception) -> float:
    """Seconds to wait from a Groq 429 (retry-after header, else the 'try again in Xs' message)."""
    response = getattr(exc, "response", None)
    try:
        return float(response.headers["retry-after"])
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    m = _RETRY_IN_RE.search(str(exc))
    return float(m.group(1)) if m else 60.0


class LLM:
    max_rate_limit_wait = 15.0

    def __init__(self, api_key: str, model: str, fallback_model: str | None = None, retries: int = 2):
        """`fallback_model` may be a comma-separated list, tried in order."""
        self.model = model
        fallbacks = [m.strip() for m in (fallback_model or "").split(",") if m.strip()]
        self.models = list(dict.fromkeys([model, *fallbacks]))
        self.retries = retries
        self.client = None
        if api_key:
            from groq import Groq

            # Retries are handled in _run so a rate-limited model can hand over to the fallback immediately.
            self.client = Groq(api_key=api_key, timeout=45.0, max_retries=0)

    @property
    def available(self) -> bool:
        return self.client is not None

    @staticmethod
    def _reasoning_kwargs(model: str) -> dict[str, Any]:
        # Reasoning models spend max_tokens on hidden thinking; without these the answer can come back empty.
        if model.startswith("openai/gpt-oss"):
            return {"reasoning_effort": "medium"}
        if model.startswith("qwen/"):
            return {"reasoning_format": "hidden"}
        return {}

    def _chat(self, model: str, messages: list[dict[str, str]], json_mode: bool) -> str:
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 4096}
        kwargs.update(self._reasoning_kwargs(model))
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    def _try_model(self, model: str, messages: list[dict[str, str]], json_mode: bool) -> tuple[Any, float | None]:
        """Returns (result or None, seconds Groq asked us to wait if rate limited)."""
        for attempt in range(self.retries + 1):
            try:
                # JSON mode occasionally fails validation on Groq; last attempt retries without it.
                use_json = json_mode and attempt < self.retries
                text = self._chat(model, messages, use_json)
                if not json_mode:
                    return _THINK_RE.sub("", text).strip(), None
                parsed = extract_json(text)
                if parsed is not None:
                    return parsed, None
                log.warning("Model %s returned non-JSON output (attempt %d)", model, attempt + 1)
            except Exception as exc:
                if getattr(exc, "status_code", None) == 429:
                    # Free-tier limits are per model, so the next model usually has headroom; don't wait here.
                    log.warning("Rate limited on %s, switching model", model)
                    return None, _retry_after(exc)
                log.warning("LLM call failed on %s (attempt %d): %s", model, attempt + 1, exc)
                time.sleep(min(2**attempt, 4))
        return None, None

    def _run(self, messages: list[dict[str, str]], json_mode: bool) -> str | dict[str, Any] | None:
        if not self.available:
            return None
        waits: list[float] = []
        for model in self.models:
            result, wait = self._try_model(model, messages, json_mode)
            if result is not None:
                return result
            if wait is not None:
                waits.append(wait)
            log.warning("Falling back from model %s", model)
        # Every model was rate limited: a short wait beats degrading to the rule-based plan.
        if waits and len(waits) == len(self.models) and min(waits) <= self.max_rate_limit_wait:
            log.warning("All models rate limited; waiting %.1fs", min(waits))
            time.sleep(min(waits))
            return self._try_model(self.model, messages, json_mode)[0]
        return None

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        result = self._run([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True)
        return result if isinstance(result, dict) else None

    def complete_text(self, system: str, user: str) -> str | None:
        result = self._run([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=False)
        return result if isinstance(result, str) else None


def build_llm() -> LLM:
    return LLM(settings.groq_api_key, settings.groq_model, settings.groq_fallback_model)
