"""Tests for the communication-strategy expansion.

The backend now resolves *how to communicate with a user* by walking a fixed
precedence ladder of criteria:

1. admin select  — an admin-picked profile stored in `communication_overrides`
2. policy        — policy-defined `when`-DSL rules
3. culture       — locale-keyed culture rules
4. user profile  — profile rules over the user context incl. history stats
5. session mood  — mood detected from the current session's messages
6. default       — neutral fallback

Everything is config-driven (`COMMUNICATION_*` tables in
`app/services/communication_strategy.py`), exposed via
`/chat/communication-strategy` (self) and the admin report + override CRUD
routes, and introspectable through `/meta/scoring-catalog`.

Existing endpoints and scores must remain byte-identical; these tests pin the
resolver behavior only.
"""
import os
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import deps, models
from app.main import app
from app.services import communication_strategy
from app.services.communication_strategy import (
    COMMUNICATION_CULTURE_RULES,
    COMMUNICATION_MOOD_CUES,
    COMMUNICATION_MOOD_RULES,
    COMMUNICATION_OVERRIDE_CATALOG,
    COMMUNICATION_POLICY_RULES,
    COMMUNICATION_PROFILE_RULES,
    detect_session_messages_mood,
    resolve_communication_strategy,
)

PARAMS_FIELDS = {"tone", "channel", "formality", "framing", "greeting_style", "reply_urgency"}


def _context(**overrides) -> dict:
    base = {
        "stage": "engaged",
        "churn_risk": "low",
        "risk_level": "low",
        "value_tier": "growth",
        "customer_classification": "loyal growth-ready",
        "journey_family": "delivery",
        "sentiment_label": "positive",
        "at_risk": False,
        "has_history": True,
        "dormant": False,
        "chat_count": 3,
        "booking_count": 2,
        "completed_bookings": 1,
        "confirmed_bookings": 1,
        "pending_bookings": 0,
        "cancelled_bookings": 0,
        "loyalty_score": 88.0,
        "monetization_readiness": 72.0,
        "churn_risk_score": 5.0,
        "signal_strength": 4.0,
        "top_issue_1": "",
        "top_issue_2": "",
        "top_issue_3": "",
        "top_issue": "",
        "days_since_last_activity": 2,
        "locale": "global",
    }
    base.update(overrides)
    return base


# --- catalogs / config tables -----------------------------------------------


def test_communication_precedence_ladder_ordered() -> None:
    catalog = communication_strategy.build_communication_strategy_catalog()
    layers = catalog["precedence"]
    assert [entry["layer"] for entry in layers] == [
        "admin_select",
        "policy",
        "culture",
        "user_profile",
        "session_mood",
        "default",
    ]
    assert [entry["precedence"] for entry in layers] == [1, 2, 3, 4, 5, 6]


def test_communication_override_catalog_unique_and_valid() -> None:
    ids = [p["profile_id"] for p in COMMUNICATION_OVERRIDE_CATALOG]
    assert len(ids) == len(set(ids))
    assert "escalation_hot" in ids
    for profile in COMMUNICATION_OVERRIDE_CATALOG:
        assert PARAMS_FIELDS <= set(profile)
        assert profile["label"]


def test_communication_rule_catalogs_valid() -> None:
    for rule in COMMUNICATION_POLICY_RULES:
        assert rule["policy_id"]
        assert "when" in rule and "params" in rule
        assert PARAMS_FIELDS <= set(rule["params"])
        assert rule.get("priority") in {"high", "medium", "low"}
    fallbacks = [r for r in COMMUNICATION_CULTURE_RULES if r.get("fallback")]
    assert len(fallbacks) == 1
    assert fallbacks[0]["locale"] == "global"
    for rule in COMMUNICATION_PROFILE_RULES:
        assert "when" in rule and PARAMS_FIELDS <= set(rule["params"])
    for rule in COMMUNICATION_MOOD_RULES:
        assert rule["mood_id"] in COMMUNICATION_MOOD_CUES or rule["mood_id"] == "neutral"
        assert PARAMS_FIELDS <= set(rule["params"])
    for cues in COMMUNICATION_MOOD_CUES.values():
        assert cues


