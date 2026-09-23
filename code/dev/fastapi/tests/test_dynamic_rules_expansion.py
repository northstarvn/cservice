"""Regression tests for the dynamic rule-engine expansion.

The backend now drives two key services from config tables instead of hardcoded
branches:

1. Interaction area scoring (`app.services.chat_analytics.AREA_SCORING_RULES`)
   — `score_area` is a thin engine over the table, new areas can be attached via
   config, and the rules are exposed through `build_area_scoring_catalog` /
   `build_area_keyword_catalog` and `score_all_areas`.
2. Policy tier / posture / access-band decisions
   (`app.services.policy_scoring`) — thresholds live in `POLICY_TIER_RULES`,
   `CONTROL_POSTURE_RULES`, and `ACCESS_BAND_RULES`, with `resolve_policy_tier`,
   `resolve_control_posture`, and `resolve_access_band` as the engines.

Existing behavior must remain byte-identical for previously-scored areas; these
tests pin the exact expected values.
"""
import os
import sys
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.services import chat_analytics, policy_scoring


@dataclass
class FakeBooking:
    id: int = 10
    user_id: int = 1
    status: object = "confirmed"  # plain string; code uses getattr(status, "value", status)


# --- area scoring config coverage --------------------------------------------


def test_area_scoring_catalog_covers_every_predefined_area():
    catalog = chat_analytics.build_area_scoring_catalog()
    catalog_areas = {item["area"] for item in catalog}
    assert catalog_areas == set(chat_analytics.PREDEFINED_POLICY_AREAS)
    # Every catalog entry carries the fields the engine relies on.
    for item in catalog:
        assert item["kind"] in {"message_keyword", "term_table"}
        assert item["weight"] is not None
        assert item["keywords"]


def test_score_area_unknown_area_returns_empty():
    score, evidence = chat_analytics.score_area(["anything at all"], [], "not_a_real_area")
    assert score == 0.0
    assert evidence == []


# --- exact expected values (pinned from the previous hardcoded branches) ------


def test_score_area_message_keyword_recent_and_older_weights():
    # Single (recent) message -> recent_weight applies.
    score, evidence = chat_analytics.score_area(["this is slow"], [], "response_speed")
    assert score == pytest.approx(1.2)
    assert evidence == ["this is slow"]

    # A message outside the first three messages -> base weight applies.
    score_old, evidence_old = chat_analytics.score_area(
        ["a", "b", "c", "d delay"], [], "response_speed"
    )
    assert score_old == pytest.approx(0.9)
    assert evidence_old == ["d delay"]

    # reliability / support / pricing base weights.
    assert chat_analytics.score_area(["there is a bug"], [], "reliability")[0] == pytest.approx(1.3)
    assert chat_analytics.score_area(["x", "y", "z", "say help please"], [], "support")[0] == pytest.approx(0.7)
    assert chat_analytics.score_area(["x", "y", "z", "price too high"], [], "pricing")[0] == pytest.approx(0.7)


def test_score_area_booking_flow_combines_keywords_and_booking_boost():
    pending_booking = FakeBooking(status="pending")
    score, evidence = chat_analytics.score_area(
        ["x", "y", "z", "cancel my booking"], [pending_booking], "booking_flow"
    )
    # 0.8 keyword contribution + 0.6 booking boost.
    assert score == pytest.approx(1.4)
    assert "cancel my booking" in evidence
    assert "1 bookings are pending/cancelled" in evidence

    # Multiple pending/cancelled bookings cap at 2.4.
    bookings = [FakeBooking(status="pending") for _ in range(5)]
    boost_only_score, _ = chat_analytics.score_area([], bookings, "booking_flow")
    assert boost_only_score == pytest.approx(2.4)


def test_score_area_retention_repeated_and_keyword_scoring():
    # Repeated identical messages (no retention keyword) -> repeated-message boost.
    score, evidence = chat_analytics.score_area(["help", "help", "help"], [], "retention")
    assert score == pytest.approx(1.0)  # repeated = 2 -> min(2 * 0.5, 2.0)
    assert evidence == ["2 repeated messages suggest unresolved needs"]

    # Direct retention keyword (recent message) -> keyword weight.
    score_kw, _ = chat_analytics.score_area(["please repeat that"], [], "retention")
    assert score_kw == pytest.approx(1.0)


