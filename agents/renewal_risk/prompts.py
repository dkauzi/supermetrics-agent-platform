"""Versioned prompts.

Prompts are code: they live in version control, they are addressed by version, and
rolling one back is a config change (`agents.renewal_risk.prompt_version`) rather
than a redeploy. Every version is kept, never edited in place — editing v2 in
place would silently invalidate every golden-eval result recorded against it.

Promotion path: add v3 -> run `python cli.py eval --prompt-version v3` ->
compare against the active version -> flip config -> keep v2 for instant rollback.
"""

from __future__ import annotations

import json
from typing import Any

from agentplatform.llm import PromptBundle
from .schemas import ChurnDriver

DRIVERS: tuple[str, ...] = ChurnDriver.__args__  # type: ignore[attr-defined]

_SYSTEM_V1 = """You are a customer-success analyst for a B2B SaaS company.
Given account facts, identify the single most likely driver of churn risk.
Respond only with JSON."""

_SYSTEM_V2 = """You are a customer-success analyst for a B2B SaaS company.

Your job: identify the SINGLE most likely driver of churn risk for this account, \
and justify it with evidence drawn strictly from the facts provided.

Hard rules:
- `driver` MUST be exactly one of: {drivers}
- Every item in `evidence` MUST cite a `metric` key that appears verbatim in the \
FACTS table, and its `value` MUST equal the value shown there. Do not round, \
reformat, derive or invent values. Claims that cannot be checked against FACTS \
will be rejected automatically and the analysis discarded.
- Cite at least 2 evidence items where the facts support it.
- If the facts genuinely do not indicate a driver, return "unknown" with low \
confidence. A calibrated "unknown" is more useful than a confident guess.
- `confidence` is your honest probability that this driver is correct, 0.0-1.0.
- `alert_message` is read by a busy account owner in Slack: 2-3 sentences, name \
the driver, cite the two strongest numbers, state what changed. No greeting, no \
sign-off, no markdown headers.

Respond with JSON only, matching this schema:
{schema}"""

_USER_TEMPLATE = """ACCOUNT
{account}

FACTS (the only values you may cite)
{facts}

TRIGGER
{trigger}

Identify the single most likely churn driver and produce the internal alert."""


def _schema_hint() -> str:
    return json.dumps({
        "driver": "one of the allowed values",
        "driver_explanation": "string, max 400 chars",
        "evidence": [{"metric": "fact key", "value": "value from FACTS",
                      "interpretation": "string, max 200 chars"}],
        "confidence": 0.0,
        "recommended_action": "string, max 300 chars",
        "alert_message": "string, max 600 chars",
    }, indent=2)


def _format_facts(facts: dict[str, Any]) -> str:
    """A flat key: value table. The model cites these keys; the verifier checks them."""
    return "\n".join(f"  {key}: {value}" for key, value in sorted(facts.items()))


def build(version: str, account: dict[str, Any], facts: dict[str, Any],
          trigger: dict[str, Any]) -> PromptBundle:
    if version not in PROMPT_VERSIONS:
        raise ValueError(
            f"Unknown prompt version '{version}'. Available: {sorted(PROMPT_VERSIONS)}"
        )

    system = PROMPT_VERSIONS[version]
    if version == "v2":
        system = system.format(drivers=", ".join(DRIVERS), schema=_schema_hint())

    user = _USER_TEMPLATE.format(
        account=json.dumps(account, indent=2, default=str),
        facts=_format_facts(facts),
        trigger=json.dumps(trigger, indent=2, default=str),
    )
    return PromptBundle(name="renewal_risk.churn_driver", version=version,
                        system=system, user=user)


PROMPT_VERSIONS: dict[str, str] = {
    # v1: kept for rollback and for golden-eval comparison. Do not edit.
    "v1": _SYSTEM_V1,
    # v2: added the closed driver taxonomy, verbatim-citation rule and the
    # calibrated-unknown instruction. Grounding violations dropped sharply.
    "v2": _SYSTEM_V2,
}
