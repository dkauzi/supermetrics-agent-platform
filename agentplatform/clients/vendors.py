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
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, Config
from . import fixtures
from .base import ToolClient


class SalesforceClient(ToolClient):
    """CRM system of record for the commercial relationship."""

    name = "salesforce"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.written: list[dict[str, Any]] = []

    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "get_account":
            account = fixtures.salesforce_account(payload["account_id"])
            if account is None:
                raise ValueError(f"account {payload['account_id']} not found in Salesforce")
            # Returned whole rather than re-picked field by field. The supplied
            # bundle carries no renewal_date - that arrives on the trigger event,
            # which is how it works in practice - and a hardcoded key list turns
            # that into a KeyError instead of a missing optional field.
            return {"id": account["account_id"], **account}

        if operation == "update_opportunity":
            # Object and field names come from `mock_crm_write_examples` in the
            # supplied payload. Writing our own invented shape would have looked
            # fine in a demo and been wrong against their actual schema.
            record = {
                "id": payload.get("opportunity_id"),
                "object": "Opportunity",
                "status": "updated",
                "written_at": datetime.now(timezone.utc).isoformat(),
                "fields": {
                    "Renewal_Risk_Level__c": payload.get("Renewal_Risk_Level__c"),
                    "Risk_Driver__c": payload.get("Risk_Driver__c"),
                    "Last_Risk_Analysis_Date__c": payload.get("Last_Risk_Analysis_Date__c"),
                },
                "trace_id": payload.get("trace_id"),
            }
            self.written.append(record)
            return record

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
        self.written: list[dict[str, Any]] = []

    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "get_health":
            health = fixtures.gainsight_health(payload["account_id"])
            if health is None:
                raise ValueError(f"account {payload['account_id']} not found in Gainsight")
            return health

        if operation == "create_timeline_activity":
            # CSTA (Timeline Activity), per the supplied payload's write example.
            record = {
                "id": f"csta_{abs(hash(payload.get('risk_summary', ''))) % 10**10:010d}",
                "object": "CSTA",
                "company_id": payload.get("gainsight_company_id"),
                "written_at": datetime.now(timezone.utc).isoformat(),
                "fields": {
                    "risk_flag": payload.get("risk_flag"),
                    "risk_summary": payload.get("risk_summary"),
                    "created_by_agent": payload.get("created_by_agent"),
                },
                "trace_id": payload.get("trace_id"),
            }
            self.written.append(record)
            return record

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

    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "get_ticket_summary":
            support = fixtures.zendesk_tickets(payload["account_id"])
            if support is None:
                raise ValueError(f"account {payload['account_id']} not found in Zendesk")
            return support

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

    def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "get_engagement":
            account = fixtures._local_accounts().get(payload["account_id"])
            if account is None:
                # Supplied accounts carry no marketing data. Absent is a valid
                # answer for a supporting signal; the agent proceeds without it.
                return {}
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
                "trace_id": payload.get("trace_id"),
                "ok": True,
                "delivery": "captured",
            }

            # Set SLACK_WEBHOOK_URL and the same message really posts. The agent
            # is unchanged either way: mocked and real differ by a transport
            # detail here, which is the point of putting vendors behind clients.
            webhook = os.getenv("SLACK_WEBHOOK_URL")
            if webhook:
                import httpx
                response = httpx.post(
                    webhook,
                    json={"text": message["text"], "channel": message["channel"]},
                    timeout=10,
                )
                response.raise_for_status()
                message["delivery"] = "posted"

            self.sent.append(message)
            return message

        raise ValueError(f"unsupported slack operation: {operation}")
