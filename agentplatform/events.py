"""The canonical event envelope and the vendor normalisers that produce it.

Agents only ever see a normalised `Event`. When a vendor changes their webhook
shape, the fix is one normaliser here - no agent is touched. That containment is
the whole point of having an event layer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field, field_validator


class Event(BaseModel):
    """Normalised trigger. Every agent input in the platform is one of these."""

    event_id: str = Field(..., description="Idempotency key. Stable per source event.")
    event_type: str = Field(..., description="e.g. health_score.dropped")
    source: str = Field(..., description="Originating system, e.g. gainsight")
    account_id: str
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _dotted(cls, value: str) -> str:
        if "." not in value:
            raise ValueError("event_type must be dotted, e.g. 'health_score.dropped'")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        # Naive timestamps silently break window maths across regions.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class UnknownEventSource(ValueError):
    """No normaliser registered for this source. Payload goes to the dead-letter table."""


def _fallback_event_id(source: str, payload: dict[str, Any]) -> str:
    """Derive a stable id when the vendor does not send one.

    Content hash means a genuine redelivery dedupes, but a real second event
    (different values) does not.
    """
    blob = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
    return f"{source}-{digest}"


def _first_present(payload: dict[str, Any], *paths: str, default: Any = None) -> Any:
    """First value found among a declared list of dotted key paths.

    Not fuzzy matching. Each path is an alias we have actually seen a vendor use,
    written down explicitly, so behaviour stays predictable and reviewable. The
    same webhook is sent with `account.id` by one Gainsight rule and `accountId`
    by another, and a missing account id is the difference between an alert and a
    dead letter. What we do NOT do is guess at unlisted keys: an unrecognised
    payload dead-letters loudly so someone adds the alias on purpose.
    """
    for path in paths:
        node: Any = payload
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node not in (None, ""):
            return node
    return default


def normalise_gainsight(payload: dict[str, Any]) -> Event:
    """Gainsight health-score webhook -> Event."""
    return Event(
        event_id=_first_present(payload, "eventId", "event_id", "id")
                 or _fallback_event_id("gainsight", payload),
        event_type="health_score.dropped",
        source="gainsight",
        account_id=str(_first_present(
            payload, "account.id", "accountId", "account_id", "account.accountId",
            "organization_id", default="")),
        occurred_at=_first_present(payload, "triggeredAt", "triggered_at", "timestamp")
                    or datetime.now(timezone.utc),
        payload=payload,
    )


def normalise_salesforce(payload: dict[str, Any]) -> Event:
    """Salesforce renewal-approaching webhook -> Event."""
    return Event(
        event_id=_first_present(payload, "eventId", "event_id", "Id")
                 or _fallback_event_id("salesforce", payload),
        event_type="renewal.approaching",
        source="salesforce",
        account_id=str(_first_present(
            payload, "AccountId", "accountId", "account_id", "Account.Id", default="")),
        occurred_at=_first_present(payload, "triggeredAt", "triggered_at", "CreatedDate")
                    or datetime.now(timezone.utc),
        payload=payload,
    )


def normalise_zendesk(payload: dict[str, Any]) -> Event:
    """Zendesk ticket-spike webhook -> Event."""
    return Event(
        event_id=payload.get("eventId") or _fallback_event_id("zendesk", payload),
        event_type="support.ticket_spike",
        source="zendesk",
        account_id=str(payload.get("organization_id") or payload.get("accountId") or ""),
        occurred_at=payload.get("triggeredAt") or datetime.now(timezone.utc),
        payload=payload,
    )


def normalise_platform(payload: dict[str, Any]) -> Event:
    """Internal trigger, e.g. the scheduled platform audit.

    The platform is a first-class event source: self-checks arrive on the same bus
    as vendor events and are traced identically. An audit that ran outside the
    observability layer would be an audit nobody could audit.
    """
    return Event(
        event_id=payload.get("eventId") or _fallback_event_id("platform", payload),
        event_type=payload.get("eventType", "platform.audit_requested"),
        source="platform",
        account_id=payload.get("accountId", "PLATFORM"),
        occurred_at=payload.get("triggeredAt") or datetime.now(timezone.utc),
        payload=payload,
    )


NORMALISERS: dict[str, Callable[[dict[str, Any]], Event]] = {
    "gainsight": normalise_gainsight,
    "salesforce": normalise_salesforce,
    "zendesk": normalise_zendesk,
    "platform": normalise_platform,
}


def normalise(source: str, payload: dict[str, Any]) -> Event:
    normaliser = NORMALISERS.get(source.lower())
    if normaliser is None:
        raise UnknownEventSource(
            f"No normaliser for source '{source}'. Known: {sorted(NORMALISERS)}"
        )
    return normaliser(payload)
