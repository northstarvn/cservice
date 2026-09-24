"""Tests for the points <-> money/currency exchange expansion.

The backend now lets users convert/exchange back and forth between *certain
types* of points and money/currencies. Exchange rules are config-driven
(`POINTS_EXCHANGE_RULES` in `app/services/points_exchange.py`): each rule
binds a (point_type, currency) pair, declares which directions are allowed
(``redeem`` = points -> money, ``purchase`` = money -> points), the rate
(points per currency unit), a fee, minimums, and daily caps, plus `when`-DSL
eligibility (e.g. premium users get a better loyalty rate; brand-new users
cannot redeem activity points).

Surface: `GET /chat/points/exchange/rates`, `GET /chat/points/wallet`,
`GET /chat/points/transactions`, `POST /chat/points/exchange/quote`,
`POST /chat/points/exchange`, `GET /chat/admin/points/exchange`, plus the
`points_exchange` catalog in `/meta/scoring-catalog`.
"""
import os
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import deps, models
from app.main import app
from app.services import points_exchange
from app.services.points_exchange import (
    POINTS_EXCHANGE_RULES,
    compute_daily_used_money,
    list_convertible_point_types,
    quote_points_exchange,
    select_exchange_rule,
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
        "top_issue": "",
    }
    base.update(overrides)
    return base


# --- catalog validity -------------------------------------------------------


def test_exchange_rule_catalog_valid() -> None:
    ids = [rule["rule_id"] for rule in POINTS_EXCHANGE_RULES]
    assert len(ids) == len(set(ids))
    for rule in POINTS_EXCHANGE_RULES:
        params = rule["params"]
        for field in (
            "purchase_enabled",
            "redeem_enabled",
            "points_per_unit",
            "fee_pct",
            "min_redeem_points",
            "min_purchase_money",
            "max_daily_redeem_money",
            "max_daily_purchase_money",
        ):
            assert field in params
        assert params["points_per_unit"] > 0


def test_convertible_point_types_flags() -> None:
    types = list_convertible_point_types()
    assert set(types) == {"loyalty_points", "activity_points", "cashback_points", "referral_points"}
    assert types["cashback_points"]["redeem_enabled"] is True
    assert types["cashback_points"]["purchase_enabled"] is False
    assert types["referral_points"]["purchase_enabled"] is False
    assert types["loyalty_points"]["purchase_enabled"] is True
    assert "USD" in types["loyalty_points"]["currencies"]
    assert "EUR" in types["loyalty_points"]["currencies"]


# --- pure quote / selection -------------------------------------------------


def test_select_premium_rule_beats_standard_for_loyalty_usd() -> None:
    rule, fields = select_exchange_rule("loyalty_points", "redeem", "USD", _context(value_tier="premium"))
    assert rule["rule_id"] == "loyalty_points_usd_premium"
    assert rule["params"]["points_per_unit"] == 90.0
    assert fields["value_tier"] == "premium"


def test_select_standard_rule_for_growth_user() -> None:
    rule, _ = select_exchange_rule("loyalty_points", "redeem", "USD", _context())
    assert rule["rule_id"] == "loyalty_points_usd_standard"
    assert rule["params"]["points_per_unit"] == 100.0


def test_select_activity_rule_blocks_new_stage() -> None:
    assert select_exchange_rule("activity_points", "redeem", "USD", _context(stage="new"))[0] is None
    rule, _ = select_exchange_rule("activity_points", "redeem", "USD", _context(stage="engaged"))
    assert rule["rule_id"] == "activity_points_usd"


def test_select_disabled_direction_returns_none() -> None:
    assert select_exchange_rule("cashback_points", "purchase", "USD", _context())[0] is None
    assert select_exchange_rule("referral_points", "purchase", "USD", _context())[0] is None
    assert select_exchange_rule("loyalty_points", "redeem", "EUR", _context())[0]["rule_id"] == "loyalty_points_eur_standard"


def test_quote_redeem_points_to_money() -> None:
    quote = quote_points_exchange("loyalty_points", "redeem", 1000.0, "USD", _context())
    assert quote["eligible"] is True
    # 1000 pts @ 100 pts/$ -> gross $10, fee 2% -> net $9.80
    assert quote["points_per_unit"] == 100.0
    assert quote["gross_output"] == pytest.approx(10.0, abs=0.01)
    assert quote["fee"] == pytest.approx(0.2, abs=0.01)
    assert quote["output_amount"] == pytest.approx(9.8, abs=0.01)
    assert quote["input_unit"] == "points"
    assert quote["output_unit"] == "USD"


