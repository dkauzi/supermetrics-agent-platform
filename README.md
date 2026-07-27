# Renewal Risk Analyser and Router

[![CI](https://github.com/dkauzi/supermetrics-agent-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/dkauzi/supermetrics-agent-platform/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-91%20passing-brightgreen)](tests/test_platform.py)
[![eval gate](https://img.shields.io/badge/golden%20eval-gated%20in%20CI-blue)](tests/golden/run_eval.py)

**[Overview and demo →](https://dkauzi.github.io/supermetrics-agent-platform/)** · [Architecture](docs/ARCHITECTURE.md) · [Engineering notes](docs/ENGINEERING_NOTES.md)

**In plain terms.** A customer's health score drops and their renewal is near. This watches for that, gathers the facts from the CRM and support tools, works out the most likely reason they might leave, records it, and alerts the right person in Slack with the numbers behind the call. Every run is logged, so anyone can open the dashboard, click a run, and read *why it did that* in plain sentences: no code, no log files. It never blindly trusts the model - every figure the AI quotes is checked against the real data first, and when it is not confident it holds the write and asks a person instead of guessing.

Built as the first agent on a shared platform rather than a standalone script: the brief describes one agent, the role owns the layer many agents plug into. Three agents run on it today.

`runner.py` executes the supplied `renewal_risk_router_sample_payload.json` as given: three accounts with near-identical triggers resolve to **three distinct correct drivers**, and the redelivered `evt_1001` is deduped with no second CRM write and no second alert.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional: add OPENROUTER_API_KEY
.venv/bin/python runner.py    # runs YOUR sample payload first, then the failure paths
.venv/bin/uvicorn app:app     # dashboard on http://127.0.0.1:8000
```

That payload caught a real defect: the prompt first gave two different accounts the same driver, which is exactly what the file is built to expose. The fix, and the discriminator behind it, are in the [overview](https://dkauzi.github.io/supermetrics-agent-platform/).

Onboarding a new agent is one command — `cli.py new-agent` writes the module and registry entry, and refuses a missing owner, an unknown tool or a malformed event name.

**Failure modes.** Redelivered webhooks dedupe on `event_id`; every write is idempotent. Malformed input is dead-lettered with a reason, never half-written. Invalid LLM JSON gets one repair round-trip, then the next model in the chain. Evidence the model cites is verified against the data we actually fetched, and ungrounded analysis is discarded rather than softened. If the model is down or over budget a deterministic analyser takes over, the run is marked degraded, and the human is still alerted. Vendor 5xx retries with backoff, 4xx does not, and a circuit breaker stops us amplifying an outage. When confidence is low, CRM writes are held and a person is asked in Slack instead.

**Config vs hardcoded.** Config: model chain, prompt version, thresholds, severity bands, routing rules and channels, spend limits, warehouse choice, per-vendor retry and circuit-breaker policy, and the whole agent registry (owner, subscriptions, tool grants, review cadence). Hardcoded deliberately: the pipeline shape, the output schema and the driver taxonomy, because those are contracts. As config, changing one would silently invalidate every recorded eval result.

**Debugging live.** `GET /traces/{id}/why` returns a run in plain English *and* as rules-and-values, rendered from one trace so they cannot disagree. `/cost`, `/quality`, `/tools` and `/audit` cover spend, eval gate, circuit state and the platform self-audit. The dashboard also carries a decision log, a per-agent review schedule that can nudge the owner in Slack, and a one-command "add an agent" panel. `cli.py replay <trace_id>` re-runs the original event against current code. Skipped runs are traced with their reason, so "why did nothing happen?" is as answerable as "why did this happen?".

**How it's tested.** `pytest -q` runs 91 tests, deliberately weighted to the failure paths: dedupe on redelivery, degraded fallback when the model is down, grounding rejection of invented figures, circuit breaking, idempotent writes, and the platform's own self-audit. A golden eval set gates prompt changes, and because the model is non-deterministic on ambiguous inputs it samples each case several times and gates on consistency, not a single lucky run. All green in CI on Python 3.11/3.12/3.13, and the dashboard shows the live count and what each area covers.

**Proving the model actually ran.** The dashboard header shows LIVE or OFFLINE, and the cost panel names the exact OpenRouter model used, the tokens spent and the real dollar cost. Offline, nothing leaves the process and cost stays $0, which is itself the evidence of which path ran. Retries are bounded and the daily budget caps spend, so nothing loops forever; when the platform still cannot be confident, the decision fails to a human rather than guessing.

**First change at 10x.** Make ingestion asynchronous: validate, persist and enqueue, with workers consuming. Today the webhook processes inline, which is fine at this volume and wrong at ten times it. That is the failure that bites first, before cost or storage.

<sub>Mocked vendor clients; the BigQuery adapter's SQL is tested but has never run against a live dataset.</sub>
