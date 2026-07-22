"""Observability: every agent run is logged, queryable, and explainable.

The design rule here is that the trace must answer "why did this agent do that?"
*without* anyone reading source code. So we record not just what happened, but the
inputs a decision was made on and the named rule that fired.

Three things every step carries: a trace_id, a status, and a latency. Two things
decisions additionally carry: the rule that matched and the values it matched on.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import telemetry
from .events import Event
from .store import Warehouse

OK = "ok"
ERROR = "error"
SKIPPED = "skipped"
DEGRADED = "degraded"

# Translations used by `plain_english`. Deliberately written for a customer
# success manager, not an engineer: no identifiers, no jargon, no metric names.
TRIGGER_IN_PLAIN_ENGLISH = {
    "health_score.dropped": "this customer's health score has fallen",
    "renewal.approaching": "this customer's renewal date is coming up",
    "renewal.risk_signal": "this customer's renewal is coming up and their health score "
                           "has fallen",
    "support.ticket_spike": "this customer has raised a lot of support tickets",
    "platform.audit_requested": "it is time for the scheduled platform check",
}

DRIVER_IN_PLAIN_ENGLISH = {
    "adoption_decline": "people have largely stopped using what they are paying for",
    "support_burden": "they have had a bad run with support",
    "champion_loss": "the person who championed us internally has moved on",
    "value_realisation_gap": "they are using it but not getting the results they wanted",
    # Covers both halves of this driver: connections switched off, and
    # connections still on but no longer returning trustworthy data. Saying only
    # "disconnected" misdescribed an account whose connector was failing, which
    # would send the account owner into the wrong conversation.
    "data_integration_regression": "the data connections they depend on have stopped "
                                   "working reliably",
    "engagement_gap": "we have not had meaningful contact with them in a long time",
    "pricing_pressure": "there is budget or pricing pressure on their side",
    "unknown": "no single clear reason stands out in the data we hold",
}


def _severity_reason(severity: dict[str, Any]) -> str:
    """Turn the numbers a severity band matched on into a readable clause."""
    parts = []
    if severity.get("health_score") is not None:
        parts.append(f"their health score is {severity['health_score']}")
    if severity.get("arr_usd"):
        parts.append(f"they are worth ${severity['arr_usd']:,} a year")
    if severity.get("days_to_renewal") is not None:
        parts.append(f"they renew in {severity['days_to_renewal']} days")
    return " and ".join(parts) if parts else "of the account's overall position"


def _configure_logger() -> logging.Logger:
    """Structured JSON so a log shipper can parse it without regex.

    Deliberately stderr, not stdout: container runtimes capture both, but stdout
    belongs to command output. Logging to stdout corrupts `cli.py ... | jq`.
    """
    logger = logging.getLogger("agentplatform")
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


LOGGER = _configure_logger()


class StepHandle:
    """Passed into a `with trace.step(...)` block so the body can attach detail."""

    def __init__(self) -> None:
        self.detail: dict[str, Any] = {}
        self.status: str = OK

    def set(self, **fields: Any) -> None:
        self.detail.update(fields)

    def mark_degraded(self, reason: str) -> None:
        """Step produced a usable but lower-confidence result. Never silent."""
        self.status = DEGRADED
        self.detail["degraded_reason"] = reason


class RunTrace:
    """One agent run. Owns the trace_id and writes every step to the warehouse."""

    def __init__(self, warehouse: Warehouse, event: Event, agent: str) -> None:
        self.warehouse = warehouse
        self.event = event
        self.agent = agent
        self.trace_id = f"tr_{uuid.uuid4().hex[:16]}"
        self.started_at = time.perf_counter()
        self._seq = 0
        self.cost_usd = 0.0

        # One root span held open for the whole run, so each step nests beneath it
        # as a child. Without this every step starts its own root trace and the
        # tracing backend shows a flat pile of unrelated one-span traces, which is
        # exactly what distributed tracing exists to avoid.
        self._spans = ExitStack()
        self.otel_ids = self._spans.enter_context(
            telemetry.span(
                f"agent.{agent}.run",
                **{"agent.name": agent, "agent.trace_id": self.trace_id,
                   "account.id": event.account_id, "event.type": event.event_type,
                   "event.source": event.source},
            )
        )

        self.warehouse.attach_trace(event.event_id, self.trace_id)
        self.record(
            "run_started",
            OK,
            event_type=event.event_type,
            source=event.source,
            account_id=event.account_id,
            **self.otel_ids,
        )

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def record(self, step: str, status: str, latency_ms: int | None = None,
               error: str | None = None, **detail: Any) -> None:
        row = {
            "trace_id": self.trace_id,
            "event_id": self.event.event_id,
            "agent": self.agent,
            "seq": self._next_seq(),
            "step": step,
            "status": status,
            "latency_ms": latency_ms,
            "detail": detail,
            "error": error,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.warehouse.record_step(row)

        LOGGER.info(json.dumps({
            "trace_id": self.trace_id,
            "agent": self.agent,
            "step": step,
            "status": status,
            "latency_ms": latency_ms,
            "error": error,
            **{f"d.{k}": v for k, v in detail.items()},
        }, default=str))

    @contextmanager
    def step(self, name: str) -> Iterator[StepHandle]:
        """Time a step, capture its detail, and never let an error go unlogged.

        Emits an OpenTelemetry span as well as the decision row. The two carry
        each other's ids, so a latency spike in a tracing backend leads to the
        business reason for that run, and back. See telemetry.py for why both
        exist rather than one replacing the other.
        """
        handle = StepHandle()
        start = time.perf_counter()

        with telemetry.span(f"agent.{self.agent}.{name}",
                            **{"agent.name": self.agent,
                               "agent.trace_id": self.trace_id,
                               "account.id": self.event.account_id,
                               "event.type": self.event.event_type}) as span_ids:
            try:
                yield handle
            except Exception as exc:
                elapsed = int((time.perf_counter() - start) * 1000)
                self.record(name, ERROR, latency_ms=elapsed,
                            error=f"{type(exc).__name__}: {exc}",
                            **span_ids, **handle.detail)
                raise
            else:
                elapsed = int((time.perf_counter() - start) * 1000)
                self.record(name, handle.status, latency_ms=elapsed,
                            **span_ids, **handle.detail)

    def decision(self, name: str, rule_id: str, because: str, **inputs: Any) -> None:
        """Record a branch the agent took, the rule that fired, and the values behind it.

        This is the single most important call in the codebase: it is what turns
        "the agent posted to #cs-exec-escalations" into "…because rule
        critical_exec_escalation matched severity=critical and arr_usd=250000".
        """
        self.record(name, OK, rule_id=rule_id, because=because, decision_inputs=inputs)

    def record_cost(self, model: str, tokens_in: int, tokens_out: int, usd: float) -> None:
        """Cost and latency are first-class metrics, not an afterthought."""
        self.cost_usd += usd
        self.record("llm_cost", OK, model=model, tokens_in=tokens_in,
                    tokens_out=tokens_out, cost_usd=round(usd, 6),
                    run_total_usd=round(self.cost_usd, 6))

    def finish(self, status: str = OK, summary: str | None = None) -> None:
        elapsed = int((time.perf_counter() - self.started_at) * 1000)
        self.record("run_finished", status, latency_ms=elapsed,
                    summary=summary, total_cost_usd=round(self.cost_usd, 6))
        # Closes the root span. Guarded because a run that failed before finish
        # must not raise a second, more confusing error on the way out.
        try:
            self._spans.close()
        except Exception:  # noqa: BLE001
            pass


class Observability:
    """Entry point. Agents get a RunTrace from here; nothing else creates one."""

    def __init__(self, warehouse: Warehouse) -> None:
        self.warehouse = warehouse

    def start_run(self, event: Event, agent: str) -> RunTrace:
        return RunTrace(self.warehouse, event, agent)

    def timeline(self, trace_id: str) -> list[dict[str, Any]]:
        return self.warehouse.steps_for_trace(trace_id)

    def plain_english(self, trace_id: str) -> dict[str, Any]:
        """The run described for someone who will never open the code.

        The brief asks that a person can answer "why did this agent do that?"
        within minutes without reading code. A list of step names and rule ids
        does not clear that bar, so this translates the same trace into sentences
        with no jargon, no identifiers and no JSON. Same data, different reader.
        """
        steps = self.warehouse.steps_for_trace(trace_id)
        if not steps:
            return {"found": False}

        by_step: dict[str, dict[str, Any]] = {}
        for step in steps:
            by_step.setdefault(step["step"], step)

        detail = lambda name: (by_step.get(name, {}).get("detail") or {})  # noqa: E731

        started = detail("run_started")
        context = detail("fetch_context")
        analysis = detail("analyse")
        severity = detail("severity").get("decision_inputs") or {}
        routing = detail("routing").get("decision_inputs") or {}
        authority = detail("write_authority")
        gate = detail("entry_gate")

        account = context.get("account_name") or started.get("account_id") or "an account"
        lines: list[str] = []

        lines.append(
            f"A message arrived from {started.get('source', 'a connected system')} saying "
            f"{TRIGGER_IN_PLAIN_ENGLISH.get(started.get('event_type'), 'something changed')} "
            f"for {account}."
        )

        if context:
            lines.append(
                f"The agent looked up this customer across our systems and gathered "
                f"{context.get('fact_count', 'the relevant')} pieces of information about them."
            )

        if gate.get("rule_id") == "skip":
            lines.append(f"It decided no action was needed: {gate.get('because')}")
            return {"found": True, "headline": f"No action taken on {account}",
                    "lines": lines, "account": account}

        if analysis:
            driver = DRIVER_IN_PLAIN_ENGLISH.get(
                analysis.get("driver"), analysis.get("driver", "an unclear reason"))
            confidence = analysis.get("confidence")
            confidence_text = f" It was {confidence:.0%} sure." if confidence else ""
            lines.append(
                f"Looking at that information, it judged the most likely reason this "
                f"customer might leave is: {driver}.{confidence_text}"
            )

            if analysis.get("method") != "llm":
                lines.append(
                    "The AI model was not used for this one, so the answer came from the "
                    "platform's own built-in rules instead. The conclusion is more "
                    "cautious, and a person was asked to check it."
                )

        if detail("verify_grounding").get("passed") is False:
            lines.append(
                "The AI's explanation quoted figures that did not match our actual data, "
                "so its answer was thrown away and the reliable rule-based answer used instead."
            )

        if severity.get("level"):
            lines.append(
                f"It rated this a {severity['level']} priority, because "
                f"{_severity_reason(severity)}."
            )

        if authority.get("rule_id") == "held_for_human":
            lines.append(
                "It did NOT record this in Salesforce or Gainsight. The platform was not "
                "confident enough to write it down as fact, so it asked a person to "
                "approve it first."
            )
        elif authority:
            lines.append("It recorded the finding in Salesforce and Gainsight.")

        if routing.get("channel"):
            who = ", ".join(routing.get("recipients") or []) or "the team"
            lines.append(
                f"Finally it posted an alert to {routing['channel']} for {who}, "
                f"with the numbers that led to this conclusion."
            )

        failures = [s for s in steps if s["status"] == ERROR]
        if failures:
            lines.append(
                f"{len(failures)} step(s) failed along the way and were retried or "
                f"recorded for follow-up. Nothing was silently dropped."
            )

        driver_label = DRIVER_IN_PLAIN_ENGLISH.get(analysis.get("driver"), "a possible risk")
        headline = (
            f"{account}: {severity.get('level', 'possible')} churn risk, "
            f"because {driver_label}"
        )
        return {"found": True, "headline": headline, "lines": lines, "account": account}

    def explain(self, trace_id: str) -> dict[str, Any]:
        """Plain-English narrative of a run, for people who will not read code.

        This backs GET /traces/{id}/why. A CS lead should be able to open it and
        understand the agent's reasoning in under a minute.
        """
        steps = self.warehouse.steps_for_trace(trace_id)
        if not steps:
            return {"trace_id": trace_id, "found": False, "narrative": []}

        narrative: list[str] = []
        decisions: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        total_cost = 0.0

        for step in steps:
            detail = step.get("detail") or {}
            name, status = step["step"], step["status"]

            if name == "run_started":
                narrative.append(
                    f"Woken by a '{detail.get('event_type')}' event from "
                    f"{detail.get('source')} for account {detail.get('account_id')}."
                )
            elif name == "llm_cost":
                total_cost += float(detail.get("cost_usd") or 0)
            elif "rule_id" in detail:
                narrative.append(f"{detail.get('because')} (rule: {detail['rule_id']})")
                decisions.append({
                    "step": name,
                    "rule_id": detail["rule_id"],
                    "because": detail.get("because"),
                    "inputs": detail.get("decision_inputs", {}),
                })
            elif name == "run_finished":
                narrative.append(
                    f"Run finished with status '{status}' in {step.get('latency_ms')}ms."
                )
            elif status == DEGRADED:
                narrative.append(
                    f"Step '{name}' completed in degraded mode: "
                    f"{detail.get('degraded_reason')}"
                )
            elif status == ERROR:
                narrative.append(f"Step '{name}' FAILED: {step.get('error')}")
            elif status == SKIPPED:
                narrative.append(f"Step '{name}' skipped: {detail.get('reason', 'n/a')}")

            if status == ERROR:
                failures.append({"step": name, "error": step.get("error")})

        return {
            "trace_id": trace_id,
            "found": True,
            "agent": steps[0]["agent"],
            "event_id": steps[0]["event_id"],
            "narrative": narrative,
            "decisions": decisions,
            "failures": failures,
            "total_cost_usd": round(total_cost, 6),
            "step_count": len(steps),
        }