def test_quote_purchase_money_to_points() -> None:
    quote = quote_points_exchange("loyalty_points", "purchase", 25.0, "USD", _context())
    assert quote["eligible"] is True
    # $25 @ 100 pts/$ -> 2500 gross, fee 2% -> 2450 net
    assert quote["gross_output"] == pytest.approx(2500.0, abs=0.01)
    assert quote["fee"] == pytest.approx(50.0, abs=0.01)
    assert quote["output_amount"] == pytest.approx(2450.0, abs=0.01)


def test_quote_min_redeem_not_met() -> None:
    quote = quote_points_exchange("loyalty_points", "redeem", 50.0, "USD", _context())
    assert quote["eligible"] is False
    assert "Minimum redeem" in quote["reason"]


def test_quote_daily_cap_exceeded() -> None:
    quote = quote_points_exchange(
        "loyalty_points", "redeem", 1000.0, "USD", _context(), daily_used_money=45.0
    )
    assert quote["eligible"] is True
    assert quote["exceeds_daily_cap"] is True


def test_quote_unknown_pair_ineligible() -> None:
    quote = quote_points_exchange("diamond_points", "redeem", 1000.0, "USD", _context())
    assert quote["eligible"] is False


def test_quote_negative_amount_rejected() -> None:
    quote = quote_points_exchange("loyalty_points", "redeem", -5.0, "USD", _context())
    assert quote["eligible"] is False


