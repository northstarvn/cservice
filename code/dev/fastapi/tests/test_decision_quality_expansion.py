"""Tests for Phase 1: decision quality & consistency.

Covers formal model versioning + shadow-mode canary (S-03,
``app/model_versioning.py``), simulation / what-if engines (S-02,
``app/simulation_engine.py``), explainability surfaces (C-05,
``app/explainability.py``), and optimistic concurrency on mutable entities
(M-03, ``app/optimistic_locking.py``), plus the ``/meta/decisions`` surfaces.
"""
from datetime import datetime, timezone

import pytest

from app import (
    explainability,
    model_versioning,
    risk_evaluator,
    simulation_engine,
)
from app.explainability import DecisionTrace, DecisionTraceStore
from app.optimistic_locking import (
    StaleVersionError,
    VersionedRecord,
    compare_and_swap,
)

RULES = [
    {
        "rule_id": "r_new_device",
        "weight": 22,
        "match": {"device_proven": False},
        "reason": "unproven device",
    },
    {
        "rule_id": "r_geo",
        "weight": 28,
        "match": {"geo_velocity_kmh": (">", 800)},
        "reason": "impossible travel",
    },
    {
        "rule_id": "r_odd_hour",
        "weight": 10,
        "match": {"odd_hour": True},
        "reason": "off hours",
    },
]
CONTEXT = {"device_proven": False, "geo_velocity_kmh": 900, "odd_hour": True}


def _registry():
    reg = model_versioning.ModelVersionRegistry()
    reg.register("risk_rules", {"rules": [dict(r) for r in RULES]})
    return reg


def _candidate_rules(*, weight=60):
    candidate = [dict(r) for r in RULES]
    candidate[0]["weight"] = weight  # r_new_device tuned
    return candidate


# --- optimistic concurrency (M-03) -------------------------------------------


def test_versioned_record_cas_success_and_bump():
    rec = VersionedRecord("risk_model", {"weight": 30})
    version, data = rec.read()
    assert version == 1 and data == {"weight": 30}
    out = rec.update(version, lambda d: d.update({"weight": 36, "note": "x"}))
    assert out == {"weight": 36, "note": "x"}
    assert rec.version == 2


def test_stale_update_raises_stale_version_error():
    rec = VersionedRecord("risk_model", {"weight": 30})
    rec.update(1, lambda d: d.update({"weight": 36}))
    with pytest.raises(StaleVersionError) as exc:
        rec.update(1, lambda d: d.update({"weight": 99}))
    assert exc.value.expected == 1 and exc.value.current == 2
    assert "risk_model" in str(exc.value)
    # the failed write never landed
    assert rec.read()[1]["weight"] == 36


def test_compare_and_swap_merges_values():
    rec = VersionedRecord("partition_policy", {"retention_days": 90})
    compare_and_swap(rec, 1, {"retention_days": 45, "interval": "monthly"})
    version, data = rec.read()
    assert version == 2
    assert data == {"retention_days": 45, "interval": "monthly"}


def test_raising_mutator_leaves_record_untouched():
    rec = VersionedRecord("risk_model", {"weight": 30})

    def boom(data):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        rec.update(1, boom)
    assert rec.read() == (1, {"weight": 30})


# --- explainability (C-05) ---------------------------------------------------


def test_risk_result_converts_to_trace_with_factor_trail():
    result = risk_evaluator.score_with_config(
        [dict(r) for r in RULES], {"device_proven": False}
    )
    trace = DecisionTrace.from_risk_result(
        {"device_proven": False}, result, entity_ref="user:7", model_version="v3"
    )
    assert trace.decision_type == "risk_score"
    assert trace.score == 22
    assert [f["rule_id"] for f in trace.factors] == [r["rule_id"] for r in RULES]
    payload = trace.to_dict()
    assert payload["entity_ref"] == "user:7" and payload["model_version"] == "v3"
    assert payload["decision_id"]
    with pytest.raises(ValueError):
        DecisionTrace(decision_type="not_a_decision_type")


