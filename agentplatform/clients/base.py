"""Shared tool-client machinery.

Every vendor call in the platform goes through `ToolClient.call`. Retries,
backoff, idempotency, timeouts, failure classification and per-call trace logging
live here exactly once. A vendor client subclass only implements the mapping from
an operation name to a request - it inherits all the reliability behaviour.

This is the "shared functions so updates are made once and fixed once" layer: when
Salesforce changes an endpoint, one subclass method changes and every agent that
uses it is fixed simultaneously.
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from typing import Any

from ..config import Config
from ..observability import OK, RunTrace


class ToolError(RuntimeError):
    """A vendor call failed after exhausting retries."""

    def __init__(self, tool: str, operation: str, message: str, retryable: bool) -> None:
        super().__init__(f"{tool}.{operation}: {message}")
        self.tool = tool
        self.operation = operation
        self.retryable = retryable


class ToolPermissionError(RuntimeError):
    """An agent tried to use a tool its registry entry does not declare."""


class ToolClient(ABC):
    """Base for every vendor client."""

    name: str = "tool"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.max_attempts = config.get("tools.retry.max_attempts", 3)
        self.backoff = config.get("tools.retry.backoff_seconds", 0.25)
        self.multiplier = config.get("tools.retry.backoff_multiplier", 2.0)
        self.failure_rate = config.get("tools.failure_injection_rate", 0.0)
        # Idempotency ledger. In production this is Redis or a warehouse table;
        # the semantics are what matter: the same key never acts twice.
        self._idempotency: dict[str, Any] = {}

    @abstractmethod
    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Perform the actual vendor call. Subclasses implement only this."""

    def call(
        self,
        operation: str,
        payload: dict[str, Any],
        trace: RunTrace,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        step = f"{self.name}.{operation}"

        if idempotency_key and idempotency_key in self._idempotency:
            # A redelivered webhook must not create a second Salesforce task.
            trace.record(step, OK, deduplicated=True, idempotency_key=idempotency_key)
            return self._idempotency[idempotency_key]

        last_error: Exception | None = None
        delay = self.backoff

        for attempt in range(1, self.max_attempts + 1):
            start = time.perf_counter()
            try:
                self._maybe_inject_failure(operation)
                result = self._execute(operation, payload)
            except Exception as exc:
                last_error = exc
                elapsed = int((time.perf_counter() - start) * 1000)
                retryable = self._is_retryable(exc)
                trace.record(
                    step, "error", latency_ms=elapsed, error=str(exc),
                    attempt=attempt, retryable=retryable,
                )
                if not retryable or attempt == self.max_attempts:
                    break
                time.sleep(delay)
                delay *= self.multiplier
                continue

            elapsed = int((time.perf_counter() - start) * 1000)
            trace.record(step, OK, latency_ms=elapsed, attempt=attempt,
                         request=self._redact(payload), response_summary=self._summarise(result))

            if idempotency_key:
                self._idempotency[idempotency_key] = result
            return result

        raise ToolError(self.name, operation, str(last_error),
                        retryable=self._is_retryable(last_error) if last_error else False)

    def _maybe_inject_failure(self, operation: str) -> None:
        """Demo/testing hook: prove the retry and DLQ paths work on demand."""
        if self.failure_rate > 0 and random.random() < self.failure_rate:
            raise TimeoutError(f"injected failure calling {self.name}.{operation}")

    @staticmethod
    def _is_retryable(exc: Exception | None) -> bool:
        # Timeouts and 5xx are worth retrying; a 4xx means we sent something wrong
        # and retrying just burns rate limit.
        return isinstance(exc, (TimeoutError, ConnectionError))

    @staticmethod
    def _redact(payload: dict[str, Any]) -> dict[str, Any]:
        """Traces are read by humans; never log secrets into them."""
        redacted = {}
        for key, value in payload.items():
            if any(s in key.lower() for s in ("token", "secret", "password", "key")):
                redacted[key] = "***"
            elif isinstance(value, str) and len(value) > 200:
                redacted[key] = value[:200] + "…"
            else:
                redacted[key] = value
        return redacted

    @staticmethod
    def _summarise(result: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in result.items() if k in ("id", "status", "url", "ok", "channel")}


class ScopedToolBelt:
    """The tools one agent is allowed to use, pre-bound to its trace.

    Enforces the registry's `tools:` declaration. An agent that was never granted
    Zendesk cannot reach Zendesk, even by accident.
    """

    def __init__(self, clients: dict[str, ToolClient], allowed: list[str], trace: RunTrace) -> None:
        self._clients = clients
        self._allowed = set(allowed)
        self._trace = trace

    def __getattr__(self, name: str) -> "BoundClient":
        if name not in self._allowed:
            raise ToolPermissionError(
                f"Agent is not granted tool '{name}'. Granted: {sorted(self._allowed)}. "
                f"Add it to the agent's `tools:` list in config/registry.yaml."
            )
        client = self._clients.get(name)
        if client is None:
            raise ToolPermissionError(f"Unknown tool '{name}'")
        return BoundClient(client, self._trace)


class BoundClient:
    """A client with the trace already attached, so agents cannot forget to log."""

    def __init__(self, client: ToolClient, trace: RunTrace) -> None:
        self._client = client
        self._trace = trace

    def call(self, operation: str, payload: dict[str, Any],
             idempotency_key: str | None = None) -> dict[str, Any]:
        return self._client.call(operation, payload, self._trace, idempotency_key)


class ToolBelt:
    """Owns one instance of each vendor client for the whole process."""

    def __init__(self, clients: dict[str, ToolClient]) -> None:
        self._clients = clients

    def scoped(self, allowed: list[str], trace: RunTrace) -> ScopedToolBelt:
        return ScopedToolBelt(self._clients, allowed, trace)

    def raw(self, name: str) -> ToolClient:
        return self._clients[name]
