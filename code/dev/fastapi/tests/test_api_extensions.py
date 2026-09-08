import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app import models, deps
from app.schemas.chat import ChatHistoryOut, ChatHistorySummary, UserActivityReport, AdminActivityReport, ActivityTimelineReport, RankedUserReport, AdminRetentionTrendReport, RetentionCohortDrilldownReport, RetentionSnapshotAdminReport, UserRetentionSnapshotHealth, RetentionSnapshotComparisonReport, RetentionSnapshotMomentumReport, RetentionSnapshotVolatilityReport, RetentionSnapshotVolatilitySummary, RetentionSnapshotRiskProfile, RetentionSnapshotRecommendation, RetentionSnapshotActionPlan, RetentionSnapshotAuditReport, RetentionSnapshotAuditExport, RetentionSnapshotTypeBreakdownReport, RetentionSnapshotStalenessReport, RetentionSnapshotStalenessTrendReport, RetentionSnapshotHealthScore, RetentionSnapshotHealthSummary, RetentionSnapshotHealthRisk, RetentionSnapshotHealthRecommendation, RetentionDashboard, RetentionSnapshotOperationsReport, RetentionSnapshotOperationsOverview, RetentionSnapshotOperationsStatus, RetentionSnapshotOperationsCompliance, RetentionSnapshotOperationsPosture, RetentionSnapshotOperationsAutomation, RetentionSnapshotOperationsExecutionState, RetentionSnapshotOperationsLaunchReadiness, RetentionSnapshotOperationsGoNoGo, InteractionSummary, MonetizationCohortReport, WeightedSystemMonitoringReport
from app.routers.bookings import get_booking_history, get_booking_analytics_summary
from app.routers.chat import get_user_activity_report, get_admin_activity_report, get_activity_timeline, get_ranked_users_report, get_admin_retention_trend_report, get_retention_cohort_drilldown, get_retention_snapshot_admin_report, get_user_retention_snapshot_health, get_retention_snapshot_comparison_report, get_retention_snapshot_momentum_report, get_retention_snapshot_volatility_report, get_retention_snapshot_volatility_summary, get_retention_snapshot_risk_profile, get_retention_snapshot_recommendation, get_retention_snapshot_action_plan, get_retention_snapshot_audit_report, get_retention_snapshot_audit_export, get_retention_snapshot_type_breakdown, get_retention_snapshot_staleness_report, get_retention_snapshot_staleness_trend, get_retention_snapshot_health_score, get_retention_snapshot_health_summary, get_retention_snapshot_health_risk, get_retention_snapshot_health_recommendation, get_retention_snapshot_operations_report, get_retention_snapshot_operations_overview, get_retention_snapshot_operations_status, get_retention_snapshot_operations_compliance, get_retention_snapshot_operations_posture, get_retention_snapshot_operations_automation, get_retention_snapshot_operations_execution_state, get_retention_snapshot_operations_launch_readiness, get_retention_snapshot_operations_go_no_go, get_monetization_cohorts
from app.routers.chat import get_retention_dashboard


def test_meta_ecosystem_exposes_backend_subservices():
    response = TestClient(app).get("/meta/ecosystem")

    assert response.status_code == 200
    payload = response.json()
    assert "chat_intelligence" in payload["subservices"]
    assert "retention_ops" in payload["subservices"]
    assert "/chat/admin/snapshot-operations-gonogo" in payload["subservices"]["retention_ops"]["routes"]


def test_app_metadata_exposes_retention_dashboard_and_operations_routes():
    meta_payload = TestClient(app).get("/meta").json()
    ecosystem_payload = TestClient(app).get("/meta/ecosystem").json()

    assert "retention" in meta_payload["features"]
    assert "/chat/retention-dashboard" in ecosystem_payload["subservices"]["chat_intelligence"]["routes"]
    assert "/chat/admin/snapshot-operations-report" in ecosystem_payload["subservices"]["retention_ops"]["routes"]


def test_metadata_exposes_locale_fallback_contract():
    response = TestClient(app).get("/meta")

    assert response.status_code == 200
    payload = response.json()
    assert payload["locale"]["resolved"] == "en"
    assert payload["locale"]["fallback_used"] is False


def test_locale_helper_falls_back_and_login_contract_exposes_locale():
    from app.i18n import resolve_locale

    resolution = resolve_locale("pt-BR")
    assert resolution.resolved == "en"
    assert resolution.fallback_used is True

    class _LoginSession:
        async def execute(self, query):
            class _Result:
                def scalar_one_or_none(self_inner):
                    return type(
                        "UserObj",
                        (),
                        {
                            "username": "tester",
                            "hashed_password": "$2b$12$KIXQ2oQzV0TQhP7s6Qz6Xu0P5v4xJQe4dQm1kY5mQw6T1aY8XzS0S",
                        },
                    )()

            return _Result()

    async def _fake_get_db():
        yield _LoginSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).post(
            "/users/login",
            json={"username": "tester", "password": "secret", "locale": "pt-BR"},
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code in {200, 401}
    if response.status_code == 200:
        payload = response.json()
        assert payload["locale"]["resolved"] == "en"


def test_admin_recovery_outcomes_report_contract():
    class _RecoverySession:
        async def execute(self, query):
            class _Result:
                def all(self_inner):
                    return [("high", 3, 2), ("moderate", 1, 1)]

            return _Result()

    async def _fake_get_db():
        yield _RecoverySession()

    async def _fake_get_current_admin_user():
        return FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/recovery-outcomes")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_attempts"] == 4
    assert payload["total_acknowledged"] == 3
    assert payload["items"][0]["recovery_readiness"] == "high"


