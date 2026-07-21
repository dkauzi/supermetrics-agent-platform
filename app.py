"""HTTP surface of the platform.

Two audiences, deliberately kept separate:

  - machines: POST /webhooks/{source}
  - humans:   GET / (dashboard), GET /traces/{id}/why, GET /calibration

The human endpoints are not an afterthought. "Answer why this agent did that in
minutes, without reading code" is a product requirement, so it gets first-class
routes and a UI, not a log file and a grep.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from agentplatform import build_platform
from agentplatform.config import data_dir
from agentplatform.events import UnknownEventSource
from agentplatform.feedback import Calibration, record_outcome
from agentplatform.limits import spend_report
from agents.platform_qa.agent import is_guarded_rejection

app = FastAPI(title="Supermetrics Agent Platform", version="1.0.0")
platform = build_platform()

DASHBOARD = Path(__file__).parent / "dashboard.html"


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Liveness plus the facts an on-call engineer wants first."""
    return {
        "status": "ok",
        "warehouse": platform.config.get("platform.warehouse"),
        "agents_enabled": len(platform.registry.enabled()),
        "agents_review_due": len(platform.registry.review_due()),
        "event_types": platform.registry.event_types(),
        "llm_model_chain": platform.config.get("llm.model_chain"),
    }


@app.post("/webhooks/{source}")
async def webhook(source: str, request: Request) -> JSONResponse:
    """Single ingestion path for every vendor.

    Returns 202 for accepted, 200 for a duplicate (already processed), 400 for a
    payload we could not normalise. Every rejection is dead-lettered first.
    """
    try:
        payload = await request.json()
    except Exception:
        platform.warehouse.dead_letter(source, "invalid_json", {"raw": "<unparseable>"})
        raise HTTPException(status_code=400, detail="body is not valid JSON")

    try:
        result = platform.ingest(source, payload)
    except UnknownEventSource as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"could not process payload: {exc}")

    status = 200 if result["status"] == "duplicate" else 202
    return JSONResponse(result, status_code=status)


@app.get("/registry")
def registry() -> dict[str, Any]:
    """The Agent Registry as data: what exists, who owns it, what is overdue review."""
    return platform.registry.catalogue()


@app.get("/traces")
def traces(limit: int = 50) -> dict[str, Any]:
    return {"traces": platform.warehouse.recent_traces(limit)}


@app.get("/traces/{trace_id}")
def trace_detail(trace_id: str) -> dict[str, Any]:
    steps = platform.observability.timeline(trace_id)
    if not steps:
        raise HTTPException(status_code=404, detail=f"no trace {trace_id}")
    return {"trace_id": trace_id, "steps": steps}


@app.get("/traces/{trace_id}/why")
def trace_why(trace_id: str) -> dict[str, Any]:
    """Why this agent did that, for two different readers.

    `plain` is written for whoever is actually asking: a CS lead, a manager, an
    account owner. No identifiers, no rule names, no JSON. `narrative` and
    `decisions` keep the engineer's version, with the rule that fired and the
    values it matched. Same trace, so the two can never disagree.
    """
    explanation = platform.observability.explain(trace_id)
    if not explanation["found"]:
        raise HTTPException(status_code=404, detail=f"no trace {trace_id}")
    explanation["plain"] = platform.observability.plain_english(trace_id)
    return explanation