# --- session mood detection -------------------------------------------------


def test_detect_session_mood_frustrated_cue() -> None:
    mood = detect_session_messages_mood(["this is absolutely ridiculous and unacceptable"])
    assert mood["label"] == "frustrated"
    assert mood["top_cue"] == "frustrated"
    assert mood["cue_hits"]["frustrated"] >= 1


def test_detect_session_mood_happy_cue() -> None:
    mood = detect_session_messages_mood(["thank you so much this is amazing"])
    assert mood["label"] == "happy"


def test_detect_session_mood_urgent_wins_on_count() -> None:
    mood = detect_session_messages_mood(
        ["need this fixed asap", "really urgent, right now please", "hurry, deadline today"]
    )
    assert mood["label"] == "urgent"


def test_detect_session_mood_empty_returns_none() -> None:
    assert detect_session_messages_mood([]) is None
    assert detect_session_messages_mood(["   "]) is None


def test_detect_session_mood_tie_break_earlier_order() -> None:
    mood = detect_session_messages_mood(["I am frustrated and confused about this"])
    assert mood["top_cue"] == "frustrated"


# --- precedence resolution --------------------------------------------------


def test_resolve_admin_select_wins_over_all() -> None:
    result = resolve_communication_strategy(
        _context(),
        admin_override={"profile_id": "escalation_hot"},
        mood={"label": "frustrated"},
    )
    assert result["resolved_layer"] == "admin_select"
    assert result["precedence"] == 1
    assert result["profile_id"] == "escalation_hot"
    assert result["params"]["reply_urgency"] == "immediate"
    assert result["decision_trail"][0]["matched"] is True


def test_resolve_policy_wins_over_culture_profile_mood() -> None:
    result = resolve_communication_strategy(
        _context(top_issue_1="refund request", top_issue="refund request"),
        locale="es_MX",
        mood={"label": "happy"},
    )
    assert result["resolved_layer"] == "policy"
    assert result["profile_id"] == "sensitive_complaint"
    assert result["params"]["tone"] == "empathic"


def test_resolve_policy_numeric_when_syntax() -> None:
    result = resolve_communication_strategy(
        _context(cancelled_bookings=1, completed_bookings=0),
        mood=None,
    )
    assert result["resolved_layer"] == "policy"
    assert result["profile_id"] == "cancellation_recovery"


def test_resolve_culture_wins_over_profile_and_mood() -> None:
    result = resolve_communication_strategy(
        _context(),
        locale="es_MX",
        mood={"label": "happy"},
    )
    assert result["resolved_layer"] == "culture"
    assert result["profile_id"] == "latam_warm"
    assert result["params"]["formality"] == "informal"


def test_resolve_user_profile_wins_over_mood() -> None:
    result = resolve_communication_strategy(
        _context(stage="new"),
        mood={"label": "happy"},
    )
    assert result["resolved_layer"] == "user_profile"
    assert result["profile_id"] == "new_customer_onboarding"
    assert result["params"]["tone"] == "encouraging"
    assert "first_value" in result["guidance"][1] or result["params"]["framing"] == "first_value"


def test_resolve_session_mood_fires_when_nothing_else_matches() -> None:
    result = resolve_communication_strategy(
        _context(stage="odd_unknown_stage", journey_family="unknown_family"),
        mood={"label": "frustrated"},
    )
    assert result["resolved_layer"] == "session_mood"
    assert result["precedence"] == 5
    assert result["params"]["tone"] == "apologetic"
    assert result["params"]["reply_urgency"] == "immediate"
    assert result["mood"]["label"] == "frustrated"


def test_resolve_default_fallback_when_nothing_matches() -> None:
    result = resolve_communication_strategy(
        _context(stage="odd_unknown_stage", journey_family="unknown_family"),
        mood=None,
    )
    assert result["resolved_layer"] == "default"
    assert result["precedence"] == 6
    assert result["profile_id"] == "default"
    assert result["params"]["tone"] == "helpful"


