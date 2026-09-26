"""Points <-> money/currency exchange capability.

Hypothesis
----------
Customers should be able to convert back and forth between *certain types* of
points and money/currencies (e.g. redeem loyalty points for a discount, or top
up an activity points wallet with cash). Not every point type is convertible,
and each convertible pair has its own economics: which directions are allowed
(``redeem`` = points -> money, ``purchase`` = money -> points), the exchange
rate (points per one unit of currency), a fee, minimums, and daily caps.

Exchange rules are config-driven with the same `when`-DSL the other engines
use, so eligibility and rates can depend on the user's state (premium users
get a better loyalty-points rate, brand-new users cannot redeem activity
points yet, ...).

Rates are also *dynamic*: the base `points_per_unit` can be adjusted by
composable factors — value-tier multipliers, LTV-proxy (monetization-readiness)
multipliers, and seasonal campaign windows (shared `when`-DSL date operators).
Multipliers are applied only when a quote evaluation is explicitly driven
(`effective_date` and/or `param_overrides`), so default user quotes are never
silently repriced and existing rate contracts stay intact; the effective rate
and its breakdown are included in the payload for explainability.

Implementation
--------------
- `POINTS_EXCHANGE_RULES` — config table; adding a rule/rate/currency is
  config-only, and a rule enables or disables a direction per point type.
- `POINTS_TIER_MULTIPLIERS` / `POINTS_LTV_MULTIPLIERS` / `POINTS_CAMPAIGN_RULES`
  — dynamic rate-factor config tables applied at quote time.
- Pure core: `select_exchange_rule`, `quote_points_exchange`.
- Async orchestrators: `get_or_create_wallet`, `list_user_wallets`,
  `list_user_point_transactions`, `quote_points_exchange_for_user`,
  `execute_points_exchange_for_user`, `build_points_exchange_admin_report`.
- `build_points_exchange_catalog` feeds `/meta/scoring-catalog`.
"""

from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app import models, rule_engine
from app.schemas.chat import (
    PointsAdminReport,
    PointsExchangeQuote,
    PointsExchangeRates,
    PointsExchangeResult,
    PointsTransactionOut,
    PointsTransactionsReport,
    PointsWalletReport,
)
from app.services.communication_strategy import load_user_strategy_context

PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# ---------------------------------------------------------------------------
# Config table (data-driven exchange rules)
# ---------------------------------------------------------------------------
# A rule binds one (point_type, currency) pair. `purchase_enabled` allows
# money -> points, `redeem_enabled` allows points -> money. Rates are expressed
# as points needed per one unit of currency (`points_per_unit`), i.e. a higher
# value is worse for the customer.

POINTS_EXCHANGE_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "loyalty_points_usd_premium",
        "point_type": "loyalty_points",
        "currency": "USD",
        "priority": "high",
        "when": {"value_tier": "premium"},
        "params": {
            "purchase_enabled": True,
            "redeem_enabled": True,
            "points_per_unit": 90.0,
            "fee_pct": 1.5,
            "min_redeem_points": 100.0,
            "min_purchase_money": 1.0,
            "max_daily_redeem_money": 100.0,
            "max_daily_purchase_money": 250.0,
        },
        "when_hint": "Premium customers exchange loyalty points at a bonus rate (90 pts per $1).",
    },
    {
        "rule_id": "loyalty_points_usd_standard",
        "point_type": "loyalty_points",
        "currency": "USD",
        "priority": "low",
        "when": {},
        "params": {
            "purchase_enabled": True,
            "redeem_enabled": True,
            "points_per_unit": 100.0,
            "fee_pct": 2.0,
            "min_redeem_points": 100.0,
            "min_purchase_money": 1.0,
            "max_daily_redeem_money": 50.0,
            "max_daily_purchase_money": 200.0,
        },
        "when_hint": "Standard loyalty points exchange: 100 pts per $1.",
    },
    {
        "rule_id": "loyalty_points_eur_standard",
        "point_type": "loyalty_points",
        "currency": "EUR",
        "priority": "medium",
        "when": {},
        "params": {
            "purchase_enabled": True,
            "redeem_enabled": True,
            "points_per_unit": 115.0,
            "fee_pct": 2.0,
            "min_redeem_points": 100.0,
            "min_purchase_money": 1.0,
            "max_daily_redeem_money": 45.0,
            "max_daily_purchase_money": 180.0,
        },
        "when_hint": "Loyalty points exchange for EUR markets.",
    },
    {
        "rule_id": "activity_points_usd",
        "point_type": "activity_points",
        "currency": "USD",
        "priority": "medium",
        "when": {"stage": {"ne": "new"}},
        "params": {
            "purchase_enabled": True,
            "redeem_enabled": True,
            "points_per_unit": 150.0,
            "fee_pct": 0.0,
            "min_redeem_points": 500.0,
            "min_purchase_money": 5.0,
            "max_daily_redeem_money": 20.0,
            "max_daily_purchase_money": 100.0,
        },
        "when_hint": "Activity points redeem only after the customer leaves the new stage.",
    },
    {
        "rule_id": "cashback_points_usd",
        "point_type": "cashback_points",
        "currency": "USD",
        "priority": "medium",
        "when": {},
        "params": {
            "purchase_enabled": False,
            "redeem_enabled": True,
            "points_per_unit": 25.0,
            "fee_pct": 0.0,
            "min_redeem_points": 25.0,
            "min_purchase_money": 0.0,
            "max_daily_redeem_money": 30.0,
            "max_daily_purchase_money": 0.0,
        },
        "when_hint": "Cashback points are redeem-only with no fee (25 pts per $1).",
    },
    {
        "rule_id": "referral_points_usd",
        "point_type": "referral_points",
        "currency": "USD",
        "priority": "medium",
        "when": {},
        "params": {
            "purchase_enabled": False,
            "redeem_enabled": True,
            "points_per_unit": 200.0,
            "fee_pct": 0.0,
            "min_redeem_points": 1000.0,
            "min_purchase_money": 0.0,
            "max_daily_redeem_money": 25.0,
            "max_daily_purchase_money": 0.0,
        },
        "when_hint": "Referral points cannot be bought; must be earned by referrals.",
    },
]

