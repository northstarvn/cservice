"""Tests for Stage 1: Business Rule Hyper-Flexibility & Granular Customization.

Three slices are covered here:

R1 — Shared `when`-DSL rule engine core (`app/rule_engine.py`)
    - whole-rule combinators (any / all / not)
    - reserved `_date` key bound to the evaluation date
    - date-window operators (between_dates, month_in, weekday_in, on_date,
      within_days)
    - safe `=formula` expression params (arithmetic, comparisons, ternary,
      allow-listed calls) and rejection of arbitrary code
    - delegation regression: loyalty_journey / communication_strategy /
      arrears_payments / points_exchange keep behaving identically

R2 — Financial flexibility
    - points: tier * LTV * seasonal-campaign rate multipliers, driven only by
      explicit `effective_date` / `param_overrides` (default quotes unchanged);
      formula `param_overrides` resolved by the rule engine
    - arrears: late-fee terms snapshotted at open, charged when past due,
      policy-score-governed fee/interest waivers

R3 — Regional bookings & tax (`app/regional_policy.py`)
    - per-region calendars (weekends + holidays, annual and fixed)
    - labor-compliance guardrails
    - per-jurisdiction tax rates by service type
    - `/meta/regional` + scoring-catalog / ecosystem wiring

Constraints honored: default single-tenant behavior and existing pinned quote
contracts are untouched — dynamic rate multipliers never silently reprice the
default path.
"""
import os
import sys
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import models, rule_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import (  # noqa: E402
    arrears_payments,
    communication_strategy,
    loyalty_journey,
    points_exchange,
)
from app.services.arrears_payments import (  # noqa: E402
    ARREARS_WAIVER_POLICY,
    build_arrears_catalog,
    compute_late_fee,
    evaluate_waiver_approval,
    settle_arrears_entry,
    waive_arrears_fees,
    waive_arrears_interest,
)
from app.services.points_exchange import (  # noqa: E402
    POINTS_CAMPAIGN_RULES,
    POINTS_LTV_MULTIPLIERS,
    POINTS_TIER_MULTIPLIERS,
    build_points_exchange_catalog,
    quote_points_exchange,
    select_active_campaigns,
)
from app.services.policy_scoring import PolicyScoreSnapshot  # noqa: E402
from app.regional_policy import (  # noqa: E402
    assess_booking_dates,
    build_regional_policy_catalog,
    compute_taxed_amount,
    evaluate_labor_compliance,
    is_working_day,
)

NOW = datetime.now(timezone.utc)

PREMIUM_SCORE = PolicyScoreSnapshot(
    system_score=90.0,
    customer_score=85.0,
    access_score=80.0,
    interest_score=80.0,
    closeness_score=75.0,
    community_closeness_score=70.0,
    policy_tier="customer-premium",
    control_posture="customer_trusted",
    summary="premium admin snapshot",
)

WEAK_SCORE = replace(PREMIUM_SCORE, access_score=50.0)
STANDARD_SCORE = replace(PREMIUM_SCORE, policy_tier="standard")


# ===========================================================================
# R1 — shared rule engine core
# ===========================================================================


class TestRuleEngineCombinators:
    def test_any_first_match_wins(self):
        ok, fields = rule_engine.evaluate_when(
            {"any": [{"stage": "new"}, {"stage": "engaged"}]},
            {"stage": "engaged"},
        )
        assert ok is True
        assert fields["any"] == {"stage": "engaged"}

    def test_any_none_match_fails(self):
        ok, _ = rule_engine.evaluate_when(
            {"any": [{"stage": "new"}, {"stage": "engaged"}]},
            {"stage": "dormant"},
        )
        assert ok is False

    def test_all_every_condition_must_match(self):
        when = {"all": [{"stage": "engaged"}, {"churn_risk": {"ne": "high"}}]}
        assert rule_engine.evaluate_when(when, {"stage": "engaged", "churn_risk": "low"})[0] is True
        assert rule_engine.evaluate_when(when, {"stage": "engaged", "churn_risk": "high"})[0] is False

    def test_not_inverts(self):
        assert rule_engine.evaluate_when({"not": {"churn_risk": "high"}}, {"churn_risk": "low"})[0] is True
        assert rule_engine.evaluate_when({"not": {"churn_risk": "high"}}, {"churn_risk": "high"})[0] is False

    def test_nested_combinator(self):
        when = {
            "all": [
                {"any": [{"stage": "new"}, {"stage": "engaged"}]},
                {"not": {"sentiment_label": "negative"}},
            ]
        }
        ctx = {"stage": "engaged", "sentiment_label": "positive"}
        assert rule_engine.evaluate_when(when, ctx)[0] is True
        assert rule_engine.evaluate_when(when, {**ctx, "sentiment_label": "negative"})[0] is False


