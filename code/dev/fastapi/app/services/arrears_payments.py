"""Pay-in-arrears capability with policy-selected interest.

Hypothesis
----------
Users should be allowed to defer a payment (pay in arrears) instead of paying
up front, and the *interest terms* attached to that deferral are not flat —
they are chosen by a data-driven policy engine so different customers, states,
and service types get different policies (premium users get the best terms,
high-risk users get a short, heavily-capped deferral, large project bookings
get installment-like terms, ...).

When an arrears payment is opened the *matching* policy's terms are snapshotted
onto the row, so later catalog changes never rewrite already-open agreements.
Interest accrues only after the grace period, is computed on demand
(simple or daily-compounding), and is capped as a percentage of principal.

Policies may also carry optional late-fee terms (a flat `late_fee_amount`
and/or `late_fee_pct` of principal) snapshotted at open time and charged at
settlement when the entry is past due — unless fees were waived. Both interest
and fee waivers are governed by the operator's administrative policy score
(`ARREARS_WAIVER_POLICY` + `evaluate_waiver_approval`); legacy calls without a
policy score keep the previous un-gated behavior.

Implementation
--------------
- `ARREARS_INTEREST_POLICIES` — config table of `when`-DSL policies; adding a
  policy is config-only.
- `ARREARS_WAIVER_POLICY` — config table gating interest/fee waivers by policy
  tier rank + access score.
- Pure core: `select_arrears_policy`, `compute_arrears_interest`,
  `compute_late_fee`, `quote_arrears_payment`, `evaluate_waiver_approval`.
- Async orchestrators: `open_arrears_payment`, `list_user_arrears`,
  `build_arrears_admin_report`, `settle_arrears_entry`,
  `waive_arrears_interest`, `waive_arrears_fees`.
- `build_arrears_catalog` feeds `/meta/scoring-catalog`.
"""

from __future__ import annotations

import operator
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app import models, rule_engine
from app.schemas.chat import (
    ArrearsAdminReport,
    ArrearsEntryOut,
    ArrearsListReport,
    ArrearsQuote,
    ArrearsSettleResult,
    ArrearsWaiveResult,
)
from app.services import policy_scoring
from app.services.communication_strategy import load_user_strategy_context

# ---------------------------------------------------------------------------
# Config table (data-driven waiver authorization)
# ---------------------------------------------------------------------------
# Administrative waiver workflows (interest / late fees) are gated by the
# operator's `PolicyScoreSnapshot` from `app/services/policy_scoring.py`:
# a minimum `policy_tier` rank and an `access_score` threshold per waiver type.
# Passing no policy score keeps the legacy un-gated behavior (used by existing
# integrations/tests) — the gate is enforced only when a snapshot is supplied.
ARREARS_WAIVER_POLICY: dict[str, dict[str, Any]] = {
    "interest": {
        "required_policy_tier": "customer-premium",
        "min_access_score": 0.0,
        "note": "Interest waivers require at least a customer-premium administrative policy tier.",
    },
    "fees": {
        "required_policy_tier": "customer-premium",
        "min_access_score": 70.0,
        "note": "Fee waivers are high-impact and require customer-premium tier plus a strong access score.",
    },
}

PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# ---------------------------------------------------------------------------
# Config table (data-driven interest policies)
# ---------------------------------------------------------------------------
# Optional late-fee terms (`late_fee_amount` flat and/or `late_fee_pct` of
# principal) may be attached to a policy's params and are snapshotted onto the
# row at open time, exactly like the interest terms.

