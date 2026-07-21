"""Severity and routing - deterministic, config-driven, and fully explainable.

The LLM explains *why* an account is at risk. It does not decide *who gets woken
up*. Blast radius is a business rule, so it lives in config and is evaluated by
code that records which rule matched and on what values.

That separation is the reason `GET /traces/{id}/why` can say "routed to
#cs-exec-escalations because rule critical_exec_escalation matched severity=critical
and arr_usd=248000" instead of "the model decided".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SeverityDecision:
    level: str
    rule_index: int
    because: str
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutingDecision:
    rule_id: str
    channel: str
    recipients: list[str]
    mention: bool
    because: str
    inputs: dict[str, Any] = field(default_factory=dict)


def _condition_matches(when: dict[str, Any], values: dict[str, Any]) -> tuple[bool, list[str]]:
    """Generic condition evaluator. Supports `<field>_max`, `<field>_min` and list membership."""
    reasons: list[str] = []

    for key, expected in when.items():
        if key.endswith("_max"):
            field_name = key[:-4]
            actual = values.get(field_name)
            if actual is None or actual > expected:
                return False, reasons
            reasons.append(f"{field_name}={actual} <= {expected}")

        elif key.endswith("_min"):
            field_name = key[:-4]
            actual = values.get(field_name)
            if actual is None or actual < expected:
                return False, reasons
            reasons.append(f"{field_name}={actual} >= {expected}")

        elif isinstance(expected, list):
            actual = values.get(key)
            if actual not in expected:
                return False, reasons
            reasons.append(f"{key}={actual} in {expected}")

        else:
            actual = values.get(key)
            if actual != expected:
                return False, reasons
            reasons.append(f"{key}={actual}")

    return True, reasons


def compute_severity(facts: dict[str, Any], account: dict[str, Any],
                     severity_config: list[dict[str, Any]]) -> SeverityDecision:
    """First matching band wins. An empty `when` is the catch-all."""
    values = {
        "health_score": facts.get("health_score"),
        "arr_usd": account.get("arr_usd"),
        "days_to_renewal": facts.get("days_to_renewal"),
    }

    for index, band in enumerate(severity_config):
        matched, reasons = _condition_matches(band.get("when") or {}, values)
        if matched:
            because = (
                f"Severity '{band['level']}' because " + " and ".join(reasons)
                if reasons else
                f"Severity '{band['level']}' as the catch-all band (no higher band matched)"
            )
            return SeverityDecision(band["level"], index, because, values)

    # Config with no catch-all is an operator error, but the alert must survive it.
    return SeverityDecision("low", -1, "No severity band matched; defaulted to 'low'", values)


def resolve_recipients(tokens: list[str], account: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Map role tokens to Slack handles. Returns (resolved, unresolved)."""
    mapping = {
        "account_owner": account.get("owner_slack"),
        "cs_lead": account.get("cs_lead_slack"),
    }
    resolved = [mapping[t] for t in tokens if mapping.get(t)]
    unresolved = [t for t in tokens if not mapping.get(t)]
    return resolved, unresolved


def route(severity: str, account: dict[str, Any],
          routing_config: dict[str, Any]) -> RoutingDecision:
    values = {"severity": severity, "arr_usd": account.get("arr_usd"),
              "segment": account.get("segment")}

    for rule in routing_config.get("rules", []):
        matched, reasons = _condition_matches(rule.get("when") or {}, values)
        if not matched:
            continue

        recipients, unresolved = resolve_recipients(rule.get("notify", []), account)
        channel = rule["channel"]
        because = f"Routed to {channel} because rule '{rule['id']}' matched " + " and ".join(reasons)

        if not recipients:
            # Never drop an alert because an owner lookup failed.
            channel = routing_config.get("fallback_channel", channel)
            because += (
                f"; no Slack handle resolved for {unresolved}, "
                f"so delivery fell back to {channel}"
            )

        return RoutingDecision(rule["id"], channel, recipients,
                               bool(rule.get("mention")), because, values)

    fallback = routing_config.get("fallback_channel", "#cs-platform-alerts")
    return RoutingDecision(
        "fallback_no_rule_matched", fallback, [], False,
        f"No routing rule matched severity='{severity}'; used fallback channel {fallback}",
        values,
    )


def build_approval_request(account: dict[str, Any], facts: dict[str, Any], analysis: Any,
                           confidence: float, reason: str, trace_id: str) -> str:
    """Asked in Slack when the platform will not write on its own judgement.

    Deliberately leads with WHY approval is needed rather than the finding, so a
    reviewer reads the caveat before the conclusion and is not anchored by it.
    """
    evidence = "\n".join(
        f"  - {item.metric} = {item.value} ({item.interpretation})"
        for item in analysis.evidence[:3]
    )
    return "\n".join([
        f":hand: *Approval needed before writing to Salesforce and Gainsight*",
        f"*{account.get('name')}* - ${account.get('arr_usd'):,} ARR, "
        f"renews in {facts.get('days_to_renewal')} days",
        "",
        f"*Why this needs a human:* {reason}",
        "",
        f"*Proposed finding:* {analysis.driver} ({confidence:.0%} confidence)",
        analysis.alert_message,
        "",
        "*Evidence:*",
        evidence,
        "",
        "Approve to write this to the CRM, or correct it and the platform learns "
        "from your verdict.",
        f"_Trace: {trace_id} - full reasoning at /traces/{trace_id}/why_",
    ])


def build_slack_message(account: dict[str, Any], facts: dict[str, Any], analysis: Any,
                        severity: SeverityDecision, decision: RoutingDecision,
                        needs_review: bool, review_reason: str,
                        confidence: float, trace_id: str) -> str:
    """The alert an account owner actually reads. Key details, then the receipts."""
    mention = " ".join(decision.recipients) if decision.mention and decision.recipients else ""
    owner_line = " ".join(decision.recipients) if decision.recipients else "unassigned"

    top_evidence = "\n".join(
        f"  • {item.metric} = {item.value} - {item.interpretation}"
        for item in analysis.evidence[:3]
    )

    header = f"{mention} *{severity.level.upper()} renewal risk* - {account.get('name')}".strip()

    body = [
        header,
        "",
        analysis.alert_message,
        "",
        f"*Driver:* {analysis.driver}  |  *Confidence:* {confidence:.0%}",
        f"*ARR:* ${account.get('arr_usd'):,}  |  *Renews in:* {facts.get('days_to_renewal')} days"
        f"  |  *Health:* {facts.get('health_score')} (was {facts.get('health_score_previous')})",
        f"*Owner:* {owner_line}",
        "",
        "*Evidence:*",
        top_evidence,
        "",
        f"*Recommended action:* {analysis.recommended_action}",
    ]

    if needs_review:
        body += ["", f":warning: *Flagged for human verification* - {review_reason}"]

    body += ["", f"_Trace: {trace_id} · why: /traces/{trace_id}/why_"]
    return "\n".join(body)
