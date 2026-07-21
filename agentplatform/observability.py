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
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .events import Event
from .store import Warehouse

OK = "ok"
ERROR = "error"
SKIPPED = "skipped"
DEGRADED = "degraded"


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

        self.warehouse.attach_trace(event.event_id, self.trace_id)
        self.record(
            "run_started",
            OK,
            event_type=event.event_type,
            source=event.source,
            account_id=event.account_id,
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
        """Time a step, capture its detail, and never let an error go unlogged."""
        handle = StepHandle()
        start = time.perf_counter()
        try:
            yield handle
        except Exception as exc:
            elapsed = int((time.perf_counter() - start) * 1000)
            self.record(name, ERROR, latency_ms=elapsed,
                        error=f"{type(exc).__name__}: {exc}", **handle.detail)
            raise
        else:
            elapsed = int((time.perf_counter() - start) * 1000)
            self.record(name, handle.status, latency_ms=elapsed, **handle.detail)

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


class Observability:
    """Entry point. Agents get a RunTrace from here; nothing else creates one."""

    def __init__(self, warehouse: Warehouse) -> None:
        self.warehouse = warehouse

    def start_run(self, event: Event, agent: str) -> RunTrace:
        return RunTrace(self.warehouse, event, agent)

    def timeline(self, trace_id: str) -> list[dict[str, Any]]:
        return self.warehouse.steps_for_trace(trace_id)

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
