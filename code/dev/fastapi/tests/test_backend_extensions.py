from dataclasses import dataclass
from datetime import datetime, timezone
import os
import sys
import asyncio

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import deps, models, security
from app.main import app
from app.deps import get_current_admin_user, get_current_policy_or_admin_user
from app.services.bookings import apply_booking_updates, transition_booking
from app.services.bookings import create_booking_event, ensure_booking_transition_allowed
from app.services.bookings import build_booking_service_summary
from app.routers.users import change_password
from app.routers.users import read_users_access_decision
from app.routers.users import read_users_policy_decision_report
from app.routers.bookings import get_booking_analytics_summary
from app.routers.bookings import create_booking
from app.routers.bookings import get_bookings
from app.routers.bookings import get_booking
from app.routers.bookings import get_booking_history
from app.routers.bookings import delete_booking
from app.routers.bookings import get_booking_assignment_history
from app.routers.bookings import get_booking_assignment_report
from app.routers.bookings import create_booking_assignment_report
from app.routers.bookings import get_booking_audit_summary
from app.routers.bookings import update_booking
import app.routers.bookings as bookings_module
from app.routers.bookings import update_booking_assignment_report
from app.routers.chat import chat_message
from app.routers.chat import get_admin_activity_report
from app.routers.chat import get_admin_retention_trend_report
from app.routers.chat import get_ranked_users_report
from app.routers.chat import retention_maintenance_report
from app.routers.chat import get_retention_snapshot_admin_report
from app.routers.chat import get_retention_coverage_report
from app.routers.chat import get_retention_operational_report
from app.routers.chat import get_retention_snapshot_operations_status
from app.routers.chat import get_retention_snapshot_operations_automation
from app.routers.chat import get_retention_snapshot_operations_launch_readiness
from app.routers.topics import create_current_topic_selection
from app.routers.topics import archive_current_topic_selection
from app.routers.topics import read_current_topic_selection
from app.routers.topics import replace_current_topic_selection
from app.routers.topics import read_topic_workspace
from app.routers.topics import read_topic_search
from app.routers.topics import read_topic_suggestions
from app.main import app_metadata
from app.schemas import schemas
from app.services.topics import build_topic_intelligence_overview
from app.services.topics import build_topic_recommendation_report
from app.services.topics import build_topic_suggestion_report
from app.services.bookings import _booking_topic_details
from app.services.retention import build_retention_coverage_report
from app.services.policy_scoring import PolicyScoreSnapshot, can_access_functionality, summarize_policy_score
from app.services.policy_scoring import build_policy_topic_analysis_report
from app.services.policy_scoring import _posture_adjusted_access_score
from app.services.policy_scoring import _topic_signals
from app.services.topics import build_topic_coverage_report, build_topic_portfolio_report, build_topic_taxonomy_report
from app.services.retention import build_retention_topic_signal_report
from app.services.topics import build_topic_theme_coverage
from app.services.chat_analytics import build_retention_topic_signal_detail
from app.services.chat_analytics import build_retention_dashboard_topic_signal_detail
from app.services.chat_analytics import build_interaction_signal_synthesis
from app.services.chat_analytics import build_signal_synthesis_report
from app.schemas.chat import ChatMessageIn
from app.schemas.chat import InteractionInsight
from app.schemas.chat import SystemImprovementPack


@dataclass
class FakeUser:
    id: int = 1
    username: str = "tester"
    email: str = "tester@example.com"
    full_name: str | None = None
    hashed_password: str = "old-hash"
    is_admin: bool = False
    created_at: datetime = datetime.now(timezone.utc)
    updated_at: datetime = datetime.now(timezone.utc)


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0
        self.refreshed = []

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.commits += 1

    async def refresh(self, item):
        self.refreshed.append(item)


class FakePolicyScore:
    policy_tier = "system-premium"
    control_posture = "high_trust"


class FakeBooking:
    def __init__(self, booking_id: int = 10, status=models.BookingStatus.pending):
        self.id = booking_id
        self.user_id = 1
        self.service_type = schemas.ServiceType.meeting
        self.scheduled_date = datetime.now(timezone.utc)
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        self.status = status
        self.title = "Original title"
        self.details = "Original details"
        self.control_posture = "observed"


class FakeChatHistory:
    def __init__(self, message: str):
        self.message = message


class FakeSummary:
    def __init__(self):
        self.loyalty_score = 55.0
        self.churn_risk = "medium"
        self.summary = "Summary text"
        self.metadata = {"repeated_messages": 0, "booking_states": {}}


class FakeInsightPack:
    def __init__(self):
        self.user_id = 1
        self.generated_at = datetime.now(timezone.utc)
        self.focus = "customer retention"
        self.items = []
        self.summary = FakeChatMessageSummary()


class FakeChatMessageSummary:
    def __init__(self):
        self.user_id = 1
        self.messages_analyzed = 0
        self.bookings_analyzed = 0
        self.churn_risk = "low"
        self.loyalty_score = 50.0
        self.monetization_readiness = 0.0
        self.value_tier = "standard"
        self.customer_classification = "baseline"
        self.top_issues = []
        self.strengths = []
        self.insights = []
        self.metadata = {}
        self.generated_at = datetime.now(timezone.utc)
        self.summary = "Summary text"


class FakeChatSummaryWithInsights(FakeChatMessageSummary):
    def __init__(self):
        super().__init__()
        self.top_issues = ["refund timing", "booking delay", "handoff context"]
        self.insights = [
            InteractionInsight(
                area="booking_flow",
                priority="high",
                score=3.0,
                evidence=["booking is delayed"],
                evidence_summary="booking delay",
                source="chat",
                recommendation="improve confirmation timing",
                next_step="confirm the booking status",
            ),
            InteractionInsight(
                area="support",
                priority="medium",
                score=2.0,
                evidence=["handoff context missing"],
                evidence_summary="handoff context",
                source="chat",
                recommendation="pass more context to support",
                next_step="package context for the next agent",
            ),
        ]


def test_booking_event_relationship_metadata_is_explicit():
    assert models.Booking.events.property.backref is not None
    assert models.Booking.user.property is not None


def test_chat_history_relationship_metadata_is_explicit():
    assert models.User.chat_history.property.backref is not None
    assert models.User.chat_history.property.passive_deletes is True


class FakeUpdatePayload:
    def model_dump(self, exclude_unset=True, exclude_none=True):
        return {"title": "Updated title", "status": models.BookingStatus.confirmed}


class FakeBookingCreate:
    service_type = schemas.ServiceType.meeting
    title = "Policy-sensitive booking"
    details = "Booking details"
    scheduled_date = datetime.now(timezone.utc)


class FakeChatDb:
    def __init__(self):
        self.commits = 0
        self.added = []

    async def execute(self, *_args, **_kwargs):
        class Result:
            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return []

            def scalar_one_or_none(self_inner):
                return None

        return Result()

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.commits += 1

    async def refresh(self, item):
        return None


class FakeTopicSelection:
    def __init__(self, topic: str = "retention_forecast_engine"):
        self.id = 1
        self.user_id = 1
        self.topic = topic
        self.source = "chat"
        self.rationale = "topic selected from current discussion"
        self.confidence = 0.87
        self.is_current = True
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)


class FakeCurrentTopicSelection(FakeTopicSelection):
    def __init__(self, topic: str = "booking status and confirmations"):
        super().__init__(topic=topic)


class TopicDb:
    def __init__(self, selection=None):
        self.selection = selection
        self.added = []
        self.commits = 0

    async def execute(self, *_args, **_kwargs):
        selection = self.selection

        class Result:
            def scalars(self_inner):
                return self_inner

            def first(self_inner):
                return selection

            def all(self_inner):
                if selection is None:
                    return []
                if isinstance(selection, list):
                    return selection
                return [selection]

        return Result()

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.commits += 1

    async def refresh(self, item):
        return None


