# Cloud Architecture

GCP, because the Golden Record already lives in BigQuery. Keeping the agent platform in the same project means the warehouse is a native sink rather than an export job, and IAM is one story instead of two.

Region **europe-north1 (Hamina)**: customer data stays in the EU, and for a Helsinki-based company it is also the closest region. Data residency is an architecture decision, not a checkbox added later.

The local build maps 1:1 onto this. Nothing here is a redesign - `store.py` already defines the warehouse interface, and ingestion is already a single function (`Platform.ingest`) that a worker can call.

## Target architecture

Rendered from [`diagrams/cloud-architecture.mmd`](diagrams/cloud-architecture.mmd) with `./scripts/render_diagrams.sh`. The `.mmd` is the source; this image is committed so nothing needs a toolchain to view it.

![Cloud architecture](diagrams/cloud-architecture.svg)

[Full-size PNG](diagrams/cloud-architecture.png) for slides.

## Why each choice

| Service | Why this, not the alternative |
|---|---|
| **Cloud Run** | Request-driven and scales to zero. Agent load is spiky (health scores recalc nightly, then nothing). GKE would mean paying for idle capacity and running a cluster nobody on a small platform team wants to own. |
| **Pub/Sub** | The decoupling the JD describes. Publishers never learn who subscribes; onboarding an agent is a subscription plus a registry entry. Gives retries, ordering keys per account, and a native DLQ. |
| **Cloud Run ingress split from workers** | Vendors time out webhooks in seconds; LLM analysis takes tens of seconds. Separating them means a slow model can never cause a vendor to mark our endpoint unhealthy and start dropping events. |
| **BigQuery** | Already the Golden Record home. Traces land where analysts can join agent behaviour to revenue data - "did accounts we alerted on actually renew?" becomes SQL, not an integration project. |
| **Firestore for the registry** | Low-latency point reads on every event, and the registry is read constantly, written rarely. BigQuery is the wrong shape for that. |
| **Memorystore for idempotency** | Dedupe must be fast and atomic. A BigQuery lookup per event is neither. |
| **Secret Manager** | Vendor keys and the OpenRouter key rotate independently of deploys, with IAM audit trails. |
| **One shared connector layer** | Salesforce, Gainsight, HubSpot, Zendesk and Slack are all reached through the same client interface, with retry, idempotency, rate limiting and circuit breaking as composable policies configured per vendor. A vendor API change is one adapter, and every agent inherits the fix. This is what stops five agents growing five slightly different Salesforce clients. |
| **Looker Studio** | Non-engineers need the dashboard. Keeping it in GCP means no extra auth surface and no data leaving the project. |
| **Cloud Trace, *and* BigQuery traces** | Two systems because they answer two questions. Cloud Trace ingests the OTel spans and answers "where did the time go"; the `agent_run_steps` table answers "why did this agent conclude that". Using Cloud Trace for the second job fails: spans are sampled, so you lose the one run someone asks about, and they expire. Each carries the other's ids. |
| **OpenRouter, not a direct model vendor** | One integration for many models, so a deprecation or an outage is a config edit to the fallback chain. The trade-off is a third party in the data path, which is why identifiers are stripped before the call. |

## BigQuery layout

![BigQuery schema](diagrams/bigquery-schema.svg)

Source: [`diagrams/bigquery-schema.mmd`](diagrams/bigquery-schema.mmd).

- `agent_events`, `agent_run_steps`, `outcomes`: **append-only**, streaming inserts, partitioned by ingestion date, clustered on `(agent, account_id)`. Append-only means the audit trail cannot be quietly rewritten.
- `golden_record_accounts`: `MERGE` on `account_id`, updating only the columns this platform has authority over. Every row carries `updated_by` and `trace_id`, so any value traces back to the run that produced it.
- Partition expiry on the trace tables (12 months) keeps cost bounded.

## Operating it

**Scaling.** Cloud Run min-instances 0 for workers, 1 for ingress (avoid cold-start on vendor webhooks). Pub/Sub ordering key = `account_id`, so two events for the same account can't race each other into the Golden Record. Concurrency capped per worker so a burst can't blow the OpenRouter rate limit.

**Cost.** Token spend is logged per run, so cost is queryable per agent, per driver, per account. A daily budget in config, plus a Cloud Billing alert. Severity band picks the model: low-severity accounts get the cheap model, exec-escalation accounts get the strong one.

**Security.** HMAC signature verification at Cloud Armor. Per-agent service accounts, so an agent's IAM grants match its registry `tools:` list - least privilege enforced twice. No secrets in traces (`_redact` in `clients/policies.py`). Slack messages carry a trace link, never raw customer PII beyond what the alert needs.

**Data protection.** The one place customer data leaves the project is the model call, so that is where minimisation happens: `privacy.py` swaps account and person identifiers for per-run tokens before the request and restores them in the output a human reads. The model gets the metrics, which is all it needs to identify a churn driver, and analysis quality is unaffected. Everything else stays in europe-north1. This is minimisation rather than anonymisation - the mapping exists in memory for the run - and it is the applicable principle for a processing step that does not need the identity to do its job. At 10x I would additionally pin OpenRouter to EU-hosted models and hold a DPA, or move the analysis step to Vertex AI in-region and drop the third party from the data path entirely.

**SLOs and alerts.** Ingest→alert p95 latency (from Cloud Trace); grounding-verification failure rate; DLQ depth > 0; agents overdue for review; daily LLM spend vs budget; deterministic-fallback rate (a spike means the model or a vendor is degraded); writes held for human approval (a spike means calibration has lost confidence in a driver, which is a signal worth waking up for).

**CI/CD.** Cloud Build on merge: tests → golden eval → deploy to staging → smoke → canary 10% via Cloud Run traffic splitting → full. Prompt changes go through the same gate as code, because they are code.
