# Runbook

## Rolling back a prompt

A prompt change is a production change, so it gets the same controls as code: versioned, gated, reversible.

```bash
# 1. Confirm the regression against the golden eval set
python cli.py eval --prompt-version v2     # suspected bad
python cli.py eval --prompt-version v1     # last known good

# 2. Roll back: one line in config/platform.yaml
#    agents.renewal_risk.prompt_version: v2  ->  v1

# 3. Verify the change took effect on a real run
python cli.py send gainsight samples/webhook_health_score_drop.json
python cli.py why <trace_id>        # confirms prompt=renewal_risk.churn_driver@v1
```

Rollback is instant because **prompt versions are never edited in place** — `prompts.py` keeps every version. Editing v2 to "fix" it would invalidate every eval result ever recorded against v2 and leave nothing to roll back to.

**Promoting a new version:** add `v3` to `PROMPT_VERSIONS` → `python cli.py eval --prompt-version v3` → compare accuracy, grounding rate and cost against the active version → flip config → keep the previous version indefinitely.

The gate (`config/platform.yaml` → `eval`) fails CI below 60% driver accuracy or **any** grounding failure. Grounding has no acceptable failure rate: a fabricated citation is a defect, not a miss.

## Swapping a model

```yaml
llm:
  model_chain:
    - anthropic/claude-sonnet-4.5   # deprecated? delete this line
    - openai/gpt-4o-mini            # traffic moves here automatically
```

No code change, no deploy. Run the eval against the new chain before promoting — a cheaper model that holds accuracy is a real cost win, and the eval is how you prove it rather than assume it.

## On-call: an agent did something surprising

```bash
python cli.py traces                 # find the run
python cli.py why <trace_id>         # what it did and which rule fired
python cli.py dlq                    # did anything fail to process?
python cli.py replay <trace_id>      # re-run against current code to verify a fix
```

`why` names the rule and the values it matched on. If the answer is "the routing rule was wrong", that's a YAML change. If it's "the model picked the wrong driver", add the case to `tests/golden/cases.json` first, then change the prompt — the case is what stops it regressing later.

**"Nothing happened and it should have."** Skipped runs are traced too. Look for the `entry_gate` decision: it records exactly why the agent declined to act.

## Common symptoms

| Symptom | Likely cause | First check |
|---|---|---|
| All runs show `deterministic_fallback` | No API key, or every model in the chain failing | `GET /healthz` → model chain; trace detail → transport errors |
| Alerts fire but nothing in Salesforce | Write step failed after analysis | `why` → look for the `salesforce.create_task` error and attempt count |
| Duplicate Slack alerts | Idempotency key not reaching the client | Confirm the vendor sends a stable `eventId`; content-hash fallback treats changed values as new events *by design* |
| DLQ growing | Vendor changed payload shape | `python cli.py dlq` → reason field names the failure; fix the normaliser |
| A driver keeps being marked wrong | Genuine model weakness | `GET /calibration` — below the review threshold it auto-flags for human review; add cases and revise the prompt |

## Adding an agent

1. Write `agents/<name>/agent.py` exposing `handle(ctx)`.
2. Add a registry entry: owner, subscriptions, tool grants, review cadence.
3. That's it — the bus routes it, tracing and retries are inherited, the dashboard picks it up.

If step 3 ever requires editing the platform, the platform has a gap. `support_escalation` exists as the standing proof this holds.

## Demoing the failure paths

```bash
TOOL_FAILURE_INJECTION_RATE=0.4 python runner.py   # forces retries and DLQ writes
LLM_MODE=offline python runner.py                  # forces the deterministic fallback
```
