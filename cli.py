"""Operator CLI.

The commands an on-call engineer actually needs at 2am, without a browser:

    python cli.py registry               what agents exist, who owns them
    python cli.py traces                 recent runs
    python cli.py why <trace_id>         plain-English explanation of one run
    python cli.py send <source> <file>   push a payload through the pipeline
    python cli.py replay <trace_id>      re-run the original event
    python cli.py golden <account_id>    inspect the golden record
    python cli.py dlq                    what failed and why
    python cli.py calibration            measured precision per driver
    python cli.py eval                   run the golden eval set
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentplatform import build_platform
from agentplatform.feedback import Calibration


def cmd_registry(platform, args) -> int:
    catalogue = platform.registry.catalogue()
    print(f"{catalogue['agent_count']} agents · {catalogue['enabled_count']} enabled · "
          f"{catalogue['review_due_count']} overdue review")
    print(f"event types routed: {', '.join(catalogue['event_types'])}\n")
    for agent in catalogue["agents"]:
        flag = "REVIEW OVERDUE" if agent["review_due"] else "ok"
        print(f"  {agent['name']:<22} v{agent['version']:<8} owner={agent['owner']:<16} "
              f"[{flag}] last reviewed {agent['last_reviewed']} ({agent['days_since_review']}d)")
        print(f"    subscribes: {', '.join(agent['subscribes_to'])}")
        print(f"    tools:      {', '.join(agent['tools'])}\n")
    return 0


def cmd_traces(platform, args) -> int:
    rows = platform.warehouse.recent_traces(args.limit)
    if not rows:
        print("No runs recorded yet. Try: python runner.py")
        return 0
    print(f"{'TRACE':<22} {'AGENT':<22} {'STEPS':>6} {'ERR':>4} {'MS':>7}  STARTED")
    for row in rows:
        print(f"{row['trace_id']:<22} {row['agent']:<22} {row['steps']:>6} "
              f"{row['errors']:>4} {row['total_ms']:>7}  {row['started_at'][:19]}")
    return 0


def cmd_why(platform, args) -> int:
    explanation = platform.observability.explain(args.trace_id)
    if not explanation["found"]:
        print(f"No trace {args.trace_id}")
        return 1

    print(f"\nTrace   {explanation['trace_id']}")
    print(f"Agent   {explanation['agent']}")
    print(f"Event   {explanation['event_id']}")
    print(f"Cost    ${explanation['total_cost_usd']:.5f}\n")

    print("What happened")
    for index, line in enumerate(explanation["narrative"], 1):
        print(f"  {index}. {line}")

    if explanation["decisions"]:
        print("\nDecisions and the rules behind them")
        for decision in explanation["decisions"]:
            print(f"  [{decision['step']}] rule={decision['rule_id']}")
            print(f"      {decision['because']}")
            print(f"      inputs: {json.dumps(decision['inputs'], default=str)}")

    if explanation["failures"]:
        print("\nFailures")
        for failure in explanation["failures"]:
            print(f"  {failure['step']}: {failure['error']}")
    print()
    return 0


def cmd_send(platform, args) -> int:
    payload = json.loads(Path(args.file).read_text())
    result = platform.ingest(args.source, payload)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_replay(platform, args) -> int:
    """Re-run the exact event a past trace was produced from.

    Replay clears the idempotency guard by design: you are asking for a fresh run
    against current code and config, which is how you verify a fix.
    """
    steps = platform.warehouse.steps_for_trace(args.trace_id)
    if not steps:
        print(f"No trace {args.trace_id}")
        return 1

    event_id = steps[0]["event_id"]
    record = platform.warehouse.get_event(event_id)
    if record is None:
        print(f"No stored event {event_id}")
        return 1

    print(f"Replaying event {event_id} (source={record['source']})")
    result = platform.bus.publish(
        __import__("agentplatform.events", fromlist=["normalise"]).normalise(
            record["source"], record["payload"]
        )
    )
    for entry in result:
        print(f"  {entry['agent']}: {entry['status']} -> {entry['trace_id']}")
    return 0


def cmd_golden(platform, args) -> int:
    record = platform.warehouse.get_golden_record(args.account_id)
    if record is None:
        print(f"No golden record for {args.account_id}")
        return 1
    print(json.dumps(record, indent=2, default=str))
    return 0


def cmd_dlq(platform, args) -> int:
    letters = platform.warehouse.dead_letters(args.limit)
    if not letters:
        print("Dead-letter queue is empty.")
        return 0
    for letter in letters:
        print(f"[{letter['ts'][:19]}] source={letter['source']} reason={letter['reason']}")
    return 0


def cmd_calibration(platform, args) -> int:
    calib = Calibration(platform.warehouse, platform.config, args.agent)
    summary = calib.summary()
    print(json.dumps(summary, indent=2))
    if calib.table():
        print(f"\n{'DRIVER':<32} {'N':>4} {'CORRECT':>8} {'PRECISION':>10}  STATUS")
        for row in calib.table():
            status = ("force review" if row["needs_review"]
                      else "trusted" if row["trusted"] else "unproven")
            print(f"{row['driver']:<32} {row['samples']:>4} {row['correct']:>8} "
                  f"{row['precision']:>10.0%}  {status}")
    return 0


def cmd_eval(platform, args) -> int:
    from tests.golden.run_eval import run_eval
    return run_eval(platform, prompt_version=args.prompt_version)


def main() -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="Agent platform operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("registry").set_defaults(func=cmd_registry)

    traces = sub.add_parser("traces")
    traces.add_argument("--limit", type=int, default=25)
    traces.set_defaults(func=cmd_traces)

    why = sub.add_parser("why")
    why.add_argument("trace_id")
    why.set_defaults(func=cmd_why)

    send = sub.add_parser("send")
    send.add_argument("source")
    send.add_argument("file")
    send.set_defaults(func=cmd_send)

    replay = sub.add_parser("replay")
    replay.add_argument("trace_id")
    replay.set_defaults(func=cmd_replay)

    golden = sub.add_parser("golden")
    golden.add_argument("account_id")
    golden.set_defaults(func=cmd_golden)

    dlq = sub.add_parser("dlq")
    dlq.add_argument("--limit", type=int, default=25)
    dlq.set_defaults(func=cmd_dlq)

    calib = sub.add_parser("calibration")
    calib.add_argument("--agent", default="renewal_risk")
    calib.set_defaults(func=cmd_calibration)

    evaluate = sub.add_parser("eval")
    evaluate.add_argument("--prompt-version", default=None)
    evaluate.set_defaults(func=cmd_eval)

    args = parser.parse_args()
    return args.func(build_platform(), args)


if __name__ == "__main__":
    sys.exit(main())
