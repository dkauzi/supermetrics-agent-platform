"""Renewal Risk Analyser and Router.

Reads top to bottom as the pipeline it is:

    fetch context -> decide whether to act -> analyse -> verify -> score severity
                  -> write to systems of record -> update golden record
                  -> route -> notify

Each stage is a small function or a traced block. The agent contains no retry
logic, no logging boilerplate and no vendor specifics — those belong to the
platform, which is exactly why this file stays readable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agentplatform.clients import ToolError
from agentplatform.feedback import Calibration
from agentplatform.observability import DEGRADED, OK, SKIPPED

from . import routing
from .analysis import analyse, build_facts

AGENT_NAME = "renewal_risk"


def _should_act(facts: dict[str, Any], triggers: dict[str, Any]) -> tuple[bool, str]:
    """Entry gate. Returns (act, reason) — the reason is logged either way."""
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
        facts = build_facts(account, health)
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

    # 4. Severity is a business rule, not a model output.
    severity = routing.compute_severity(facts, account, ctx.agent_config("severity", []) or [])
    trace.decision("severity", f"band_{severity.rule_index}", severity.because,
                   level=severity.level, **severity.inputs)

    # 5. Writes. Idempotency keys are derived from the event id, so a redelivered
    #    webhook cannot create duplicate records.
    writes: dict[str, Any] = {}

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
        record = ctx.warehouse.upsert_golden_record(
            account_id=account_id,
            data={
                "account_name": account.get("name"),
                "arr_usd": account.get("arr_usd"),
                "renewal_date": account.get("renewal_date"),
                "health_score": facts.get("health_score"),
                "renewal_risk_driver": analysis.driver,
                "renewal_risk_severity": severity.level,
                "renewal_risk_confidence": confidence,
                "renewal_risk_method": meta.method,
                "renewal_risk_needs_review": needs_review,
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

    # 8. Notify. A Slack failure must not lose the alert — it goes to the
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
        "channel": decision.channel,
        "routing_rule": decision.rule_id,
        "writes": writes,
        "summary": (
            f"{severity.level} risk on {account.get('name')}: {analysis.driver} "
            f"({confidence:.0%} confidence) -> {decision.channel}"
        ),
    }