class TestRuleEngineDateOps:
    def test_between_dates_on_reserved_date_key(self):
        when = {"_date": {"between_dates": ["2026-06-01", "2026-08-31"]}}
        assert rule_engine.evaluate_when(when, {}, effective_date="2026-07-15")[0] is True
        assert rule_engine.evaluate_when(when, {}, effective_date="2026-09-01")[0] is False

    def test_month_in(self):
        when = {"_date": {"month_in": [6, 7, 8]}}
        assert rule_engine.evaluate_when(when, {}, effective_date="2026-08-20")[0] is True
        assert rule_engine.evaluate_when(when, {}, effective_date="2026-09-20")[0] is False

    def test_weekday_in(self):
        when = {"_date": {"weekday_in": ["sat", "sun"]}}
        # 2026-07-18 is a Saturday
        assert rule_engine.evaluate_when(when, {}, effective_date="2026-07-18")[0] is True
        assert rule_engine.evaluate_when(when, {}, effective_date="2026-07-20")[0] is False  # Monday

    def test_on_date(self):
        when = {"_date": {"on_date": "2026-12-25"}}
        assert rule_engine.evaluate_when(when, {}, effective_date="2026-12-25")[0] is True
        assert rule_engine.evaluate_when(when, {}, effective_date="2026-12-24")[0] is False

    def test_within_days_inclusive(self):
        # within_days measures a context date field against the evaluation date
        when = {"booking_date": {"within_days": 7}}
        ctx = {"booking_date": "2026-07-20"}
        assert rule_engine.evaluate_when(when, ctx, effective_date="2026-07-25")[0] is True
        assert rule_engine.evaluate_when(when, {"booking_date": "2026-07-01"}, effective_date="2026-07-25")[0] is False
        assert rule_engine.evaluate_when(when, {"booking_date": "2026-07-18"}, effective_date="2026-07-25")[0] is True

    def test_date_ops_on_context_fields(self):
        # a booking date field matched against the evaluation date
        when = {"booking_date": {"month_in": [6, 7, 8]}}
        ctx = {"booking_date": "2026-08-14"}
        assert rule_engine.evaluate_when(when, ctx, effective_date="2026-08-14")[0] is True

    def test_missing_context_field_fails_like_legacy(self):
        ok, _ = rule_engine.evaluate_when({"not_there": "x"}, {"stage": "engaged"})
        assert ok is False