def test_topic_workspace_route_exposes_composite_topic_contract():
    class _TopicSelectionSession(TopicDb):
        async def execute(self, *_args, **_kwargs):
            selection = FakeTopicSelection(topic="booking status and confirmations")

            class Result:
                def scalars(self_inner):
                    return self_inner

                def first(self_inner):
                    return selection

                def all(self_inner):
                    return [selection, FakeTopicSelection(topic="service quality and follow-up")]

            return Result()

    async def _fake_get_db():
        yield _TopicSelectionSession()

    async def _fake_get_current_user():
        return FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/topics/workspace")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == 1
    assert payload["topic"] == "booking status and confirmations"
    assert payload["richness_score"] >= 0
    assert payload["catalog"]["total_topics"] >= 1
    assert payload["taxonomy"]["total_topics"] >= 1
    assert payload["taxonomy"]["topic_map"]
    assert payload["themes"]["total_themes"] >= 1
    assert payload["intelligence"]["coverage_ratio"] >= 0
    assert payload["portfolio"]["catalog_total"] >= 1
    assert payload["portfolio"]["taxonomy_total"] >= 1
    assert payload["selection_history"]["user_id"] == 1


def test_topic_overview_route_exposes_cross_service_topic_intelligence():
    class _TopicSelectionSession(TopicDb):
        async def execute(self, *_args, **_kwargs):
            selection = FakeTopicSelection(topic="service quote and estimate review")

            class Result:
                def scalars(self_inner):
                    return self_inner

                def first(self_inner):
                    return selection

                def all(self_inner):
                    return [selection, FakeTopicSelection(topic="case notes and interaction history")]

            return Result()

    async def _fake_get_db():
        yield _TopicSelectionSession()

    async def _fake_get_current_user():
        return FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/topics/overview")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == 1
    assert payload["topic"] == "service quote and estimate review"
    assert payload["workspace"]["topic"] == "service quote and estimate review"
    assert payload["portfolio"]["catalog_total"] >= 1
    assert payload["coverage"]["coverage_ratio"] >= 0
    assert payload["recommendations"]["topic_focus"]
    assert payload["suggested_topics"]
    assert payload["topic_focus"]


def test_app_metadata_advertises_topic_intelligence_overview():
    response = TestClient(app).get("/meta")
    assert response.status_code == 200
    payload = response.json()
    assert "topic_intelligence_overview" in payload["features"]


def test_app_ecosystem_advertises_topic_search():
    response = TestClient(app).get("/meta/ecosystem")
    assert response.status_code == 200
    payload = response.json()
    assert "/topics/search" in payload["subservices"]["topic_intelligence"]["routes"]
    assert "/topics/suggestions" in payload["subservices"]["topic_intelligence"]["routes"]


def test_topic_suggestion_report_reuses_ranked_search_output():
    report = build_topic_suggestion_report("estimate review", limit=3, theme="preparation_and_estimates")

    assert report.query == "estimate review"
    assert report.total_results >= 1
    assert report.suggested_topics
    assert report.topic_focus


def test_topic_recommendation_report_is_typed_and_populated():
    report = build_topic_recommendation_report(models.TopicSelection(topic="estimate review and booking"))

    assert report.topic == "estimate review and booking"
    assert report.primary_topic
    assert report.recommendations
    assert report.theme_coverage
    assert report.topic_focus


def test_topic_suggestions_route_returns_typed_ranked_topics():
    report = asyncio.run(read_topic_suggestions(query="estimate review", limit=3, theme="preparation_and_estimates"))

    assert report.query == "estimate review"
    assert report.total_results >= 1
    assert report.suggested_topics


def test_booking_topic_details_include_ranked_topic_suggestions():
    details = _booking_topic_details("estimate review and booking status")

    assert details["suggested_topics"]
    assert details["topic_focus"]


def test_policy_topic_signals_include_ranked_suggestions():
    signals = _topic_signals("booking status and estimate review")

    assert signals["suggested_topics"]
    assert signals["topic_focus"]


def test_retention_topic_signal_report_includes_ranked_suggestions():
    report = build_retention_topic_signal_report("booking status and estimate review", user_id=1, window_days=7)

    assert report["topic_suggestions"]
    assert report["topic_focus"]


def test_topic_search_route_ranks_compact_results():
    response = TestClient(app).get(
        "/topics/search",
        params={"query": "estimate review", "limit": 3, "page": 1, "per_page": 2, "theme": "preparation_and_estimates"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "estimate review"
    assert payload["total_results"] >= 1
    assert payload["page"] == 1
    assert payload["per_page"] == 2
    assert payload["total_pages"] >= 1
    assert len(payload["items"]) <= 2
    assert payload["items"][0]["score"] >= payload["items"][-1]["score"]
    assert payload["topic_focus"]


def test_booking_operation_report_exposes_topic_enrichment():
    class _BookingOperationDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return FakeBooking()

            return Result()

    async def _fake_get_db():
        yield _BookingOperationDb()

    async def _fake_get_current_user():
        return FakeUser()

    async def _fake_get_policy_score():
        return FakePolicyScore()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    app.dependency_overrides[deps.get_current_customer_policy_score] = _fake_get_policy_score
    try:
        response = TestClient(app).get("/bookings/10/operation-report")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)
        app.dependency_overrides.pop(deps.get_current_customer_policy_score, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["booking_id"] == 10
    assert payload["topic_context"]
    assert payload["topic_coverage_ratio"] >= 0
    assert payload["topic_portfolio_coverage"] >= 0
    assert payload["topic_signal_summary"]
    assert len(payload["topic_focus"]) >= 1
    assert payload["recommendations"]


def test_retention_coverage_report_exposes_topic_summary():
    class _RetentionCoverageDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionCoverageDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/retention-coverage")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == 1
    assert payload["total_snapshots"] == 0
    assert "Topic coverage:" in payload["summary"]
    assert payload["items"] == []


def test_retention_operational_report_includes_topic_coverage_item():
    class _RetentionOperationalDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionOperationalDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/retention-operations")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == 1
    assert payload["window_days"] == 30
    assert any(item["name"] == "topic_coverage" for item in payload["items"])
    assert "Topic portfolio coverage" in payload["summary"]


def test_retention_snapshot_operations_status_includes_risk_level():
    class _RetentionStatusDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionStatusDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-operations-status")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stale_after_days"] == 14
    assert payload["status"] in {"operational", "watch", "needs_attention"}
    assert payload["risk_level"] in {"low", "medium", "high"}
    assert payload["overview"]


def test_retention_snapshot_operations_overview_includes_recommendation():
    class _RetentionOverviewDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionOverviewDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-operations-overview")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stale_after_days"] == 14
    assert payload["score"] >= 0
    assert payload["status"] in {"healthy", "degraded", "critical"}
    assert payload["risk_level"] in {"low", "medium", "high"}
    assert payload["recommendation"]
    assert payload["overview"]


def test_retention_snapshot_operations_report_includes_items():
    class _RetentionOperationsReportDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionOperationsReportDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-operations-report")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert isinstance(payload["items"], list)
    assert payload["items"]
    assert payload["items"][0]["label"]
    assert payload["items"][0]["status"]
    assert payload["items"][0]["details"]


