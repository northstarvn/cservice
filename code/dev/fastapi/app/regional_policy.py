"""Regional booking & tax policy engine (Stage 1: Business Rule Hyper-Flexibility).

Enterprise clients operate across regions, and "a booking day" stops meaning
"any day" the moment you cross borders: statutory holidays differ, weekends
differ, labor-compliance limits differ, and tax treatment differs per
jurisdiction and service type. This module centralizes those differences in a
few config tables so the core booking flow stays generic:

- ``REGIONAL_CALENDARS`` — per-region calendars: timezone, non-working
  weekdays, and holidays (fixed ISO dates and/or annual recurring month/day).
- ``REGIONAL_LABOR_RULES`` — per-region labor constraints evaluated against a
  proposed staff schedule (max consecutive working days, max shift hours,
  minimum rest between shifts).
- ``TAX_RULES`` — per-region tax rates by service type (a ``"*"`` service type
  matches every service).

Everything is pure and config-driven; the existing `bookings.py` lifecycle and
its pinned routes/contracts are intentionally untouched — this module adds the
regional *checks* that callers (booking orchestration, admin review) can run
without changing booking semantics.

Surface: `build_regional_policy_catalog` feeds `/meta/scoring-catalog`, and
`/meta/regional` exposes the live catalogs plus a resolved calendar per region.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from app import rule_engine

# ---------------------------------------------------------------------------
# Config tables
# ---------------------------------------------------------------------------

REGIONAL_CALENDARS: list[dict[str, Any]] = [
    {
        "region": "global",
        "label": "Global default",
        "timezone": "UTC",
        "weekend_days": [],
        "holidays": [],
        "when_hint": "Fallback calendar: every day is a working day (no regional constraints).",
    },
    {
        "region": "us",
        "label": "United States",
        "timezone": "America/New_York",
        "weekend_days": ["sat", "sun"],
        "holidays": [
            {"month": 1, "day": 1, "annual": True, "label": "New Year's Day"},
            {"month": 7, "day": 4, "annual": True, "label": "Independence Day"},
            {"month": 12, "day": 25, "annual": True, "label": "Christmas Day"},
            {"date": "2026-11-26", "label": "Thanksgiving"},
        ],
        "when_hint": "US business calendar with statutory federal holidays.",
    },
    {
        "region": "de",
        "label": "Germany",
        "timezone": "Europe/Berlin",
        "weekend_days": ["sat", "sun"],
        "holidays": [
            {"month": 1, "day": 1, "annual": True, "label": "Neujahr"},
            {"month": 5, "day": 1, "annual": True, "label": "Tag der Arbeit"},
            {"month": 12, "day": 25, "annual": True, "label": "1. Weihnachtstag"},
            {"month": 12, "day": 26, "annual": True, "label": "2. Weihnachtstag"},
            {"date": "2026-10-03", "label": "Tag der Deutschen Einheit 2026"},
        ],
        "when_hint": "German business calendar with statutory public holidays.",
    },
    {
        "region": "jp",
        "label": "Japan",
        "timezone": "Asia/Tokyo",
        "weekend_days": ["sat", "sun"],
        "holidays": [
            {"month": 1, "day": 1, "annual": True, "label": "元日 (New Year's Day)"},
            {"month": 12, "day": 23, "annual": True, "label": "天皇誕生日 (Emperor's Birthday)"},
            {"date": "2026-07-20", "label": "海の日 (Marine Day) 2026"},
        ],
        "when_hint": "Japanese business calendar with sample national holidays.",
    },
]

REGIONAL_LABOR_RULES: list[dict[str, Any]] = [
    {
        "region": "us",
        "max_consecutive_days": 6,
        "max_shift_hours": 12.0,
        "min_rest_hours_between_shifts": 10.0,
        "when_hint": "US-style labor guardrails.",
    },
    {
        "region": "de",
        "max_consecutive_days": 6,
        "max_shift_hours": 8.0,
        "min_rest_hours_between_shifts": 11.0,
        "when_hint": "German Arbeitsschutz-style guardrails (simplified).",
    },
    {
        "region": "jp",
        "max_consecutive_days": 6,
        "max_shift_hours": 8.0,
        "min_rest_hours_between_shifts": 11.0,
        "when_hint": "Japanese labor guardrails (simplified).",
    },
]

TAX_RULES: list[dict[str, Any]] = [
    {
        "region": "de",
        "currency": "EUR",
        "vat_pct": 19.0,
        "service_types": ["*"],
        "when_hint": "German standard VAT (19%) on all service types.",
    },
    {
        "region": "jp",
        "currency": "JPY",
        "vat_pct": 10.0,
        "service_types": ["*"],
        "when_hint": "Japanese consumption tax (10%) on all service types.",
    },
    {
        "region": "us",
        "currency": "USD",
        "vat_pct": 8.5,
        "service_types": ["project"],  # example blended rate for project services only
        "when_hint": "Example US blended sales-tax rate applied to project services.",
    },
]

_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------


def get_region_calendar(region: str) -> dict[str, Any]:
    """Resolve a region's calendar config (falls back to the global calendar)."""
    for calendar in REGIONAL_CALENDARS:
        if calendar["region"] == region:
            return calendar
    return REGIONAL_CALENDARS[0]