def test_trace_store_record_get_and_explain():
    store = DecisionTraceStore(capacity=5)
    did = store.record(
        DecisionTrace(decision_type="risk_score", entity_ref="user:7", outcome="high", score=80)
    )
    assert store.get(did)["score"] == 80
    assert store.explain(did)["outcome"] == "high"
    with pytest.raises(KeyError):
        store.explain("missing-id")


def test_trace_store_list_filters_newest_first():
    store = DecisionTraceStore()
    store.record(DecisionTrace(decision_type="risk_score", entity_ref="user:7", outcome="high"))
    store.record(DecisionTrace(decision_type="simulation", entity_ref="simulation:risk"))
    store.record(DecisionTrace(decision_type="risk_score", entity_ref="user:8", outcome="low"))
    assert [t["entity_ref"] for t in store.list(entity_ref="user:7")] == ["user:7"]
    assert len(store.list(decision_type="risk_score")) == 2
    assert len(store.list(limit=1)) == 1


def test_trace_store_bounded_eviction():
    store = DecisionTraceStore(capacity=3)
    ids = [
        store.record(DecisionTrace(decision_type="risk_score", entity_ref=f"u:{i}"))
        for i in range(5)
    ]
    assert store.stats()["total"] == 3
    assert store.get(ids[0]) is None  # oldest evicted
    assert store.get(ids[-1]) is not None


def test_record_decision_and_risk_trace_helpers():
    store = DecisionTraceStore()
    did = explainability.record_decision(
        decision_type="retention_action",
        entity_ref="partition:audit_log_entries__2026-01",
        outcome="drop",
        store=store,
    )
    assert store.get(did)["outcome"] == "drop"
    rid = explainability.record_risk_trace(
        {"a": 1}, {"score": 5, "level": "low", "factors": [], "weight_version": 2},
        store=store,
    )
    assert store.get(rid)["score"] == 5
    assert store.get(rid)["model_version"] == "2"


# --- model versioning + canary (S-03) ---------------------------------------


def test_registry_default_seed_has_live_models():
    reg = model_versioning.seed_default_registry()
    assert set(reg.models()) == {"risk_rules", "partition_policies"}
    info = reg.model("risk_rules")
    assert info["active_version"] == 1 and info["canary_version"] is None
    assert "rules" in reg.active_snapshot("risk_rules")
    assert "policies" in reg.active_snapshot("partition_policies")


def test_add_version_is_immutable_and_promote_switches_active():
    reg = _registry()
    reg.add_version("risk_rules", {"rules": _candidate_rules()}, label="tightened")
    assert reg.model("risk_rules")["canary_version"] == 2
    # active config untouched by registering the candidate
    assert reg.active_snapshot("risk_rules")["rules"][0]["weight"] == 22
    promoted = reg.promote("risk_rules")
    assert promoted["from_version"] == 1 and promoted["to_version"] == 2
    assert reg.model("risk_rules")["canary_version"] is None
    states = {v["version"]: v["state"] for v in reg.model("risk_rules")["versions"]}
    assert states == {1: "archived", 2: "active"}


def test_promote_with_stale_guard_conflicts():
    reg = _registry()
    reg.add_version("risk_rules", {"rules": _candidate_rules()})  # guard stays 1
    stale = reg.guard_version("risk_rules")
    reg.promote("risk_rules")  # guard 1 -> 2
    reg.add_version("risk_rules", {"rules": _candidate_rules()})  # canary back, guard 2
    with pytest.raises(StaleVersionError):
        reg.promote("risk_rules", expected_version=stale)
    # nothing was clobbered by the stale write
    assert reg.guard_version("risk_rules") == 2
    assert reg.model("risk_rules")["canary_version"] == 3
    with pytest.raises(ValueError):
        reg.promote("unknown_model")