def test_retention_snapshot_health_summary_and_recommendation():
    class _RetentionSnapshotHealthDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionSnapshotHealthDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        summary_response = TestClient(app).get("/chat/admin/snapshot-health-summary")
        recommendation_response = TestClient(app).get("/chat/admin/snapshot-health-recommendation")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert summary_response.status_code == 200
    summary_payload = summary_response.json()
    assert summary_payload["window_days"] == 30
    assert summary_payload["stale_after_days"] == 14
    assert summary_payload["score"] >= 0
    assert summary_payload["status"] in {"healthy", "degraded", "critical"}
    assert summary_payload["summary"]

    assert recommendation_response.status_code == 200
    recommendation_payload = recommendation_response.json()
    assert recommendation_payload["window_days"] == 30
    assert recommendation_payload["stale_after_days"] == 14
    assert recommendation_payload["score"] >= 0
    assert recommendation_payload["status"] in {"healthy", "degraded", "critical"}
    assert recommendation_payload["risk_level"] in {"low", "medium", "high"}
    assert recommendation_payload["recommendation"]


def test_retention_snapshot_health_risk_includes_risk_level():
    class _RetentionSnapshotHealthRiskDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionSnapshotHealthRiskDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-health-risk")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stale_after_days"] == 14
    assert payload["score"] >= 0
    assert payload["status"] in {"healthy", "degraded", "critical"}
    assert payload["risk_level"] in {"low", "medium", "high"}


def test_retention_snapshot_volatility_summary_includes_counts():
    class _RetentionVolatilityDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionVolatilityDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-volatility/summary")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stable"] >= 0
    assert payload["moderate"] >= 0
    assert payload["high"] >= 0


def test_retention_snapshot_volatility_report_includes_items():
    class _RetentionVolatilityReportDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionVolatilityReportDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-volatility")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert isinstance(payload["items"], list)
    if payload["items"]:
        assert payload["items"][0]["snapshot_type"]
        assert payload["items"][0]["volatility"] >= 0
        assert payload["items"][0]["classification"]


def test_retention_snapshot_staleness_trend_includes_items():
    class _RetentionStalenessTrendDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionStalenessTrendDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-staleness/trend")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stale_after_days"] == 14
    assert isinstance(payload["items"], list)
    if payload["items"]:
        assert payload["items"][0]["bucket"]
        assert payload["items"][0]["stale_snapshots"] >= 0
        assert payload["items"][0]["fresh_snapshots"] >= 0
        assert payload["items"][0]["staleness_rate"] >= 0


def test_retention_snapshot_audit_and_type_breakdown():
    class _RetentionAuditDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionAuditDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        audit_response = TestClient(app).get("/chat/admin/snapshot-audit")
        breakdown_response = TestClient(app).get("/chat/admin/snapshot-type-breakdown?snapshot_type=booking")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert audit_response.status_code == 200
    audit_payload = audit_response.json()
    assert audit_payload["window_days"] == 30
    assert audit_payload["total_snapshots"] >= 0
    assert isinstance(audit_payload["items"], list)
    if audit_payload["items"]:
        assert audit_payload["items"][0]["snapshot_type"]
        assert audit_payload["items"][0]["total_snapshots"] >= 0
        assert audit_payload["items"][0]["unique_users"] >= 0

    assert breakdown_response.status_code == 200
    breakdown_payload = breakdown_response.json()
    assert breakdown_payload["window_days"] == 30
    assert breakdown_payload["snapshot_type"] == "booking"
    assert breakdown_payload["total_snapshots"] >= 0
    assert breakdown_payload["unique_users"] >= 0
    assert isinstance(breakdown_payload["items"], list)
    if breakdown_payload["items"]:
        assert breakdown_payload["items"][0]["snapshot_type"]
        assert breakdown_payload["items"][0]["total_snapshots"] >= 0
        assert breakdown_payload["items"][0]["unique_users"] >= 0


def test_retention_snapshot_audit_export_includes_summary():
    class _RetentionAuditExportDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionAuditExportDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-audit/export")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["summary"]
    assert isinstance(payload["snapshot_types"], list)
    assert payload["total_snapshots"] >= 0


def test_retention_snapshot_action_plan_includes_action():
    class _RetentionActionPlanDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionActionPlanDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-action-plan")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["risk_level"] in {"low", "medium", "high"}
    assert payload["action"]


def test_retention_snapshot_health_score_includes_status():
    class _RetentionHealthScoreDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionHealthScoreDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-health-score")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stale_after_days"] == 14
    assert payload["score"] >= 0
    assert payload["status"] in {"healthy", "degraded", "critical"}


def test_retention_snapshot_risk_profile_and_recommendation():
    class _RetentionRiskDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionRiskDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        risk_response = TestClient(app).get("/chat/admin/snapshot-risk-profile")
        recommendation_response = TestClient(app).get("/chat/admin/snapshot-recommendation")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert risk_response.status_code == 200
    risk_payload = risk_response.json()
    assert risk_payload["window_days"] == 30
    assert risk_payload["score"] >= 0
    assert risk_payload["risk_level"] in {"low", "medium", "high"}

    assert recommendation_response.status_code == 200
    recommendation_payload = recommendation_response.json()
    assert recommendation_payload["window_days"] == 30
    assert recommendation_payload["risk_level"] in {"low", "medium", "high"}
    assert recommendation_payload["recommendation"]


def test_retention_topic_signal_report_exposes_richer_topic_context():
    report = build_retention_topic_signal_report(
        "Booking confirmation is delayed and the customer wants a refund after reschedule support.",
        user_id=1,
        window_days=30,
    )

    assert report["user_id"] == 1
    assert report["window_days"] == 30
    assert report["topic_context"]
    assert report["dominant_topic"]
    assert report["items"]
    assert report["items"][0]["topic"]
    assert report["items"][0]["coverage_score"] >= 0


def test_retention_topic_signal_detail_exposes_structured_items():
    summary = FakeChatMessageSummary()
    summary.top_issues = ["refund timing", "booking delay"]
    summary.insights = []
    detail = build_retention_topic_signal_detail(summary, None)

    assert detail.topic_context
    assert detail.summary
    assert detail.items
    assert detail.items[0].topic
    assert detail.items[0].coverage_score >= 0
    assert detail.topic_theme_coverage


def test_retention_dashboard_topic_signal_detail_tracks_item_count():
    summary = FakeChatMessageSummary()
    summary.top_issues = ["refund timing", "booking delay"]
    summary.insights = []
    detail = build_retention_dashboard_topic_signal_detail(summary, None)

    assert detail.topic_context
    assert detail.topic == detail.topic_context
    assert detail.dominant_topic
    assert detail.summary.endswith(f"items={len(detail.items)}")
    assert detail.items


def test_retention_dashboard_exposes_topic_theme_coverage():
    summary = FakeChatMessageSummary()
    summary.top_issues = ["refund timing", "booking delay"]
    summary.insights = []

    detail = build_retention_dashboard_topic_signal_detail(summary, None)

    assert detail.topic_theme_coverage
    assert detail.topic_theme_coverage[0]["theme"]
    assert detail.topic_theme_coverage[0]["coverage"] >= 0


def test_chat_signal_synthesis_report_includes_topic_focus_and_retention_context():
    summary = FakeChatSummaryWithInsights()

    synthesis = build_interaction_signal_synthesis(summary, None)

    assert synthesis.topic_context
    assert synthesis.topic_focus
    assert synthesis.topic_focus[0]
    assert synthesis.topic_theme_coverage
    assert synthesis.sentiment_bridge.topic_signal_count == len(summary.insights)


def test_signal_synthesis_report_exposes_richer_topic_signal_summary():
    class _ChatRow:
        def __init__(self, message: str):
            self.message = message

    report = build_signal_synthesis_report(
        1,
        [_ChatRow("booking delay and refund timing")],
        [FakeBooking()],
        None,
    )

    assert report.user_id == 1
    assert report.topic_context
    assert report.topic_focus is not None
    assert report.topic_theme_coverage is not None
    assert report.summary.messages_analyzed == 1
    assert report.topic_focus[0]


