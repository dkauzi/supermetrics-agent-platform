"""Golden eval set - the regression gate for prompt and model changes.

A prompt change is a production change. This is what stops one going live on a
hunch. It runs the real analysis path (prompt -> model -> schema validation ->
grounding verification) over labelled cases and scores three things:

  driver accuracy   did it name the right churn driver?
  grounding rate    was every cited number real?
  citation recall   did it cite the metric that actually mattered?

Two properties make this useful rather than decorative:

  * It runs against ANY prompt version and ANY model, both config-driven, so
    "is v3 better than v2?" and "is the cheap model good enough?" are the same
    command with one flag changed.
  * Offline it scores the deterministic fallback. That is the floor the LLM has
    to beat - if the model cannot outscore rules-based logic, it is not worth
    its latency or its cost, and this prints that verdict plainly.

CI runs this offline on every push. Before promoting a prompt, run it live.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentplatform.config import llm_mode
from agentplatform.events import Event
from agentplatform.verifier import claim_is_grounded

CASES = Path(__file__).parent / "cases.json"
EVAL_AGENT = "renewal_risk_eval"


@dataclass
class EvalContext:
    """Minimal stand-in for AgentContext, so eval runs the real analysis code path."""

    event: Event
    trace: Any
    warehouse: Any
    config: Any
    entry: Any

    def agent_config(self, path: str, default: Any = None) -> Any:
        return self.config.get(f"agents.renewal_risk.{path}", default)


@dataclass
class _Entry:
    name: str = EVAL_AGENT
    version: str = "eval"


def _score_case(analysis, meta, case: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    cited = {item.metric for item in analysis.evidence}

    driver_ok = analysis.driver == case["expected_driver"]
    # Same grounding definition the production verifier uses. Reimplementing it
    # here previously reported 62% on output production had accepted as valid.
    grounded = all(claim_is_grounded(item, facts) for item in analysis.evidence)
    citation_ok = bool(cited & set(case.get("must_cite_any", []))) if case.get("must_cite_any") else True

    # For the ambiguous case, a confident wrong answer is worse than a hedge.
    confidence_ok = True
    if case.get("expect_low_confidence"):
        confidence_ok = analysis.confidence <= 0.5 or analysis.driver == "unknown"

    return {
        "case": case["id"],
        "expected": case["expected_driver"],
        "actual": analysis.driver,
        "driver_ok": driver_ok,
        "grounded": grounded,
        "citation_ok": citation_ok,
        "confidence_ok": confidence_ok,
        "confidence": round(analysis.confidence, 2),
        "method": meta.method,
        "cost_usd": round(meta.cost_usd, 6),
        "passed": driver_ok and grounded and citation_ok and confidence_ok,
    }


def run_eval(platform, prompt_version: str | None = None, samples: int = 3) -> int:
    """Score each case over `samples` runs, not one.

    An LLM is stochastic, so a single run per case measures luck. This was not
    theoretical: prompt v3 returned "unknown" on the ambiguous case in one run and
    a confident "engagement_gap" (0.8) in the next, from identical input. A
    one-shot eval would have promoted it on the strength of the lucky run.

    So the unit of measurement is the pass RATE per case, and consistency is
    reported as a first-class metric. A case that passes 2 of 3 times is not a
    passing case; it is an unstable one, and instability on ambiguous accounts is
    exactly where false positives come from.
    """
    from agents.renewal_risk.analysis import analyse, build_facts

    cases = json.loads(CASES.read_text())
    if prompt_version:
        # Override for this run only, so you can diff versions without editing config.
        platform.config.raw.setdefault("agents", {}).setdefault(
            "renewal_risk", {})["prompt_version"] = prompt_version

    active_version = platform.config.get("agents.renewal_risk.prompt_version", "v2")
    mode = llm_mode()

    # Offline the analyser is deterministic, so repeating it only burns time.
    effective_samples = 1 if mode == "offline" else samples

    print(f"\nGolden eval · prompt={active_version} · mode={mode} · "
          f"samples={effective_samples} · models={platform.config.get('llm.model_chain')}")
    print(f"{len(cases)} cases x {effective_samples} runs\n")
    print(f"{'CASE':<28} {'EXPECTED':<30} {'OBSERVED':<34} {'PASS RATE'}")
    print("─" * 108)

    results, per_case = [], []
    for case in cases:
        runs = []
        for sample in range(effective_samples):
            event = Event(
                event_id=f"eval-{case['id']}-{sample}-{datetime.now(timezone.utc).timestamp()}",
                event_type="health_score.dropped", source="eval",
                account_id=case.get("supplied_account") or case["account"]["account_id"],
                occurred_at=datetime.now(timezone.utc),
                payload={"eval_case": case["id"], "sample": sample},
            )
            platform.warehouse.record_event(event)
            trace = platform.observability.start_run(event, EVAL_AGENT)

            # Cases either carry inline fixtures, or name an account from the
            # supplied payload and pull it through the same client layer the
            # agent uses. The latter means the eval exercises the real retrieval
            # path, not a hand-copied snapshot that can drift from it.
            if case.get("supplied_account"):
                from agentplatform.clients import fixtures as supplied_fixtures
                account = supplied_fixtures.salesforce_account(case["supplied_account"])
                health = supplied_fixtures.gainsight_health(case["supplied_account"])
                support = supplied_fixtures.zendesk_tickets(case["supplied_account"]) or {}
                health = {**health, **{"support": support}}
                facts = build_facts(account, health)
                facts["support_ticket_subjects"] = support.get("ticket_subjects", [])
            else:
                account = case["account"]
                facts = build_facts(case["account"], case["health"])
            ctx = EvalContext(event, trace, platform.warehouse, platform.config, _Entry())

            analysis, meta = analyse(ctx, account, facts, {"eval": True})
            score = _score_case(analysis, meta, case, facts)
            trace.finish("ok", summary=f"eval {case['id']} #{sample}: {score['passed']}")
            runs.append(score)
            results.append(score)

        observed = sorted({r["actual"] for r in runs})
        passes = sum(r["passed"] for r in runs)
        stable = len(observed) == 1

        per_case.append({
            "case": case["id"], "expected": case["expected_driver"],
            "observed": observed, "pass_rate": passes / len(runs),
            "stable": stable, "runs": len(runs),
        })

        flag = "" if stable else "  UNSTABLE"
        print(f"{case['id']:<28} {case['expected_driver']:<30} "
              f"{','.join(observed)[:34]:<34} {passes}/{len(runs)}{flag}")

    total = len(results)
    passed = sum(r["passed"] for r in results)
    driver_accuracy = sum(r["driver_ok"] for r in results) / total
    grounding_rate = sum(r["grounded"] for r in results) / total
    consistency = sum(c["stable"] for c in per_case) / len(per_case)
    cost = sum(r["cost_usd"] for r in results)

    print("─" * 108)
    print(f"passed          {passed}/{total} runs")
    print(f"driver accuracy {driver_accuracy:.0%}")
    print(f"grounding rate  {grounding_rate:.0%}   (must be 100% - an ungrounded claim is a defect, not a miss)")
    print(f"consistency     {consistency:.0%}   (cases giving the same driver on every run)")
    print(f"eval cost       ${cost:.4f}")

    min_accuracy = platform.config.get("eval.min_driver_accuracy", 0.6)
    min_grounding = platform.config.get("eval.min_grounding_rate", 1.0)
    min_consistency = platform.config.get("eval.min_consistency", 0.8)

    failures = []
    if driver_accuracy < min_accuracy:
        failures.append(f"driver accuracy {driver_accuracy:.0%} below floor {min_accuracy:.0%}")
    if grounding_rate < min_grounding:
        failures.append(f"grounding rate {grounding_rate:.0%} below floor {min_grounding:.0%}")
    if effective_samples > 1 and consistency < min_consistency:
        unstable = [c["case"] for c in per_case if not c["stable"]]
        failures.append(
            f"consistency {consistency:.0%} below floor {min_consistency:.0%}; "
            f"unstable cases: {unstable}"
        )

    # Persist the verdict so the dashboard can show the gate status without
    # anyone having to re-run the eval or read CI logs.
    from agentplatform.config import data_dir
    (data_dir() / "last_eval.json").write_text(json.dumps({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "prompt_version": active_version,
        "mode": mode,
        "samples": effective_samples,
        "models": platform.config.get("llm.model_chain"),
        "passed": passed, "total": total,
        "driver_accuracy": round(driver_accuracy, 3),
        "grounding_rate": round(grounding_rate, 3),
        "consistency": round(consistency, 3),
        "cost_usd": round(cost, 5),
        "gate_passed": not failures,
        "failures": failures,
        "cases": [
            {"case": c["case"], "expected": c["expected"],
             "actual": ",".join(c["observed"]),
             "passed": c["pass_rate"] == 1.0,
             "pass_rate": round(c["pass_rate"], 2), "stable": c["stable"]}
            for c in per_case
        ],
    }, indent=2))

    if failures:
        print("\nGATE FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        print("Do not promote this prompt/model combination.")
        return 1

    print("\nGATE PASSED - safe to promote.")
    return 0


if __name__ == "__main__":
    from agentplatform import build_platform
    sys.exit(run_eval(build_platform()))
