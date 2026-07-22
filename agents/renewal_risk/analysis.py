"""Churn-driver analysis: fact assembly, the LLM call, and the fallback.

The order of operations matters and is deliberate:

  facts -> LLM -> schema validation -> grounding verification -> calibration

If any stage fails we do not abandon the alert; we degrade to a deterministic
analysis built directly from the facts (and therefore grounded by construction),
mark the run degraded, and still notify the human. Losing the alert would be a
worse failure than sending a less clever one.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from agentplatform.feedback import Calibration
from agentplatform.limits import check_limits
from agentplatform.llm import LLMUnavailable
from agentplatform.privacy import build_pseudonymiser
from agentplatform.observability import DEGRADED
from agentplatform.verifier import verify_grounding

from . import prompts
from .schemas import AnalysisMeta, ChurnAnalysis, EvidenceItem


def _pct(numerator: float, denominator: float) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


def _days_until(value: str | None) -> int | None:
    if not value:
        return None
    try:
        target = date.fromisoformat(value)
    except ValueError:
        return None
    return (target - datetime.now(timezone.utc).date()).days


def build_facts(account: dict[str, Any], health: dict[str, Any],
                marketing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flatten everything we retrieved into one citable table.

    This dict is simultaneously the model's input, the verifier's source of truth
    and the deterministic fallback's input. One representation, three consumers -
    so a claim can never be checked against different data than it was made on.
    """
    support = health.get("support", {}) or {}
    # Marketing engagement comes from HubSpot and may be absent; falling back to
    # the embedded copy keeps build_facts usable standalone in tests and evals.
    engagement = marketing if marketing is not None else (health.get("marketing", {}) or {})

    facts: dict[str, Any] = {
        "arr_usd": account.get("arr_usd"),
        "segment": account.get("segment"),
        "days_to_renewal": _days_until(account.get("renewal_date")),
        "health_score": health.get("health_score"),
        "health_score_previous": health.get("health_score_previous"),
        "health_score_30d_ago": health.get("health_score_30d_ago"),
        "seats_licensed": health.get("seats_licensed"),
        "seats_active_30d": health.get("seats_active_30d"),
        "seats_active_90d_ago": health.get("seats_active_90d_ago"),
        "weekly_active_users": health.get("weekly_active_users"),
        "data_sources_connected": health.get("data_sources_connected"),
        "data_sources_connected_90d_ago": health.get("data_sources_connected_90d_ago"),
        "last_login_days_ago": health.get("last_login_days_ago"),
        "nps_last": health.get("nps_last"),
        "nps_previous": health.get("nps_previous"),
        "qbr_last_days_ago": health.get("qbr_last_days_ago"),
        "exec_sponsor_changed": health.get("exec_sponsor_changed"),
        "open_tickets": support.get("open_tickets"),
        "p1_tickets_30d": support.get("p1_tickets_30d"),
        "tickets_30d": support.get("tickets_30d"),
        "tickets_prev_30d": support.get("tickets_prev_30d"),
        "avg_first_response_hours": support.get("avg_first_response_hours"),
        "csat_30d": support.get("csat_30d"),
        # Narrative context, kept as prose. The three supplied accounts have
        # near-identical trigger events and near-identical health drops; the only
        # thing that separates a departed champion from a broken connector lives
        # in these strings. Flattening them to numbers would delete the answer.
        # Each line is individually citable, and the verifier checks quotes
        # against these exact strings.
        "usage_snippets": health.get("usage_snippets", []),
        "cs_notes": health.get("cs_notes", []),
        "support_ticket_subjects": support.get("ticket_subjects", []),
        "unresolved_ticket_count": support.get("unresolved_tickets"),
        "reopened_ticket_count": support.get("reopened_tickets"),
        "health_score_trend_6mo": health.get("health_score_trend_6mo", []),
        "connected_data_sources": account.get("connected_data_sources", []),
        "primary_destination": account.get("primary_destination"),
        "plan": account.get("plan"),
        "scheduled_transfers": account.get("scheduled_transfers"),
        "email_open_rate_30d": engagement.get("email_open_rate_30d"),
        "email_open_rate_prev_30d": engagement.get("email_open_rate_prev_30d"),
        "webinar_attendance_90d": engagement.get("webinar_attendance_90d"),
        "content_downloads_90d": engagement.get("content_downloads_90d"),
        "marketing_contacts_active": engagement.get("marketing_contacts_active"),
        "marketing_contacts_active_90d_ago": engagement.get("marketing_contacts_active_90d_ago"),
    }

    # Derived deltas are computed once here rather than left to the model, which
    # would otherwise "cite" arithmetic it performed itself and could get wrong.
    current, previous = facts["health_score"], facts["health_score_previous"]
    if current is not None and previous is not None:
        facts["health_score_delta"] = current - previous

    licensed, active = facts["seats_licensed"], facts["seats_active_30d"]
    if licensed and active is not None:
        facts["seat_utilisation_pct"] = _pct(active, licensed)
    if licensed and facts["seats_active_90d_ago"] is not None:
        facts["seat_utilisation_90d_ago_pct"] = _pct(facts["seats_active_90d_ago"], licensed)

    if facts["tickets_30d"] is not None and facts["tickets_prev_30d"]:
        facts["ticket_volume_change_pct"] = _pct(
            facts["tickets_30d"] - facts["tickets_prev_30d"], facts["tickets_prev_30d"]
        )

    return {k: v for k, v in facts.items() if v is not None}