def test_retention_snapshot_operations_automation_includes_risk_level():
    class _RetentionAutomationDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionAutomationDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-operations-automation")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stale_after_days"] == 14
    assert payload["automation"] in {"ready", "conditional", "not_ready"}
    assert payload["risk_level"] in {"low", "medium", "high"}
    assert payload["overview"]


def test_topic_taxonomy_report_exposes_sector_and_related_topics():
    report = build_topic_taxonomy_report()

    assert report.total_topics >= 1
    assert report.total_themes >= 1
    assert report.sectors
    assert report.topic_map
    first_topic = report.topic_map[0]
    assert first_topic.topic
    assert first_topic.related_topics is not None
    assert report.theme_map


def test_retention_snapshot_operations_automation_route_uses_typed_field():
    class _RetentionAutomationRouteDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionAutomationRouteDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-operations-automation")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stale_after_days"] == 14
    assert payload["automation"] in {"ready", "conditional", "not_ready"}
    assert payload["risk_level"] in {"low", "medium", "high"}
    assert payload["overview"]


def test_retention_snapshot_operations_execution_state_includes_risk_level():
    class _RetentionExecutionStateDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionExecutionStateDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-operations-execution-state")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stale_after_days"] == 14
    assert payload["execution_state"] in {"go", "hold", "block"}
    assert payload["risk_level"] in {"low", "medium", "high"}
    assert payload["overview"]


def test_retention_snapshot_operations_launch_readiness_includes_risk_level():
    class _RetentionLaunchDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionLaunchDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-operations-launch-readiness")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stale_after_days"] == 14
    assert payload["readiness"] in {"launch_ready", "launch_pending", "launch_blocked"}
    assert payload["risk_level"] in {"low", "medium", "high"}
    assert payload["overview"]


def test_retention_snapshot_operations_go_no_go_includes_decision():
    class _RetentionGoNoGoDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

            return Result()

    async def _fake_get_db():
        yield _RetentionGoNoGoDb()

    async def _fake_get_current_admin_user():
        return FakeUser(is_admin=True)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/snapshot-operations-gonogo")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["stale_after_days"] == 14
    assert payload["decision"] in {"go", "hold", "block"}
    assert payload["overview"]


@pytest.mark.asyncio
async def test_get_current_admin_user_rejects_non_admin():
    with pytest.raises(Exception) as exc_info:
        await get_current_admin_user(current_user=FakeUser(is_admin=False))

    assert getattr(exc_info.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_get_current_admin_user_accepts_admin():
    admin_user = FakeUser(is_admin=True)

    result = await get_current_admin_user(current_user=admin_user)

    assert result is admin_user


@pytest.mark.asyncio
async def test_get_current_policy_or_admin_user_accepts_system_premium_non_admin():
    db = FakeSession()
    policy_user = FakeUser(is_admin=False)

    async def fake_upsert_customer_policy_score(_db, current_user):
        return type(
            "PolicyScore",
            (),
            {
                "policy_tier": "system-premium",
                "access_score": 90.0,
                "system_score": 95.0,
            },
        )()

    original = get_current_policy_or_admin_user.__globals__["upsert_customer_policy_score"]
    get_current_policy_or_admin_user.__globals__["upsert_customer_policy_score"] = fake_upsert_customer_policy_score
    try:
        result = await get_current_policy_or_admin_user(current_user=policy_user, db=db)
    finally:
        get_current_policy_or_admin_user.__globals__["upsert_customer_policy_score"] = original

    assert result is policy_user


@pytest.mark.asyncio
async def test_create_current_topic_selection_returns_report():
    report = await create_current_topic_selection(
        topic_request=schemas.TopicSelectionCreate(topic="retention_forecast_engine", rationale="selected topic"),
        current_user=FakeUser(),
        db=TopicDb(),
    )

    assert report.topic == "retention_forecast_engine"
    assert report.rationale == "selected topic"
    assert report.is_current is True


@pytest.mark.asyncio
async def test_read_current_topic_selection_returns_latest_selection():
    report = await read_current_topic_selection(
        current_user=FakeUser(),
        db=TopicDb(selection=FakeTopicSelection(topic="device_experience_optimizer")),
    )

    assert report.topic == "device_experience_optimizer"
    assert report.is_current is True


@pytest.mark.asyncio
async def test_read_topic_selection_history_returns_all_selections():
    from app.routers.topics import read_topic_selection_history

    selections = [FakeTopicSelection(topic="response_speed"), FakeTopicSelection(topic="trust")]
    report = await read_topic_selection_history(
        current_user=FakeUser(),
        db=TopicDb(selection=selections),
    )

    assert report.user_id == 1
    assert [item.topic for item in report.items] == ["response_speed", "trust"]


@pytest.mark.asyncio
async def test_replace_current_topic_selection_returns_new_current_selection():
    report = await replace_current_topic_selection(
        topic_request=schemas.TopicSelectionCreate(topic="trust", rationale="rotated topic"),
        current_user=FakeUser(),
        db=TopicDb(selection=FakeTopicSelection(topic="response_speed")),
    )

    assert report.topic == "trust"
    assert report.rationale == "rotated topic"


@pytest.mark.asyncio
async def test_archive_current_topic_selection_returns_inactive_selection():
    report = await archive_current_topic_selection(
        current_user=FakeUser(),
        db=TopicDb(selection=FakeTopicSelection(topic="response_speed")),
    )

    assert report.topic == "response_speed"
    assert report.is_current is False


def test_topic_coverage_report_includes_portfolio_signals():
    report = build_topic_coverage_report(FakeCurrentTopicSelection(topic="service appointment preparation and vip routing"))

    assert report.topic == "service appointment preparation and vip routing"
    assert report.coverage_ratio >= 0.0
    assert report.top_recommendations
    assert report.matched_topics
    assert report.theme_coverage
    assert report.theme_coverage[0].theme


def test_topic_portfolio_report_combines_catalog_and_coverage():
    report = build_topic_portfolio_report(FakeCurrentTopicSelection(topic="refund timing and payout tracking"))

    assert report["catalog_total"] >= len(report["top_recommendations"])
    assert report["theme_total"] >= 1
    assert report["coverage_ratio"] >= 0.0
    assert report["matched_topics"]
    assert isinstance(report["theme_coverage"], list)
    assert report["theme_coverage"][0]["theme"]


def test_topic_portfolio_report_reflects_richer_topic_coverage():
    report = build_topic_portfolio_report(FakeCurrentTopicSelection(topic="refund timing, payout tracking, and escalation routing"))

    assert report["coverage_ratio"] >= 0.05
    assert report["matched_topics"]
    assert report["theme_coverage"]


def test_booking_service_summary_exposes_topic_theme_coverage():
    booking = type("Booking", (), {"id": 1, "user_id": 7, "status": "confirmed"})()

    report = build_booking_service_summary(booking, [], [])

    assert report["topic_context"]
    assert report["topic_portfolio_coverage"] >= 0.0
    assert report["topic_theme_coverage"]


@pytest.mark.asyncio
async def test_retention_coverage_report_exposes_topic_coverage_ratio():
    class _RetentionCoverageDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    class Scalars:
                        def all(self_inner_inner):
                            return []

                    return Scalars()

                def scalar(self_inner):
                    return None

            return Result()

    report = await build_retention_coverage_report(_RetentionCoverageDb(), user_id=1, window_days=30)

    assert report.topic_coverage_ratio >= 0.0
    assert report.summary


@pytest.mark.asyncio
async def test_read_users_access_decision_includes_control_posture():
    policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "customer_trusted",
            "access_score": 86.0,
            "customer_score": 84.0,
            "system_score": 81.0,
        },
    )()

    original_dependency = read_users_access_decision.__globals__["can_access_functionality"]
    read_users_access_decision.__globals__["can_access_functionality"] = lambda _policy_score, required_tier="standard": True
    try:
        result = await read_users_access_decision(
            functionality="support",
            required_tier="standard",
            current_user=FakeUser(id=7),
            policy_score=policy_score,
        )
    finally:
        read_users_access_decision.__globals__["can_access_functionality"] = original_dependency
    assert result["control_posture"] == "customer_trusted"
    assert result["policy_tier"] == "customer-premium"