def test_resolve_decision_trail_records_every_layer() -> None:
    result = resolve_communication_strategy(_context(stage="new"))
    trail = result["decision_trail"]
    assert [decision["layer"] for decision in trail] == [
        "admin_select",
        "policy",
        "culture",
        "user_profile",
    ]
    assert [decision["matched"] for decision in trail[:-1]] == [False, False, False]
    assert trail[-1]["matched"] is True
    fallback = resolve_communication_strategy(
        _context(stage="odd_unknown_stage", journey_family="unknown_family"), mood=None
    )
    assert len(fallback["decision_trail"]) == 6
    assert fallback["decision_trail"][-1]["layer"] == "default"


def test_resolve_context_snapshot_includes_history_stats() -> None:
    result = resolve_communication_strategy(_context(loyalty_score=91.0, journey_family="monetization"))
    snapshot = result["context_snapshot"]
    assert snapshot["loyalty_score"] == 91.0
    assert snapshot["journey_family"] == "monetization"
    assert "completed_bookings" in snapshot
    assert "stage" in snapshot
    assert "locale" in snapshot


# --- override persistence (service level, fake db) --------------------------


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


class _OverrideDb:
    """Fake session: override rows, user rows; every other query is empty."""

    def __init__(self, users=(), overrides=()):
        self.users = list(users)
        self.overrides = list(overrides)
        self.commits = 0

    async def execute(self, statement, *_args, **_kwargs):
        text = str(statement)
        if "communication_overrides" in text:
            return _RowsResult(list(self.overrides))
        if "FROM users" in text:
            return _RowsResult(list(self.users))
        return _EmptyResult()

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = max([getattr(o, "id", 0) for o in self.overrides] or [0]) + 1
        self.overrides.append(obj)

    async def delete(self, obj):
        if obj in self.overrides:
            self.overrides.remove(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def _make_override(profile_id="premium_concierge", user_id=7):
    return models.UserCommunicationOverride(
        user_id=user_id,
        profile_id=profile_id,
        set_by_admin_id=1,
        note="needs concierge treatment",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_override_set_and_list() -> None:
    db = _OverrideDb(users=[(7, "grace")])
    item = await communication_strategy.set_communication_admin_override(
        db, 7, "premium_concierge", admin_id=1, note="needs concierge treatment"
    )
    assert item["user_id"] == 7
    assert item["profile_id"] == "premium_concierge"
    assert item["profile_label"] == "Premium concierge"
    assert db.commits == 1

    loaded = await communication_strategy.load_admin_override(db, 7)
    assert loaded is not None
    assert loaded["profile_id"] == "premium_concierge"

    report = await communication_strategy.list_communication_admin_overrides(db, 50)
    assert report.total == 1
    assert report.overrides[0].username == "grace"


@pytest.mark.asyncio
async def test_override_set_unknown_profile_raises() -> None:
    db = _OverrideDb()
    with pytest.raises(ValueError):
        await communication_strategy.set_communication_admin_override(
            db, 7, "no_such_profile", admin_id=1
        )


@pytest.mark.asyncio
async def test_override_replace_and_delete() -> None:
    db = _OverrideDb(overrides=[_make_override()])
    await communication_strategy.set_communication_admin_override(
        db, 7, "quiet_email", admin_id=2, note="switch to email"
    )
    assert len(db.overrides) == 1
    assert db.overrides[0].profile_id == "quiet_email"

    removed = await communication_strategy.delete_communication_admin_override(db, 7)
    assert removed is True
    assert await communication_strategy.load_admin_override(db, 7) is None
    assert await communication_strategy.delete_communication_admin_override(db, 7) is False


# --- endpoint wiring --------------------------------------------------------


class _FakeUser:
    id = 1
    username = "tester"
    is_admin = True


async def _fake_get_db():
    yield _EmptyResultDb()


class _EmptyResultDb:
    async def execute(self, *_args, **_kwargs):
        return _EmptyResult()


def test_communication_strategy_self_endpoint_empty_db() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/chat/communication-strategy")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == 1
    assert payload["user_name"] == "tester"
    assert payload["window_days"] == 30
    assert payload["resolved_layer"] == "user_profile"
    assert payload["profile_id"] == "new_customer_onboarding"
    assert payload["precedence"] == 4
    assert payload["params"]["tone"] == "encouraging"
    assert payload["decision_trail"][0] == {
        "layer": "admin_select",
        "precedence": 1,
        "matched": False,
        "reason": "No admin-selected override for this user.",
    }


def test_communication_strategy_admin_endpoint_empty_db() -> None:
    async def _fake_get_current_admin_user():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/communication-strategy")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_users"] == 0
    assert payload["total_matched"] == 0
    assert payload["coverage_by_layer"] == {}
    assert payload["top_layer"] is None
    assert payload["users"] == []


def test_communication_strategy_admin_endpoint_two_users() -> None:
    async def _fake_get_current_admin_user():
        return _FakeUser()

    class _UsersDb(_EmptyResultDb):
        async def execute(self, statement, *_args, **_kwargs):
            if "FROM users" in str(statement):
                return _RowsResult([(1, "alice"), (2, "bob")])
            return _EmptyResult()

    async def _fake_get_db():
        yield _UsersDb()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/communication-strategy")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_users"] == 2
    assert payload["coverage_by_layer"] == {"user_profile": 2}
    assert payload["top_layer"] == "user_profile"
    usernames = {item["username"] for item in payload["users"]}
    assert usernames == {"alice", "bob"}
    assert all(item["resolved_layer"] == "user_profile" for item in payload["users"])