class TestRuleEngineExpressions:
    def test_arithmetic(self):
        assert rule_engine.evaluate_expression("2 + 3 * 4") == 14
        assert rule_engine.evaluate_expression("(1 + 2) ** 2") == 9
        assert rule_engine.evaluate_expression("7 // 2") == 3
        assert rule_engine.evaluate_expression("7 % 3") == 1

    def test_scope_names_and_math_constants(self):
        assert rule_engine.evaluate_expression("x * 1.1", {"x": 100}) == pytest.approx(110.0)
        assert rule_engine.evaluate_expression("round(pi, 2)") == pytest.approx(3.14)

    def test_ternary_and_comparisons(self):
        assert rule_engine.evaluate_expression("20 if readiness >= 85 else 10", {"readiness": 90}) == 20
        assert rule_engine.evaluate_expression("20 if readiness >= 85 else 10", {"readiness": 70}) == 10
        assert rule_engine.evaluate_expression("1 < x <= 3", {"x": 2}) is True

    def test_allow_listed_calls(self):
        assert rule_engine.evaluate_expression("abs(-3) + max(1, 5) + min(9, 4)") == 12
        # sqrt(16)=4.0 + ceil(1.2)=2 + floor(1.9)=1 -> 7.0
        assert rule_engine.evaluate_expression("sqrt(16) + ceil(1.2) + floor(1.9)") == pytest.approx(7.0)

    @pytest.mark.parametrize(
        "expr",
        [
            "__import__('os')",
            "a.b",
            "a[0]",
            "a.b if True else 1",  # attribute access in the evaluated branch
            "open('/etc/passwd')",
        ],
    )
    def test_unsafe_expression_rejected(self, expr):
        with pytest.raises(ValueError):
            rule_engine.evaluate_expression(expr, {"a": {"b": 1}})

    def test_resolve_params_nested(self):
        params = {
            "points_per_unit": "=100 * 1.1",
            "limits": {"allowed": "=threshold + 5", "label": "static"},
            "tags": ["=1 + 1", "plain"],
        }
        scope = {"threshold": 10}
        resolved = rule_engine.resolve_params(params, scope)
        assert resolved["points_per_unit"] == pytest.approx(110.0)
        assert resolved["limits"] == {"allowed": 15, "label": "static"}
        assert resolved["tags"] == [2, "plain"]


class TestSharedEngineDelegation:
    """The four service engines delegate to the same core; behavior is identical."""

    def test_loyalty_journey_field_matcher(self):
        # `_matches_rule` is a field-level matcher (scalar rule values and
        # operator dicts), exactly how `evaluate_scenario_when` calls it.
        assert loyalty_journey._matches_rule("new", "engaged") is False
        assert loyalty_journey._matches_rule("new", "new") is True
        assert loyalty_journey._matches_rule({"gte": 1}, 2) is True
        assert loyalty_journey._matches_rule({"lt": 5}, 7) is False
        assert loyalty_journey._matches_rule(["low", "medium"], "low") is True
        assert loyalty_journey._matches_rule(True, True) is True
        assert loyalty_journey._matches_rule("positive", ["negative", "positive"]) is True

    def test_communication_strategy_delegation(self):
        ok, _ = communication_strategy.evaluate_when(
            {"churn_risk": "high", "at_risk": True},
            {"churn_risk": "high", "at_risk": True, "dormant": False},
        )
        assert ok is True
        ok, _ = communication_strategy.evaluate_when(
            {"churn_risk": "high"},
            {"churn_risk": "low"},
        )
        assert ok is False

    def test_arrears_payments_delegation(self):
        ok, _ = arrears_payments.evaluate_when({"stage": "new"}, {"stage": "new"})
        assert ok is True

    def test_points_exchange_delegation(self):
        ok, _ = points_exchange.evaluate_when({"value_tier": "premium"}, {"value_tier": "premium"})
        assert ok is True
        assert points_exchange._matches_rule("premium", "growth") is False

    def test_catalog_lists_all_consumers(self):
        catalog = rule_engine.build_rule_engine_catalog()
        assert catalog["catalog_version"] == "rule_engine_v1"
        for consumer in ("loyalty_journey", "communication_strategy", "arrears_payments", "points_exchange"):
            assert consumer in catalog["consumers"]
        assert "between_dates" in catalog["date_window_operators"]
        assert "any" in catalog["combine_operators"]


# ===========================================================================
# R2 — points: dynamic rate multipliers
# ===========================================================================


def _points_context(**overrides) -> dict:
    base = {
        "value_tier": "growth",
        "monetization_readiness": 72.0,
        "stage": "engaged",
        "churn_risk": "low",
    }
    base.update(overrides)
    return base


class TestPointsMultiplierConfig:
    def test_config_tables_present(self):
        assert POINTS_TIER_MULTIPLIERS[0]["value_tier"] == "premium"
        assert POINTS_LTV_MULTIPLIERS[0]["min_monetization_readiness"] == 85.0
        assert POINTS_CAMPAIGN_RULES[0]["campaign_id"] == "summer_earn_boost_2026"

    def test_campaign_selection_by_effective_date(self):
        ctx = _points_context()
        summer = select_active_campaigns(ctx, effective_date="2026-07-15")
        spring = select_active_campaigns(ctx, effective_date="2026-04-01")
        dormant = select_active_campaigns(ctx)  # today (2026-09-26) is past both windows
        assert [campaign["campaign_id"] for campaign in summer] == ["summer_earn_boost_2026"]
        assert [campaign["campaign_id"] for campaign in spring] == ["spring_purchase_boost_2026"]
        assert dormant == []


