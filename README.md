# Renewal Risk Analyser and Router

A renewal-risk agent, built as the **first agent on a shared platform** rather than a standalone script — because the brief describes one agent, but the role is owning the layer many agents plug into.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # add OPENROUTER_API_KEY (runs without one, in offline mode)
.venv/bin/python runner.py    # 5 scenarios end-to-end, including the failure paths
.venv/bin/uvicorn app:app     # dashboard on http://127.0.0.1:8000
```

**Flow:** webhook → normalise → dedupe → event bus → *(registry decides who subscribes)* → agent: fetch context → LLM analysis → schema validation → grounding verification → severity → write Salesforce + Gainsight + Golden Record → route → Slack. Every step writes a trace row.

## Failure modes I designed for

| Failure | Behaviour |
|---|---|
| Vendor redelivers a webhook | Dedupe on `event_id`; idempotency keys on every write. One event, one Salesforce task. |
| Malformed / unknown payload | Rejected at the boundary and dead-lettered **with a reason**. No partial writes. |
| LLM returns invalid JSON | One repair round-trip with the validation error fed back, then the next model in the chain. |
| LLM cites numbers that don't exist | `verifier.py` checks every claim against retrieved facts. Ungrounded analysis is **discarded**, not softened. |
| LLM entirely unavailable / over budget | Deterministic rules-based analysis takes over. Run marked `degraded`, **the human is still alerted.** |
| Model deprecated by vendor | Fallback chain in config. No code change. |
| Salesforce/Slack 5xx or timeout | Retry with backoff; 4xx is *not* retried. Failed alerts go to the DLQ for replay. |
| Account owner can't be resolved | Falls back to a monitoring channel. An alert is never silently dropped. |
| Agent raises | Isolated — other agents on the same event still run. |
| Model is confidently wrong over time | Human verdicts feed a calibration table; low-precision drivers are auto-flagged for review. |

## Config vs hardcoded

**Config** (`config/*.yaml`, env): model chain, prompt version, all thresholds, severity bands, routing rules and channels, retry/timeout policy, warehouse choice, eval gates, and the entire agent registry (name, owner, subscriptions, tool grants, review cadence).

**Hardcoded** (deliberately): the pipeline *shape*, the output schema, and the driver taxonomy. These are contracts — if they were config, changing one would silently invalidate every recorded eval result and break the learning loop's ability to compare like with like.

My test before shipping: *if a vendor changes something or a number moves, do I edit one place or ten?*

## Debugging this live in production

1. `GET /traces/{id}/why` — plain-English narrative naming **the rule that fired and the values it matched on**. Built for a CS lead, not an engineer.
2. `GET /accounts/{id}/audit` — every decision ever made about that account, plus human verdicts.
3. `GET /quality` — what the guardrails caught: eval-gate status, grounding rejections, fallback rate.
4. `GET /dead-letters` — anything that didn't process. Should be empty.
5. `python cli.py replay <trace_id>` — re-run the original event against current code to verify a fix.
6. `python cli.py audit` — the `platform_qa` agent checks the platform against its own contract (every agent owned and reviewed, DLQ empty, eval gate green, fallback rate sane, no driver below its precision floor). Non-zero exit on critical, so it doubles as a CI gate.

Skipped runs are logged with their reason too, so *"why did nothing happen?"* is as answerable as *"why did this happen?"*

## What I'd change first at 10x

**First: make ingestion asynchronous.** Today the webhook processes inline — fine at this volume, wrong at 10x. I'd have the endpoint validate, persist and enqueue (Pub/Sub), with workers consuming. That decouples vendor webhook timeouts from LLM latency, which is the failure that would bite first.

Then, in order: SQLite → BigQuery (interface already exists, `store.py`); cache account context to stop re-fetching the same Salesforce record per event; batch/downgrade the model for low-severity accounts and enforce the daily cost budget; add per-account rate limiting so one flapping health score can't fire fifty alerts.

## Deep dives

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — cloud deployment proposal + diagrams
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — prompt rollback, on-call procedures
- [docs/AI_BUILD_LOG.md](docs/AI_BUILD_LOG.md) — how I drove the coding agent

**Three agents on one bus:** `renewal_risk` (the brief), `support_escalation` (proves onboarding an agent touches no existing agent), `platform_qa` (audits the platform itself — deliberately no LLM, since every check has a correct answer).

`pytest -q` → 32 tests, weighted to failure paths. `python cli.py eval` → golden eval gate.