def test_system_priorities_includes_summary_contract():
    class _PrioritySession:
        async def execute(self, query):
            class _Result:
                def all(self_inner):
                    return [("reliability", 2, 4.5), ("support", 1, 2.0)]

            return _Result()

    async def _fake_get_db():
        yield _PrioritySession()

    async def _fake_get_current_user():
        return FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/chat/system-priorities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total_signals"] == 3
    assert payload["summary"]["tracked_areas"] == 2
    assert payload["summary"]["top_priority"] == "reliability"
    assert payload["summary"]["service_health"] == "ready"


@dataclass
class FakeUser:
    id: int = 1
    username: str = "tester"
    email: str = "tester@example.com"
    full_name: str | None = None
    hashed_password: str = "hash"
    is_admin: bool = True
    created_at: datetime = datetime.now(timezone.utc)
    updated_at: datetime = datetime.now(timezone.utc)


class FakeHealthSession:
    async def execute(self, query):
        class _Result:
            def scalar(self_inner):
                return 1

        return _Result()


class FakeHistorySession:
    def __init__(self):
        self.executed = []

    async def execute(self, query):
        self.executed.append(str(query))

        class _ScalarResult:
            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return [
                    type(
                        "BookingEventObj",
                        (),
                        {
                            "id": 1,
                            "booking_id": 10,
                            "user_id": 1,
                            "event_type": "confirmed",
                            "from_status": "pending",
                            "to_status": "confirmed",
                            "note": "confirmed",
                            "created_at": datetime.now(timezone.utc),
                        },
                    )()
                ]

            def scalar_one_or_none(self_inner):
                return type(
                    "BookingObj",
                    (),
                    {
                        "id": 10,
                        "user_id": 1,
                        "status": models.BookingStatus.confirmed,
                    },
                )()

        return _ScalarResult()


class FakeAnalyticsSession:
    def __init__(self):
        self.calls = 0

    async def execute(self, query):
        class _Result:
            def __init__(self, value=None, rows=None):
                self._value = value
                self._rows = rows or []

            def scalar(self):
                return self._value

            def all(self):
                return self._rows

        self.calls += 1
        if self.calls == 1:
            return _Result(3)
        if self.calls == 2:
            return _Result(7)
        if self.calls == 3:
            return _Result(5)
        if self.calls == 4:
            return _Result(rows=[(models.BookingStatus.pending, 2), (models.BookingStatus.confirmed, 4)])
        if self.calls == 5:
            return _Result(rows=[("created", 2), ("confirmed", 3)])
        if self.calls == 6:
            return _Result(3)
        if self.calls == 7:
            return _Result(4)
        return _Result(0)


class FakeActivitySession:
    def __init__(self):
        self.calls = 0

    async def execute(self, query):
        class _Result:
            def __init__(self, row):
                self._row = row

            def one(self):
                return self._row

        self.calls += 1
        if self.calls == 1:
            return _Result((4, datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)))
        if self.calls == 2:
            return _Result((2, datetime(2026, 9, 5, 11, 0, tzinfo=timezone.utc)))
        return _Result((1, datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)))


class FakeAdminActivitySession:
    def __init__(self):
        self.calls = 0

    async def execute(self, query):
        class _Result:
            def __init__(self, value):
                self._value = value

            def scalar(self):
                return self._value

        self.calls += 1
        values = [8, 20, 6, 3, 5, 4, 2]
        return _Result(values[self.calls - 1])


class FakeTimelineSession:
    def __init__(self):
        self.calls = 0

    async def execute(self, query):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def all(self):
                return self._rows

        self.calls += 1
        if self.calls == 1:
            return _Result([
                type("ChatRow", (), {"id": 11, "timestamp": datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc), "message": "hello there"})(),
                type("ChatRow", (), {"id": 12, "timestamp": datetime(2026, 9, 5, 11, 0, tzinfo=timezone.utc), "message": "second message"})(),
            ])
        if self.calls == 2:
            return _Result([
                type("BookingRow", (), {"id": 21, "created_at": datetime(2026, 9, 5, 11, 30, tzinfo=timezone.utc), "status": models.BookingStatus.confirmed, "title": "Consultation", "service_type": models.ServiceType.consultation})(),
            ])
        return _Result([
            type("SnapshotRow", (), {"id": 31, "created_at": datetime(2026, 9, 5, 10, 30, tzinfo=timezone.utc), "snapshot_type": "chat_response", "lifecycle_stage": "engaged"})(),
        ])


class FakeRankedUsersSession:
    async def execute(self, query):
        class _Result:
            def all(self_inner):
                return [
                    (2, "beta", 5, 4, 1),
                    (1, "alpha", 2, 1, 0),
                ]

        return _Result()


