"""The learning loop.

A dashboard that only shows what happened is a report. This makes it a loop: a CS
person marks an alert correct or wrong, and the agent reads the resulting
precision back *at analysis time* on the next run.

Concretely, if "support_burden" has been the predicted churn driver 12 times and
was judged wrong 8 of them, the agent stops asserting it confidently and routes
those alerts for human review instead. Nobody has to remember to tune a threshold;
the measured outcome does it.

Everything here is derived from recorded outcomes. No hardcoded quality scores.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Config
from .store import Warehouse

CORRECT = "correct"
WRONG = "wrong"
UNCLEAR = "unclear"
VALID_VERDICTS = {CORRECT, WRONG, UNCLEAR}


@dataclass
class DriverStats:
    driver: str
    samples: int
    correct: int
    precision: float
    confident: bool          # enough samples to act on
    trusted: bool            # precision high enough to assert without review
    needs_review: bool       # precision low enough to force a human

    def as_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "samples": self.samples,
            "correct": self.correct,
            "precision": round(self.precision, 3),
            "confident": self.confident,
            "trusted": self.trusted,
            "needs_review": self.needs_review,
        }


class Calibration:
    """Precision per predicted churn driver, computed from human verdicts."""

    def __init__(self, warehouse: Warehouse, config: Config, agent: str) -> None:
        self.agent = agent
        self.min_samples = config.get("learning.min_samples", 5)
        self.trust_threshold = config.get("learning.trust_precision", 0.75)
        self.review_threshold = config.get("learning.review_precision", 0.5)
        self._stats = self._compute(warehouse.outcomes(agent))

    def _compute(self, rows: list[dict[str, Any]]) -> dict[str, DriverStats]:
        buckets: dict[str, list[str]] = {}
        for row in rows:
            # 'unclear' verdicts carry no signal - excluded rather than counted
            # as wrong, which would bias every driver downward.
            if row["verdict"] == UNCLEAR:
                continue
            buckets.setdefault(row["driver"], []).append(row["verdict"])

        stats: dict[str, DriverStats] = {}
        for driver, verdicts in buckets.items():
            samples = len(verdicts)
            correct = sum(1 for v in verdicts if v == CORRECT)
            precision = correct / samples if samples else 0.0
            confident = samples >= self.min_samples
            stats[driver] = DriverStats(
                driver=driver,
                samples=samples,
                correct=correct,
                precision=precision,
                confident=confident,
                # Until we have enough samples we neither trust nor distrust:
                # cold start must not silently behave like a proven-good driver.
                trusted=confident and precision >= self.trust_threshold,
                needs_review=confident and precision < self.review_threshold,
            )
        return stats

    def for_driver(self, driver: str) -> DriverStats | None:
        return self._stats.get(driver)

    def needs_human_review(self, driver: str) -> tuple[bool, str]:
        """Should this prediction be held for a human before it is asserted?"""
        stats = self._stats.get(driver)
        if stats is None:
            return False, f"no outcome history for driver '{driver}' - treated as unproven"
        if stats.needs_review:
            return True, (
                f"driver '{driver}' has been judged correct {stats.correct}/{stats.samples} "
                f"times (precision {stats.precision:.0%}, below the "
                f"{self.review_threshold:.0%} review threshold)"
            )
        return False, (
            f"driver '{driver}' precision {stats.precision:.0%} over {stats.samples} "
            f"reviewed alerts"
        )

    def confidence_multiplier(self, driver: str) -> float:
        """Scale the model's self-reported confidence by measured precision.

        Cold start returns 1.0 - we do not invent a penalty we cannot justify.
        """
        stats = self._stats.get(driver)
        if stats is None or not stats.confident:
            return 1.0
        return max(0.3, min(1.15, stats.precision / self.trust_threshold))

    def table(self) -> list[dict[str, Any]]:
        return [s.as_dict() for s in sorted(
            self._stats.values(), key=lambda s: (-s.samples, s.driver)
        )]

    def summary(self) -> dict[str, Any]:
        total = sum(s.samples for s in self._stats.values())
        correct = sum(s.correct for s in self._stats.values())
        return {
            "agent": self.agent,
            "reviewed_alerts": total,
            "overall_precision": round(correct / total, 3) if total else None,
            "drivers_tracked": len(self._stats),
            "drivers_needing_review": [s.driver for s in self._stats.values() if s.needs_review],
            "drivers_trusted": [s.driver for s in self._stats.values() if s.trusted],
            "min_samples_to_act": self.min_samples,
        }


def record_outcome(
    warehouse: Warehouse,
    trace_id: str,
    agent: str,
    account_id: str,
    driver: str,
    severity: str,
    verdict: str,
    notes: str | None = None,
    reviewer: str | None = None,
) -> dict[str, Any]:
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VALID_VERDICTS)}, got '{verdict}'")

    row = {
        "trace_id": trace_id, "agent": agent, "account_id": account_id,
        "driver": driver, "severity": severity, "verdict": verdict,
        "notes": notes, "reviewer": reviewer,
    }
    warehouse.record_outcome(row)
    return row
