"""LLM access, shared by every agent.

No agent calls OpenRouter directly. They call this, and get back a *validated
Pydantic object* or an exception - never a raw string. That single rule is what
stops hallucinated or malformed output from reaching Salesforce.

What this layer owns:
  - the model fallback chain (a vendor deprecating a model is a config change)
  - JSON-schema-constrained output, validated with Pydantic
  - one repair round-trip when validation fails, with the error fed back
  - cost and token accounting, written to the trace
  - a hard failure signal so callers can fall back to deterministic logic
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Config, llm_mode, openrouter_api_key
from .observability import RunTrace

T = TypeVar("T", bound=BaseModel)

# Approximate USD per 1M tokens. Used for budget accounting and the cost shown in
# the dashboard. Override in config under llm.pricing when vendor prices move.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "anthropic/claude-sonnet-4.5": (3.00, 15.00),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "google/gemini-2.0-flash-001": (0.10, 0.40),
}
FALLBACK_PRICE = (1.00, 3.00)


class LLMUnavailable(RuntimeError):
    """Every model in the chain failed, or output never validated.

    Callers must handle this by degrading to deterministic logic - never by
    dropping the work silently.
    """


@dataclass
class PromptBundle:
    """A prompt is a versioned artefact, not a string literal buried in code."""

    name: str
    version: str
    system: str
    user: str

    def fingerprint(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass
class LLMMeta:
    model: str
    attempts: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    repaired: bool = False
    errors: list[str] = field(default_factory=list)


def _extract_json(text: str) -> dict[str, Any]:
    """Models wrap JSON in prose or fences more often than anyone admits."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])

    raise ValueError("no JSON object found in model output")


def _price(model: str, config: Config) -> tuple[float, float]:
    configured = config.get("llm.pricing", {}) or {}
    if model in configured:
        entry = configured[model]
        return float(entry["input"]), float(entry["output"])
    return DEFAULT_PRICING.get(model, FALLBACK_PRICE)


class LLMClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.base_url = config.get("llm.base_url", "https://openrouter.ai/api/v1")
        self.model_chain: list[str] = config.get("llm.model_chain", [])
        self.temperature = config.get("llm.temperature", 0.1)
        self.max_tokens = config.get("llm.max_tokens", 900)
        self.timeout = config.get("llm.timeout_seconds", 30)
        self.max_repair = config.get("llm.max_repair_attempts", 1)

    def _budget_remaining(self, trace: RunTrace) -> float | None:
        """Spend left in today's budget, or None when no budget is configured."""
        budget = self.config.get("llm.daily_cost_budget_usd")
        if not budget:
            return None

        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()
        return float(budget) - trace.warehouse.llm_spend_since(midnight)

    def complete_structured(
        self, bundle: PromptBundle, schema: type[T], trace: RunTrace
    ) -> tuple[T, LLMMeta]:
        """Return a validated instance of `schema`, or raise LLMUnavailable."""
        if llm_mode() == "offline":
            raise LLMUnavailable("llm_offline_mode")

        api_key = openrouter_api_key()
        if not api_key:
            raise LLMUnavailable("no_openrouter_api_key")

        # Budget exhaustion is treated exactly like the model being unavailable:
        # the caller degrades to deterministic analysis and the human is still
        # alerted. A runaway spend must never become a silent outage, and it must
        # never become an unbounded bill either.
        remaining = self._budget_remaining(trace)
        if remaining is not None and remaining <= 0:
            trace.record("llm_budget_exhausted", "degraded",
                         daily_budget_usd=self.config.get("llm.daily_cost_budget_usd"),
                         remaining_usd=round(remaining, 4))
            raise LLMUnavailable(
                f"daily_cost_budget_exhausted (budget "
                f"${self.config.get('llm.daily_cost_budget_usd')})"
            )

        errors: list[str] = []
        attempts = 0

        for model in self.model_chain:
            messages = [
                {"role": "system", "content": bundle.system},
                {"role": "user", "content": bundle.user},
            ]

            for repair_round in range(self.max_repair + 1):
                attempts += 1
                try:
                    content, tokens_in, tokens_out = self._call(model, messages, api_key)
                except Exception as exc:
                    # Model unavailable / rate limited / network. Try the next model.
                    errors.append(f"{model}: transport: {type(exc).__name__}: {exc}")
                    break

                in_price, out_price = _price(model, self.config)
                cost = (tokens_in / 1_000_000) * in_price + (tokens_out / 1_000_000) * out_price
                trace.record_cost(model, tokens_in, tokens_out, cost)

                try:
                    validated = schema.model_validate(_extract_json(content))
                except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"{model}: validation: {exc}")
                    if repair_round >= self.max_repair:
                        break
                    # Feed the exact validation error back. Models fix their own
                    # schema violations far more reliably than they avoid them.
                    messages.extend([
                        {"role": "assistant", "content": content},
                        {"role": "user", "content":
                            f"Your response failed schema validation with this error:\n{exc}\n\n"
                            f"Return ONLY corrected JSON matching the schema. No prose."},
                    ])
                    continue

                return validated, LLMMeta(
                    model=model,
                    attempts=attempts,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost,
                    repaired=repair_round > 0,
                    errors=errors,
                )

        raise LLMUnavailable(
            f"all {len(self.model_chain)} models failed after {attempts} attempts: {errors}"
        )

    def _call(
        self, model: str, messages: list[dict[str, str]], api_key: str
    ) -> tuple[str, int, int]:
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # OpenRouter attribution headers.
                "HTTP-Referer": "https://github.com/supermetrics/agent-platform",
                "X-Title": "Supermetrics Agent Platform",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()

        content = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        return (
            content,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