def test_rollback_restores_archived_snapshot():
    reg = _registry()
    reg.add_version("risk_rules", {"rules": _candidate_rules()}, label="v2")
    reg.promote("risk_rules")
    reg.add_version("risk_rules", {"rules": _candidate_rules()}, label="v3")
    reg.promote("risk_rules")
    assert reg.model("risk_rules")["active_version"] == 3
    rolled = reg.rollback("risk_rules", to_version=1)
    assert rolled["to_version"] == 1
    assert reg.active_snapshot("risk_rules")["rules"][0]["weight"] == 22
    with pytest.raises(ValueError):
        reg.rollback("risk_rules", to_version=99)


def test_canary_shadow_scoring_serves_active_and_flags_divergence():
    reg = _registry()
    reg.add_version("risk_rules", {"rules": _candidate_rules(weight=60)})
    runner = model_versioning.CanaryRunner(registry=reg, auto_promote=False)
    result = runner.shadow_score("risk_rules", {"device_proven": False})
    assert result["served"]["score"] == 22  # active model is the served decision
    assert result["shadow"]["score"] == 60  # candidate scores in shadow only
    assert result["delta"] == 38
    assert result["within_tolerance"] is False  # 38 > 0.15 * 22
    assert result["runs"] == 1


def test_canary_confidence_accumulates_and_no_candidate_short_circuits():
    reg = _registry()
    reg.add_version("risk_rules", {"rules": _candidate_rules(weight=20)})
    runner = model_versioning.CanaryRunner(registry=reg, auto_promote=False)
    first = runner.shadow_score("risk_rules", {"device_proven": False})
    second = runner.shadow_score("risk_rules", {"device_proven": False})
    assert first["within_tolerance"] and second["within_tolerance"]
    assert runner.canary_confidence("risk_rules") == 1.0
    stats = runner.canary_stats("risk_rules")
    assert stats["runs"] == 2 and stats["within_tolerance_runs"] == 2

    bare = _registry()
    no_candidate = model_versioning.CanaryRunner(registry=bare)
    assert no_candidate.shadow_score("risk_rules", {})["reason"] == "no_canary_candidate"


def test_shadow_score_records_canary_trace():
    reg = _registry()
    reg.add_version("risk_rules", {"rules": _candidate_rules(weight=25)})
    runner = model_versioning.CanaryRunner(registry=reg, auto_promote=False)
    store = explainability.get_default_trace_store()
    before = store.stats()["total"]
    runner.shadow_score("risk_rules", {"device_proven": False}, entity_ref="probe:1")
    assert store.stats()["total"] == before + 1
    found = store.list(entity_ref="probe:1")
    assert found and found[0]["decision_type"] == "canary_score"
    assert found[0]["detail"]["delta"] == 3


def test_auto_promote_needs_confidence_and_run_count():
    reg = _registry()
    reg.add_version("risk_rules", {"rules": _candidate_rules(weight=22)})  # identical
    runner = model_versioning.CanaryRunner(
        registry=reg, auto_promote=True, promote_threshold=1.0, min_runs=2
    )
    first = runner.shadow_score("risk_rules", {"device_proven": False})
    assert not first.get("promoted")  # min_runs not met
    second = runner.shadow_score("risk_rules", {"device_proven": False})
    assert second.get("promoted") is True
    assert reg.model("risk_rules")["active_version"] == 2
    assert reg.model("risk_rules")["canary_version"] is None


# --- simulation / what-if (S-02) ---------------------------------------------


def test_score_with_config_and_effective_rules_agree_with_live_evaluator():
    risk_evaluator.reset_weight_state()
    effective = risk_evaluator.effective_risk_rules()
    assert {r["rule_id"] for r in effective} == {r["rule_id"] for r in risk_evaluator.RISK_RULES}
    assert all("weight" in r for r in effective)
    ctx = {"device_proven": False, "odd_hour": True}
    snapshot = risk_evaluator.score_with_config(effective, ctx)
    live = risk_evaluator.evaluate_risk(ctx)
    assert snapshot["score"] == live["score"] == 32
    assert snapshot["level"] == live["level"] == "moderate"


