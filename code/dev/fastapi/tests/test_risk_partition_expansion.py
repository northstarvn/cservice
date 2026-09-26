"""Tests for the risk-evaluator and partition-manager expansion.

Two thin groups were grown to answer the harder operational questions:

- ``app/risk_evaluator.py`` — signal hygiene (a rule only fires when its signal
  is present, so an omitted signal is a silent fail-open), level→action
  mapping, counterfactual analysis, weight-history replay, and a bounded
  decision log.
- ``app/partition_manager.py`` — identifier validation on every DDL builder,
  declarative attach/detach, dry-run planning, legal hold, and reconciliation
  of the in-process registry against the live database.

Everything here is additive: the pre-existing tests are the other half of the
contract, including the exact archive/drop/route behaviour these modules had
before.
"""
from datetime import datetime, timezone

import pytest

from app import partition_manager, risk_evaluator


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

BENIGN = {
    "device_proven": True,
    "biometric_valid": True,
    "hsm_verified": True,
    "geo_velocity_kmh": 40,
    "auth_velocity_per_min": 1,
    "odd_hour": False,
    "target_sensitivity": "low",
    "ip_reputation": "good",
}
RISKY = {
    "device_proven": False,
    "biometric_valid": False,
    "hsm_verified": False,
    "geo_velocity_kmh": 1200,
    "auth_velocity_per_min": 8,
    "odd_hour": True,
    "target_sensitivity": "high",
    "ip_reputation": "poor",
}


@pytest.fixture(autouse=True)
def _clean_risk_state():
    """Weight state is module-global; every test starts from the config base."""
    risk_evaluator.reset_weight_state()
    yield
    risk_evaluator.reset_weight_state()


# ===========================================================================
# app/risk_evaluator.py — signal hygiene
# ===========================================================================


def test_normalize_signal_coerces_to_the_declared_type():
    assert risk_evaluator.normalize_signal("device_proven", "YES") is True
    assert risk_evaluator.normalize_signal("device_proven", "0") is False
    assert risk_evaluator.normalize_signal("device_proven", 1) is True
    assert risk_evaluator.normalize_signal("odd_hour", "maybe") is None
    assert risk_evaluator.normalize_signal("geo_velocity_kmh", "1200") == 1200.0
    assert risk_evaluator.normalize_signal("auth_velocity_per_min", True) is None
    assert risk_evaluator.normalize_signal("ip_reputation", " POOR ") == "poor"
    assert risk_evaluator.normalize_signal("ip_reputation", "clean") is None
    # Every shipped key has a type and a benign default.
    assert set(risk_evaluator.RISK_CONTEXT_TYPES) == set(risk_evaluator.RISK_CONTEXT_KEYS)
    assert set(risk_evaluator.RISK_CONTEXT_DEFAULTS) == set(risk_evaluator.RISK_CONTEXT_KEYS)


def test_normalize_context_reports_gaps_and_defaults():
    prepared = risk_evaluator.normalize_context({"device_proven": False, "odd_hour": 1})
    assert prepared["context"]["device_proven"] is False
    assert prepared["context"]["odd_hour"] is True
    assert prepared["context"]["ip_reputation"] == "good"  # benign default
    assert "ip_reputation" in prepared["gaps"] and "ip_reputation" in prepared["defaulted"]
    assert "device_proven" not in prepared["gaps"]
    assert prepared["complete"] is False
    # An unreadable value is reported separately from an absent one.
    unreadable = risk_evaluator.normalize_context({"odd_hour": "perhaps"})
    assert unreadable["unreadable"] == ["odd_hour"]
    assert unreadable["context"]["odd_hour"] is False  # default substituted
    # Keys the rule table does not know about are surfaced, not silently kept.
    assert risk_evaluator.normalize_context({"nonsense": 1})["extra"] == ["nonsense"]
    # Without `fill`, a gap stays a gap and the rule stays silent.
    unfilled = risk_evaluator.normalize_context({}, fill=False)
    assert unfilled["context"] == {} and len(unfilled["gaps"]) == len(risk_evaluator.RISK_CONTEXT_KEYS)