def _default_evaluation_date(*, now: Any = None) -> date:
    if now is not None and isinstance(now, datetime):
        return now.date()
    if isinstance(now, date):
        return now
    return datetime.now(timezone.utc).date()


def _holiday_label_for(calendar: dict[str, Any], day: date) -> Optional[str]:
    for holiday in calendar.get("holidays", []):
        if "date" in holiday:
            try:
                fixed = datetime.fromisoformat(holiday["date"].strip()).date()
            except (ValueError, AttributeError):
                continue
            if fixed == day:
                return str(holiday.get("label", holiday["date"]))
            continue
        if (
            int(holiday.get("month", 0)) == day.month
            and int(holiday.get("day", 0)) == day.day
            and bool(holiday.get("annual", False))
        ):
            return str(holiday.get("label", f"{day.month:02d}-{day.day:02d}"))
    return None


def is_working_day(region: str, effective_date: Any = None, *, now: Any = None) -> dict[str, Any]:
    """Is `effective_date` (default: today) a working day for `region`?

    Returns a decision record with the resolved calendar, weekday, weekend and
    holiday flags, and the human reason — the same explainable shape the rest of
    the decision intelligence surfaces use.
    """
    calendar = get_region_calendar(region)
    day = _coerce(effective_date, now)
    weekend_names = calendar.get("weekend_days", [])
    weekday_name = _WEEKDAY_NAMES[day.weekday()]
    weekend = weekday_name in weekend_names
    holiday_label = _holiday_label_for(calendar, day)
    working = not weekend and holiday_label is None
    return {
        "region": region,
        "calendar": calendar["region"],
        "date": day.isoformat(),
        "weekday": weekday_name,
        "timezone": calendar.get("timezone", "UTC"),
        "working": working,
        "weekend": weekend,
        "holiday": holiday_label,
        "reason": (
            "Working day."
            if working
            else (
                f"Not a working day: {holiday_label}."
                if holiday_label
                else f"Not a working day: {weekday_name} is a weekend day."
            )
        ),
    }


def _coerce(value: Any, now: Any = None) -> date:
    if value is None:
        return _default_evaluation_date(now=now)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).date()
    except ValueError:
        return _default_evaluation_date(now=now)


# ---------------------------------------------------------------------------
# Labor compliance
# ---------------------------------------------------------------------------


def get_labor_rule(region: str) -> dict[str, Any]:
    for rule in REGIONAL_LABOR_RULES:
        if rule["region"] == region:
            return rule
    return {
        "region": region,
        "max_consecutive_days": 0,
        "max_shift_hours": 0.0,
        "min_rest_hours_between_shifts": 0.0,
        "when_hint": "No labor rules configured for this region.",
    }