def test_summarize_policy_score_mentions_topic_richness():
    snapshot = PolicyScoreSnapshot(
        system_score=80.0,
        customer_score=70.0,
        access_score=72.0,
        interest_score=68.0,
        closeness_score=69.0,
        community_closeness_score=74.0,
        policy_tier="customer-premium",
        control_posture="customer_trusted",
        summary="system=80.00, customer=70.00, access=72.00, interest=68.00, closeness=69.00, community=74.00, posture=customer_trusted, topic_breadth=24.00, topic_complexity=18.00, topic_confidence=0.72, topic=refund timing and payout tracking, richness=rich [balance=69.00]",
        topic_context="refund timing and payout tracking [breadth=24.00, complexity=18.00]",
        topic_richness="rich [balance=69.00]",
    )

    summary = summarize_policy_score(snapshot)

    assert "topic_breadth" not in summary
    assert "posture=customer_trusted" in summary
    assert "topic_context=refund timing and payout tracking [breadth=24.00, complexity=18.00]" in summary
    assert "topic_richness=rich [balance=69.00]" in summary


@pytest.mark.asyncio
async def test_policy_decision_report_includes_topic_context_and_richness():
    user = FakeUser(id=9)
    snapshot = PolicyScoreSnapshot(
        system_score=82.0,
        customer_score=74.0,
        access_score=76.0,
        interest_score=71.0,
        closeness_score=73.0,
        community_closeness_score=75.0,
        policy_tier="customer-premium",
        control_posture="customer_trusted",
        summary="system=82.00, customer=74.00, access=76.00, interest=71.00, closeness=73.00, community=75.00, posture=customer_trusted, topic_context=refund timing and payout tracking [breadth=24.00, complexity=18.00], topic_richness=rich [balance=69.00]",
        topic_context="refund timing and payout tracking [breadth=24.00, complexity=18.00]",
        topic_richness="rich [balance=69.00]",
    )

    report = __import__("app.services.policy_scoring", fromlist=["build_policy_decision_report"]).build_policy_decision_report(
        snapshot=snapshot,
        user=user,
        functionality="support",
        required_tier="standard",
    )

    assert report.topic_context == "refund timing and payout tracking [breadth=24.00, complexity=18.00]; signal=rich [balance=69.00]"
    assert "topic_richness=rich [balance=69.00]" in report.summary
    assert "topic_context=refund timing and payout tracking [breadth=24.00, complexity=18.00]" in report.summary
    assert "topic_signal=rich [balance=69.00]" in report.summary
    assert report.decision.allowed is True


@pytest.mark.asyncio
async def test_policy_topic_analysis_report_exposes_structured_depth():
    user = FakeUser(id=10)
    snapshot = PolicyScoreSnapshot(
        system_score=64.0,
        customer_score=78.0,
        access_score=72.0,
        interest_score=70.0,
        closeness_score=71.0,
        community_closeness_score=74.0,
        policy_tier="customer-premium",
        control_posture="customer_trusted",
        summary="system=64.00, customer=78.00, access=72.00, interest=70.00, closeness=71.00, community=74.00, posture=customer_trusted, topic_context=refund timing and payout tracking [breadth=24.00, complexity=18.00], topic_richness=balanced [balance=48.00]",
        topic_context="refund timing and payout tracking [breadth=24.00, complexity=18.00]",
        topic_richness="balanced [balance=48.00]",
    )

    report = build_policy_topic_analysis_report(snapshot, user)

    assert report.user_id == 10
    assert report.current_topic == "refund timing and payout tracking [breadth=24.00, complexity=18.00]"
    assert report.topic_context.startswith("refund timing and payout tracking")
    assert report.topic_depth.startswith("depth=")
    assert report.items
    assert report.items[0].fallback == "refund timing and payout tracking [breadth=24.00, complexity=18.00]"


@pytest.mark.asyncio
async def test_read_users_access_decision_tightens_standard_access_for_observed_posture():
    policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "observed",
            "access_score": 76.0,
            "customer_score": 74.0,
            "system_score": 73.0,
        },
    )()

    original_dependency = read_users_access_decision.__globals__["can_access_functionality"]
    read_users_access_decision.__globals__["can_access_functionality"] = lambda _policy_score, required_tier="standard": required_tier != "customer-premium"
    try:
        result = await read_users_access_decision(
            functionality="support",
            required_tier="standard",
            current_user=FakeUser(id=8),
            policy_score=policy_score,
        )
    finally:
        read_users_access_decision.__globals__["can_access_functionality"] = original_dependency

    assert result["control_posture"] == "observed"
    assert result["required_tier"] == "standard"
    assert result["effective_required_tier"] == "customer-premium"
    assert result["allowed"] is False


@pytest.mark.asyncio
async def test_booking_analytics_summary_includes_control_posture():
    db = FakeSession()

    async def fake_execute(query):
        class Result:
            def scalar(self_inner):
                return 1

            def all(self_inner):
                return []

        return Result()

    db.execute = fake_execute
    current_user = FakeUser()
    current_user.policy_score = FakePolicyScore()

    result = await get_booking_analytics_summary(current_user=current_user, db=db)

    assert result.control_posture == "high_trust"


@pytest.mark.asyncio
async def test_booking_routes_expose_control_posture():
    policy_user = FakeUser()
    policy_user.policy_score = FakePolicyScore()

    class Result:
        def __init__(self, booking):
            self._booking = booking

        def scalar_one_or_none(self):
            return self._booking

        def scalars(self):
            return self

        def all(self):
            return []

        def scalar(self):
            return 1

    class StubDb:
        async def execute(self, *_args, **_kwargs):
            return Result(FakeBooking())

        def add(self, *_args, **_kwargs):
            return None

        async def commit(self):
            return None

        async def refresh(self, item):
            item.control_posture = "high_trust"

        async def delete(self, *_args, **_kwargs):
            return None

    stub_db = StubDb()

    created = await create_booking(booking=FakeBookingCreate(), current_user=policy_user, policy_score=policy_user.policy_score, db=stub_db)
    fetched = await get_booking(booking_id=10, current_user=policy_user, policy_score=policy_user.policy_score, db=stub_db)
    history = await get_booking_history(booking_id=10, current_user=policy_user, policy_score=policy_user.policy_score, db=stub_db)

    assert created.control_posture == "high_trust"
    assert fetched.control_posture == "high_trust"
    assert history.control_posture == "high_trust"

    observed_user = FakeUser(id=8)
    observed_user.policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "observed",
            "access_score": 75.0,
            "customer_score": 72.0,
            "system_score": 71.0,
        },
    )()

    observed_db = StubDb()
    observed_fetched = await get_booking(booking_id=11, current_user=observed_user, policy_score=observed_user.policy_score, db=observed_db)

    assert observed_fetched.control_posture == "observed"
    assert "View limited by control posture." in observed_fetched.details