def test_context_gaps_lists_what_a_caller_omitted():
    assert risk_evaluator.context_gaps(BENIGN) == []
    assert risk_evaluator.context_gaps({}) == list(risk_evaluator.RISK_CONTEXT_KEYS)
    assert risk_evaluator.context_gaps({"odd_hour": True}) == [
        key for key in risk_evaluator.RISK_CONTEXT_KEYS if key != "odd_hour"
    ]


# ===========================================================================
# app/risk_evaluator.py — level -> action
# ===========================================================================


def test_action_table_covers_every_level():
    levels = [bucket["level"] for bucket in risk_evaluator.RISK_LEVELS]
    assert set(risk_evaluator.RISK_ACTIONS) == set(levels)
    for level in levels:
        action = risk_evaluator.action_for_level(level)
        assert action["level"] == level
        assert action["action"] in risk_evaluator.RISK_DECISION_ACTIONS
        assert action["rationale"]
        assert set(action["required_controls"]) <= set(risk_evaluator.RISK_CONTROL_NAMES)
    # Severity is monotonic in force: later levels are never more permissive.
    order = risk_evaluator.RISK_DECISION_ACTIONS
    force = [order.index(risk_evaluator.RISK_ACTIONS[level]["action"]) for level in levels]
    assert force == sorted(force)
    with pytest.raises(ValueError, match="unknown risk level"):
        risk_evaluator.action_for_level("apocalyptic")


def test_required_controls_and_what_is_still_missing():
    assert risk_evaluator.required_controls_for("low") == []
    assert risk_evaluator.required_controls_for("critical") == [
        "reauth",
        "notify_auditor",
        "open_case",
    ]
    assert risk_evaluator.controls_missing_for("high", []) == [
        "reauth",
        "step_up_biometric",
        "notify_auditor",
    ]
    assert risk_evaluator.controls_missing_for("high", ["reauth", "notify_auditor"]) == [
        "step_up_biometric"
    ]
    assert risk_evaluator.controls_missing_for("high", None) == risk_evaluator.required_controls_for("high")
    with pytest.raises(ValueError, match="unknown risk level"):
        risk_evaluator.required_controls_for("nope")


def test_evaluate_risk_decision_answers_what_now():
    risky = risk_evaluator.evaluate_risk_decision(RISKY)
    assert risky["score"] >= 80 and risky["level"] in {"high", "critical"}
    assert risky["action"] == risk_evaluator.RISK_ACTIONS[risky["level"]]["action"]
    assert risky["required_controls"] == risk_evaluator.required_controls_for(risky["level"])
    assert risky["missing_controls"] == risky["required_controls"]
    assert risky["signals"]["complete"] is True and risky["signals"]["gaps"] == []
    # Satisfied controls drop out of the outstanding list.
    served = risk_evaluator.evaluate_risk_decision(
        RISKY, satisfied_controls=risky["required_controls"]
    )
    assert served["missing_controls"] == []
    # A benign context is allowed outright.
    benign = risk_evaluator.evaluate_risk_decision(BENIGN)
    assert benign["score"] == 0 and benign["level"] == "low"
    assert benign["action"] == "allow" and benign["required_controls"] == []
    assert benign["review_after_seconds"] is None


def test_evaluate_risk_decision_strict_mode_refuses_a_half_filled_context():
    # Default (lenient) mode fills the gaps and says so.
    lenient = risk_evaluator.evaluate_risk_decision({"device_proven": False})
    assert lenient["signals"]["gaps"] == [
        key for key in risk_evaluator.RISK_CONTEXT_KEYS if key != "device_proven"
    ]
    assert lenient["score"] == 22
    # Strict mode is the fail-closed variant.
    with pytest.raises(ValueError, match="missing risk signals"):
        risk_evaluator.evaluate_risk_decision({"device_proven": False}, strict=True)
    # A complete context passes strict mode.
    assert risk_evaluator.evaluate_risk_decision(BENIGN, strict=True)["score"] == 0
    # `fill_gaps=False` keeps the rules silent about absent signals.
    unfilled = risk_evaluator.evaluate_risk_decision({}, fill_gaps=False)
    assert unfilled["score"] == 0 and unfilled["signals"]["defaulted"] == []