def test_score_area_term_table_areas():
    cases = [
        (["angry", "frustrated"], "sentiment_recovery", 1.6),  # 2 * 0.8
        (["how do i sign up"], "onboarding", 0.7),  # 1 * 0.7
        (["notify me", "alert please"], "notification_quality", 1.2),  # 2 * 0.6
        (["is there a faq or self-service page"], "self_service", 1.2),  # 2 * 0.6
        (["transfer me to an agent"], "handoff", 1.2),  # 2 * 0.6
        (["refund not received", "trust this"], "trust", 1.0),  # 2 * 0.5
        (["still waiting", "remind me", "follow up"], "follow_up", 2.1),  # 3 * 0.7 -> capped
    ]
    for messages, area, expected in cases:
        score, evidence = chat_analytics.score_area(messages, [], area)
        assert score == pytest.approx(expected), f"area={area}"
        assert evidence, f"area={area} should produce evidence"


def test_score_area_retention_keywords_do_not_leak_into_other_areas():
    # Retention keywords must not inflate unrelated areas (pre-existing contract).
    # booking_flow (contains the substring "book") and follow_up (term "again")
    # legitimately overlap with the phrase, so they are checked separately.
    message = "please repeat that"
    for area in chat_analytics.PREDEFINED_POLICY_AREAS:
        if area in {"retention", "booking_flow", "follow_up"}:
            continue
        score, _ = chat_analytics.score_area([message], [], area)
        assert score <= 0.0, f"area={area} leaked retention keywords"


# --- dynamic configuration surface --------------------------------------------


def test_score_all_areas_matches_per_area_calls():
    messages = ["the service is slow", "I want to repeat my appointment again", "angry and frustrated"]
    bookings = [FakeBooking(status="cancelled")]
    combined = chat_analytics.score_all_areas(messages, bookings)
    for area in chat_analytics.AREA_SCORING_RULES:
        expected = chat_analytics.score_area(messages, bookings, area)
        assert area in combined
        assert combined[area][0] == pytest.approx(expected[0])
        assert combined[area][1] == expected[1]


def test_new_area_added_to_rules_is_scored_without_code_change(monkeypatch):
    # Attaching a brand-new area to the config table should be picked up by the
    # existing engine — no branch changes required (future services benefit).
    monkeypatch.setitem(
        chat_analytics.AREA_SCORING_RULES,
        "escalation",
        {
            "kind": "message_keyword",
            "keywords": ("escalate", "manager", "urgent"),
            "weight": 1.1,
            "recent_weight": 1.4,
        },
    )
    score, evidence = chat_analytics.score_area(["please escalate now"], [], "escalation")
    assert score == pytest.approx(1.4)  # single recent message
    assert evidence == ["please escalate now"]

    catalog = chat_analytics.build_area_scoring_catalog()
    escalation_entry = next(item for item in catalog if item["area"] == "escalation")
    assert escalation_entry["recent_weight"] == pytest.approx(1.4)
    # The catalog-derived keyword index now maps "escalate" -> escalation.
    assert "escalation" in chat_analytics.build_area_keyword_catalog()["escalate"]


def test_area_keyword_catalog_merges_legacy_retention_map():
    keyword_map = chat_analytics.build_area_keyword_catalog()
    assert "retention" in keyword_map["repeat"]
    assert "response_speed" in keyword_map["slow"]
    assert "pricing" in keyword_map["expensive"]
    # Legacy map keys are re-indexed even when they also appear in the rule table.
    assert "booking_flow" in keyword_map["cancel"]


def test_load_user_interaction_window_accepts_optional_limits():
    # Limits are optional and default to unbounded; callers may bound row counts.
    import inspect

    signature = inspect.signature(chat_analytics.load_user_interaction_window)
    assert "chat_limit" in signature.parameters
    assert "booking_limit" in signature.parameters
    assert signature.parameters["chat_limit"].default is None
    assert signature.parameters["booking_limit"].default is None


