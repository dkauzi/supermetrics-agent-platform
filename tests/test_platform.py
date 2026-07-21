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
from agentplatform.clients.base import ToolClient, ToolError, ToolPermissionError
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


def test_retryable_failure_is_retried_then_succeeds(platform):
    client = FlakyClient(platform.config, fail_times=2)
    trace = _trace(platform)
    assert client.call("op", {}, trace)["id"] == "ok-1"
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
    with pytest.raises(ToolError):
        client.call("op", {}, _trace(platform))
    assert client.calls == 1, "a 4xx must not burn the rate limit on retries"


def test_idempotency_key_prevents_second_write(platform):
    client = FlakyClient(platform.config, fail_times=0)
    trace = _trace(platform)
    client.call("op", {}, trace, idempotency_key="k1")
    client.call("op", {}, trace, idempotency_key="k1")
    assert client.calls == 1


def test_agent_cannot_use_undeclared_tool(platform):
    scoped = platform.tools.scoped(["slack"], _trace(platform))
    scoped.slack  # granted
    with pytest.raises(ToolPermissionError):
        scoped.salesforce  # not granted


def test_secrets_are_redacted_from_traces():
    redacted = ToolClient._redact({"api_token": "supersecret", "name": "fine"})
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


# ── Platform QA agent ──────────────────────────────────────────────────────────

def test_platform_qa_passes_on_a_clean_platform(platform, monkeypatch, tmp_path):
    from agents.platform_qa import agent as qa
    monkeypatch.setattr(qa, "check_review_cadence", lambda registry: [])
    monkeypatch.setattr(qa, "check_eval_gate", lambda: [])

    result = platform.ingest("platform", {"eventId": "audit-clean"})
    payload = result["results"][0]["result"]
    assert payload["verdict"] == "PASS"
    assert payload["critical"] == 0


def test_platform_qa_flags_dead_letters_as_critical(platform):
    platform.warehouse.dead_letter("gainsight", "normalisation_failed: boom", {})
    result = platform.ingest("platform", {"eventId": "audit-dlq"})
    payload = result["results"][0]["result"]
    assert payload["verdict"] == "FAIL"
    assert any(f["check"] == "dead_letters" and f["severity"] == "critical"
               for f in payload["findings"])


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