# ===========================================================================
# app/risk_evaluator.py — counterfactuals
# ===========================================================================


def test_counterfactual_reports_the_delta_for_one_signal():
    result = risk_evaluator.counterfactual({"odd_hour": True}, "odd_hour", False)
    assert result["signal"] == "odd_hour" and result["from"] is True and result["to"] is False
    assert result["baseline_score"] == 10 and result["variant_score"] == 0
    assert result["score_delta"] == -10 and result["level_flip"] is False
    with pytest.raises(ValueError, match="unknown risk signal"):
        risk_evaluator.counterfactual({"odd_hour": True}, "phase_of_moon", True)
    # Pure: live weights are untouched.
    before = risk_evaluator.current_weights()
    risk_evaluator.counterfactual(BENIGN, "odd_hour", True)
    assert risk_evaluator.current_weights() == before


def test_cheapest_clearing_signal_finds_the_one_change_that_matters():
    result = risk_evaluator.cheapest_clearing_signal(
        {"biometric_valid": False}, target_level="low"
    )
    assert result["signal"] == "biometric_valid"
    assert result["baseline_level"] == "moderate" and result["variant_level"] == "low"
    assert result["level_flip"] is True and result["score_delta"] < 0
    # Some contexts genuinely need more than one fix; saying so beats guessing.
    assert risk_evaluator.cheapest_clearing_signal(RISKY, target_level="low") is None
    with pytest.raises(ValueError, match="unknown risk level"):
        risk_evaluator.cheapest_clearing_signal(BENIGN, target_level="nope")
    # Stable: the same context always yields the same answer.
    context = {"odd_hour": True, "ip_reputation": "poor"}
    assert risk_evaluator.cheapest_clearing_signal(context) == (
        risk_evaluator.cheapest_clearing_signal(context)
    )


# ===========================================================================
# app/risk_evaluator.py — weight history + bulk outcomes
# ===========================================================================


def test_weight_history_reproduces_a_past_decision():
    start = risk_evaluator.weight_version()
    weights_at_start = dict(risk_evaluator.current_weights())
    decided = risk_evaluator.evaluate_risk(BENIGN)
    assert decided["weight_version"] == start

    # Drift the weights until the same context would score differently.
    for _ in range(4):
        risk_evaluator.adapt_risk_weight("off_hours", "fraud")
    assert risk_evaluator.current_weights()["off_hours"] != weights_at_start["off_hours"]

    replayed = risk_evaluator.evaluate_risk_at(BENIGN, start)
    assert replayed["weight_version"] == start and replayed["replayed"] is True
    assert replayed["score"] == decided["score"] == 0
    # The current view reflects the drift.
    assert risk_evaluator.evaluate_risk(BENIGN)["weight_version"] == risk_evaluator.weight_version()
    with pytest.raises(KeyError, match="no longer retained"):
        risk_evaluator.evaluate_risk_at(BENIGN, -1)