def test_risk_whatif_weight_override_changes_score():
    report = simulation_engine.simulate_risk_whatif(
        CONTEXT,
        base_rules=[dict(r) for r in RULES],
        weight_overrides={"r_geo": 50},
    )
    assert report["baseline"]["score"] == 60  # 22 + 28 + 10
    assert report["variant"]["score"] == 82  # 22 + 50 + 10
    assert report["delta"]["score"] == 22
    assert report["summary"]["affected_rule_count"] >= 1
    changes = {c["rule_id"]: c["change"] for c in report["changed_factors"]}
    assert changes["r_geo"].startswith("weight")


def test_risk_whatif_disable_rule_and_level_retune():
    disabled = simulation_engine.simulate_risk_whatif(
        CONTEXT,
        base_rules=[dict(r) for r in RULES],
        disabled_rules=["r_new_device", "r_geo"],
    )
    assert disabled["baseline"]["score"] == 60
    assert disabled["variant"]["score"] == 10
    assert disabled["summary"]["affected_rule_count"] == 2

    retuned = simulation_engine.simulate_risk_whatif(
        {"geo_velocity_kmh": 900},
        base_rules=[dict(r) for r in RULES],
        level_max_overrides={"low": 50},
    )
    assert retuned["baseline"]["level"] == "moderate"  # score 28 vs low-cap 25
    assert retuned["variant"]["level"] == "low"
    assert retuned["delta"]["level_flip"] is True


def test_risk_whatif_is_pure():
    risk_evaluator.reset_weight_state()
    before = risk_evaluator.current_weights()
    simulation_engine.simulate_risk_whatif(
        {"device_proven": False},
        base_rules=[dict(r) for r in RULES],
        weight_overrides={"r_new_device": 100},
    )
    assert risk_evaluator.current_weights() == before  # live weights untouched


def test_retention_whatif_flips_actions_both_directions():
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    policies = [
        {"policy_id": "audit_log_monthly", "table": "audit_log_entries", "interval": "monthly", "retention_days": 90, "enabled": True},
        {"policy_id": "security_events_monthly", "table": "security_events", "interval": "monthly", "retention_days": 180, "enabled": True},
    ]
    partitions = [
        {"policy_id": "audit_log_monthly", "table": "audit_log_entries", "key": "2026-07", "interval": "monthly"},
        {"policy_id": "security_events_monthly", "table": "security_events", "key": "2026-02", "interval": "monthly"},
    ]
    report = simulation_engine.simulate_retention_whatif(
        policies,
        partitions,
        moment=now,
        retention_overrides={"audit_log_monthly": 30, "security_events_monthly": 365},
    )
    assert report["changed_count"] == 2
    rows = {p["policy_id"]: p for p in report["partitions"]}
    assert rows["audit_log_monthly"]["age_days"] < 90
    assert (rows["audit_log_monthly"]["baseline_action"], rows["audit_log_monthly"]["variant_action"]) == ("keep", "drop")
    assert rows["security_events_monthly"]["age_days"] > 180
    assert (rows["security_events_monthly"]["baseline_action"], rows["security_events_monthly"]["variant_action"]) == ("drop", "keep")


def test_retention_whatif_is_pure():
    from app.partition_manager import PARTITION_POLICIES

    before = [dict(p) for p in PARTITION_POLICIES]
    simulation_engine.simulate_retention_whatif(
        before, [], retention_overrides={"audit_log_monthly": 7}
    )
    assert [p["retention_days"] for p in PARTITION_POLICIES] == [
        p["retention_days"] for p in before
    ]


def test_run_simulation_dispatch_records_trace_and_validates_kind():
    store = explainability.get_default_trace_store()
    before = store.stats()["total"]
    report = simulation_engine.run_simulation(
        "risk",
        context={"device_proven": False},
        base_rules=[dict(r) for r in RULES],
        weight_overrides={"r_new_device": 60},
        label="sim-label",
    )
    assert report["decision_type"] == "risk_score" and report["scenario"] == "sim-label"
    assert store.stats()["total"] == before + 1
    with pytest.raises(ValueError):
        simulation_engine.run_simulation("not_a_kind")