@app.get("/accounts/{account_id}/golden")
def golden_record(account_id: str) -> dict[str, Any]:
    record = platform.warehouse.get_golden_record(account_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no golden record for {account_id}")
    return record


@app.get("/accounts/{account_id}/audit")
def account_audit(account_id: str) -> dict[str, Any]:
    """Full decision history for one account, newest first.

    "What has this platform ever concluded about Northwind, and was it right?"
    is the question a CS lead actually asks. Answering it from the trace store
    means no one has to reconstruct history from Slack scrollback.
    """
    verdicts = {row["trace_id"]: row for row in platform.warehouse.outcomes()}
    entries = []

    for run in platform.warehouse.traces_for_account(account_id):
        steps = {s["step"]: s for s in platform.warehouse.steps_for_trace(run["trace_id"])}
        analyse_step = steps.get("analyse", {}).get("detail", {})
        severity_step = steps.get("severity", {}).get("detail", {})
        routing_step = steps.get("routing", {}).get("detail", {})
        outcome = verdicts.get(run["trace_id"])

        entries.append({
            **run,
            "driver": analyse_step.get("driver"),
            "method": analyse_step.get("method"),
            "confidence": analyse_step.get("confidence"),
            "severity": (severity_step.get("decision_inputs") or {}).get("level"),
            "channel": (routing_step.get("decision_inputs") or {}).get("channel"),
            "routing_rule": routing_step.get("rule_id"),
            "human_verdict": outcome["verdict"] if outcome else None,
            "why_url": f"/traces/{run['trace_id']}/why",
        })

    return {
        "account_id": account_id,
        "golden_record": platform.warehouse.get_golden_record(account_id),
        "run_count": len(entries),
        "history": entries,
    }


@app.get("/tools")
def tools() -> dict[str, Any]:
    """Reliability posture of every vendor integration.

    Each vendor call is wrapped in a chain of policies composed from config, not
    inherited from a base class, so different vendors can have genuinely
    different behaviour. This endpoint makes that posture visible instead of
    leaving it buried in YAML, and reports live circuit-breaker state.
    """
    described = platform.tools.describe()
    grants: dict[str, list[str]] = {}
    for entry in platform.registry.all():
        for tool in entry.tools:
            grants.setdefault(tool, []).append(entry.name)

    return {
        "tools": [{**item, "granted_to": grants.get(item["tool"], [])} for item in described],
        "policy_order_note": (
            "Listed outermost first. Order is the contract: dedupe before spending "
            "a network call, fail fast when a vendor is down, pace ourselves, then retry."
        ),
    }


@app.get("/cost")
def cost() -> dict[str, Any]:
    """Spend against budget, and what happens when it runs out.

    Cost is a first-class operational metric here, not a monthly surprise. The
    important field is `throttled`: past the soft ceiling the platform stops
    paying for the model and routes runs to a human instead of either
    overspending or silently dropping alerts.
    """
    report = spend_report(platform.warehouse, platform.config)
    report["limits"] = {
        "max_llm_calls_per_account_per_hour":
            platform.config.get("limits.max_llm_calls_per_account_per_hour"),
        "human_review_reserve_ratio":
            platform.config.get("limits.human_review_reserve_ratio"),
    }

    # Runs that a limit pushed onto the deterministic path.
    throttled = [
        {"trace_id": s["trace_id"], "ts": s["ts"],
         "limit": (s["detail"] or {}).get("rule_id"),
         "because": (s["detail"] or {}).get("because")}
        for s in platform.warehouse.steps_named("spend_limits", limit=200)
        if (s["detail"] or {}).get("rule_id") not in (None, "within_limits")
    ]
    report["throttled_runs"] = len(throttled)
    report["recent_throttled"] = throttled[:8]
    return report


@app.get("/quality")
def quality() -> dict[str, Any]:
    """What the guardrails caught, and the state of the eval gate.

    Two things a platform owner needs at a glance and cannot get from a log:
    how often the model had to be overruled, and whether the current prompt is
    still passing its regression set. Both are evidence the safety net is load
    bearing rather than decorative.
    """
    eval_file = data_dir() / "last_eval.json"
    last_eval = json.loads(eval_file.read_text()) if eval_file.exists() else None

    # Every analysis step records which method actually produced the result.
    analyses = platform.warehouse.steps_named("analyse", limit=500)
    fallbacks = [
        {
            "trace_id": step["trace_id"], "ts": step["ts"],
            "reason": (step["detail"] or {}).get("degraded_reason"),
            "driver": (step["detail"] or {}).get("driver"),
            "why_url": f"/traces/{step['trace_id']}/why",
        }
        for step in analyses
        if (step["detail"] or {}).get("method") == "deterministic_fallback"
    ]

    # Grounding verification that rejected a model's citations.
    verifications = platform.warehouse.steps_named("verify_grounding", limit=500)
    caught = [
        {
            "trace_id": step["trace_id"], "ts": step["ts"],
            "violations": (step["detail"] or {}).get("violations", []),
            "grounding_rate": (step["detail"] or {}).get("grounding_rate"),
            "why_url": f"/traces/{step['trace_id']}/why",
        }
        for step in verifications if not (step["detail"] or {}).get("passed", True)
    ]

    total = len(analyses)
    return {
        "eval": last_eval,
        "analysis_runs": total,
        "llm_runs": total - len(fallbacks),
        "fallback_runs": len(fallbacks),
        "fallback_rate": round(len(fallbacks) / total, 3) if total else 0.0,
        "grounding_checks": len(verifications),
        "grounding_rejections": len(caught),
        "recent_fallbacks": fallbacks[:10],
        "recent_grounding_rejections": caught[:10],
    }


@app.get("/dead-letters")
def dead_letters(limit: int = 50) -> dict[str, Any]:
    """Everything that did not flow through, split by whether it is our problem.

    A payload refused at the boundary is the negative path working; a payload we
    failed to process is an incident. The classification comes from the same
    function the platform_qa agent uses, so there is one definition of "problem"
    rather than one per consumer.
    """
    letters = platform.warehouse.dead_letters(limit)
    classified = [
        {**letter, "guarded": is_guarded_rejection(letter["reason"])}
        for letter in letters
    ]
    return {
        "dead_letters": classified,
        "needs_triage": sum(1 for letter in classified if not letter["guarded"]),
        "guarded_rejections": sum(1 for letter in classified if letter["guarded"]),
    }


@app.get("/calibration")
def calibration(agent: str = "renewal_risk") -> dict[str, Any]:
    """Measured precision per churn driver, from human verdicts."""
    calib = Calibration(platform.warehouse, platform.config, agent)
    return {"summary": calib.summary(), "drivers": calib.table()}


@app.post("/feedback")
async def feedback(request: Request) -> dict[str, Any]:
    """Close the loop: a human marks an alert correct or wrong.

    This is what makes the dashboard a learning system rather than a report. The
    next run reads the resulting precision back at analysis time.
    """
    body = await request.json()
    required = ("trace_id", "agent", "account_id", "driver", "severity", "verdict")
    missing = [f for f in required if not body.get(f)]
    if missing:
        raise HTTPException(status_code=400, detail=f"missing fields: {missing}")

    try:
        row = record_outcome(
            platform.warehouse,
            trace_id=body["trace_id"], agent=body["agent"], account_id=body["account_id"],
            driver=body["driver"], severity=body["severity"], verdict=body["verdict"],
            notes=body.get("notes"), reviewer=body.get("reviewer"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    calib = Calibration(platform.warehouse, platform.config, body["agent"])
    return {"recorded": row, "updated_calibration": calib.summary()}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD.read_text()