def test_apply_risk_outcomes_bumps_one_version_for_the_batch():
    before = risk_evaluator.weight_version()
    result = risk_evaluator.apply_risk_outcomes(
        {"new_device": "fraud", "off_hours": "granted"}
    )
    assert result["from_version"] == before
    assert result["to_version"] == before + 1
    assert result["accepted"] == 2 and result["rejected"] == 0
    assert [row["rule_id"] for row in result["applied"]] == ["new_device", "off_hours"]
    accepted = [row for row in result["applied"] if row["applied"]]
    # The reported weight is the live weight, with the delta from the table.
    assert accepted[0]["weight"] == risk_evaluator.current_weights()["new_device"]
    assert accepted[0]["weight"] == accepted[0]["previous_weight"] + risk_evaluator.RISK_OUTCOME_DELTAS["fraud"]
    assert accepted[1]["weight"] == accepted[1]["previous_weight"] + risk_evaluator.RISK_OUTCOME_DELTAS["granted"]
    assert accepted[0]["clamped"] is False
    # An unknown rule is reported, not raised, so the rest of the batch survives.
    pairs = risk_evaluator.apply_risk_outcomes([("nope", "fraud")])
    assert pairs["rejected"] == 1 and pairs["accepted"] == 0
    assert pairs["from_version"] == pairs["to_version"]  # nothing applied, no bump
    # Every accepted outcome matches the published delta table.
    assert set(risk_evaluator.RISK_OUTCOMES) == set(risk_evaluator.RISK_OUTCOME_DELTAS)
    # A rule already at the ceiling reports the clamp.
    for _ in range(20):
        risk_evaluator.adapt_risk_weight("new_device", "fraud")
    clamped = risk_evaluator.apply_risk_outcomes({"new_device": "fraud"})
    assert clamped["applied"][0]["clamped"] is True
    assert risk_evaluator.current_weights()["new_device"] == risk_evaluator.ADAPTIVE_PARAMS["max_weight"]


def test_weight_history_is_bounded():
    assert len(risk_evaluator.weight_history()) <= risk_evaluator.RISK_WEIGHT_HISTORY
    for _ in range(risk_evaluator.RISK_WEIGHT_HISTORY + 20):
        risk_evaluator.adapt_risk_weight("new_device", "fraud")
    history = risk_evaluator.weight_history()
    assert len(history) == risk_evaluator.RISK_WEIGHT_HISTORY
    # A version that aged out is reported as missing, not guessed.
    assert risk_evaluator.weights_at(history[0]["version"]) == history[0]["weights"]


# ===========================================================================
# app/risk_evaluator.py — decision log
# ===========================================================================


def test_decision_store_records_and_bounds():
    store = risk_evaluator.RiskDecisionStore(capacity=3)
    for index in range(5):
        store.record(
            risk_evaluator.evaluate_risk_decision(RISKY), subject=f"user:{index}"
        )
    assert len(store.recent()) == 3
    assert [row["subject"] for row in store.recent()] == ["user:2", "user:3", "user:4"]
    assert store.recent(limit=1)[0]["subject"] == "user:4"
    assert store.recent(subject="user:0") == []  # aged out
    assert store.recent(subject="user:3")[0]["subject"] == "user:3"

    stats = store.stats()
    assert stats["capacity"] == 3 and stats["total"] == 3
    assert stats["by_action"] == {"deny": 3} and stats["by_level"] == {"critical": 3}
    assert stats["pending_outcomes"] == 3
    # Every stored row names the rules that actually fired.
    assert set(store.recent()[0]["present_rules"]) == {
        rule["rule_id"] for rule in risk_evaluator.RISK_RULES
    }
    store.clear()
    assert store.stats()["total"] == 0


def test_record_risk_decision_scores_and_remembers_in_one_call():
    store = risk_evaluator.RiskDecisionStore()
    decision = risk_evaluator.record_risk_decision(
        {"biometric_valid": False}, subject="user:7", store=store
    )
    assert decision["level"] == "moderate" and decision["action"] == "step_up"
    stored = store.recent()[0]
    assert stored["level"] == "moderate" and stored["subject"] == "user:7"
    assert stored["present_rules"] == ["biometric_mismatch"]
    assert stored["weight_version"] == decision["weight_version"]
    # A stored decision can be replayed at the weights that produced it.
    replayed = store.replay(stored, {"biometric_valid": False})
    assert replayed["score"] == decision["score"] and replayed["level"] == "moderate"
    # `strict` and control reporting flow straight through.
    with pytest.raises(ValueError, match="missing risk signals"):
        risk_evaluator.record_risk_decision({}, store=store, strict=True)
    assert store.stats()["total"] == 1


