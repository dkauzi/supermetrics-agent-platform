"""Platform bootstrap.

One object owns the wiring: config -> warehouse -> observability -> registry ->
tools -> event bus. Every entry point (HTTP server, CLI, runner, tests) builds the
same `Platform`, so there is no second code path that behaves subtly differently.
"""

from __future__ import annotations

from typing import Any

from .clients import ToolBelt, build_toolbelt
from .config import Config, load_config
from .event_bus import AgentContext, EventBus
from .events import Event, UnknownEventSource, normalise
from .llm import LLMClient
from .observability import Observability
from .registry import AgentRegistry
from .store import Warehouse, build_warehouse

__all__ = [
    "Platform", "AgentContext", "Event", "UnknownEventSource", "normalise", "build_platform",
]


class Platform:
    def __init__(
        self,
        config: Config,
        warehouse: Warehouse,
        registry: AgentRegistry,
        observability: Observability,
        tools: ToolBelt,
        bus: EventBus,
        llm: LLMClient,
    ) -> None:
        self.config = config
        self.warehouse = warehouse
        self.registry = registry
        self.observability = observability
        self.tools = tools
        self.bus = bus
        self.llm = llm

    def ingest(self, source: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The single ingestion path: normalise -> dedupe -> publish.

        Every trigger enters here, whether from HTTP, the CLI or a replay.
        """
        try:
            event = normalise(source, payload)
        except UnknownEventSource as exc:
            self.warehouse.dead_letter(source, f"unknown_source: {exc}", payload)
            raise
        except Exception as exc:
            # Malformed payload: dead-letter it with the reason, never guess at
            # what the sender meant.
            self.warehouse.dead_letter(source, f"normalisation_failed: {exc}", payload)
            raise

        if not event.account_id:
            self.warehouse.dead_letter(source, "missing_account_id", payload)
            raise ValueError("payload has no resolvable account_id")

        is_new = self.warehouse.record_event(event)
        if not is_new:
            # Idempotency: a redelivered webhook returns the original run rather
            # than creating a second Salesforce task and a second Slack ping.
            existing = self.warehouse.get_event(event.event_id) or {}
            return {
                "status": "duplicate",
                "event_id": event.event_id,
                "trace_ids": existing.get("trace_ids", []),
                "results": [],
            }

        results = self.bus.publish(event)
        return {
            "status": "accepted",
            "event_id": event.event_id,
            "event_type": event.event_type,
            "account_id": event.account_id,
            "trace_ids": [r["trace_id"] for r in results],
            "results": results,
        }


def build_platform(config: Config | None = None) -> Platform:
    config = config or load_config()
    warehouse = build_warehouse(config)
    registry = AgentRegistry.load()
    observability = Observability(warehouse)
    tools = build_toolbelt(config)
    bus = EventBus(registry, observability, warehouse, config, tools)
    llm = LLMClient(config)
    return Platform(config, warehouse, registry, observability, tools, bus, llm)