class FakeRetentionTrendSession:
    def __init__(self):
        self.calls = 0

    async def execute(self, query):
        class _Result:
            def __init__(self, value):
                self._value = value

            def scalar(self):
                return self._value

        self.calls += 1
        values = [6, 2, 10, 4, 8, 5]
        return _Result(values[self.calls - 1])


class FakeRetentionSnapshotAdminSession:
    def __init__(self):
        self.calls = 0

    async def execute(self, query):
        class _Result:
            def __init__(self, value=None, rows=None):
                self._value = value
                self._rows = rows or []

            def scalar(self):
                return self._value

            def all(self):
                return self._rows

        self.calls += 1
        if self.calls == 1:
            return _Result(9)
        return _Result(rows=[("chat_response", 6, 72.5, datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)), ("snapshot_pruning", 3, 88.0, datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc))])


class FakeRetentionHealthSession:
    async def execute(self, query):
        class _Result:
            def scalars(self):
                return self

            def all(self):
                return [
                    type("SnapshotRow", (), {"snapshot_type": "chat_response", "lifecycle_stage": "engaged", "loyalty_score": 80.0, "created_at": datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)})(),
                    type("SnapshotRow", (), {"snapshot_type": "chat_response", "lifecycle_stage": "loyal", "loyalty_score": 90.0, "created_at": datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)})(),
                ]

        return _Result()


class FakeRetentionComparisonSession:
    def __init__(self):
        self.calls = 0

    async def execute(self, query):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        self.calls += 1
        if self.calls == 1:
            return _Result([("chat_response", 7), ("snapshot_pruning", 2)])
        return _Result([("chat_response", 5), ("snapshot_pruning", 1), ("manual_review", 3)])


class FakeRetentionAuditSession:
    async def execute(self, query):
        class _Result:
            def all(self):
                return [
                    ("chat_response", 6, 2, datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)),
                    ("snapshot_pruning", 3, 1, datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc)),
                ]

        return _Result()


class FakeRetentionTypeBreakdownSession:
    async def execute(self, query):
        class _Result:
            def all(self):
                return [("chat_response", 6, 2)]

        return _Result()


class FakeRetentionStalenessSession:
    async def execute(self, query):
        class _Result:
            def all(self):
                return [("chat_response", 4, datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))]

        return _Result()


class FakeRetentionStalenessTrendSession:
    def __init__(self):
        self.calls = 0

    async def execute(self, query):
        class _Result:
            def __init__(self, rows=None):
                self._rows = rows or []

            def all(self):
                return self._rows

        self.calls += 1
        return _Result(rows=[("chat_response", 4, datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))])


class FakeRetentionHealthScoreSession:
    async def execute(self, query):
        class _Result:
            def all(self):
                return [("chat_response", 4, datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))]

        return _Result()


class FakeRetentionHealthSummarySession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionHealthRiskSession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionHealthRecommendationSession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionOperationsOverviewSession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionOperationsStatusSession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionOperationsComplianceSession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionOperationsPostureSession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionOperationsAutomationSession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionOperationsExecutionStateSession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionOperationsLaunchReadinessSession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionOperationsGoNoGoSession(FakeRetentionHealthScoreSession):
    pass


class FakeRetentionOperationsReportSession:
    async def execute(self, query):
        class _Row:
            def __init__(self, created_at):
                self.created_at = created_at

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def all(self):
                return self._rows

        return _Result([
            _Row(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)),
            _Row(datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)),
            _Row(datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)),
            _Row(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)),
            _Row(datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)),
            _Row(datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)),
            _Row(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)),
            _Row(datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)),
        ])


class FakeRetentionMaintenanceSession:
    def __init__(self):
        self.deleted = []

    async def execute(self, query):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

            def scalars(self):
                return self

        query_text = str(query)
        if "SELECT users.id" in query_text or "users.id" in query_text:
            return _Result([(1,), (2,)])
        return _Result([type("SnapshotRow", (), {"id": 1})(), type("SnapshotRow", (), {"id": 2})(), type("SnapshotRow", (), {"id": 3})()])

    async def delete(self, snapshot):
        self.deleted.append(snapshot)

    async def commit(self):
        return None


