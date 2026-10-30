"""LLM providers (stdlib HTTP, no SDK dependency) and a structured-output helper.

Provider selection (--llm auto):
    ANTHROPIC_API_KEY set           -> Anthropic Messages API
    not set                         -> offline: agents use deterministic playbooks

Other providers plug in by subclassing LLMProvider and implementing complete().

Guardrails here: a per-run call/token budget, request timeouts, and output
validation. Workspace content sent to a model is wrapped as quoted data, and
model output is never executed; it is parsed into structured results that the
engine policy-checks before applying.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class Budget:
    def __init__(self, max_calls: int = 60, max_tokens: int = 400_000) -> None:
        self.max_calls, self.max_tokens = max_calls, max_tokens
        self.calls = self.tokens = 0
        self._lock = threading.Lock()

    def charge(self, tokens: int = 0, call: bool = False) -> None:
        with self._lock:
            if call:
                if self.calls >= self.max_calls:
                    raise BudgetExceeded(f"LLM call budget exhausted ({self.max_calls})")
                self.calls += 1
            self.tokens += tokens
            if self.tokens > self.max_tokens:
                raise BudgetExceeded(f"LLM token budget exhausted ({self.max_tokens})")


class LLMProvider:
    name = "offline"
    available = False

    def __init__(self, budget: Budget | None = None) -> None:
        self.budget = budget or Budget()

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> LLMResponse:
        raise LLMError("no LLM provider configured")


class OfflineProvider(LLMProvider):
    pass


def _post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise LLMError(f"HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LLMError(f"request failed: {exc}") from exc


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    available = True

    def __init__(self, api_key: str, model: str | None = None, budget: Budget | None = None,
                 timeout: float = 120) -> None:
        super().__init__(budget)
        self.api_key = api_key
        self.model = model or os.getenv("AGENTIC_MODEL", "claude-sonnet-5-5")
        self.base = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        self.timeout = timeout

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> LLMResponse:
        self.budget.charge(call=True)
        data = _post(
            f"{self.base}/v1/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
             "content-type": "application/json"},
            {"model": self.model, "max_tokens": max_tokens, "system": system,
             "messages": [{"role": "user", "content": prompt}]},
            self.timeout,
        )
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        resp = LLMResponse(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0), self.model)
        self.budget.charge(tokens=resp.input_tokens + resp.output_tokens)
        return resp


def make_provider(mode: str = "auto", budget: Budget | None = None) -> LLMProvider:
    mode = mode.lower()
    if mode in ("auto", "anthropic") and os.getenv("ANTHROPIC_API_KEY"):
        return AnthropicProvider(os.environ["ANTHROPIC_API_KEY"], budget=budget)
    if mode == "anthropic":
        raise LLMError(f"--llm {mode} requested but its API key is not set")
    return OfflineProvider(budget)


JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict:
    """Parse the first JSON object in a model response (fenced or bare)."""
    m = JSON_BLOCK.search(text)
    candidate = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    if not candidate:
        raise LLMError("response contained no JSON object")
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMError(f"invalid JSON in response: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMError("response JSON must be an object")
    return value


def as_data(label: str, content: str, limit: int = 12000) -> str:
    """Quote untrusted content (files, prior outputs) so it reads as data, not instructions."""
    clipped = content if len(content) <= limit else content[:limit] + "\n...[truncated]"
    return f'<data label="{label}">\n{clipped}\n</data>'
