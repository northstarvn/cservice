"""Tests for the activity-tree monitoring expansion.

The backend now organizes customer activities as query-time tree structures:

1. `app/services/activity_tree.py` hosts config-driven grouping axes
   (`ACTIVITY_TREE_GROUP_AXES`), rank options, absolute anomaly rules, and
   group-relative deviation rules — adding an axis/rank/rule is a config change.
2. `/chat/activity-tree` (self) and `/chat/admin/activity-tree` (admin) expose
   grouped, ranked, filterable trees with anomaly highlighting.
3. `/meta/scoring-catalog` exposes the live `activity_monitoring` catalog so
   future surfaces can validate/display the rules.

Existing endpoints and scores must remain byte-identical; these tests pin the
tree behavior only.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import deps, models
from app.main import app
from app.services import activity_tree
from app.services.activity_tree import (
    ACTIVITY_TREE_ANOMALY_RULES,
    ACTIVITY_TREE_GROUP_AXES,
    ACTIVITY_TREE_RANK_OPTIONS,
    ACTIVITY_TREE_RELATIVE_ANOMALY_RULES,
    TREE_NEGATIVE_KEYWORDS,
    apply_user_filters,
    attach_group_relative_anomalies,
    build_activity_items,
    detect_user_anomalies,
    group_members_by_axis,
    rank_and_slice,
    rank_items,
)
from app.services.loyalty_journey import resolve_top_journey_family
from app.schemas.chat import ChurnPrediction, InteractionSummary

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
    monetization_readiness=80.0,
) -> InteractionSummary:
    return InteractionSummary(
        user_id=user_id,
        messages_analyzed=1,
        bookings_analyzed=0,
        churn_risk=churn_risk,
        loyalty_score=loyalty_score,
        monetization_readiness=monetization_readiness,
        value_tier="growth",
        customer_classification="loyal growth-ready",
        top_issues=[],
        strengths=["no strong negative sentiment"],
        insights=[],
        metadata={"signal_strength": 0.0, "repeated_messages": 0, "booking_states": {}},
        generated_at=NOW,
    )


def _metrics(**overrides) -> dict:
    base = {
        "user_id": 1,
        "username": "alice",
        "lifecycle_stage": "engaged",
        "churn_risk": "low",
        "risk_level": "low",
        "value_tier": "growth",
        "customer_classification": "loyal growth-ready",
        "loyalty_score": 90.0,
        "monetization_readiness": 80.0,
        "signal_strength": 2.0,
        "churn_risk_score": 10.0,
        "sentiment_label": "neutral",
        "chat_count": 2,
        "booking_count": 1,
        "completed_bookings": 1,
        "pending_bookings": 0,
        "cancelled_bookings": 0,
        "dormant": False,
        "journey_family": "delivery",
        "top_issue": "",
        "activity_count": 3,
        "latest_activity_at": NOW.isoformat(),
    }
    base.update(overrides)
    return base


# --- catalog / config tables ------------------------------------------------


def test_activity_tree_catalog_group_axes_shape():
    assert set(ACTIVITY_TREE_GROUP_AXES) == {
        "lifecycle_stage",
        "value_tier",
        "customer_classification",
        "churn_risk",
        "journey_family",
    }
    for axis, info in ACTIVITY_TREE_GROUP_AXES.items():
        assert "label" in info
        assert "metric_field" in info


def test_activity_tree_catalog_rank_options_are_config_keys():
    assert "loyalty_score" in ACTIVITY_TREE_RANK_OPTIONS
    assert "churn_risk_score" in ACTIVITY_TREE_RANK_OPTIONS
    assert "activity_count" in ACTIVITY_TREE_RANK_OPTIONS


def test_activity_tree_anomaly_rules_are_valid():
    ids = [rule["id"] for rule in ACTIVITY_TREE_ANOMALY_RULES]
    assert len(ids) == len(set(ids)), "anomaly rule ids must be unique"
    for rule in ACTIVITY_TREE_ANOMALY_RULES:
        assert rule["op"] in {"gte", "lte", "eq"}
        assert rule["severity"] in {"high", "medium", "low"}
        assert isinstance(rule["threshold"], (int, float, str, bool))


def test_activity_tree_relative_anomaly_rules_are_valid():
    for rule in ACTIVITY_TREE_RELATIVE_ANOMALY_RULES:
        assert rule["direction"] in {"above", "below"}
        assert isinstance(rule["margin"], (int, float))
        assert rule["metric"] in {"loyalty_score", "churn_risk_score"}


# --- absolute anomaly detection --------------------------------------------


def test_detect_user_anomalies_churn_spike_and_loyalty_drop():
    anomalies = detect_user_anomalies(
        _metrics(churn_risk_score=65.0, loyalty_score=30.0)
    )
    ids = {a["anomaly"] for a in anomalies}
    assert "churn_spike" in ids
    assert "loyalty_drop" in ids
    spike = next(a for a in anomalies if a["anomaly"] == "churn_spike")
    assert spike["severity"] == "high"
    assert spike["metric_value"] == 65.0
    assert "rule" in spike


def test_detect_user_anomalies_sentiment_cancellation_signal_dormancy():
    anomalies = detect_user_anomalies(
        _metrics(
            sentiment_label="negative",
            cancelled_bookings=3,
            signal_strength=12.0,
            dormant=True,
        )
    )
    ids = {a["anomaly"] for a in anomalies}
    assert "negative_sentiment" in ids
    assert "cancellation_burst" in ids
    assert "signal_spike" in ids
    assert "dormancy" in ids


def test_detect_user_anomalies_ignores_missing_metrics():
    metrics = _metrics()
    for key in ("churn_risk_score", "loyalty_score", "sentiment_label"):
        metrics.pop(key, None)
    assert detect_user_anomalies(metrics) == []


def test_detect_user_anomalies_healthy_user_has_none():
    assert detect_user_anomalies(_metrics()) == []


# --- smart filters ----------------------------------------------------------


def test_apply_user_filters_loyalty_range():
    metrics = _metrics(loyalty_score=55.0)
    assert apply_user_filters(metrics, min_loyalty=50.0, max_loyalty=60.0)
    assert not apply_user_filters(metrics, min_loyalty=60.0)
    assert not apply_user_filters(metrics, max_loyalty=50.0)


def test_apply_user_filters_churn_sentiment_q():
    metrics = _metrics(username="bobby", churn_risk="high", sentiment_label="negative")
    assert apply_user_filters(metrics, churn_risk="high", sentiment="negative")
    assert not apply_user_filters(metrics, churn_risk="low")
    assert not apply_user_filters(metrics, sentiment="positive")
    assert apply_user_filters(metrics, q="bobby")
    assert apply_user_filters(metrics, q="BOBBY")
    assert not apply_user_filters(metrics, q="zoe")


def test_apply_user_filters_anomalies_only():
    no_anomaly = _metrics()
    no_anomaly["_anomaly_count"] = 0
    with_anomaly = _metrics()
    with_anomaly["_anomaly_count"] = 2
    assert not apply_user_filters(no_anomaly, anomalies_only=True)
    assert apply_user_filters(with_anomaly, anomalies_only=True)
    assert apply_user_filters(no_anomaly, anomalies_only=False)


# --- ranking / grouping -----------------------------------------------------


def test_rank_and_slice_descending_and_cap():
    members = [
        _metrics(user_id=1, loyalty_score=40.0),
        _metrics(user_id=2, loyalty_score=90.0),
        _metrics(user_id=3, loyalty_score=60.0),
    ]
    ranked = rank_and_slice(members, "loyalty_score", "desc", 2)
    assert [m["user_id"] for m in ranked] == [2, 3]
    ascending = rank_and_slice(members, "loyalty_score", "asc", 0)
    assert [m["user_id"] for m in ascending] == [1, 3, 2]


def test_group_members_by_axis_buckets():
    members = [
        _metrics(user_id=1, lifecycle_stage="new"),
        _metrics(user_id=2, lifecycle_stage="engaged"),
        _metrics(user_id=3, lifecycle_stage="new"),
    ]
    buckets = group_members_by_axis(members, "lifecycle_stage")
    assert set(buckets) == {"new", "engaged"}
    assert len(buckets["new"]) == 2
    assert len(buckets["engaged"]) == 1


def test_attach_group_relative_anomalies():
    members = [
        _metrics(user_id=1, loyalty_score=90.0, churn_risk_score=10.0),
        _metrics(user_id=2, loyalty_score=88.0, churn_risk_score=12.0),
        _metrics(user_id=3, loyalty_score=50.0, churn_risk_score=60.0),
    ]
    attach_group_relative_anomalies(members)
    ids_3 = {a["anomaly"] for a in members[2]["_anomalies"]}
    assert "loyalty_gap" in ids_3
    assert "churn_deviation" in ids_3
    assert members[2]["_anomaly_count"] >= 2
    assert members[0]["_anomaly_count"] == 0


# --- leaf activity items ----------------------------------------------------


class _ChatRow:
    def __init__(self, message, timestamp=None, id=1):
        self.message = message
        self.timestamp = timestamp or NOW
        self.id = id


class _BookingRow:
    def __init__(self, status, service_type="consultation", scheduled_date=None, created_at=None, id=1):
        self.status = status
        self.service_type = service_type
        self.scheduled_date = scheduled_date or NOW + timedelta(days=2)
        self.created_at = created_at or NOW
        self.id = id


def test_build_activity_items_chat_negative_keyphrase_flagging():
    rows = [
        _ChatRow("thanks for the quick help"),
        _ChatRow("this delivery is so slow and I want a refund"),
    ]
    items = build_activity_items(rows, [], limit=None)
    kinds = {item["kind"] for item in items}
    assert kinds == {"chat"}
    flagged = [item for item in items if item["flagged"]]
    assert len(flagged) == 1
    assert "slow" in flagged[0]["flag_reason"] or "refund" in flagged[0]["flag_reason"]
    assert items[0]["timestamp"] >= items[1]["timestamp"]


def test_build_activity_items_booking_flagging_and_kind_filter():
    bookings = [
        _BookingRow("cancelled"),
        _BookingRow("completed"),
        _BookingRow("pending"),
    ]
    items = build_activity_items([], bookings, kind="booking", limit=None)
    assert all(item["kind"] == "booking" for item in items)
    cancelled = next(item for item in items if item["label"].endswith("cancelled"))
    assert cancelled["flagged"] is True
    assert cancelled["flag_reason"] == "Booking cancelled"
    completed = next(item for item in items if item["label"].endswith("completed"))
    assert completed["flagged"] is False
    assert completed["value"] == 3.0


def test_build_activity_items_kind_exclusive():
    chat_only = build_activity_items([_ChatRow("hi")], [_BookingRow("completed")], kind="chat")
    assert all(item["kind"] == "chat" for item in chat_only)
    booking_only = build_activity_items([_ChatRow("hi")], [_BookingRow("completed")], kind="booking")
    assert all(item["kind"] == "booking" for item in booking_only)


def test_rank_items_recency_and_score():
    old = _ChatRow("old message", timestamp=NOW - timedelta(days=5))
    new = _ChatRow("new message", timestamp=NOW)
    items = build_activity_items([old, new], [], limit=None)
    by_recency = rank_items(items, "recency", "desc")
    assert by_recency[0]["timestamp"] == new.timestamp
    by_score = rank_items(items, "score", "desc")
    assert by_score[0]["value"] >= by_score[1]["value"]


# --- resolve_top_journey_family helper --------------------------------------


def test_resolve_top_journey_family_known_families():
    family = resolve_top_journey_family(
        _summary(churn_risk="high", loyalty_score=35.0),
        _churn(risk_level="high", risk_score=60.0),
        [_ChatRow("I want to cancel everything")],
        [_BookingRow("cancelled")],
        None,
        total_chat_count=1,
        total_booking_count=1,
    )
    known = {
        "onboarding",
        "activation",
        "delivery",
        "habit",
        "recovery",
        "retention",
        "churn",
        "trust",
        "monetization",
        "data",
        "none",
    }
    assert family in known
    assert family != "none"


# --- endpoint wiring --------------------------------------------------------


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


class _RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class _UsersDb:
    """Returns the configured users for the user-list query, empty elsewhere."""

    def __init__(self, users):
        self.users = users

    async def execute(self, statement, *_args, **_kwargs):
        if "FROM users" in str(statement):
            return _RowsResult(self.users)
        return _EmptyResult()


class _OneChatDb:
    """First execute returns chat rows (self-tree path), the rest empty."""

    def __init__(self, chat_rows):
        self.chat_rows = chat_rows
        self.calls = 0

    async def execute(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return _RowsResult(self.chat_rows)
        return _EmptyResult()


class _FakeUser:
    id = 1
    username = "tester"
    is_admin = True


async def _fake_get_db():
    yield _EmptyDb()


def test_activity_tree_admin_endpoint_empty_db():
    async def _fake_get_current_admin_user():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/activity-tree")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "all_users"
    assert payload["group_by"] == "lifecycle_stage"
    assert payload["rank_by"] == "loyalty_score"
    assert payload["total_users"] == 0
    assert payload["group_count"] == 0
    assert payload["total_nodes"] == 1
    assert payload["root"]["kind"] == "root"
    assert payload["root"]["children"] == []


def test_activity_tree_admin_endpoint_has_meta_filters():
    async def _fake_get_current_admin_user():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get(
            "/chat/admin/activity-tree",
            params={
                "group_by": "value_tier",
                "rank_by": "activity_count",
                "order": "asc",
                "anomalies_only": "true",
                "min_loyalty": 40,
            },
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["group_by"] == "value_tier"
    assert payload["rank_by"] == "activity_count"
    assert payload["order"] == "asc"
    assert payload["filtered"]["min_loyalty"] == 40
    assert payload["filtered"]["anomalies_only"] is True


def test_activity_tree_admin_endpoint_two_users_grouped_by_stage():
    async def _fake_get_current_admin_user():
        return _FakeUser()

    class _TwoUsersDb(_UsersDb):
        pass

    async def _fake_get_db():
        yield _TwoUsersDb([(1, "alice"), (2, "bob")])

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/activity-tree")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_users"] == 2
    assert payload["group_count"] == 1
    group = payload["root"]["children"][0]
    assert group["kind"] == "group"
    assert group["metrics"]["member_count"] == 2
    assert group["child_count"] == 2
    children = group["children"]
    assert [child["kind"] for child in children] == ["user", "user"]
    assert {child["label"] for child in children} == {"alice", "bob"}
    for child in children:
        assert child["metrics"]["lifecycle_stage"] == "new"
        assert child["metrics"]["journey_family"] in {
            "onboarding",
            "activation",
            "delivery",
            "habit",
            "recovery",
            "retention",
            "churn",
            "trust",
            "monetization",
            "data",
            "none",
        }


def test_activity_tree_admin_anomalies_only_prunes_empty_groups():
    async def _fake_get_current_admin_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _UsersDb([(1, "alice"), (2, "bob")])

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/activity-tree", params={"anomalies_only": "true"})
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    # Two brand-new users with zero signals carry no absolute anomalies, so the
    # only group is pruned away even though the users are still counted.
    assert payload["total_users"] == 2
    assert payload["group_count"] == 0
    assert payload["anomaly_count"] == 0


def test_activity_tree_admin_min_loyalty_filter_keeps_users():
    async def _fake_get_current_admin_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _UsersDb([(1, "alice")])

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get(
            "/chat/admin/activity-tree", params={"min_loyalty": 99.5}
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    # Fresh empty users score loyalty 100, so they survive the floor.
    assert payload["total_users"] == 1
    assert payload["group_count"] == 1


def test_activity_tree_self_endpoint_empty_db():
    async def _fake_get_current_user():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/chat/activity-tree")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "self"
    assert payload["group_by"] == "activity_kind"
    assert payload["rank_by"] == "recency"
    assert payload["total_users"] == 1
    assert payload["group_count"] == 0
    assert payload["root"]["label"] == "My activities"


def test_activity_tree_self_endpoint_with_flagged_chat():
    async def _fake_get_current_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _OneChatDb([_ChatRow("this is so slow, very bad service")])

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/chat/activity-tree")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["group_count"] == 1
    group = payload["root"]["children"][0]
    assert group["label"] == "Activity kind · chat"
    assert group["anomaly_count"] == 1
    items = group["activity_items"]
    assert len(items) == 1
    assert items[0]["flagged"] is True
    assert "slow" in items[0]["flag_reason"]
    assert payload["highlights"], "flagged activity must surface in highlights"


def test_activity_tree_invalid_group_by_and_rank_by_rejected():
    async def _fake_get_current_admin_user():
        return _FakeUser()

    async def _fake_get_current_user():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        bad_group = TestClient(app).get(
            "/chat/admin/activity-tree", params={"group_by": "bogus_axis"}
        )
        bad_rank = TestClient(app).get(
            "/chat/admin/activity-tree", params={"rank_by": "bogus_metric"}
        )
        bad_window = TestClient(app).get("/chat/activity-tree", params={"window_days": 1})
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert bad_group.status_code == 422
    assert bad_rank.status_code == 422
    assert bad_window.status_code == 422


def test_meta_scoring_catalog_includes_activity_monitoring():
    response = TestClient(app).get("/meta/scoring-catalog")
    assert response.status_code == 200
    payload = response.json()
    monitoring = payload["activity_monitoring"]
    assert monitoring["catalog_version"] == "activity_tree_v1"
    assert {item["axis"] for item in monitoring["group_axes"]} == set(
        ACTIVITY_TREE_GROUP_AXES
    )
    assert monitoring["anomaly_rules_absolute"]
    assert monitoring["anomaly_rules_relative"]
    assert "negative_keywords" in monitoring


def test_meta_ecosystem_lists_activity_monitoring_routes():
    response = TestClient(app).get("/meta/ecosystem")
    assert response.status_code == 200
    subservices = response.json()["subservices"]
    assert "activity_monitoring" in subservices
    assert "/chat/activity-tree" in subservices["activity_monitoring"]["routes"]
    assert "/chat/admin/activity-tree" in subservices["activity_monitoring"]["routes"]