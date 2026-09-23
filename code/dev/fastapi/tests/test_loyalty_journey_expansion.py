"""Tests for the loyalty-journey expansion (new -> loyal next-best-action engine).

The backend now hosts a data-driven journey engine in
`app.services.loyalty_journey`:

1. `LOYALTY_SCENARIO_CATALOG` is a config table encoding the hypothesized
   scenarios that turn a new customer into a loyal one (onboarding, activation,
   delivery, habit, recovery, retention, churn, trust, monetization, data).
2. `match_loyalty_scenarios` evaluates each rule's `when` DSL against a
   `JourneyContext` — adding a scenario is a config change, not a code change.
3. `/chat/loyalty-journey` (self) and `/chat/admin/loyalty-journey` (admin)
   expose per-user plans and an operations rollup.
4. `/meta/scoring-catalog` exposes the live journey-scenario rules so future
   surfaces can validate/display them.

Existing endpoints and scores must remain byte-identical; these tests pin the
scenario-matching behavior only.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import deps
from app.main import app
from app.services import loyalty_journey
from app.services.loyalty_journey import (
    JourneyContext,
    LOYALTY_SCENARIO_CATALOG,
    _matches_rule,
    build_journey_context,
    build_loyalty_journey_plan,
    build_loyalty_scenario_catalog,
    match_loyalty_scenarios,
)
from app.schemas.chat import ChurnPrediction, InteractionSummary, Sentiment

NOW = datetime.now(timezone.utc)


def _churn(risk_level="low", risk_score=0.0) -> ChurnPrediction:
    return ChurnPrediction(
        user_id=1,
        risk_score=risk_score,
        risk_level=risk_level,
        warning_reasons=[],
        next_best_actions=[],
        generated_at=NOW,
    )


def _summary(
    *,
    user_id=1,
    churn_risk="low",
    loyalty_score=90.0,
    monetization_readiness=60.0,
    value_tier="standard",
    classification="stable value",
    top_issues=None,
    insights=None,
    repeated_messages=0,
    booking_states=None,
    signal_strength=0.0,
) -> InteractionSummary:
    return InteractionSummary(
        user_id=user_id,
        messages_analyzed=0,
        bookings_analyzed=0,
        churn_risk=churn_risk,
        loyalty_score=loyalty_score,
        monetization_readiness=monetization_readiness,
        value_tier=value_tier,
        customer_classification=classification,
        top_issues=top_issues or [],
        strengths=["test fixture"],
        insights=insights or [],
        metadata={
            "repeated_messages": repeated_messages,
            "booking_states": booking_states or {},
            "signal_strength": signal_strength,
        },
        generated_at=NOW,
    )


class FakeChatRow:
    def __init__(self, message):
        self.message = message
        self.timestamp = NOW
        self.user_id = 1


class FakeBooking:
    def __init__(self, status):
        self.status = status
        self.id = 1
        self.user_id = 1
        self.created_at = NOW


# --- catalog shape / self-check ----------------------------------------------


def test_loyalty_scenario_catalog_shape_and_context_contract():
    ids = []
    families = set()
    for rule in LOYALTY_SCENARIO_CATALOG:
        required = {"scenario", "family", "goal", "priority", "owner_hint", "when", "actions", "kpis", "when_hint"}
        assert required <= set(rule), f"scenario missing keys: {required - set(rule)}"
        assert rule["scenario"] not in ids, f"duplicate scenario id: {rule['scenario']}"
        ids.append(rule["scenario"])
        assert rule["priority"] in {"high", "medium", "low"}
        assert rule["actions"] and rule["kpis"] and rule["when"]
        assert isinstance(rule["when"], dict)
        # Every condition field must be a real JourneyContext field (fail-safe at runtime).
        for field in rule["when"]:
            assert field in JourneyContext.__dataclass_fields__, (
                f"scenario {rule['scenario']} references unknown context field {field!r}"
            )
        families.add(rule["family"])
    assert len(ids) == len(set(ids))
    assert len(ids) >= 15
    assert len(families) >= 8
    assert "onboarding" in families and "recovery" in families and "monetization" in families


def test_scenario_conditions_cover_the_whole_customer_journey():
    scenarios = {str(rule["scenario"]) for rule in LOYALTY_SCENARIO_CATALOG}
    # The hypothesized new -> loyal pathway families must all be represented.
    assert {"discovery_and_welcome", "first_value_activation", "first_booking_pending", "delivery_in_flight",
            "post_service_delight", "completion_to_return", "service_recovery", "negative_sentiment_recovery",
            "repeated_concern_loop", "follow_up_gap", "dormancy_winback", "churn_risk_save", "trust_building",
            "expansion_ready", "growth_cross_sell", "data_sufficiency"} <= scenarios


# --- condition DSL -----------------------------------------------------------


def test_when_dsl_numeric_list_and_scalar_operators():
    assert _matches_rule({"gte": 1}, 2) is True
    assert _matches_rule({"gte": 1}, 0) is False
    assert _matches_rule({"lte": 0}, 0) is True
    assert _matches_rule({"lte": 0}, -1) is True
    assert _matches_rule(["new", "engaged"], "engaged") is True
    assert _matches_rule(["new", "engaged"], "loyal") is False
    assert _matches_rule("negative", "negative") is True
    assert _matches_rule("negative", "positive") is False
    assert _matches_rule("follow up", ["follow up", "pricing"]) is True
    assert _matches_rule("follow up", ["pricing"]) is False
    assert _matches_rule({"gte": 5}, None) is False


# --- journey context ---------------------------------------------------------


def test_build_journey_context_new_user():
    summary = _summary(loyalty_score=100.0, monetization_readiness=100.0)
    context = build_journey_context(summary, _churn(), [], [], None)
    assert context.lifecycle_stage == "new"
    assert context.has_history is False
    assert context.dormant is False
    assert context.days_since_last_activity is None
    assert context.signal_total == 0
    assert context.completed_bookings == 0
    assert context.sentiment_label == "none"


def test_build_journey_context_dormant_customer():
    summary = _summary()
    latest_booking_at = NOW - timedelta(days=40)
    context = build_journey_context(
        summary,
        _churn(),
        [],
        [],
        None,
        total_chat_count=4,
        total_booking_count=5,
        latest_chat_at=NOW - timedelta(days=45),
        latest_booking_at=latest_booking_at,
    )
    assert context.has_history is True
    assert context.dormant is True
    assert context.had_bookings is True
    assert context.days_since_last_activity == 40


# --- scenario matching -------------------------------------------------------


def test_new_user_matches_welcome_and_data_sufficiency_only():
    summary = _summary(loyalty_score=100.0, monetization_readiness=100.0)
    context = build_journey_context(summary, _churn(), [], [], None)
    matched = {str(rule["scenario"]) for rule in match_loyalty_scenarios(context)}
    assert matched == {"discovery_and_welcome", "data_sufficiency"}


def test_at_risk_user_with_cancellations_matches_recovery_set():
    summary = _summary(
        churn_risk="high",
        loyalty_score=38.0,
        monetization_readiness=30.0,
        repeated_messages=2,
        booking_states={"pending": 1, "cancelled": 1, "completed": 0},
    )
    chat_rows = [FakeChatRow("I keep having problems"), FakeChatRow("still broken"), FakeChatRow("so frustrated")]
    bookings = [FakeBooking("pending"), FakeBooking("cancelled")]
    context = build_journey_context(summary, _churn(risk_level="critical", risk_score=82.0), chat_rows, bookings, Sentiment(label="negative", score=0.9))
    matched = {str(rule["scenario"]) for rule in match_loyalty_scenarios(context)}
    assert {
        "negative_sentiment_recovery",
        "service_recovery",
        "trust_building",
        "churn_risk_save",
        "first_booking_pending",
        "booking_abandonment_risk",
        "repeated_concern_loop",
    } <= matched
    assert "data_sufficiency" not in matched  # enough signal; avoid placeholder
    assert "discovery_and_welcome" not in matched


def test_loyal_high_value_customer_matches_expansion_set():
    summary = _summary(
        loyalty_score=92.0,
        monetization_readiness=88.0,
        value_tier="premium",
        classification="loyal high-value",
        booking_states={"completed": 3},
    )
    chat_rows = [FakeChatRow(m) for m in ("great", "thank you", "loved it", "recommend", "again soon", "excellent")]
    bookings = [FakeBooking("completed") for _ in range(3)]
    context = build_journey_context(summary, _churn(risk_level="low"), chat_rows, bookings, Sentiment(label="positive", score=0.95))
    matched = {str(rule["scenario"]) for rule in match_loyalty_scenarios(context)}
    assert {
        "post_service_delight",
        "completion_to_return",
        "growth_cross_sell",
        "expansion_ready",
    } <= matched
    assert "churn_risk_save" not in matched
    assert "data_sufficiency" not in matched
    assert "discovery_and_welcome" not in matched


def test_dormant_customer_with_history_matches_winback():
    summary = _summary(booking_states={"completed": 0})
    context = build_journey_context(
        summary,
        _churn(),
        [],
        [],
        None,
        total_chat_count=4,
        total_booking_count=5,
        latest_booking_at=NOW - timedelta(days=40),
    )
    matched = {str(rule["scenario"]) for rule in match_loyalty_scenarios(context)}
    assert "dormancy_winback" in matched
    assert "discovery_and_welcome" not in matched


def test_pending_first_booking_matches_activation_but_not_recovery():
    summary = _summary(booking_states={"pending": 1, "completed": 0})
    chat_rows = [FakeChatRow("how do I confirm my booking")]
    bookings = [FakeBooking("pending")]
    context = build_journey_context(summary, _churn(), chat_rows, bookings, None)
    matched = {str(rule["scenario"]) for rule in match_loyalty_scenarios(context)}
    assert "first_booking_pending" in matched
    assert "delivery_in_flight" not in matched  # confirmed, not pending
    assert "churn_risk_save" not in matched


# --- plan builder ------------------------------------------------------------


def test_build_loyalty_journey_plan_structure_and_ordering():
    summary = _summary(
        churn_risk="high",
        repeated_messages=2,
        booking_states={"pending": 1, "cancelled": 1},
    )
    chat_rows = [FakeChatRow(m) for m in ("help", "help", "still waiting", "so slow")]
    bookings = [FakeBooking("pending"), FakeBooking("cancelled")]
    context = build_journey_context(
        summary, _churn(risk_level="critical", risk_score=75.0), chat_rows, bookings, Sentiment(label="negative", score=0.85)
    )
    plan = build_loyalty_journey_plan(1, 30, summary, _churn(risk_level="critical", risk_score=75.0), context)

    assert plan.user_id == 1
    assert plan.window_days == 30
    assert plan.matched_scenario_count == len(plan.scenario_items) > 0
    assert plan.scenario_families == list(dict.fromkeys(plan.scenario_families))
    assert plan.next_best_actions
    # High-priority scenarios sort before medium/low.
    priorities = [item.priority for item in plan.scenario_items]
    assert priorities == sorted(priorities, key=loyalty_journey.PRIORITY_RANK.__getitem__)
    assert priorities[0] == "high"
    # Every scenario item surfaces evidence derived from matched conditions.
    for item in plan.scenario_items:
        assert item.actions
        assert item.matched_conditions
        assert item.evidence
    assert "stage=" in plan.summary_text
    assert f"matched={plan.matched_scenario_count}" in plan.summary_text


def test_plan_dedupes_next_best_actions():
    summary = _summary(booking_states={"completed": 3}, loyalty_score=92.0, monetization_readiness=88.0)
    chat_rows = [FakeChatRow(m) for m in ("great", "thank you", "again please", "recommended")]
    bookings = [FakeBooking("completed") for _ in range(3)]
    context = build_journey_context(summary, _churn(), chat_rows, bookings, Sentiment(label="positive", score=0.9))
    plan = build_loyalty_journey_plan(1, 30, summary, _churn(), context)
    assert len(plan.next_best_actions) == len(set(plan.next_best_actions))
    assert len(plan.next_best_actions) <= 7


# --- catalog discovery -------------------------------------------------------


def test_build_loyalty_scenario_catalog_is_json_safe_and_complete():
    catalog = build_loyalty_scenario_catalog()
    assert catalog["catalog_version"] == "loyalty_journey_v1"
    assert catalog["total_scenarios"] == len(LOYALTY_SCENARIO_CATALOG)
    assert catalog["families"]
    assert catalog["context_fields"]  # the JourneyContext contract is exposed
    scenario_ids = {entry["scenario"] for entry in catalog["scenarios"]}
    assert scenario_ids == {rule["scenario"] for rule in LOYALTY_SCENARIO_CATALOG}
    for entry in catalog["scenarios"]:
        assert entry["condition_fields"] == sorted(entry["when"].keys())
        assert set(entry["condition_fields"]) <= set(catalog["context_fields"])


def test_build_loyalty_scenario_catalog_picks_up_attached_scenario(monkeypatch):
    attached = {
        "scenario": "same_day_urgency",
        "family": "activation",
        "goal": "Fast-track same-day booking changes.",
        "priority": "high",
        "owner_hint": "service operations",
        "when": {"pending_bookings": {"gte": 1}, "chat_count": {"gte": 3}},
        "actions": ["Fast-path urgent changes to a human queue"],
        "kpis": ["same-day resolution time"],
        "when_hint": "repeated contact around an urgent booking",
    }
    monkeypatch.setattr(
        loyalty_journey,
        "LOYALTY_SCENARIO_CATALOG",
        list(loyalty_journey.LOYALTY_SCENARIO_CATALOG) + [attached],
    )
    catalog = build_loyalty_scenario_catalog()
    ids = {entry["scenario"] for entry in catalog["scenarios"]}
    assert "same_day_urgency" in ids
    # The engine itself matches the new config with no code changes.
    summary = _summary(booking_states={"pending": 1})
    chat_rows = [FakeChatRow(m) for m in ("urgent", "urgent", "urgent")]
    context = build_journey_context(summary, _churn(), chat_rows, [FakeBooking("pending")], None)
    assert "same_day_urgency" in {str(rule["scenario"]) for rule in match_loyalty_scenarios(context)}


# --- meta endpoint -----------------------------------------------------------


def test_meta_scoring_catalog_includes_loyalty_scenarios():
    response = TestClient(app).get("/meta/scoring-catalog")
    assert response.status_code == 200
    payload = response.json()
    assert payload["area_scoring"]
    assert payload["policy_tiers"]["tier_rules"]
    loyalty = payload["loyalty_scenarios"]
    assert loyalty["total_scenarios"] == len(LOYALTY_SCENARIO_CATALOG)
    assert loyalty["families"]
    assert any(entry["scenario"] == "discovery_and_welcome" for entry in loyalty["scenarios"])


def test_meta_features_documents_loyalty_journey():
    response = TestClient(app).get("/meta/features")
    assert response.status_code == 200
    endpoints = response.json()["endpoints"]
    assert endpoints["loyalty_journey"] == "/chat/loyalty-journey"
    assert endpoints["loyalty_admin_journey"] == "/chat/admin/loyalty-journey"


# --- endpoint wiring ---------------------------------------------------------


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None


class _EmptyDb:
    async def execute(self, *_args, **_kwargs):
        return _EmptyResult()


class _FakeUser:
    id = 1
    username = "tester"
    is_admin = True


async def _fake_get_db():
    yield _EmptyDb()


def test_loyalty_journey_endpoint_for_new_user():
    async def _fake_get_current_user():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/chat/loyalty-journey")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == 1
    assert payload["window_days"] == 30
    assert payload["lifecycle_stage"] == "new"
    scenarios = {item["scenario"] for item in payload["scenario_items"]}
    assert "discovery_and_welcome" in scenarios
    assert payload["next_best_actions"]
    assert "stage=new" in payload["summary_text"]


def test_loyalty_journey_admin_endpoint_empty_rollup():
    async def _fake_get_current_admin_user():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/loyalty-journey")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["total_users"] == 0
    assert payload["total_matched"] == 0
    assert payload["users"] == []
    assert payload["coverage_by_family"] == {}


def test_loyalty_journey_window_must_be_at_least_seven_days():
    async def _fake_get_current_user():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/chat/loyalty-journey", params={"window_days": 1})
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 422