# --- persistence / orchestrators (fake db) ----------------------------------


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
    def __init__(self, rows, scalar_value=None):
        self.rows = rows
        self.scalar_value = scalar_value

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.scalar_value

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class _PointsDb:
    def __init__(self, users=(), wallets=(), txns=()):
        self.users = list(users)
        self.wallets = list(wallets)
        self.txns = list(txns)
        self.commits = 0

    async def execute(self, statement, *_args, **_kwargs):
        text = str(statement)
        if "coalesce" in text:  # daily-used aggregate
            total = sum(float(getattr(t, "currency_amount", 0.0) or 0.0) for t in self.txns)
            return _RowsResult([], scalar_value=total)
        if "points_wallets" in text:
            return _RowsResult(list(self.wallets))
        if "points_transactions" in text:
            return _RowsResult(list(self.txns))
        if "FROM users" in text:
            return _RowsResult(list(self.users))
        return _EmptyResult()

    def add(self, obj):
        if isinstance(obj, models.PointsWallet):
            if getattr(obj, "id", None) is None:
                obj.id = max([getattr(w, "id", 0) for w in self.wallets] or [0]) + 1
            self.wallets.append(obj)
        elif isinstance(obj, models.PointsTransaction):
            obj.id = max([getattr(t, "id", 0) for t in self.txns] or [0]) + 1
            self.txns.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def _make_wallet(point_type="loyalty_points", balance=1000.0, user_id=1):
    return models.PointsWallet(
        id=1, user_id=user_id, point_type=point_type, balance=balance,
        created_at=NOW, updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_execute_purchase_creates_wallet_and_ledger() -> None:
    db = _PointsDb(users=[(1, "tester")])
    result = await points_exchange.execute_points_exchange_for_user(
        db, 1, "loyalty_points", "purchase", 25.0, "USD", reference="topup-1"
    )
    assert result is not None
    assert result.kind == "purchase_points"
    # Empty-history context resolves to the premium tier on the journey engine,
    # so the premium rule applies: 90 pts/$ with a 1.5% fee -> 25 * 90 * 0.985
    assert result.points_delta == pytest.approx(2216.25, abs=0.01)
    assert result.new_balance == pytest.approx(2216.25, abs=0.01)
    assert result.currency_amount == 25.0
    assert len(db.wallets) == 1
    assert db.wallets[0].balance == pytest.approx(2216.25, abs=0.01)
    assert len(db.txns) == 1
    assert db.txns[0].point_type == "loyalty_points"


@pytest.mark.asyncio
async def test_execute_redeem_debits_wallet() -> None:
    db = _PointsDb(users=[(1, "tester")], wallets=[_make_wallet(balance=2000.0)])
    result = await points_exchange.execute_points_exchange_for_user(
        db, 1, "loyalty_points", "redeem", 1000.0, "USD"
    )
    assert result is not None
    assert result.kind == "redeem_points"
    assert result.points_delta == pytest.approx(-1000.0, abs=0.01)
    assert result.new_balance == pytest.approx(1000.0, abs=0.01)
    # gross money on the premium rule: 1000 / 90 pts-per-$
    assert result.currency_amount == pytest.approx(round(1000.0 / 90.0, 2), abs=0.01)


@pytest.mark.asyncio
async def test_execute_redeem_insufficient_balance() -> None:
    db = _PointsDb(users=[(1, "tester")], wallets=[_make_wallet(balance=50.0)])
    with pytest.raises(ValueError, match="Insufficient"):
        await points_exchange.execute_points_exchange_for_user(
            db, 1, "loyalty_points", "redeem", 1000.0, "USD"
        )


@pytest.mark.asyncio
async def test_execute_blocks_cashback_purchase() -> None:
    db = _PointsDb(users=[(1, "tester")])
    with pytest.raises(ValueError, match="No purchase exchange rule"):
        await points_exchange.execute_points_exchange_for_user(
            db, 1, "cashback_points", "purchase", 10.0, "USD"
        )


@pytest.mark.asyncio
async def test_execute_respects_daily_cap(monkeypatch) -> None:
    async def fake_daily(*_args, **_kwargs):
        return 95.0  # premium rule caps daily redeems at $100

    monkeypatch.setattr(points_exchange, "compute_daily_used_money", fake_daily)
    db = _PointsDb(users=[(1, "tester")], wallets=[_make_wallet(balance=2000.0)])
    with pytest.raises(ValueError, match="Daily exchange cap"):
        await points_exchange.execute_points_exchange_for_user(
            db, 1, "loyalty_points", "redeem", 1000.0, "USD"
        )


@pytest.mark.asyncio
async def test_compute_daily_used_summarizes_today() -> None:
    db = _PointsDb(
        users=[(1, "tester")],
        txns=[
            models.PointsTransaction(
                id=1, user_id=1, point_type="loyalty_points", kind="redeem_points",
                points_delta=-100.0, currency="USD", currency_amount=1.0, rate=100.0,
                fee=0.02, reference="", created_at=NOW, updated_at=NOW,
            ),
            models.PointsTransaction(
                id=2, user_id=1, point_type="loyalty_points", kind="redeem_points",
                points_delta=-200.0, currency="USD", currency_amount=2.0, rate=100.0,
                fee=0.04, reference="", created_at=NOW, updated_at=NOW,
            ),
        ],
    )
    used = await compute_daily_used_money(db, 1, "loyalty_points", "USD", "redeem")
    assert used == pytest.approx(3.0, abs=0.01)


@pytest.mark.asyncio
async def test_wallet_report_zero_fills_convertible_types() -> None:
    db = _PointsDb(users=[(1, "tester")])
    report = await points_exchange.list_user_wallets(db, 1)
    assert report.convertible_types == 4
    assert report.total_balance == 0.0
    by_type = {wallet.point_type: wallet for wallet in report.wallets}
    assert by_type["cashback_points"].redeem_enabled is True
    assert by_type["cashback_points"].purchase_enabled is False
    assert by_type["loyalty_points"].purchase_enabled is True


@pytest.mark.asyncio
async def test_wallet_report_includes_balance() -> None:
    db = _PointsDb(users=[(1, "tester")], wallets=[_make_wallet(balance=1500.0)])
    report = await points_exchange.list_user_wallets(db, 1)
    by_type = {wallet.point_type: wallet for wallet in report.wallets}
    assert by_type["loyalty_points"].balance == pytest.approx(1500.0, abs=0.01)


@pytest.mark.asyncio
async def test_transactions_report_and_admin_rollup() -> None:
    db = _PointsDb(
        users=[(1, "tester")],
        txns=[
            models.PointsTransaction(
                id=1, user_id=1, point_type="loyalty_points", kind="purchase_points",
                points_delta=2450.0, currency="USD", currency_amount=25.0, rate=100.0,
                fee=50.0, reference="", created_at=NOW, updated_at=NOW,
            ),
            models.PointsTransaction(
                id=2, user_id=1, point_type="loyalty_points", kind="redeem_points",
                points_delta=-1000.0, currency="USD", currency_amount=10.0, rate=100.0,
                fee=0.2, reference="", created_at=NOW, updated_at=NOW,
            ),
        ],
    )
    report = await points_exchange.list_user_point_transactions(db, 1)
    assert report.total == 2
    admin = await points_exchange.build_points_exchange_admin_report(db)
    assert admin.total_transactions == 2
    assert admin.total_purchased_points == pytest.approx(2450.0, abs=0.01)
    assert admin.total_redeemed_points == pytest.approx(-1000.0, abs=0.01)
    assert admin.total_money_moved == pytest.approx(35.0, abs=0.01)
    assert admin.top_user == "tester"
    assert admin.by_kind == {"purchase_points": 1, "redeem_points": 1}


def test_rates_builder_public_shape() -> None:
    rates = points_exchange.build_points_exchange_rates()
    assert rates.catalog_version == "points_exchange_v1"
    assert rates.currencies == ["EUR", "USD"]
    assert len(rates.point_types) == 4
    assert len(rates.rules) == len(POINTS_EXCHANGE_RULES)


def test_points_exchange_catalog_exposes_rules() -> None:
    catalog = points_exchange.build_points_exchange_catalog()
    assert catalog["catalog_version"] == "points_exchange_v1"
    assert catalog["directions"] == ["redeem", "purchase"]
    assert {rule["rule_id"] for rule in catalog["rules"]} == {r["rule_id"] for r in POINTS_EXCHANGE_RULES}
    assert "cashback_points" in catalog["point_types"]


# --- endpoint wiring --------------------------------------------------------


class _FakeUser:
    id = 1
    username = "tester"
    is_admin = True


class _EmptyResultDb:
    async def execute(self, *_args, **_kwargs):
        return _EmptyResult()


def test_points_rates_endpoint() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _EmptyResultDb()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).get("/chat/points/exchange/rates")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["currencies"] == ["EUR", "USD"]
    assert len(payload["rules"]) == 6