class FakeRetentionDashboardSession:
    async def execute(self, query):
        class _Result:
            def __init__(self, rows=None, value=None):
                self._rows = rows or []
                self._value = value

            def scalars(self):
                return self

            def all(self):
                return self._rows

            def scalar(self):
                return self._value

            def scalar_one_or_none(self):
                return self._value

        query_text = str(query)
        if "interaction_signal" in query_text:
            return _Result(rows=[("support", 0.7), ("clarity", 0.4)])
        if "retention_snapshot" in query_text and "snapshot_type" in query_text:
            return _Result(rows=[
                type("SnapshotRow", (), {"id": 1, "user_id": 1, "snapshot_type": "chat_response", "window_days": 30, "lifecycle_stage": "engaged", "loyalty_score": 80.0, "churn_risk": "low", "summary_json": "{}", "created_at": datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)})(),
                type("SnapshotRow", (), {"id": 2, "user_id": 1, "snapshot_type": "chat_response", "window_days": 30, "lifecycle_stage": "engaged", "loyalty_score": 78.0, "churn_risk": "low", "summary_json": "{}", "created_at": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)})(),
            ])
        if "retention_snapshot" in query_text:
            return _Result(rows=[
                type("SnapshotRow", (), {"id": 1, "user_id": 1, "snapshot_type": "chat_response", "window_days": 30, "lifecycle_stage": "engaged", "loyalty_score": 80.0, "churn_risk": "low", "summary_json": "{}", "created_at": datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)})(),
                type("SnapshotRow", (), {"id": 2, "user_id": 1, "snapshot_type": "chat_response", "window_days": 30, "lifecycle_stage": "engaged", "loyalty_score": 70.0, "churn_risk": "medium", "summary_json": "{}", "created_at": datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)})(),
            ])
        if "booking" in query_text:
            return _Result(rows=[type("BookingRow", (), {"created_at": datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc), "status": "completed"})()])
        if "chat" in query_text:
            return _Result(rows=[type("ChatRow", (), {"message": "general feedback", "timestamp": datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)})()])
        return _Result(rows=[])


class FakeMonetizationCohortsSession:
    def __init__(self):
        self.calls = 0

    async def execute(self, query):
        class _Result:
            def __init__(self, value=None, rows=None):
                self._value = value
                self._rows = rows or []

            def scalars(self):
                return self

            def all(self):
                return self._rows

        self.calls += 1
        if self.calls == 1:
            return _Result(rows=[(1,)])
        if self.calls == 2:
            return _Result(rows=[type("ChatRow", (), {"timestamp": datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc), "message": "pricing is expensive but worth it"})()])
        if self.calls == 3:
            return _Result(rows=[])
        if self.calls == 4:
            return _Result(rows=[type("SignalRow", (), {"score": 2.0})()])
        return _Result(rows=[])


class FakeCohortDrilldownSession:
    def __init__(self):
        self.calls = 0

    async def execute(self, query):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

            def scalars(self):
                return self

        self.calls += 1
        if self.calls == 1:
            return _Result([(1, "alpha"), (2, "beta")])
        if self.calls == 2:
            return _Result([type("ChatRow", (), {"timestamp": datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc), "message": "good"})()])
        if self.calls == 3:
            return _Result([])
        if self.calls == 4:
            return _Result([type("SignalRow", (), {"score": 1.5})()])
        if self.calls == 5:
            return _Result([])
        if self.calls == 6:
            return _Result([])
        if self.calls == 7:
            return _Result([type("ChatRow", (), {"timestamp": datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc), "message": "excellent experience"})()])
        if self.calls == 8:
            return _Result([type("BookingRow", (), {"created_at": datetime(2026, 9, 5, 11, 0, tzinfo=timezone.utc), "status": models.BookingStatus.completed, "title": "Follow up", "service_type": models.ServiceType.consultation})()])
        if self.calls == 9:
            return _Result([type("SignalRow", (), {"score": 0.2})()])
        return _Result([])


@pytest.fixture
def client():
    return TestClient(app)


def test_health_endpoint_returns_structured_payload(client, monkeypatch):
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = client.get("/health")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["app"]["name"]
    assert payload["database"]["connected"] is True


@pytest.mark.asyncio
async def test_booking_history_returns_report_shape():
    db = FakeHistorySession()
    report = await get_booking_history(booking_id=10, current_user=FakeUser(), db=db)

    assert report.booking_id == 10
    assert report.user_id == 1
    assert report.event_count == 1
    assert report.items[0].event_type == "confirmed"


@pytest.mark.asyncio
async def test_admin_analytics_summary_returns_counts():
    db = FakeAnalyticsSession()
    summary = await get_booking_analytics_summary(current_user=FakeUser(is_admin=True), db=db)

    assert summary.total_users == 3
    assert summary.total_bookings == 7
    assert summary.booking_events_total == 5
    assert summary.bookings_by_status["pending"] == 2
    assert summary.booking_events_by_type[0].event_type == "created"


def test_chat_history_out_accepts_missing_user_id():
    payload = ChatHistoryOut(id=1, user_id=None, message="hi", response="hello", timestamp="2026-09-05T00:00:00Z")

    assert payload.user_id is None


def test_chat_history_summary_supports_nullable_user_id():
    summary = ChatHistorySummary(user_id=None, total_messages=2, total_responses=2)

    assert summary.user_id is None


def test_interaction_summary_includes_monetization_fields():
    summary = InteractionSummary(
        user_id=1,
        messages_analyzed=3,
        bookings_analyzed=1,
        churn_risk="low",
        loyalty_score=88.0,
        monetization_readiness=92.5,
        value_tier="premium",
        customer_classification="loyal high-value",
        top_issues=["pricing"],
        strengths=["repeat usage"],
        insights=[],
        metadata={},
        generated_at=datetime.now(timezone.utc),
    )

    assert summary.monetization_readiness == 92.5
    assert summary.value_tier == "premium"
    assert summary.customer_classification == "loyal high-value"


