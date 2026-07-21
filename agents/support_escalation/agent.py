"""Support Escalation agent.

This agent exists to prove the platform claim rather than to be clever: it was
added by writing this file and one registry entry. It subscribes to a different
event, is granted a different set of tools, and reuses the same tracing, retry,
idempotency and routing machinery.

No line of renewal_risk changed to add it. That is the test of whether an event
bus is real or decorative.
"""

from __future__ import annotations

from typing import Any

from agentplatform.observability import SKIPPED

AGENT_NAME = "support_escalation"
P1_THRESHOLD = 2


def handle(ctx) -> dict[str, Any]:
    trace = ctx.trace
    account_id = ctx.event.account_id

    with trace.step("fetch_tickets") as step:
        summary = ctx.tools.zendesk.call("get_ticket_summary", {"account_id": account_id})
        step.set(open_tickets=summary.get("open_tickets"),
                 p1_tickets_30d=summary.get("p1_tickets_30d"))

    p1 = summary.get("p1_tickets_30d", 0) or 0
    csat = summary.get("csat_30d")

    escalate = p1 >= P1_THRESHOLD or (csat is not None and csat < 3.0)
    reason = (
        f"{p1} P1 tickets in 30 days (threshold {P1_THRESHOLD}) with CSAT {csat}"
        if escalate else
        f"{p1} P1 tickets and CSAT {csat} are within normal range"
    )
    trace.decision("escalation_gate", "escalate" if escalate else "hold", reason,
                   p1_tickets_30d=p1, csat_30d=csat)

    if not escalate:
        trace.record("agent_skipped", SKIPPED, reason=reason)
        return {"acted": False, "reason": reason, "summary": f"skipped: {reason}"}

    text = (
        f"*Support escalation* — account {account_id}\n"
        f"{p1} P1 tickets in the last 30 days, CSAT {csat}, "
        f"{summary.get('open_tickets')} open tickets.\n"
        f"_Trace: {trace.trace_id}_"
    )

    with trace.step("notify_slack") as step:
        sent = ctx.tools.slack.call(
            "post_message",
            {"channel": "#support-escalations", "text": text},
            idempotency_key=f"{ctx.event.event_id}:slack",
        )
        step.set(channel="#support-escalations")

    return {"acted": True, "p1_tickets_30d": p1, "slack_ts": sent["ts"],
            "summary": f"escalated {account_id}: {p1} P1 tickets"}
