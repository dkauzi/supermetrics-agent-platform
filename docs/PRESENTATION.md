# The 20 minute walkthrough

Speaker notes for the panel. Timings are deliberate: **demo first, architecture second, findings last.** People believe what they watch run.

Total 18 minutes, leaving buffer in a 15-20 minute slot.

---

## Before you start (2 min before the call)

```bash
cd supermetrics-agent-platform
rm -f data/platform.db data/last_eval.json     # clean slate, no confusing leftovers
.venv/bin/uvicorn app:app                      # leave running in tab 1
```
Tab 2 at the repo root. Browser on http://127.0.0.1:8000. Confirm `.env` has the key and `LLM_MODE` is unset or `live`.

Have a second terminal ready with `LLM_MODE=offline` exported, as your safety net if the wifi dies.

**Do this once before the day:** `docker compose up --build`. The image has never been built (no Docker daemon on the machine it was written on), so prove it works, or drop the Jaeger moment from the script and say the tracing is wired but the container is unproven. Do not find out live.

---

## 1. Frame it (90 seconds, no slides)

> "The brief asks for one agent. The role is owning the platform that many agents plug into. So I built the platform and made the renewal agent its first tenant. Everything you're about to see is reusable by the next agent, and I'll prove that by showing you a second and third one running on the same rails without a line of the first one changing."

State the three things you optimised for, because these are your spine for the whole session:

1. **Nothing the model says is trusted without checking.**
2. **Every decision is explainable to a non-engineer.**
3. **When anything goes wrong, work routes to a human. It never silently stops.**

---

## 2. Run it (5 minutes)

```bash
.venv/bin/python runner.py
```

Talk over the output. Do not read it aloud, narrate what matters:

- **Scenario 1 is their own payload, run as given.** This is the one that matters. Three accounts, near-identical triggers, and the output is three *different* drivers with different evidence. Say it plainly: *"Your file is built to catch a system that just reformats the trigger. It caught mine. My first prompt called the usage-collapse account champion_loss because a contact had also changed role, so two accounts got the same answer. The discriminator turned out to be in the data: Northwind's automation stopped too, which one person leaving does not explain. Verdant's automation is fine and only the human work stopped. All three are now permanent eval cases."* Then point at the deduped redelivery: one CRM write, one alert.
- **Scenario 2**, our own webhook shape. *"A different payload shape for the same business fact. One normaliser, no agent change."*
- **Scenario 3**, Salesforce payload, a third shape again. *"Vendor payload changes stay contained."*
- **Scenario 4**, a second agent on a different event. *"I added this by writing one file and one registry entry. Zero lines of the renewal agent changed. That's the test of whether an event bus is real or decorative."*
- **Scenarios 5 and 6**, bad input. *"Rejected at the boundary with a reason, in the dead-letter queue. Not lost, not half-written."*

---

## 3. Answer "why did this agent do that?" (4 minutes)

This is requirement 5 in the brief and the panel will care most about it. Open the dashboard, click the top run.

Read the plain-English panel out loud, verbatim. Then say:

> "That's the actual product requirement. A CS lead can answer 'why did the agent do that' in about fifteen seconds, without opening code, without a log aggregator, without me."

Expand **Technical detail** and show the same run as rules and values.

> "Same trace, two readers. The engineer's version names the rule that fired and the values it matched, so it can't drift from the plain-English one."

Then the terminal version, because on-call at 2am nobody wants a browser:

```bash
.venv/bin/python cli.py why <trace_id>
```

Scroll the dashboard: **agent registry** (owner, subscriptions, overdue review), **integrations** (per-vendor policy chain and circuit state), **cost**, **guardrails**, **learning loop**.

If you have `docker compose up` running, open Jaeger on :16686 for ten seconds:

> "Standard OpenTelemetry as well, one trace per run with each step as a child span. That answers 'where did the time go'. It does *not* answer 'why did the agent conclude that', which is why both exist. They cross-reference by id."

---

## 4. The findings (4 minutes) - your strongest material

This is what separates you from someone who wired an API to Slack. Tell it as a story.

**Finding one: structured evidence makes hallucination checkable by code.**