class TestPointsQuoteMultipliers:
    def test_default_quote_additive_and_unchanged(self):
        # No effective_date / param_overrides -> identical legacy payload; the
        # base 100 pts/$ standard rule on a growth context is untouched.
        payload = quote_points_exchange("loyalty_points", "redeem", 1000, "USD", _points_context())
        assert payload["eligible"] is True
        assert payload["points_per_unit"] == 100.0
        assert "points_per_unit_effective" not in payload
        assert "rate_multipliers" not in payload

    def test_driven_quote_applies_summer_campaign(self):
        # growth context (neutral tier/LTV) + summer window: redeem factor 1.05
        ctx = _points_context()
        payload = quote_points_exchange(
            "loyalty_points", "redeem", 1000, "USD", ctx, effective_date="2026-07-15"
        )
        assert payload["eligible"] is True
        assert payload["points_per_unit"] == 100.0
        assert payload["rate_multipliers"]["campaign_id"] == "summer_earn_boost_2026"
        expected_ppu = round(100.0 / 1.05, 4)
        assert payload["points_per_unit_effective"] == pytest.approx(expected_ppu, abs=0.0001)
        gross = 1000.0 / expected_ppu
        assert payload["output_amount"] == pytest.approx(round(gross - gross * 0.02, 2), abs=0.01)

    def test_driven_quote_spring_purchase_only(self):
        ctx = _points_context()
        payload = quote_points_exchange(
            "loyalty_points", "purchase", 25, "USD", ctx, effective_date="2026-04-01"
        )
        assert payload["points_per_unit_effective"] == pytest.approx(100.0 * 1.08, abs=0.0001)
        assert payload["rate_multipliers"]["campaign_id"] == "spring_purchase_boost_2026"
        assert payload["rate_multipliers"]["redeem_multiplier"] == 1.0

    def test_premium_tier_and_ltv_compose(self):
        ctx = _points_context(value_tier="premium", monetization_readiness=90.0)
        payload = quote_points_exchange(
            "loyalty_points", "redeem", 500, "USD", ctx, effective_date="2026-07-15"
        )
        # multiplier is rounded to 4dp before dividing (1.1 * 1.05 * 1.05 -> 1.2128)
        factor = round(1.1 * 1.05 * 1.05, 4)
        assert payload["points_per_unit_effective"] == pytest.approx(round(90.0 / factor, 4), abs=0.0001)
        assert payload["rate_multipliers"]["tier"] == "premium"
        assert payload["rate_multipliers"]["ltv_min_readiness"] == 85.0

    def test_param_overrides_resolve_formula(self):
        ctx = _points_context()
        payload = quote_points_exchange(
            "loyalty_points",
            "redeem",
            1000,
            "USD",
            ctx,
            effective_date="2026-07-15",
            param_overrides={"points_per_unit": "=90 * redeem_multiplier"},
        )
        assert payload["applied_param_overrides"]["points_per_unit"] == pytest.approx(90.0 * 1.05, abs=0.0001)
        assert payload["points_per_unit_effective"] == pytest.approx(round(90.0 * 1.05, 4), abs=0.0001)

    def test_param_overrides_without_date_still_driven(self):
        ctx = _points_context()
        payload = quote_points_exchange(
            "loyalty_points",
            "redeem",
            1000,
            "USD",
            ctx,
            param_overrides={"points_per_unit": "=100 * 1.5"},
        )
        assert payload["applied_param_overrides"]["points_per_unit"] == 150.0
        assert payload["points_per_unit_effective"] == 150.0

    def test_catalog_exposes_rate_multipliers(self):
        catalog = build_points_exchange_catalog()
        assert "rate_multipliers" in catalog
        assert catalog["rate_multipliers"]["campaigns"][0]["campaign_id"] == "summer_earn_boost_2026"
        assert catalog["rate_multipliers"]["tiers"][0]["value_tier"] == "premium"


