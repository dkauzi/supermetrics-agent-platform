"""Platform QA - the agent that audits the platform.

Deliberately contains no LLM call. Every check here has a correct answer, and a
model would only make a deterministic result probabilistic while costing money.
I use a model where the task is genuinely fuzzy; this one is not.

What it enforces is the platform's own contract: every agent owned, every agent
reviewed on schedule, nothing stuck in the DLQ, the eval gate green, the model not
quietly failing over to fallback, and no churn driver drifting below its precision
floor. In production this runs on a schedule (Cloud Scheduler -> Pub/Sub) and
posts a digest; the same checks gate CI.

It runs on the same bus, with the same tracing, as every other agent - so the
audit is itself auditable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agentplatform.config import data_dir
from agentplatform.feedback import Calibration
from agentplatform.observability import DEGRADED, OK

AGENT_NAME = "platform_qa"

CRITICAL, WARNING, INFO = "critical", "warning", "info"
MAX_FALLBACK_RATE = 0.25


@dataclass
class Finding:
    severity: str
    check: str
    message: str
    fix: str

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "check": self.check,
                "message": self.message, "fix": self.fix}


def check_ownership(registry) -> list[Finding]:
    """Nothing runs unowned. An unowned agent is an outage with no one to page."""
    findings = []
    for entry in registry.all():
        if not entry.owner or not entry.owner_slack:
            findings.append(Finding(
                CRITICAL, "ownership",
                f"Agent '{entry.name}' has no resolvable owner",
                "Set owner and owner_slack in config/registry.yaml",
            ))
    return findings


def check_review_cadence(registry) -> list[Finding]:
    return [
        Finding(
            WARNING, "review_cadence",
            f"Agent '{entry.name}' last reviewed {entry.days_since_review} days ago "
            f"(interval {entry.review_interval_days})",
            f"Review with {entry.owner}, then update last_reviewed in the registry",
        )
        for entry in registry.review_due()
    ]


def check_dead_letters(warehouse) -> list[Finding]:
    letters = warehouse.dead_letters(200)
    if not letters:
        return []
    reasons: dict[str, int] = {}
    for letter in letters:
        reasons[letter["reason"].split(":")[0]] = reasons.get(letter["reason"].split(":")[0], 0) + 1
    return [Finding(
        CRITICAL, "dead_letters",
        f"{len(letters)} events failed to process: {reasons}",
        "python cli.py dlq - fix the normaliser or vendor payload, then replay",
    )]


def check_eval_gate() -> list[Finding]:
    eval_file = data_dir() / "last_eval.json"
    if not eval_file.exists():
        return [Finding(WARNING, "eval_gate", "No golden eval has been run",
                        "python cli.py eval")]

    report = json.loads(eval_file.read_text())
    if not report.get("gate_passed"):
        return [Finding(
            CRITICAL, "eval_gate",
            f"Golden eval FAILED for prompt {report['prompt_version']}: {report['failures']}",
            "Roll back the prompt version in config - see docs/RUNBOOK.md",
        )]
    return []


def check_fallback_rate(warehouse) -> list[Finding]:
    """A rising fallback rate means the model or a vendor is degrading quietly."""
    analyses = warehouse.steps_named("analyse", limit=500)
    if not analyses:
        return []

    fallbacks = [s for s in analyses
                 if (s["detail"] or {}).get("method") == "deterministic_fallback"]
    rate = len(fallbacks) / len(analyses)
    if rate <= MAX_FALLBACK_RATE:
        return []

    reasons = {(s["detail"] or {}).get("degraded_reason", "unknown") for s in fallbacks}
    return [Finding(
        WARNING if "llm_offline_mode" in reasons else CRITICAL, "fallback_rate",
        f"{rate:.0%} of analyses fell back to deterministic logic "
        f"({len(fallbacks)}/{len(analyses)}). Reasons: {sorted(reasons)}",
        "Check OPENROUTER_API_KEY and the model chain in config/platform.yaml",
    )]


def check_driver_precision(warehouse, config) -> list[Finding]:
    calib = Calibration(warehouse, config, "renewal_risk")
    return [
        Finding(
            WARNING, "driver_precision",
            f"Driver '{stats['driver']}' precision {stats['precision']:.0%} "
            f"over {stats['samples']} reviewed alerts - below the review floor",
            "Add cases to tests/golden/cases.json and revise the prompt; "
            "alerts are auto-flagged for human review meanwhile",
        )
        for stats in calib.table() if stats["needs_review"]
    ]


def check_run_errors(warehouse) -> list[Finding]:
    failing = [t for t in warehouse.recent_traces(100) if t["errors"] > 0]
    if not failing:
        return []
    return [Finding(
        WARNING, "run_errors",
        f"{len(failing)} of the last 100 runs recorded step errors "
        f"(e.g. {failing[0]['trace_id']})",
        f"python cli.py why {failing[0]['trace_id']}",
    )]


def handle(ctx) -> dict[str, Any]:
    trace = ctx.trace
    findings: list[Finding] = []

    checks = (
        ("ownership", lambda: check_ownership(ctx.registry)),
        ("review_cadence", lambda: check_review_cadence(ctx.registry)),
        ("dead_letters", lambda: check_dead_letters(ctx.warehouse)),
        ("eval_gate", check_eval_gate),
        ("fallback_rate", lambda: check_fallback_rate(ctx.warehouse)),
        ("driver_precision", lambda: check_driver_precision(ctx.warehouse, ctx.config)),
        ("run_errors", lambda: check_run_errors(ctx.warehouse)),
    )

    for name, check in checks:
        with trace.step(f"check_{name}") as step:
            results = check()
            findings.extend(results)
            step.set(findings=len(results),
                     severities=[f.severity for f in results])
            if any(f.severity == CRITICAL for f in results):
                step.mark_degraded(f"{name} failed")

    criticals = [f for f in findings if f.severity == CRITICAL]
    warnings = [f for f in findings if f.severity == WARNING]

    verdict = "FAIL" if criticals else "WARN" if warnings else "PASS"
    trace.decision(
        "platform_health", f"audit_{verdict.lower()}",
        f"Platform audit {verdict}: {len(criticals)} critical, {len(warnings)} warnings "
        f"across {len(checks)} checks",
        critical=len(criticals), warnings=len(warnings), checks=len(checks),
    )

    lines = [f"*Platform audit: {verdict}*",
             f"{len(checks)} checks · {len(criticals)} critical · {len(warnings)} warnings", ""]
    for finding in criticals + warnings:
        icon = ":red_circle:" if finding.severity == CRITICAL else ":warning:"
        lines.append(f"{icon} *{finding.check}* - {finding.message}")
        lines.append(f"     _fix:_ {finding.fix}")
    if not findings:
        lines.append(":white_check_mark: Every agent owned, reviewed, and running clean.")
    lines += ["", f"_Trace: {trace.trace_id}_"]

    with trace.step("notify_slack") as step:
        ctx.tools.slack.call(
            "post_message",
            {"channel": "#agent-platform-health", "text": "\n".join(lines)},
            idempotency_key=f"{ctx.event.event_id}:slack",
        )
        step.set(verdict=verdict)

    trace.record("audit_complete", OK if verdict == "PASS" else DEGRADED,
                 verdict=verdict, findings=[f.as_dict() for f in findings])

    return {
        "acted": True, "verdict": verdict,
        "critical": len(criticals), "warnings": len(warnings),
        "findings": [f.as_dict() for f in findings],
        "summary": f"platform audit {verdict}: "
                   f"{len(criticals)} critical, {len(warnings)} warnings",
    }