# ---------------------------------------------------------------------------
# Config tables (dynamic earn/burn rate multipliers)
# ---------------------------------------------------------------------------
# Rates can be multiplied by three independent, composable factors:
#
# - `POINTS_TIER_MULTIPLIERS`  — value-tier factors (e.g. premium earns more
#   and redeems at a better rate). Tier absent from the table is neutral.
# - `POINTS_LTV_MULTIPLIERS`   — LTV-proxy factors keyed on
#   `monetization_readiness` (first threshold the user reaches wins).
# - `POINTS_CAMPAIGN_RULES`    — seasonal campaigns driven by the shared
#   `when`-DSL with date-window operators on the reserved `_date` field
#   (e.g. `{"_date": {"between_dates": [...]}}`). Active only while the
#   evaluation date is inside the window; a campaign can be simulated in
#   tests/ops review by passing `effective_date` to `quote_points_exchange`.
#
# Direction semantics:
# - `redeem_multiplier`  > 1  -> better customer rate (fewer points per $1).
# - `purchase_multiplier` > 1 -> more points earned per $1.
# Combined factor is tier * ltv * campaign; the product stays 1.0 (neutral)
# when none apply.
POINTS_TIER_MULTIPLIERS: list[dict[str, Any]] = [
    {
        "value_tier": "premium",
        "purchase_multiplier": 1.2,
        "redeem_multiplier": 1.1,
        "when_hint": "Premium tier earns 20% more points and redeems at a 10% better rate.",
    },
]

POINTS_LTV_MULTIPLIERS: list[dict[str, Any]] = [
    {
        "min_monetization_readiness": 85.0,
        "purchase_multiplier": 1.15,
        "redeem_multiplier": 1.05,
        "when_hint": "High monetization readiness (LTV proxy) boosts earn/burn rates.",
    },
]

POINTS_CAMPAIGN_RULES: list[dict[str, Any]] = [
    {
        "campaign_id": "summer_earn_boost_2026",
        "label": "Summer Earn Boost 2026",
        "priority": "high",
        "when": {"_date": {"between_dates": ["2026-06-01", "2026-08-31"]}},
        "params": {"purchase_multiplier": 1.15, "redeem_multiplier": 1.05},
        "when_hint": "Seasonal window 2026-06-01 .. 2026-08-31: earn 15% extra points and redeem at a 5% better rate.",
    },
    {
        "campaign_id": "spring_purchase_boost_2026",
        "label": "Spring Purchase Boost 2026",
        "priority": "medium",
        "when": {"_date": {"between_dates": ["2026-03-01", "2026-04-30"]}},
        "params": {"purchase_multiplier": 1.08, "redeem_multiplier": 1.0},
        "when_hint": "Seasonal window 2026-03-01 .. 2026-04-30: earn 8% extra points.",
    },
]

# ---------------------------------------------------------------------------
# Condition DSL (same shape as the loyalty / communication engines)
#
# Matching is delegated to the shared `app/rule_engine.py` core: behavior for
# existing rules is identical, and the engine adds date-window operators and
# any/all/not combinators for richer rules (e.g. seasonal campaign windows).
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return value


