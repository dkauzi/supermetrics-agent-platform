"""Output verification - the gate between the model and any system of record.

The LLM is never trusted. Before anything is written to Salesforce or posted to
Slack, the analysis passes through here.

The important design choice: evidence is *structured*, not prose. The model must
cite `metric` / `value` pairs, and this module checks each one against the facts
we actually fetched. That turns "did the model hallucinate?" from a judgement call
into a deterministic comparison. No second LLM required, no extra cost, no
non-determinism in the safety layer.

A separate optional LLM judge scores the *writing* quality of the alert, which is
genuinely subjective - that is the right division of labour.
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


def claim_is_grounded(item: Any, facts: dict[str, Any], tolerance: float = 0.01) -> bool:
    """Is this one citation supported by the facts we retrieved?

    The single definition of "grounded". Both the production verifier and the
    golden eval call this, because two implementations of the same rule will
    drift and then disagree - which they did: the eval scored narrative
    citations with exact string equality and reported 62% grounding on output
    the production path had already accepted as valid.
    """
    metric = getattr(item, "metric", None)
    if metric not in facts:
        return False

    actual = facts[metric]
    if _is_text_list(actual):
        return _quotes_source(getattr(item, "value", None), actual)
    return _matches(getattr(item, "value", None), actual, tolerance)


def _is_text_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)


def _normalise_text(text: str) -> str:
    """Fold whitespace and quote characters so formatting is not a violation.

    A model rewrapping a line or using a typographic apostrophe is not
    hallucinating. Changing a word is. This normalises the former and leaves the
    latter detectable.
    """
    text = str(text).replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("—", "-").replace("–", "-")
    return " ".join(text.lower().split())


def _quotes_source(value: Any, sources: list[str]) -> bool:
    """True when the citation is a verbatim slice of one retrieved record."""
    claim = _normalise_text(value)
    if len(claim) < 12:
        # Too short to be evidence of anything; "open" would match half the corpus.
        return False
    return any(claim in _normalise_text(source) for source in sources)


def _truncate(value: Any, limit: int = 70) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


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

    Two kinds of claim, both checked, neither trusted:

    * **Numeric** - `metric` names a scalar fact and `value` must match it within
      tolerance. Exact, cheap, and catches an invented number immediately.
    * **Narrative** - `metric` names a list of strings we retrieved (CS notes,
      usage telemetry, ticket subjects) and `value` must appear verbatim in one
      of them. Real customer-success evidence is prose, and a churn driver like
      "the champion left" has no numeric form. Requiring a verbatim substring
      means the model can only quote what we actually gave it: paraphrase is a
      violation, because a paraphrase is where a hallucination hides.
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

        actual = facts[metric]

        # Narrative claim: the fact is a list of strings we retrieved, so the
        # citation must appear verbatim inside one of them.
        if _is_text_list(actual):
            if not _quotes_source(value, actual):
                violations.append(
                    f"'{metric}' cited as \"{_truncate(value)}\" but that text does not "
                    f"appear in the {len(actual)} record(s) we retrieved"
                )
                continue
            grounded += 1
            continue

        if not _matches(value, actual, tolerance):
            violations.append(
                f"metric '{metric}' cited as '{value}' but actual value is '{actual}'"
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