def test_communication_override_crud_endpoints() -> None:
    async def _fake_get_current_admin_user():
        return _FakeUser()

    db = _OverrideDb(users=[(7, "grace")])

    async def _fake_get_db():
        yield db

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        client = TestClient(app)
        post = client.post(
            "/chat/admin/communication-overrides",
            json={"user_id": 7, "profile_id": "senior_care", "note": "older customer"},
        )
        assert post.status_code == 200
        assert post.json()["profile_id"] == "senior_care"
        assert post.json()["user_id"] == 7

        listing = client.get("/chat/admin/communication-overrides")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["overrides"][0]["username"] == "grace"

        deleted = client.delete("/chat/admin/communication-overrides/7")
        assert deleted.status_code == 200
        assert deleted.json()["total"] == 0

        already_gone = client.delete("/chat/admin/communication-overrides/7")
        assert already_gone.status_code == 404
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)


def test_communication_override_post_unknown_profile_422() -> None:
    async def _fake_get_current_admin_user():
        return _FakeUser()

    db = _OverrideDb()

    async def _fake_get_db():
        yield db

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).post(
            "/chat/admin/communication-overrides",
            json={"user_id": 7, "profile_id": "not_a_profile"},
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 422
    assert db.overrides == []


def test_communication_strategy_window_validation() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/chat/communication-strategy", params={"window_days": 1})
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 422


def test_meta_scoring_catalog_includes_communication_strategy() -> None:
    response = TestClient(app).get("/meta/scoring-catalog")
    assert response.status_code == 200
    payload = response.json()
    strategy = payload["communication_strategy"]
    assert strategy["catalog_version"] == "communication_strategy_v1"
    assert strategy["precedence"][0]["layer"] == "admin_select"
    assert strategy["precedence"][-1]["layer"] == "default"
    assert "override_catalog" in strategy
    assert "policy_rules" in strategy
    assert "culture_rules" in strategy
    assert "profile_rules" in strategy
    assert "mood_rules" in strategy
    assert "mood_cues" in strategy


def test_meta_ecosystem_lists_communication_strategy() -> None:
    response = TestClient(app).get("/meta/ecosystem")
    assert response.status_code == 200
    subservices = response.json()["subservices"]
    assert "communication_strategy" in subservices
    routes = subservices["communication_strategy"]["routes"]
    assert "/chat/communication-strategy" in routes
    assert "/chat/admin/communication-overrides" in routes