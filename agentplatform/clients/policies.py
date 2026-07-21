"""Call policies as composable adapters.

The reliability behaviour around a vendor call - tracing, idempotency, circuit
breaking, rate limiting, retry - is not one behaviour. It is five, and different
vendors need different combinations of them. Salesforce has strict API limits and
needs pacing; Slack should fail fast rather than queue; a flaky vendor needs a
circuit breaker so we stop hammering it during an outage.

Inheritance models that badly: one base class means every vendor gets identical
policy, and adding a circuit breaker for one means editing the class all of them
share. So policies are decorators over a transport instead, assembled per vendor
from config:

    Tracing(Idempotency(CircuitBreaker(RateLimit(Retry(Transport)))))

Each policy does one thing, is independently testable, and can be reordered or
dropped per vendor without touching any other. Adding a new cross-cutting concern
(request signing, PII scrubbing, a response cache) means writing one class here
and naming it in config - no vendor client changes.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..observability import OK, RunTrace


@dataclass
class Call:
    """One vendor call travelling down the policy chain."""

    tool: str
    operation: str
    payload: dict[str, Any]
    trace: RunTrace
    idempotency_key: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def step(self) -> str:
        return f"{self.tool}.{self.operation}"


class Handler(Protocol):
    def handle(self, call: Call) -> dict[str, Any]: ...


class ToolError(RuntimeError):
    """A vendor call failed and no policy could recover it."""

    def __init__(self, tool: str, operation: str, message: str, retryable: bool = False) -> None:
        super().__init__(f"{tool}.{operation}: {message}")
        self.tool = tool
        self.operation = operation
        self.retryable = retryable


class CircuitOpen(ToolError):
    """The breaker is open: the vendor is down and we are not making it worse."""


class Policy(ABC):
    """Wraps another handler. Subclasses implement exactly one concern."""

    def __init__(self, inner: Handler) -> None:
        self.inner = inner

    @abstractmethod
    def handle(self, call: Call) -> dict[str, Any]: ...


def is_retryable(exc: Exception) -> bool:
    """Timeouts and connection faults are worth retrying.

    A 4xx means we sent something wrong; retrying it only burns rate limit and
    delays the real error, so it is deliberately excluded.
    """
    return isinstance(exc, (TimeoutError, ConnectionError))


class TransportHandler:
    """Terminates the chain by performing the actual vendor call."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    def handle(self, call: Call) -> dict[str, Any]:
        return self.transport.execute(call.operation, call.payload)


class RetryPolicy(Policy):
    def __init__(self, inner: Handler, max_attempts: int = 3,
                 backoff_seconds: float = 0.25, backoff_multiplier: float = 2.0) -> None:
        super().__init__(inner)
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.backoff_multiplier = backoff_multiplier

    def handle(self, call: Call) -> dict[str, Any]:
        delay = self.backoff_seconds
        last: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            call.meta["attempt"] = attempt
            try:
                return self.inner.handle(call)
            except Exception as exc:
                last = exc
                if not is_retryable(exc) or attempt == self.max_attempts:
                    break
                call.trace.record(call.step, "error", error=str(exc),
                                  attempt=attempt, retrying=True)
                time.sleep(delay)
                delay *= self.backoff_multiplier

        raise ToolError(call.tool, call.operation, str(last),
                        retryable=is_retryable(last) if last else False)


class IdempotencyPolicy(Policy):
    """Same key, same result, one side effect.

    Sits above retry and the breaker so a redelivered webhook short-circuits
    before any network work happens at all.
    """

    def __init__(self, inner: Handler) -> None:
        super().__init__(inner)
        self._seen: dict[str, dict[str, Any]] = {}

    def handle(self, call: Call) -> dict[str, Any]:
        key = call.idempotency_key
        if key is None:
            return self.inner.handle(call)

        if key in self._seen:
            call.trace.record(call.step, OK, deduplicated=True, idempotency_key=key)
            return self._seen[key]

        result = self.inner.handle(call)
        self._seen[key] = result
        return result


