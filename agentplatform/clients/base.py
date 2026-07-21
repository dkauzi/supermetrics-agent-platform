"""Tool access: transports, scoping, and the toolbelt.

A vendor client here is a *transport* and nothing more - it maps an operation name
to a request and returns a response. Everything else (tracing, idempotency,
circuit breaking, rate limiting, retry) is a policy chain composed around it from
config. See `policies.py` for why that is a chain rather than a base class.

The practical payoff: when Salesforce changes an endpoint I edit one `_execute`.
When I want Salesforce paced differently from Slack, I edit config. Neither change
touches the other, and neither touches any agent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..config import Config
from ..observability import RunTrace
from .policies import Call, CircuitBreakerPolicy, Handler, Policy, ToolError, build_chain


class ToolPermissionError(RuntimeError):
    """An agent tried to use a tool its registry entry does not declare."""


class ToolClient(ABC):
    """Transport base. Subclasses implement `_execute` and nothing else."""

    name: str = "tool"

    def __init__(self, config: Config) -> None:
        self.config = config

    @abstractmethod
    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Perform the vendor call. No retry, logging or dedupe logic belongs here."""

    def execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._execute(operation, payload)


class BoundClient:
    """A policy chain with the trace already attached.

    Agents receive these, so they cannot forget to pass a trace and cannot opt out
    of the reliability policy their vendor is configured with.
    """

    def __init__(self, tool: str, chain: Handler, trace: RunTrace) -> None:
        self._tool = tool
        self._chain = chain
        self._trace = trace

    def call(self, operation: str, payload: dict[str, Any],
             idempotency_key: str | None = None) -> dict[str, Any]:
        return self._chain.handle(Call(
            tool=self._tool, operation=operation, payload=payload,
            trace=self._trace, idempotency_key=idempotency_key,
        ))


class ScopedToolBelt:
    """The tools one agent is allowed to use.

    Enforces the registry's `tools:` declaration. An agent never granted Zendesk
    cannot reach Zendesk, even by accident. Least privilege enforced by the
    platform rather than by convention.
    """

    def __init__(self, chains: dict[str, Handler], allowed: list[str], trace: RunTrace) -> None:
        self._chains = chains
        self._allowed = set(allowed)
        self._trace = trace

    def __getattr__(self, name: str) -> BoundClient:
        if name not in self._allowed:
            raise ToolPermissionError(
                f"Agent is not granted tool '{name}'. Granted: {sorted(self._allowed)}. "
                f"Add it to the agent's `tools:` list in config/registry.yaml."
            )
        chain = self._chains.get(name)
        if chain is None:
            raise ToolPermissionError(f"Unknown tool '{name}'")
        return BoundClient(name, chain, self._trace)


class ToolBelt:
    """Owns one transport and one policy chain per vendor, process-wide.

    The chains are stateful on purpose: the idempotency ledger and the circuit
    breaker's failure count only mean anything if they persist across calls.
    """

    def __init__(self, transports: dict[str, ToolClient], chains: dict[str, Handler],
                 policy_config: dict[str, Any]) -> None:
        self._transports = transports
        self._chains = chains
        self._policy_config = policy_config

    def scoped(self, allowed: list[str], trace: RunTrace) -> ScopedToolBelt:
        return ScopedToolBelt(self._chains, allowed, trace)

    def raw(self, name: str) -> ToolClient:
        """Direct transport access, for tests and demo assertions."""
        return self._transports[name]

    def describe(self) -> list[dict[str, Any]]:
        """Which policies wrap each vendor, and the live circuit state.

        Surfaced on the dashboard so the reliability posture of each integration
        is visible rather than buried in config.
        """
        described = []
        for name, chain in self._chains.items():
            layers, breaker_state = [], None

            node: Any = chain
            while isinstance(node, Policy):
                policy_name = next(
                    (k for k, v in _POLICY_TYPES.items() if isinstance(node, v)),
                    type(node).__name__,
                )
                layers.append({
                    "policy": policy_name,
                    "settings": self._policy_config.get(name, {}).get(policy_name, {}),
                })
                if isinstance(node, CircuitBreakerPolicy):
                    breaker_state = node.state
                node = node.inner

            described.append({
                "tool": name,
                "layers": layers,
                "circuit_state": breaker_state,
            })
        return described


from .policies import POLICIES as _POLICY_TYPES  # noqa: E402  (avoids a cycle at import time)

__all__ = ["ToolClient", "ToolBelt", "ScopedToolBelt", "BoundClient",
           "ToolError", "ToolPermissionError", "Call", "build_chain"]
