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

Expansion (thin-group pass): the three tables above answer "is this day a
working day", "is this roster legal" and "what is the tax". Real regional
operations also need to answer questions those tables cannot:

- ``REGION_HIERARCHY`` — a *chain* per region (a state inherits its country's
  calendar), so ``de-at`` resolves through ``de`` instead of silently landing
  on the global fallback that ``get_region_calendar`` returns today.
- ``HOLIDAY_OVERRIDES`` / ``HOLIDAY_SHIFT_RULES`` / ``MOVABLE_HOLIDAYS`` — local
  closures, weekend-observed holidays, and holidays that move (Thanksgiving is
  not a fixed ISO date). All three are **opt-in**: ``is_working_day`` is
  unchanged unless the caller passes ``overrides=True``.
- ``SERVICE_REGION_RULES`` — which regions offer which ``ServiceType``, with lead
  times and surcharges, so "is this bookable in JP?" is a table lookup.
- ``TAX_INCLUSIVE_RULES`` / ``ROUNDING_MODES`` — gross↔net split and money
  rounding. A gross-inclusive quote and a gross-exclusive quote differ, and
  Python's banker's rounding is the wrong default for invoices.
- ``CAPACITY_RULES`` — bookable slots per working day, used by
  ``forecast_capacity`` to turn a calendar into a staffing number.

