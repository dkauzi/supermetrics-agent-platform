"""Fixture data behind the mocked vendor clients.

Supermetrics supplied `renewal_risk_router_sample_payload.json`, which carries a
trigger event plus an `account_context_bundles` blob per account. It would be
easy to hand that whole blob to the agent and call it done.

That is the wrong shape, and the file itself explains why: three accounts have
similar-looking triggers but different underlying drivers, and "a candidate whose
analysis step just reformats the trigger event will produce near-identical
summaries for all three". The trigger tells you *something is wrong*. Only the
context tells you *what*.

So the bundle is split across the systems it would really come from, and each
mocked client serves its own slice:

    Salesforce  company profile, ARR, owner, renewal, opportunity id
    Gainsight   health score trend, usage telemetry, CS notes, company id
    Zendesk     support ticket history

The agent then has to go and fetch, from three systems, exactly as it would in
production. Nothing gets a free ride on the trigger payload. When these become
real API calls, the agent does not change.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SUPPLIED = REPO_ROOT / "samples" / "renewal_risk_router_sample_payload.json"
LOCAL = REPO_ROOT / "samples" / "accounts.json"


@lru_cache(maxsize=1)
def _supplied_bundles() -> dict[str, Any]:
    if not SUPPLIED.exists():
        return {}
    with SUPPLIED.open() as handle:
        return json.load(handle).get("account_context_bundles", {}) or {}


@lru_cache(maxsize=1)
def _local_accounts() -> dict[str, Any]:
    """Our own fixtures, kept so the original demo scenarios still run."""
    if not LOCAL.exists():
        return {}
    with LOCAL.open() as handle:
        return {a["account_id"]: a for a in json.load(handle)}


def _days_between(from_date: str, to_date: str) -> int | None:
    from datetime import date
    try:
        a = date.fromisoformat(from_date[:10])
        b = date.fromisoformat(to_date[:10])
        return (a - b).days
    except (ValueError, TypeError):
        return None


def salesforce_account(account_id: str) -> dict[str, Any] | None:
    """The commercial record: who they are, what they pay, who owns them."""
    bundle = _supplied_bundles().get(account_id)
    if bundle is None:
        return _local_accounts().get(account_id)

    profile = bundle.get("company_profile", {})
    owner = profile.get("account_owner", {})
    ids = profile.get("crm_record_ids", {})
    footprint = profile.get("supermetrics_footprint", {})

    return {
        "account_id": account_id,
        "name": profile.get("name"),
        "arr_usd": profile.get("arr_usd"),
        "industry": profile.get("industry"),
        "employees": profile.get("employees"),
        "contract_type": profile.get("contract_type"),
        "primary_use_case": profile.get("primary_use_case"),
        "segment": _segment_of(profile),
        "owner": owner.get("name"),
        "owner_email": owner.get("email"),
        # Slack handle from the corporate email local-part: these fixtures carry
        # no handle, and inventing one would put a fake mention in a real alert.
        "owner_slack": "@" + (owner.get("email", "").split("@")[0].replace(".", "")),
        "owner_team": owner.get("team"),
        "opportunity_id": ids.get("salesforce_opportunity_id"),
        "gainsight_company_id": ids.get("gainsight_company_id"),
        "plan": footprint.get("plan"),
        "seats_licensed": footprint.get("seats"),
        "scheduled_transfers": footprint.get("scheduled_transfers"),
        "connected_data_sources": footprint.get("connected_data_sources", []),
        "primary_destination": footprint.get("primary_destination"),
        "admin_users": footprint.get("admin_users", []),
    }


def _segment_of(profile: dict[str, Any]) -> str:
    contract = (profile.get("contract_type") or "").lower()
    if "enterprise" in contract:
        return "enterprise"
    if "mid-market" in contract or "mid_market" in contract:
        return "mid-market"
    return "smb"


def gainsight_health(account_id: str) -> dict[str, Any] | None:
    """Health trend, product telemetry and the CS relationship record.

    `usage_snippets` and `cs_notes` stay as prose deliberately. Real CS context
    is prose, and flattening it to numbers here would quietly discard the signal
    that separates these three accounts - a departed champion and a broken
    connector do not show up as a metric.
    """
    bundle = _supplied_bundles().get(account_id)
    if bundle is None:
        local = _local_accounts().get(account_id)
        return local.get("health") if local else None

    trend = bundle.get("health_score_trend_6mo", [])
    current = trend[-1]["score"] if trend else None
    previous = trend[-2]["score"] if len(trend) > 1 else None

    return {
        "health_score": current,
        "health_score_previous": previous,
        "health_score_trend_6mo": trend,
        "health_score_6mo_ago": trend[0]["score"] if trend else None,
        "usage_snippets": bundle.get("usage_snippets", []),
        "cs_notes": bundle.get("cs_notes", []),
    }


def zendesk_tickets(account_id: str) -> dict[str, Any] | None:
    """Support history, including whether tickets are reopened or still open."""
    bundle = _supplied_bundles().get(account_id)
    if bundle is None:
        local = _local_accounts().get(account_id)
        return (local.get("health", {}) or {}).get("support") if local else None

    tickets = bundle.get("recent_support_tickets", [])
    return {
        "tickets": tickets,
        "ticket_count": len(tickets),
        "open_tickets": sum(1 for t in tickets if t.get("status") == "open"),
        "reopened_tickets": sum(1 for t in tickets if t.get("status") == "reopened"),
        "unresolved_tickets": sum(
            1 for t in tickets if t.get("status") in ("open", "reopened")
        ),
        "ticket_subjects": [t.get("subject", "") for t in tickets],
    }


def known_accounts() -> list[str]:
    return sorted({*_supplied_bundles(), *_local_accounts()})
