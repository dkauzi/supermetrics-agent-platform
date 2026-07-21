"""Data minimisation before anything leaves for a third-party model provider.

Supermetrics is an EU company and account context is customer data. Sending a
customer's name, their account owner's name and email to OpenRouter, which then
routes to whichever model vendor, is a transfer nobody signed off on and nobody
can later audit.

The observation this rests on: **the model does not need the identity.** Churn
driver analysis is a judgement about metrics. "Seat utilisation fell from 86% to
34%" identifies the driver exactly as well whether the account is called
"Northwind Media Group" or "ACCOUNT_A1". So we replace direct identifiers with
stable per-run tokens, send the metrics, and re-insert the real values into the
model's output afterwards.

The result: full-quality analysis, nothing personally identifying crosses the
boundary, and the alert a human reads still names the real account. Minimisation
by design rather than a policy document telling people to be careful.

This is not anonymisation in the GDPR sense (the mapping exists in memory for the
duration of the run, and the metrics themselves could be linkable). It is data
minimisation, which is the applicable principle for a processing step that does
not need the identity to do its job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import Config

# Account fields that identify a person or a customer. Metrics are never listed
# here: the whole point is that they still go to the model.
IDENTIFYING_FIELDS = ("name", "owner", "owner_slack", "cs_lead_slack",
                      "owner_email", "account_id", "id")

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass
class Pseudonymiser:
    """Swaps identifiers for tokens on the way out, and back on the way in."""

    enabled: bool = True
    _to_token: dict[str, str] = field(default_factory=dict)
    _to_real: dict[str, str] = field(default_factory=dict)

    def _token_for(self, value: str, kind: str) -> str:
        if value in self._to_token:
            return self._to_token[value]
        token = f"{kind.upper()}_{len(self._to_token) + 1}"
        self._to_token[value] = token
        self._to_real[token] = value
        return token

    def scrub_account(self, account: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of the account with identifiers replaced by tokens."""
        if not self.enabled:
            return account

        scrubbed = dict(account)
        for key in IDENTIFYING_FIELDS:
            value = account.get(key)
            if isinstance(value, str) and value:
                kind = "account" if key in ("name", "account_id", "id") else "person"
                scrubbed[key] = self._token_for(value, kind)
        return scrubbed

    def rehydrate(self, text: str) -> str:
        """Put the real names back into model output before a human sees it."""
        if not self.enabled or not text:
            return text
        for token, real in self._to_real.items():
            text = text.replace(token, real)
        return text

    @property
    def token_count(self) -> int:
        return len(self._to_token)

    def audit(self) -> dict[str, Any]:
        """What was withheld, without recording what it was.

        Deliberately reports only the shape. Writing the mapping into the trace
        would reintroduce exactly the data we just removed.
        """
        return {
            "enabled": self.enabled,
            "identifiers_replaced": len(self._to_token),
            "tokens": sorted(self._to_real),
        }


def redact_free_text(text: str) -> str:
    """Strip emails from anything we persist. Cheap defence in depth."""
    return EMAIL.sub("[email]", text or "")


def build_pseudonymiser(config: Config) -> Pseudonymiser:
    return Pseudonymiser(enabled=bool(config.get("privacy.minimise_llm_payload", True)))