_NUMERIC_OPS: dict[str, Any] = {
    "gte": operator.ge,
    "gt": operator.gt,
    "lte": operator.le,
    "lt": operator.lt,
    "eq": operator.eq,
    "ne": operator.ne,
}


def _matches_rule(rule_value: Any, context_value: Any) -> bool:
    return rule_engine.matches_field(rule_value, context_value)


def evaluate_when(when: dict[str, object], context: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    return rule_engine.evaluate_when(when, context)


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

EXCHANGE_DIRECTIONS = ("redeem", "purchase")
_KIND_BY_DIRECTION = {"redeem": "redeem_points", "purchase": "purchase_points"}


def _rule_params(rule: dict[str, Any]) -> dict[str, Any]:
    params = rule["params"]
    return {
        "rule_id": str(rule["rule_id"]),
        "point_type": str(rule["point_type"]),
        "currency": str(rule["currency"]),
        "priority": str(rule.get("priority", "medium")),
        "purchase_enabled": bool(params.get("purchase_enabled", False)),
        "redeem_enabled": bool(params.get("redeem_enabled", False)),
        "points_per_unit": round(float(params.get("points_per_unit", 0.0) or 0.0), 2),
        "fee_pct": round(float(params.get("fee_pct", 0.0) or 0.0), 2),
        "min_redeem_points": round(float(params.get("min_redeem_points", 0.0) or 0.0), 2),
        "min_purchase_money": round(float(params.get("min_purchase_money", 0.0) or 0.0), 2),
        "max_daily_redeem_money": round(float(params.get("max_daily_redeem_money", 0.0) or 0.0), 2),
        "max_daily_purchase_money": round(float(params.get("max_daily_purchase_money", 0.0) or 0.0), 2),
        "when_hint": str(rule.get("when_hint", "")),
    }


def _match_tier_multipliers(context: dict[str, Any]) -> dict[str, Any]:
    """First tier rule whose `value_tier` equals the context tier, else neutral."""
    value_tier = context.get("value_tier")
    for rule in POINTS_TIER_MULTIPLIERS:
        if rule.get("value_tier") == value_tier:
            return rule
    return {"value_tier": value_tier, "purchase_multiplier": 1.0, "redeem_multiplier": 1.0, "when_hint": ""}


def _match_ltv_multipliers(context: dict[str, Any]) -> dict[str, Any]:
    """First LTV rule (highest threshold) whose readiness threshold is reached, else neutral."""
    readiness = context.get("monetization_readiness")
    if readiness is None:
        return {"min_monetization_readiness": 0.0, "purchase_multiplier": 1.0, "redeem_multiplier": 1.0, "when_hint": ""}
    ranked = sorted(
        POINTS_LTV_MULTIPLIERS,
        key=lambda rule: float(rule.get("min_monetization_readiness", 0.0) or 0.0),
        reverse=True,
    )
    for rule in ranked:
        if float(readiness or 0.0) >= float(rule.get("min_monetization_readiness", 0.0) or 0.0):
            return rule
    return {"min_monetization_readiness": 0.0, "purchase_multiplier": 1.0, "redeem_multiplier": 1.0, "when_hint": ""}


def select_active_campaigns(context: dict[str, Any], effective_date: Any = None) -> list[dict[str, Any]]:
    """Every campaign whose `when` (date-window DSL, reserved `_date` key) holds.

    Without an explicit `effective_date` the evaluation date defaults to the
    current UTC date, so fixed-window campaigns activate only while the window
    contains today. Ops/review can simulate a window with `effective_date`.
    """
    ranked = sorted(
        POINTS_CAMPAIGN_RULES,
        key=lambda rule: (
            PRIORITY_RANK.get(str(rule.get("priority", "medium")), 1),
            POINTS_CAMPAIGN_RULES.index(rule),
        ),
    )
    active: list[dict[str, Any]] = []
    for rule in ranked:
        ok, fields = rule_engine.evaluate_when(dict(rule["when"]), dict(context), effective_date=effective_date)
        if ok:
            active.append({**rule, "matched_conditions": fields})
    return active


def _select_active_campaign(context: dict[str, Any], effective_date: Any = None) -> Optional[dict[str, Any]]:
    active = select_active_campaigns(context, effective_date=effective_date)
    return active[0] if active else None


def _combined_points_multipliers(
    context: dict[str, Any],
    *,
    effective_date: Any = None,
    campaign: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compose the tier * LTV * campaign rate factors (1.0 when nothing applies)."""
    tier = _match_tier_multipliers(context)
    ltv = _match_ltv_multipliers(context)
    if campaign is None:
        campaign = _select_active_campaign(context, effective_date=effective_date)
    campaign_params = (campaign or {}).get("params") or {}
    camp_purchase = float(campaign_params.get("purchase_multiplier", 1.0) or 1.0)
    camp_redeem = float(campaign_params.get("redeem_multiplier", 1.0) or 1.0)
    purchase = round(
        float(tier.get("purchase_multiplier", 1.0) or 1.0)
        * float(ltv.get("purchase_multiplier", 1.0) or 1.0)
        * camp_purchase,
        4,
    )
    redeem = round(
        float(tier.get("redeem_multiplier", 1.0) or 1.0)
        * float(ltv.get("redeem_multiplier", 1.0) or 1.0)
        * camp_redeem,
        4,
    )
    return {
        "tier": tier,
        "ltv": ltv,
        "campaign": campaign,
        "purchase_multiplier": purchase,
        "redeem_multiplier": redeem,
    }


def select_exchange_rule(
    point_type: str,
    direction: str,
    currency: str,
    context: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """First eligible rule for (point_type, currency, direction), by priority."""
    if direction not in EXCHANGE_DIRECTIONS:
        return None, {}
    ranked = sorted(
        POINTS_EXCHANGE_RULES,
        key=lambda rule: (
            PRIORITY_RANK.get(str(rule.get("priority", "medium")), 1),
            POINTS_EXCHANGE_RULES.index(rule),
        ),
    )
    for rule in ranked:
        if rule["point_type"] != point_type or rule["currency"] != currency:
            continue
        enabled = rule["params"].get(f"{direction}_enabled", False)
        if not enabled:
            continue
        ok, fields = evaluate_when(dict(rule["when"]), context)
        if ok:
            return rule, fields
    return None, {}


def quote_points_exchange(
    point_type: str,
    direction: str,
    amount: float,
    currency: str,
    context: dict[str, Any],
    *,
    daily_used_money: float = 0.0,
    effective_date: Any = None,
    param_overrides: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Quote a conversion (no write). `amount` is points for redeem, money for purchase.

    Dynamic rate multipliers (value-tier * LTV/readiness * seasonal campaign)
    are applied only when an evaluation is explicitly driven — i.e. either
    `effective_date` (simulate/review a campaign window or seasonal pricing) or
    `param_overrides` (enterprise `=formula` points_per_unit, resolved by the
    shared rule engine against the context plus the resolved multipliers) is
    supplied. Without either, the quote uses the base rule rate exactly as
    before, so default user behavior is never silently repriced. The published
    effective rate and its breakdown are included in the payload whenever a
    dynamic evaluation runs.
    """
    rule, fields = select_exchange_rule(point_type, direction, currency, context)
    if rule is None:
        return {
            "eligible": False,
            "reason": f"No {direction} exchange rule for {point_type} in {currency} for this user.",
            "rule": None,
        }
    terms = _rule_params(rule)
    amount = float(amount or 0.0)
    if amount <= 0:
        return {"eligible": False, "reason": "Amount must be positive.", "rule": terms}
    daily_used = float(daily_used_money or 0.0)

    # --- dynamic rate multipliers (only when the evaluation is driven) ------
    driven = effective_date is not None or bool(param_overrides)
    base_ppu = terms["points_per_unit"]
    effective_ppu = base_ppu
    rate_multipliers: dict[str, Any] = {}
    applied_overrides: dict[str, Any] = {}
    if driven:
        multipliers = _combined_points_multipliers(context, effective_date=effective_date)
        direction_factor = (
            multipliers["redeem_multiplier"] if direction == "redeem" else multipliers["purchase_multiplier"]
        )
        if direction == "redeem":
            effective_ppu = base_ppu / direction_factor if direction_factor > 0 else base_ppu
        else:
            effective_ppu = base_ppu * direction_factor
        effective_ppu = round(float(effective_ppu), 4)
        campaign = multipliers["campaign"]
        rate_multipliers = {
            "purchase_multiplier": multipliers["purchase_multiplier"],
            "redeem_multiplier": multipliers["redeem_multiplier"],
            "tier": multipliers["tier"].get("value_tier"),
            "ltv_min_readiness": multipliers["ltv"].get("min_monetization_readiness"),
            "campaign_id": (campaign or {}).get("campaign_id"),
            "campaign_label": (campaign or {}).get("label"),
        }
        if param_overrides:
            scope = dict(context)
            scope["purchase_multiplier"] = multipliers["purchase_multiplier"]
            scope["redeem_multiplier"] = multipliers["redeem_multiplier"]
            scope["campaign_multiplier"] = multipliers["purchase_multiplier"]
            applied_overrides = rule_engine.resolve_params(param_overrides, scope)
            if applied_overrides.get("points_per_unit") is not None:
                try:
                    effective_ppu = round(float(applied_overrides["points_per_unit"]), 4)
                except (TypeError, ValueError):
                    effective_ppu = base_ppu

    if direction == "redeem":
        points_in = amount
        if points_in < terms["min_redeem_points"]:
            return {
                "eligible": False,
                "reason": f"Minimum redeem is {terms['min_redeem_points']:.0f} points.",
                "rule": terms,
            }
        gross_money = points_in / effective_ppu if effective_ppu > 0 else 0.0
        fee = gross_money * terms["fee_pct"] / 100.0
        net_money = gross_money - fee
        daily_max = terms["max_daily_redeem_money"]
        remaining = max(0.0, daily_max - daily_used)
        exceeded = daily_max > 0 and gross_money > remaining
        payload = {
            "eligible": True,
            "exceeds_daily_cap": exceeded,
            "reason": "Redeem quote computed." if not exceeded else "Daily redeem cap would be exceeded.",
            "rule": terms,
            "direction": "redeem",
            "input_amount": round(points_in, 2),
            "input_unit": "points",
            "output_amount": round(net_money, 2),
            "output_unit": terms["currency"],
            "gross_output": round(gross_money, 2),
            "fee": round(fee, 2),
            "points_per_unit": terms["points_per_unit"],
            "daily_used_money": round(daily_used, 2),
            "daily_max_money": daily_max,
            "matched_conditions": fields,
        }
        if driven:
            payload["points_per_unit_effective"] = effective_ppu
            payload["rate_multipliers"] = rate_multipliers
            payload["applied_param_overrides"] = applied_overrides
        return payload
    # purchase: money -> points
    money_in = amount
    if money_in < terms["min_purchase_money"]:
        return {
            "eligible": False,
            "reason": f"Minimum purchase is {terms['min_purchase_money']:.2f} {terms['currency']}.",
            "rule": terms,
        }
    gross_points = money_in * effective_ppu
    fee = gross_points * terms["fee_pct"] / 100.0
    net_points = gross_points - fee
    daily_max = terms["max_daily_purchase_money"]
    remaining = max(0.0, daily_max - daily_used)
    exceeded = daily_max > 0 and money_in > remaining
    payload = {
        "eligible": True,
        "exceeds_daily_cap": exceeded,
        "reason": "Purchase quote computed." if not exceeded else "Daily purchase cap would be exceeded.",
        "rule": terms,
        "direction": "purchase",
        "input_amount": round(money_in, 2),
        "input_unit": terms["currency"],
        "output_amount": round(net_points, 2),
        "output_unit": "points",
        "gross_output": round(gross_points, 2),
        "fee": round(fee, 2),
        "points_per_unit": terms["points_per_unit"],
        "daily_used_money": round(daily_used, 2),
        "daily_max_money": daily_max,
        "matched_conditions": fields,
    }
    if driven:
        payload["points_per_unit_effective"] = effective_ppu
        payload["rate_multipliers"] = rate_multipliers
        payload["applied_param_overrides"] = applied_overrides
    return payload


def list_convertible_point_types() -> dict[str, dict[str, Any]]:
    """Aggregate rule params per point type for catalog/wallet reporting."""
    merged: dict[str, dict[str, Any]] = {}
    for rule in POINTS_EXCHANGE_RULES:
        terms = _rule_params(rule)
        point_type = terms["point_type"]
        entry = merged.setdefault(
            point_type,
            {
                "point_type": point_type,
                "label": point_type.replace("_", " ").title(),
                "currencies": [],
                "purchase_enabled": False,
                "redeem_enabled": False,
            },
        )
        entry["currencies"].append(terms["currency"])
        entry["purchase_enabled"] = entry["purchase_enabled"] or terms["purchase_enabled"]
        entry["redeem_enabled"] = entry["redeem_enabled"] or terms["redeem_enabled"]
    return merged


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _wallet_dict(row: Any) -> dict[str, Any]:
    return {
        "point_type": str(getattr(row, "point_type", "") or ""),
        "balance": round(float(getattr(row, "balance", 0.0) or 0.0), 2),
        "updated_at": getattr(row, "updated_at", None),
    }


def _txn_dict(row: Any, username: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": int(getattr(row, "id", 0) or 0),
        "user_id": int(getattr(row, "user_id", 0) or 0),
        "username": username or "",
        "point_type": str(getattr(row, "point_type", "") or ""),
        "kind": str(getattr(row, "kind", "") or ""),
        "points_delta": round(float(getattr(row, "points_delta", 0.0) or 0.0), 2),
        "currency": str(getattr(row, "currency", "USD") or "USD"),
        "currency_amount": round(float(getattr(row, "currency_amount", 0.0) or 0.0), 2),
        "rate": round(float(getattr(row, "rate", 0.0) or 0.0), 2),
        "fee": round(float(getattr(row, "fee", 0.0) or 0.0), 2),
        "reference": str(getattr(row, "reference", "") or ""),
        "created_at": getattr(row, "created_at", None),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_or_create_wallet(db: AsyncSession, user_id: int, point_type: str):
    result = await db.execute(
        select(models.PointsWallet).where(
            models.PointsWallet.user_id == user_id,
            models.PointsWallet.point_type == point_type,
        )
    )
    wallet = result.scalars().first()
    if wallet is None:
        wallet = models.PointsWallet(user_id=int(user_id), point_type=str(point_type), balance=0.0)
        db.add(wallet)
        await db.flush()
    return wallet


async def compute_daily_used_money(
    db: AsyncSession, user_id: int, point_type: str, currency: str, direction: str
) -> float:
    """Sum of today's exchanged money (gross) for the same pair/direction."""
    kind = _KIND_BY_DIRECTION.get(direction)
    if kind is None:
        return 0.0
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.coalesce(func.sum(models.PointsTransaction.currency_amount), 0.0)).where(
            models.PointsTransaction.user_id == user_id,
            models.PointsTransaction.point_type == point_type,
            models.PointsTransaction.currency == currency,
            models.PointsTransaction.kind == kind,
            models.PointsTransaction.created_at >= start,
        )
    )
    value = result.scalar()
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def load_user_points_context(db: AsyncSession, user_id: int, window_days: int = 30) -> dict[str, Any]:
    context, _mood = await load_user_strategy_context(db, user_id, window_days)
    return context


# ---------------------------------------------------------------------------
# Async orchestrators
# ---------------------------------------------------------------------------


async def list_user_wallets(db: AsyncSession, user_id: int) -> PointsWalletReport:
    result = await db.execute(
        select(models.PointsWallet)
        .where(models.PointsWallet.user_id == user_id)
        .order_by(models.PointsWallet.point_type)
    )
    wallets = {
        getattr(row, "point_type", ""): _wallet_dict(row) for row in result.scalars().all()
    }
    entries = []
    for point_type in sorted(list_convertible_point_types()):
        entry = wallets.get(point_type, {"point_type": point_type, "balance": 0.0, "updated_at": None})
        categories = list_convertible_point_types()[point_type]
        entry = dict(entry)
        entry["redeem_enabled"] = categories["redeem_enabled"]
        entry["purchase_enabled"] = categories["purchase_enabled"]
        entries.append(entry)
    total_points = sum(float(e["balance"]) for e in entries)
    redeemable = sum(float(e["balance"]) for e in entries if e.get("redeem_enabled"))
    return PointsWalletReport(
        generated_at=_now(),
        user_id=user_id,
        total_balance=round(total_points, 2),
        redeemable_balance=round(redeemable, 2),
        convertible_types=len(entries),
        wallets=entries,
    )


async def list_user_point_transactions(
    db: AsyncSession, user_id: int, *, limit: int = 50
) -> PointsTransactionsReport:
    result = await db.execute(
        select(models.PointsTransaction)
        .where(models.PointsTransaction.user_id == user_id)
        .order_by(desc(models.PointsTransaction.created_at))
        .limit(max(1, int(limit)))
    )
    rows = list(result.scalars().all())
    return PointsTransactionsReport(
        generated_at=_now(),
        user_id=user_id,
        total=len(rows),
        transactions=[PointsTransactionOut(**_txn_dict(row)) for row in rows],
    )


async def quote_points_exchange_for_user(
    db: AsyncSession,
    user_id: int,
    point_type: str,
    direction: str,
    amount: float,
    currency: str = "USD",
    *,
    window_days: int = 30,
) -> PointsExchangeQuote:
    context = await load_user_points_context(db, user_id, window_days)
    daily_used = await compute_daily_used_money(db, user_id, point_type, currency, direction)
    quote = quote_points_exchange(
        point_type, direction, amount, currency, context, daily_used_money=daily_used
    )
    return PointsExchangeQuote(
        generated_at=_now(),
        user_id=user_id,
        point_type=point_type,
        direction=direction,
        currency=currency,
        eligible=bool(quote.get("eligible", False)),
        reason=str(quote.get("reason", "")),
        rule=quote.get("rule"),
        input_amount=float(quote.get("input_amount", 0.0) or 0.0),
        input_unit=str(quote.get("input_unit", "") or ""),
        output_amount=float(quote.get("output_amount", 0.0) or 0.0),
        output_unit=str(quote.get("output_unit", "") or ""),
        fee=float(quote.get("fee", 0.0) or 0.0),
        points_per_unit=float(quote.get("points_per_unit", 0.0) or 0.0),
        daily_used_money=float(quote.get("daily_used_money", 0.0) or 0.0),
        daily_max_money=float(quote.get("daily_max_money", 0.0) or 0.0),
        exceeds_daily_cap=bool(quote.get("exceeds_daily_cap", False)),
        matched_conditions=quote.get("matched_conditions", {}),
    )


async def execute_points_exchange_for_user(
    db: AsyncSession,
    user_id: int,
    point_type: str,
    direction: str,
    amount: float,
    currency: str = "USD",
    *,
    reference: str = "",
    window_days: int = 30,
) -> Optional[PointsExchangeResult]:
    context = await load_user_points_context(db, user_id, window_days)
    daily_used = await compute_daily_used_money(db, user_id, point_type, currency, direction)
    quote = quote_points_exchange(
        point_type, direction, amount, currency, context, daily_used_money=daily_used
    )
    if not quote.get("eligible", False):
        raise ValueError(f"Exchange not allowed: {quote['reason']}")
    if quote.get("exceeds_daily_cap", False):
        raise ValueError("Daily exchange cap would be exceeded.")

    wallet = await get_or_create_wallet(db, user_id, point_type)
    now = _now()
    if direction == "redeem":
        points_required = float(amount)
        if float(getattr(wallet, "balance", 0.0) or 0.0) < points_required:
            raise ValueError(
                f"Insufficient {point_type} balance: have "
                f"{float(getattr(wallet, 'balance', 0.0) or 0.0):.2f}, need {points_required:.2f}."
            )
        net_money = float(quote["output_amount"])
        points_delta = -points_required
        currency_amount = float(quote.get("gross_output", net_money))
        fee = float(quote.get("fee", 0.0) or 0.0)
        wallet.balance = float(getattr(wallet, "balance", 0.0) or 0.0) - points_required
        kind = "redeem_points"
    else:  # purchase
        net_points = float(quote["output_amount"])
        points_delta = net_points
        currency_amount = float(amount)
        fee = float(quote.get("fee", 0.0) or 0.0)
        wallet.balance = float(getattr(wallet, "balance", 0.0) or 0.0) + net_points
        kind = "purchase_points"

    wallet.updated_at = now
    txn = models.PointsTransaction(
        user_id=int(user_id),
        point_type=str(point_type),
        kind=kind,
        points_delta=round(points_delta, 2),
        currency=str(currency),
        currency_amount=round(currency_amount, 2),
        rate=float(quote.get("points_per_unit", 0.0) or 0.0),
        fee=round(fee, 2),
        reference=str(reference or ""),
        created_at=now,
        updated_at=now,
    )
    db.add(txn)
    await db.commit()
    await db.refresh(wallet)
    await db.refresh(txn)
    return PointsExchangeResult(
        generated_at=now,
        user_id=user_id,
        point_type=point_type,
        direction=direction,
        currency=currency,
        transaction_id=int(getattr(txn, "id", 0) or 0),
        kind=kind,
        points_delta=round(points_delta, 2),
        currency_amount=round(currency_amount, 2),
        gross_output=float(quote.get("gross_output", 0.0) or 0.0),
        fee=round(fee, 2),
        rate=float(quote.get("points_per_unit", 0.0) or 0.0),
        new_balance=round(float(getattr(wallet, "balance", 0.0) or 0.0), 2),
        reference=str(reference or ""),
    )


async def build_points_exchange_admin_report(
    db: AsyncSession, *, window_days: int = 30, limit: int = 50
) -> PointsAdminReport:
    result = await db.execute(
        select(models.PointsTransaction).order_by(desc(models.PointsTransaction.created_at)).limit(max(1, int(limit)))
    )
    rows = list(result.scalars().all())
    names_result = await db.execute(select(models.User.id, models.User.username))
    names = {int(row[0]): str(row[1]) for row in names_result.all()}
    transactions = [_txn_dict(row, username=names.get(getattr(row, "user_id", None))) for row in rows]
    total_redeemed = sum(float(txn["points_delta"]) for txn in transactions if txn["kind"] == "redeem_points")
    total_purchased = sum(float(txn["points_delta"]) for txn in transactions if txn["kind"] == "purchase_points")
    total_money = sum(float(txn["currency_amount"]) for txn in transactions)
    by_kind: dict[str, int] = {}
    by_point_type: dict[str, int] = {}
    for txn in transactions:
        by_kind[txn["kind"]] = by_kind.get(txn["kind"], 0) + 1
        by_point_type[txn["point_type"]] = by_point_type.get(txn["point_type"], 0) + 1
    top_user: Optional[str] = None
    top_user_money = 0.0
    by_user: dict[int, float] = {}
    for txn in transactions:
        by_user[txn["user_id"]] = by_user.get(txn["user_id"], 0.0) + float(txn["currency_amount"])
    for user_id, total in by_user.items():
        if total > top_user_money:
            top_user_money = total
            top_user = names.get(user_id, f"user-{user_id}")
    return PointsAdminReport(
        generated_at=_now(),
        window_days=window_days,
        limit=max(1, int(limit)),
        total_transactions=len(transactions),
        total_redeemed_points=round(total_redeemed, 2),
        total_purchased_points=round(total_purchased, 2),
        total_money_moved=round(total_money, 2),
        top_user=top_user,
        by_kind=by_kind,
        by_point_type=by_point_type,
        transactions=[PointsTransactionOut(**txn) for txn in transactions],
    )


def build_points_exchange_catalog() -> dict[str, Any]:
    """Introspection payload for `/meta/scoring-catalog`."""
    point_types = list_convertible_point_types()
    currencies = sorted({rule["currency"] for rule in POINTS_EXCHANGE_RULES})
    return {
        "catalog_version": "points_exchange_v1",
        "directions": list(EXCHANGE_DIRECTIONS),
        "currencies": currencies,
        "point_types": point_types,
        "rules": [
            {
                "rule_id": rule["rule_id"],
                "point_type": rule["point_type"],
                "currency": rule["currency"],
                "priority": rule.get("priority", "medium"),
                "when": _json_safe(rule["when"]),
                "params": dict(rule["params"]),
                "when_hint": rule.get("when_hint", ""),
            }
            for rule in POINTS_EXCHANGE_RULES
        ],
        "rate_multipliers": {
            "note": "Effective rate = base points_per_unit adjusted by composed tier * LTV * campaign factors (redeem: divide; purchase: multiply).",
            "tiers": [_json_safe(rule) for rule in POINTS_TIER_MULTIPLIERS],
            "ltv": [_json_safe(rule) for rule in POINTS_LTV_MULTIPLIERS],
            "campaigns": [
                {
                    "campaign_id": rule["campaign_id"],
                    "label": rule.get("label", rule["campaign_id"]),
                    "priority": rule.get("priority", "medium"),
                    "when": _json_safe(rule["when"]),
                    "params": dict(rule["params"]),
                    "when_hint": rule.get("when_hint", ""),
                }
                for rule in POINTS_CAMPAIGN_RULES
            ],
        },
        "endpoints": {
            "catalog": "/chat/points/exchange/rates",
            "wallet": "/chat/points/wallet",
            "transactions": "/chat/points/transactions",
            "quote": "/chat/points/exchange/quote",
            "execute": "/chat/points/exchange",
            "admin_report": "/chat/admin/points/exchange",
        },
    }


def build_points_exchange_rates() -> PointsExchangeRates:
    """Public-facing rate card for the wallet/exchange surface."""
    point_types = list_convertible_point_types()
    rules = []
    for rule in POINTS_EXCHANGE_RULES:
        terms = _rule_params(rule)
        rules.append(
            {
                "rule_id": terms["rule_id"],
                "point_type": terms["point_type"],
                "currency": terms["currency"],
                "purchase_enabled": terms["purchase_enabled"],
                "redeem_enabled": terms["redeem_enabled"],
                "points_per_unit": terms["points_per_unit"],
                "fee_pct": terms["fee_pct"],
                "min_redeem_points": terms["min_redeem_points"],
                "min_purchase_money": terms["min_purchase_money"],
                "max_daily_redeem_money": terms["max_daily_redeem_money"],
                "max_daily_purchase_money": terms["max_daily_purchase_money"],
                "when_hint": terms["when_hint"],
            }
        )
    currencies = sorted({rule["currency"] for rule in POINTS_EXCHANGE_RULES})
    return PointsExchangeRates(
        generated_at=_now(),
        catalog_version="points_exchange_v1",
        currencies=currencies,
        point_types=list(point_types.values()),
        rules=rules,
    )