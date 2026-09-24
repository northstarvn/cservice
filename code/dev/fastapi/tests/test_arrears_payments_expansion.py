"""Tests for the pay-in-arrears with policy-selected interest expansion.

The backend now lets users defer a payment (pay in arrears); the interest
terms are chosen by a config-driven policy engine (`ARREARS_INTEREST_POLICIES`
in `app/services/arrears_payments.py`) so different customers / states /
service types get different policies. Interest accrues only after the grace
period, is computed on demand (simple or daily-compounding) and is capped as a
percentage of principal.

Surface: `GET/POST /chat/payments/arrears`, `GET /chat/payments/arrears/quote`,
`GET /chat/admin/payments/arrears`,
`POST /chat/admin/payments/arrears/{entry_id}/settle` and
`POST /chat/admin/payments/arrears/{entry_id}/waive-interest`, plus the
`arrears_payments` catalog in `/meta/scoring-catalog`.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import deps, models
from app.main import app
from app.services import arrears_payments
from app.services.arrears_payments import (
    ARREARS_INTEREST_POLICIES,
    compute_arrears_interest,
    quote_arrears_payment,
    select_arrears_policy,
)

NOW = datetime.now(timezone.utc)


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
        "service_type": "consultation",
        "principal": 300.0,
        "currency": "USD",
    }
    base.update(overrides)
    return base


# --- pure interest math -----------------------------------------------------


def test_interest_zero_within_grace_period() -> None:
    result = compute_arrears_interest(1000.0, 12.0, 10, grace_days=14)
    assert result["interest"] == 0.0
    assert result["total_due"] == 1000.0
    assert result["days_after_grace"] == 0


def test_interest_simple_after_grace() -> None:
    result = compute_arrears_interest(1000.0, 12.0, 30, grace_days=14)
    # 16 days after grace, 12% annual simple
    assert result["days_after_grace"] == 16
    assert result["interest"] == pytest.approx(round(1000.0 * 0.12 * 16 / 365, 2), abs=0.01)
    assert result["total_due"] == pytest.approx(1000.0 + result["interest"], abs=0.01)


def test_interest_daily_compounding_exceeds_simple() -> None:
    simple = compute_arrears_interest(1000.0, 24.0, 60, grace_days=7, compounding="simple")
    daily = compute_arrears_interest(1000.0, 24.0, 60, grace_days=7, compounding="daily")
    assert daily["interest"] > simple["interest"]
    assert daily["compounding"] == "daily"


def test_interest_capped_at_cap_pct() -> None:
    # 1 year at 40% simple would be 40% of principal; cap at 25%.
    result = compute_arrears_interest(1000.0, 40.0, 365, grace_days=0, cap_pct=25.0)
    assert result["interest"] == pytest.approx(250.0, abs=0.01)
    assert result["total_due"] == pytest.approx(1250.0, abs=0.01)


def test_interest_zero_for_zero_rate_or_principal() -> None:
    assert compute_arrears_interest(0.0, 12.0, 30)["interest"] == 0.0
    assert compute_arrears_interest(500.0, 0.0, 30)["interest"] == 0.0


# --- policy selection -------------------------------------------------------


def test_policy_catalog_valid() -> None:
    ids = [rule["policy_id"] for rule in ARREARS_INTEREST_POLICIES]
    assert len(ids) == len(set(ids))
    for rule in ARREARS_INTEREST_POLICIES:
        assert rule["priority"] in {"high", "medium", "low"}
        params = rule["params"]
        for field in (
            "annual_rate",
            "grace_days",
            "compounding",
            "interest_cap_pct",
            "min_principal",
            "max_principal",
            "max_defer_days",
        ):
            assert field in params
        assert params["compounding"] in {"simple", "daily"}


def test_policy_selection_new_customer() -> None:
    policy, fields = select_arrears_policy(_context(stage="new"))
    assert policy["policy_id"] == "new_customer_growth_deferral"
    assert fields["stage"] == "new"


def test_policy_premium_wins_over_high_risk() -> None:
    policy, _ = select_arrears_policy(_context(value_tier="premium", churn_risk="high", risk_level="high"))
    assert policy["policy_id"] == "vip_premium_deferral"


def test_policy_high_risk_secured_for_at_risk() -> None:
    policy, _ = select_arrears_policy(_context(value_tier="standard", churn_risk="high", risk_level="critical"))
    assert policy["policy_id"] == "high_risk_secured_deferral"
    assert policy["params"]["annual_rate"] == 24.0


def test_policy_large_project_installment() -> None:
    policy, _ = select_arrears_policy(_context(service_type="project", principal=1000.0))
    assert policy["policy_id"] == "large_project_installment"


def test_policy_standard_vs_universal_fallback() -> None:
    engaged, _ = select_arrears_policy(_context())
    assert engaged["policy_id"] == "standard_deferral"
    fallback, _ = select_arrears_policy(_context(stage="lapsed", journey_family="churn"))
    assert fallback["policy_id"] == "universal_deferral"


def test_quote_eligibility_outer_principal_bounds() -> None:
    quote = quote_arrears_payment(_context(stage="new"), 5000.0, 30, service_type="consultation")
    assert quote["eligible"] is False
    assert "range" in quote["reason"]
    assert quote["policy"]["policy_id"] == "new_customer_growth_deferral"


def test_quote_eligibility_defer_too_long() -> None:
    quote = quote_arrears_payment(_context(stage="new"), 300.0, 200, service_type="consultation")
    assert quote["eligible"] is False
    assert "exceeds the policy max" in quote["reason"]


def test_quote_free_within_grace() -> None:
    quote = quote_arrears_payment(_context(stage="new"), 300.0, 30, service_type="consultation")
    assert quote["eligible"] is True
    assert quote["interest"] == 0.0
    assert quote["total_due"] == 300.0
    assert quote["days_after_grace"] == 0


def test_quote_interest_after_grace() -> None:
    quote = quote_arrears_payment(_context(stage="new"), 1000.0, 60, service_type="consultation")
    assert quote["eligible"] is True
    # 60 - 30 grace = 30 days at 9% annual on 1000
    assert quote["interest"] == pytest.approx(round(1000.0 * 0.09 * 30 / 365, 2), abs=0.01)
    assert quote["policy"]["annual_rate"] == 9.0


# --- persistence / orchestrators (fake db) ---------------------------------


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


class _ArrearsDb:
    def __init__(self, users=(), entries=()):
        self.users = list(users)
        self.entries = list(entries)
        self.commits = 0

    async def execute(self, statement, *_args, **_kwargs):
        text = str(statement)
        if "arrears_entries" in text:
            return _RowsResult(list(self.entries))
        if "FROM users" in text:
            return _RowsResult(list(self.users))
        return _EmptyResult()

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = max([getattr(o, "id", 0) for o in self.entries] or [0]) + 1
        self.entries.append(obj)

    async def delete(self, obj):
        if obj in self.entries:
            self.entries.remove(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def _make_entry(
    principal=1000.0,
    annual_rate=12.0,
    grace_days=14,
    compounding="simple",
    cap_pct=100.0,
    opened_days_ago=60,
    status="open",
    user_id=1,
    defer_days=30,
):
    return models.ArrearsEntry(
        id=1,
        user_id=user_id,
        booking_id=None,
        reference="booking-1",
        service_type="consultation",
        principal=principal,
        currency="USD",
        policy_id="standard_deferral",
        annual_rate=annual_rate,
        grace_days=grace_days,
        compounding=compounding,
        interest_cap_pct=cap_pct,
        defer_days=defer_days,
        interest_accrued=0.0,
        status=status,
        opened_at=NOW - timedelta(days=opened_days_ago),
        due_at=(NOW - timedelta(days=opened_days_ago)) + timedelta(days=defer_days),
        settled_at=None,
        settled_interest=0.0,
        total_settled=0.0,
        interest_waived=False,
        waived_interest=0.0,
        note="",
    )


@pytest.mark.asyncio
async def test_open_arrears_selects_policy_and_snapshots_terms() -> None:
    db = _ArrearsDb(users=[(1, "alice")])
    entry = await arrears_payments.open_arrears_payment(
        db, 1, 1000.0, 60, service_type="consultation", reference="booking-9", note="ok"
    )
    assert entry["status"] == "open"
    # The journey engine scores an empty history as premium tier, so the VIP
    # policy (best terms) is selected on this bare fake DB.
    assert entry["policy_id"] == "vip_premium_deferral"
    assert entry["annual_rate"] == 6.0
    assert entry["grace_days"] == 45
    assert entry["interest_accrued"] == 0.0
    assert entry["reference"] == "booking-9"
    assert entry["due_at"] is not None


@pytest.mark.asyncio
async def test_open_arrears_rejects_ineligible_principal() -> None:
    db = _ArrearsDb(users=[(1, "alice")])
    with pytest.raises(ValueError):
        await arrears_payments.open_arrears_payment(db, 1, 50000.0, 30, service_type="consultation")


@pytest.mark.asyncio
async def test_quote_arrears_for_user() -> None:
    db = _ArrearsDb(users=[(1, "alice")])
    quote = await arrears_payments.quote_arrears_for_user(db, 1, 1000.0, 60, service_type="consultation")
    assert quote.eligible is True
    # VIP policy on the default (empty-history premium) context: 15 days after
    # the 45-day grace at 6% annual on 1000.
    assert quote.policy["policy_id"] == "vip_premium_deferral"
    assert quote.interest == pytest.approx(round(1000.0 * 0.06 * 15 / 365, 2), abs=0.01)


@pytest.mark.asyncio
async def test_list_user_arrears_aggregates_open_totals() -> None:
    db = _ArrearsDb(
        users=[(1, "alice")],
        entries=[_make_entry(principal=1000.0), _make_entry(principal=500.0, opened_days_ago=100, user_id=1)],
    )
    report = await arrears_payments.list_user_arrears(db, 1)
    assert report.total == 2
    assert report.open_total_principal == pytest.approx(1500.0, abs=0.01)


@pytest.mark.asyncio
async def test_settle_arrears_charges_accrued_interest() -> None:
    db = _ArrearsDb(users=[(1, "alice")], entries=[_make_entry(opened_days_ago=60)])
    result = await arrears_payments.settle_arrears_entry(db, 1)
    assert result is not None
    # 60 - 14 grace = 46 days at 12% simple on 1000
    expected_interest = round(1000.0 * 0.12 * 46 / 365, 2)
    assert result.interest_charged == pytest.approx(expected_interest, abs=0.01)
    assert result.total_paid == pytest.approx(1000.0 + expected_interest, abs=0.01)
    assert result.entry.status == "settled"
    assert db.entries[0].status == "settled"


@pytest.mark.asyncio
async def test_settle_twice_raises() -> None:
    db = _ArrearsDb(users=[(1, "alice")], entries=[_make_entry(opened_days_ago=60)])
    await arrears_payments.settle_arrears_entry(db, 1)
    with pytest.raises(ValueError):
        await arrears_payments.settle_arrears_entry(db, 1)


@pytest.mark.asyncio
async def test_waive_interest_then_settle_principal_only() -> None:
    db = _ArrearsDb(users=[(1, "alice")], entries=[_make_entry(opened_days_ago=60)])
    result = await arrears_payments.waive_arrears_interest(db, 1)
    assert result is not None
    expected_interest = round(1000.0 * 0.12 * 46 / 365, 2)
    assert result.waived_interest == pytest.approx(expected_interest, abs=0.01)
    assert result.total_owed == 1000.0
    assert db.entries[0].status == "waived"
    settled = await arrears_payments.settle_arrears_entry(db, 1)
    assert settled is not None
    assert settled.interest_charged == 0.0
    assert settled.total_paid == 1000.0


@pytest.mark.asyncio
async def test_waive_only_open_entries() -> None:
    closed = _make_entry(opened_days_ago=60)
    closed.status = "settled"
    db = _ArrearsDb(users=[(1, "alice")], entries=[closed])
    with pytest.raises(ValueError):
        await arrears_payments.waive_arrears_interest(db, 1)


@pytest.mark.asyncio
async def test_admin_report_rollup() -> None:
    db = _ArrearsDb(
        users=[(1, "alice")],
        entries=[
            _make_entry(principal=1000.0, opened_days_ago=90),
            _make_entry(principal=500.0, opened_days_ago=10, user_id=1),
        ],
    )
    report = await arrears_payments.build_arrears_admin_report(db)
    assert report.total_entries == 2
    assert report.total_open == 2
    assert report.principal_at_risk == pytest.approx(1500.0, abs=0.01)
    assert report.overdue_count == 1  # 90-day-old entry past its 30-day due date
    assert report.top_policy == "standard_deferral"
    assert report.entries[0].username == "alice"


def test_arrears_catalog_exposes_policies() -> None:
    catalog = arrears_payments.build_arrears_catalog()
    assert catalog["catalog_version"] == "arrears_payments_v1"
    assert set(catalog["statuses"]) == {"open", "settled", "waived"}
    assert catalog["compounding_modes"] == ["simple", "daily"]
    assert {rule["policy_id"] for rule in catalog["interest_policies"]} == {
        rule["policy_id"] for rule in ARREARS_INTEREST_POLICIES
    }


# --- endpoint wiring --------------------------------------------------------


class _FakeUser:
    id = 1
    username = "tester"
    is_admin = True


def test_arrears_quote_endpoint_empty_db() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _EmptyResultDb()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get(
            "/chat/payments/arrears/quote",
            params={"principal": 1000, "defer_days": 60, "service_type": "consultation"},
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["eligible"] is True
    assert payload["policy"]["policy_id"] == "vip_premium_deferral"
    assert payload["user_id"] == 1


def test_arrears_open_endpoint_and_list() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    db = _ArrearsDb(users=[(1, "tester")])

    async def _fake_get_db():
        yield db

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        client = TestClient(app)
        opened = client.post(
            "/chat/payments/arrears",
            json={
                "principal": 800.0,
                "defer_days": 60,
                "service_type": "consultation",
                "reference": "booking-7",
            },
        )
        assert opened.status_code == 200
        entry = opened.json()
        assert entry["status"] == "open"
        assert entry["policy_id"] == "vip_premium_deferral"
        assert entry["principal"] == 800.0

        listing = client.get("/chat/payments/arrears")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["open_total_principal"] == 800.0
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)


def test_arrears_open_endpoint_422_out_of_range() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _ArrearsDb(users=[(1, "tester")])

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).post(
            "/chat/payments/arrears",
            json={"principal": 50000.0, "defer_days": 30, "service_type": "consultation"},
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 422
    assert "not eligible" in response.json()["detail"]


def test_arrears_quote_validation_422() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _EmptyResultDb()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/chat/payments/arrears/quote", params={"principal": 0})
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 422


def test_arrears_admin_settle_and_waive_endpoints() -> None:
    async def _fake_get_current_admin_user():
        return _FakeUser()

    db = _ArrearsDb(users=[(1, "tester")], entries=[_make_entry(opened_days_ago=60)])

    async def _fake_get_db():
        yield db

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        client = TestClient(app)
        report = client.get("/chat/admin/payments/arrears")
        assert report.status_code == 200
        assert report.json()["total_entries"] == 1
        assert report.json()["entries"][0]["username"] == "tester"

        waived = client.post("/chat/admin/payments/arrears/1/waive-interest")
        assert waived.status_code == 200
        assert waived.json()["total_owed"] == 1000.0
        assert waived.json()["waived_interest"] > 0

        settled = client.post("/chat/admin/payments/arrears/1/settle")
        assert settled.status_code == 200
        assert settled.json()["interest_charged"] == 0.0
        assert settled.json()["total_paid"] == 1000.0

        already = client.post("/chat/admin/payments/arrears/1/settle")
        assert already.status_code == 422
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)


def test_arrears_admin_settle_missing_entry_404() -> None:
    async def _fake_get_current_admin_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _ArrearsDb(users=[(1, "tester")])

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).post("/chat/admin/payments/arrears/99/settle")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 404


def test_meta_scoring_catalog_includes_arrears_payments() -> None:
    response = TestClient(app).get("/meta/scoring-catalog")
    assert response.status_code == 200
    catalog = response.json()["arrears_payments"]
    assert catalog["catalog_version"] == "arrears_payments_v1"
    assert "interest_policies" in catalog
    assert "/chat/payments/arrears" in catalog["endpoints"]["self_open"]


class _EmptyResultDb:
    async def execute(self, *_args, **_kwargs):
        return _EmptyResult()