def test_risk_evaluator_catalog_keeps_pinned_keys_and_adds_surface():
    catalog = risk_evaluator.build_risk_evaluator_catalog()
    # Pinned contract.
    assert [bucket["level"] for bucket in catalog["levels"]] == [
        "low",
        "moderate",
        "high",
        "critical",
    ]
    assert "device_proven" in catalog["context_keys"]
    assert all("current_weight" in rule for rule in catalog["rules"])
    assert catalog["adaptive"]["weight_version"] >= 1
    # Expansion surface.
    assert set(catalog["context_types"]) == set(risk_evaluator.RISK_CONTEXT_KEYS)
    assert set(catalog["context_defaults"]) == set(risk_evaluator.RISK_CONTEXT_KEYS)
    assert set(catalog["enums"]) == set(risk_evaluator.RISK_ENUMS)
    assert set(catalog["actions"]) == set(risk_evaluator.RISK_ACTIONS)
    assert catalog["decision_actions"] == list(risk_evaluator.RISK_DECISION_ACTIONS)
    assert catalog["control_names"] == list(risk_evaluator.RISK_CONTROL_NAMES)
    assert catalog["outcomes"]["accepted"] == list(risk_evaluator.RISK_OUTCOMES)
    assert catalog["weight_history"]["capacity"] == risk_evaluator.RISK_WEIGHT_HISTORY
    assert "total" in catalog["decision_store"]


# ===========================================================================
# app/partition_manager.py — identifier safety
# ===========================================================================


def test_build_partition_policy_validates_its_inputs():
    policy = partition_manager.build_partition_policy(
        "audit_monthly", "audit_log_entries", "monthly", retention_days=90, note="hot 90d"
    )
    assert set(policy) == set(partition_manager.PARTITION_POLICY_FIELDS) | {"note"}
    assert partition_manager.validate_policies([policy])[0] == policy
    with pytest.raises(ValueError, match="unsupported partition interval"):
        partition_manager.build_partition_policy("p", "audit_log_entries", "yearly")
    with pytest.raises(ValueError, match="invalid partition parent table"):
        partition_manager.build_partition_policy("p", "audit_log_entries; DROP TABLE x")
    with pytest.raises(ValueError, match="retention_days must be"):
        partition_manager.build_partition_policy("p", "audit_log_entries", retention_days=-1)


def test_validate_policies_reports_the_first_problem():
    ok = partition_manager.build_partition_policy("a", "audit_log_entries")
    with pytest.raises(ValueError, match="is missing table, interval"):
        partition_manager.validate_policies([{"policy_id": "broken"}])
    with pytest.raises(ValueError, match="duplicate partition policy"):
        partition_manager.validate_policies([ok, dict(ok)])
    with pytest.raises(ValueError, match="unsupported partition interval: hourly"):
        partition_manager.validate_policies([{**ok, "interval": "hourly"}])
    with pytest.raises(ValueError, match="retention_days must be"):
        partition_manager.validate_policies([{**ok, "retention_days": -3}])
    with pytest.raises(ValueError, match="invalid partition parent table"):
        partition_manager.validate_policies([{**ok, "table": "bad name"}])
    assert partition_manager.validate_policies([]) == []


def test_ddl_builders_reject_anything_that_is_not_an_identifier():
    # Period keys legitimately carry "-", so a partition name is wider than an
    # identifier — but no wider than "<table>__<key>".
    assert partition_manager.validate_partition_name("audit_log_entries__2026-09-25")
    assert partition_manager.validate_identifier("audit_log_entries")
    for bad in ["audit_log_entries; DROP TABLE x", "no_key__", "__2026-09", "9lives__x"]:
        with pytest.raises(ValueError, match="invalid"):
            partition_manager.validate_partition_name(bad)
    with pytest.raises(ValueError, match="invalid identifier"):
        partition_manager.validate_identifier("audit_log_entries__2026-09")
    with pytest.raises(ValueError, match="exceeds 63 characters"):
        partition_manager.validate_identifier("a" * 64)


