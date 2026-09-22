"""Regression tests for backend consolidation work.

These tests guard the consolidation contracts introduced across the backend:

1. `analyze_sentiment` lives in `app.services.chat_analytics` and is shared with
   the chat router (no service -> router import cycle).
2. The chat router keeps the module-global names the main test suite patches via
   `chat_message.__globals__[...]`.
3. The router `_build_system_improvement_pack` delegates to the canonical service
   builder (sync, single implementation).
4. `RETENTION_KEYWORDS` is wired into `score_area` scoring as extra evidence.
5. The duplicate `/meta/capabilities` route was removed from the chat router while
   the app-level endpoint still serves.
6. `/meta/features` documents the real retention route paths.
7. `app.routers.users._current_control_posture` is an alias of
   `deps.current_control_posture`.
"""
import inspect
import os
import sys
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import deps
from app.main import app
import app.routers.chat as chat_router
import app.routers.users as users_router
import app.services.chat_analytics as chat_analytics
from app.routers.chat import (
    _build_system_improvement_pack,
    analyze_sentiment as router_analyze_sentiment,
)
from app.schemas.chat import Sentiment, SystemImprovementPack

ROUTER_PATCHED_GLOBALS = (
    "_load_user_interaction_window",
    "_build_interaction_insights",
    "_build_summary",
    "_build_system_improvement_pack",
    "analyze_sentiment",
    "_save_retention_snapshot",
    "_prune_retention_snapshots",
    "_store_interaction_signals",
)


@dataclass
class FakeChatRow:
    message: str
    user_id: int = 1


@dataclass
class FakeBooking:
    id: int = 10
    user_id: int = 1
    status: object = "confirmed"  # plain string; code uses getattr(status, "value", status)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# --- sentiment consolidation -------------------------------------------------


def test_router_analyze_sentiment_is_service_analyze_sentiment():
    # The router must share the service implementation (no deferred router import).
    assert router_analyze_sentiment is chat_analytics.analyze_sentiment


def test_analyze_sentiment_returns_none_on_failure(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("network error")

    monkeypatch.setattr(chat_analytics.requests, "post", _boom)
    assert chat_analytics.analyze_sentiment("anything") is None


def test_analyze_sentiment_parses_inference_payload(monkeypatch):
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse([[{"label": "NEGATIVE", "score": 0.97}]])

    monkeypatch.setattr(chat_analytics.requests, "post", _fake_post)
    monkeypatch.setattr(chat_analytics, "HF_API_TOKEN", None)

    result = chat_analytics.analyze_sentiment("I am very disappointed")

    assert isinstance(result, Sentiment)
    assert result.label == "negative"
    assert result.score == pytest.approx(0.97, abs=1e-6)
    assert captured["url"] == chat_analytics.HF_SENTIMENT_URL
    assert captured["headers"] == {}


def test_analyze_sentiment_sends_bearer_token_when_configured(monkeypatch):
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse([[{"label": "POSITIVE", "score": 0.91}]])

    monkeypatch.setattr(chat_analytics.requests, "post", _fake_post)
    monkeypatch.setattr(chat_analytics, "HF_API_TOKEN", "test-token")

    result = chat_analytics.analyze_sentiment("great")

    assert result.label == "positive"
    assert captured["headers"] == {"Authorization": "Bearer test-token"}


# --- router global-name contract ---------------------------------------------


def test_chat_router_keeps_test_patched_globals():
    missing = [name for name in ROUTER_PATCHED_GLOBALS if name not in vars(chat_router)]
    assert not missing, f"router globals relied on by tests are missing: {missing}"


def test_chat_router_no_duplicate_meta_capabilities_route():
    paths = {getattr(route, "path", None) for route in chat_router.router.routes}
    assert "/meta/capabilities" not in paths


# --- system improvement pack consolidation -----------------------------------


def _without_generated_at(pack: SystemImprovementPack) -> dict:
    payload = pack.model_dump()
    payload.pop("generated_at", None)
    payload["summary"] = payload["summary"].copy()
    payload["summary"].pop("generated_at", None)
    return payload


def test_router_improvement_pack_delegates_to_service_builder():
    rows = [
        FakeChatRow(message="I keep having issues with the booking flow"),
        FakeChatRow(message="can you help me understand this again"),
    ]
    bookings = [FakeBooking(status="cancelled")]
    sentiment = Sentiment(label="negative", score=0.8)

    router_pack = _build_system_improvement_pack(1, rows, bookings, sentiment)
    service_pack = chat_analytics.build_system_improvement_pack(1, rows, bookings, sentiment)

    assert isinstance(router_pack, SystemImprovementPack)
    # The improvement-pack builder is synchronous; the router wrapper is not a coroutine.
    assert not inspect.iscoroutinefunction(_build_system_improvement_pack)
    assert not inspect.iscoroutinefunction(chat_analytics.build_system_improvement_pack)
    assert _without_generated_at(router_pack) == _without_generated_at(service_pack)


# --- retention keyword scoring ------------------------------------------------


def test_score_area_uses_retention_keywords_from_keyword_map():
    score, evidence = chat_analytics.score_area(
        ["I want to repeat this appointment"], [], "retention"
    )
    assert score > 0
    assert evidence

    score_again, _ = chat_analytics.score_area(
        ["please remind me again tomorrow"], [], "retention"
    )
    assert score_again > 0


def test_score_area_retention_keywords_only_fire_for_retention_area():
    # "repeat" should not inflate unrelated areas.
    score_pricing, _ = chat_analytics.score_area(["I want to repeat my booking"], [], "pricing")
    assert score_pricing <= 0


# --- meta endpoint docs ------------------------------------------------------


def test_meta_features_documents_real_retention_paths():
    response = TestClient(app).get("/meta/features")
    assert response.status_code == 200
    endpoints = response.json()["endpoints"]
    assert endpoints["retention_dashboard"] == "/chat/retention-dashboard"
    assert endpoints["retention_maintenance"] == "/retention/maintenance"


def test_meta_capabilities_still_served_single_source():
    response = TestClient(app).get("/meta/capabilities")
    assert response.status_code == 200
    assert response.json()["domain"] == "chat-retention"
    app_level_paths = [
        getattr(route, "path", None)
        for route in app.routes
        if getattr(route, "path", None) is not None
    ]
    assert app_level_paths.count("/meta/capabilities") == 1


# --- users router control posture alias -------------------------------------


def test_users_router_control_posture_is_deps_alias():
    assert users_router._current_control_posture is deps.current_control_posture


def test_deps_no_longer_exposes_removed_policy_admin_dependency():
    assert not hasattr(deps, "get_current_policy_admin_user")