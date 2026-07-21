"""Tests for the platform layer.

Weighted deliberately toward failure paths. The happy path is demonstrated by
runner.py; what needs proving in tests is that the platform behaves correctly when
inputs are malformed, vendors are down, and events arrive twice.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentplatform import build_platform
from agentplatform.clients import ToolClient, ToolError, ToolPermissionError, build_chain
from agentplatform.clients.policies import Call, CircuitOpen, TracingPolicy
from agentplatform.llm import PromptBundle
from agents.renewal_risk.schemas import ChurnAnalysis
from agentplatform.config import load_config
from agentplatform.events import UnknownEventSource, normalise
from agentplatform.feedback import Calibration, record_outcome
from agentplatform.registry import AgentRegistry, RegistryError
from agentplatform.store import SQLiteWarehouse
from agentplatform.verifier import verify_grounding

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """Isolated platform per test - no shared SQLite state between tests."""
    load_config.cache_clear()
    monkeypatch.setenv("LLM_MODE", "offline")
    monkeypatch.setenv("WAREHOUSE", "sqlite")
    config = load_config()
    config.raw["platform"]["sqlite_path"] = str(tmp_path / "test.db")
    from agentplatform.store import build_warehouse
    from agentplatform.clients import build_toolbelt
    from agentplatform.event_bus import EventBus
    from agentplatform.observability import Observability
    from agentplatform.llm import LLMClient
    from agentplatform import Platform

    warehouse = SQLiteWarehouse(tmp_path / "test.db")
    registry = AgentRegistry.load()
    observability = Observability(warehouse)
    tools = build_toolbelt(config)
    bus = EventBus(registry, observability, warehouse, config, tools)
    return Platform(config, warehouse, registry, observability, tools, bus, LLMClient(config))


def load_sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())


# ── Event normalisation ────────────────────────────────────────────────────────

def test_normalise_maps_vendor_payload_to_canonical_event():
    event = normalise("gainsight", load_sample("webhook_health_score_drop.json"))
    assert event.event_type == "health_score.dropped"
    assert event.account_id == "ACC-4417"
    assert event.source == "gainsight"


def test_unknown_source_is_rejected():
    with pytest.raises(UnknownEventSource):
        normalise("hubspot", {"anything": 1})


def test_missing_event_id_falls_back_to_content_hash():
    payload = {"account": {"id": "ACC-1"}, "health": {"current": 10}}
    first = normalise("gainsight", payload)
    second = normalise("gainsight", payload)
    # Same content must dedupe; different content must not.
    assert first.event_id == second.event_id
    third = normalise("gainsight", {"account": {"id": "ACC-1"}, "health": {"current": 11}})
    assert third.event_id != first.event_id


def test_naive_timestamp_is_made_timezone_aware():
    event = normalise("gainsight", {
        "eventId": "x", "account": {"id": "A"},
        "triggeredAt": datetime(2026, 7, 21, 8, 0, 0),
    })
    assert event.occurred_at.tzinfo is not None


# ── Idempotency and dead-lettering ─────────────────────────────────────────────

def test_duplicate_event_does_not_run_twice(platform):
    payload = load_sample("webhook_health_score_drop.json")
    first = platform.ingest("gainsight", payload)
    second = platform.ingest("gainsight", payload)

    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    # The critical assertion: exactly one Salesforce task across both deliveries.
    assert len(platform.tools.raw("salesforce").written) == 1


def test_payload_without_account_id_is_dead_lettered(platform):
    with pytest.raises(ValueError):
        platform.ingest("gainsight", {"eventId": "no-account", "health": {"current": 20}})
    letters = platform.warehouse.dead_letters()
    assert any("missing_account_id" in letter["reason"] for letter in letters)


def test_event_with_no_subscriber_is_dead_lettered(platform):
    from agentplatform.events import Event
    event = Event(event_id="orphan-1", event_type="nobody.listens", source="gainsight",
                  account_id="ACC-4417", occurred_at=datetime.now(timezone.utc))
    platform.warehouse.record_event(event)
    assert platform.bus.publish(event) == []
    assert any("no_subscriber" in letter["reason"] for letter in platform.warehouse.dead_letters())


# ── Registry ───────────────────────────────────────────────────────────────────

def test_registry_loads_and_resolves_handlers():
    registry = AgentRegistry.load()
    entry = registry.get("renewal_risk")
    assert entry is not None
    assert callable(entry.load_handler())


def test_registry_rejects_duplicate_agent_names():
    registry = AgentRegistry.load()
    entry = registry.get("renewal_risk")
    with pytest.raises(RegistryError):
        AgentRegistry([entry, entry])


def test_registry_flags_overdue_review():
    registry = AgentRegistry.load()
    # support_escalation is deliberately stale in the fixture registry.
    assert any(e.review_due for e in registry.all())


def test_subscriptions_are_data_not_code():
    registry = AgentRegistry.load()
    assert registry.subscribers_of("health_score.dropped")[0].name == "renewal_risk"
    assert registry.subscribers_of("support.ticket_spike")[0].name == "support_escalation"


# ── Tool client behaviour ──────────────────────────────────────────────────────

class FlakyClient(ToolClient):
    name = "flaky"

    def __init__(self, config, fail_times: int):
        super().__init__(config)
        self.fail_times = fail_times
        self.calls = 0

    def _execute(self, operation, payload):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("vendor timeout")
        return {"id": "ok-1", "status": "done"}


def _chain(transport, policies, settings=None):
    """Build a policy chain the same way build_toolbelt does."""
    return build_chain(transport, policies, settings or {})


def _invoke(chain, trace, idempotency_key=None, operation="op"):
    return chain.handle(Call(tool="flaky", operation=operation, payload={},
                             trace=trace, idempotency_key=idempotency_key))


def test_retryable_failure_is_retried_then_succeeds(platform):
    client = FlakyClient(platform.config, fail_times=2)
    chain = _chain(client, ["tracing", "retry"], {"retry": {"max_attempts": 3,
                                                           "backoff_seconds": 0}})
    assert _invoke(chain, _trace(platform))["id"] == "ok-1"
    assert client.calls == 3


def test_non_retryable_failure_is_not_retried(platform):
    class BadRequest(ToolClient):
        name = "bad"
        def __init__(self, config):
            super().__init__(config)
            self.calls = 0
        def _execute(self, operation, payload):
            self.calls += 1
            raise ValueError("400 bad request")

    client = BadRequest(platform.config)
    chain = _chain(client, ["tracing", "retry"], {"retry": {"max_attempts": 3,
                                                            "backoff_seconds": 0}})
    with pytest.raises(ToolError):
        _invoke(chain, _trace(platform))
    assert client.calls == 1, "a 4xx must not burn the rate limit on retries"


def test_idempotency_key_prevents_second_write(platform):
    client = FlakyClient(platform.config, fail_times=0)
    chain = _chain(client, ["tracing", "idempotency", "retry"])
    trace = _trace(platform)
    _invoke(chain, trace, idempotency_key="k1")
    _invoke(chain, trace, idempotency_key="k1")
    assert client.calls == 1


def test_calls_without_idempotency_key_are_not_deduplicated(platform):
    client = FlakyClient(platform.config, fail_times=0)
    chain = _chain(client, ["idempotency"])
    trace = _trace(platform)
    _invoke(chain, trace)
    _invoke(chain, trace)
    assert client.calls == 2, "dedupe must be opt-in, never implicit"


def test_circuit_opens_after_threshold_and_stops_calling_vendor(platform):
    class AlwaysDown(ToolClient):
        name = "down"
        def __init__(self, config):
            super().__init__(config)
            self.calls = 0
        def _execute(self, operation, payload):
            self.calls += 1
            raise TimeoutError("vendor down")

    client = AlwaysDown(platform.config)
    chain = _chain(client, ["circuit_breaker", "retry"],
                   {"circuit_breaker": {"failure_threshold": 3, "cooldown_seconds": 60},
                    "retry": {"max_attempts": 1, "backoff_seconds": 0}})
    trace = _trace(platform)

    for _ in range(3):
        with pytest.raises(ToolError):
            _invoke(chain, trace)
    assert client.calls == 3

    # Breaker is now open: further calls fail fast without touching the vendor.
    with pytest.raises(CircuitOpen):
        _invoke(chain, trace)
    assert client.calls == 3, "an open circuit must not reach the vendor"


def test_policy_chain_is_composed_per_vendor_from_config(platform):
    described = {d["tool"]: d for d in platform.tools.describe()}
    salesforce = [layer["policy"] for layer in described["salesforce"]["layers"]]
    slack = [layer["policy"] for layer in described["slack"]["layers"]]

    # Slack is configured to fail fast: no circuit breaker, no rate limit.
    assert "circuit_breaker" in salesforce
    assert "circuit_breaker" not in slack
    # Vendor overrides layer over defaults rather than replacing them.
    sf_retry = next(l for l in described["salesforce"]["layers"] if l["policy"] == "retry")
    assert sf_retry["settings"]["max_attempts"] == 4
    assert sf_retry["settings"]["backoff_multiplier"] == 2.0


def test_unknown_policy_name_fails_loudly(platform):
    with pytest.raises(ValueError, match="Unknown tool policies"):
        _chain(FlakyClient(platform.config, 0), ["tracing", "teleportation"])


def test_agent_cannot_use_undeclared_tool(platform):
    scoped = platform.tools.scoped(["slack"], _trace(platform))
    scoped.slack  # granted
    with pytest.raises(ToolPermissionError):
        scoped.salesforce  # not granted


def test_secrets_are_redacted_from_traces():
    redacted = TracingPolicy._redact({"api_token": "supersecret", "name": "fine"})
    assert redacted["api_token"] == "***"
    assert redacted["name"] == "fine"


# ── Verification ───────────────────────────────────────────────────────────────

class _Ev:
    def __init__(self, metric, value):
        self.metric, self.value = metric, value


def test_verifier_passes_grounded_claims(platform):
    result = verify_grounding([_Ev("health_score", 38), _Ev("arr_usd", 248000)],
                              {"health_score": 38, "arr_usd": 248000}, _trace(platform))
    assert result.passed and result.grounding_rate == 1.0


def test_verifier_catches_fabricated_value(platform):
    result = verify_grounding([_Ev("health_score", 12)], {"health_score": 38},
                              _trace(platform), min_claims=1)
    assert not result.passed
    assert "actual value is '38'" in result.violations[0]


def test_verifier_catches_invented_metric(platform):
    result = verify_grounding([_Ev("made_up_metric", 5)], {"health_score": 38},
                              _trace(platform), min_claims=1)
    assert not result.passed
    assert "unknown metric" in result.violations[0]


def test_verifier_enforces_minimum_evidence(platform):
    result = verify_grounding([_Ev("health_score", 38)], {"health_score": 38},
                              _trace(platform), min_claims=2)
    assert not result.passed


# ── Learning loop ──────────────────────────────────────────────────────────────

def test_calibration_is_neutral_before_enough_samples(platform):
    for i in range(3):
        record_outcome(platform.warehouse, f"t{i}", "renewal_risk", "A",
                       "support_burden", "high", "wrong")
    calib = Calibration(platform.warehouse, platform.config, "renewal_risk")
    # 3 samples is below min_samples=5: we must not act on noise.
    assert calib.confidence_multiplier("support_burden") == 1.0
    assert calib.needs_human_review("support_burden")[0] is False


def test_poor_driver_precision_forces_human_review(platform):
    for i in range(8):
        record_outcome(platform.warehouse, f"t{i}", "renewal_risk", "A",
                       "support_burden", "high", "wrong" if i < 6 else "correct")
    calib = Calibration(platform.warehouse, platform.config, "renewal_risk")
    needs_review, reason = calib.needs_human_review("support_burden")
    assert needs_review
    assert "2/8" in reason
    assert calib.confidence_multiplier("support_burden") < 1.0


def test_unclear_verdicts_do_not_count_against_a_driver(platform):
    for i in range(6):
        record_outcome(platform.warehouse, f"t{i}", "renewal_risk", "A",
                       "adoption_decline", "high", "unclear")
    calib = Calibration(platform.warehouse, platform.config, "renewal_risk")
    assert calib.for_driver("adoption_decline") is None


def test_invalid_verdict_is_rejected(platform):
    with pytest.raises(ValueError):
        record_outcome(platform.warehouse, "t", "renewal_risk", "A", "d", "high", "maybe")


# ── Observability ──────────────────────────────────────────────────────────────

def test_run_produces_explainable_trace(platform):
    result = platform.ingest("gainsight", load_sample("webhook_health_score_drop.json"))
    trace_id = result["trace_ids"][0]
    explanation = platform.observability.explain(trace_id)

    assert explanation["found"]
    assert explanation["narrative"], "a run with no narrative is not explainable"
    # The routing decision must name the rule that fired, not just the outcome.
    rules = [d["rule_id"] for d in explanation["decisions"]]
    assert any("critical" in r or "high" in r or "routine" in r for r in rules)


def test_offline_mode_degrades_but_still_alerts(platform):
    result = platform.ingest("gainsight", load_sample("webhook_health_score_drop.json"))
    agent_result = result["results"][0]["result"]
    assert agent_result["acted"] is True
    assert agent_result["method"] == "deterministic_fallback"
    assert len(platform.tools.raw("slack").sent) == 1, "alert must survive LLM unavailability"


def test_golden_record_records_provenance(platform):
    platform.ingest("gainsight", load_sample("webhook_health_score_drop.json"))
    record = platform.warehouse.get_golden_record("ACC-4417")
    assert record["updated_by"].startswith("renewal_risk@")
    assert record["trace_id"].startswith("tr_")
    assert record["revision"] == 1


def test_golden_record_merges_rather_than_overwrites(platform):
    platform.warehouse.upsert_golden_record("ACC-X", {"owned_by_other_system": "keep me"},
                                            "other_agent", "tr_x")
    platform.warehouse.upsert_golden_record("ACC-X", {"renewal_risk_driver": "adoption_decline"},
                                            "renewal_risk", "tr_y")
    record = platform.warehouse.get_golden_record("ACC-X")
    assert record["data"]["owned_by_other_system"] == "keep me"
    assert record["revision"] == 2


# ── LLM cost budget ────────────────────────────────────────────────────────────

def test_spend_is_summed_from_the_same_rows_the_dashboard_shows(platform):
    trace = _trace(platform)
    trace.record_cost("test/model", 1000, 500, 0.02)
    trace.record_cost("test/model", 1000, 500, 0.03)
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    assert platform.warehouse.llm_spend_since(midnight) == pytest.approx(0.05)


def test_exhausted_budget_degrades_instead_of_billing_or_failing(platform, monkeypatch):
    from agentplatform.llm import LLMClient, LLMUnavailable

    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    platform.config.raw["llm"]["daily_cost_budget_usd"] = 0.01

    trace = _trace(platform)
    trace.record_cost("test/model", 1000, 500, 0.05)  # already over budget

    client = LLMClient(platform.config)
    bundle = PromptBundle(name="t", version="v1", system="s", user="u")

    # Must refuse before any network call, and refuse in the way callers already
    # handle, so the alert still reaches a human via the deterministic path.
    with pytest.raises(LLMUnavailable, match="daily_cost_budget_exhausted"):
        client.complete_structured(bundle, ChurnAnalysis, trace)


def test_no_budget_configured_means_no_ceiling(platform, monkeypatch):
    from agentplatform.llm import LLMClient

    platform.config.raw["llm"]["daily_cost_budget_usd"] = None
    assert LLMClient(platform.config)._budget_remaining(_trace(platform)) is None


# ── Runaway protection and the human gate ──────────────────────────────────────

def _burn_llm_calls(platform, account_id, n, cost=0.001):
    """Simulate n prior model calls about one account."""
    from agentplatform.events import Event
    for i in range(n):
        event = Event(event_id=f"burn-{account_id}-{i}", event_type="health_score.dropped",
                      source="gainsight", account_id=account_id,
                      occurred_at=datetime.now(timezone.utc))
        platform.warehouse.record_event(event)
        platform.observability.start_run(event, "renewal_risk").record_cost(
            "test/model", 100, 50, cost)


def test_flapping_account_is_throttled_not_billed_repeatedly(platform):
    from agentplatform.limits import check_limits

    limit = platform.config.get("limits.max_llm_calls_per_account_per_hour")
    _burn_llm_calls(platform, "ACC-4417", limit)

    decision = check_limits(platform.warehouse, platform.config, "ACC-4417")
    assert decision.allow_llm is False
    assert decision.limit_hit == "account_hourly_llm_calls"
    # The whole point: throttling must escalate, never silently drop the work.
    assert decision.force_human_review is True


def test_throttle_is_per_account_not_global(platform):
    from agentplatform.limits import check_limits

    _burn_llm_calls(platform, "ACC-4417",
                    platform.config.get("limits.max_llm_calls_per_account_per_hour"))
    # A different account must be unaffected by one noisy neighbour.
    assert check_limits(platform.warehouse, platform.config, "ACC-2201").allow_llm is True


def test_soft_ceiling_stops_spend_before_the_budget_is_drained(platform):
    from agentplatform.limits import check_limits

    platform.config.raw["llm"]["daily_cost_budget_usd"] = 1.0
    platform.config.raw["limits"]["max_llm_calls_per_account_per_hour"] = 0
    _burn_llm_calls(platform, "ACC-9033", 1, cost=0.95)  # 95% of budget

    decision = check_limits(platform.warehouse, platform.config, "ACC-9033")
    assert decision.allow_llm is False
    assert decision.limit_hit == "daily_cost_soft_ceiling"
    assert decision.force_human_review is True


def test_throttled_run_still_alerts_the_human(platform):
    platform.config.raw["limits"]["max_llm_calls_per_account_per_hour"] = 1
    _burn_llm_calls(platform, "ACC-4417", 2)

    result = platform.ingest("gainsight", load_sample("webhook_health_score_drop.json"))
    payload = result["results"][0]["result"]

    assert payload["acted"] is True
    assert payload["needs_human_review"] is True
    assert platform.tools.raw("slack").sent, "throttling must never swallow the alert"


def test_flagged_run_holds_crm_writes_and_asks_instead(platform):
    # Drive the driver's measured precision below the review floor.
    for i in range(8):
        record_outcome(platform.warehouse, f"seed-{i}", "renewal_risk", "ACC-4417",
                       "adoption_decline", "critical", "wrong")

    result = platform.ingest("gainsight", load_sample("webhook_health_score_drop.json"))
    payload = result["results"][0]["result"]

    assert payload["needs_human_review"] is True
    assert payload["writes_held_for_approval"] is True
    # Nothing was written to the systems of record on a prediction we distrust.
    assert platform.tools.raw("salesforce").written == []
    assert platform.tools.raw("gainsight").written == []
    # But a human was asked, on the approvals channel.
    assert any(m["channel"] == "#cs-agent-approvals"
               for m in platform.tools.raw("slack").sent)


def test_held_writes_are_visible_in_the_golden_record(platform):
    for i in range(8):
        record_outcome(platform.warehouse, f"seed-{i}", "renewal_risk", "ACC-4417",
                       "adoption_decline", "critical", "wrong")
    platform.ingest("gainsight", load_sample("webhook_health_score_drop.json"))

    record = platform.warehouse.get_golden_record("ACC-4417")
    # A downstream consumer must be able to tell a held finding from an asserted one.
    assert record["data"]["renewal_risk_write_status"] == "awaiting_approval"


def test_trusted_run_writes_without_asking(platform):
    result = platform.ingest("gainsight", load_sample("webhook_health_score_drop.json"))
    payload = result["results"][0]["result"]

    assert payload["writes_held_for_approval"] is False
    assert len(platform.tools.raw("salesforce").written) == 1
    record = platform.warehouse.get_golden_record("ACC-4417")
    assert record["data"]["renewal_risk_write_status"] == "asserted"


# ── Platform QA agent ──────────────────────────────────────────────────────────

def test_platform_qa_passes_on_a_clean_platform(platform, monkeypatch, tmp_path):
    from agents.platform_qa import agent as qa
    monkeypatch.setattr(qa, "check_review_cadence", lambda registry: [])
    monkeypatch.setattr(qa, "check_eval_gate", lambda: [])

    result = platform.ingest("platform", {"eventId": "audit-clean"})
    payload = result["results"][0]["result"]
    assert payload["verdict"] == "PASS"
    assert payload["critical"] == 0


def test_platform_qa_flags_real_processing_failures_as_critical(platform):
    platform.warehouse.dead_letter("gainsight", "normalisation_failed: boom", {})
    result = platform.ingest("platform", {"eventId": "audit-dlq"})
    payload = result["results"][0]["result"]
    assert payload["verdict"] == "FAIL"
    assert any(f["check"] == "dead_letters" and f["severity"] == "critical"
               for f in payload["findings"])


def test_guarded_rejections_are_not_treated_as_incidents(platform):
    # Input we correctly refused at the boundary is the negative path working.
    # Paging on it trains people to ignore the alert, which is how a real
    # failure gets missed.
    platform.warehouse.dead_letter("hubspot", "unknown_source: no normaliser", {})
    platform.warehouse.dead_letter("gainsight", "missing_account_id", {})

    result = platform.ingest("platform", {"eventId": "audit-guarded"})
    payload = result["results"][0]["result"]

    assert payload["critical"] == 0
    assert payload["verdict"] != "FAIL"
    assert any(f["check"] == "guarded_rejections" for f in payload["findings"])


def test_dashboard_and_audit_share_one_definition_of_a_problem(platform):
    from agents.platform_qa.agent import is_guarded_rejection
    assert is_guarded_rejection("unknown_source: no normaliser for 'hubspot'")
    assert is_guarded_rejection("missing_account_id")
    assert is_guarded_rejection("no_subscriber_for_event_type:foo.bar")
    assert not is_guarded_rejection("normalisation_failed: boom")
    assert not is_guarded_rejection("agent_raised:renewal_risk:ValueError")


def test_platform_qa_flags_unowned_agent(platform):
    from agents.platform_qa.agent import check_ownership

    entry = platform.registry.get("renewal_risk").model_copy(update={"owner_slack": ""})

    class _Reg:
        def all(self): return [entry]

    findings = check_ownership(_Reg())
    assert findings and findings[0].severity == "critical"


def test_platform_qa_audit_is_itself_traced(platform):
    result = platform.ingest("platform", {"eventId": "audit-traced"})
    explanation = platform.observability.explain(result["results"][0]["trace_id"])
    # The audit must be as explainable as the agents it audits.
    assert any(d["rule_id"].startswith("audit_") for d in explanation["decisions"])


def _trace(platform):
    from agentplatform.events import Event
    event = Event(event_id=f"t-{datetime.now(timezone.utc).timestamp()}",
                  event_type="test.event", source="gainsight", account_id="ACC-4417",
                  occurred_at=datetime.now(timezone.utc))
    platform.warehouse.record_event(event)
    return platform.observability.start_run(event, "test")
