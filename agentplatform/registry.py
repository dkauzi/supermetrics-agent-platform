"""The Agent Registry — the master catalogue.

Nothing runs on this platform unless it is registered here with an owner. That is
the rule that prevents the snowflake problem: undocumented agents nobody owns,
which nobody can debug when they break at 2am.

The registry is also what wires the event bus, so subscriptions are declared data,
not hardwired calls between agents.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field, field_validator

from .config import CONFIG_DIR


class RegistryError(RuntimeError):
    """Registry is malformed. Fails at startup rather than at 2am."""


class AgentEntry(BaseModel):
    """One agent's catalogue entry. Every field here exists to answer a real question."""

    name: str
    version: str
    description: str
    owner: str                      # who to wake up
    owner_email: str
    owner_slack: str
    team: str
    handler: str                    # "module.path:function"
    subscribes_to: list[str] = Field(min_length=1)
    tools: list[str] = Field(default_factory=list)
    writes_golden_record: bool = False
    last_reviewed: date
    review_interval_days: int = 90
    enabled: bool = True

    @field_validator("handler")
    @classmethod
    def _handler_shape(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError("handler must be 'module.path:function'")
        return value

    @property
    def days_since_review(self) -> int:
        return (datetime.now(timezone.utc).date() - self.last_reviewed).days

    @property
    def review_due(self) -> bool:
        """Surfaced on the dashboard so reviews are scheduled, not remembered."""
        return self.days_since_review > self.review_interval_days

    def load_handler(self) -> Callable[..., Any]:
        module_path, func_name = self.handler.split(":", 1)
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise RegistryError(
                f"Agent '{self.name}' declares handler '{self.handler}' "
                f"but module '{module_path}' cannot be imported: {exc}"
            ) from exc

        handler = getattr(module, func_name, None)
        if handler is None:
            raise RegistryError(
                f"Agent '{self.name}': module '{module_path}' has no '{func_name}'"
            )
        return handler

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": " ".join(self.description.split()),
            "owner": self.owner,
            "owner_slack": self.owner_slack,
            "team": self.team,
            "subscribes_to": self.subscribes_to,
            "tools": self.tools,
            "writes_golden_record": self.writes_golden_record,
            "last_reviewed": self.last_reviewed.isoformat(),
            "days_since_review": self.days_since_review,
            "review_due": self.review_due,
            "enabled": self.enabled,
        }


class AgentRegistry:
    def __init__(self, entries: list[AgentEntry]) -> None:
        names = [e.name for e in entries]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise RegistryError(f"Duplicate agent names in registry: {sorted(duplicates)}")
        self._entries = {e.name: e for e in entries}

    @classmethod
    def load(cls, path: Path | None = None) -> "AgentRegistry":
        registry_path = path or CONFIG_DIR / "registry.yaml"
        if not registry_path.exists():
            raise RegistryError(f"Registry file not found: {registry_path}")

        with registry_path.open() as handle:
            data = yaml.safe_load(handle) or {}

        raw_agents = data.get("agents") or []
        if not raw_agents:
            raise RegistryError("Registry contains no agents")

        entries = []
        for raw in raw_agents:
            try:
                entries.append(AgentEntry(**raw))
            except Exception as exc:
                raise RegistryError(
                    f"Invalid registry entry {raw.get('name', '<unnamed>')}: {exc}"
                ) from exc

        return cls(entries)

    def all(self) -> list[AgentEntry]:
        return list(self._entries.values())

    def enabled(self) -> list[AgentEntry]:
        return [e for e in self._entries.values() if e.enabled]

    def get(self, name: str) -> AgentEntry | None:
        return self._entries.get(name)

    def subscribers_of(self, event_type: str) -> list[AgentEntry]:
        return [e for e in self.enabled() if event_type in e.subscribes_to]

    def event_types(self) -> list[str]:
        return sorted({t for e in self.enabled() for t in e.subscribes_to})

    def review_due(self) -> list[AgentEntry]:
        return [e for e in self._entries.values() if e.review_due]

    def catalogue(self) -> dict[str, Any]:
        return {
            "agent_count": len(self._entries),
            "enabled_count": len(self.enabled()),
            "review_due_count": len(self.review_due()),
            "event_types": self.event_types(),
            "agents": [e.summary() for e in self._entries.values()],
        }