Every one of these is a table plus a pure helper. ``is_working_day``,
``evaluate_labor_compliance``, ``tax_rate``, ``compute_taxed_amount`` and
``assess_booking_dates`` keep their exact signatures and payloads; the richer
behaviour lives in new ``resolve_*`` functions that the old ones never call.
"""

from __future__ import annotations

import decimal
from datetime import date, datetime, timedelta, timezone
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
# Expansion config tables
# ---------------------------------------------------------------------------

# Region inheritance. `None` parent = root. A region not listed here resolves to
# the global calendar exactly as `get_region_calendar` does today; a region
# listed here walks its chain, so a country-level calendar can be reused by
# every subdivision without duplicating the holiday list.
REGION_HIERARCHY: list[dict[str, Any]] = [
    {
        "region": "global",
        "parent": None,
        "label": "Global default",
        "currency": "USD",
        "when_hint": "Root of every chain; the terminal fallback when nothing matches.",
    },
    {
        "region": "us",
        "parent": "global",
        "label": "United States",
        "currency": "USD",
        "when_hint": "Federal calendar; states may add closures via HOLIDAY_OVERRIDES.",
    },
    {
        "region": "de",
        "parent": "global",
        "label": "Germany",
        "currency": "EUR",
        "when_hint": "German national calendar.",
    },
    {
        "region": "jp",
        "parent": "global",
        "label": "Japan",
        "currency": "JPY",
        "when_hint": "Japanese national calendar.",
    },
    {
        "region": "eu",
        "parent": "global",
        "label": "European Union (shared)",
        "currency": "EUR",
        "when_hint": "EU-wide layer; inherits the global calendar and adds nothing by default.",
    },
    {
        "region": "us-ca",
        "parent": "us",
        "label": "California, United States",
        "currency": "USD",
        "when_hint": "Inherits the US federal calendar, then applies California closures.",
    },
    {
        "region": "de-at",
        "parent": "de",
        "label": "Austria",
        "currency": "EUR",
        "when_hint": "Inherits the German calendar, then applies Austrian closures.",
    },
    {
        "region": "de-by",
        "parent": "de",
        "label": "Bavaria, Germany",
        "currency": "EUR",
        "when_hint": "Inherits the German calendar, then applies Bavarian closures.",
    },
]
PARENT_BY_REGION = {entry["region"]: entry["parent"] for entry in REGION_HIERARCHY}
HIERARCHY_BY_REGION = {entry["region"]: dict(entry) for entry in REGION_HIERARCHY}

# Closures and openings that are real but are not national statutory holidays.
# These are OPT-IN: `is_working_day` ignores them unless `overrides=True`, so
# the pinned v1 calendar verdicts are unchanged.
HOLIDAY_OVERRIDES: list[dict[str, Any]] = [
    {
        "region": "us-ca",
        "date": "2026-03-31",
        "kind": "holiday",
        "label": "Cesar Chavez Day",
        "authority": "California state law",
        "when_hint": "Observed in California; not a federal holiday.",
    },
    {
        "region": "de-at",
        "date": "2026-12-26",
        "kind": "holiday",
        "label": "Stefanitag",
        "authority": "Austrian province-wide",
        "when_hint": "Second Christmas Day is a service-sector holiday in Austria.",
    },
    {
        "region": "de-by",
        "month": 1,
        "day": 6,
        "annual": True,
        "kind": "holiday",
        "label": "Heilige Drei Koenige",
        "authority": "Bavarian state holiday",
        "when_hint": "Epiphany is a public holiday in Bavaria only.",
    },
    {
        "region": "de-by",
        "month": 8,
        "day": 15,
        "annual": True,
        "kind": "working_day",
        "label": "Marien Himmelfahrt (bridge day)",
        "authority": "Bavarian state holiday",
        "when_hint": "Falls on a Saturday in 2026: shown as an explicit bridge-day opening.",
    },
    {
        "region": "global",
        "month": 12,
        "day": 24,
        "annual": True,
        "kind": "working_day",
        "label": "Christmas Eve (company open)",
        "authority": "tenant override",
        "when_hint": "Example: a tenant that trades on Christmas Eve.",
    },
]

# A holiday that lands on a weekend is usually observed on a nearby weekday.
# Opt-in, and `direction` decides whether that is the following or the
# preceding weekday (and "none" means the holiday simply lapses).
HOLIDAY_SHIFT_RULES: list[dict[str, Any]] = [
    {
        "region": "us",
        "applies_to": "weekend",
        "direction": "next",
        "label_suffix": " (observed)",
        "when_hint": "Federal practice: a weekend holiday is observed the next weekday.",
    },
    {
        "region": "jp",
        "applies_to": "weekend",
        "direction": "next",
        "label_suffix": " (振替休日)",
        "when_hint": "Japanese substitute-holiday practice (振替休日).",
    },
    {
        "region": "de",
        "applies_to": "weekend",
        "direction": "none",
        "label_suffix": "",
        "when_hint": "German practice: a weekend statutory holiday is not shifted.",
    },
]

# Holidays with no fixed ISO date, resolved by "nth <weekday> of <month>".
# Thanksgiving is pinned in REGIONAL_CALENDARS as 2026-11-26, which is exactly
# the 4th Thursday of November 2026 — the two mechanisms agree for 2026, and
# the movable form keeps working in every other year.
MOVABLE_HOLIDAYS: list[dict[str, Any]] = [
    {
        "region": "us",
        "label": "Thanksgiving",
        "weekday": "thu",
        "nth": 4,
        "month": 11,
        "when_hint": "Fourth Thursday of November.",
    },
    {
        "region": "us",
        "label": "Memorial Day",
        "weekday": "mon",
        "month": 5,
        "from_month_day": "05-25",
        "when_hint": "Last Monday of May (the only US federal holiday defined by 'last').",
    },
]

# Which regions offer which ServiceType, and with what lead time / surcharge.
# `regions` is an allow-list; an empty list plus `*` semantics is not used —
# absence of a row means "no restriction configured", which is the safe default.
SERVICE_REGION_RULES: list[dict[str, Any]] = [
    {
        "service_type": "consultation",
        "regions": ["global", "us", "de", "jp", "eu", "us-ca", "de-at", "de-by"],
        "min_lead_days": 1,
        "working_days_only": True,
        "surcharge_pct": 0.0,
        "max_bookings_per_day": 8,
        "when_hint": "Available everywhere; one clear business day of notice.",
    },
    {
        "service_type": "meeting",
        "regions": ["global", "us", "de", "jp", "eu", "us-ca", "de-at", "de-by"],
        "min_lead_days": 2,
        "working_days_only": True,
        "surcharge_pct": 0.0,
        "max_bookings_per_day": 6,
        "when_hint": "Same-day meetings are not offered; two clear working days.",
    },
    {
        "service_type": "project",
        "regions": ["us", "us-ca", "de", "de-at", "de-by", "eu"],
        "min_lead_days": 10,
        "working_days_only": False,
        "surcharge_pct": 5.0,
        "max_bookings_per_day": 2,
        "when_hint": "Multi-week engagements: 10 working days, not offered in JP.",
    },
    {
        "service_type": "delivery",
        "regions": ["us", "de", "eu", "us-ca", "de-at", "de-by"],
        "min_lead_days": 5,
        "working_days_only": False,
        "surcharge_pct": 0.0,
        "max_bookings_per_day": 3,
        "when_hint": "Physical delivery is not offered in JP; cross-border surcharge on projects.",
    },
]

# Whether a quoted price is expected to be tax-inclusive in each region. A
# gross-inclusive quote cannot be treated as a net amount, and treating it as
# one silently under-collects tax by the whole rate.
TAX_INCLUSIVE_RULES: list[dict[str, Any]] = [
    {
        "region": "us",
        "prices_include_tax": True,
        "when_hint": "US storefront prices are shown tax-inclusive.",
    },
    {
        "region": "de",
        "prices_include_tax": True,
        "when_hint": "German consumer prices are shown gross (Mehrwertsteuer enthalten).",
    },
    {
        "region": "jp",
        "prices_include_tax": True,
        "when_hint": "Japanese consumer prices are shown tax-inclusive (税込).",
    },
    {
        "region": "eu",
        "prices_include_tax": True,
        "when_hint": "EU consumer prices are shown gross.",
    },
]

# Money rounding. Python's built-in `round` is banker's rounding, which makes
# invoice totals depend on the parity of the cent — rarely what finance wants.
ROUNDING_MODES: list[dict[str, Any]] = [
    {
        "mode": "half_up",
        "label": "Half away from zero",
        "when_hint": "Default for invoices: 0.005 rounds up.",
    },
    {
        "mode": "half_even",
        "label": "Banker's rounding",
        "when_hint": "What Python's round() already does; kept for parity with existing reports.",
    },
    {"mode": "down", "label": "Toward zero", "when_hint": "Never over-collects."},
    {"mode": "up", "label": "Away from zero", "when_hint": "Never under-collects."},
]
ROUNDING_BY_MODE = {entry["mode"]: dict(entry) for entry in ROUNDING_MODES}
DEFAULT_ROUNDING_MODE = "half_up"

# Bookable capacity per region, used to turn a calendar into a staffing number.
CAPACITY_RULES: list[dict[str, Any]] = [
    {
        "region": "global",
        "slots_per_working_day": 8,
        "target_utilization": 0.75,
        "when_hint": "Fallback capacity; also the default for unknown regions.",
    },
    {
        "region": "us",
        "slots_per_working_day": 8,
        "target_utilization": 0.80,
        "public_holiday_discount": 0.0,
        "when_hint": "Standard US day; federal holidays are not discounted in this model.",
    },
    {
        "region": "de",
        "slots_per_working_day": 6,
        "target_utilization": 0.70,
        "public_holiday_discount": 0.0,
        "when_hint": "German statutory holidays and the works council agreement cap the day.",
    },
    {
        "region": "jp",
        "slots_per_working_day": 5,
        "target_utilization": 0.65,
        "public_holiday_discount": 0.0,
        "when_hint": "Japanese substitute holidays (振替休日) reduce the working year further.",
    },
    {
        "region": "de-at",
        "slots_per_working_day": 6,
        "target_utilization": 0.70,
        "public_holiday_discount": 0.0,
        "when_hint": "Inherits German capacity via the region chain.",
    },
    {
        "region": "de-by",
        "slots_per_working_day": 6,
        "target_utilization": 0.70,
        "public_holiday_discount": 0.0,
        "when_hint": "Inherits German capacity via the region chain.",
    },
    {
        "region": "us-ca",
        "slots_per_working_day": 7,
        "target_utilization": 0.80,
        "public_holiday_discount": 0.0,
        "when_hint": "Slightly tighter than the federal default.",
    },
    {
        "region": "eu",
        "slots_per_working_day": 6,
        "target_utilization": 0.70,
        "public_holiday_discount": 0.0,
        "when_hint": "EU-wide planning assumption.",
    },
]

# How many working days of notice a booking needs before it can be *confirmed*
# versus merely *requested*. Expressed in working days so it composes with the
# calendar rather than fighting it.
BOOKING_NOTICE_POLICY: list[dict[str, Any]] = [
    {
        "tier": "same_day",
        "min_working_days": 0,
        "label": "Requested — not yet confirmed",
        "when_hint": "Inside the notice window; accept as a request.",
    },
    {
        "tier": "short_notice",
        "min_working_days": 1,
        "label": "Confirmed",
        "when_hint": "At least one clear working day of notice.",
    },
    {
        "tier": "standard",
        "min_working_days": 3,
        "label": "Confirmed — normal terms",
        "when_hint": "Three or more clear working days.",
    },
    {
        "tier": "long_lead",
        "min_working_days": 10,
        "label": "Confirmed — long lead time",
        "when_hint": "Ten or more clear working days; projects and deliveries.",
    },
]



# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------


def get_region_calendar(region: str) -> dict[str, Any]:
    """Resolve a region's calendar config (falls back to the global calendar)."""
    for calendar in REGIONAL_CALENDARS:
        if calendar["region"] == region:
            return calendar
    return REGIONAL_CALENDARS[0]