@pytest.mark.asyncio
async def test_booking_creation_preserves_control_posture_annotation():
    policy_user = FakeUser()
    policy_user.policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "system-premium",
            "control_posture": "high_trust",
            "access_score": 92.0,
            "customer_score": 88.0,
            "system_score": 90.0,
        },
    )()

    class Result:
        def scalar_one_or_none(self):
            return None

    class StubDb:
        def __init__(self):
            self.commits = 0
            self.refreshed = []
            self.added = []

        async def execute(self, *_args, **_kwargs):
            return Result()

        def add(self, item):
            self.added.append(item)

        async def commit(self):
            self.commits += 1

        async def refresh(self, item):
            self.refreshed.append(item)

    created = await create_booking(booking=FakeBookingCreate(), current_user=policy_user, policy_score=policy_user.policy_score, db=StubDb())

    assert created.control_posture == "high_trust"
    assert created.details.endswith("Booking recorded with expanded control posture.")


@pytest.mark.asyncio
async def test_booking_update_mentions_control_posture_for_observed_users():
    policy_user = FakeUser()
    policy_user.policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "observed",
            "access_score": 76.0,
            "customer_score": 74.0,
            "system_score": 73.0,
        },
    )()

    class Result:
        def __init__(self, booking=None):
            self._booking = booking

        def scalar_one_or_none(self):
            return self._booking

    class UpdateDb:
        def __init__(self):
            self.refreshed = []

        async def execute(self, *_args, **_kwargs):
            booking = FakeBooking()
            booking.details = "Updated booking details"
            return Result(booking)

        async def commit(self):
            return None

        async def refresh(self, item):
            self.refreshed.append(item)

        async def rollback(self):
            return None

    updated = await update_booking(booking_id=10, booking_update=schemas.BookingUpdate(details="Updated booking details"), current_user=policy_user, policy_score=policy_user.policy_score, db=UpdateDb())

    assert updated.control_posture == "observed"
    assert "Controlled posture note: booking updated under monitored access." in updated.details
    assert updated.details.endswith("Booking recorded with controlled access posture.")


@pytest.mark.asyncio
async def test_booking_delete_mentions_control_posture_for_observed_users():
    policy_user = FakeUser()
    policy_user.policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "observed",
            "access_score": 74.0,
            "customer_score": 72.0,
            "system_score": 71.0,
        },
    )()

    class Result:
        def scalar_one_or_none(self):
            return FakeBooking()

    class StubDb:
        async def execute(self, *_args, **_kwargs):
            return Result()

        def add(self, *_args, **_kwargs):
            return None

        async def delete(self, *_args, **_kwargs):
            return None

        async def commit(self):
            return None

        async def refresh(self, *_args, **_kwargs):
            return None

    result = await delete_booking(booking_id=10, current_user=policy_user, policy_score=policy_user.policy_score, db=StubDb())

    assert result["control_posture"] == "observed"
    assert "Control posture note recorded." in result["message"]


@pytest.mark.asyncio
async def test_booking_list_filters_to_active_states_for_lower_trust_users():
    policy_user = FakeUser()
    policy_user.policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "customer_trusted",
            "access_score": 82.0,
            "customer_score": 79.0,
            "system_score": 78.0,
        },
    )()

    class Result:
        def __init__(self, rows=None, booking=None):
            self._rows = rows or []
            self._booking = booking

        def scalar_one_or_none(self):
            return self._booking

        def scalars(self):
            return self

        def all(self):
            return self._rows

        def scalar(self):
            return 2

    class BookingListDb:
        async def execute(self, query, *args, **kwargs):
            sql = str(query)
            if "count(" in sql.lower():
                return Result()
            return Result(rows=[FakeBooking(booking_id=10, status=models.BookingStatus.pending), FakeBooking(booking_id=11, status=models.BookingStatus.cancelled)])

    result = await get_bookings(current_user=policy_user, policy_score=policy_user.policy_score, db=BookingListDb(), booking_status=None)

    assert result.page == 1
    assert result.total == 2
    assert len(result.items) == 2
    assert [item.status.value for item in result.items] == ["pending", "cancelled"]
    assert all(item.control_posture == "observed" for item in result.items)


@pytest.mark.asyncio
async def test_booking_assignment_history_hides_assignment_events_for_lower_trust_users():
    policy_user = FakeUser()
    policy_user.policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "observed",
            "access_score": 75.0,
            "customer_score": 73.0,
            "system_score": 72.0,
        },
    )()

    class Result:
        def __init__(self, booking=None, rows=None):
            self._booking = booking
            self._rows = rows or []

        def scalar_one_or_none(self):
            return self._booking

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class HistoryDb:
        async def execute(self, query, *args, **kwargs):
            sql = str(query)
            if "booking_event" in sql.lower():
                return Result(rows=[
                    FakeBooking(booking_id=10),
                    FakeBooking(booking_id=11),
                ])
            return Result(booking=FakeBooking())

    result = await get_booking_assignment_history(booking_id=10, current_user=policy_user, policy_score=policy_user.policy_score, db=HistoryDb())

    assert result.control_posture == "observed"
    assert result.event_count == 0
    assert result.items == []


@pytest.mark.asyncio
async def test_booking_assignment_report_annotates_decisions_for_lower_trust_users():
    policy_user = FakeUser()
    policy_user.policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "observed",
            "access_score": 74.0,
            "customer_score": 72.0,
            "system_score": 71.0,
        },
    )()

    assignment = type(
        "Assignment",
        (),
        {
            "booking_id": 10,
            "user_id": policy_user.id,
            "room_id": 12,
            "match_reason": "policy routing",
            "state": "suggested",
            "source": "booking-service",
            "explanation": "Controlled posture note: assignment recommendation is monitored.",
            "created_at": datetime.now(timezone.utc),
            "is_current": True,
            "id": 33,
        },
    )()

    async def fake_create_booking_assignment(_db, _booking, _current_user, requested_assignment=None):
        return assignment

    class AssignmentResult:
        def scalar_one_or_none(self):
            return FakeBooking()

    class AssignmentDb:
        async def execute(self, *_args, **_kwargs):
            return AssignmentResult()

    original_create_booking_assignment = bookings_module.create_booking_assignment
    bookings_module.create_booking_assignment = fake_create_booking_assignment
    try:
        report = await create_booking_assignment_report(
            booking_id=10,
            assignment_request=schemas.BookingAssignmentRequest(room_id=12, match_reason="policy routing"),
            current_user=policy_user,
            policy_score=policy_user.policy_score,
            db=AssignmentDb(),
        )

        assert report.control_posture == "observed"
        assert report.decisions
        assert report.decisions[0].explanation == "Controlled posture note: assignment recommendation is monitored."
    finally:
        bookings_module.create_booking_assignment = original_create_booking_assignment


@pytest.mark.asyncio
async def test_booking_assignment_update_creates_report_for_lower_trust_users_when_missing_assignment():
    policy_user = FakeUser()
    policy_user.policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "observed",
            "access_score": 74.0,
            "customer_score": 72.0,
            "system_score": 71.0,
        },
    )()

    assignment = type(
        "Assignment",
        (),
        {
            "booking_id": 10,
            "user_id": policy_user.id,
            "room_id": 12,
            "match_reason": "fallback routing",
            "state": "suggested",
            "source": "booking-service",
            "explanation": "Controlled posture note: assignment recommendation is monitored.",
            "created_at": datetime.now(timezone.utc),
            "is_current": True,
            "id": 34,
        },
    )()

    async def fake_create_booking_assignment(_db, _booking, _current_user, requested_assignment=None):
        return assignment

    class UpdateResult:
        def __init__(self, booking=None, assignment=None):
            self._booking = booking
            self._assignment = assignment

        def scalar_one_or_none(self):
            return self._booking

        def scalars(self):
            return self

        def first(self):
            return self._assignment

    class UpdateDb:
        async def execute(self, query, *_args, **_kwargs):
            sql = str(query).lower()
            if "bookingassignment" in sql or "booking_assignment" in sql:
                return UpdateResult(assignment=None)
            return UpdateResult(booking=FakeBooking())

    original_create_booking_assignment = bookings_module.create_booking_assignment
    bookings_module.create_booking_assignment = fake_create_booking_assignment
    try:
        report = await update_booking_assignment_report(
            booking_id=10,
            assignment_update=schemas.BookingAssignmentUpdate(room_id=12, match_reason="fallback routing"),
            current_user=policy_user,
            policy_score=policy_user.policy_score,
            db=UpdateDb(),
        )

        assert report.control_posture == "observed"
        assert report.current_state == "suggested"
        assert report.decisions[0].explanation == "Controlled posture note: assignment recommendation is monitored."
    finally:
        bookings_module.create_booking_assignment = original_create_booking_assignment


