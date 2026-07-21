# How I built this with a coding agent

Built with **Claude Code**. I'm recording how I drove it because "used an AI agent" is not the interesting part - how you keep one honest is.

## How I directed it

I did not ask for "a renewal risk app." I specified the architecture first - platform layer, agent layer, the boundary between them - and had the agent implement against that shape. The decisions below were mine, and they're the ones that determine whether this is a demo or a platform:

- **Build the platform, not just the agent.** The brief describes one agent; the role owns the layer beneath. So the event bus, registry, tool layer and trace store came first, and the renewal agent was written as a consumer of them.
- **Structured evidence, not prose.** My call, and the highest-leverage one. Because `EvidenceItem` is a metric/value pair, grounding is verified by comparison in `verifier.py` instead of by asking a second model to judge the first. Deterministic, free, and it can't itself hallucinate.
- **Closed driver taxonomy.** Free-text drivers can't be measured, and unmeasurable output can't have a learning loop. Fixing the enum is what makes per-driver precision meaningful.
- **Severity is code, not model output.** The LLM explains; the platform decides blast radius. I wouldn't let a probabilistic system decide who gets paged.
- **Fallback must alert anyway.** When the model is unavailable, degrade the analysis, never drop the human notification.

## Where I corrected it

- The first pass had agents importing clients directly. I replaced it with an injected `AgentContext` plus tool grants from the registry, so an agent can only touch what it's declared - least privilege enforced by the platform, not by convention.
- The first schema let `driver` be a free string. I closed it to an enum for the reason above.
- Naming the package `platform` shadowed a Python stdlib module. Renamed to `agentplatform`.
- Pinned dependency versions forced source builds on Python 3.14. I moved to floors and forced wheels.
- The generated fallback path originally swallowed LLM failures silently. I made every degradation write a `degraded_reason` to the trace - silent degradation is the failure mode that erodes trust in an agent platform fastest.

## What I did not delegate

Test design. The suite is deliberately weighted toward failure paths - idempotency, non-retryable 4xx, ungrounded citations, cold-start calibration, golden-record merge semantics - because those are the assertions that encode intent. An agent will happily generate tests that pass against whatever the code currently does; that's how you get a green suite that guards nothing.

Same reason the golden eval set is hand-labelled. `ambiguous_must_not_guess` exists specifically to catch a confidently wrong answer, which is the failure mode that actually costs a CS team its trust in an agent.

## Verification

Everything in this repo was executed, not assumed: 32 tests passing, five end-to-end scenarios via `runner.py`, the golden eval gate at 5/5 with 100% grounding, and the platform self-audit correctly failing on a deliberately dirty state.

One more decision worth naming: when I added the QA agent, the obvious move was an LLM judging other agents' output. I didn't. Grounding is already verified by comparison, and every remaining check (is this agent owned? is the DLQ empty? is the eval gate green?) has a correct answer. Putting a model in the safety path would have made a deterministic result probabilistic and charged me for the privilege. Knowing where *not* to use the model is most of the job.