# ---------------------------------------------------------------------------
# Expansion: region chains
# ---------------------------------------------------------------------------


def resolve_region_chain(region: str) -> list[str]:
    """The inheritance chain for `region`, nearest first, ending at ``global``.

    Cycle-safe by construction: a cycle in ``REGION_HIERARCHY`` terminates the
    walk at the repeated node instead of hanging, and a region that is not in
    the table yields ``["<region>", "global"]`` so callers always see the
    request and the fallback.
    """
    chain: list[str] = []
    current: Optional[str] = str(region)
    while current is not None and current not in chain:
        chain.append(current)
        current = PARENT_BY_REGION.get(current)
    if "global" not in chain:
        chain.append("global")
    return chain


def resolve_region(region: str) -> dict[str, Any]:
    """Explain how `region` resolves: chain, effective calendar, fallback flag.

    ``calendar`` is the nearest ancestor that actually declares a calendar, so
    ``de-at`` reports ``de`` — the existing ``get_region_calendar`` would have
    reported ``global`` because it only matches exact region keys.
    """
    requested = str(region)
    chain = resolve_region_chain(requested)
    declared = [name for name in chain if any(c["region"] == name for c in REGIONAL_CALENDARS)]
    calendar_name = declared[0] if declared else "global"
    calendar = next(c for c in REGIONAL_CALENDARS if c["region"] == calendar_name)
    inherited = calendar_name != requested
    return {
        "requested": requested,
        "resolved": calendar_name,
        "chain": chain,
        "declared": bool(HIERARCHY_BY_REGION.get(requested)),
        "inherited": inherited,
        "inherited_from": HIERARCHY_BY_REGION.get(requested, {}).get("parent"),
        "calendar": calendar,
        "currency": HIERARCHY_BY_REGION.get(calendar_name, {}).get("currency")
        or calendar.get("currency", "USD"),
        "reason": (
            "Exact region match."
            if not inherited
            else (
                f"No calendar declared for {requested!r}; inherited from {calendar_name!r}"
                + (f" via {requested!r} -> {HIERARCHY_BY_REGION[requested]['parent']!r}." if HIERARCHY_BY_REGION.get(requested, {}).get("parent") else ".")
            )
        ),
    }