def deterministic_analysis(facts: dict[str, Any]) -> ChurnAnalysis:
    """Rules-based fallback. Every claim is read straight from `facts`.

    This is not a toy: it is the floor of quality the agent guarantees when the
    LLM is unavailable, over budget, or produced ungrounded output. It is also the
    baseline the golden eval measures the LLM against - if the model cannot beat
    this, the model is not earning its cost.
    """
    def ev(metric: str, interpretation: str) -> EvidenceItem:
        return EvidenceItem(metric=metric, value=facts[metric], interpretation=interpretation)

    seat_util = facts.get("seat_utilisation_pct")
    seat_util_before = facts.get("seat_utilisation_90d_ago_pct")

    # Ordered by how decisively each signal explains churn risk.
    if seat_util is not None and seat_util_before is not None and seat_util < seat_util_before - 20:
        evidence = [ev("seat_utilisation_pct", "Licensed seats largely unused"),
                    ev("seat_utilisation_90d_ago_pct", "Utilisation was far higher 90 days ago")]
        if "weekly_active_users" in facts:
            evidence.append(ev("weekly_active_users", "Weekly active users are low"))
        return ChurnAnalysis(
            driver="adoption_decline",
            driver_explanation=(
                f"Seat utilisation fell from {seat_util_before}% to {seat_util}%, "
                "so the customer is paying for capacity they no longer use."
            ),
            evidence=evidence,
            confidence=0.6,
            recommended_action="Book an adoption review and identify which teams stopped using the product.",
            alert_message=(
                f"Churn risk driver: adoption decline. Seat utilisation dropped from "
                f"{seat_util_before}% to {seat_util}% and health score is now "
                f"{facts.get('health_score')}. Recommend an adoption review before renewal."
            ),
        )

    if (facts.get("p1_tickets_30d", 0) or 0) >= 3 or (facts.get("csat_30d") or 5) < 3.0:
        evidence = [ev("p1_tickets_30d", "Multiple P1 incidents in the last 30 days")]
        for metric, note in (("csat_30d", "Support satisfaction is below acceptable"),
                             ("open_tickets", "Unresolved ticket backlog"),
                             ("avg_first_response_hours", "Slow first response")):
            if metric in facts:
                evidence.append(ev(metric, note))
        return ChurnAnalysis(
            driver="support_burden",
            driver_explanation="Support experience has deteriorated sharply and is the most likely driver.",
            evidence=evidence[:4],
            confidence=0.58,
            recommended_action="Escalate open P1s and have support lead join the renewal conversation.",
            alert_message=(
                f"Churn risk driver: support burden. {facts.get('p1_tickets_30d')} P1 tickets "
                f"in 30 days with CSAT {facts.get('csat_30d')}. Health score is "
                f"{facts.get('health_score')} ahead of renewal."
            ),
        )

    if facts.get("exec_sponsor_changed") is True:
        evidence = [ev("exec_sponsor_changed", "Executive sponsor changed")]
        if "qbr_last_days_ago" in facts:
            evidence.append(ev("qbr_last_days_ago", "No recent executive touchpoint"))
        return ChurnAnalysis(
            driver="champion_loss",
            driver_explanation="The executive sponsor changed and no relationship has been rebuilt.",
            evidence=evidence,
            confidence=0.55,
            recommended_action="Identify and meet the new sponsor before renewal discussions begin.",
            alert_message=(
                f"Churn risk driver: champion loss. Executive sponsor changed and the last QBR "
                f"was {facts.get('qbr_last_days_ago')} days ago. Health score {facts.get('health_score')}."
            ),
        )

    sources_now = facts.get("data_sources_connected")
    sources_before = facts.get("data_sources_connected_90d_ago")
    if sources_now is not None and sources_before and sources_now < sources_before:
        return ChurnAnalysis(
            driver="data_integration_regression",
            driver_explanation="Connected data sources dropped, which usually precedes disengagement.",
            evidence=[ev("data_sources_connected", "Fewer sources connected now"),
                      ev("data_sources_connected_90d_ago", "More sources were connected 90 days ago")],
            confidence=0.5,
            recommended_action="Check why connectors were removed and whether a competing tool was introduced.",
            alert_message=(
                f"Churn risk driver: data integration regression. Connected sources fell from "
                f"{sources_before} to {sources_now}. Health score {facts.get('health_score')}."
            ),
        )

    # No rule fired. Say so honestly rather than inventing a driver.
    fallback_metric = "health_score" if "health_score" in facts else next(iter(facts))
    return ChurnAnalysis(
        driver="unknown",
        driver_explanation="Health score dropped but no single signal in the retrieved facts explains it.",
        evidence=[ev(fallback_metric, "Primary signal available at analysis time")],
        confidence=0.25,
        recommended_action="Manual review: the available telemetry does not isolate a driver.",
        alert_message=(
            f"Churn risk flagged but driver is unclear. Health score is "
            f"{facts.get('health_score')} with no dominant signal. Needs human review."
        ),
    )