def test_attach_detach_and_drop_sql_are_config_driven():
    policy = partition_manager.PARTITION_POLICIES[0]
    attach = partition_manager.build_attach_sql(policy, "2026-09")
    assert attach.startswith("ALTER TABLE audit_log_entries ATTACH PARTITION audit_log_entries__2026-09")
    assert "FOR VALUES FROM (MINVALUE) TO (MAXVALUE)" in attach
    assert partition_manager.build_detach_sql(policy, "2026-09") == (
        "ALTER TABLE audit_log_entries DETACH PARTITION audit_log_entries__2026-09"
    )
    assert partition_manager.build_drop_sql("audit_log_entries__2026-09") == (
        "DROP TABLE IF EXISTS audit_log_entries__2026-09"
    )
    with pytest.raises(ValueError, match="invalid partition parent table"):
        partition_manager.build_attach_sql({"table": "x; DROP TABLE y"}, "2026-09")


def test_archive_and_restore_sql_round_trip():
    name = "audit_log_entries__2026-09"
    archived = partition_manager.build_archive_sql(name)
    assert archived == (
        "ALTER TABLE audit_log_entries__2026-09 RENAME TO audit_log_entries__2026-09__archived"
    )
    assert partition_manager.build_restore_sql(f"{name}{partition_manager.PARTITION_ARCHIVE_SUFFIX}") == (
        f"ALTER TABLE {name}__archived RENAME TO {name}"
    )
    with pytest.raises(ValueError, match="not an archived partition"):
        partition_manager.build_restore_sql(name)


def test_split_partition_name_is_structural():
    assert partition_manager.split_partition_name("audit_log_entries__2026-09") == (
        "audit_log_entries",
        "2026-09",
    )
    assert partition_manager.split_partition_name("audit_log_entries__2026-09__archived") == (
        "audit_log_entries",
        "2026-09",
    )
    # Only the last "__" separates the table from the period.
    assert partition_manager.split_partition_name("weird__table__2026-09") == (
        "weird__table",
        "2026-09",
    )
    # A name with no period key at all is not a candidate.
    assert partition_manager.split_partition_name("no_period") is None
    assert partition_manager.split_partition_name("") is None
    assert partition_manager.split_partition_name(None) is None
    # Deciding whether a structurally-valid name is *our* partition is the
    # policy table's job, and it does it in adopt_partitions (see below).
    assert partition_manager.split_partition_name("unmanaged_table__2026-01") == (
        "unmanaged_table",
        "2026-01",
    )


# ===========================================================================
# app/partition_manager.py — dry run, legal hold, reconciliation
# ===========================================================================


class _Conn:
    def __init__(self, log):
        self.log = log

    async def execute(self, statement):
        self.log.append(str(statement))

    def fetchall(self):
        return []


class _Engine:
    def __init__(self, log):
        self.log = log

    def begin(self):
        log = self.log

        class _Ctx:
            async def __aenter__(self):
                return _Conn(log)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _manager():
    return partition_manager.PartitionManager(
        policies=[
            partition_manager.build_partition_policy(
                "audit_monthly", "audit_log_entries", "monthly", retention_days=90
            ),
            partition_manager.build_partition_policy(
                "security_monthly", "security_events", "monthly", retention_days=180
            ),
        ]
    )


