"""Spend and runaway protection.

Two failure modes this exists to prevent, both of which end with a large bill and
no alerts delivered:

  1. A flapping signal. One account's health score oscillates across a threshold
     and fires the same agent fifty times an hour, each run paying for an LLM
     call to reach the same conclusion.
  2. Budget exhaustion. Spend climbs until the daily cap trips, after which every
     account silently degrades - including the ones that genuinely needed the
     model.

The rule in both cases is the same and it is the important part: **when a limit
trips, the work does not stop, it routes to a human.** A cost control that
silently drops customer alerts is worse than the cost it saves. So a throttled
run still produces a deterministic analysis, still writes, still notifies, and is
explicitly flagged for human verification with the reason recorded in the trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .store import Warehouse


@dataclass
class LimitDecision:
    """Whether the LLM may be called, and why not if it may not."""

    allow_llm: bool
    reason: str
    limit_hit: str | None = None
    # A throttled run is never silently downgraded: a human is told.
    force_human_review: bool = False

    def as_detail(self) -> dict[str, Any]:
        return {
            "allow_llm": self.allow_llm,
            "reason": self.reason,
            "limit_hit": self.limit_hit,
            "force_human_review": self.force_human_review,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def check_limits(warehouse: Warehouse, config: Config, account_id: str) -> LimitDecision:
    """Decide whether this run may spend money on a model call."""
    max_per_account = config.get("limits.max_llm_calls_per_account_per_hour", 5)
    daily_budget = config.get("llm.daily_cost_budget_usd")
    reserve_ratio = config.get("limits.human_review_reserve_ratio", 0.9)

    # 1. Per-account throttle. Catches the flapping-signal loop.
    if max_per_account:
        since = (_utc_now() - timedelta(hours=1)).isoformat()
        used = warehouse.count_llm_calls_for_account(account_id, since)
        if used >= max_per_account:
            return LimitDecision(
                allow_llm=False,
                reason=(
                    f"account {account_id} has already used {used} model calls in the "
                    f"last hour (limit {max_per_account}); this looks like a flapping "
                    f"trigger, so the run continues without the model and goes to a human"
                ),
                limit_hit="account_hourly_llm_calls",
                force_human_review=True,
            )

    # 2. Daily budget, with a reserve. We stop paying for the model before the
    #    cap is fully drained, so the last runs of the day degrade predictably
    #    rather than mid-analysis.
    if daily_budget:
        midnight = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        spent = warehouse.llm_spend_since(midnight)
        ceiling = float(daily_budget) * float(reserve_ratio)
        if spent >= ceiling:
            return LimitDecision(
                allow_llm=False,
                reason=(
                    f"daily model spend ${spent:.4f} has reached the "
                    f"${ceiling:.4f} soft ceiling ({reserve_ratio:.0%} of the "
                    f"${daily_budget} budget); analysis continues deterministically "
                    f"and goes to a human"
                ),
                limit_hit="daily_cost_soft_ceiling",
                force_human_review=True,
            )

    return LimitDecision(allow_llm=True, reason="within per-account and daily limits")


def spend_report(warehouse: Warehouse, config: Config) -> dict[str, Any]:
    """Today's spend against budget. Backs the dashboard cost panel."""
    budget = config.get("llm.daily_cost_budget_usd")
    reserve_ratio = config.get("limits.human_review_reserve_ratio", 0.9)
    midnight = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    spent = warehouse.llm_spend_since(midnight)
    calls = len(warehouse.steps_named("llm_cost", limit=10_000))
    analyses = warehouse.steps_named("analyse", limit=10_000)
    llm_runs = [s for s in analyses if (s["detail"] or {}).get("method") == "llm"]

    soft_ceiling = float(budget) * float(reserve_ratio) if budget else None

    return {
        "daily_budget_usd": budget,
        "soft_ceiling_usd": round(soft_ceiling, 4) if soft_ceiling else None,
        "spent_today_usd": round(spent, 5),
        "remaining_usd": round(float(budget) - spent, 5) if budget else None,
        "budget_used_pct": round(100 * spent / float(budget), 1) if budget else None,
        "throttled": bool(soft_ceiling and spent >= soft_ceiling),
        "llm_calls": calls,
        "cost_per_analysis_usd": round(spent / len(llm_runs), 5) if llm_runs else None,
        # A projection is a planning number, not a promise: it assumes today's
        # rate holds, which it will not. Labelled as such in the UI.
        "projected_monthly_usd": round(spend_projection(spent), 2),
        "by_agent": warehouse.spend_by("agent", midnight),
    }


def spend_projection(spent_today: float) -> float:
    """Naive 30-day projection from today's spend so far, scaled to a full day."""
    now = _utc_now()
    elapsed_hours = now.hour + now.minute / 60
    if elapsed_hours < 0.5:
        return spent_today * 30
    return (spent_today / elapsed_hours) * 24 * 30