> "The model has to cite evidence as metric/value pairs, not prose. That means I can verify every claim against the data we actually fetched, in code, deterministically. No second LLM judging the first. If it invents a number, the analysis is discarded and the rules-based answer used instead. My grounding rate is 100% because ungrounded output never survives."

**Finding two: single-run evals measure luck.**

> "I have a golden eval set with a deliberately ambiguous account, where the correct answer is 'I don't know'. Prompt v2 confidently claimed adoption decline. So I wrote v3 with an explicit materiality test, and it passed. Then I ran it again and got a different answer, 0.8 confidence, on identical input. The model is non-deterministic on ambiguous cases, and my eval had been measuring luck. So the eval now samples each case multiple times and gates on a consistency metric, because a model that answers differently each time is not usable for automated action however good its average looks."

Pause there. That is the most senior thing in the whole submission.

**Finding three: the QA agent deliberately has no LLM.**

> "It checks that every agent has an owner, that reviews aren't overdue, that the dead-letter queue is clean, that the eval gate is green. Every one of those has a correct answer, so putting a model in the safety path would make a deterministic result probabilistic and charge me for it. Knowing where not to use the model is most of this job."

---

## 5. Architecture (3 minutes)

Open `docs/ARCHITECTURE.md`, show the diagram, do not narrate every box. Make three points:

- **Cloud Run + Pub/Sub, ingestion split from workers.** *"Vendors time out webhooks in seconds, LLM analysis takes tens of seconds. If those share a process, a slow model makes a vendor mark our endpoint unhealthy and start dropping events."*
- **BigQuery because the Golden Record already lives there.** Traces land where analysts can join agent behaviour to revenue. *"Did the accounts we alerted on actually renew?" becomes SQL.* Be upfront: the implementation exists and its SQL is tested, but has not run against a live dataset.
- **The 10x change:** async ingestion first. Say why it's first.

---

## 6. Close (30 seconds)

> "The honest summary: the vendor clients are mocks, and the BigQuery adapter hasn't run against a real dataset. Everything else you've seen actually executes: 81 tests, the eval gate, the self-audit, all green in CI on three Python versions. What I'd build next is the approval loop closing properly, so a Slack approve button writes back and feeds the calibration table."

---

## Questions you should expect

| Question | Where to go |
|---|---|
| "How do you know the LLM is right?" | Structured evidence, deterministic grounding check, golden eval, calibration from human verdicts. |
| "What happens when OpenRouter is down?" | Model chain, then deterministic fallback, run marked degraded, human still alerted. Show it: `LLM_MODE=offline python runner.py`. |
| "How would you onboard a new agent?" | One module, one registry entry. Offer to do it live. |
| "How do you handle a Salesforce API change?" | One normaliser or one `_execute`. Everything else inherits. |
| "What about cost?" | `GET /cost`, per-account throttle, soft ceiling, degrade to human not to silence. |
| "GDPR?" | Identifiers pseudonymised before the payload leaves for OpenRouter, restored in output. Be honest it's minimisation, not anonymisation. |
| "What would you do differently?" | Async ingestion, approval write-back, and a real BigQuery run. |
| "Why not just use OpenTelemetry?" | I do, both. OTel answers "where did the time go"; the decision trace answers "why did it conclude that". Sampling would lose the one run someone asks about, and no CS lead opens Jaeger. Each carries the other's ids. `docker compose up` and show Jaeger. |
| "Why functions, not a BaseAgent class?" | The registry entry *is* the interface, and it is data, so the platform can validate ownership, tool grants and review dates without importing anything. A base class puts that contract in Python where only Python can read it. |
| "Why GCP and not AWS?" | The Golden Record is already in BigQuery. Same project means the warehouse is a native sink, not an export job, and IAM is one story. On AWS I'd use API Gateway → SQS → Fargate workers, with the same interfaces. |

## If something breaks live

Do not debug silently. Say what you expected, what happened, and where you'd look. Then:

```bash
LLM_MODE=offline .venv/bin/python runner.py    # no network needed at all
```

A candidate who calmly falls back to a designed degradation path is demonstrating the exact thing the system is built around. That is a better moment than a clean run.