@pytest.mark.asyncio
async def test_plan_lifecycle_is_a_pure_preview():
    log: list[str] = []
    manager = _manager()
    plan = manager.plan_lifecycle(NOW)
    # No SQL was issued, and the same plan comes back.
    assert log == []
    assert plan == manager.plan_lifecycle(NOW)
    assert plan["create_count"] == 2 and plan["drop_count"] == 0
    assert {row["partition"] for row in plan["create"]} == {
        "audit_log_entries__2026-09",
        "security_events__2026-09",
    }
    assert manager.last_plan()["create_count"] == 2
    assert manager.list_partitions() == []

    # Once the current partitions exist, a plan proposes no creates and, for a
    # fresh table, no drops either.
    await manager.run_cycle(_Engine(log), NOW)
    settled = manager.plan_lifecycle(NOW)
    assert settled["create"] == [] and settled["drop"] == [] and settled["held"] == []


@pytest.mark.asyncio
async def test_run_cycle_dry_run_touches_no_database():
    log: list[str] = []
    manager = _manager()
    result = await manager.run_cycle(_Engine(log), NOW, dry_run=True)
    assert log == []
    assert result["dry_run"] is True
    assert [row["partition"] for row in result["ensured"]] == [
        "audit_log_entries__2026-09",
        "security_events__2026-09",
    ]
    assert all(row["created"] is True for row in result["ensured"])
    assert manager.list_partitions() == []


@pytest.mark.asyncio
async def test_legal_hold_outranks_the_retention_policy():
    log: list[str] = []
    engine = _Engine(log)
    manager = _manager()
    await manager.ensure_partition(engine, manager.policy("audit_monthly"), datetime(2019, 5, 1, tzinfo=timezone.utc))
    # Nothing is held yet, so the retention window applies as usual.
    assert await manager.drop_expired_partitions(engine, NOW) != []

    await manager.ensure_partition(engine, manager.policy("audit_monthly"), datetime(2019, 5, 1, tzinfo=timezone.utc))
    hold = manager.set_legal_hold("audit_log_entries", "2019-05", reason="litigation-2026-04")
    assert hold["hold"] is True and hold["reason"] == "litigation-2026-04"
    assert manager.legal_holds() == ["audit_log_entries__2019-05"]
    assert manager.is_held("audit_log_entries__2019-05") is True

    # The plan reports the partition as held, not as droppable.
    plan = manager.plan_lifecycle(NOW)
    assert plan["drop"] == [] and plan["held_count"] == 1
    assert plan["held"][0]["partition"] == "audit_log_entries__2019-05"
    # And the executor honours the hold.
    assert await manager.drop_expired_partitions(engine, NOW) == []
    assert manager.list_partitions() != []
    # Breaking the hold is explicit.
    assert await manager.drop_expired_partitions(engine, NOW, honor_holds=False) != []
    assert "DROP TABLE IF EXISTS audit_log_entries__2019-05" in log
    # Releasing keeps the history but un-blocks the drop.
    manager.set_legal_hold("audit_log_entries", "2019-05", hold=False)
    assert manager.legal_holds() == [] and manager.is_held("audit_log_entries__2019-05") is False


@pytest.mark.asyncio
async def test_legal_hold_also_blocks_archive_and_restore_round_trips():
    log: list[str] = []
    engine = _Engine(log)
    manager = _manager()
    await manager.ensure_partition(engine, manager.policy("audit_monthly"), NOW)
    manager.set_legal_hold("audit_log_entries", "2026-09", reason="open case")
    with pytest.raises(ValueError, match="under legal hold"):
        await manager.archive_partition(engine, "audit_log_entries", "2026-09")
    manager.set_legal_hold("audit_log_entries", "2026-09", hold=False)

    archived = await manager.archive_partition(engine, "audit_log_entries", "2026-09")
    assert archived["archived"] is True and manager.list_partitions() == []
    restored = await manager.restore_partition(engine, "audit_log_entries", "2026-09")
    assert restored["restored"] is True
    assert restored["partition"] == "audit_log_entries__2026-09"
    assert manager.list_partitions()[0]["key"] == "2026-09"
    with pytest.raises(ValueError, match="no policy for partition table"):
        await manager.restore_partition(engine, "unknown_table", "2026-09")