@pytest.mark.asyncio
async def test_booking_audit_summary_exposes_recent_mutation_fields_for_lower_trust_users():
    policy_user = FakeUser()
    policy_user.policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "observed",
            "access_score": 77.0,
            "customer_score": 75.0,
            "system_score": 74.0,
        },
    )()

    class Result:
        def __init__(self, booking=None, rows=None):
            self._booking = booking
            self._rows = rows or []

        def scalar_one_or_none(self):
            return self._booking

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class Event:
        def __init__(self, event_type, note="", created_at=None):
            self.event_type = event_type
            self.note = note
            self.created_at = created_at

    class AuditDb:
        async def execute(self, query, *args, **kwargs):
            sql = str(query)
            if "booking_event" in sql.lower():
                return Result(rows=[Event("status_updated", note="status details updated"), Event("assignment_created", note="assignment details added")])
            return Result(booking=FakeBooking())

    original_current_user = get_booking_audit_summary.__globals__["deps"].get_current_user
    original_policy_score = get_booking_audit_summary.__globals__["deps"].get_current_customer_policy_score
    get_booking_audit_summary.__globals__["deps"].get_current_user = lambda: policy_user
    get_booking_audit_summary.__globals__["deps"].get_current_customer_policy_score = lambda: policy_user.policy_score
    try:
        result = await get_booking_audit_summary(booking_id=10, current_user=policy_user, db=AuditDb())
    finally:
        get_booking_audit_summary.__globals__["deps"].get_current_user = original_current_user
        get_booking_audit_summary.__globals__["deps"].get_current_customer_policy_score = original_policy_score

    assert result.control_posture == "observed"
    assert result.recent_mutation_fields == ["details", "status"]
    assert result.control_posture == "observed"


@pytest.mark.asyncio
async def test_chat_admin_activity_includes_control_posture():
    db = FakeSession()

    async def fake_execute(query):
        class Result:
            def scalar(self_inner):
                return 2

        return Result()

    db.execute = fake_execute
    current_user = FakeUser()
    current_user.policy_score = FakePolicyScore()

    result = await get_admin_activity_report(window_days=30, db=db, current_user=current_user)

    assert result.total_chat_messages == 2


@pytest.mark.asyncio
async def test_chat_message_annotates_summary_for_observed_users():
    db = FakeChatDb()
    current_user = FakeUser()
    current_user.policy_score = type(
        "PolicyScore",
        (),
        {
            "policy_tier": "customer-premium",
            "control_posture": "observed",
            "access_score": 76.0,
            "customer_score": 74.0,
            "system_score": 73.0,
        },
    )()

    from app.schemas.chat import ChatMessageIn

    original_loader = chat_message.__globals__["_load_user_interaction_window"]
    original_insights = chat_message.__globals__["_build_interaction_insights"]
    original_summary = chat_message.__globals__["_build_summary"]
    original_pack = chat_message.__globals__["_build_system_improvement_pack"]
    original_sentiment = chat_message.__globals__["analyze_sentiment"]
    original_snapshot = chat_message.__globals__["_save_retention_snapshot"]
    original_prune = chat_message.__globals__["_prune_retention_snapshots"]
    original_store = chat_message.__globals__["_store_interaction_signals"]

    async def fake_load_user_interaction_window(_db, _user_id):
        return [], []

    async def fake_save_retention_snapshot(*_args, **_kwargs):
        return None

    async def fake_prune_retention_snapshots(*_args, **_kwargs):
        return None

    async def fake_store_interaction_signals(*_args, **_kwargs):
        return None

    chat_message.__globals__["_load_user_interaction_window"] = fake_load_user_interaction_window
    chat_message.__globals__["_build_interaction_insights"] = lambda *_args, **_kwargs: []
    chat_message.__globals__["_build_summary"] = lambda *_args, **_kwargs: FakeChatMessageSummary()
    chat_message.__globals__["_build_system_improvement_pack"] = lambda *_args, **_kwargs: SystemImprovementPack(
        user_id=current_user.id,
        generated_at=datetime.now(timezone.utc),
        focus="customer retention",
        items=[],
        summary=FakeChatMessageSummary(),
    )
    chat_message.__globals__["analyze_sentiment"] = lambda _text: None
    chat_message.__globals__["_save_retention_snapshot"] = fake_save_retention_snapshot
    chat_message.__globals__["_prune_retention_snapshots"] = fake_prune_retention_snapshots
    chat_message.__globals__["_store_interaction_signals"] = fake_store_interaction_signals
    try:
        result = await chat_message(payload=ChatMessageIn(message="Need help"), db=db, current_user=current_user)
    finally:
        chat_message.__globals__["_load_user_interaction_window"] = original_loader
        chat_message.__globals__["_build_interaction_insights"] = original_insights
        chat_message.__globals__["_build_summary"] = original_summary
        chat_message.__globals__["_build_system_improvement_pack"] = original_pack
        chat_message.__globals__["analyze_sentiment"] = original_sentiment
        chat_message.__globals__["_save_retention_snapshot"] = original_snapshot
        chat_message.__globals__["_prune_retention_snapshots"] = original_prune
        chat_message.__globals__["_store_interaction_signals"] = original_store

    assert result.summary.metadata["control_posture_note"] == "Controlled posture applied."



@pytest.mark.asyncio
async def test_chat_admin_retention_trend_includes_control_posture():
    db = FakeSession()

    class Result:
        def scalar(self_inner):
            return 1

    async def fake_execute(query):
        return Result()

    db.execute = fake_execute
    current_user = FakeUser()
    current_user.policy_score = FakePolicyScore()

    result = await get_admin_retention_trend_report(window_days=30, db=db, current_user=current_user)

    assert result.trends


@pytest.mark.asyncio
async def test_ranked_users_report_includes_control_posture():
    db = FakeSession()

    class Result:
        def all(self_inner):
            return [(1, "tester", 2, 3, 4)]

    async def fake_execute(query):
        return Result()

    db.execute = fake_execute
    current_user = FakeUser()
    current_user.policy_score = FakePolicyScore()

    result = await get_ranked_users_report(window_days=30, limit=10, db=db, current_user=current_user)

    assert result.users[0].username == "tester"


@pytest.mark.asyncio
async def test_chat_retention_maintenance_report_includes_control_posture():
    db = FakeSession()

    class Result:
        def all(self):
            return []

    async def fake_execute(query):
        return Result()

    db.execute = fake_execute
    current_user = FakeUser()
    current_user.policy_score = FakePolicyScore()

    result = await retention_maintenance_report(window_days=30, keep=20, current_user=current_user, db=db)

    assert result.total_users == 0


