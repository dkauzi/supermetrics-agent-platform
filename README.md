# Renewal Risk Analyser and Router

[![CI](https://github.com/dkauzi/supermetrics-agent-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/dkauzi/supermetrics-agent-platform/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-91%20passing-brightgreen)](tests/test_platform.py)
[![eval gate](https://img.shields.io/badge/golden%20eval-gated%20in%20CI-blue)](tests/golden/run_eval.py)

**[Overview and demo →](https://dkauzi.github.io/supermetrics-agent-platform/)** · [Architecture](docs/ARCHITECTURE.md) · [Engineering notes](docs/ENGINEERING_NOTES.md)

A customer's health score drops as their renewal nears. This works out why they might leave, writes the finding to Salesforce and Gainsight, alerts the account owner in Slack with the evidence, and logs every step so anyone can ask *"why did it do that?"* and get a plain-English answer in seconds. Built as the first agent on a shared platform, not a standalone script: the brief is one agent, the role owns the layer many agents plug into. Three agents run on it today.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional: add OPENROUTER_API_KEY
.venv/bin/python runner.py    # runs the supplied sample payload, then the failure paths
.venv/bin/uvicorn app:app     # dashboard on http://127.0.0.1:8000
```

`runner.py` runs the supplied payload as given: three near-identical triggers resolve to **three distinct correct drivers**, and a redelivered event is deduped with no second write or alert. That payload caught a real defect: two accounts first got the same driver, exactly what the file is built to expose. The fix is in the [overview](https://dkauzi.github.io/supermetrics-agent-platform/) and [engineering notes](docs/ENGINEERING_NOTES.md).

**Failure modes.** Redelivered webhooks dedupe on `event_id`; every write is idempotent. Malformed input is dead-lettered with a reason, never half-written. Invalid model JSON gets one repair round-trip, then the next model in the chain. Cited evidence is verified against the data actually fetched, and ungrounded analysis is discarded rather than softened. If the model is down or over budget a deterministic analyser takes over, the run is marked degraded, and the human is still alerted. Vendor 5xx retries with backoff, 4xx does not, and a circuit breaker stops us amplifying an outage. When confidence is low, CRM writes are held and a person is asked in Slack.

**Config vs hardcoded.** Config: model chain, prompt version, thresholds, severity bands, routing rules and channels, spend limits, warehouse choice, per-vendor retry and circuit-breaker policy, and the whole agent registry (owner, subscriptions, tool grants, review cadence). Hardcoded deliberately: the pipeline shape, the output schema and the driver taxonomy, because those are contracts. As config, changing one would silently invalidate every recorded eval result.

**Debugging live.** `GET /traces/{id}/why` returns a run in plain English *and* as rules-and-values from one trace, so they cannot disagree. `/cost`, `/quality`, `/tools` and `/audit` cover spend, the eval gate, circuit state and the platform self-audit; `cli.py replay <trace_id>` re-runs an event against current code. Skipped runs are traced with their reason, so "why did nothing happen?" is as answerable as "why did this happen?". Fuller tour, including the dashboard panels, in the [engineering notes](docs/ENGINEERING_NOTES.md).

**First change at 10x.** Make ingestion asynchronous: validate, persist and enqueue, with workers consuming. Today the webhook processes inline, which is fine at this volume and wrong at ten times it. That is the failure that bites first, before cost or storage.

<sub>91 tests weighted to failure paths, plus a golden eval gate, green in CI. Mocked vendor clients; the BigQuery adapter's SQL is tested but has never run against a live dataset.</sub>