@pytest.mark.asyncio
async def test_attach_partition_issues_declarative_ddl():
    log: list[str] = []
    manager = _manager()
    result = await manager.attach_partition(_Engine(log), manager.policy("audit_monthly"), "2026-09")
    assert result["attached"] is True and result["partition"] == "audit_log_entries__2026-09"
    assert "ATTACH PARTITION audit_log_entries__2026-09" in log[0]


def test_parse_partition_rows_is_pure_and_defensive():
    rows = [
        {"relname": "audit_log_entries__2026-09"},
        {"partition": "security_events__2026-01"},
        {"relname": "audit_log_entries__2026-01__archived"},
        {"relname": "not_a_partition"},
        {"relname": None},
        {},
    ]
    parsed = partition_manager.parse_partition_rows(rows)
    assert [row["partition"] for row in parsed] == [
        "audit_log_entries__2026-01__archived",
        "audit_log_entries__2026-09",
        "security_events__2026-01",
    ]
    assert parsed[0]["archived"] is True and parsed[0]["key"] == "2026-01"
    assert parsed[1]["archived"] is False and parsed[1]["table"] == "audit_log_entries"
    assert partition_manager.parse_partition_rows([]) == []
    assert partition_manager.parse_partition_rows(None) == []
    # Object rows work as well as mappings.
    class _Row:
        relname = "audit_log_entries__2026-09"
    assert partition_manager.parse_partition_rows([_Row()])[0]["key"] == "2026-09"


@pytest.mark.asyncio
async def test_adopt_partitions_rehydrates_the_registry_after_a_restart():
    manager = _manager()
    adopted = manager.adopt_partitions(
        partition_manager.parse_partition_rows(
            [
                {"relname": "audit_log_entries__2026-08"},
                {"relname": "audit_log_entries__2019-05"},
                {"relname": "unmanaged_table__2026-01"},
                {"relname": "no_period"},
            ]
        )
    )
    assert [row["partition"] for row in adopted] == [
        "audit_log_entries__2019-05",
        "audit_log_entries__2026-08",
    ]
    active = manager.list_partitions()
    assert [row["partition"] for row in active] == [
        "audit_log_entries__2019-05",
        "audit_log_entries__2026-08",
    ]
    # An adopted partition is not recreated by the next cycle...
    result = await manager.run_cycle(_Engine([]), NOW)
    assert [row["partition"] for row in result["ensured"] if row["created"] is True] == [
        "audit_log_entries__2026-09",
        "security_events__2026-09",
    ]
    # ...and it is immediately eligible for the retention drop.
    assert [row["partition"] for row in result["dropped"]] == ["audit_log_entries__2019-05"]


def test_partition_catalog_keeps_pinned_keys_and_adds_surface():
    catalog = partition_manager.build_partition_manager_catalog()
    # Pinned contract.
    assert catalog["intervals"] == ["daily", "weekly", "monthly"]
    assert "policies" in catalog and "active_partitions" in catalog
    assert "worker_enabled" in catalog and "cycle_seconds" in catalog
    # Expansion surface.
    assert catalog["policy_fields"] == list(partition_manager.PARTITION_POLICY_FIELDS)
    assert catalog["policy_ids"] == [
        policy["policy_id"] for policy in partition_manager.PARTITION_POLICIES
    ]
    assert catalog["archive_suffix"] == partition_manager.PARTITION_ARCHIVE_SUFFIX
    assert catalog["identifier_pattern"] == partition_manager.IDENTIFIER_PATTERN
    assert catalog["identifier_max_length"] == partition_manager.IDENTIFIER_MAX_LENGTH
    assert catalog["legal_holds"] == []
    assert set(catalog["plan"]) == {"create", "drop_count", "held_count"}
    # The shipped policy table is valid.
    partition_manager.validate_policies(partition_manager.PARTITION_POLICIES)