ARREARS_INTEREST_POLICIES: list[dict[str, Any]] = [
    {
        "policy_id": "new_customer_growth_deferral",
        "label": "New customer growth deferral",
        "priority": "medium",
        "when": {"stage": "new"},
        "params": {
            "annual_rate": 9.0,
            "grace_days": 30,
            "compounding": "simple",
            "interest_cap_pct": 15.0,
            "min_principal": 10.0,
            "max_principal": 1000.0,
            "max_defer_days": 90,
        },
        "when_hint": "New customers get a light 9% annual rate and a 30-day interest-free grace period to encourage first use.",
    },
    {
        "policy_id": "vip_premium_deferral",
        "label": "VIP / premium preferential deferral",
        "priority": "high",
        "when": {"value_tier": "premium"},
        "params": {
            "annual_rate": 6.0,
            "grace_days": 45,
            "compounding": "simple",
            "interest_cap_pct": 10.0,
            "min_principal": 50.0,
            "max_principal": 10000.0,
            "max_defer_days": 180,
        },
        "when_hint": "Premium customers receive the most favorable terms.",
    },
    {
        "policy_id": "high_risk_secured_deferral",
        "label": "High-risk secured deferral",
        "priority": "high",
        "when": {"churn_risk": "high", "risk_level": {"ne": "low"}},
        "params": {
            "annual_rate": 24.0,
            "grace_days": 7,
            "compounding": "daily",
            "interest_cap_pct": 30.0,
            "min_principal": 25.0,
            "max_principal": 2000.0,
            "max_defer_days": 45,
            "late_fee_amount": 10.0,
        },
        "when_hint": "Customers flagged at churn risk are offered a short, visibly-capped deferral.",
    },
    {
        "policy_id": "large_project_installment",
        "label": "Large project installment deferral",
        "priority": "medium",
        "when": {"service_type": "project", "principal": {"gte": 500.0}},
        "params": {
            "annual_rate": 8.0,
            "grace_days": 20,
            "compounding": "simple",
            "interest_cap_pct": 12.0,
            "min_principal": 500.0,
            "max_principal": 50000.0,
            "max_defer_days": 120,
            "late_fee_pct": 1.0,
        },
        "when_hint": "Large project bookings may be deferred with favorable terms.",
    },
    {
        "policy_id": "trust_repair_deferral",
        "label": "Trust-repair deferral",
        "priority": "medium",
        "when": {"journey_family": "trust"},
        "params": {
            "annual_rate": 5.0,
            "grace_days": 30,
            "compounding": "simple",
            "interest_cap_pct": 8.0,
            "min_principal": 10.0,
            "max_principal": 3000.0,
            "max_defer_days": 90,
        },
        "when_hint": "After trust-repair recovery signals, generous terms rebuild confidence.",
    },
    {
        "policy_id": "standard_deferral",
        "label": "Standard deferral",
        "priority": "low",
        "when": {"stage": "engaged"},
        "params": {
            "annual_rate": 12.0,
            "grace_days": 14,
            "compounding": "simple",
            "interest_cap_pct": 25.0,
            "min_principal": 10.0,
            "max_principal": 5000.0,
            "max_defer_days": 60,
        },
        "when_hint": "Engaged standard users get the default terms.",
    },
    {
        "policy_id": "universal_deferral",
        "label": "Universal deferral",
        "priority": "low",
        "when": {},
        "params": {
            "annual_rate": 15.0,
            "grace_days": 10,
            "compounding": "simple",
            "interest_cap_pct": 30.0,
            "min_principal": 10.0,
            "max_principal": 5000.0,
            "max_defer_days": 45,
        },
        "when_hint": "Baseline terms available to every other eligible user (lapsed, dormant, at-risk stages).",
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
    """Evaluate every condition in `when` against `context` (AND semantics).

    Unknown fields fail the whole rule (fail-safe) so catalog typos surface in
    self-check tests instead of silently never matching.
    """
    return rule_engine.evaluate_when(when, context)


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------


def select_arrears_policy(context: dict[str, Any]) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """First policy whose `when` conditions hold, by (priority, catalog order)."""
    ranked = sorted(
        ARREARS_INTEREST_POLICIES,
        key=lambda rule: (
            PRIORITY_RANK.get(str(rule.get("priority", "medium")), 1),
            ARREARS_INTEREST_POLICIES.index(rule),
        ),
    )
    for rule in ranked:
        ok, fields = evaluate_when(dict(rule["when"]), context)
        if ok:
            return rule, fields
    return None, {}


def compute_arrears_interest(
    principal: float,
    annual_rate: float,
    days_elapsed: int,
    *,
    grace_days: int = 0,
    compounding: str = "simple",
    cap_pct: float = 100.0,
) -> dict[str, Any]:
    """Accrued interest for `principal` holding `days_elapsed` days.

    Interest starts only after the grace period elapses and is capped at
    `cap_pct`% of the principal. `compounding` is ``"simple"`` or ``"daily"``.
    """
    principal = float(principal or 0.0)
    annual_rate = float(annual_rate or 0.0)
    days_elapsed = max(0, int(days_elapsed or 0))
    grace_days = max(0, int(grace_days or 0))
    cap_pct = float(cap_pct or 0.0)
    days_after_grace = max(0, days_elapsed - grace_days)
    if principal <= 0 or annual_rate <= 0 or days_after_grace <= 0:
        interest = 0.0
    elif compounding == "daily":
        daily_rate = annual_rate / 100.0 / 365.0
        interest = principal * ((1.0 + daily_rate) ** days_after_grace - 1.0)
    else:  # simple
        interest = principal * (annual_rate / 100.0) * days_after_grace / 365.0
    cap = principal * cap_pct / 100.0
    interest = min(interest, cap)
    total_due = principal + interest
    return {
        "principal": round(principal, 2),
        "annual_rate": round(annual_rate, 2),
        "grace_days": grace_days,
        "compounding": compounding,
        "days_elapsed": days_elapsed,
        "days_after_grace": days_after_grace,
        "interest": round(interest, 2),
        "total_due": round(total_due, 2),
    }


def _policy_terms(policy: dict[str, Any]) -> dict[str, Any]:
    params = policy["params"]
    return {
        "policy_id": str(policy["policy_id"]),
        "label": str(policy.get("label", policy["policy_id"])),
        "priority": str(policy.get("priority", "medium")),
        "annual_rate": round(float(params.get("annual_rate", 0.0)), 2),
        "grace_days": int(params.get("grace_days", 0)),
        "compounding": str(params.get("compounding", "simple")),
        "interest_cap_pct": round(float(params.get("interest_cap_pct", 100.0)), 2),
        "min_principal": round(float(params.get("min_principal", 0.0)), 2),
        "max_principal": round(float(params.get("max_principal", 0.0)), 2),
        "max_defer_days": int(params.get("max_defer_days", 0)),
        "late_fee_amount": round(float(params.get("late_fee_amount", 0.0) or 0.0), 2),
        "late_fee_pct": round(float(params.get("late_fee_pct", 0.0) or 0.0), 2),
        "when_hint": str(policy.get("when_hint", "")),
    }


def compute_late_fee(
    principal: float,
    terms: Optional[dict[str, Any]] = None,
    *,
    past_due: bool = False,
) -> dict[str, Any]:
    """Late fee for a past-due entry: flat amount plus a % of principal (both optional).

    A fee only accrues once the entry is past due; policies without fee params
    contribute 0. `terms` accepts either a policy-terms dict or raw policy
    params (handled defensively so callers can pass either shape).
    """
    source = terms or {}
    if "late_fee_amount" not in source and "params" in source:
        source = dict(source["params"])
    flat = round(float(source.get("late_fee_amount", 0.0) or 0.0), 2)
    pct = round(float(source.get("late_fee_pct", 0.0) or 0.0), 2)
    principal_float = max(0.0, float(principal or 0.0))
    if not past_due or (flat <= 0.0 and pct <= 0.0):
        return {"applies": False, "late_fee_amount": 0.0, "late_fee_pct": pct, "fee_total": 0.0}
    fee_total = round(flat + principal_float * pct / 100.0, 2)
    return {"applies": True, "late_fee_amount": flat, "late_fee_pct": pct, "fee_total": fee_total}


def evaluate_waiver_approval(waiver_type: str, policy_score: Any) -> dict[str, Any]:
    """Governed approval for an arrears waiver, keyed on an administrative
    `PolicyScoreSnapshot` (policy tier rank + access score).

    ``policy_score=None`` keeps the legacy un-gated path (allowed), so existing
    integrations and tests are untouched; when a snapshot is supplied the
    waiver type's requirement rule from `ARREARS_WAIVER_POLICY` is enforced.
    """
    rule = ARREARS_WAIVER_POLICY.get(waiver_type)
    if rule is None:
        return {
            "allowed": False,
            "reason": f"Unknown waiver type {waiver_type!r}.",
            "policy_gated": True,
            "waiver_type": waiver_type,
        }
    required_tier = rule["required_policy_tier"]
    min_access = float(rule.get("min_access_score", 0.0) or 0.0)
    if policy_score is None:
        return {
            "allowed": True,
            "reason": "Legacy/unscored context: no administrative policy score supplied.",
            "policy_gated": False,
            "waiver_type": waiver_type,
            "required_policy_tier": required_tier,
            "min_access_score": min_access,
            "actual_policy_tier": None,
            "access_score": None,
        }
    actual_tier = str(getattr(policy_score, "policy_tier", "") or "")
    access_score = float(getattr(policy_score, "access_score", 0.0) or 0.0)
    tier_ok = policy_scoring.POLICY_TIER_RANK.get(actual_tier, -1) >= policy_scoring.POLICY_TIER_RANK.get(
        required_tier, 0
    )
    access_ok = access_score >= min_access
    allowed = tier_ok and access_ok
    return {
        "allowed": allowed,
        "reason": (
            "Approved by administrative policy score."
            if allowed
            else (
                f"Denied: policy tier {actual_tier!r} must be at least {required_tier!r} "
                f"and access score {access_score:.1f} must be >= {min_access:.1f}."
            )
        ),
        "policy_gated": True,
        "waiver_type": waiver_type,
        "required_policy_tier": required_tier,
        "min_access_score": min_access,
        "actual_policy_tier": actual_tier,
        "access_score": round(access_score, 2),
        "tier_sufficient": tier_ok,
        "access_sufficient": access_ok,
    }


def _assert_waiver_authorized(waiver_type: str, policy_score: Any) -> dict[str, Any]:
    """Raise when a policy score is supplied but the waiver is not authorized."""
    approval = evaluate_waiver_approval(waiver_type, policy_score)
    if policy_score is not None and not approval["allowed"]:
        raise PermissionError(f"Arrears {waiver_type} waiver not authorized: {approval['reason']}")
    return approval


def quote_arrears_payment(
    context: dict[str, Any],
    principal: float,
    defer_days: int,
    *,
    service_type: str = "consultation",
    currency: str = "USD",
) -> dict[str, Any]:
    """Quote (no write) for deferring `principal` for `defer_days` days."""
    ctx = dict(context or {})
    ctx["service_type"] = str(service_type)
    ctx["principal"] = float(principal or 0.0)
    ctx["currency"] = str(currency or "USD")
    policy, fields = select_arrears_policy(ctx)
    if policy is None:
        return {
            "eligible": False,
            "reason": "No arrears interest policy matched the user state.",
            "policy": None,
            "interest": 0.0,
            "total_due": round(float(principal or 0.0), 2),
            "matched_conditions": {},
        }
    terms = _policy_terms(policy)
    principal_float = float(principal or 0.0)
    defer_days_int = max(1, int(defer_days or 1))
    if principal_float < terms["min_principal"] or (
        terms["max_principal"] > 0 and principal_float > terms["max_principal"]
    ):
        return {
            "eligible": False,
            "reason": (
                f"Principal ${principal_float:.2f} is outside the policy range "
                f"(${terms['min_principal']:.2f} - ${terms['max_principal']:.2f})."
            ),
            "policy": terms,
            "interest": 0.0,
            "total_due": round(principal_float, 2),
            "matched_conditions": fields,
        }
    if defer_days_int > terms["max_defer_days"]:
        return {
            "eligible": False,
            "reason": f"Deferral of {defer_days_int} days exceeds the policy max of {terms['max_defer_days']} days.",
            "policy": terms,
            "interest": 0.0,
            "total_due": round(principal_float, 2),
            "matched_conditions": fields,
        }
    interest = compute_arrears_interest(
        principal_float,
        terms["annual_rate"],
        defer_days_int,
        grace_days=terms["grace_days"],
        compounding=terms["compounding"],
        cap_pct=terms["interest_cap_pct"],
    )
    return {
        "eligible": True,
        "reason": "Policy matched; quote computed.",
        "policy": terms,
        "interest": interest["interest"],
        "total_due": interest["total_due"],
        "days_after_grace": interest["days_after_grace"],
        "late_fee": compute_late_fee(principal_float, terms, past_due=False),
        "matched_conditions": fields,
    }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _entry_dict(row: Any, username: Optional[str] = None, as_of: Optional[datetime] = None) -> dict[str, Any]:
    status = str(getattr(row, "status", "open") or "open")
    principal = float(getattr(row, "principal", 0.0) or 0.0)
    accrued = float(getattr(row, "interest_accrued", 0.0) or 0.0)
    annual_rate = float(getattr(row, "annual_rate", 0.0) or 0.0)
    grace_days = int(getattr(row, "grace_days", 0) or 0)
    compounding = str(getattr(row, "compounding", "simple") or "simple")
    cap_pct = float(getattr(row, "interest_cap_pct", 100.0) or 100.0)
    opened_at = getattr(row, "opened_at", None)
    due_at = getattr(row, "due_at", None)
    interest_waived = bool(getattr(row, "interest_waived", False))
    late_fee_amount = float(getattr(row, "late_fee_amount", 0.0) or 0.0)
    late_fee_pct = float(getattr(row, "late_fee_pct", 0.0) or 0.0)
    late_fee_charged = bool(getattr(row, "late_fee_charged", False))
    fees_waived = bool(getattr(row, "fees_waived", False))
    waived_fees = float(getattr(row, "waived_fees", 0.0) or 0.0)
    now = _as_utc(as_of) or datetime.now(timezone.utc)
    opened_at = _as_utc(opened_at)
    due_at = _as_utc(due_at)
    days_elapsed = max(0, (now - opened_at).days) if opened_at else 0
    past_due = bool(due_at and due_at < now and status == "open")
    if status == "open":
        if interest_waived:
            interest_now = 0.0
        else:
            interest_now = compute_arrears_interest(
                principal, annual_rate, days_elapsed,
                grace_days=grace_days, compounding=compounding, cap_pct=cap_pct,
            )["interest"]
        pending_fee = compute_late_fee(
            principal,
            {"late_fee_amount": late_fee_amount, "late_fee_pct": late_fee_pct},
            past_due=past_due and not fees_waived,
        )["fee_total"]
        total_due = principal + interest_now + pending_fee
    elif status == "settled":
        interest_now = float(getattr(row, "settled_interest", 0.0) or 0.0)
        total_due = float(getattr(row, "total_settled", principal) or 0.0)
        pending_fee = round(max(0.0, total_due - principal - interest_now), 2) if late_fee_charged else 0.0
    else:  # waived
        interest_now = float(getattr(row, "waived_interest", accrued) or 0.0)
        total_due = principal
        pending_fee = 0.0
    return {
        "id": int(getattr(row, "id", 0) or 0),
        "user_id": int(getattr(row, "user_id", 0) or 0),
        "username": username or "",
        "booking_id": getattr(row, "booking_id", None),
        "reference": str(getattr(row, "reference", "") or ""),
        "service_type": str(getattr(row, "service_type", "") or ""),
        "principal": round(principal, 2),
        "currency": str(getattr(row, "currency", "USD") or "USD"),
        "policy_id": str(getattr(row, "policy_id", "") or ""),
        "annual_rate": round(annual_rate, 2),
        "grace_days": grace_days,
        "compounding": compounding,
        "interest_cap_pct": round(cap_pct, 2),
        "defer_days": int(getattr(row, "defer_days", 0) or 0),
        "status": status,
        "opened_at": opened_at,
        "due_at": due_at,
        "days_elapsed": days_elapsed,
        "past_due": past_due,
        "interest_accrued": round(interest_now, 2),
        "free_of_interest": interest_now <= 0.0 and status == "open",
        "total_due": round(total_due, 2),
        "settled_at": getattr(row, "settled_at", None),
        "settled_interest": round(float(getattr(row, "settled_interest", 0.0) or 0.0), 2),
        "total_settled": round(float(getattr(row, "total_settled", 0.0) or 0.0), 2),
        "interest_waived": interest_waived,
        "waived_interest": round(float(getattr(row, "waived_interest", 0.0) or 0.0), 2),
        "late_fee_amount": round(late_fee_amount, 2),
        "late_fee_pct": round(late_fee_pct, 2),
        "late_fee_charged": late_fee_charged,
        "fees_waived": fees_waived,
        "waived_fees": round(waived_fees, 2),
        "late_fee_total": round(float(pending_fee), 2),
        "note": str(getattr(row, "note", "") or ""),
    }


async def _load_entry_row(db: AsyncSession, entry_id: int):
    result = await db.execute(
        select(models.ArrearsEntry).where(models.ArrearsEntry.id == entry_id)
    )
    return result.scalars().first()


async def _username_map(db: AsyncSession) -> dict[int, str]:
    user_result = await db.execute(select(models.User.id, models.User.username))
    return {int(row[0]): str(row[1]) for row in user_result.all()}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a stored timestamp to aware UTC.

    SQLite returns naive datetimes for `DateTime(timezone=True)` columns and
    PostgreSQL returns aware ones, for the same column and the same row. Every
    arithmetic here therefore subtracts a value that came out of the database
    from a value that came out of `datetime.now(timezone.utc)`, and on SQLite that
    is naive-minus-aware:

        TypeError: can't subtract offset-naive and offset-aware datetimes

    which means *every* arrears endpoint raised on SQLite while working perfectly
    on PostgreSQL -- the kind of defect that only appears once the suite runs
    against a second database. Assumed UTC rather than localised: the values were
    written by `_now()`, which is UTC, so the naive reading is the correct one.
    """
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# Async orchestrators
# ---------------------------------------------------------------------------


async def load_user_payment_context(db: AsyncSession, user_id: int, window_days: int = 30) -> dict[str, Any]:
    """User metrics for policy matching (reuses the strategy context loader)."""
    context, _mood = await load_user_strategy_context(db, user_id, window_days)
    return context


async def open_arrears_payment(
    db: AsyncSession,
    user_id: int,
    principal: float,
    defer_days: int,
    *,
    service_type: str = "consultation",
    booking_id: Optional[int] = None,
    reference: str = "",
    currency: str = "USD",
    note: str = "",
    window_days: int = 30,
) -> Optional[dict[str, Any]]:
    """Open a pay-in-arrears entry, selecting the interest policy by context."""
    context = await load_user_payment_context(db, user_id, window_days)
    quote = quote_arrears_payment(
        context,
        principal,
        defer_days,
        service_type=service_type,
        currency=currency,
    )
    if not quote["eligible"]:
        raise ValueError(f"Arrears deferral not eligible: {quote['reason']}")
    policy = quote["policy"]
    now = _now()
    entry = models.ArrearsEntry(
        user_id=int(user_id),
        booking_id=int(booking_id) if booking_id is not None else None,
        reference=str(reference or ""),
        service_type=str(service_type),
        principal=round(float(principal), 2),
        currency=str(currency or "USD"),
        policy_id=policy["policy_id"],
        annual_rate=policy["annual_rate"],
        grace_days=policy["grace_days"],
        compounding=policy["compounding"],
        interest_cap_pct=policy["interest_cap_pct"],
        defer_days=max(1, int(defer_days or 1)),
        interest_accrued=0.0,
        status="open",
        opened_at=now,
        due_at=now + timedelta(days=max(1, int(defer_days or 1))),
        interest_waived=False,
        waived_interest=0.0,
        late_fee_amount=policy["late_fee_amount"],
        late_fee_pct=policy["late_fee_pct"],
        late_fee_charged=False,
        fees_waived=False,
        waived_fees=0.0,
        note=str(note or ""),
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return _entry_dict(entry, username=None, as_of=now)


async def quote_arrears_for_user(
    db: AsyncSession,
    user_id: int,
    principal: float,
    defer_days: int,
    *,
    service_type: str = "consultation",
    currency: str = "USD",
    window_days: int = 30,
) -> ArrearsQuote:
    context = await load_user_payment_context(db, user_id, window_days)
    quote = quote_arrears_payment(context, principal, defer_days, service_type=service_type, currency=currency)
    return ArrearsQuote(
        generated_at=_now(),
        user_id=user_id,
        window_days=window_days,
        principal=round(float(principal), 2),
        currency=str(currency or "USD"),
        service_type=str(service_type),
        defer_days=max(1, int(defer_days or 1)),
        eligible=bool(quote["eligible"]),
        reason=str(quote["reason"]),
        policy=quote.get("policy"),
        interest=float(quote.get("interest", 0.0) or 0.0),
        total_due=float(quote.get("total_due", 0.0) or 0.0),
        days_after_grace=int(quote.get("days_after_grace", 0) or 0),
        matched_conditions=quote.get("matched_conditions", {}),
    )


async def list_user_arrears(
    db: AsyncSession,
    user_id: int,
    *,
    status_filter: Optional[str] = None,
    limit: int = 50,
) -> ArrearsListReport:
    query = select(models.ArrearsEntry).where(models.ArrearsEntry.user_id == user_id)
    if status_filter:
        query = query.where(models.ArrearsEntry.status == status_filter)
    query = query.order_by(desc(models.ArrearsEntry.opened_at)).limit(max(1, int(limit)))
    result = await db.execute(query)
    rows = list(result.scalars().all())
    entries = [_entry_dict(row) for row in rows]
    open_principal = sum(float(e["principal"]) for e in entries if e["status"] == "open")
    open_interest = sum(float(e["interest_accrued"]) for e in entries if e["status"] == "open")
    return ArrearsListReport(
        generated_at=_now(),
        user_id=user_id,
        total=len(entries),
        open_total_principal=round(open_principal, 2),
        open_total_interest=round(open_interest, 2),
        entries=[ArrearsEntryOut(**entry) for entry in entries],
    )


async def build_arrears_admin_report(
    db: AsyncSession,
    *,
    status_filter: Optional[str] = None,
    window_days: int = 30,
    limit: int = 50,
) -> ArrearsAdminReport:
    query = select(models.ArrearsEntry)
    if status_filter:
        query = query.where(models.ArrearsEntry.status == status_filter)
    query = query.order_by(desc(models.ArrearsEntry.opened_at)).limit(max(1, int(limit)))
    result = await db.execute(query)
    rows = list(result.scalars().all())
    names = await _username_map(db)
    entries = [_entry_dict(row, username=names.get(getattr(row, "user_id", None))) for row in rows]
    principal_at_risk = sum(float(e["principal"]) for e in entries if e["status"] == "open")
    interest_at_risk = sum(float(e["interest_accrued"]) for e in entries if e["status"] == "open")
    overdue_count = sum(1 for e in entries if e["past_due"])
    coverage: dict[str, int] = {}
    for entry in entries:
        coverage[entry["policy_id"]] = coverage.get(entry["policy_id"], 0) + 1
    top_policy = max(coverage, key=coverage.get) if coverage else None
    return ArrearsAdminReport(
        generated_at=_now(),
        window_days=window_days,
        limit=max(1, int(limit)),
        total_entries=len(entries),
        total_open=sum(1 for e in entries if e["status"] == "open"),
        total_settled=sum(1 for e in entries if e["status"] == "settled"),
        total_waived=sum(1 for e in entries if e["status"] == "waived"),
        principal_at_risk=round(principal_at_risk, 2),
        interest_at_risk=round(interest_at_risk, 2),
        overdue_count=overdue_count,
        coverage_by_policy=coverage,
        top_policy=top_policy,
        entries=[ArrearsEntryOut(**entry) for entry in entries],
    )


async def settle_arrears_entry(
    db: AsyncSession,
    entry_id: int,
    *,
    settled_by: int = 0,
    as_of: Optional[datetime] = None,
) -> Optional[ArrearsSettleResult]:
    row = await _load_entry_row(db, entry_id)
    if row is None:
        return None
    status = str(getattr(row, "status", "open") or "open")
    if status == "settled":
        raise ValueError("Arrears entry is already settled.")
    now = _as_utc(as_of) or _now()
    principal = float(getattr(row, "principal", 0.0) or 0.0)
    interest_waived = bool(getattr(row, "interest_waived", False))
    if interest_waived:
        interest_due = 0.0
    else:
        interest_due = compute_arrears_interest(
            principal,
            float(getattr(row, "annual_rate", 0.0) or 0.0),
            max(0, (now - _as_utc(getattr(row, "opened_at", None))).days)
            if _as_utc(getattr(row, "opened_at", None))
            else 0,
            grace_days=int(getattr(row, "grace_days", 0) or 0),
            compounding=str(getattr(row, "compounding", "simple") or "simple"),
            cap_pct=float(getattr(row, "interest_cap_pct", 100.0) or 100.0),
        )["interest"]
    due_at = _as_utc(getattr(row, "due_at", None))
    past_due = bool(due_at is not None and due_at < now)
    fees_waived = bool(getattr(row, "fees_waived", False))
    fee_terms = {
        "late_fee_amount": float(getattr(row, "late_fee_amount", 0.0) or 0.0),
        "late_fee_pct": float(getattr(row, "late_fee_pct", 0.0) or 0.0),
    }
    fee_due = compute_late_fee(principal, fee_terms, past_due=past_due and not fees_waived)
    late_fee_total = round(float(fee_due["fee_total"]), 2)
    total = principal + interest_due + late_fee_total
    row.status = "settled"
    row.settled_at = now
    row.settled_interest = round(interest_due, 2)
    row.total_settled = round(total, 2)
    row.interest_accrued = round(interest_due, 2)
    row.late_fee_charged = late_fee_total > 0
    await db.commit()
    await db.refresh(row)
    return ArrearsSettleResult(
        generated_at=now,
        entry=_entry_dict(row, as_of=now, username=None),
        interest_charged=round(interest_due, 2),
        late_fee_charged=late_fee_total,
        principal=round(principal, 2),
        total_paid=round(total, 2),
    )


async def waive_arrears_interest(
    db: AsyncSession,
    entry_id: int,
    *,
    waived_by: int = 0,
    as_of: Optional[datetime] = None,
    policy_score: Any = None,
) -> Optional[ArrearsWaiveResult]:
    # Governed approval: enforced only when an administrative policy score is
    # supplied; legacy calls without one keep the previous un-gated behavior.
    _assert_waiver_authorized("interest", policy_score)
    row = await _load_entry_row(db, entry_id)
    if row is None:
        return None
    if str(getattr(row, "status", "open") or "open") != "open":
        raise ValueError("Only open arrears entries can have interest waived.")
    if bool(getattr(row, "interest_waived", False)):
        raise ValueError("Interest has already been waived for this entry.")
    # `_as_utc` on both sides, and the reason is not tidiness.
    #
    # `opened_at` comes back from the database, and a database without a time
    # zone -- which SQLite is, and which a test harness is -- hands back a naive
    # datetime. Subtracting that from an aware `now` raises `TypeError`, which the
    # global handler turns into a bare 500. `settle_arrears_entry` already
    # normalised both sides and `waive_arrears_fees` never subtracts at all, so
    # this one line made `POST /chat/admin/payments/arrears/{id}/waive-interest`
    # the only mutator on this subsystem that 500s on a naive row: the operator
    # could settle a debt but not forgive the interest on it.
    #
    # `as_of` is normalised too, because a caller passing a naive `as_of` got the
    # same 500 through a different door.
    now = _as_utc(as_of) or _now()
    opened_at = _as_utc(getattr(row, "opened_at", None))
    principal = float(getattr(row, "principal", 0.0) or 0.0)
    accrued = compute_arrears_interest(
        principal,
        float(getattr(row, "annual_rate", 0.0) or 0.0),
        max(0, (now - opened_at).days) if opened_at else 0,
        grace_days=int(getattr(row, "grace_days", 0) or 0),
        compounding=str(getattr(row, "compounding", "simple") or "simple"),
        cap_pct=float(getattr(row, "interest_cap_pct", 100.0) or 100.0),
    )["interest"]
    row.interest_waived = True
    row.waived_interest = round(accrued, 2)
    row.interest_accrued = 0.0
    row.status = "waived"
    await db.commit()
    await db.refresh(row)
    return ArrearsWaiveResult(
        generated_at=now,
        entry=_entry_dict(row, as_of=now, username=None),
        waived_interest=round(accrued, 2),
        principal=round(principal, 2),
        total_owed=round(principal, 2),
    )


async def waive_arrears_fees(
    db: AsyncSession,
    entry_id: int,
    *,
    waived_by: int = 0,
    as_of: Optional[datetime] = None,
    policy_score: Any = None,
) -> Optional[dict[str, Any]]:
    """Waive the pending late fee on an open entry (the entry stays open; the
    principal and any interest remain payable).

    Governed the same way as interest waivers: when an administrative
    `policy_score` is supplied the `ARREARS_WAIVER_POLICY["fees"]` requirement
    (customer-premium tier + access score >= 70) is enforced via
    ``evaluate_waiver_approval``; legacy calls without a score stay un-gated.
    """
    _assert_waiver_authorized("fees", policy_score)
    row = await _load_entry_row(db, entry_id)
    if row is None:
        return None
    if str(getattr(row, "status", "open") or "open") != "open":
        raise ValueError("Only open arrears entries can have fees waived.")
    if bool(getattr(row, "fees_waived", False)):
        raise ValueError("Fees have already been waived for this entry.")
    now = _as_utc(as_of) or _now()
    principal = float(getattr(row, "principal", 0.0) or 0.0)
    fee_terms = {
        "late_fee_amount": float(getattr(row, "late_fee_amount", 0.0) or 0.0),
        "late_fee_pct": float(getattr(row, "late_fee_pct", 0.0) or 0.0),
    }
    pending = compute_late_fee(principal, fee_terms, past_due=True)
    waived_fees = round(float(pending["fee_total"]), 2)
    row.fees_waived = True
    row.waived_fees = waived_fees
    row.late_fee_charged = False
    row.note = (f"{str(getattr(row, 'note', '') or '').strip()} | fees waived by admin {int(waived_by or 0)}").strip(" |")
    await db.commit()
    await db.refresh(row)
    return {
        "generated_at": now,
        "entry": _entry_dict(row, as_of=now, username=None),
        "waived_fees": waived_fees,
        "principal": round(principal, 2),
        "total_owed": round(principal, 2),
    }


def build_arrears_catalog() -> dict[str, Any]:
    """Introspection payload for `/meta/scoring-catalog`."""
    return {
        "catalog_version": "arrears_payments_v1",
        "statuses": ["open", "settled", "waived"],
        "compounding_modes": ["simple", "daily"],
        "late_fee_fields": ["late_fee_amount", "late_fee_pct"],
        "waiver_policy": {
            "note": "Administrative waivers are gated by PolicyScoreSnapshot (policy-tier rank + access score) per waiver type; legacy calls without a policy score stay un-gated.",
            "types": {
                waiver_type: {
                    "required_policy_tier": rule["required_policy_tier"],
                    "min_access_score": rule["min_access_score"],
                    "note": rule["note"],
                }
                for waiver_type, rule in ARREARS_WAIVER_POLICY.items()
            },
        },
        "interest_policies": [
            {
                "policy_id": rule["policy_id"],
                "label": rule.get("label", rule["policy_id"]),
                "priority": rule.get("priority", "medium"),
                "when": _json_safe(rule["when"]),
                "params": dict(rule["params"]),
                "when_hint": rule.get("when_hint", ""),
            }
            for rule in ARREARS_INTEREST_POLICIES
        ],
        "endpoints": {
            "self_quote": "/chat/payments/arrears/quote",
            "self_open": "/chat/payments/arrears",
            "self_list": "/chat/payments/arrears",
            "admin_report": "/chat/admin/payments/arrears",
            "admin_settle": "/chat/admin/payments/arrears/{entry_id}/settle",
            "admin_waive": "/chat/admin/payments/arrears/{entry_id}/waive-interest",
        },
    }