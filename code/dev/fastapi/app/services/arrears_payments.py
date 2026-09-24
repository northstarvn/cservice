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

Implementation
--------------
- `ARREARS_INTEREST_POLICIES` — config table of `when`-DSL policies; adding a
  policy is config-only.
- Pure core: `select_arrears_policy`, `compute_arrears_interest`,
  `quote_arrears_payment`.
- Async orchestrators: `open_arrears_payment`, `list_user_arrears`,
  `build_arrears_admin_report`, `settle_arrears_entry`,
  `waive_arrears_interest`.
- `build_arrears_catalog` feeds `/meta/scoring-catalog`.
"""

from __future__ import annotations

import operator
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas.chat import (
    ArrearsAdminReport,
    ArrearsEntryOut,
    ArrearsListReport,
    ArrearsQuote,
    ArrearsSettleResult,
    ArrearsWaiveResult,
)
from app.services.communication_strategy import load_user_strategy_context

# ---------------------------------------------------------------------------
# Config table (data-driven interest policies)
# ---------------------------------------------------------------------------

PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

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
    if isinstance(rule_value, dict):
        for op_name, threshold in rule_value.items():
            op = _NUMERIC_OPS.get(op_name)
            if op is None or context_value is None:
                return False
            try:
                if not op(context_value, threshold):
                    return False
            except TypeError:
                return False
        return True
    if isinstance(context_value, (list, tuple, set)):
        return _json_safe(rule_value) in list(context_value)
    if isinstance(rule_value, (list, tuple, set, frozenset)):
        return context_value in rule_value
    if isinstance(rule_value, bool):
        return bool(context_value) == rule_value
    return context_value == rule_value


def evaluate_when(when: dict[str, object], context: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Evaluate every condition in `when` against `context` (AND semantics).

    Unknown fields fail the whole rule (fail-safe) so catalog typos surface in
    self-check tests instead of silently never matching.
    """
    matched_fields: dict[str, Any] = {}
    for field_name, rule_value in when.items():
        if field_name not in context:
            return False, {}
        context_value = context[field_name]
        if not _matches_rule(rule_value, context_value):
            return False, {}
        matched_fields[field_name] = _json_safe(context_value)
    return True, matched_fields


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
        "when_hint": str(policy.get("when_hint", "")),
    }


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
    now = as_of or datetime.now(timezone.utc)
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
        total_due = principal + interest_now
    elif status == "settled":
        interest_now = float(getattr(row, "settled_interest", 0.0) or 0.0)
        total_due = float(getattr(row, "total_settled", principal) or 0.0)
    else:  # waived
        interest_now = float(getattr(row, "waived_interest", accrued) or 0.0)
        total_due = principal
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
    now = as_of or _now()
    principal = float(getattr(row, "principal", 0.0) or 0.0)
    interest_waived = bool(getattr(row, "interest_waived", False))
    if interest_waived:
        interest_due = 0.0
    else:
        interest_due = compute_arrears_interest(
            principal,
            float(getattr(row, "annual_rate", 0.0) or 0.0),
            max(0, (now - getattr(row, "opened_at", now)).days) if getattr(row, "opened_at", None) else 0,
            grace_days=int(getattr(row, "grace_days", 0) or 0),
            compounding=str(getattr(row, "compounding", "simple") or "simple"),
            cap_pct=float(getattr(row, "interest_cap_pct", 100.0) or 100.0),
        )["interest"]
    total = principal + interest_due
    row.status = "settled"
    row.settled_at = now
    row.settled_interest = round(interest_due, 2)
    row.total_settled = round(total, 2)
    row.interest_accrued = round(interest_due, 2)
    await db.commit()
    await db.refresh(row)
    return ArrearsSettleResult(
        generated_at=now,
        entry=_entry_dict(row, as_of=now, username=None),
        interest_charged=round(interest_due, 2),
        principal=round(principal, 2),
        total_paid=round(total, 2),
    )


async def waive_arrears_interest(
    db: AsyncSession,
    entry_id: int,
    *,
    waived_by: int = 0,
    as_of: Optional[datetime] = None,
) -> Optional[ArrearsWaiveResult]:
    row = await _load_entry_row(db, entry_id)
    if row is None:
        return None
    if str(getattr(row, "status", "open") or "open") != "open":
        raise ValueError("Only open arrears entries can have interest waived.")
    if bool(getattr(row, "interest_waived", False)):
        raise ValueError("Interest has already been waived for this entry.")
    now = as_of or _now()
    principal = float(getattr(row, "principal", 0.0) or 0.0)
    accrued = compute_arrears_interest(
        principal,
        float(getattr(row, "annual_rate", 0.0) or 0.0),
        max(0, (now - getattr(row, "opened_at", now)).days) if getattr(row, "opened_at", None) else 0,
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


def build_arrears_catalog() -> dict[str, Any]:
    """Introspection payload for `/meta/scoring-catalog`."""
    return {
        "catalog_version": "arrears_payments_v1",
        "statuses": ["open", "settled", "waived"],
        "compounding_modes": ["simple", "daily"],
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