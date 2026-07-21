"""HTTP surface of the platform.

Two audiences, deliberately kept separate:

  - machines: POST /webhooks/{source}
  - humans:   GET / (dashboard), GET /traces/{id}/why, GET /calibration

The human endpoints are not an afterthought. "Answer why this agent did that in
minutes, without reading code" is a product requirement, so it gets first-class
routes and a UI, not a log file and a grep.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from agentplatform import build_platform
from agentplatform.events import UnknownEventSource
from agentplatform.feedback import Calibration, record_outcome

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
    """Plain-English explanation of one run. The answer to the 2am question."""
    explanation = platform.observability.explain(trace_id)
    if not explanation["found"]:
        raise HTTPException(status_code=404, detail=f"no trace {trace_id}")
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


@app.get("/dead-letters")
def dead_letters(limit: int = 50) -> dict[str, Any]:
    """Everything the platform could not process. Should normally be empty."""
    return {"dead_letters": platform.warehouse.dead_letters(limit)}


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
