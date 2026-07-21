"""Mocked vendor clients: Salesforce, Gainsight, Zendesk, Slack.

These are mocks in the sense that they do not cross the network - but they hold a
realistic fixture dataset and honour the same interface, retry semantics and
idempotency as the real thing would. Swapping in a real client means implementing
`_execute` against the vendor SDK; no agent changes.

In production each of these is its own module with its own auth and rate-limit
policy. They are together here to keep the review surface small.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, Config
from .base import ToolClient

FIXTURES = REPO_ROOT / "samples" / "accounts.json"


def _load_fixtures() -> dict[str, Any]:
    if not FIXTURES.exists():
        return {}
    with FIXTURES.open() as handle:
        return {a["account_id"]: a for a in json.load(handle)}


class SalesforceClient(ToolClient):
    """CRM system of record for the commercial relationship."""

    name = "salesforce"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._accounts = _load_fixtures()
        self.written: list[dict[str, Any]] = []

    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "get_account":
            account = self._accounts.get(payload["account_id"])
            if account is None:
                raise ValueError(f"account {payload['account_id']} not found in Salesforce")
            return {
                "id": account["account_id"],
                "name": account["name"],
                "arr_usd": account["arr_usd"],
                "renewal_date": account["renewal_date"],
                "owner": account["owner"],
                "owner_slack": account["owner_slack"],
                "cs_lead_slack": account.get("cs_lead_slack"),
                "segment": account["segment"],
                "contract_start": account.get("contract_start"),
            }

        if operation == "create_task":
            record = {
                "id": f"00T{abs(hash(payload.get('subject', ''))) % 10**12:012d}",
                "status": "created",
                "object": "Task",
                "written_at": datetime.now(timezone.utc).isoformat(),
                **payload,
            }
            self.written.append(record)
            return record

        raise ValueError(f"unsupported salesforce operation: {operation}")


class GainsightClient(ToolClient):
    """Customer-success system of record: health, usage, adoption signals."""

    name = "gainsight"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._accounts = _load_fixtures()
        self.written: list[dict[str, Any]] = []

    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "get_health":
            account = self._accounts.get(payload["account_id"])
            if account is None:
                raise ValueError(f"account {payload['account_id']} not found in Gainsight")
            return account["health"]

        if operation == "create_cta":
            record = {
                "id": f"cta_{abs(hash(payload.get('title', ''))) % 10**10:010d}",
                "status": "open",
                "written_at": datetime.now(timezone.utc).isoformat(),
                **payload,
            }
            self.written.append(record)
            return record

        raise ValueError(f"unsupported gainsight operation: {operation}")


class ZendeskClient(ToolClient):
    """Support system of record."""

    name = "zendesk"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._accounts = _load_fixtures()

    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "get_ticket_summary":
            account = self._accounts.get(payload["account_id"])
            if account is None:
                raise ValueError(f"account {payload['account_id']} not found in Zendesk")
            return account["health"].get("support", {})

        raise ValueError(f"unsupported zendesk operation: {operation}")


class HubSpotClient(ToolClient):
    """Marketing engagement. Named in the brief alongside the other systems.

    Added to show what onboarding a fifth vendor actually costs on this platform:
    this class, one entry in TRANSPORTS, and a `tools:` grant in the registry.
    No agent, policy or observability code changes.
    """

    name = "hubspot"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._accounts = _load_fixtures()

    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "get_engagement":
            account = self._accounts.get(payload["account_id"])
            if account is None:
                raise ValueError(f"account {payload['account_id']} not found in HubSpot")
            return account["health"].get("marketing", {})

        raise ValueError(f"unsupported hubspot operation: {operation}")


class SlackClient(ToolClient):
    """Notification channel. Mocked: messages are captured, not sent."""

    name = "slack"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.sent: list[dict[str, Any]] = []

    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "post_message":
            message = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "channel": payload["channel"],
                "text": payload["text"],
                "blocks": payload.get("blocks", []),
                "ok": True,
            }
            self.sent.append(message)
            return message

        raise ValueError(f"unsupported slack operation: {operation}")