# ===========================================================================
# R2 — arrears: late fees + policy-score-gated waivers
# ===========================================================================


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
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


def _entry(
    principal=1000.0,
    annual_rate=12.0,
    grace_days=14,
    compounding="simple",
    cap_pct=100.0,
    opened_days_ago=60,
    status="open",
    defer_days=30,
    late_fee_amount=0.0,
    late_fee_pct=0.0,
    fees_waived=False,
    waived_fees=0.0,
):
    opened = NOW - timedelta(days=opened_days_ago)
    return models.ArrearsEntry(
        id=1,
        user_id=1,
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
        opened_at=opened,
        due_at=opened + timedelta(days=defer_days),
        settled_at=None,
        settled_interest=0.0,
        total_settled=0.0,
        interest_waived=False,
        waived_interest=0.0,
        late_fee_amount=late_fee_amount,
        late_fee_pct=late_fee_pct,
        late_fee_charged=False,
        fees_waived=fees_waived,
        waived_fees=waived_fees,
        note="",
    )


class TestLateFeeCore:
    def test_flat_plus_pct_when_past_due(self):
        result = compute_late_fee(500.0, {"late_fee_amount": 10.0, "late_fee_pct": 1.0}, past_due=True)
        assert result["applies"] is True
        assert result["fee_total"] == 15.0  # 10 flat + 1% of 500

    def test_no_fee_until_past_due(self):
        result = compute_late_fee(500.0, {"late_fee_amount": 10.0, "late_fee_pct": 1.0}, past_due=False)
        assert result["applies"] is False
        assert result["fee_total"] == 0.0

    def test_policy_terms_carry_fee_fields(self):
        policy = next(
            p for p in arrears_payments.ARREARS_INTEREST_POLICIES if p["policy_id"] == "high_risk_secured_deferral"
        )
        terms = arrears_payments._policy_terms(policy)
        assert terms["late_fee_amount"] == 10.0
        installment = next(
            p for p in arrears_payments.ARREARS_INTEREST_POLICIES if p["policy_id"] == "large_project_installment"
        )
        assert arrears_payments._policy_terms(installment)["late_fee_pct"] == 1.0


class TestWaiverApproval:
    def test_legacy_no_score_allowed(self):
        approval = evaluate_waiver_approval("interest", None)
        assert approval["allowed"] is True
        assert approval["policy_gated"] is False

    def test_premium_score_allows_fees(self):
        approval = evaluate_waiver_approval("fees", PREMIUM_SCORE)
        assert approval["allowed"] is True
        assert approval["policy_gated"] is True

    def test_low_access_score_denies_fees(self):
        approval = evaluate_waiver_approval("fees", WEAK_SCORE)
        assert approval["allowed"] is False
        assert approval["reason"].startswith("Denied")

    def test_low_tier_denies_interest(self):
        approval = evaluate_waiver_approval("interest", STANDARD_SCORE)
        assert approval["allowed"] is False

    def test_unknown_waiver_type(self):
        approval = evaluate_waiver_approval("unknown", PREMIUM_SCORE)
        assert approval["allowed"] is False

    def test_arrested_policy_config_exposed(self):
        assert ARREARS_WAIVER_POLICY["fees"]["min_access_score"] == 70.0
        assert ARREARS_WAIVER_POLICY["interest"]["required_policy_tier"] == "customer-premium"


