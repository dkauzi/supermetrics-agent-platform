# Renewal Risk Analyser and Router

[![CI](https://github.com/dkauzi/supermetrics-agent-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/dkauzi/supermetrics-agent-platform/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-67%20passing-brightgreen)](tests/test_platform.py)
[![eval gate](https://img.shields.io/badge/golden%20eval-gated%20in%20CI-blue)](tests/golden/run_eval.py)
[![python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue)](.github/workflows/ci.yml)

**[See the dashboard →](https://dkauzi.github.io/supermetrics-agent-platform/)**

---

## In plain English

A customer's health score drops and their renewal is coming up. Someone should look at them, but nobody has time to watch every account.

This is a small piece of software that watches for you. When a warning arrives, it:

1. **Gathers the facts** about that customer from Salesforce, Gainsight, HubSpot and Zendesk.
2. **Works out the likely reason** they might leave, using AI: are they not using it? Did their main contact leave? Have they had a bad run with support?
3. **Writes what it found** into Salesforce and Gainsight so it isn't lost.
4. **Tells the right person** in Slack, with the actual numbers behind the conclusion.
5. **Records every step**, so anyone can ask *"why did it do that?"* and get a readable answer in seconds.

That last point is the one that matters most. Open the dashboard, click any run, and you get something like:

> **Halcyon Analytics: high churn risk, because the person who championed us internally has moved on**
>
> 1. A message arrived from Salesforce saying this customer's renewal date is coming up.
> 2. The agent looked up this customer across our systems and gathered 33 pieces of information about them.
> 3. It judged the most likely reason this customer might leave is: the person who championed us internally has moved on. It was 55% sure.
> 4. It rated this a high priority, because their health score is 52, they are worth $121,000 a year, and they renew in 30 days.
> 5. It posted an alert to #cs-renewals for @ines, with the numbers that led to this conclusion.

No code, no log files, no engineer required.

**Two things it will not do.** It won't blindly trust the AI: every number the AI quotes is checked against the real data first, and if it invents one, its answer is thrown away and a safer rules-based answer used instead. And it won't write something into your CRM that it isn't confident about: it holds the write and asks a person in Slack instead.

---

## For engineers

Built as the **first agent on a shared platform**, not a standalone script, because the brief describes one agent but the role is owning the layer many agents plug into.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # add OPENROUTER_API_KEY (runs without one, in offline mode)
.venv/bin/python runner.py    # 6 scenarios end-to-end, including the failure paths
.venv/bin/uvicorn app:app     # dashboard on http://127.0.0.1:8000
```

**Flow:** webhook → normalise → dedupe → event bus → *(registry decides who subscribes)* → agent: fetch context → minimise PII → LLM analysis → schema validation → grounding verification → severity → human gate → write Salesforce + Gainsight + Golden Record → route → Slack. Every step writes a trace row.

**Three agents on one bus:** `renewal_risk` (the brief), `support_escalation` (proves onboarding an agent touches no existing agent), `platform_qa` (audits the platform itself, deliberately with no LLM since every check has a correct answer).

### Failure modes I designed for

| Failure | Behaviour |
|---|---|
| Vendor redelivers a webhook | Dedupe on `event_id`; idempotency keys on every write. One event, one Salesforce task. |
| Malformed / unknown payload | Rejected at the boundary and dead-lettered **with a reason**. No partial writes. |
| LLM returns invalid JSON | One repair round-trip with the validation error fed back, then the next model in the chain. |
| LLM cites numbers that don't exist | Every claim is checked against retrieved facts. Ungrounded analysis is **discarded**, not softened. |
| LLM unavailable or over budget | Deterministic rules-based analysis takes over. Run marked `degraded`, **the human is still alerted.** |
| Model deprecated by vendor | Fallback chain in config. No code change. |
| Vendor 5xx or timeout | Retry with backoff; 4xx is *not* retried. Failed alerts go to the DLQ for replay. |
| A vendor goes down entirely | Circuit breaker opens and stops calling it, so retries don't amplify their outage. |
| A supporting vendor is down (HubSpot) | Analysis proceeds with fewer facts. A nice-to-have never blocks a churn alert. |
| Account owner can't be resolved | Falls back to a monitoring channel. An alert is never silently dropped. |
| Agent raises | Isolated. Other agents on the same event still run. |
| A flapping trigger loops on one account | Per-account hourly cap on model calls; the run continues deterministically and escalates. |
| Daily budget runs out | Spending stops at a soft ceiling (90%) so runs degrade predictably, not mid-analysis. |
| We don't trust the prediction | CRM writes are **held** and a human is asked. Golden record marks it `awaiting_approval`. |
| Two agents write one account at once | Optimistic concurrency on the golden record; the stale write is rejected and retried, not lost. |
| Model is confidently wrong over time | Human verdicts feed a calibration table; low-precision drivers are auto-flagged for review. |

### Config vs hardcoded

**Config** (`config/*.yaml`, env): model chain, prompt version, all thresholds, severity bands, routing rules and channels, spend limits, privacy toggle, warehouse choice, eval gates, the entire agent registry (name, owner, subscriptions, tool grants, review cadence), and the **per-vendor reliability policy chain**.

That last one is worth calling out. Tracing, idempotency, circuit breaking, rate limiting and retry are composable adapters wrapped around a vendor transport and assembled from config, not inherited from a base class. Salesforce gets pacing and 4 attempts while Slack fails fast with no breaker, without a subclass per combination. `GET /tools` shows the live chain and circuit state.

**Hardcoded** (deliberately): the pipeline *shape*, the output schema, and the driver taxonomy. These are contracts. If they were config, changing one would silently invalidate every recorded eval result and break the learning loop's ability to compare like with like.

My test before shipping: *if a vendor changes something or a number moves, do I edit one place or ten?*

### Debugging this live in production

1. `GET /traces/{id}/why` returns two views of one run: `plain` for whoever is asking, and the rule-and-values version for whoever is fixing it. They come from the same trace, so they cannot disagree.
2. `GET /accounts/{id}/audit` gives every decision ever made about that account, plus human verdicts.
3. `GET /cost` shows spend against budget and which runs were throttled to a human.
4. `GET /quality` shows the eval gate, ungrounded citations rejected, and fallback rate.
5. `python cli.py replay <trace_id>` re-runs the original event against current code to verify a fix.
6. `python cli.py audit` runs the `platform_qa` agent against the platform's own contract. Non-zero exit on critical, so it doubles as a CI gate.

Skipped runs are traced with their reason, so *"why did nothing happen?"* is as answerable as *"why did this happen?"*

### What I'd change first at 10x

**First: make ingestion asynchronous.** Today the webhook processes inline. Fine at this volume, wrong at 10x. I'd have the endpoint validate, persist and enqueue (Pub/Sub) with workers consuming, decoupling vendor webhook timeouts from LLM latency. That is the failure that would bite first.

Then, in order: SQLite → BigQuery (the implementation exists, `warehouse_bigquery.py`); cache account context to stop re-fetching the same Salesforce record per event; route low-severity accounts to a cheaper model; per-account rate limiting beyond the current hourly cap.

### Deep dives

- [docs/PRESENTATION.md](docs/PRESENTATION.md) - the 20 minute walkthrough
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - cloud deployment proposal + diagrams
- [docs/RUNBOOK.md](docs/RUNBOOK.md) - prompt rollback, on-call procedures
- [docs/LIVE_MODIFICATION.md](docs/LIVE_MODIFICATION.md) - how to extend it, worked examples
- [docs/AI_BUILD_LOG.md](docs/AI_BUILD_LOG.md) - how I drove the coding agent

### Honest limitations

- `warehouse_bigquery.py` is a complete implementation whose **SQL is asserted in tests but has never run against a live BigQuery dataset** (no GCP credentials for this exercise).
- Vendor clients are mocks over a fixture dataset. They honour the real interface, retry semantics and idempotency, but nothing crosses the network except the LLM call.
- Pseudonymisation before the LLM call is data *minimisation*, not GDPR anonymisation: the mapping lives in memory for the run, and metrics could in principle be linkable.

`pytest -q` → 67 tests, weighted to failure paths. `python cli.py eval` → golden eval gate.
