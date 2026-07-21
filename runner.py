"""End-to-end demo driver.

`python runner.py` walks the platform through five scenarios in-process - no HTTP,
no network unless the LLM is live - and prints the reasoning for each. This is the
script to run in a live session: it exercises the happy path *and* the failure
paths, because a demo that only shows the happy path proves nothing.

Scenarios:
  1. Critical renewal risk        full pipeline, LLM analysis, exec escalation
  2. Redelivery of the same event idempotency - no duplicate writes
  3. Support ticket spike         a second agent on the same bus, untouched code
  4. Malformed payload            dead-lettered with a reason, no partial writes
  5. Unknown source               rejected at the boundary
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

    # 1 - the main scenario from the brief.
    banner(1, "Renewal approaching + health score dropped (ACC-4417, $248k ARR)")
    payload = load("webhook_health_score_drop.json")
    outcome = platform.ingest("gainsight", payload)
    show_result(platform, outcome)
    show_slack(platform)

    # 2 - the same webhook again. Vendors redeliver; the platform must not double-act.
    banner(2, "Same webhook redelivered - idempotency")
    repeat = platform.ingest("gainsight", payload)
    print(f"\n  {DIM}ingest status:{RESET} {BOLD}{repeat['status']}{RESET}")
    print(f"  {DIM}reused trace(s):{RESET} {repeat['trace_ids']}")
    sf_tasks = platform.tools.raw("salesforce").written
    print(f"  {GREEN}Salesforce tasks created across both deliveries: "
          f"{len(sf_tasks)}{RESET} {DIM}(expected 1){RESET}")

    # 3 - a different event type, a different agent, zero changes to agent 1.
    banner(3, "Support ticket spike - a second agent on the same bus")
    outcome = platform.ingest("zendesk", load("webhook_support_spike.json"))
    show_result(platform, outcome)

    # 4 - bad input must be visible, not swallowed.
    banner(4, "Malformed payload - dead-lettered, not silently dropped")
    try:
        platform.ingest("gainsight", {"eventId": "broken-1", "health": {"current": 20}})
    except Exception as exc:
        print(f"\n  {RED}rejected:{RESET} {exc}")
    letters = platform.warehouse.dead_letters(5)
    print(f"  {DIM}dead-letter queue depth:{RESET} {len(letters)}")
    if letters:
        print(f"  {DIM}most recent reason:{RESET} {letters[0]['reason']}")

    # 5 - an unregistered vendor.
    banner(5, "Unknown source - rejected at the boundary")
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
