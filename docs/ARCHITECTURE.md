# Cloud Architecture

GCP, because the Golden Record already lives in BigQuery. Keeping the agent platform in the same project means the warehouse is a native sink rather than an export job, and IAM is one story instead of two.

The local build maps 1:1 onto this. Nothing here is a redesign — `store.py` already defines the warehouse interface, and ingestion is already a single function (`Platform.ingest`) that a worker can call.

## Target architecture

```mermaid
flowchart TB
    subgraph vendors["Vendor systems"]
        SF[Salesforce]:::v
        GS[Gainsight]:::v
        ZD[Zendesk]:::v
    end

    subgraph edge["Edge"]
        LB[Cloud Load Balancer<br/>+ Cloud Armor<br/><i>WAF, rate limit, HMAC verify</i>]:::e
    end

    subgraph ingest["Ingestion — thin and fast"]
        ING[Cloud Run: ingress<br/><i>normalise, validate, dedupe</i>]:::c
    end

    subgraph bus["Event routing"]
        PS[Pub/Sub topic<br/>agent-events]:::b
        DLQ[Pub/Sub DLQ<br/><i>after 5 attempts</i>]:::d
    end

    subgraph workers["Agent execution"]
        W1[Cloud Run: renewal_risk]:::c
        W2[Cloud Run: support_escalation]:::c
        W3[Cloud Run: future agents]:::c
    end

    subgraph shared["Shared platform services"]
        REG[(Agent Registry<br/>Firestore)]:::s
        SM[Secret Manager<br/><i>vendor + OpenRouter keys</i>]:::s
        IDEM[(Memorystore Redis<br/><i>idempotency keys</i>)]:::s
    end

    subgraph llm["Model access"]
        OR[OpenRouter<br/><i>model fallback chain</i>]:::x
    end

    subgraph data["Warehouse — BigQuery"]
        BQE[(agent_events)]:::q
        BQS[(agent_run_steps)]:::q
        BQG[(golden_record_accounts)]:::q
        BQO[(outcomes)]:::q
    end

    subgraph obs["Observability"]
        LOG[Cloud Logging<br/><i>structured JSON</i>]:::o
        MON[Cloud Monitoring<br/><i>SLOs + alerts</i>]:::o
        LK[Looker Studio<br/><i>agent + learning dashboards</i>]:::o
    end

    SF & GS & ZD -->|webhook| LB --> ING
    ING -->|validated event| PS
    ING -.->|rejected| DLQ
    PS --> W1 & W2 & W3
    PS -.->|exhausted retries| DLQ
    W1 & W2 --> REG & SM & IDEM
    W1 -->|analysis| OR
    W1 & W2 -->|writes back| SF & GS
    W1 & W2 -->|Slack alert| SLACK[Slack]:::x
    W1 & W2 -->|trace rows| BQE & BQS & BQG
    LK -->|human verdicts| BQO
    BQO -->|calibration read at analysis time| W1
    W1 & W2 --> LOG --> MON
    BQS --> LK

    classDef v fill:#e8f0fe,stroke:#4285f4,color:#111
    classDef e fill:#fce8e6,stroke:#ea4335,color:#111
    classDef c fill:#e6f4ea,stroke:#34a853,color:#111
    classDef b fill:#fef7e0,stroke:#fbbc04,color:#111
    classDef d fill:#fce8e6,stroke:#c5221f,color:#111
    classDef s fill:#f3e8fd,stroke:#a142f4,color:#111
    classDef q fill:#e0f7fa,stroke:#00acc1,color:#111
    classDef o fill:#eceff1,stroke:#546e7a,color:#111
    classDef x fill:#fff,stroke:#999,color:#111
```

## Why each choice

| Service | Why this, not the alternative |
|---|---|
| **Cloud Run** | Request-driven and scales to zero. Agent load is spiky (health scores recalc nightly, then nothing). GKE would mean paying for idle capacity and running a cluster nobody on a small platform team wants to own. |
| **Pub/Sub** | The decoupling the JD describes. Publishers never learn who subscribes; onboarding an agent is a subscription plus a registry entry. Gives retries, ordering keys per account, and a native DLQ. |
| **Cloud Run ingress split from workers** | Vendors time out webhooks in seconds; LLM analysis takes tens of seconds. Separating them means a slow model can never cause a vendor to mark our endpoint unhealthy and start dropping events. |
| **BigQuery** | Already the Golden Record home. Traces land where analysts can join agent behaviour to revenue data — "did accounts we alerted on actually renew?" becomes SQL, not an integration project. |
| **Firestore for the registry** | Low-latency point reads on every event, and the registry is read constantly, written rarely. BigQuery is the wrong shape for that. |
| **Memorystore for idempotency** | Dedupe must be fast and atomic. A BigQuery lookup per event is neither. |
| **Secret Manager** | Vendor keys and the OpenRouter key rotate independently of deploys, with IAM audit trails. |
| **Looker Studio** | Non-engineers need the dashboard. Keeping it in GCP means no extra auth surface and no data leaving the project. |

## BigQuery layout

```mermaid
erDiagram
    agent_events ||--o{ agent_run_steps : "event_id"
    agent_run_steps ||--o| outcomes : "trace_id"
    agent_events }o--|| golden_record_accounts : "account_id"

    agent_events {
        string event_id PK
        string event_type
        string source
        string account_id
        timestamp occurred_at
        json payload
    }
    agent_run_steps {
        string trace_id
        string agent
        int seq
        string step
        string status
        int latency_ms
        json detail
    }
    golden_record_accounts {
        string account_id PK
        string renewal_risk_driver
        string renewal_risk_severity
        float confidence
        string updated_by
        string trace_id
        int revision
    }
    outcomes {
        string trace_id PK
        string driver
        string verdict
        string reviewer
    }
```

- `agent_events`, `agent_run_steps`, `outcomes`: **append-only**, streaming inserts, partitioned by ingestion date, clustered on `(agent, account_id)`. Append-only means the audit trail cannot be quietly rewritten.
- `golden_record_accounts`: `MERGE` on `account_id`, updating only the columns this platform has authority over. Every row carries `updated_by` and `trace_id`, so any value traces back to the run that produced it.
- Partition expiry on the trace tables (12 months) keeps cost bounded.

## Operating it

**Scaling.** Cloud Run min-instances 0 for workers, 1 for ingress (avoid cold-start on vendor webhooks). Pub/Sub ordering key = `account_id`, so two events for the same account can't race each other into the Golden Record. Concurrency capped per worker so a burst can't blow the OpenRouter rate limit.

**Cost.** Token spend is logged per run, so cost is queryable per agent, per driver, per account. A daily budget in config, plus a Cloud Billing alert. Severity band picks the model: low-severity accounts get the cheap model, exec-escalation accounts get the strong one.

**Security.** HMAC signature verification at Cloud Armor. Per-agent service accounts, so an agent's IAM grants match its registry `tools:` list — least privilege enforced twice. No secrets in traces (`_redact` in `clients/base.py`). Slack messages carry a trace link, never raw customer PII beyond what the alert needs.

**SLOs and alerts.** Ingest→alert p95 latency; grounding-verification failure rate; DLQ depth > 0; agents overdue for review; daily LLM spend vs budget; deterministic-fallback rate (a spike means the model or a vendor is degraded).

**CI/CD.** Cloud Build on merge: tests → golden eval → deploy to staging → smoke → canary 10% via Cloud Run traffic splitting → full. Prompt changes go through the same gate as code, because they are code.
