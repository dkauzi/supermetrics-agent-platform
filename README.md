# Renewal Risk Analyser and Router

[![CI](https://github.com/dkauzi/supermetrics-agent-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/dkauzi/supermetrics-agent-platform/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-81%20passing-brightgreen)](tests/test_platform.py)
[![eval gate](https://img.shields.io/badge/golden%20eval-gated%20in%20CI-blue)](tests/golden/run_eval.py)

**[Overview and demo →](https://dkauzi.github.io/supermetrics-agent-platform/)** · [Architecture](docs/ARCHITECTURE.md) · [Engineering notes](docs/ENGINEERING_NOTES.md)

Built as the first agent on a shared platform rather than a standalone script: the brief describes one agent, the role owns the layer many agents plug into. Three agents run on it today.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional: add OPENROUTER_API_KEY
.venv/bin/python runner.py    # 6 scenarios, including the failure paths
.venv/bin/uvicorn app:app     # dashboard on http://127.0.0.1:8000
```

**Failure modes.** Redelivered webhooks dedupe on `event_id`; every write is idempotent. Malformed input is dead-lettered with a reason, never half-written. Invalid LLM JSON gets one repair round-trip, then the next model in the chain. Evidence the model cites is verified against the data we actually fetched, and ungrounded analysis is discarded rather than softened. If the model is down or over budget a deterministic analyser takes over, the run is marked degraded, and the human is still alerted. Vendor 5xx retries with backoff, 4xx does not, and a circuit breaker stops us amplifying an outage. When confidence is low, CRM writes are held and a person is asked in Slack instead.

**Config vs hardcoded.** Config: model chain, prompt version, thresholds, severity bands, routing rules and channels, spend limits, warehouse choice, per-vendor retry and circuit-breaker policy, and the whole agent registry (owner, subscriptions, tool grants, review cadence). Hardcoded deliberately: the pipeline shape, the output schema and the driver taxonomy, because those are contracts. As config, changing one would silently invalidate every recorded eval result.

**Debugging live.** `GET /traces/{id}/why` returns a run in plain English *and* as rules-and-values, rendered from one trace so they cannot disagree. `/cost`, `/quality` and `/tools` cover spend, eval gate and circuit state. `cli.py replay <trace_id>` re-runs the original event against current code. Skipped runs are traced with their reason, so "why did nothing happen?" is as answerable as "why did this happen?".

**First change at 10x.** Make ingestion asynchronous: validate, persist and enqueue, with workers consuming. Today the webhook processes inline, which is fine at this volume and wrong at ten times it. That is the failure that bites first, before cost or storage.

<sub>Mocked vendor clients; the BigQuery adapter's SQL is tested but has never run against a live dataset.</sub>