class CircuitBreakerPolicy(Policy):
    """Stop calling a vendor that is clearly down.

    Without this, an outage turns every queued event into a slow failure and the
    retry policy multiplies the load on a system already struggling.
    """

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, inner: Handler, failure_threshold: int = 5,
                 cooldown_seconds: float = 30.0) -> None:
        super().__init__(inner)
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0

    def handle(self, call: Call) -> dict[str, Any]:
        if self.state == self.OPEN:
            if time.monotonic() - self._opened_at < self.cooldown_seconds:
                call.trace.record(call.step, "error", circuit="open",
                                  error="circuit open, call not attempted")
                raise CircuitOpen(call.tool, call.operation,
                                  f"circuit open after {self._failures} consecutive failures")
            # Cooldown elapsed: let one call through to test the water.
            self.state = self.HALF_OPEN

        try:
            result = self.inner.handle(call)
        except Exception:
            self._failures += 1
            if self._failures >= self.failure_threshold or self.state == self.HALF_OPEN:
                self.state = self.OPEN
                self._opened_at = time.monotonic()
                call.trace.record(call.step, "error", circuit="opened",
                                  consecutive_failures=self._failures)
            raise

        self._failures = 0
        self.state = self.CLOSED
        return result


class RateLimitPolicy(Policy):
    """Minimum spacing between calls to one vendor.

    Cheap insurance: being throttled by a vendor is slower and harder to debug
    than pacing ourselves.
    """

    def __init__(self, inner: Handler, min_interval_seconds: float = 0.0) -> None:
        super().__init__(inner)
        self.min_interval = min_interval_seconds
        self._last_call = 0.0

    def handle(self, call: Call) -> dict[str, Any]:
        if self.min_interval > 0:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()
        return self.inner.handle(call)


class FailureInjectionPolicy(Policy):
    """Demo and test hook: make a vendor fail on demand.

    Being able to prove the retry, breaker and dead-letter paths work on a live
    system is worth far more than asserting they exist.
    """

    def __init__(self, inner: Handler, rate: float = 0.0) -> None:
        super().__init__(inner)
        self.rate = rate

    def handle(self, call: Call) -> dict[str, Any]:
        if self.rate > 0:
            import random
            if random.random() < self.rate:
                raise TimeoutError(f"injected failure calling {call.step}")
        return self.inner.handle(call)


class TracingPolicy(Policy):
    """Outermost: every call gets exactly one timed, redacted trace row."""

    SENSITIVE = ("token", "secret", "password", "key", "authorization")

    def handle(self, call: Call) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            result = self.inner.handle(call)
        except Exception as exc:
            call.trace.record(
                call.step, "error", latency_ms=int((time.perf_counter() - start) * 1000),
                error=str(exc), attempt=call.meta.get("attempt", 1),
                retryable=is_retryable(exc),
            )
            raise

        call.trace.record(
            call.step, OK, latency_ms=int((time.perf_counter() - start) * 1000),
            attempt=call.meta.get("attempt", 1),
            request=self._redact(call.payload),
            response_summary=self._summarise(result),
        )
        return result

    @classmethod
    def _redact(cls, payload: dict[str, Any]) -> dict[str, Any]:
        redacted = {}
        for key, value in payload.items():
            if any(s in key.lower() for s in cls.SENSITIVE):
                redacted[key] = "***"
            elif isinstance(value, str) and len(value) > 200:
                redacted[key] = value[:200] + "..."
            else:
                redacted[key] = value
        return redacted

    @staticmethod
    def _summarise(result: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in result.items()
                if k in ("id", "status", "url", "ok", "channel")}


# Registry of available policies. Config names these; order in the config list is
# outermost-first, so the chain is built by wrapping in reverse.
POLICIES: dict[str, type[Policy]] = {
    "tracing": TracingPolicy,
    "idempotency": IdempotencyPolicy,
    "circuit_breaker": CircuitBreakerPolicy,
    "rate_limit": RateLimitPolicy,
    "retry": RetryPolicy,
    "failure_injection": FailureInjectionPolicy,
}


def build_chain(transport: Any, policy_names: list[str],
                settings: dict[str, Any]) -> Handler:
    """Assemble a policy chain around a transport.

    `policy_names` is outermost-first. Unknown names fail loudly at startup rather
    than silently leaving a vendor with no retry policy in production.
    """
    unknown = [name for name in policy_names if name not in POLICIES]
    if unknown:
        raise ValueError(
            f"Unknown tool policies {unknown}. Available: {sorted(POLICIES)}"
        )

    handler: Handler = TransportHandler(transport)
    for name in reversed(policy_names):
        handler = POLICIES[name](handler, **(settings.get(name) or {}))
    return handler
