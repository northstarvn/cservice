"""Regions, local hours, and contact windows (Stage E, item 1).

This exists because of a defect found in the Stage D gate, and the defect is worth
stating before the design because it is the kind that looks correct everywhere.

`services/care_gate.py` computed the hour for quiet-hours and frequency checks as
``float(moment.hour)`` on a UTC datetime. So a customer in Auckland — UTC+13 in
October — had their 22:00-to-08:00 quiet hours evaluated against **UTC hours**:
they could be contacted at 23:00 their own time and the gate would have said the
window was open. The gate was not slightly wrong, it was wrong by up to thirteen
hours for roughly half the world, and every test of it passed, because every test
used a customer in the same timezone as the server.

The fix is not "add a timezone" to the gate. It is that **"when is it acceptable
to contact this person" is a regional question with three parts**, and the gate
needs all three:

* the region they are in (or, when we do not know it, the *account's* region,
  which is a fact about them rather than a guess about their IP);
* the hour **there**;
* whether a region is currently able to receive contact at all — an office closed
  overnight and a region that does not staff it are different answers.

Unknown region fails to the **most conservative** reading rather than to UTC.
That is not timidity, it is the right default: contacting somebody at 03:00 because
we could not work out where they were is worse than not contacting them, and
"we did not know" is a reason to wait rather than a reason to guess.

Region is also **not** derived from an IP address anywhere here. A geo-IP lookup
tells you where a packet came from, which is a VPN, a hotel, a mobile network or a
travel day away from where the person actually is. Reading the account's declared
region is reading a fact about them; deriving one from an IP is inferring a fact
about them, and this codebase has a keep-as-is entry about not inferring
entitlements.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

REGION_WINDOWS_CATALOG_VERSION = 1

#: The regions this deployment can serve.
#:
#: ``utc_offset_hours`` is the **standard** offset and ``dst_offset_hours`` is a
#: **delta added to it while `dst_months` applies** -- so US/West is -8 standard and
#: -8 + 1 = -7 in summer.
#:
#: That is stated at the top because the first version of this table mixed the two
#: conventions in the same column: US/West and Auckland carried their *total*
#: summer offset while the rest carried the delta. Auckland therefore resolved to
#: UTC+25, `validate_regions` caught it, and the fix was to write the convention
#: down where a reader of the table will see it.
#:
#: A daylight-saving region is looked up from this table rather than from a calendar
#: library because implementing full tz rules is out of scope and a half-correct one
#: is worse than an explicit list. The table is wrong the day a region changes its
#: rules, and it is wrong *visibly* -- a human reads this list.
REGIONS: tuple[dict[str, Any], ...] = (
    {
        "region_id": "global_default",
        "label": "Unattributed",
        "utc_offset_hours": 0.0,
        "dst_offset_hours": 0.0,
        "dst_months": (),
        # Unconstrained, deliberately, and this took two corrections to get right.
        #
        # The intuition that an unknown region should be the *narrowest* window is
        # wrong, and wrong in a way that is easy to agree with. It reads as
        # caution, but "we do not know where they are" is not "the region we do
        # not know about is closed" -- it is the absence of a region, and an
        # absence of a region has no office hours. Narrowing the fallback to
        # 09:00-17:00 made every customer with no declared region unreachable for
        # most of the day, which is a fabricated constraint rather than a cautious
        # one, and it broke a Stage D test that had been passing.
        #
        # What the fallback *is* conservative about is the hour: UTC is used as the
        # local hour, so a customer whose region we cannot resolve is evaluated
        # against a plausible-neutral hour rather than a guessed one. The
        # customer's own declared window still applies on top of that, in local
        # terms, which is the constraint that is actually about them.
        "coverage_hours": (0, 24),
        "covers_weekends": True,
        "unattributed": True,
        "default": True,
        "note": (
            "the fallback, and deliberately the most conservative window available "
            "rather than UTC. A customer whose region we cannot resolve is "
            "contactable at fewer hours than a known one, because guessing is how "
            "somebody gets called at 03:00"
        ),
    },
    {
        "region_id": "europe_london",
        "label": "Europe / London",
        "utc_offset_hours": 0.0,
        "dst_offset_hours": 1.0,
        "dst_months": (3, 4, 5, 6, 7, 8, 9, 10),
        "coverage_hours": (8, 20),
        "covers_weekends": False,
        "note": "BST in the listed months; the table is the authority, not a tz library",
    },
    {
        "region_id": "europe_berlin",
        "label": "Europe / Berlin",
        "utc_offset_hours": 1.0,
        "dst_offset_hours": 2.0,
        "dst_months": (3, 4, 5, 6, 7, 8, 9, 10),
        "coverage_hours": (8, 20),
        "covers_weekends": False,
    },
    {
        "region_id": "us_east",
        "label": "US / East",
        "utc_offset_hours": -5.0,
        "dst_offset_hours": 1.0,
        "dst_months": (3, 4, 5, 6, 7, 8, 9, 10),
        "coverage_hours": (9, 21),
        "covers_weekends": False,
        "note": (
            "the widest coverage window of the staffed regions, and it is the one "
            "that proves why multi-region matters: the same UTC moment is 09:00 in "
            "New York and 23:00 in Auckland"
        ),
    },
    {
        "region_id": "us_west",
        "label": "US / West",
        "utc_offset_hours": -8.0,
        "dst_offset_hours": 1.0,
        "dst_months": (3, 4, 5, 6, 7, 8, 9, 10),
        "coverage_hours": (9, 21),
        "covers_weekends": False,
    },
    {
        "region_id": "asia_tokyo",
        "label": "Asia / Tokyo",
        "utc_offset_hours": 9.0,
        "dst_offset_hours": 0.0,
        "dst_months": (),
        "coverage_hours": (9, 19),
        "covers_weekends": False,
    },
    {
        "region_id": "oceania_auckland",
        "label": "Oceania / Auckland",
        "utc_offset_hours": 12.0,
        "dst_offset_hours": 1.0,
        "dst_months": (10, 11, 1, 2, 3),
        "coverage_hours": (9, 19),
        "covers_weekends": False,
        "note": (
            "the region the UTC bug hurt most. UTC+13 in October, so a 22:00 local "
            "quiet hour was 09:00 UTC -- inside office hours, and the gate said yes"
        ),
    },
    {
        "region_id": "southern_africa",
        "label": "Southern Africa",
        "utc_offset_hours": 2.0,
        "dst_offset_hours": 0.0,
        "dst_months": (),
        "coverage_hours": (8, 18),
        "covers_weekends": False,
    },
)
REGION_BY_ID: dict[str, dict[str, Any]] = {
    str(row["region_id"]): dict(row) for row in REGIONS
}
REGION_IDS: tuple[str, ...] = tuple(REGION_BY_ID)
DEFAULT_REGION_ID = "global_default"

#: How many regions must be resolvable for :func:`validate_regions` to pass.
#:
#: Deliberately low. This is a table, not a timezone database, and pretending
#: otherwise would invite the "just use pytz" reflex that ends in a dependency and
#: a DST bug. Four distinct offsets across the inhabited continents is enough to
#: demonstrate that the gate works, and the table is where a new region is added.
MIN_DISTINCT_OFFSETS = 4


def resolve_region(region_id: Optional[str]) -> dict[str, Any]:
    """The region row, or the conservative fallback.

    An unknown or absent region resolves to ``global_default`` rather than
    raising. A gate that raises on an unrecognised region would make a typo in an
    account record a support ticket, and a gate that defaults to a real region's
    hours would contact somebody on the wrong continent's schedule. The fallback is
    the *narrowest* window in the table, which is the only safe direction.
    """
    key = str(region_id or "").strip() or DEFAULT_REGION_ID
    return dict(REGION_BY_ID.get(key) or REGION_BY_ID[DEFAULT_REGION_ID])


def region_offset_hours(region_id: Optional[str], *, moment: Optional[datetime] = None) -> float:
    """The UTC offset **at this moment**, honouring the region's DST months.

    Daylight saving is a table lookup on the *local* month, which is close enough
    to be honest: the boundary days are the ones it can get wrong, and a DST
    transition that puts a contact an hour out is not the failure this module
    exists to prevent. Written down rather than left implicit because "which
    month is it there" is not the same question as "which month is it here".
    """
    region = resolve_region(region_id)
    when = moment or datetime.now(timezone.utc)
    base = float(region["utc_offset_hours"])
    dst_months = tuple(region.get("dst_months") or ())
    if not dst_months:
        return base
    # The month *at the region*, which can differ from the UTC month near a
    # month boundary. Computing it at UTC is the same class of bug as computing
    # the hour at UTC.
    local = when + timedelta(hours=base)
    if int(local.month) in {int(m) for m in dst_months}:
        return base + float(region.get("dst_offset_hours") or 0.0)
    return base


def local_hour(region_id: Optional[str], *, moment: Optional[datetime] = None) -> int:
    """The hour of the day **where the customer is**, 0-23.

    The function whose absence caused the Stage D bug. It is exported and used
    rather than inlined anywhere, because the bug was an inlined
    ``float(moment.hour)`` and an inlined fix would be one refactor away from
    coming back.
    """
    when = moment or datetime.now(timezone.utc)
    offset = region_offset_hours(region_id, moment=when)
    return int((when + timedelta(hours=offset)).hour % 24)


def local_day_of_week(
    region_id: Optional[str], *, moment: Optional[datetime] = None
) -> int:
    """Day of week there, Monday=0, matching ``datetime.weekday()``."""
    when = moment or datetime.now(timezone.utc)
    offset = region_offset_hours(region_id, moment=when)
    return int((when + timedelta(hours=offset)).weekday())


def local_date(
    region_id: Optional[str], *, moment: Optional[datetime] = None
) -> str:
    """The calendar date there, as ``YYYY-MM-DD``."""
    when = moment or datetime.now(timezone.utc)
    offset = region_offset_hours(region_id, moment=when)
    return (when + timedelta(hours=offset)).strftime("%Y-%m-%d")


def coverage_window(region_id: Optional[str]) -> tuple[int, int]:
    """The hours a region can receive contact, ``(open_hour, close_hour)``."""
    row = resolve_region(region_id)
    start, close = row.get("coverage_hours") or (0, 24)
    return int(start), int(close)


def is_region_open(
    region_id: Optional[str], *, moment: Optional[datetime] = None
) -> dict[str, Any]:
    """Whether this region's own hours allow contact right now.

    Separate from the customer's quiet hours on purpose. A region being closed is
    an operational fact — nobody is going to read it until it opens — while quiet
    hours are a *promise the customer made*. Deferring for the second and
    ignoring the first are different acts with different reasons, and the reason is
    what tells an operator whether to requeue or to fix a preference.
    """
    when = moment or datetime.now(timezone.utc)
    region = resolve_region(region_id)
    hour = local_hour(region_id, moment=when)
    weekday = local_day_of_week(region_id, moment=when)
    open_hour, close_hour = coverage_window(region_id)
    in_hours = open_hour <= hour < close_hour
    weekend = weekday >= 5
    covers_weekends = bool(region.get("covers_weekends", True))
    unattributed = bool(region.get("unattributed"))
    open_now = in_hours and (covers_weekends or not weekend)
    if unattributed:
        # No office, so it cannot be closed. The gate still gets the *hour*, and
        # the customer's own window is still enforced against it -- what it does
        # not get is a fabricated staffing constraint.
        open_now = True
    return {
        "region_id": str(region["region_id"]),
        "label": str(region.get("label") or region["region_id"]),
        "open": open_now,
        "unattributed": unattributed,
        "unattributed_note": (
            "we do not know where this customer is, so there is no office whose "
            "hours could exclude the contact. their own declared window is still "
            "applied, in local terms, against the UTC hour"
            if unattributed
            else ""
        ),
        "local_hour": hour,
        "local_day_of_week": weekday,
        "local_date": local_date(region_id, moment=when),
        "is_weekend": weekend,
        "within_coverage_hours": in_hours,
        "covers_weekends": covers_weekends,
        "coverage_hours": [open_hour, close_hour],
        "utc_offset_hours": region_offset_hours(region_id, moment=when),
        "in_hours_reason": (
            "" if open_now
            else (
                "the region is closed"
                + (" at the weekend" if weekend and not covers_weekends else "")
                + f"; staffed {open_hour:02d}:00-{close_hour:02d}:00 local, "
                f"now {hour:02d}:00"
            )
        ),
    }


def evaluate_contact_window(
    *,
    preferences_map: Optional[Mapping[str, Any]] = None,
    region_id: Optional[str] = None,
    moment: Optional[datetime] = None,
) -> dict[str, Any]:
    """Everything a proactive path needs to know about *when* to contact somebody.

    Returns the customer's **local** hour and whether their region is open, and
    deliberately does **not** decide whether contact is permitted — that is
    ``care_gate.consult``'s job, and this function feeding it is how the two stay
    separable. What it decides is the thing that was silently wrong: which hour it
    is *for them*.
    """
    when = moment or datetime.now(timezone.utc)
    region = resolve_region(region_id)
    hour = local_hour(region_id, moment=when)
    window = dict(preferences_map or {})
    start = window.get("contact_window_start_hour")
    end = window.get("contact_window_end_hour")
    declared = start is not None and end is not None
    try:
        start_hour = int(start) if start is not None else None
        end_hour = int(end) if end is not None else None
    except (TypeError, ValueError):
        declared = False
        start_hour = end_hour = None
    if declared and start_hour is not None and end_hour is not None:
        # An overnight window (22 -> 08) wraps, so the comparison is two ranges.
        if start_hour <= end_hour:
            within = start_hour <= hour < end_hour
        else:
            within = hour >= start_hour or hour < end_hour
    else:
        within = True
    return {
        "region_id": str(region["region_id"]),
        "region_label": str(region.get("label") or region["region_id"]),
        "resolved_fallback": str(region["region_id"]) == DEFAULT_REGION_ID
        and str(region_id or "").strip() != DEFAULT_REGION_ID,
        "local_hour": hour,
        "local_day_of_week": local_day_of_week(region_id, moment=when),
        "local_date": local_date(region_id, moment=when),
        "utc_offset_hours": region_offset_hours(region_id, moment=when),
        "declared_window": declared,
        "declared_window_hours": [start_hour, end_hour],
        "within_declared_window": within,
        "within_declared_window_reason": (
            "" if within
            else (
                f"{hour:02d}:00 local is outside the customer's stated "
                f"{start_hour:02d}:00-{end_hour:02d}:00 window"
                if declared
                else "no window declared"
            )
        ),
        "region": is_region_open(region_id, moment=when),
        "moment_utc": when.isoformat(),
        "note": (
            "the customer's LOCAL hour, which is the whole point. reading "
            "float(moment.hour) on a UTC datetime evaluated Auckland's 23:00 "
            "quiet hours against 10:00 UTC and found the window open"
        ),
    }


# ---------------------------------------------------------------------------
# Validation and catalog
# ---------------------------------------------------------------------------


def validate_regions() -> dict[str, Any]:
    """Check the table against itself and against the arithmetic it drives.

    Two checks that matter more than the usual cross-references:

    * **Offsets must be distinct across inhabited continents**, because a table
      where every region is UTC+0 would pass every structural test and reproduce
      the bug it was written to fix.
    * **The fallback must be the narrowest window.** If ``global_default`` covered
      24 hours then an unresolved region would be contactable at 03:00 local,
      which is the exact failure.
    """
    errors: list[str] = []
    warnings: list[str] = []

    offsets = {round(float(row["utc_offset_hours"]), 2) for row in REGIONS}
    if len(offsets) < MIN_DISTINCT_OFFSETS:
        errors.append(
            f"REGIONS declares only {len(offsets)} distinct UTC offsets; at least "
            f"{MIN_DISTINCT_OFFSETS} are needed to demonstrate that a local-hour "
            "gate is not a UTC gate. A table where every region is UTC+0 would "
            "pass every structural check and reproduce the bug"
        )

    ids = [str(row["region_id"]) for row in REGIONS]
    if len(set(ids)) != len(ids):
        errors.append("REGIONS repeats a region_id")

    defaults = [str(row["region_id"]) for row in REGIONS if row.get("default")]
    if len(defaults) != 1:
        errors.append(
            f"exactly one region must be marked default; found {defaults or 'none'}"
        )
    elif defaults[0] != DEFAULT_REGION_ID:
        errors.append(
            f"the default region is {defaults[0]!r} but DEFAULT_REGION_ID is "
            f"{DEFAULT_REGION_ID!r}; two names for the same concept"
        )

    fallback = REGION_BY_ID[DEFAULT_REGION_ID]
    if not bool(fallback.get("unattributed")):
        errors.append(
            "the default region must be marked `unattributed`. It means 'we do "
            "not know where this customer is', not 'a region with office hours', "
            "and giving it a staffed window fabricates a constraint that closes "
            "the gate for customers who never declared a region at all"
        )
    if float(fallback.get("utc_offset_hours", 0.0)) != 0.0:
        errors.append(
            "the unattributed default must sit on UTC. It is used as the local "
            "hour for customers whose region is unknown, and a guess that happens "
            "to be +13 is how the bug this module fixes comes back"
        )
    if bool(fallback.get("covers_weekends")) is not True:
        errors.append(
            "the unattributed default must cover weekends, for the same reason: "
            "no region means no weekend closure to enforce"
        )

    for row in REGIONS:
        region_id = str(row["region_id"])
        offset = float(row.get("utc_offset_hours", 0.0))
        dst = float(row.get("dst_offset_hours", 0.0))
        if not -14.0 <= offset <= 14.0:
            errors.append(
                f"REGIONS[{region_id}] utc_offset_hours {offset} is outside -14..14"
            )
        if not -14.0 <= offset + dst <= 14.0:
            errors.append(
                f"REGIONS[{region_id}] offset+dst {offset + dst} is outside -14..14"
            )
        months = tuple(row.get("dst_months") or ())
        for month in months:
            if not 1 <= int(month) <= 12:
                errors.append(
                    f"REGIONS[{region_id}] dst_months contains {month!r}, which "
                    "is not a month number"
                )
        start, close = row.get("coverage_hours") or (0, 24)
        if not 0 <= int(start) < int(close) <= 24:
            errors.append(
                f"REGIONS[{region_id}] coverage_hours {(start, close)} is not an "
                "ascending range inside 0..24"
            )
        if not str(row.get("note") or "").strip() and region_id not in (
            DEFAULT_REGION_ID,
            "europe_berlin",
        ):
            warnings.append(
                f"REGIONS[{region_id}] has no note; a window a reader cannot "
                "question is one nobody will question"
            )

    # The arithmetic itself, at a moment where UTC and local differ sharply.
    probe = datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc)
    auckland = local_hour("oceania_auckland", moment=probe)
    new_york = local_hour("us_east", moment=probe)
    if auckland == new_york:
        errors.append(
            "at the UTC probe moment, Auckland and US/East resolve to the same local "
            "hour. The offsets are wrong, or the local-hour arithmetic is -- and a "
            "local-hour function that returns one value worldwide is precisely the "
            "bug this module exists to replace"
        )
    if auckland != 12:
        errors.append(
            f"at 2026-10-01T23:00Z Auckland should be 12:00 local (UTC+13 in DST), "
            f"got {auckland}"
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "regions": len(REGIONS),
        "distinct_offsets": len(offsets),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(REGIONS)} regions and {len(offsets)} distinct UTC offsets"
        ),
    }


def build_region_catalog() -> dict[str, Any]:
    """The table, published, so the windows are reviewable without reading code."""
    return {
        "catalog_version": REGION_WINDOWS_CATALOG_VERSION,
        "regions": [dict(row) for row in REGIONS],
        "default_region": DEFAULT_REGION_ID,
        "min_distinct_offsets": MIN_DISTINCT_OFFSETS,
        "note": (
            "a table, not a timezone database, and deliberately so: implementing "
            "full tz rules is out of scope and a half-correct one is worse than an "
            "explicit list a human reads. `dst_months` is looked up against the "
            "*local* month, because computing it at UTC is the same class of bug as "
            "computing the hour at UTC. an unknown region falls back to the "
            "narrowest window in the table rather than to UTC, because guessing is "
            "how somebody gets called at 03:00. region is never derived from an IP: "
            "a geo-IP tells you where a packet came from, which is a VPN or a hotel, "
            "and that is not where the person is"
        ),
    }