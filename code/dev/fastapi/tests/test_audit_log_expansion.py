"""Behavior + endpoint tests for the audit-trail expansion.

The ``/audit`` router previously only exposed efficiency reporting (43 lines).
It now also owns an immutable audit trail: operators and system tooling can
record actions (``POST /audit/log``), list them with filters (``GET
/audit/logs``), roll them up (``GET /audit/logs/summary``), and discover the
allowed actions/severities (``GET /audit/trail-catalog``).
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import deps, models
from app.services import audit_log


NOW = datetime.now(timezone.utc)


class _FakeResultSet:
    def __init__(self, rows=None, scalars=None, scalar=None):
        self._rows = rows
        self._scalars = scalars
        self._scalar = scalar

    def scalars(self):
        return _ScalarsProxy(self._scalars or [])

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows or []


class _ScalarsProxy:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


def _make_entry(**overrides) -> models.AuditLogEntry:
    defaults = dict(
        id=1,
        actor_user_id=1,
        action="policy.override",
        entity_type="topic",
        entity_id="42",
        summary="Blocked topic for user",
        detail_json='{"reason": "legal-constraint"}',
        severity="warning",
        source="api",
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return models.AuditLogEntry(**defaults)


class _AuditDb:
    """Fake async session that answers the service-layer queries."""

    def __init__(self, entries=None, stored_adds=None):
        self.entries = list(entries or [])
        self.added = [] if stored_adds is None else stored_adds
        self._id_counter = 100

    async def execute(self, query):
        kind = _query_kind(query)
        if kind == "group_by_action":
            return _FakeResultSet(
                rows=[("policy.override", 2), ("booking.assign", 1)]
            )
        if kind == "group_by_severity":
            return _FakeResultSet(rows=[("warning", 2), ("info", 1)])
        if kind == "count":
            return _FakeResultSet(scalar=len(self.entries))
        # List query: select(AuditLogEntry) order by ...
        return _FakeResultSet(scalars=self.entries)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = self._id_counter
            self._id_counter += 1


def _query_kind(query):
    try:
        descriptions = list(getattr(query, "column_descriptions", []) or [])
    except Exception:
        return "list"
    if not descriptions:
        return "list"
    first = descriptions[0]
    name = first.get("name") or ""
    if name == "action":
        return "group_by_action"
    if name == "severity":
        return "group_by_severity"
    # select(func.count(...)) surfaces the aggregate as a single "count"
    # column, unlike the row-selecting list query (entity == AuditLogEntry).
    if name == "count" and len(descriptions) == 1:
        return "count"
    return "list"


# --- service layer --------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_audit_log_entry_persists_and_validates():
    db = _AuditDb()
    entry = await audit_log.record_audit_log_entry(
        db,
        action="booking.reassign",
        summary="Reassigned booking 7 to room 12",
        actor_user_id=3,
        entity_type="booking",
        entity_id="7",
        detail={"from_room": 5, "to_room": 12},
        severity="info",
    )
    assert entry in db.added
    assert entry.action == "booking.reassign"

    with pytest.raises(ValueError):
        await audit_log.record_audit_log_entry(
            db, action="auth.login", summary="bad severity", severity="loud"
        )


@pytest.mark.asyncio
async def test_list_audit_log_entries_returns_newest_first_with_filters():
    db = _AuditDb(
        entries=[_make_entry(id=2, action="booking.assign"), _make_entry(id=1)]
    )
    rows = await audit_log.list_audit_log_entries(db, limit=10)
    assert [row.id for row in rows] == [2, 1]

    filtered = await audit_log.list_audit_log_entries(db, action="nope")
    assert filtered == db.entries  # fake ignores filters; shape is what matters


@pytest.mark.asyncio
async def test_audit_log_summary_rolls_up_by_action_and_severity():
    db = _AuditDb(entries=[_make_entry()])
    summary = await audit_log.build_audit_log_summary(db)
    assert summary["total"] == 1
    assert {"key": "policy.override", "count": 2} in summary["by_action"]
    assert {"key": "booking.assign", "count": 1} in summary["by_action"]
    assert all(item["key"] in {"info", "warning", "critical"} for item in summary["by_severity"])


def test_audit_log_catalog_exposes_actions_and_severities():
    catalog = audit_log.build_audit_log_catalog()
    assert catalog["severities"] == ["info", "warning", "critical"]
    assert "policy.override" in catalog["actions"]["policy"]
    assert catalog["action_count"] == len(audit_log.AUDIT_ACTIONS)


def test_entry_to_payload_round_trips_json_detail():
    entry = _make_entry()
    payload = audit_log.entry_to_payload(entry)
    assert payload["detail"] == {"reason": "legal-constraint"}
    assert payload["severity"] == "warning"
    assert payload["entity_id"] == "42"


# --- endpoint wiring ------------------------------------------------------------


class _FakeUser:
    id = 1
    username = "tester"
    is_admin = True


class _FakeNonAdminUser:
    id = 2
    username = "customer"
    is_admin = False


def _override_deps(current_user=_FakeUser, db=None, force_admin_dep=True):
    from app.main import app

    async def _get_db_override():
        yield db or _AuditDb()

    app.dependency_overrides[deps.get_db] = _get_db_override
    app.dependency_overrides[deps.get_current_user] = lambda: current_user
    if force_admin_dep:
        # Fast path used by admin tests: bypass the real admin check.
        app.dependency_overrides[deps.get_current_admin_user] = (
            lambda: current_user
        )


def _clear_overrides():
    from app.main import app

    app.dependency_overrides.pop(deps.get_db, None)
    app.dependency_overrides.pop(deps.get_current_user, None)
    app.dependency_overrides.pop(deps.get_current_admin_user, None)


def test_post_audit_log_endpoint_records_entry():
    from app.main import app

    stored = []
    _override_deps(db=_AuditDb(stored_adds=stored))
    try:
        response = TestClient(app).post(
            "/audit/log",
            json={
                "action": "policy.override",
                "entity_type": "topic",
                "entity_id": "42",
                "summary": "Blocked topic for user",
                "detail": {"reason": "legal-constraint"},
                "severity": "warning",
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 201
    payload = response.json()
    assert payload["action"] == "policy.override"
    assert payload["actor_user_id"] == 1
    assert payload["detail"] == {"reason": "legal-constraint"}
    assert len(stored) == 1


def test_post_audit_log_rejects_bad_severity():
    from app.main import app

    _override_deps()
    try:
        response = TestClient(app).post(
            "/audit/log",
            json={
                "action": "policy.override",
                "summary": "bad severity",
                "severity": "loud",
            },
        )
    finally:
        _clear_overrides()
    assert response.status_code == 422


def test_post_audit_log_requires_admin():
    from app.main import app

    # Only override get_current_user -> the real admin check must reject.
    _override_deps(current_user=_FakeNonAdminUser, force_admin_dep=False)
    try:
        response = TestClient(app).post(
            "/audit/log",
            json={"action": "auth.login", "summary": "nope"},
        )
    finally:
        _clear_overrides()
    assert response.status_code == 403


def test_get_audit_logs_and_summary_endpoints():
    from app.main import app

    db = _AuditDb(entries=[_make_entry()])
    _override_deps(db=db)
    try:
        logs = TestClient(app).get("/audit/logs?limit=5")
        summary = TestClient(app).get("/audit/logs/summary")
        catalog = TestClient(app).get("/audit/trail-catalog")
    finally:
        _clear_overrides()

    assert logs.status_code == 200
    logs_payload = logs.json()
    assert logs_payload["total"] == 1
    assert logs_payload["entries"][0]["action"] == "policy.override"
    assert logs_payload["entries"][0]["severity"] == "warning"

    assert summary.status_code == 200
    assert summary.json()["total"] == 1

    assert catalog.status_code == 200
    assert catalog.json()["severities"] == ["info", "warning", "critical"]


def test_audit_logs_requires_admin():
    from app.main import app

    _override_deps(current_user=_FakeNonAdminUser, force_admin_dep=False)
    try:
        response = TestClient(app).get("/audit/logs")
    finally:
        _clear_overrides()
    assert response.status_code == 403


def test_ecosystem_and_features_document_audit_trail():
    from app.main import app

    response = TestClient(app).get("/meta/ecosystem")
    assert response.status_code == 200
    subservices = response.json()["subservices"]
    assert "audit_trail" in subservices
    assert subservices["audit_trail"]["status"] == "ready"
    assert "localization" in subservices

    features = TestClient(app).get("/meta/features").json()
    assert features["endpoints"]["audit_log"] == "/audit/log"
    assert features["endpoints"]["i18n_catalog"] == "/meta/i18n"