class TestArrearsFeeWorkflow:
    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    def test_waive_fees_ungated_legacy(self):
        db = _ArrearsDb(users=[(1, "alice")], entries=[_entry(late_fee_amount=10.0, late_fee_pct=1.0)])
        result = self._run(waive_arrears_fees(db, 1))
        assert result is not None
        assert result["waived_fees"] == 20.0  # 10 flat + 1% of 1000
        assert result["entry"]["fees_waived"] is True
        assert result["entry"]["waived_fees"] == 20.0

    def test_waive_fees_denied_when_score_weak(self):
        db = _ArrearsDb(users=[(1, "alice")], entries=[_entry(late_fee_amount=10.0)])
        with pytest.raises(PermissionError):
            self._run(waive_arrears_fees(db, 1, policy_score=STANDARD_SCORE))

    def test_waive_interest_denied_when_score_weak(self):
        # interest waivers require the customer-premium tier (access is 0-threshold)
        db = _ArrearsDb(users=[(1, "alice")], entries=[_entry()])
        with pytest.raises(PermissionError):
            self._run(waive_arrears_interest(db, 1, policy_score=STANDARD_SCORE))

    def test_waive_interest_allowed_with_premium_score(self):
        db = _ArrearsDb(users=[(1, "alice")], entries=[_entry()])
        result = self._run(waive_arrears_interest(db, 1, policy_score=PREMIUM_SCORE))
        assert result is not None
        assert result.entry.interest_waived is True

    def test_waive_fees_requires_open_entry(self):
        db = _ArrearsDb(users=[(1, "alice")], entries=[_entry(status="settled")])
        with pytest.raises(ValueError):
            self._run(waive_arrears_fees(db, 1))

    def test_settle_charges_late_fee_when_past_due(self):
        row = _entry(late_fee_amount=10.0, late_fee_pct=1.0)  # due 30 days ago, past due
        db = _ArrearsDb(users=[(1, "alice")], entries=[row])

        async def run():
            return await settle_arrears_entry(db, 1)

        result = asyncio.run(run())
        interest = arrears_payments.compute_arrears_interest(
            1000.0, 12.0, 60, grace_days=14, compounding="simple", cap_pct=100.0
        )["interest"]
        assert result.late_fee_charged == pytest.approx(20.0, abs=0.01)
        assert result.total_paid == pytest.approx(1000.0 + interest + 20.0, abs=0.01)
        assert result.entry.late_fee_total == pytest.approx(20.0, abs=0.01)

    def test_settle_no_fee_when_fees_waived(self):
        row = _entry(late_fee_amount=10.0, late_fee_pct=1.0, fees_waived=True, waived_fees=20.0)
        db = _ArrearsDb(users=[(1, "alice")], entries=[row])

        async def run():
            return await settle_arrears_entry(db, 1)

        result = asyncio.run(run())
        assert result.late_fee_charged == 0.0

    def test_catalog_exposes_fee_and_waiver_surfaces(self):
        catalog = build_arrears_catalog()
        assert "late_fee_amount" in catalog["late_fee_fields"]
        assert catalog["waiver_policy"]["types"]["fees"]["min_access_score"] == 70.0


# ===========================================================================
# R3 — regional booking & tax policy
# ===========================================================================


class TestRegionalCalendar:
    def test_fixed_holiday(self):
        verdict = is_working_day("de", "2026-10-03")
        assert verdict["working"] is False
        assert verdict["holiday"] == "Tag der Deutschen Einheit 2026"

    def test_annual_holiday_repeats(self):
        assert is_working_day("us", "2026-12-25")["working"] is False
        assert is_working_day("us", "2027-12-25")["working"] is False

    def test_weekend_not_working(self):
        verdict = is_working_day("us", "2026-07-19")  # Sunday
        assert verdict["working"] is False
        assert verdict["weekend"] is True

    def test_global_fallback_every_day_works(self):
        assert is_working_day("global", "2026-07-19")["working"] is True
        assert is_working_day("unknown-region", "2026-12-25")["working"] is True  # falls back to global

    def test_weekday_record_shape(self):
        verdict = is_working_day("jp", "2026-07-15")  # Wednesday
        assert verdict["weekday"] == "wed"
        assert verdict["working"] is True
        assert verdict["calendar"] == "jp"