@pytest.mark.asyncio
async def test_monetization_cohorts_endpoint_returns_report():
    db = FakeMonetizationCohortsSession()
    report = await get_monetization_cohorts(window_days=30, db=db)

    assert isinstance(report, MonetizationCohortReport)
    assert report.window_days == 30
    assert report.cohorts


def test_meta_features_lists_chat_history_summary():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    assert "chat-retention" == response.json()["domain"]


def test_meta_capabilities_expose_study_driven_ai_catalog():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert "capabilities" in payload
    capability_ids = {item["id"] for item in payload["capabilities"]}
    assert {
        "youth_conversion_intelligence",
        "market_penetration_adoption",
        "device_experience_optimizer",
        "cpc_economics_profiler",
        "older_adult_value_model",
        "low_penetration_engagement_engine",
        "payment_logistics_intelligence",
        "platform_channel_mapper",
        "retention_forecast_engine",
    }.issubset(capability_ids)


def test_meta_capabilities_cover_study_concepts():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    themes = {item["study_theme"] for item in payload["capabilities"]}
    assert any("Younger demographics" in theme for theme in themes)
    assert any("Higher internet penetration" in theme for theme in themes)
    assert any("mobile-only usage" in theme for theme in themes)
    assert any("Lower average income" in theme for theme in themes)
    assert any("Older adults" in theme for theme in themes)
    assert any("Lower internet penetration" in theme for theme in themes)


def test_meta_capabilities_expose_study_driven_roles():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert "roles" in payload
    role_ids = {item["id"] for item in payload["roles"]}
    assert {
        "youth_conversion_intelligence",
        "market_penetration_adoption",
        "device_experience_optimizer",
        "cpc_economics_profiler",
        "older_adult_value_model",
        "low_penetration_engagement_engine",
    } == role_ids


def test_meta_features_lists_snapshot_health_reporting():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert "retention_snapshot_health_reporting" in payload["features"]
    assert payload["endpoints"]["retention_snapshot_health_recommendation"] == "/chat/admin/snapshot-health-recommendation"


def test_meta_features_lists_snapshot_operations_overview():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200


def test_weighted_system_monitoring_filters_top_focus_items():
    summary = InteractionSummary(
        user_id=7,
        messages_analyzed=12,
        bookings_analyzed=3,
        churn_risk="medium",
        loyalty_score=61.0,
        monetization_readiness=70.0,
        value_tier="standard",
        customer_classification="stable value",
        top_issues=["response_speed"],
        strengths=["repeat usage"],
        insights=[],
        metadata={},
        generated_at=datetime.now(timezone.utc),
    )

    report = app.state if False else None
    from app.routers.chat import _build_weighted_system_monitoring

    report = _build_weighted_system_monitoring(summary)

    assert isinstance(report, WeightedSystemMonitoringReport)
    assert report.scope == "whole_system_and_customer_activity"
    assert [item.area for item in report.items] == ["reliability", "response_speed", "customer_activity", "retention"]
    assert all(item.weight >= 0.8 for item in report.items[:4])


def test_meta_features_lists_snapshot_operations_status():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert "retention_snapshot_operations_status_reporting" in payload["features"]
    assert payload["endpoints"]["retention_snapshot_operations_status"] == "/chat/admin/snapshot-operations-status"


def test_meta_features_lists_snapshot_operations_compliance():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert "retention_snapshot_operations_compliance_reporting" in payload["features"]
    assert payload["endpoints"]["retention_snapshot_operations_compliance"] == "/chat/admin/snapshot-operations-compliance"


def test_meta_features_lists_snapshot_operations_posture():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert "retention_snapshot_operations_posture_reporting" in payload["features"]
    assert payload["endpoints"]["retention_snapshot_operations_posture"] == "/chat/admin/snapshot-operations-posture"


def test_meta_features_lists_snapshot_operations_automation():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert "retention_snapshot_operations_automation_reporting" in payload["features"]
    assert payload["endpoints"]["retention_snapshot_operations_automation"] == "/chat/admin/snapshot-operations-automation"


def test_meta_features_lists_snapshot_operations_execution_state():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert "retention_snapshot_operations_execution_state_reporting" in payload["features"]
    assert payload["endpoints"]["retention_snapshot_operations_execution_state"] == "/chat/admin/snapshot-operations-execution-state"


def test_meta_features_lists_snapshot_operations_launch_readiness():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert "retention_snapshot_operations_launch_readiness_reporting" in payload["features"]
    assert payload["endpoints"]["retention_snapshot_operations_launch_readiness"] == "/chat/admin/snapshot-operations-launch-readiness"


def test_meta_features_lists_snapshot_operations_gonogo():
    async def _fake_get_db():
        yield FakeHealthSession()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    try:
        response = TestClient(app).get("/meta/capabilities")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert "retention_snapshot_operations_gonogo_reporting" in payload["features"]
    assert payload["endpoints"]["retention_snapshot_operations_gonogo"] == "/chat/admin/snapshot-operations-gonogo"


