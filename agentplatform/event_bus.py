"""Event routing. Triggers do not know which agents exist.

A source system posts an event. The bus asks the registry who subscribes to that
event type and dispatches to each. Adding an agent that reacts to health-score
drops is a registry entry, not a code change anywhere in this file or in any
existing agent. That decoupling is the whole reason this layer exists.

Isolation rule: one agent raising must never stop another agent from running.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .clients import ToolBelt
from .config import Config
from .events import Event
from .observability import ERROR, OK, Observability, RunTrace
from .registry import AgentEntry, AgentRegistry
from .store import Warehouse


@dataclass
class AgentContext:
    """Everything an agent is allowed to touch, handed to it explicitly.

    Agents never import clients or config directly — they receive this. That keeps
    the blast radius of a platform change visible and testable.
    """

    event: Event
    trace: RunTrace
    tools: ToolBelt
    warehouse: Warehouse
    config: Config
    entry: AgentEntry

    def agent_config(self, path: str, default: Any = None) -> Any:
        """Config scoped to this agent, e.g. ctx.agent_config('routing.rules')."""
        return self.config.get(f"agents.{self.entry.name}.{path}", default)


class EventBus:
    def __init__(
        self,
        registry: AgentRegistry,
        observability: Observability,
        warehouse: Warehouse,
        config: Config,
        tools: ToolBelt,
    ) -> None:
        self.registry = registry
        self.observability = observability
        self.warehouse = warehouse
        self.config = config
        self.tools = tools

    def publish(self, event: Event) -> list[dict[str, Any]]:
        """Dispatch an event to every subscribed agent. Returns one result per agent."""
        subscribers = self.registry.subscribers_of(event.event_type)

        if not subscribers:
            # Not an error — but it must be visible, or events vanish quietly.
            self.warehouse.dead_letter(
                event.source,
                f"no_subscriber_for_event_type:{event.event_type}",
                event.model_dump(mode="json"),
            )
            return []

        results = []
        for entry in subscribers:
            results.append(self._dispatch(entry, event))
        return results

    def _dispatch(self, entry: AgentEntry, event: Event) -> dict[str, Any]:
        trace = self.observability.start_run(event, entry.name)

        try:
            handler = entry.load_handler()
        except Exception as exc:
            # A broken registry entry is an operator problem: log it against the
            # owner so the dashboard shows who needs to fix it.
            trace.record("load_handler", ERROR, error=str(exc), owner=entry.owner)
            trace.finish(ERROR, summary="handler could not be loaded")
            return {"agent": entry.name, "trace_id": trace.trace_id,
                    "status": ERROR, "error": str(exc)}

        context = AgentContext(
            event=event,
            trace=trace,
            # Scoped toolbelt: an agent can only call the tools its registry entry
            # declares. Least privilege, enforced by the platform not by convention.
            tools=self.tools.scoped(entry.tools, trace),
            warehouse=self.warehouse,
            config=self.config,
            entry=entry,
        )

        try:
            result = handler(context) or {}
        except Exception as exc:
            trace.finish(ERROR, summary=f"{type(exc).__name__}: {exc}")
            self.warehouse.dead_letter(
                event.source,
                f"agent_raised:{entry.name}:{type(exc).__name__}: {exc}",
                {"event": event.model_dump(mode="json"), "trace_id": trace.trace_id},
            )
            return {"agent": entry.name, "trace_id": trace.trace_id,
                    "status": ERROR, "error": str(exc)}

        trace.finish(OK, summary=result.get("summary"))
        return {"agent": entry.name, "trace_id": trace.trace_id,
                "status": OK, "result": result}