def _holiday_key(holiday: dict[str, Any]) -> str:
    """Identity of a holiday entry, so a nearer region can override a parent's."""
    if "date" in holiday:
        return f"date:{holiday['date']}"
    return f"md:{int(holiday.get('month', 0)):02d}-{int(holiday.get('day', 0)):02d}"


def _nth_weekday_of_month(year: int, month: int, weekday: str, nth: int) -> Optional[date]:
    """The `nth` `weekday` of `month`; ``nth=-1`` means the last one."""
    target = _WEEKDAY_NAMES.index(weekday)
    if nth == -1:
        last_day = (date(year + month // 12, month % 12 + 1, 1) - _ONE_DAY).day
        day = date(year, month, last_day)
        while day.weekday() != target:
            day -= _ONE_DAY
        return day
    day = date(year, month, 1)
    while day.weekday() != target:
        day += _ONE_DAY
    return day + _ONE_DAY * ((nth - 1) * 7)


_ONE_DAY = timedelta(days=1)


def movable_holiday_dates(region: str, year: int) -> dict[str, list[dict[str, Any]]]:
    """Resolve ``MOVABLE_HOLIDAYS`` for a region/year into concrete dates.

    Two forms are supported: ``nth`` (4th Thursday) and ``from_month_day``
    (last Monday of May = the first Monday on or after 05-25).
    """
    chain = resolve_region_chain(region)
    out: dict[str, list[dict[str, Any]]] = {}
    for rule in MOVABLE_HOLIDAYS:
        if str(rule["region"]) not in chain:
            continue
        month = int(rule["month"])
        if "from_month_day" in rule:
            # "Last Monday of May" == the first Monday on or after 05-25. This
            # form is checked first: it is strictly more specific than `nth`,
            # and the two are mutually exclusive in a well-formed table.
            month_text, day_text = str(rule["from_month_day"]).split("-")
            candidate = date(int(year), int(month_text), int(day_text))
            target = _WEEKDAY_NAMES.index(str(rule["weekday"]))
            if candidate.weekday() != target:
                candidate += _ONE_DAY * ((target - candidate.weekday()) % 7)
            day = candidate
        else:
            day = _nth_weekday_of_month(int(year), month, str(rule["weekday"]), int(rule["nth"]))
        if day is None:
            continue
        out.setdefault(day.isoformat(), []).append(
            {
                "label": str(rule["label"]),
                "region": str(rule["region"]),
                "movable": True,
                "rule": {k: rule[k] for k in ("weekday", "nth", "month", "from_month_day") if k in rule},
            }
        )
    return out


def holiday_overrides_for(region: str, day: date, year: Optional[int] = None) -> list[dict[str, Any]]:
    """Every ``HOLIDAY_OVERRIDES`` entry that applies to `day`, nearest region first."""
    chain = resolve_region_chain(region)
    out: list[dict[str, Any]] = []
    for entry in HOLIDAY_OVERRIDES:
        if str(entry["region"]) not in chain:
            continue
        if "date" in entry:
            try:
                if datetime.fromisoformat(str(entry["date"]).strip()).date() != day:
                    continue
            except (ValueError, AttributeError):
                continue
        elif not entry.get("annual"):
            continue
        elif int(entry.get("month", 0)) != day.month or int(entry.get("day", 0)) != day.day:
            continue
        out.append(dict(entry))
    return out


def shift_rule_for(region: str) -> Optional[dict[str, Any]]:
    chain = resolve_region_chain(region)
    for rule in HOLIDAY_SHIFT_RULES:
        if str(rule["region"]) in chain:
            return dict(rule)
    return None


def observed_holiday_date(region: str, day: date, label: str) -> Optional[date]:
    """Where a holiday landing on a weekend is *observed*, or ``None``.

    Walks the shift direction day by day and stops at the first day the region's
    own calendar calls a working day, so a chain of two weekend holidays does
    not push the observance onto a second weekend.
    """
    rule = shift_rule_for(region)
    if rule is None or str(rule.get("applies_to")) != "weekend":
        return None
    direction = str(rule.get("direction", "none"))
    if direction == "none":
        return None
    step = _ONE_DAY if direction == "next" else -_ONE_DAY
    cursor = day + step
    for _ in range(14):
        if _holiday_label_for(get_region_calendar(region), cursor) is None:
            return cursor
        cursor += step
    return None



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
# Expansion: the full working-day resolution
# ---------------------------------------------------------------------------


def resolve_working_day(
    region: str,
    effective_date: Any = None,
    *,
    now: Any = None,
    overrides: bool = True,
) -> dict[str, Any]:
    """The complete working-day verdict, with every mechanism applied.

    ``is_working_day`` answers the v1 question (weekend? statutory holiday?) and
    is frozen because its verdicts are pinned. This answers the operational
    question and layers, in order:

    1. the inherited region chain (``de-at`` -> ``de``);
    2. the parent's holidays plus the child's, with the child winning ties;
    3. ``HOLIDAY_OVERRIDES`` (a closure *or* an explicit opening);
    4. ``MOVABLE_HOLIDAYS`` resolved for the year;
    5. ``HOLIDAY_SHIFT_RULES`` weekend observance.

    Pass ``overrides=False`` to get exactly the v1 verdict under the resolved
    chain — useful for showing a customer the statutory picture separately from
    the tenant's own closures.
    """
    resolution = resolve_region(region)
    calendar = resolution["calendar"]
    day = _coerce(effective_date, now)
    weekday_name = _WEEKDAY_NAMES[day.weekday()]
    weekend = weekday_name in calendar.get("weekend_days", [])
    holiday_label = _holiday_label_for(calendar, day)
    sources: list[str] = []
    if holiday_label:
        sources.append("statutory")
    if overrides:
        for entry in holiday_overrides_for(region, day):
            kind = str(entry.get("kind", "holiday"))
            sources.append(f"override:{kind}")
            if kind == "working_day":
                # An explicit opening wins over a weekend and over a holiday:
                # that is the whole point of recording it.
                holiday_label = None
                weekend = False
            else:
                holiday_label = str(entry.get("label", holiday_label or "closure"))
        for label in [
            item["label"] for item in movable_holiday_dates(region, day.year).get(day.isoformat(), [])
        ]:
            sources.append("movable")
            holiday_label = label
        if holiday_label and weekend:
            observed = observed_holiday_date(region, day, holiday_label)
            if observed is not None:
                sources.append(f"observed:{observed.isoformat()}")
    working = not weekend and holiday_label is None
    return {
        "region": resolution["requested"],
        "resolved_region": resolution["resolved"],
        "inherited": resolution["inherited"],
        "chain": resolution["chain"],
        "calendar": calendar["region"],
        "date": day.isoformat(),
        "weekday": weekday_name,
        "timezone": calendar.get("timezone", "UTC"),
        "working": working,
        "weekend": weekend,
        "holiday": holiday_label,
        "sources": sources or (["working"] if working else ["weekend"]),
        "overrides_applied": overrides,
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


def working_days_between(region: str, start: Any, end: Any, *, include_end: bool = True) -> int:
    """Count working days in ``[start, end]``.

    A negative result means ``end`` precedes ``start`` and the span is reported
    as its negative mirror, so a caller doing date subtraction never has to
    special-case direction. ``include_end=False`` makes it a half-open interval,
    which is what you want when the end date is an exclusive deadline.
    """
    first, last = _coerce(start), _coerce(end)
    sign = 1
    if last < first:
        first, last = last, first
        sign = -1
    total = 0
    cursor = first
    while cursor <= last:
        if (include_end or cursor < last) and resolve_working_day(region, cursor)["working"]:
            total += 1
        cursor += _ONE_DAY
    return total * sign


def add_working_days(region: str, start: Any, count: int, *, now: Any = None) -> dict[str, Any]:
    """Move `count` working days from `start` (negative counts move backwards).

    Stepping is over *working* days, so a 1-working-day lead time from a Friday
    lands on the next real working day, skipping the weekend and any holiday in
    between. The returned trace lists the days actually traversed, which is the
    only way a caller can justify the resulting date to a customer.
    """
    day = _coerce(start, now)
    step = 1 if int(count) >= 0 else -1
    remaining = abs(int(count))
    traversed: list[dict[str, Any]] = []
    guard = 0
    while remaining > 0:
        guard += 1
        if guard > 3650:
            raise ValueError("add_working_days did not converge; check the region's calendar")
        day += _ONE_DAY * step
        verdict = resolve_working_day(region, day)
        if verdict["working"]:
            remaining -= 1
        traversed.append({"date": day.isoformat(), "working": verdict["working"]})
    return {
        "region": region,
        "start": _coerce(start, now).isoformat(),
        "result": day.isoformat(),
        "count": int(count),
        "traversed": traversed,
        "calendar_days": (day - _coerce(start, now)).days,
    }


def next_working_day(region: str, start: Any = None, *, inclusive: bool = True, now: Any = None) -> dict[str, Any]:
    """The first working day at or after (`inclusive`) / strictly after `start`."""
    day = _coerce(start, now)
    if not inclusive:
        day += _ONE_DAY
    for _ in range(3650):
        verdict = resolve_working_day(region, day)
        if verdict["working"]:
            return verdict
        day += _ONE_DAY
    raise ValueError(f"no working day found for region {region!r}")


def working_day_calendar(region: str, start: Any, days: int) -> dict[str, Any]:
    """A day-by-day calendar window: the scheduling view of a region."""
    first = _coerce(start)
    rows: list[dict[str, Any]] = []
    for offset in range(max(0, int(days))):
        day = first + _ONE_DAY * offset
        verdict = resolve_working_day(region, day)
        rows.append(
            {
                "date": day.isoformat(),
                "weekday": verdict["weekday"],
                "working": verdict["working"],
                "holiday": verdict["holiday"],
                "sources": verdict["sources"],
            }
        )
    working = sum(1 for row in rows if row["working"])
    return {
        "region": region,
        "from": first.isoformat(),
        "to": (first + _ONE_DAY * (max(0, int(days)) - 1)).isoformat() if days else None,
        "days": rows,
        "working_days": working,
        "non_working_days": len(rows) - working,
    }


def notice_tier(region: str, service_type: str, requested: Any, *, now: Any = None) -> dict[str, Any]:
    """Classify a booking request by how much working-day notice it carries."""
    rule = get_service_region_rule(service_type)
    lead = int(rule.get("min_lead_days", 0) or 0)
    # Notice is measured forwards, from now to the requested date.
    available = working_days_between(region, _coerce(now), _coerce(requested))
    # Highest tier whose threshold the request clears, not the lowest: a request
    # with 9 working days of notice is "standard", not "same_day".
    ladder = sorted(BOOKING_NOTICE_POLICY, key=lambda item: int(item["min_working_days"]))
    cleared = [entry for entry in ladder if available >= int(entry["min_working_days"])]
    tier = dict(cleared[-1]) if cleared else dict(ladder[0])
    meets = available >= lead
    return {
        "region": region,
        "service_type": service_type,
        "requested": _coerce(requested, now).isoformat(),
        "working_days_notice": available,
        "required_lead_days": lead,
        "meets_lead_time": meets,
        "tier": tier["tier"],
        "label": tier["label"],
        "reason": (
            f"{available} working day(s) of notice; {service_type} needs {lead}."
            if meets
            else f"Only {available} working day(s) of notice; {service_type} needs {lead}."
        ),
    }



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
# Expansion: service availability, tax split, capacity
# ---------------------------------------------------------------------------


def get_service_region_rule(service_type: str) -> dict[str, Any]:
    """Availability row for a ``ServiceType``.

    A service with no row is *unrestricted* rather than unavailable: the
    absence of configuration must not silently remove a bookable service that
    already works today.
    """
    for rule in SERVICE_REGION_RULES:
        if str(rule["service_type"]) == str(service_type):
            return rule
    return {
        "service_type": str(service_type),
        "regions": ["*"],
        "min_lead_days": 0,
        "working_days_only": False,
        "surcharge_pct": 0.0,
        "max_bookings_per_day": 0,
        "when_hint": "No service rule configured; treated as unrestricted.",
    }


def service_available(service_type: str, region: str) -> dict[str, Any]:
    """Is `service_type` offered in `region`, and on what terms?"""
    rule = get_service_region_rule(service_type)
    regions = [str(name) for name in rule.get("regions", [])]
    if "*" in regions:
        available, reason = True, "No region restriction configured."
    else:
        chain = resolve_region_chain(region)
        hit = next((name for name in regions if name in chain), None)
        available = hit is not None
        reason = (
            f"{service_type} is offered in {hit!r}; {region!r} inherits it."
            if hit and hit != region
            else (f"{service_type} is offered in {region!r}." if hit else f"{service_type} is not offered in {region!r}.")
        )
    return {
        "service_type": str(service_type),
        "region": region,
        "chain": resolve_region_chain(region),
        "available": available,
        "min_lead_days": int(rule.get("min_lead_days", 0) or 0),
        "working_days_only": bool(rule.get("working_days_only", False)),
        "surcharge_pct": float(rule.get("surcharge_pct", 0.0) or 0.0),
        "max_bookings_per_day": int(rule.get("max_bookings_per_day", 0) or 0),
        "rule": rule,
        "reason": reason,
    }


def service_regions(service_type: str) -> list[str]:
    """Every region that offers `service_type`, expansions included."""
    rule = get_service_region_rule(service_type)
    if "*" in [str(name) for name in rule.get("regions", [])]:
        return [calendar["region"] for calendar in REGIONAL_CALENDARS]
    out: list[str] = []
    for name in rule.get("regions", []):
        if str(name) in out:
            continue
        out.append(str(name))
        for child in HIERARCHY_BY_REGION.values():
            if child.get("parent") == name and child["region"] not in out:
                out.append(child["region"])
    return out


def get_capacity_rule(region: str) -> dict[str, Any]:
    chain = resolve_region_chain(region)
    for name in chain:
        for rule in CAPACITY_RULES:
            if str(rule["region"]) == name:
                return dict(rule)
    return dict(CAPACITY_RULES[0])


def forecast_capacity(
    region: str,
    start: Any,
    days: int,
    *,
    committed: int = 0,
    now: Any = None,
) -> dict[str, Any]:
    """Turn a calendar window into bookable capacity.

    Only working days contribute. ``target_utilization`` is the share of a day's
    slots the planner intends to sell, so ``committed`` is compared against
    sellable capacity rather than raw slots — otherwise a region at 80%
    utilization looks fully booked while a third of the day is idle.
    """
    calendar = working_day_calendar(region, start, days)
    rule = get_capacity_rule(region)
    slots = int(rule.get("slots_per_working_day", 0) or 0)
    target = float(rule.get("target_utilization", 1.0) or 0.0)
    sellable = int(round(slots * target)) if slots else 0
    used = max(0, int(committed))
    return {
        "region": region,
        "rule": rule,
        "window": {"from": calendar["from"], "to": calendar["to"], "days": days},
        "working_days": calendar["working_days"],
        "non_working_days": calendar["non_working_days"],
        "slots_per_working_day": slots,
        "target_utilization": target,
        "gross_capacity": slots * calendar["working_days"],
        "sellable_capacity": sellable * calendar["working_days"],
        "committed": used,
        "remaining": max(0, sellable * calendar["working_days"] - used),
        "overcommitted": used > sellable * calendar["working_days"],
        "closed_days": [row["date"] for row in calendar["days"] if not row["working"]],
    }


def _round_money(value: float, mode: str = DEFAULT_ROUNDING_MODE) -> float:
    """Round to 2dp under a named mode (``round`` is banker's by default)."""
    chosen = str(mode)
    if chosen not in ROUNDING_BY_MODE:
        raise ValueError(f"unknown rounding mode {mode!r} (expected one of {sorted(ROUNDING_BY_MODE)})")
    quant = decimal.Decimal("0.01")
    number = decimal.Decimal(str(value))
    if chosen == "half_up":
        rounding = decimal.ROUND_HALF_UP
    elif chosen == "half_even":
        rounding = decimal.ROUND_HALF_EVEN
    elif chosen == "down":
        rounding = decimal.ROUND_DOWN
    else:
        rounding = decimal.ROUND_UP
    return float(number.quantize(quant, rounding=rounding))


def prices_include_tax(region: str) -> bool:
    """Do storefront prices in `region` already contain the tax?

    Falls back to the region chain, then to ``False`` (tax-exclusive), which is
    the conservative reading: quoting gross as net under-collects, while the
    reverse only over-quotes.
    """
    chain = resolve_region_chain(region)
    for rule in TAX_INCLUSIVE_RULES:
        if str(rule["region"]) in chain:
            return bool(rule["prices_include_tax"])
    return False


def split_gross(
    gross: float,
    region: str,
    service_type: str = "consultation",
    *,
    price_is_gross: bool | None = None,
    rounding: str = DEFAULT_ROUNDING_MODE,
) -> dict[str, Any]:
    """Decompose an amount into net / tax / gross without losing a cent.

    A gross-inclusive quote and a gross-exclusive quote are different numbers
    wearing the same label, so the basis is resolved explicitly (region default
    first, caller override second) and reported back in the result. Net + tax is
    reconciled against the input to the cent, which is what an auditor checks.
    """
    rule = tax_rate(region, service_type)
    pct = float(rule.get("vat_pct", 0.0) or 0.0)
    basis_gross = prices_include_tax(region) if price_is_gross is None else bool(price_is_gross)
    amount = _round_money(max(0.0, float(gross or 0.0)), rounding)
    if basis_gross:
        # Extract the tax from the gross so net + tax reproduces it exactly.
        net = _round_money(amount / (1.0 + pct / 100.0), rounding) if pct else amount
        tax = _round_money(amount - net, rounding)
    else:
        net = amount
        tax = _round_money(net * pct / 100.0, rounding)
    total = _round_money(net + tax, rounding)
    return {
        "region": region,
        "service_type": service_type,
        "currency": str(rule.get("currency", "USD")),
        "tax_pct": pct,
        "input": amount,
        "input_is": "gross" if basis_gross else "net",
        "net": net,
        "tax_amount": tax,
        "gross": total,
        "rounding": str(rounding),
        "reconciles": _round_money(net + tax, rounding) == amount if basis_gross else True,
        "reverse_of": "compute_taxed_amount (which always adds tax to a net amount)",
    }


def tax_breakdown(
    region: str, service_type: str = "consultation", *, rounding: str = DEFAULT_ROUNDING_MODE
) -> dict[str, Any]:
    """Explain a region's tax treatment for a service, in words and numbers."""
    rule = tax_rate(region, service_type)
    inclusive = next(
        (entry for entry in TAX_INCLUSIVE_RULES if str(entry["region"]) in resolve_region_chain(region)),
        None,
    )
    return {
        "region": region,
        "service_type": service_type,
        "rule": rule,
        "prices_include_tax": prices_include_tax(region),
        "inclusive_source": (inclusive or {}).get("region"),
        "rounding": str(rounding),
        "worked_example": split_gross(100.0, region, service_type, rounding=rounding),
        "reason": (
            f"{region}: {rule.get('vat_pct', 0.0)}% on {service_type}; storefront prices are "
            + ("tax-inclusive." if prices_include_tax(region) else "tax-exclusive.")
        ),
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
        "region_hierarchy": {
            "table": [dict(entry) for entry in REGION_HIERARCHY],
            "roots": [entry["region"] for entry in REGION_HIERARCHY if entry["parent"] is None],
            "policy": "an undeclared region resolves to global, matching get_region_calendar",
            "helper": "resolve_region / resolve_region_chain",
        },
        "holiday_mechanisms": {
            "overrides": [dict(entry) for entry in HOLIDAY_OVERRIDES],
            "shift_rules": [dict(entry) for entry in HOLIDAY_SHIFT_RULES],
            "movable": [dict(entry) for entry in MOVABLE_HOLIDAYS],
            "opt_in": True,
            "policy": (
                "is_working_day() ignores all three; resolve_working_day() applies them. "
                "An override of kind 'working_day' opens a day that the statutory calendar closes."
            ),
            "helper": "resolve_working_day / movable_holiday_dates / observed_holiday_date",
        },
        "service_region_rules": {
            "table": [dict(entry) for entry in SERVICE_REGION_RULES],
            "unconfigured": "unrestricted (absence of a row never removes a bookable service)",
            "service_types": sorted({str(entry["service_type"]) for entry in SERVICE_REGION_RULES}),
            "helper": "get_service_region_rule / service_available / service_regions",
        },
        "tax_basis": {
            "inclusive_rules": [dict(entry) for entry in TAX_INCLUSIVE_RULES],
            "rounding_modes": [dict(entry) for entry in ROUNDING_MODES],
            "default_rounding": DEFAULT_ROUNDING_MODE,
            "policy": "compute_taxed_amount always adds tax to a net amount; split_gross honours the basis",
            "helper": "split_gross / prices_include_tax / tax_breakdown",
        },
        "capacity": {
            "table": [dict(entry) for entry in CAPACITY_RULES],
            "policy": "committed bookings are measured against sellable (target-utilization) capacity",
            "helper": "get_capacity_rule / forecast_capacity",
        },
        "notice_policy": {
            "table": [dict(entry) for entry in BOOKING_NOTICE_POLICY],
            "unit": "working days (calendar-aware, via add_working_days)",
            "helper": "notice_tier / add_working_days / working_days_between",
        },
        "working_day_arithmetic": {
            "helpers": [
                "working_days_between(region, start, end, include_end=True)",
                "add_working_days(region, start, count) -> date + traversal trace",
                "next_working_day(region, start, inclusive=True)",
                "working_day_calendar(region, start, days)",
                "notice_tier(region, service_type, requested)",
            ],
            "policy": "a negative span is the negative mirror of its forward span",
        },
        "rules_engine": "delegated to the shared app/rule_engine.py when-DSL core",
        "reserved_date_key": rule_engine.RESERVED_DATE_KEY,
        "endpoint": "/meta/regional",
    }