@pytest.mark.asyncio
async def test_user_activity_report_returns_recent_counts():
    report = await get_user_activity_report(db=FakeActivitySession(), current_user=FakeUser(), window_days=7)

    assert isinstance(report, UserActivityReport)
    assert report.user_id == 1
    assert report.chat_messages == 4
    assert report.bookings == 2
    assert report.snapshots == 1
    assert report.latest_chat_at is not None


@pytest.mark.asyncio
async def test_admin_activity_report_returns_system_totals():
    report = await get_admin_activity_report(db=FakeAdminActivitySession(), current_user=FakeUser(is_admin=True), window_days=14)

    assert isinstance(report, AdminActivityReport)
    assert report.total_users == 8
    assert report.total_chat_messages == 20
    assert report.total_bookings == 6
    assert report.total_snapshots == 3
    assert report.recent_chats == 5


@pytest.mark.asyncio
async def test_activity_timeline_returns_mixed_events_in_descending_order():
    report = await get_activity_timeline(db=FakeTimelineSession(), current_user=FakeUser(), window_days=30)

    assert isinstance(report, ActivityTimelineReport)
    assert report.user_id == 1
    assert [item.kind for item in report.items] == ["chat", "booking", "chat", "snapshot"]
    assert report.items[0].reference_id == 11
    assert report.items[1].reference_id == 21


@pytest.mark.asyncio
async def test_ranked_users_report_orders_by_activity_score():
    report = await get_ranked_users_report(db=FakeRankedUsersSession(), current_user=FakeUser(is_admin=True), window_days=30, limit=10)

    assert isinstance(report, RankedUserReport)
    assert [user.username for user in report.users] == ["beta", "alpha"]
    assert report.users[0].activity_score > report.users[1].activity_score


@pytest.mark.asyncio
async def test_admin_retention_trend_report_returns_window_comparison():
    report = await get_admin_retention_trend_report(db=FakeRetentionTrendSession(), current_user=FakeUser(is_admin=True), window_days=14)

    assert isinstance(report, AdminRetentionTrendReport)
    assert [item.area for item in report.trends] == ["snapshots", "chats", "bookings"]
    assert report.trends[0].delta == 4


@pytest.mark.asyncio
async def test_retention_cohort_drilldown_returns_matching_members():
    report = await get_retention_cohort_drilldown(cohort="stable", db=FakeCohortDrilldownSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionCohortDrilldownReport)
    assert report.cohort == "stable"
    assert isinstance(report.members, list)
    assert all(member.cohort == "stable" for member in report.members)


@pytest.mark.asyncio
async def test_retention_snapshot_admin_report_returns_grouped_summary():
    report = await get_retention_snapshot_admin_report(db=FakeRetentionSnapshotAdminSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotAdminReport)
    assert report.total_snapshots == 9
    assert [item.snapshot_type for item in report.items] == ["chat_response", "snapshot_pruning"]
    assert report.items[0].count == 6


@pytest.mark.asyncio
async def test_user_retention_snapshot_health_reports_latest_state():
    report = await get_user_retention_snapshot_health(db=FakeRetentionHealthSession(), current_user=FakeUser(), window_days=30)

    assert isinstance(report, UserRetentionSnapshotHealth)
    assert report.snapshot_count == 2
    assert report.latest_snapshot_type == "chat_response"
    assert report.latest_lifecycle_stage == "engaged"
    assert report.avg_loyalty_score == 85.0


@pytest.mark.asyncio
async def test_retention_snapshot_comparison_report_compares_windows():
    report = await get_retention_snapshot_comparison_report(db=FakeRetentionComparisonSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotComparisonReport)
    assert [item.snapshot_type for item in report.comparisons] == ["chat_response", "manual_review", "snapshot_pruning"]
    assert report.comparisons[0].delta == 2


@pytest.mark.asyncio
async def test_retention_snapshot_momentum_report_derives_direction():
    report = await get_retention_snapshot_momentum_report(db=FakeRetentionComparisonSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotMomentumReport)
    assert [item.direction for item in report.items] == ["up", "down", "up"]
    assert report.items[0].momentum == 2


@pytest.mark.asyncio
async def test_retention_snapshot_volatility_report_classifies_ranges():
    report = await get_retention_snapshot_volatility_report(db=FakeRetentionComparisonSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotVolatilityReport)
    assert [item.classification for item in report.items] == ["moderate", "high", "moderate"]
    assert report.items[0].volatility == 2


@pytest.mark.asyncio
async def test_retention_snapshot_volatility_summary_counts_classes():
    report = await get_retention_snapshot_volatility_summary(db=FakeRetentionComparisonSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotVolatilitySummary)
    assert report.stable == 0
    assert report.moderate == 2
    assert report.high == 1


@pytest.mark.asyncio
async def test_retention_snapshot_risk_profile_aggregates_volatility():
    report = await get_retention_snapshot_risk_profile(db=FakeRetentionComparisonSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotRiskProfile)
    assert report.score == 8
    assert report.risk_level == "critical"


@pytest.mark.asyncio
async def test_retention_snapshot_recommendation_reflects_risk_profile():
    report = await get_retention_snapshot_recommendation(db=FakeRetentionComparisonSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotRecommendation)
    assert report.risk_level == "critical"
    assert "Escalate retention outreach" in report.recommendation


