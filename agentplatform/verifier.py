"""Output verification — the gate between the model and any system of record.

The LLM is never trusted. Before anything is written to Salesforce or posted to
Slack, the analysis passes through here.

The important design choice: evidence is *structured*, not prose. The model must
cite `metric` / `value` pairs, and this module checks each one against the facts
we actually fetched. That turns "did the model hallucinate?" from a judgement call
into a deterministic comparison. No second LLM required, no extra cost, no
non-determinism in the safety layer.

A separate optional LLM judge scores the *writing* quality of the alert, which is
genuinely subjective — that is the right division of labour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .observability import DEGRADED, OK, RunTrace


@dataclass
class VerificationResult:
    passed: bool
    grounded_claims: int
    total_claims: int
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def grounding_rate(self) -> float:
        return self.grounded_claims / self.total_claims if self.total_claims else 0.0

    def as_detail(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "grounded_claims": self.grounded_claims,
            "total_claims": self.total_claims,
            "grounding_rate": round(self.grounding_rate, 3),
            "violations": self.violations,
            "warnings": self.warnings,
        }


def _matches(claimed: Any, actual: Any, tolerance: float) -> bool:
    """Numbers compare within tolerance; everything else compares as text."""
    if isinstance(claimed, (int, float)) and isinstance(actual, (int, float)):
        if actual == 0:
            return abs(float(claimed)) <= tolerance
        return abs(float(claimed) - float(actual)) / abs(float(actual)) <= tolerance
    return str(claimed).strip().lower() == str(actual).strip().lower()


def verify_grounding(
    evidence: list[Any],
    facts: dict[str, Any],
    trace: RunTrace,
    *,
    min_claims: int = 1,
    tolerance: float = 0.01,
    allow_unverifiable: bool = False,
) -> VerificationResult:
    """Check every cited claim against the facts we actually retrieved.

    `evidence` items must expose `.metric` and `.value`. `facts` is the flattened
    dict of everything we fetched from Salesforce / Gainsight / Zendesk.
    """
    violations: list[str] = []
    warnings: list[str] = []
    grounded = 0

    for item in evidence:
        metric = getattr(item, "metric", None)
        value = getattr(item, "value", None)

        if metric not in facts:
            message = f"cited unknown metric '{metric}' (not in retrieved facts)"
            (warnings if allow_unverifiable else violations).append(message)
            continue

        if not _matches(value, facts[metric], tolerance):
            violations.append(
                f"metric '{metric}' cited as '{value}' but actual value is '{facts[metric]}'"
            )
            continue

        grounded += 1

    total = len(evidence)
    if total < min_claims:
        violations.append(f"analysis cited {total} evidence items, minimum is {min_claims}")

    result = VerificationResult(
        passed=not violations,
        grounded_claims=grounded,
        total_claims=total,
        violations=violations,
        warnings=warnings,
    )

    trace.record(
        "verify_grounding",
        OK if result.passed else DEGRADED,
        **result.as_detail(),
    )
    return result