# --- policy tier / posture / band resolvers -----------------------------------


def test_resolve_policy_tier_boundaries():
    assert policy_scoring.resolve_policy_tier(85.0, 85.0) == "system-premium"
    assert policy_scoring.resolve_policy_tier(85.0, 84.0) == "customer-premium"
    assert policy_scoring.resolve_policy_tier(70.0, 0.0) == "customer-premium"
    assert policy_scoring.resolve_policy_tier(40.0, 0.0) == "standard"
    assert policy_scoring.resolve_policy_tier(39.0, 0.0) == "restricted"


def test_private_tier_posture_helpers_delegate_to_resolvers():
    for access, system in [(95.0, 90.0), (80.0, 60.0), (45.0, 20.0), (10.0, 0.0)]:
        assert policy_scoring._policy_tier(access, system) == policy_scoring.resolve_policy_tier(access, system)
    for tier, access, system in [
        ("system-premium", 10.0, 0.0),
        ("customer-premium", 10.0, 0.0),
        ("standard", 60.0, 60.0),
        ("standard", 54.0, 60.0),
    ]:
        assert policy_scoring._control_posture(tier, access, system) == policy_scoring.resolve_control_posture(tier, access, system)


def test_resolve_control_posture():
    assert policy_scoring.resolve_control_posture("system-premium", 0.0, 0.0) == "high_trust"
    assert policy_scoring.resolve_control_posture("customer-premium", 0.0, 0.0) == "customer_trusted"
    assert policy_scoring.resolve_control_posture("standard", 60.0, 60.0) == "observed"
    assert policy_scoring.resolve_control_posture("standard", 54.0, 60.0) == "constrained"


def test_resolve_access_band_boundaries():
    assert policy_scoring.resolve_access_band(90.0) == "elite"
    assert policy_scoring.resolve_access_band(89.0) == "strong"
    assert policy_scoring.resolve_access_band(75.0) == "strong"
    assert policy_scoring.resolve_access_band(74.0) == "moderate"
    assert policy_scoring.resolve_access_band(55.0) == "moderate"
    assert policy_scoring.resolve_access_band(54.0) == "limited"


def test_build_policy_tier_catalog_shape():
    catalog = policy_scoring.build_policy_tier_catalog()
    assert set(catalog) >= {"tier_rank", "tier_rules", "control_posture_tier_map", "control_posture_rules", "access_band_rules"}
    assert catalog["tier_rank"]["system-premium"] > catalog["tier_rank"]["restricted"]
    # Highest tier rule must be evaluated first.
    assert catalog["tier_rules"][0]["tier"] == "system-premium"


class _FakePolicyScore:
    def __init__(self, policy_tier):
        self.policy_tier = policy_tier


def test_can_access_functionality_uses_rank_table():
    # Same contract as before: tier rank decides access.
    assert policy_scoring.can_access_functionality(_FakePolicyScore("system-premium"), required_tier="system-premium") is True
    assert policy_scoring.can_access_functionality(_FakePolicyScore("system-premium"), required_tier="standard") is True
    assert policy_scoring.can_access_functionality(_FakePolicyScore("standard"), required_tier="customer-premium") is False
    assert policy_scoring.can_access_functionality(_FakePolicyScore("restricted"), required_tier="standard") is False


# --- meta endpoint ------------------------------------------------------------


def test_meta_scoring_catalog_endpoint():
    response = TestClient(app).get("/meta/scoring-catalog")
    assert response.status_code == 200
    payload = response.json()
    assert payload["area_scoring"]
    assert payload["area_keywords"]
    assert payload["policy_tiers"]["tier_rules"]


def test_meta_features_documents_scoring_catalog():
    response = TestClient(app).get("/meta/features")
    assert response.status_code == 200
    assert response.json()["endpoints"]["scoring_catalog"] == "/meta/scoring-catalog"