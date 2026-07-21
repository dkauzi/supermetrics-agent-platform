"""The contract the LLM must satisfy.

Two decisions worth defending in review:

1. `driver` is a closed enum, not free text. Free-text drivers drift ("low usage",
   "usage down", "declining adoption" are three labels for one thing), which makes
   precision unmeasurable and the learning loop impossible. A fixed taxonomy is
   what lets us say "this driver has been right 4 of 12 times".

2. `evidence` is structured metric/value pairs, not prose. That is what allows
   agentplatform.verifier to check grounding deterministically instead of asking
   another model whether the first model was lying.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ChurnDriver = Literal[
    "adoption_decline",              # licensed seats stopped being used
    "support_burden",                # volume/severity of support pain
    "champion_loss",                 # sponsor or key user left
    "value_realisation_gap",         # using it, not getting outcomes
    "data_integration_regression",   # connectors/sources dropped off
    "engagement_gap",                # no QBR / no contact with CS
    "pricing_pressure",              # budget or commercial objection
    "unknown",                       # explicit escape hatch, never a guess
]


class EvidenceItem(BaseModel):
    """One verifiable claim. `metric` must be a key from the retrieved facts."""

    metric: str = Field(..., description="Exact metric key from the FACTS table")
    value: float | int | str | bool = Field(..., description="The value as retrieved")
    interpretation: str = Field(..., max_length=200,
                                description="Why this value supports the driver")


class ChurnAnalysis(BaseModel):
    driver: ChurnDriver
    driver_explanation: str = Field(..., max_length=400)
    evidence: list[EvidenceItem] = Field(..., min_length=1, max_length=6)
    confidence: float = Field(..., ge=0.0, le=1.0)
    recommended_action: str = Field(..., max_length=300)
    alert_message: str = Field(
        ..., max_length=600,
        description="Short internal alert naming the driver and citing evidence",
    )


class AnalysisMeta(BaseModel):
    """How the analysis was produced. Always written to the trace."""

    method: Literal["llm", "deterministic_fallback"]
    model: str | None = None
    prompt_version: str | None = None
    attempts: int = 0
    repaired: bool = False
    cost_usd: float = 0.0
    degraded_reason: str | None = None
    raw_confidence: float | None = None
    calibrated_confidence: float | None = None
