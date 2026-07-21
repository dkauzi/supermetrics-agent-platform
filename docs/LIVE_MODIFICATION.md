# Extending this live

The brief says "be prepared to explain and modify your own code during the live conversation". These are the changes most likely to be asked for, each rehearsed, each with the exact edit and the command that proves it worked.

The point of every one: **the change is small because the platform absorbed the complexity.** Say that out loud each time.

---

## 1. "Change who gets alerted" (30 seconds, no code)

Business asks: mid-market critical accounts should go to a different channel.

`config/platform.yaml` → `agents.renewal_risk.routing.rules`, add above `critical_standard`:

```yaml
- id: critical_midmarket
  when: { severity: [critical], segment: [mid-market] }
  channel: "#cs-midmarket"
  notify: [account_owner]
  mention: true
```

Prove it:
```bash
.venv/bin/python cli.py send gainsight samples/webhook_health_score_drop.json
.venv/bin/python cli.py why <trace_id>      # shows rule=critical_midmarket and why it matched
```

> "Routing is a business rule, so it lives in config and the matched rule id goes into the trace. A CS lead can change who gets paged without me."

---

## 2. "A model got deprecated" (20 seconds, no code)

`config/platform.yaml` → delete the first line of `llm.model_chain`.

```bash
curl -s localhost:8000/healthz | jq .llm_model_chain
```

> "Vendor model changes are a config edit. The agent never names a model."

---

## 3. "Make it stricter about weak evidence" (1 minute)

`config/platform.yaml`:
```yaml
verification:
  min_evidence_items: 3        # was 2
agents:
  renewal_risk:
    min_confidence: 0.7        # was 0.55
```

```bash
.venv/bin/python cli.py send gainsight samples/webhook_health_score_drop.json
```

Show the run now flagged for human review, CRM writes held, approval requested in Slack.

> "That's the dial between autonomy and caution, and it's one number."

---

## 4. "Add a case to the eval set" (2 minutes)

Append to `tests/golden/cases.json`:

```json
{
  "id": "pricing_pressure_case",
  "expected_driver": "pricing_pressure",
  "must_cite_any": ["arr_usd", "health_score"],
  "account": {"account_id": "EVAL-6", "name": "Zeta Oy", "arr_usd": 40000,
              "renewal_date": "2026-09-12", "segment": "smb"},
  "health": {"health_score": 50, "health_score_previous": 72, "seats_licensed": 20,
             "seats_active_30d": 18, "seats_active_90d_ago": 19, "weekly_active_users": 15,
             "data_sources_connected": 4, "data_sources_connected_90d_ago": 4,
             "last_login_days_ago": 1, "nps_last": 6, "nps_previous": 7,
             "qbr_last_days_ago": 30, "exec_sponsor_changed": false,
             "support": {"open_tickets": 1, "p1_tickets_30d": 0, "tickets_30d": 2,
                         "tickets_prev_30d": 3, "avg_first_response_hours": 4.0,
                         "csat_30d": 4.5}}
}
```

```bash
.venv/bin/python cli.py eval --samples 3
```

> "A prompt change is a production change, so it goes through a regression set like code. Note it samples three times: I found the model is non-deterministic on ambiguous inputs, so a single run measures luck."

**Expect this one to fail**, and let it. The deterministic analyser has no pricing rule. That is the honest, useful answer: *"the eval just told me my fallback has a gap. That's exactly what it's for."*

---

## 5. "Add a fourth agent" (4 minutes) - the money shot

Rehearse this one until it's muscle memory. It's the clearest proof the platform is real.

**Step 1.** `agents/expansion_signal/agent.py`:

```python
"""Flags accounts healthy enough to upsell. Proof that onboarding an agent
touches nothing that already exists."""
from __future__ import annotations
from typing import Any
from agentplatform.observability import SKIPPED

def handle(ctx) -> dict[str, Any]:
    with ctx.trace.step("fetch_context") as step:
        account = ctx.tools.salesforce.call("get_account",
                                            {"account_id": ctx.event.account_id})
        health = ctx.tools.gainsight.call("get_health",
                                          {"account_id": ctx.event.account_id})
        step.set(account_name=account.get("name"))

    licensed = health.get("seats_licensed") or 0
    active = health.get("seats_active_30d") or 0
    utilisation = (active / licensed) if licensed else 0
    score = health.get("health_score") or 0

    expand = utilisation > 0.85 and score >= 70
    reason = (f"seat utilisation {utilisation:.0%} and health {score}: "
              f"{'room to upsell' if expand else 'not an expansion candidate'}")
    ctx.trace.decision("expansion_gate", "expand" if expand else "hold", reason,
                       utilisation=round(utilisation, 2), health_score=score)

    if not expand:
        ctx.trace.record("agent_skipped", SKIPPED, reason=reason)
        return {"acted": False, "summary": f"skipped: {reason}"}

    with ctx.trace.step("notify_slack"):
        ctx.tools.slack.call("post_message", {
            "channel": "#gtm-expansion",
            "text": (f"*Expansion signal* - {account.get('name')}\n"
                     f"{active} of {licensed} seats active, health {score}. "
                     f"Worth an upsell conversation.\n_Trace: {ctx.trace.trace_id}_")},
            idempotency_key=f"{ctx.event.event_id}:slack")

    return {"acted": True, "summary": f"expansion signal for {account.get('name')}"}
```

```bash
touch agents/expansion_signal/__init__.py
```

**Step 2.** `config/registry.yaml`, add:

```yaml
  - name: expansion_signal
    version: "0.1.0"
    description: Flags healthy, high-utilisation accounts worth an upsell conversation.
    owner: denis.miano
    owner_email: denis.miano@supermetrics.com
    owner_slack: "@denis"
    team: gtm-ai
    handler: agents.expansion_signal.agent:handle
    subscribes_to: [health_score.dropped]
    tools: [salesforce, gainsight, slack]
    writes_golden_record: false
    last_reviewed: "2026-07-21"
    review_interval_days: 90
    enabled: true
```

**Step 3.** Prove it:

```bash
.venv/bin/python cli.py registry                                   # now 4 agents
.venv/bin/python cli.py send gainsight samples/webhook_health_score_drop.json
```

Both agents now run on that one event. Show the dashboard: two traces, both fully instrumented, retries and idempotency inherited, the new agent already in the registry with an owner and a review date.

> "Two files. Nothing existing changed. It inherited tracing, retries, idempotency, tool permissions and the dashboard for free, and it can only touch the three tools its registry entry grants. That's what I mean by a platform."

---

## 6. "Show me the failure handling" (30 seconds)

```bash
TOOL_FAILURE_INJECTION_RATE=0.4 .venv/bin/python runner.py
```

> "Forty percent of every vendor call failing. Measured across twelve runs earlier: eleven still completed, fifteen calls recovered by retry, and only the genuinely unrecoverable ones reached the dead-letter queue for replay."

---

## Things to say while editing

- **On any config change:** "If I had to change this in Python, it would be in the wrong place."
- **On the new agent:** "The complexity didn't disappear, it moved into the platform where it's written once and tested once."
- **If something fails:** name what you expected before you look. Then look. Never guess at a cause you can't point to.