def evaluate_labor_compliance(region: str, schedule: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate a proposed staff schedule against the region's labor rules.

    ``schedule`` is a list of ``{"date": "YYYY-MM-DD", "shift_hours": 8.0}``
    entries (presumed contiguous working shifts). Checks (simplified model):

    - no shift longer than ``max_shift_hours``;
    - no more than ``max_consecutive_days`` working days in a row;
    - at least ``min_rest_hours_between_shifts`` between consecutive shifts
      (computed as 24h minus the previous day's shift hours).

    Returns ``{compliant, region, rule, violations: [{code, message}]}``.
    """
    rule = get_labor_rule(region)
    violations: list[dict[str, Any]] = []
    days = sorted(
        (
            {"date": _coerce(item.get("date")), "hours": float(item.get("shift_hours", 8.0) or 8.0)}
            for item in (schedule or [])
            if item.get("date") is not None
        ),
        key=lambda item: item["date"],
    )
    max_shift = float(rule.get("max_shift_hours", 0.0) or 0.0)
    if max_shift > 0:
        for item in days:
            if item["hours"] > max_shift:
                violations.append(
                    {
                        "code": "shift_hours_exceeded",
                        "message": f"Shift on {item['date']} is {item['hours']:.1f}h, above the {max_shift:.1f}h regional max.",
                    }
                )
    max_consecutive = int(rule.get("max_consecutive_days", 0) or 0)
    if max_consecutive > 0:
        running = 1
        for prev, current in zip(days, days[1:]):
            if (current["date"] - prev["date"]).days == 1:
                running += 1
            else:
                running = 1
            if running > max_consecutive:
                violations.append(
                    {
                        "code": "consecutive_days_exceeded",
                        "message": f"{current['date']} would be the {running}th consecutive working day (regional max {max_consecutive}).",
                    }
                )
                running = 1
    min_rest = float(rule.get("min_rest_hours_between_shifts", 0.0) or 0.0)
    if min_rest > 0:
        for prev, current in zip(days, days[1:]):
            if (current["date"] - prev["date"]).days != 1:
                continue
            rest = 24.0 - prev["hours"]
            if rest < min_rest:
                violations.append(
                    {
                        "code": "rest_hours_below_minimum",
                        "message": f"Rest between {prev['date']} ({prev['hours']:.1f}h) and {current['date']} is {rest:.1f}h, below the {min_rest:.1f}h regional minimum.",
                    }
                )
    return {
        "region": region,
        "rule": rule,
        "compliant": not violations,
        "violations": violations,
        "window": [item["date"].isoformat() for item in days],
    }


# ---------------------------------------------------------------------------
# Tax
# ---------------------------------------------------------------------------


def tax_rate(region: str, service_type: str = "consultation") -> dict[str, Any]:
    """Resolve the tax rule for a region/service type (a ``"*"`` type matches all)."""
    for rule in TAX_RULES:
        if rule["region"] != region:
            continue
        types = rule.get("service_types", [])
        if "*" in types or service_type in types:
            return rule
    return {"region": region, "currency": "USD", "vat_pct": 0.0, "service_types": [], "when_hint": "No tax rule."}


def compute_taxed_amount(amount: float, region: str, service_type: str = "consultation") -> dict[str, Any]:
    """Compute tax and total for a gross amount under the region's tax rule."""
    rule = tax_rate(region, service_type)
    pct = float(rule.get("vat_pct", 0.0) or 0.0)
    amount_float = round(max(0.0, float(amount or 0.0)), 2)
    tax = round(amount_float * pct / 100.0, 2)
    return {
        "region": region,
        "service_type": service_type,
        "currency": str(rule.get("currency", "USD")),
        "tax_pct": pct,
        "tax_amount": tax,
        "total": round(amount_float + tax, 2),
    }


# ---------------------------------------------------------------------------
# Composite booking-date assessment
# ---------------------------------------------------------------------------


def assess_booking_dates(
    region: str,
    dates: list[Any],
    *,
    default_shift_hours: float = 8.0,
    now: Any = None,
) -> dict[str, Any]:
    """Assess a list of proposed booking dates for a region: per-date working-day
    verdicts plus the labor-compliance verdict for a schedule of those dates."""
    working_days: list[dict[str, Any]] = []
    schedule: list[dict[str, Any]] = []
    for raw in (dates or []):
        day = _coerce(raw, now)
        verdict = is_working_day(region, day, now=now)
        working_days.append(verdict)
        schedule.append({"date": day.isoformat(), "shift_hours": default_shift_hours})
    return {
        "region": region,
        "working_days": working_days,
        "labor": evaluate_labor_compliance(region, schedule),
    }


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def build_regional_policy_catalog() -> dict[str, Any]:
    """Introspection payload for `/meta/scoring-catalog` and `/meta/regional`."""
    return {
        "catalog_version": "regional_policy_v1",
        "regions": [calendar["region"] for calendar in REGIONAL_CALENDARS],
        "calendars": [
            {
                "region": calendar["region"],
                "label": calendar.get("label", calendar["region"]),
                "timezone": calendar.get("timezone", "UTC"),
                "weekend_days": list(calendar.get("weekend_days", [])),
                "holidays": list(calendar.get("holidays", [])),
                "when_hint": calendar.get("when_hint", ""),
            }
            for calendar in REGIONAL_CALENDARS
        ],
        "labor_rules": [
            {
                "region": rule["region"],
                "max_consecutive_days": rule.get("max_consecutive_days", 0),
                "max_shift_hours": rule.get("max_shift_hours", 0.0),
                "min_rest_hours_between_shifts": rule.get("min_rest_hours_between_shifts", 0.0),
                "when_hint": rule.get("when_hint", ""),
            }
            for rule in REGIONAL_LABOR_RULES
        ],
        "tax_rules": [
            {
                "region": rule["region"],
                "currency": rule.get("currency", "USD"),
                "vat_pct": rule.get("vat_pct", 0.0),
                "service_types": list(rule.get("service_types", [])),
                "when_hint": rule.get("when_hint", ""),
            }
            for rule in TAX_RULES
        ],
        "helpers": {
            "is_working_day": "is_working_day(region, effective_date=None)",
            "evaluate_labor_compliance": "evaluate_labor_compliance(region, schedule)",
            "compute_taxed_amount": "compute_taxed_amount(amount, region, service_type)",
            "assess_booking_dates": "assess_booking_dates(region, dates)",
        },
        "rules_engine": "delegated to the shared app/rule_engine.py when-DSL core",
        "reserved_date_key": rule_engine.RESERVED_DATE_KEY,
        "endpoint": "/meta/regional",
    }