# --- metadata surfaces -------------------------------------------------------


def test_meta_decisions_documents_surfaces():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.get("/meta/decisions")
    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload) >= {
        "model_versioning",
        "simulation",
        "explainability",
        "optimistic_locking",
    }
    assert "risk_rules" in {m["name"] for m in payload["model_versioning"]["models"]}
    assert payload["simulation"]["kinds"] == ["risk", "retention"]
    assert payload["optimistic_locking"]["conflict_policy"]["expected_status"] == 409
    assert payload["explainability"]["store"]["total"] >= 0

    ecosystem = client.get("/meta/ecosystem").json()["subservices"]
    assert ecosystem["decision_intelligence"]["status"] == "ready"
    assert ecosystem["decision_intelligence"]["canary_auto_promote"] is False

    features = client.get("/meta/features").json()["endpoints"]
    assert features["decisions"] == "/meta/decisions"
    assert features["decision_canary_promote"] == "/meta/decisions/canary/promote"

    scoring = client.get("/meta/scoring-catalog").json()["decision_intelligence"]
    assert set(scoring) == {
        "model_versioning",
        "simulation",
        "explainability",
        "optimistic_locking",
    }


def test_simulate_endpoint_risk_and_retention():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    risk = client.post(
        "/meta/decisions/simulate",
        json={"kind": "risk", "context": {"device_proven": False}, "weight_overrides": {"new_device": 60}},
    )
    assert risk.status_code == 200
    body = risk.json()
    assert body["decision_type"] == "risk_score"
    assert body["variant"]["score"] > body["baseline"]["score"]

    retention = client.post("/meta/decisions/simulate", json={"kind": "retention"})
    assert retention.status_code == 200
    assert retention.json()["decision_type"] == "retention_action"

    bad = client.post("/meta/decisions/simulate", json={"kind": "nope"})
    assert bad.status_code == 422


def test_canary_endpoints_run_and_promote_with_optimistic_conflict():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    registry = model_versioning.get_default_registry()
    active = registry.active_snapshot("risk_rules")
    candidate = [dict(r) for r in active["rules"]]
    candidate[0]["weight"] = float(candidate[0]["weight"]) + 10
    registry.add_version(
        "risk_rules",
        {"rules": candidate, "levels": active["levels"]},
        label="endpoint-canary",
    )
    guard = registry.guard_version("risk_rules")

    run = client.post(
        "/meta/decisions/canary/run",
        json={"model_name": "risk_rules", "context": {"device_proven": False}, "entity_ref": "endpoint:canary"},
    )
    assert run.status_code == 200
    body = run.json()
    assert body["served"] and body["shadow"] and "confidence" in body

    promote = client.post("/meta/decisions/canary/promote", json={"model_name": "risk_rules"})
    assert promote.status_code == 200
    promoted = promote.json()
    assert promoted["to_version"] == promoted["from_version"] + 1

    # stale expected_version -> 409 conflict, no silent clobber
    conflict = client.post(
        "/meta/decisions/canary/promote",
        json={"model_name": "risk_rules", "expected_version": guard},
    )
    assert conflict.status_code == 409

    # restore a clean default registry for sibling tests
    model_versioning.set_default_registry(model_versioning.seed_default_registry())


def test_explanations_endpoint_list_and_detail():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    store = explainability.get_default_trace_store()
    did = explainability.record_decision(
        decision_type="simulation", entity_ref="endpoint:trace-test", outcome="noop"
    )
    listing = client.get(
        "/meta/decisions/explanations", params={"entity_ref": "endpoint:trace-test"}
    ).json()
    assert listing["traces"][0]["decision_id"] == did

    detail = client.get(f"/meta/decisions/explanations/{did}")
    assert detail.status_code == 200
    assert detail.json()["decision_id"] == did

    missing = client.get("/meta/decisions/explanations/not-a-real-id")
    assert missing.status_code == 404
    assert store.get(did) is not None  # untouched by reads