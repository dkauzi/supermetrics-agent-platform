"""Tool clients. Agents reach vendors only through here."""

from __future__ import annotations

from ..config import Config
from .base import BoundClient, ScopedToolBelt, ToolBelt, ToolClient, ToolError, ToolPermissionError
from .vendors import GainsightClient, SalesforceClient, SlackClient, ZendeskClient

__all__ = [
    "BoundClient", "ScopedToolBelt", "ToolBelt", "ToolClient", "ToolError",
    "ToolPermissionError", "GainsightClient", "SalesforceClient", "SlackClient",
    "ZendeskClient", "build_toolbelt",
]


def build_toolbelt(config: Config) -> ToolBelt:
    """One instance per vendor for the process. The registry decides who may use what."""
    return ToolBelt({
        "salesforce": SalesforceClient(config),
        "gainsight": GainsightClient(config),
        "zendesk": ZendeskClient(config),
        "slack": SlackClient(config),
    })