def test_points_quote_endpoint_eligible() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _EmptyResultDb()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).post(
            "/chat/points/exchange/quote",
            json={"point_type": "loyalty_points", "direction": "redeem", "amount": 1000, "currency": "USD"},
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["eligible"] is True
    # premium rule on default (empty-history) context: 1000 pts / 90 pts-$ minus 1.5%
    assert payload["output_amount"] == pytest.approx(10.94, abs=0.01)


def test_points_quote_endpoint_ineligible_returns_200() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _EmptyResultDb()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).post(
            "/chat/points/exchange/quote",
            json={"point_type": "cashback_points", "direction": "purchase", "amount": 10, "currency": "USD"},
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 200
    assert response.json()["eligible"] is False


def test_points_quote_validation_bad_direction_422() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _EmptyResultDb()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).post(
            "/chat/points/exchange/quote",
            json={"point_type": "loyalty_points", "direction": "gold", "amount": 1000},
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 422


def test_points_execute_endpoint_purchase_then_redeem() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    db = _PointsDb(users=[(1, "tester")])

    async def _fake_get_db():
        yield db

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        client = TestClient(app)
        purchase = client.post(
            "/chat/points/exchange",
            json={"point_type": "loyalty_points", "direction": "purchase", "amount": 20, "currency": "USD", "reference": "topup-1"},
        )
        assert purchase.status_code == 200
        # premium rule on default context: 20 * 90 pts-$ * (1 - 1.5%) = 1773
        assert purchase.json()["new_balance"] == pytest.approx(1773.0, abs=0.01)

        wallet = client.get("/chat/points/wallet")
        assert wallet.status_code == 200
        loyalty = next(w for w in wallet.json()["wallets"] if w["point_type"] == "loyalty_points")
        assert loyalty["balance"] == pytest.approx(1773.0, abs=0.01)

        redeem = client.post(
            "/chat/points/exchange",
            json={"point_type": "loyalty_points", "direction": "redeem", "amount": 1000, "currency": "USD"},
        )
        assert redeem.status_code == 200
        assert redeem.json()["new_balance"] == pytest.approx(773.0, abs=0.01)
        assert redeem.json()["points_delta"] == pytest.approx(-1000.0, abs=0.01)

        txns = client.get("/chat/points/transactions")
        assert txns.status_code == 200
        assert txns.json()["total"] == 2
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)


def test_points_execute_endpoint_insufficient_422() -> None:
    async def _fake_get_current_user():
        return _FakeUser()

    async def _fake_get_db():
        yield _PointsDb(users=[(1, "tester")])

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_user] = _fake_get_current_user
    try:
        response = TestClient(app).post(
            "/chat/points/exchange",
            json={"point_type": "loyalty_points", "direction": "redeem", "amount": 1000, "currency": "USD"},
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_user, None)

    assert response.status_code == 422
    assert "Insufficient" in response.json()["detail"]


def test_points_admin_report_endpoint() -> None:
    async def _fake_get_current_admin_user():
        return _FakeUser()

    txns = [
        models.PointsTransaction(
            id=1, user_id=1, point_type="loyalty_points", kind="purchase_points",
            points_delta=1000.0, currency="USD", currency_amount=10.0, rate=100.0,
            fee=20.0, reference="", created_at=NOW, updated_at=NOW,
        )
    ]

    async def _fake_get_db():
        yield _PointsDb(users=[(1, "tester")], txns=txns)

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/chat/admin/points/exchange")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_transactions"] == 1
    assert payload["top_user"] == "tester"


def test_meta_scoring_catalog_includes_points_exchange() -> None:
    response = TestClient(app).get("/meta/scoring-catalog")
    assert response.status_code == 200
    catalog = response.json()["points_exchange"]
    assert catalog["catalog_version"] == "points_exchange_v1"
    assert catalog["directions"] == ["redeem", "purchase"]
    assert "/chat/points/exchange" in catalog["endpoints"]["execute"]