def analyse(ctx, account: dict[str, Any], facts: dict[str, Any],
            trigger: dict[str, Any]) -> tuple[ChurnAnalysis, AnalysisMeta]:
    """Produce a verified, calibrated churn analysis. Never raises."""
    from agentplatform.llm import LLMClient  # local import keeps the agent thin

    version = ctx.agent_config("prompt_version", "v2")
    llm = LLMClient(ctx.config)

    analysis: ChurnAnalysis | None = None
    meta = AnalysisMeta(method="deterministic_fallback", prompt_version=version)

    # Runaway and budget protection, checked before we spend anything. A tripped
    # limit degrades the analysis and escalates to a human; it never drops the run.
    limit = check_limits(ctx.warehouse, ctx.config, ctx.event.account_id)
    ctx.trace.decision(
        "spend_limits",
        limit.limit_hit or "within_limits",
        limit.reason,
        **limit.as_detail(),
    )

    # Identities are replaced with tokens before the payload crosses to a third
    # party, and restored in the output a human reads. The model gets the metrics,
    # which is all it needs to identify a driver.
    pseudonymiser = build_pseudonymiser(ctx.config)

    try:
        if not limit.allow_llm:
            raise LLMUnavailable(f"limit:{limit.limit_hit}")

        safe_account = pseudonymiser.scrub_account(account)
        ctx.trace.record("privacy_minimisation", "ok", **pseudonymiser.audit())

        bundle = prompts.build(version, safe_account, facts, trigger)
        ctx.trace.record("prompt_built", "ok", prompt=bundle.fingerprint(),
                         fact_count=len(facts))
        candidate, llm_meta = llm.complete_structured(bundle, ChurnAnalysis, ctx.trace)

        # Put the real names back before anything reaches a person or a CRM.
        candidate.alert_message = pseudonymiser.rehydrate(candidate.alert_message)
        candidate.driver_explanation = pseudonymiser.rehydrate(candidate.driver_explanation)
        candidate.recommended_action = pseudonymiser.rehydrate(candidate.recommended_action)
        meta = AnalysisMeta(
            method="llm", model=llm_meta.model, prompt_version=version,
            attempts=llm_meta.attempts, repaired=llm_meta.repaired,
            cost_usd=llm_meta.cost_usd,
        )
        analysis = candidate
    except LLMUnavailable as exc:
        meta.degraded_reason = str(exc)
        ctx.trace.record("llm_analysis", DEGRADED, degraded_reason=str(exc))

    # Grounding gate. An LLM analysis that cites facts we never retrieved is
    # discarded outright - we do not "mostly trust" it.
    if analysis is not None:
        verification = verify_grounding(
            analysis.evidence, facts, ctx.trace,
            min_claims=ctx.config.get("verification.min_evidence_items", 2),
            tolerance=ctx.config.get("verification.numeric_tolerance", 0.01),
            allow_unverifiable=ctx.config.get("verification.allow_unverifiable_claims", False),
        )
        if not verification.passed:
            meta = AnalysisMeta(
                method="deterministic_fallback", prompt_version=version,
                model=meta.model, attempts=meta.attempts, cost_usd=meta.cost_usd,
                degraded_reason=f"grounding_failed: {verification.violations}",
            )
            analysis = None

    if analysis is None:
        analysis = deterministic_analysis(facts)

    # Calibration: scale the stated confidence by how often this driver has
    # actually been right. Measured outcomes override self-reported confidence.
    calibration = Calibration(ctx.warehouse, ctx.config, ctx.entry.name)
    multiplier = calibration.confidence_multiplier(analysis.driver)
    meta.raw_confidence = analysis.confidence
    meta.calibrated_confidence = round(min(1.0, analysis.confidence * multiplier), 3)

    needs_review, reason = calibration.needs_human_review(analysis.driver)
    ctx.trace.record(
        "calibration_applied", "ok",
        driver=analysis.driver,
        raw_confidence=meta.raw_confidence,
        calibrated_confidence=meta.calibrated_confidence,
        multiplier=round(multiplier, 3),
        needs_human_review=needs_review,
        because=reason,
    )

    # A run that was throttled reached its conclusion without the model, so it
    # carries the escalation forward regardless of what calibration says.
    if limit.force_human_review:
        meta.forced_human_review = True
        meta.degraded_reason = meta.degraded_reason or f"limit:{limit.limit_hit}"

    return analysis, meta