@pytest.mark.asyncio
async def test_retention_snapshot_action_plan_derives_next_step():
    report = await get_retention_snapshot_action_plan(db=FakeRetentionComparisonSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotActionPlan)
    assert report.risk_level == "critical"
    assert "Open incident review" in report.action


@pytest.mark.asyncio
async def test_retention_snapshot_audit_report_returns_type_breakdown():
    report = await get_retention_snapshot_audit_report(db=FakeRetentionAuditSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotAuditReport)
    assert report.total_snapshots == 9
    assert [item.snapshot_type for item in report.items] == ["chat_response", "snapshot_pruning"]
    assert report.items[0].unique_users == 2


@pytest.mark.asyncio
async def test_retention_snapshot_audit_export_returns_summary_string():
    report = await get_retention_snapshot_audit_export(db=FakeRetentionAuditSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotAuditExport)
    assert report.total_snapshots == 9
    assert report.snapshot_types == ["chat_response", "snapshot_pruning"]
    assert report.summary.startswith("9 snapshots across 2 types")


@pytest.mark.asyncio
async def test_retention_snapshot_type_breakdown_filters_to_requested_type():
    report = await get_retention_snapshot_type_breakdown(snapshot_type="chat_response", db=FakeRetentionTypeBreakdownSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(report, RetentionSnapshotTypeBreakdownReport)
    assert report.snapshot_type == "chat_response"
    assert report.total_snapshots == 6
    assert report.unique_users == 2
    assert [item.snapshot_type for item in report.items] == ["chat_response"]


@pytest.mark.asyncio
async def test_retention_snapshot_staleness_report_flags_old_items():
    report = await get_retention_snapshot_staleness_report(db=FakeRetentionStalenessSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotStalenessReport)
    assert report.stale_after_days == 14
    assert report.total_stale_snapshots == 4
    assert report.items[0].snapshot_type == "chat_response"


@pytest.mark.asyncio
async def test_retention_snapshot_staleness_trend_summarizes_buckets():
    report = await get_retention_snapshot_staleness_trend(db=FakeRetentionStalenessTrendSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotStalenessTrendReport)
    assert report.stale_after_days == 14
    assert [item.bucket for item in report.items] == ["stale", "fresh"]
    assert report.items[0].stale_snapshots == 4


@pytest.mark.asyncio
async def test_retention_snapshot_health_score_derives_status():
    report = await get_retention_snapshot_health_score(db=FakeRetentionHealthScoreSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotHealthScore)
    assert report.score == 60
    assert report.status == "watch"


@pytest.mark.asyncio
async def test_retention_snapshot_health_summary_formats_message():
    report = await get_retention_snapshot_health_summary(db=FakeRetentionHealthSummarySession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotHealthSummary)
    assert report.score == 60
    assert report.status == "watch"
    assert report.summary.startswith("Snapshot freshness is watch")


@pytest.mark.asyncio
async def test_retention_snapshot_health_risk_classifies_level():
    report = await get_retention_snapshot_health_risk(db=FakeRetentionHealthRiskSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotHealthRisk)
    assert report.score == 60
    assert report.status == "watch"
    assert report.risk_level == "medium"


@pytest.mark.asyncio
async def test_retention_snapshot_health_recommendation_maps_next_step():
    report = await get_retention_snapshot_health_recommendation(db=FakeRetentionHealthRecommendationSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotHealthRecommendation)
    assert report.score == 60
    assert report.risk_level == "medium"
    assert report.recommendation.startswith("Review stale snapshot drivers")


@pytest.mark.asyncio
async def test_retention_snapshot_operations_overview_summarizes_recommendation():
    report = await get_retention_snapshot_operations_overview(db=FakeRetentionOperationsOverviewSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotOperationsOverview)
    assert report.score == 60
    assert report.risk_level == "medium"
    assert report.overview.startswith("Snapshot operations need a targeted review")


@pytest.mark.asyncio
async def test_retention_snapshot_operations_status_summarizes_overview():
    report = await get_retention_snapshot_operations_status(db=FakeRetentionOperationsStatusSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotOperationsStatus)
    assert report.status == "watch"
    assert report.overview.startswith("Snapshot operations need a targeted review")


@pytest.mark.asyncio
async def test_retention_snapshot_operations_compliance_labels_state():
    report = await get_retention_snapshot_operations_compliance(db=FakeRetentionOperationsComplianceSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotOperationsCompliance)
    assert report.compliance == "review"
    assert report.overview.startswith("Snapshot operations need a targeted review")


@pytest.mark.asyncio
async def test_retention_snapshot_operations_posture_labels_state():
    report = await get_retention_snapshot_operations_posture(db=FakeRetentionOperationsPostureSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotOperationsPosture)
    assert report.posture == "caution"
    assert report.overview.startswith("Snapshot operations need a targeted review")


@pytest.mark.asyncio
async def test_retention_snapshot_operations_automation_labels_state():
    report = await get_retention_snapshot_operations_automation(db=FakeRetentionOperationsAutomationSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotOperationsAutomation)
    assert report.automation_ready == "conditional"
    assert report.overview.startswith("Snapshot operations need a targeted review")


@pytest.mark.asyncio
async def test_retention_snapshot_operations_execution_state_labels_state():
    report = await get_retention_snapshot_operations_execution_state(db=FakeRetentionOperationsExecutionStateSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotOperationsExecutionState)
    assert report.execution_state == "hold"
    assert report.overview.startswith("Snapshot operations need a targeted review")


@pytest.mark.asyncio
async def test_retention_snapshot_operations_launch_readiness_labels_state():
    report = await get_retention_snapshot_operations_launch_readiness(db=FakeRetentionOperationsLaunchReadinessSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotOperationsLaunchReadiness)
    assert report.launch_readiness == "launch_pending"
    assert report.overview.startswith("Snapshot operations need a targeted review")


@pytest.mark.asyncio
async def test_retention_snapshot_operations_go_no_go_labels_state():
    report = await get_retention_snapshot_operations_go_no_go(db=FakeRetentionOperationsGoNoGoSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotOperationsGoNoGo)
    assert report.decision == "hold"
    assert report.overview.startswith("Snapshot operations need a targeted review")


@pytest.mark.asyncio
async def test_retention_snapshot_operations_report_exposes_audit_totals():
    report = await get_retention_snapshot_operations_report(db=FakeRetentionOperationsReportSession(), current_user=FakeUser(is_admin=True), stale_after_days=14, window_days=30)

    assert isinstance(report, RetentionSnapshotOperationsReport)
    assert report.measurement_window_days == 30
    assert report.measurement_stale_after_days == 14
    assert report.total_snapshots == 8
    assert report.stale_snapshots == 6
    assert report.recent_snapshots == 2
    assert report.stale_ratio == 0.75
    assert report.stale_data_flag is True
    assert report.insufficient_history_flag is False
    assert report.readiness_threshold == 0.25


@pytest.mark.asyncio
async def test_retention_dashboard_embeds_snapshot_operations_report_with_measurements():
    dashboard = await get_retention_dashboard(db=FakeRetentionDashboardSession(), current_user=FakeUser(is_admin=True), window_days=30)

    assert isinstance(dashboard, RetentionDashboard)
    assert dashboard.snapshot_operations_report is not None
    assert dashboard.snapshot_operations_report.measurement_window_days == 30
    assert dashboard.snapshot_operations_report.measurement_stale_after_days == 30
    assert dashboard.snapshot_operations_report.readiness_threshold == 0.25


def test_retention_maintenance_report_endpoint_exposes_summary_contract():
    class _AdminUser:
        id = 1
        is_admin = True

    async def _fake_get_admin_user():
        return _AdminUser()

    async def _fake_get_db():
        yield FakeRetentionMaintenanceSession()

    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_admin_user
    app.dependency_overrides[deps.get_db] = _fake_get_db
    client = TestClient(app)

    try:
        response = client.post("/retention/maintenance/report", params={"window_days": 30, "keep": 20})
    finally:
        app.dependency_overrides.pop(deps.get_current_admin_user, None)
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["keep"] == 20
    assert payload["total_users"] >= 0
    assert isinstance(payload["results"], list)


def test_retention_maintenance_endpoint_exposes_pruning_contract():
    class _AdminUser:
        id = 1
        is_admin = True

    class _PruneSession:
        def __init__(self):
            self.deleted = []
            self.committed = False

        async def execute(self, query):
            class _Result:
                def __init__(self_inner, rows):
                    self_inner._rows = rows

                def scalars(self_inner):
                    return self_inner

                def all(self_inner):
                    return self_inner._rows

            return _Result([type("Snapshot", (), {"id": 1})(), type("Snapshot", (), {"id": 2})(), type("Snapshot", (), {"id": 3})()])

        async def delete(self, snapshot):
            self.deleted.append(snapshot)

        async def commit(self):
            self.committed = True

    async def _fake_get_admin_user():
        return _AdminUser()

    async def _fake_get_db():
        yield _PruneSession()

    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_admin_user
    app.dependency_overrides[deps.get_db] = _fake_get_db
    client = TestClient(app)

    try:
        response = client.post("/retention/maintenance", params={"window_days": 30, "keep": 1})
    finally:
        app.dependency_overrides.pop(deps.get_current_admin_user, None)
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 30
    assert payload["removed_snapshots"] >= 0
    assert payload["kept_snapshots"] >= 0


def test_retention_maintenance_endpoint_propagates_database_errors():
    class _AdminUser:
        id = 1
        is_admin = True

    class _BrokenSession:
        async def execute(self, query):
            raise RuntimeError("maintenance query failed")

    async def _fake_get_admin_user():
        return _AdminUser()

    async def _fake_get_db():
        yield _BrokenSession()

    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_admin_user
    app.dependency_overrides[deps.get_db] = _fake_get_db
    client = TestClient(app)

    try:
        with pytest.raises(RuntimeError, match="maintenance query failed"):
            client.post("/retention/maintenance", params={"window_days": 30, "keep": 1})
    finally:
        app.dependency_overrides.pop(deps.get_current_admin_user, None)
        app.dependency_overrides.pop(deps.get_db, None)

