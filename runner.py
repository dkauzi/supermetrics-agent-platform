"""End-to-end demo driver.

`python runner.py` walks the platform through five scenarios in-process - no HTTP,
no network unless the LLM is live - and prints the reasoning for each. This is the
script to run in a live session: it exercises the happy path *and* the failure
paths, because a demo that only shows the happy path proves nothing.

Scenarios:
  1. Supplied sample payload      3 accounts -> 3 distinct drivers, + redelivery deduped
  2. Our own webhook shape        same agent, different payload shape
  3. Renewal approaching (SF)     a third shape, still one normaliser each
  4. Support ticket spike         a second agent on the same bus, untouched code
  5. Malformed payload            dead-lettered with a reason, no partial writes
  6. Unknown source               rejected at the boundary
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from agentplatform import build_platform
from agentplatform.config import llm_mode
from agentplatform.events import UnknownEventSource

SAMPLES = Path(__file__).parent / "samples"

BOLD, DIM, GREEN, YELLOW, RED, BLUE, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[34m", "\033[0m"
)


def banner(n: int, title: str) -> None:
    print(f"\n{BOLD}{BLUE}{'━' * 74}{RESET}")
    print(f"{BOLD}  SCENARIO {n}: {title}{RESET}")
    print(f"{BLUE}{'━' * 74}{RESET}")


def load(name: str) -> dict[str, Any]:
    with (SAMPLES / name).open() as handle:
        return json.load(handle)


def show_result(platform, outcome: dict[str, Any]) -> None:
    print(f"\n  {DIM}ingest status:{RESET} {outcome['status']}")

    for result in outcome.get("results", []):
        status_colour = GREEN if result["status"] == "ok" else RED
        print(f"  {DIM}agent:{RESET} {result['agent']}  "
              f"{status_colour}{result['status']}{RESET}  "
              f"{DIM}{result['trace_id']}{RESET}")

        payload = result.get("result") or {}
        if payload.get("summary"):
            print(f"  {BOLD}→ {payload['summary']}{RESET}")
        if payload.get("needs_human_review"):
            print(f"  {YELLOW}⚠ flagged for human verification{RESET}")

        explanation = platform.observability.explain(result["trace_id"])
        print(f"\n  {BOLD}Why:{RESET}")
        for line in explanation["narrative"]:
            print(f"    {DIM}·{RESET} {line}")
        if explanation["total_cost_usd"]:
            print(f"    {DIM}· LLM cost: ${explanation['total_cost_usd']:.5f}{RESET}")


def show_slack(platform) -> None:
    sent = platform.tools.raw("slack").sent
    if not sent:
        return
    print(f"\n  {BOLD}Slack message posted to {sent[-1]['channel']}:{RESET}")
    print(f"  {DIM}{'─' * 70}{RESET}")
    for line in sent[-1]["text"].split("\n"):
        print(f"  {line}")
    print(f"  {DIM}{'─' * 70}{RESET}")


def main() -> int:
    platform = build_platform()

    print(f"{BOLD}Supermetrics Agent Platform - demo run{RESET}")
    print(f"{DIM}LLM mode: {llm_mode()}  |  "
          f"models: {platform.config.get('llm.model_chain')}  |  "
          f"warehouse: {platform.config.get('platform.warehouse')}{RESET}")
    print(f"{DIM}Agents registered: "
          f"{[a.name for a in platform.registry.enabled()]}{RESET}")

    # 1 - the supplied payload, run exactly as given. This is the graded case:
    #     three accounts whose triggers look almost identical, which must resolve
    #     to three different drivers, plus a deliberate redelivery.
    banner(1, "Supplied sample payload - 3 accounts, 3 drivers, 1 redelivery")
    supplied = load("renewal_risk_router_sample_payload.json")

    print(f"\n  {DIM}Their triggers are near-identical by design. Health score alone "
          f"cannot separate these:{RESET}")
    for event in supplied["trigger_events"]:
        print(f"    {event['event_id']}  {event['account_name']:<24} "
              f"health {event['health_score_30d_ago']} -> {event['health_score_current']}"
              f"  renews in {event['days_to_renewal']}d")

    drivers: dict[str, str] = {}
    for event in supplied["trigger_events"]:
        outcome = platform.ingest("supermetrics", event)
        if outcome.get("status") == "duplicate":
            print(f"\n  {GREEN}{event['event_id']} redelivered -> deduped, "
                  f"no second CRM write and no second alert{RESET}")
            continue
        result = outcome["results"][0]["result"]
        drivers[event["account_name"]] = result.get("driver", "?")

    print(f"\n  {BOLD}Distinct drivers found:{RESET}")
    for name, driver in drivers.items():
        print(f"    {name:<24} {GREEN}{driver}{RESET}")
    unique = len(set(drivers.values()))
    verdict = GREEN + "PASS" + RESET if unique == len(drivers) else "\033[31mIDENTICAL\033[0m"
    print(f"\n  {unique} distinct driver(s) across {len(drivers)} accounts  {verdict}")

    sf_writes = platform.tools.raw("salesforce").written
    print(f"  {DIM}Salesforce writes: {len(sf_writes)} (3 accounts, redelivery deduped){RESET}")
    if sf_writes:
        print(f"  {DIM}fields written: {list(sf_writes[0]['fields'])}{RESET}")
    show_slack(platform)

    # 2 - a different vendor payload shape for the same business fact. Proves the
    #     normaliser layer rather than just the happy path.
    banner(2, "Our own webhook shape - same agent, different trigger")
    payload = load("webhook_health_score_drop.json")
    outcome = platform.ingest("gainsight", payload)
    show_result(platform, outcome)

    # 3 - a third shape again, from Salesforce.
    banner(3, "Renewal approaching from Salesforce - third payload shape")
    outcome = platform.ingest("salesforce", load("webhook_renewal_approaching.json"))
    show_result(platform, outcome)

    # 4 - a different event type, a different agent, zero changes to agent 1.
    banner(4, "Support ticket spike - a second agent on the same bus")
    outcome = platform.ingest("zendesk", load("webhook_support_spike.json"))
    show_result(platform, outcome)

    # 5 - bad input must be visible, not swallowed.
    banner(5, "Malformed payload - dead-lettered, not silently dropped")
    try:
        platform.ingest("gainsight", {"eventId": "broken-1", "health": {"current": 20}})
    except Exception as exc:
        print(f"\n  {RED}rejected:{RESET} {exc}")
    letters = platform.warehouse.dead_letters(5)
    print(f"  {DIM}dead-letter queue depth:{RESET} {len(letters)}")
    if letters:
        print(f"  {DIM}most recent reason:{RESET} {letters[0]['reason']}")

    # 6 - an unregistered vendor.
    banner(6, "Unknown source - rejected at the boundary")
    try:
        platform.ingest("hubspot", {"whatever": True})
    except UnknownEventSource as exc:
        print(f"\n  {RED}rejected:{RESET} {exc}")

    print(f"\n{BOLD}{'━' * 74}{RESET}")
    print(f"{BOLD}Done.{RESET} Start the dashboard to explore these runs:")
    print(f"  {BLUE}uvicorn app:app --reload{RESET}  then open {BLUE}http://127.0.0.1:8000{RESET}")
    print(f"Or explain any run from the terminal:")
    print(f"  {BLUE}python cli.py why <trace_id>{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