class TestRegionalLabor:
    def test_shift_hours_violation(self):
        result = evaluate_labor_compliance(
            "de",
            [{"date": "2026-07-13", "shift_hours": 10.0}],  # de max is 8
        )
        assert result["compliant"] is False
        codes = {v["code"] for v in result["violations"]}
        assert "shift_hours_exceeded" in codes

    def test_consecutive_days_violation(self):
        dates = ["2026-07-{:02d}".format(day) for day in range(13, 20)]  # 7 consecutive days
        schedule = [{"date": day, "shift_hours": 8.0} for day in dates]
        result = evaluate_labor_compliance("us", schedule)
        assert result["compliant"] is False
        assert "consecutive_days_exceeded" in {v["code"] for v in result["violations"]}

    def test_rest_hours_violation(self):
        # 12h shift then next day leaves only 12h rest; us requires >= 10 -> ok.
        ok = evaluate_labor_compliance(
            "us",
            [{"date": "2026-07-13", "shift_hours": 12.0}, {"date": "2026-07-14", "shift_hours": 8.0}],
        )
        assert ok["compliant"] is True
        # 4h shift then next day leaves 20h rest; still fine.
        assert evaluate_labor_compliance(
            "de",
            [{"date": "2026-07-13", "shift_hours": 4.0}, {"date": "2026-07-14", "shift_hours": 8.0}],
        )["compliant"] is True

    def test_compliant_schedule(self):
        result = evaluate_labor_compliance(
            "de",
            [{"date": "2026-07-13", "shift_hours": 8.0}, {"date": "2026-07-14", "shift_hours": 8.0}],
        )
        assert result["compliant"] is True
        assert result["rule"]["max_shift_hours"] == 8.0


class TestRegionalTax:
    def test_germany_standard_vat(self):
        taxed = compute_taxed_amount(100.0, "de", "consultation")
        assert taxed["currency"] == "EUR"
        assert taxed["tax_pct"] == 19.0
        assert taxed["tax_amount"] == 19.0
        assert taxed["total"] == 119.0

    def test_japan_consumption_tax(self):
        taxed = compute_taxed_amount(100.0, "jp", "consultation")
        assert taxed["tax_amount"] == 10.0
        assert taxed["total"] == 110.0

    def test_us_service_type_scoped(self):
        assert compute_taxed_amount(100.0, "us", "project")["total"] == pytest.approx(108.5)
        assert compute_taxed_amount(100.0, "us", "consultation")["total"] == 100.0  # no rule -> untaxed

    def test_assess_booking_dates(self):
        assessment = assess_booking_dates("us", ["2026-07-13", "2026-07-19", "2026-12-25"])
        assert len(assessment["working_days"]) == 3
        assert assessment["working_days"][0]["working"] is True
        assert assessment["working_days"][1]["working"] is False  # Sunday
        assert assessment["working_days"][2]["working"] is False  # Christmas
        assert assessment["labor"]["compliant"] is True


# ===========================================================================
# Main.py wiring
# ===========================================================================


class TestMainWiring:
    def test_scoring_catalog_exposes_rule_engine_and_regional(self):
        response = TestClient(app).get("/meta/scoring-catalog")
        assert response.status_code == 200
        payload = response.json()
        assert payload["rule_engine"]["catalog_version"] == "rule_engine_v1"
        assert payload["rule_engine"]["reserved_date_key"] == "_date"
        assert payload["regional_policy"]["catalog_version"] == "regional_policy_v1"
        assert payload["arrears_payments"]["catalog_version"] == "arrears_payments_v1"
        assert "rate_multipliers" in payload["points_exchange"]

    def test_ecosystem_lists_rule_engine_and_regional(self):
        response = TestClient(app).get("/meta/ecosystem")
        assert response.status_code == 200
        subservices = response.json()["subservices"]
        assert "rule_engine" in subservices
        assert "regional_policy" in subservices

    def test_regional_endpoint_with_region(self):
        response = TestClient(app).get("/meta/regional")
        assert response.status_code == 200
        payload = response.json()
        assert payload["catalog"]["regions"] == ["global", "us", "de", "jp"]

        resolved = TestClient(app).get("/meta/regional?region=de")
        assert resolved.status_code == 200
        body = resolved.json()
        assert body["resolved"]["region"] == "de"
        assert body["resolved"]["tax"]["tax_pct"] == 19.0
        # 2026-10-03 is a German public holiday in the config, but the resolved
        # verdict uses *today*; just assert the shape rather than the value.
        assert "working_day" in body["resolved"]

    def test_root_meta_lists_regional_feature(self):
        response = TestClient(app).get("/meta")
        assert response.status_code == 200
        assert "regional_policy" in response.json()["features"]
        assert "rule_engine" in response.json()["features"]