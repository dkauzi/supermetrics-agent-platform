"""Tool clients. Agents reach vendors only through here."""

from __future__ import annotations

from typing import Any

from ..config import Config
from .base import (
    BoundClient, ScopedToolBelt, ToolBelt, ToolClient, ToolError, ToolPermissionError,
)
from .policies import Call, CircuitOpen, build_chain
from .vendors import (
    GainsightClient, HubSpotClient, SalesforceClient, SlackClient, ZendeskClient,
)

__all__ = [
    "BoundClient", "ScopedToolBelt", "ToolBelt", "ToolClient", "ToolError",
    "ToolPermissionError", "CircuitOpen", "Call", "build_chain",
    "GainsightClient", "HubSpotClient", "SalesforceClient", "SlackClient",
    "ZendeskClient", "build_toolbelt",
]

TRANSPORTS: dict[str, type[ToolClient]] = {
    "salesforce": SalesforceClient,
    "gainsight": GainsightClient,
    "zendesk": ZendeskClient,
    "hubspot": HubSpotClient,
    "slack": SlackClient,
}


def _settings_for(vendor: str, config: Config) -> tuple[list[str], dict[str, Any]]:
    """Vendor policy settings, layered over the platform defaults.

    A vendor overrides only what it needs; everything else inherits. That keeps
    the common case to zero config while still allowing Salesforce to be paced
    differently from Slack.
    """
    defaults = config.get("tools.defaults", {}) or {}
    override = (config.get("tools.vendors", {}) or {}).get(vendor, {}) or {}

    policies = list(override.get("policies", defaults.get("policies", ["tracing", "retry"])))

    # Failure injection is enabled globally (TOOL_FAILURE_INJECTION_RATE) and must
    # sit INNERMOST, wrapping the transport directly. Placed anywhere else the
    # injected fault is raised above the retry policy and bypasses the very path
    # the injection exists to exercise.
    injection = (defaults.get("failure_injection") or {}).get("rate", 0)
    if injection and "failure_injection" not in policies:
        policies.append("failure_injection")

    merged: dict[str, Any] = {}
    for name in policies:
        merged[name] = {**(defaults.get(name) or {}), **(override.get(name) or {})}
    return policies, merged


def build_toolbelt(config: Config) -> ToolBelt:
    """One transport and one policy chain per vendor for the whole process."""
    transports: dict[str, ToolClient] = {
        name: cls(config) for name, cls in TRANSPORTS.items()
    }

    chains, policy_config = {}, {}
    for name, transport in transports.items():
        policies, settings = _settings_for(name, config)
        chains[name] = build_chain(transport, policies, settings)
        policy_config[name] = settings

    return ToolBelt(transports, chains, policy_config)
