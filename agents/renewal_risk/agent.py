"""Renewal Risk Analyser and Router.

Reads top to bottom as the pipeline it is:

    fetch context -> decide whether to act -> analyse -> verify -> score severity
                  -> write to systems of record -> update golden record
                  -> route -> notify

Each stage is a small function or a traced block. The agent contains no retry
logic, no logging boilerplate and no vendor specifics - those belong to the
platform, which is exactly why this file stays readable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agentplatform.clients import ToolError
from agentplatform.store import ConcurrentUpdate, merge_golden_record
from agentplatform.feedback import Calibration
from agentplatform.observability import DEGRADED, OK, SKIPPED

from . import routing
from .analysis import analyse, build_facts

AGENT_NAME = "renewal_risk"


def _should_act(facts: dict[str, Any], triggers: dict[str, Any]) -> tuple[bool, str]:
    """Entry gate. Returns (act, reason) - the reason is logged either way."""
    days = facts.get("days_to_renewal")
    score = facts.get("health_score")
    delta = facts.get("health_score_delta")

    window = triggers.get("renewal_window_days", 90)
    floor = triggers.get("health_score_floor", 65)
    min_drop = triggers.get("health_score_drop_min", 10)

    if days is None:
        return True, "No renewal date available; acting rather than silently ignoring the event"

    if days > window:
        return False, f"Renewal is {days} days away, outside the {window}-day action window"

    if score is not None and score >= floor and (delta is None or abs(delta) < min_drop):
        return False, (
            f"Health score {score} is at or above the floor of {floor} and the drop "
            f"({delta}) is under the {min_drop}-point threshold"
        )

    return True, (
        f"Renewal in {days} days (within {window}) with health score {score} "
        f"and a change of {delta} points"
    )


def handle(ctx) -> dict[str, Any]:
    trace = ctx.trace
    account_id = ctx.event.account_id
    triggers = ctx.agent_config("triggers", {}) or {}

    # 1. Context from the systems of record.
    with trace.step("fetch_context") as step:
        account = ctx.tools.salesforce.call("get_account", {"account_id": account_id})
        health = ctx.tools.gainsight.call("get_health", {"account_id": account_id})

        # HubSpot is a supporting signal, not a dependency. If marketing data is
        # unavailable the analysis proceeds with fewer facts rather than failing,
        # because a missing nice-to-have must never block a churn alert.
        try:
            marketing = ctx.tools.hubspot.call("get_engagement", {"account_id": account_id})
        except ToolError as exc:
            marketing = {}
            step.set(hubspot_unavailable=str(exc))

        facts = build_facts(account, health, marketing)
        step.set(account_name=account.get("name"), arr_usd=account.get("arr_usd"),
                 fact_count=len(facts))

    # 2. Should this agent act at all? Logged either way, so "why did nothing
    #    happen?" is as answerable as "why did this happen?".
    act, reason = _should_act(facts, triggers)
    trace.decision("entry_gate", "act" if act else "skip", reason,
                   days_to_renewal=facts.get("days_to_renewal"),
                   health_score=facts.get("health_score"),
                   health_score_delta=facts.get("health_score_delta"))

    if not act:
        trace.record("agent_skipped", SKIPPED, reason=reason)
        return {"acted": False, "reason": reason, "summary": f"skipped: {reason}"}

    # 3. LLM analysis, schema-validated, grounding-verified, calibrated.
    with trace.step("analyse") as step:
        analysis, meta = analyse(ctx, account, facts, ctx.event.payload)
        if meta.method != "llm":
            step.mark_degraded(meta.degraded_reason or "llm_unavailable")
        step.set(driver=analysis.driver, method=meta.method, model=meta.model,
                 prompt_version=meta.prompt_version,
                 confidence=meta.calibrated_confidence, cost_usd=round(meta.cost_usd, 6))

    confidence = meta.calibrated_confidence or analysis.confidence
    calibration = Calibration(ctx.warehouse, ctx.config, AGENT_NAME)
    needs_review, review_reason = calibration.needs_human_review(analysis.driver)

    if confidence < ctx.agent_config("min_confidence", 0.55):
        needs_review = True
        review_reason = (
            f"calibrated confidence {confidence:.0%} is below the "
            f"{ctx.agent_config('min_confidence', 0.55):.0%} minimum"
        )

    if meta.forced_human_review:
        needs_review = True
        review_reason = (
            f"a spend or runaway limit forced the deterministic path "
            f"({meta.degraded_reason})"
        )

    # 4. Severity is a business rule, not a model output.
    severity = routing.compute_severity(facts, account, ctx.agent_config("severity", []) or [])
    trace.decision("severity", f"band_{severity.rule_index}", severity.because,
                   level=severity.level, **severity.inputs)

    # 5. Human approval gate. This is what makes needs_human_review a control
    #    rather than a label: when we do not trust the prediction we do not write
    #    it into the systems of record and then apologise later. We ask first.
    #    The alert itself is never withheld.
    approval = ctx.agent_config("human_approval", {}) or {}
    hold_writes = needs_review and approval.get("block_writes_when_flagged", False)

    trace.decision(
        "write_authority",
        "held_for_human" if hold_writes else "auto_approved",
        (f"CRM writes held pending human approval because {review_reason}"
         if hold_writes else
         f"Writing to systems of record automatically: {review_reason}"),
        needs_human_review=needs_review, confidence=confidence,
        driver=analysis.driver, method=meta.method,
    )

    # 5b. Writes. Idempotency keys are derived from the event id, so a redelivered
    #     webhook cannot create duplicate records.
    writes: dict[str, Any] = {}

    if hold_writes:
        with trace.step("request_human_approval") as step:
            ask = ctx.tools.slack.call(
                "post_message",
                {
                    "channel": approval.get("channel", "#cs-agent-approvals"),
                    "text": routing.build_approval_request(
                        account, facts, analysis, confidence, review_reason, trace.trace_id
                    ),
                },
                idempotency_key=f"{ctx.event.event_id}:approval",
            )
            writes["approval_request_ts"] = ask["ts"]
            step.set(channel=approval.get("channel"), reason=review_reason)
            step.mark_degraded(f"writes_held: {review_reason}")

    if hold_writes:
        # Recorded as skipped, with the reason, so the trace shows the writes were
        # deliberately withheld rather than lost.
        trace.record("write_salesforce", SKIPPED, reason="awaiting human approval")
        trace.record("write_gainsight", SKIPPED, reason="awaiting human approval")
    else:
        with trace.step("write_salesforce") as step:
            task = ctx.tools.salesforce.call(
                "create_task",
                {
                    "account_id": account_id,
                    "subject": f"[{severity.level.upper()}] Renewal risk: {analysis.driver}",
                    "description": analysis.alert_message,
                    "priority": "High" if severity.level in ("critical", "high") else "Normal",
                    "owner": account.get("owner"),
                    "trace_id": trace.trace_id,
                },
                idempotency_key=f"{ctx.event.event_id}:sf_task",
            )
            writes["salesforce_task_id"] = task["id"]
            step.set(task_id=task["id"])

        with trace.step("write_gainsight") as step:
            cta = ctx.tools.gainsight.call(
                "create_cta",
                {
                    "account_id": account_id,
                    "title": f"Renewal risk: {analysis.driver}",
                    "reason": analysis.driver_explanation,
                    "priority": severity.level,
                    "evidence": [e.model_dump() for e in analysis.evidence],
                    "trace_id": trace.trace_id,
                },
                idempotency_key=f"{ctx.event.event_id}:gs_cta",
            )
            writes["gainsight_cta_id"] = cta["id"]
            step.set(cta_id=cta["id"])

    # 6. Golden record. This platform owns these fields and stamps provenance on
    #    every write, so no downstream consumer has to guess where they came from.
    with trace.step("update_golden_record") as step:
        # Optimistic write: two agents can react to the same account at once, and
        # on the record we claim write authority over a lost update is a bug.
        record = merge_golden_record(
            ctx.warehouse,
            account_id=account_id,
            fields={
                "account_name": account.get("name"),
                "arr_usd": account.get("arr_usd"),
                "renewal_date": account.get("renewal_date"),
                "health_score": facts.get("health_score"),
                "renewal_risk_driver": analysis.driver,
                "renewal_risk_severity": severity.level,
                "renewal_risk_confidence": confidence,
                "renewal_risk_method": meta.method,
                "renewal_risk_needs_review": needs_review,
                # Downstream consumers must be able to tell an asserted finding
                # from one still waiting on a person.
                "renewal_risk_write_status": "awaiting_approval" if hold_writes else "asserted",
                "renewal_risk_assessed_at": datetime.now(timezone.utc).isoformat(),
            },
            updated_by=f"{AGENT_NAME}@{ctx.entry.version}",
            trace_id=trace.trace_id,
        )
        step.set(revision=record["revision"])

    # 7. Routing.
    decision = routing.route(severity.level, account, ctx.agent_config("routing", {}) or {})
    trace.decision("routing", decision.rule_id, decision.because,
                   channel=decision.channel, recipients=decision.recipients, **decision.inputs)

    # 8. Notify. A Slack failure must not lose the alert - it goes to the
    #    dead-letter table so it can be replayed.
    message = routing.build_slack_message(
        account, facts, analysis, severity, decision,
        needs_review, review_reason, confidence, trace.trace_id,
    )

    with trace.step("notify_slack") as step:
        try:
            sent = ctx.tools.slack.call(
                "post_message",
                {"channel": decision.channel, "text": message},
                idempotency_key=f"{ctx.event.event_id}:slack",
            )
            writes["slack_ts"] = sent["ts"]
            step.set(channel=decision.channel, recipients=decision.recipients)
        except ToolError as exc:
            step.mark_degraded(f"slack_delivery_failed: {exc}")
            ctx.warehouse.dead_letter(
                "slack", f"notification_failed: {exc}",
                {"channel": decision.channel, "text": message, "trace_id": trace.trace_id},
            )

    return {
        "acted": True,
        "driver": analysis.driver,
        "severity": severity.level,
        "confidence": confidence,
        "method": meta.method,
        "needs_human_review": needs_review,
        "writes_held_for_approval": hold_writes,
        "channel": decision.channel,
        "routing_rule": decision.rule_id,
        "writes": writes,
        "summary": (
            f"{severity.level} risk on {account.get('name')}: {analysis.driver} "
            f"({confidence:.0%} confidence) -> {decision.channel}"
        ),
    }