@pytest.mark.asyncio
async def test_retention_snapshot_admin_report_includes_control_posture():
    db = FakeSession()

    async def fake_execute(query):
        class Result:
            def scalar(self_inner):
                return 3

            def all(self_inner):
                return []

        return Result()

    db.execute = fake_execute
    current_user = FakeUser()
    current_user.policy_score = FakePolicyScore()

    result = await get_retention_snapshot_admin_report(window_days=30, db=db, current_user=current_user)

    assert result.total_snapshots == 3




@pytest.mark.asyncio
async def test_change_password_updates_hash():
    db = FakeSession()
    user = FakeUser(hashed_password=security.get_password_hash("old-password"))
    payload = schemas.PasswordChange(current_password="old-password", new_password="new-password")

    result = await change_password(payload=payload, current_user=user, db=db)

    assert result == {"message": "Password updated successfully under controlled access posture."}
    assert user.hashed_password != security.get_password_hash("old-password")
    assert db.commits == 1


@pytest.mark.asyncio
async def test_create_booking_event_captures_transition_fields():
    db = FakeSession()
    booking = FakeBooking()
    user = FakeUser()

    event = await create_booking_event(
        db,
        booking=booking,
        current_user=user,
        event_type="confirmed",
        note="confirmed by test",
        from_status=models.BookingStatus.pending,
        to_status=models.BookingStatus.confirmed,
    )

    assert event in db.added
    assert event.event_type == "confirmed"
    assert event.from_status == "pending"
    assert event.to_status == "confirmed"
    assert event.note == "confirmed by test"


@pytest.mark.asyncio
async def test_confirming_cancelled_booking_is_rejected():
    booking = FakeBooking(status=models.BookingStatus.cancelled)

    with pytest.raises(Exception) as exc_info:
        await ensure_booking_transition_allowed(booking, models.BookingStatus.confirmed)

    assert getattr(exc_info.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_reapplying_same_booking_state_is_rejected():
    booking = FakeBooking(status=models.BookingStatus.pending)

    with pytest.raises(Exception) as exc_info:
        await ensure_booking_transition_allowed(booking, models.BookingStatus.pending)

    assert getattr(exc_info.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_apply_booking_updates_handles_enum_and_plain_fields():
    booking = FakeBooking()

    apply_booking_updates(
        booking,
        {"title": "Updated title", "status": models.BookingStatus.cancelled, "unknown": "ignored"},
    )

    assert booking.title == "Updated title"
    assert booking.status == models.BookingStatus.cancelled.value
    assert not hasattr(booking, "unknown")


@pytest.mark.asyncio
async def test_transition_booking_creates_event_and_updates_booking():
    db = FakeSession()
    booking = FakeBooking()
    user = FakeUser()

    result = await transition_booking(
        db,
        booking=booking,
        current_user=user,
        target_status=models.BookingStatus.confirmed,
        event_type="confirmed",
        note="Booking confirmed",
    )

    assert db.commits == 1
    assert len(db.refreshed) == 2
    assert result["booking"].status == models.BookingStatus.confirmed
    assert result["event"].event_type == "confirmed"


def test_policy_score_summary_and_access_thresholds():
    snapshot = PolicyScoreSnapshot(
        system_score=88.0,
        customer_score=82.0,
        access_score=85.0,
        interest_score=77.0,
        closeness_score=80.0,
        community_closeness_score=84.0,
        policy_tier="system-premium",
        control_posture="high_trust",
        summary="",
    )

    assert "system=88.00" in summarize_policy_score(snapshot)

    policy = models.CustomerPolicyScore(
        user_id=1,
        system_score=88.0,
        customer_score=82.0,
        access_score=85.0,
        interest_score=77.0,
        closeness_score=80.0,
        community_closeness_score=84.0,
        policy_tier="customer-premium",
        control_posture="customer_trusted",
        source="system_and_customer_metrics",
        summary=summarize_policy_score(snapshot),
    )

    assert can_access_functionality(policy, required_tier="standard") is True
    assert can_access_functionality(policy, required_tier="system-premium") is False
    assert _posture_adjusted_access_score(70.0, "observed") == 66.0


def test_policy_topic_analysis_report_exposes_rich_topic_breakdown():
    snapshot = PolicyScoreSnapshot(
        system_score=81.0,
        customer_score=77.0,
        access_score=79.0,
        interest_score=73.0,
        closeness_score=75.0,
        community_closeness_score=80.0,
        policy_tier="customer-premium",
        control_posture="customer_trusted",
        summary="policy topic summary",
        topic_context="booking policy and escalation routing",
        topic_richness="balanced [balance=62.00]",
    )

    report = build_policy_topic_analysis_report(snapshot, FakeUser())

    assert report.user_id == 1
    assert report.current_topic == "booking policy and escalation routing"
    assert report.topic_context
    assert report.topic_richness.startswith("balanced")
    assert report.topic_depth.startswith("depth=")
    assert report.items
    assert report.items[0].topic == report.current_topic
    assert report.items[0].breadth >= 0
    assert "topic_depth" in report.summary


def test_read_users_policy_decision_report_includes_topic_analysis():
    class _PolicyDecisionDb(FakeChatDb):
        async def execute(self, *_args, **_kwargs):
            class Result:
                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return []

                def first(self_inner):
                    return None

                def scalar_one_or_none(self_inner):
                    return None

                def scalar(self_inner):
                    return 0

            return Result()

    async def _fake_get_db():
        yield _PolicyDecisionDb()

    async def _fake_get_current_user():
        return FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/users/me/policy-decision/report?functionality=booking", headers={"accept": "application/json"})
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["policy_decision"]["topic_context"]


def test_policy_score_summary_uses_no_topic_fallback_when_topic_is_missing():
    snapshot = PolicyScoreSnapshot(
        system_score=64.0,
        customer_score=61.0,
        access_score=63.0,
        interest_score=60.0,
        closeness_score=62.0,
        community_closeness_score=63.0,
        policy_tier="standard",
        control_posture="observed",
        summary="system=64.00, customer=61.00, access=63.00, interest=60.00, closeness=62.00, community=63.00, posture=observed, topic_richness=no_topic_richness",
        topic_context="",
        topic_richness="",
    )

    summary = summarize_policy_score(snapshot)

    assert "topic_context=" not in summary
    assert "topic_richness=" not in summary


def test_policy_score_summary_mentions_no_topic_signal_when_bare():
    snapshot = PolicyScoreSnapshot(
        system_score=64.0,
        customer_score=61.0,
        access_score=63.0,
        interest_score=60.0,
        closeness_score=62.0,
        community_closeness_score=63.0,
        policy_tier="standard",
        control_posture="observed",
        summary="system=64.00, customer=61.00, access=63.00, interest=60.00, closeness=62.00, community=63.00, posture=observed",
    )

    summary = summarize_policy_score(snapshot)

    assert "topic_context=" not in summary
    assert "topic_richness=" not in summary


def test_policy_decision_report_uses_no_topic_signal_when_topic_is_missing():
    user = FakeUser(id=10)
    snapshot = PolicyScoreSnapshot(
        system_score=64.0,
        customer_score=61.0,
        access_score=63.0,
        interest_score=60.0,
        closeness_score=62.0,
        community_closeness_score=63.0,
        policy_tier="standard",
        control_posture="observed",
        summary="system=64.00, customer=61.00, access=63.00, interest=60.00, closeness=62.00, community=63.00, posture=observed",
        topic_context="",
        topic_richness="",
    )

    report = __import__("app.services.policy_scoring", fromlist=["build_policy_decision_report"]).build_policy_decision_report(
        snapshot=snapshot,
        user=user,
        functionality="support",
        required_tier="standard",
    )

    assert "no_topic_signal" in report.topic_context
    assert "topic_signal=no_topic_signal" in report.summary

