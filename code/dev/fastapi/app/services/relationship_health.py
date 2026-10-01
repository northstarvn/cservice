"""Customer relationship health: the live signal, and its rollup (Stage C).

This is the *other* half of ``loyalty_status``, and keeping them apart is the
whole design:

* **``loyalty_status`` answers "what have they earned?"** Status is monotone.
  Nobody is demoted for going quiet.
* **this module answers "how are they doing *now*?"** Health moves daily and is
  allowed to fall.

A loyalty programme needs both, and conflating them produces either a status
that lies about the present or a health score that refuses to acknowledge a
customer in trouble. ``loyalty_status`` feeds this one rather than the other way
round, so a customer can be ``Trusted`` and ``critical`` at the same time -- which
is precisely the case worth acting on fastest.

Why a composite score at all
----------------------------
Because the alternative is a dashboard with six dials and no answer. But the
composite is reported **next to its parts, never instead of them**, and the parts
outrank it: a reader is expected to disagree with ``health_score`` by reading
``signals``. The score exists to rank a queue, not to be the queue.

Health is not a status, and never becomes one. There is no path from
``critical_health`` to a lower status tier, and :func:`assert_health_is_not_status`
says so in code so a future "let's demote them" is a visible edit rather than an
emergent consequence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.services import loyalty_status, retention

RELATIONSHIP_HEALTH_CATALOG_VERSION = 1

#: The health ladder, worst-first because that is the order an operator works
#: in: the customers at the top of this list are the ones who need a person.
#:
#: ``min_score`` is a floor, so the bands are read as "at or above". The
#: `no_data` band is separate from `critical` for the same reason
#: ``loyalty_status`` keeps ``unscored`` out of its tier table: a customer we have
#: never heard from is not a customer in crisis, and reporting them as one
#: manufactures an emergency out of an absence of data.
RELATIONSHIP_HEALTH_BANDS: tuple[dict[str, Any], ...] = (
    {
        "band": "no_data",
        "rank": 0,
        "min_score": 0.0,
        "label": "Not enough history",
        "action": "none",
        "sla_hours": None,
    },
    {
        "band": "critical",
        "rank": 1,
        "min_score": 0.0,
        "label": "Critical",
        "action": "human_review",
        "sla_hours": 4,
        "offer_window_hours": None,
        "note": (
            "the only band with a hard SLA, and that is the whole point of "
            "separating the two fields. an open complaint plus repeated chasing is "
            "two systems both failing the same customer, and somebody has to look. "
            "sla_hours means 'a person is accountable within N hours'; "
            "offer_window_hours means 'an automated credit stays worth giving for "
            "N hours'. giving both bands an sla_hours makes the one that matters "
            "read as decoration -- the first draft did exactly that, at_risk had a "
            "24h SLA, and validate_relationship_health caught it because 24 hours is "
            "a deadline on a credit, not on a person"
        ),
    },
    {
        "band": "at_risk",
        "rank": 2,
        "min_score": 0.30,
        "label": "At risk",
        "action": "offer_and_follow_up",
        # The one *offer* with a deadline rather than a person: 24 hours is how
        # long a goodwill credit stays worth giving, not how long an operator has
        # to look. It is not an SLA and is named in `offer_window_hours` instead,
        # so the two never get conflated -- see the note on `sla_hours` below.
        "sla_hours": None,
        "offer_window_hours": 24,
    },
    {
        "band": "strained",
        "rank": 3,
        "min_score": 0.55,
        "label": "Strained",
        "action": "watch",
        "sla_hours": None,
        "note": (
            "the band with no scheduled action and the most likely to be ignored. "
            "It exists so that a customer who is merely worse than average is "
            "visible without being escalated"
        ),
    },
    {
        "band": "healthy",
        "rank": 4,
        "min_score": 0.80,
        "label": "Healthy",
        "action": "none",
        "sla_hours": None,
    },
)
RELATIONSHIP_HEALTH_BANDS_BY_ID: dict[str, dict[str, Any]] = {
    str(row["band"]): dict(row) for row in RELATIONSHIP_HEALTH_BANDS
}

#: The signals, and how much each moves the score.
#:
#: Weights are relative. As in `loyalty_status`, the values below are all
#: 0..1 ratios and are combined directly -- the units bug that made every
#: customer score 0.079 is not repeated here.
HEALTH_SIGNALS: tuple[dict[str, Any], ...] = (
    {
        "signal_id": "churn_risk",
        "label": "Predicted churn risk",
        "source": "retention.RETENTION_CHURN_BANDS",
        # 9.0 of 21.5, i.e. 42% of the composite. Not arbitrary: with the flat
        # weights this started at (3.0), a customer at *high* churn risk with
        # five otherwise-perfect signals scored 0.855 and came back `healthy`.
        # A weighted mean cannot make one signal dominant unless that signal
        # carries most of the weight, and this module's own docstring calls churn
        # "the single most predictive number this system has" -- so the weight
        # says that out loud rather than the prose merely asserting it.
        "weight": 9.0,
        "why": (
            "the single most predictive number this system has, and the reason "
            "the health score is allowed to move at all"
        ),
    },
    {
        "signal_id": "open_complaints",
        "label": "Complaints nobody has closed",
        "source": "complaints",
        "weight": 5.0,
        "why": (
            "weighted with churn risk rather than below it. an unresolved "
            "complaint is not a signal about the relationship, it is the "
            "relationship"
        ),
    },
    {
        "signal_id": "chasing_effort",
        "label": "How much they have had to chase",
        "source": "chat_history + booking_events",
        "weight": 3.0,
        "why": (
            "the quietest early warning there is. nobody complains about being "
            "chased; they just stop asking"
        ),
    },
    {
        "signal_id": "follow_through",
        "label": "Do requests get finished",
        "source": "bookings",
        "weight": 2.0,
        "why": "a pending booking is an open loop, and open loops drive churn",
    },
    {
        "signal_id": "reliability",
        "label": "Kept promises",
        "source": "bookings + booking_events",
        "weight": 1.5,
        "why": (
            "lowest weight of the risk signals on purpose: our own completion "
            "rate is largely out of our hands, and a customer who was let down "
            "should not have that held against them"
        ),
    },
    {
        "signal_id": "dormancy",
        "label": "How long since we heard from them",
        "source": "bookings + chat_history",
        "weight": 1.0,
        "why": (
            "lowest weight because dormancy is the *ambiguous* signal. a customer "
            "who has everything they need has no reason to contact us, and "
            "treating silence as dissatisfaction is how a programme ends up "
            "cajoling people who are content"
        ),
    },
)

#: **Polarity: higher is healthier.** Every signal below is a *risk* measurement
#: and is inverted on the way in, so the published score reads the way the bands
#: do -- `healthy` at the top, `critical` at the floor.
#:
#: The first version left them un-inverted, which inverted the whole ladder: a
#: customer at ``critical`` churn risk scored 0.12 and came back ``critical``,
#: while a healthy one scored 0.36 and came back ``at_risk``. The composite moved
#: in the right direction and the bands read it backwards, which is the most
#: damaging way this table can fail -- it sent the healthiest customers to a
#: human and the sickest to an automated offer.
POLARITY = "higher_is_healthier"

#: Open loops tolerated before a signal counts at all, so a brand-new customer is
#: not reported as strained for having one pending booking.
CHASE_TOLERANCE = 3
OPEN_COMPLAINT_TOLERANCE = 0
DORMANCY_DAYS_FOR_FATIGUE = 30
DORMANCY_DAYS_SEVERE = 90

#: Declared above HEALTH_OVERRIDES because a rule references
#: ``DORMANCY_DAYS_SEVERE``: a table that cannot name its own thresholds from the
#: rows above it is a table whose thresholds get copied into it and drift.

#: Findings that are dispositive **regardless of the arithmetic**.
#:
#: A composite cannot express "an open complaint *and* high churn risk is a crisis
#: even though nothing else looks wrong", and forcing it to means one signal
#: carries ~70% of the weight and the other five become decoration. So these are
#: rules, applied after the composite, and each reports why it fired. This is also
#: how a clinician reasons: some findings are dispositive on their own.
HEALTH_OVERRIDES: tuple[dict[str, Any], ...] = (
    {
        "override_id": "critical_churn",
        "minimum_band": "at_risk",
        "when": {"churn_band": ["critical"]},
        "reason": (
            "a customer predicted to leave is never merely 'strained'. the "
            "arithmetic cannot express this -- with five other healthy signals a "
            "zeroed churn risk still scores above the at_risk floor"
        ),
    },
    {
        "override_id": "severe_dormancy",
        "minimum_band": "strained",
        "when": {"days_since_activity_min": DORMANCY_DAYS_SEVERE},
        "reason": (
            "a weighted mean cannot express this: with the other five signals "
            "perfect, total silence still scored above the healthy floor. but "
            "'not heard from in six months' is not healthy, whatever the history "
            "says, and the band is the place to say so"
        ),
    },
    {
        "override_id": "open_complaint_and_risk",
        "minimum_band": "critical",
        "when": {"churn_band": ["high", "critical"], "open_complaints_min": 1},
        "reason": (
            "two systems failing the same customer at once. this is the finding "
            "that gets a person, and it is the combination rather than either "
            "part: plenty of people complain and stay, and plenty are churning "
            "without a case open"
        ),
    },
)



#: Only ``critical`` may carry ``sla_hours``, and ``at_risk`` is the only band
#: with an ``offer_window_hours``. Enforced by :func:`validate_relationship_health`
#: on purpose: the temptation to give every band a deadline is exactly how the one
#: deadline that means something stops being distinguishable from the rest.
SLA_BANDS: tuple[str, ...] = ("critical",)
OFFER_WINDOW_BANDS: tuple[str, ...] = ("at_risk",)

#: Why ``sla_hours`` and ``offer_window_hours`` are separate columns, which is not
#: a style preference:
#:
#: * ``sla_hours`` -- a *person* is accountable. An open complaint plus repeated
#:   chasing is two systems both failing one customer, and somebody has to look.
#: * ``offer_window_hours`` -- an automated credit stays *worth giving* for this
#:   long. Nobody is accountable; it is a property of the offer.
#:
#: The first draft gave ``at_risk`` an ``sla_hours`` of 24, meaning "act within a
#: day", and the validator caught it -- because an SLA nobody owns is decoration
#: and it makes the one that is owned read as decoration too.

#: Band resolution: best-first, skipping the two floor bands.
_SKIP_IN_FLOOR_WALK = ("no_data", "critical")


def apply_health_overrides(
    *,
    band: str,
    risk: str,
    open_complaints: int,
    days_since: Optional[int] = None,
) -> dict[str, Any]:
    """Raise the band to whatever a dispositive finding requires.

    Bands get *worse* as the rank rises here (``critical`` is rank 1,
    ``healthy`` is rank 4), so a minimum band is a maximum rank. That inversion is
    easy to get backwards, so it is expressed as "not worse than" and the
    comparison is one ``min`` on rank.

    Each firing override is reported with its reason, so a reader can tell the
    difference between "the arithmetic said critical" and "an open complaint plus
    high churn risk said critical while the arithmetic said healthy" -- which are
    very different conversations to have about a customer.
    """
    worst = band
    fired: list[dict[str, Any]] = []
    for rule in HEALTH_OVERRIDES:
        when = dict(rule.get("when") or {})
        wanted = [str(item) for item in (when.get("churn_band") or ())]
        if wanted and risk not in wanted:
            continue
        minimum_open = int(when.get("open_complaints_min") or 0)
        if open_complaints < minimum_open:
            continue
        minimum_days = when.get("days_since_activity_min")
        if minimum_days is not None:
            # None means we have no evidence of activity at all, which is not the
            # same as "quiet for a long time". Passing None here would let an
            # unknown look like a year of silence and escalate on absent data.
            if days_since is None or days_since < int(minimum_days):
                continue
        floor = str(rule["minimum_band"])
        floor_rank = int(
            RELATIONSHIP_HEALTH_BANDS_BY_ID.get(floor, {}).get("rank") or 0
        )
        current_rank = int(
            RELATIONSHIP_HEALTH_BANDS_BY_ID.get(worst, {}).get("rank") or 0
        )
        # "not worse than" is a lower rank, so min() on ranks keeps the worst.
        if floor_rank < current_rank:
            worst = floor
            fired.append(
                {
                    "override_id": str(rule["override_id"]),
                    "forced_band": floor,
                    "previous_band": band,
                    "reason": str(rule["reason"]),
                }
            )
    return {"band": worst, "overrides_fired": fired}


def _now() -> Optional[datetime]:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    return loyalty_status._aware(value)


def _clamp01(value: Any, default: float = 0.0) -> float:
    return loyalty_status._clamp01(value, default)


def resolve_health_band(score: float) -> str:
    """The band for a 0..1 health score. Higher is healthier.

    Takes the **highest** band whose floor the score meets. The first version took
    the *first match* in declaration order, and since the table is declared worst
    first that is always ``at_risk`` -- so a score of a flawless 1.000 came back
    ``at_risk``. The docstring described a best-first walk the code did not do.

    ``critical`` and ``no_data`` are skipped: ``critical`` is the floor for a
    customer **with** history, and ``no_data`` is decided by coverage in
    :func:`resolve_health_band_for` rather than by a score.
    """
    value = _clamp01(score)
    best = "critical"
    for row in RELATIONSHIP_HEALTH_BANDS:
        band = str(row["band"])
        if band in ("no_data", "critical"):
            continue
        if value >= float(row["min_score"]):
            best = band
    return best


def resolve_health_band_for(
    *, score: float, has_history: bool
) -> str:
    """The band, given whether there is any history to judge on.

    The `no_data` / `critical` distinction lives here rather than in the score,
    because it is a fact about **coverage** rather than about quality. A customer
    we have never heard from scores 0.0 for the same reason a customer we have
    lost scores 0.0, and those two must never land in the same queue.
    """
    if not has_history:
        return "no_data"
    return resolve_health_band(score)


# ---------------------------------------------------------------------------
# The per-customer health report
# ---------------------------------------------------------------------------


def build_relationship_health(
    *,
    ledger: Mapping[str, Any],
    churn_band: str = "",
    churn_risk: Optional[float] = None,
    complaints: Sequence[Mapping[str, Any]] = (),
    chat_rows: Sequence[Mapping[str, Any]] = (),
    booking_events: Sequence[Mapping[str, Any]] = (),
    latest_activity_at: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """How this relationship is doing right now, and what it needs.

    **Not a status.** Health is allowed to fall where status is not, and the two
    are reported together precisely so a reader can see a customer who is
    ``Trusted`` and ``critical`` at once -- the case that deserves the fastest
    response and that a single blended number would hide.
    """
    moment = _aware(now) or _now()
    values: dict[str, float] = {}
    counts: dict[str, int] = {}

    # --- churn risk, read from the retention vocabulary rather than re-banded.
    # Inverted: INVERTED_RISK maps a churn band to how *healthy* it is, which is
    # the polarity this module's bands need.
    risk = str(churn_band or "").strip().lower()
    counts["churn_band_known"] = 1 if risk else 0
    values["churn_risk"] = INVERTED_RISK.get(risk, 0.0)

    # --- complaints: an unresolved one is the relationship, not a signal of it
    complaint_rows = list(complaints or ())
    open_complaints = [
        row for row in complaint_rows
        if str(row.get("status") or getattr(row, "status", "") or "") not in {"closed", "withdrawn"}
    ]
    counts["complaints_open"] = len(open_complaints)
    counts["complaints_total"] = len(complaint_rows)
    values["open_complaints"] = 1.0 - _clamp01(
        len(open_complaints) / 3.0 if len(open_complaints) > OPEN_COMPLAINT_TOLERANCE else 0.0
    )

    # --- chasing effort
    contacts = len(list(chat_rows or ()))
    repeats = loyalty_status._repeated_contact(chat_rows)
    counts["chat_contacts"] = contacts
    counts["chase_repeats"] = repeats
    chase_total = contacts + repeats
    values["chasing_effort"] = 1.0 - _clamp01(
        max(0.0, chase_total - CHASE_TOLERANCE) / 20.0
    )

    # --- follow-through and reliability, straight from the ledger
    ledger_counts = dict(ledger.get("counts") or {})
    completed = int(ledger_counts.get("reliability_positive") or 0)
    cancelled = int(ledger_counts.get("reliability_negative") or 0)
    pending = int(ledger_counts.get("follow_through_negative") or 0)
    counts["bookings_completed"] = completed
    counts["bookings_cancelled"] = cancelled
    counts["bookings_pending"] = pending
    values["follow_through"] = 1.0 - _clamp01(pending / 5.0)
    # Already in healthiness: our own completion rate.
    values["reliability"] = 1.0 - (
        (cancelled / (completed + cancelled)) if (completed + cancelled) else 0.0
    )

    # --- dormancy: the ambiguous one, weighted lowest
    latest = _aware(latest_activity_at)
    if latest is None:
        # Fall back to the ledger's own tenure evidence rather than guessing.
        tenure_days = int(ledger_counts.get("tenure_days") or 0)
        days_since = None
    else:
        days_since = max(0, (moment - latest).days)
        counts["days_since_activity"] = days_since
    if days_since is None:
        # Unmeasured dormancy is not "no dormancy": absent the evidence, this
        # signal is uncountable rather than healthy.
        values["dormancy"] = 0.0
        dormancy_countable = False
        counts["days_since_activity"] = None
    else:
        dormancy_countable = True
        if days_since >= DORMANCY_DAYS_SEVERE:
            values["dormancy"] = 0.0
        elif days_since >= DORMANCY_DAYS_FOR_FATIGUE:
            values["dormancy"] = 1.0 - (
                (days_since - DORMANCY_DAYS_FOR_FATIGUE)
                / float(DORMANCY_DAYS_SEVERE - DORMANCY_DAYS_FOR_FATIGUE)
            )
        else:
            values["dormancy"] = 1.0

    total_weight = 0.0
    weighted = 0.0
    signals: list[dict[str, Any]] = []
    for spec in HEALTH_SIGNALS:
        signal_id = str(spec["signal_id"])
        weight = float(spec["weight"])
        value = _clamp01(values.get(signal_id, 0.0))
        # A signal with nothing to measure is not a zero score. `churn_risk` is
        # uncountable when the retention engine has not produced a band, and
        # counting it as 1.0 would report a customer as healthy because we have
        # not assessed them.
        countable = True
        if signal_id == "churn_risk" and not counts["churn_band_known"]:
            countable = False
        if signal_id == "dormancy" and not dormancy_countable:
            countable = False
        if not countable:
            signals.append(
                {
                    "signal_id": signal_id,
                    "label": str(spec["label"]),
                    "weight": weight,
                    "value": None,
                    "countable": False,
                    "contribution": 0.0,
                    "why": str(spec["why"]),
                }
            )
            continue
        total_weight += abs(weight)
        weighted += weight * value
        signals.append(
            {
                "signal_id": signal_id,
                "label": str(spec["label"]),
                "weight": weight,
                "value": round(value, 4),
                "countable": True,
                "contribution": round(weight * value, 4),
                "why": str(spec["why"]),
            }
        )

    score = _clamp01(weighted / total_weight) if total_weight > 0 else 0.0
    has_history = bool(
        completed or cancelled or pending or complaint_rows or counts["chat_contacts"]
    )
    band = resolve_health_band_for(score=score, has_history=has_history)
    overrides = apply_health_overrides(
        band=band,
        risk=risk,
        open_complaints=len(open_complaints),
        days_since=counts.get("days_since_activity"),
    )
    band = str(overrides["band"])
    declared = RELATIONSHIP_HEALTH_BANDS_BY_ID.get(band, {})

    return {
        "generated_at": moment.isoformat(),
        "score": round(score, 4),
        "polarity": POLARITY,
        "band": band,
        "label": str(declared.get("label") or band),
        "rank": int(declared.get("rank") or 0),
        "action": str(declared.get("action") or "none"),
        "sla_hours": declared.get("sla_hours"),
        "offer_window_hours": declared.get("offer_window_hours"),
        "arithmetic_band": resolve_health_band_for(score=score, has_history=has_history),
        "overrides_fired": overrides["overrides_fired"],
        "signals": signals,
        "counts": counts,
        "values": {key: round(value, 4) for key, value in values.items()},
        "has_history": has_history,
        "is_status": False,
        "investment_band": str(
            loyalty_status.resolve_investment_band(
                ledger=ledger,
                churn_band=risk,
                relationship_health={"score": score},
            )["band"]
        ),
        "note": (
            "health, not status. this is allowed to fall where a loyalty status "
            "is not, and the two are reported together precisely so a customer "
            "who is Trusted and critical at once is visible -- that combination is "
            "the one that deserves the fastest response, and a single blended "
            "number hides it. read the signals, not the score: the score exists "
            "to rank a queue, not to be the queue"
        ),
    }


#: Churn risk inverted into a *health* contribution. Read from the retention
#: vocabulary at import time where possible, and mirrored here with a validator
#: check so a rename in `retention` cannot leave this silently applying 0.0 to
#: every customer -- which would report everyone as healthy.
INVERTED_RISK: dict[str, float] = dict(loyalty_status.INVERTED_RISK_CONTRIBUTIONS)


def assert_health_is_not_status(report: Mapping[str, Any]) -> None:
    """The invariant, asserted rather than assumed.

    A future "let's demote them when they go critical" would be a visible edit
    here rather than an emergent consequence of a blended score.
    """
    if report.get("is_status"):
        raise AssertionError(
            "relationship health is not a status: it is allowed to fall where a "
            "loyalty status is not, and demoting someone for being in trouble "
            "would punish them for our own failure"
        )


# ---------------------------------------------------------------------------
# The rollup
# ---------------------------------------------------------------------------


async def build_relationship_health_report(
    db: AsyncSession,
    *,
    window_days: int = 30,
    limit: int = 50,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The operator's queue: who needs a person, worst first.

    Built from **aggregate-safe reads** over the rows that exist, not from N
    per-customer builds. The reason is written in `customer_360`'s admin rollup
    and it applies identically here: the expensive path runs 8 engines per user
    and one of them calls a sentiment model, so "every customer" is not a
    question this function may answer.

    So the queue is ordered by *evidence we can cheaply see* -- open complaints,
    chase volume, pending bookings, days since activity -- and
    ``is_exhaustive: False`` is published. A report that silently returned the
    twenty loudest customers and called itself the health of the book would be
    worse than one that admits it is a triage queue.

    ``missing`` carries the customers with no rows at all, counted not listed,
    because "nobody" and "nobody we have data on" are different answers and the
    second one is common early on.
    """
    moment = _aware(now) or _now()
    since = moment - timedelta(days=int(window_days))

    complaint_stmt = select(models.ComplaintCase).where(
        models.ComplaintCase.updated_at >= since
    )
    complaints = list((await db.execute(complaint_stmt)).scalars().all())

    booking_stmt = select(models.Booking)
    bookings = list((await db.execute(booking_stmt)).scalars().all())
    booking_ids = {int(row.id) for row in bookings}

    chat_stmt = select(models.ChatHistory).where(models.ChatHistory.timestamp >= since)
    chats = list((await db.execute(chat_stmt)).scalars().all())

    # Per-customer evidence, from the cheapest rows that exist.
    evidence: dict[int, dict[str, Any]] = {}

    def _bucket(user_id: int) -> dict[str, Any]:
        return evidence.setdefault(
            int(user_id),
            {
                "user_id": int(user_id),
                "open_complaints": 0,
                "pending_bookings": 0,
                "cancelled_bookings": 0,
                "completed_bookings": 0,
                "chat_contacts": 0,
                "latest_activity_at": None,
                "first_seen_at": None,
            },
        )

    for case in complaints:
        bucket = _bucket(case.user_id)
        status = str(getattr(case, "status", "") or "")
        if status not in {"closed", "withdrawn"}:
            bucket["open_complaints"] += 1
        _touch(bucket, getattr(case, "created_at", None), moment)

    for booking in bookings:
        bucket = _bucket(booking.user_id)
        status = str(getattr(booking, "status", "") or "")
        if status == "pending":
            bucket["pending_bookings"] += 1
        elif status == "cancelled":
            bucket["cancelled_bookings"] += 1
        elif status == "completed":
            bucket["completed_bookings"] += 1
        _touch(bucket, getattr(booking, "created_at", None), moment)

    for chat in chats:
        bucket = _bucket(chat.user_id)
        bucket["chat_contacts"] += 1
        _touch(bucket, getattr(chat, "timestamp", None), moment)

    queue: list[dict[str, Any]] = []
    for bucket in evidence.values():
        health = _health_from_evidence(bucket, moment)
        queue.append(health)
    queue.sort(
        key=lambda row: (
            row["rank"],
            -(row["score"]),
            -int(row["counts"].get("open_complaints") or 0),
            row["user_id"],
        )
    )
    truncated = queue[: int(limit)]

    user_stmt = select(models.User.id)
    known_ids = {int(row) for row in (await db.execute(user_stmt)).scalars().all()}
    missing = len(set(evidence) - known_ids)

    return {
        "generated_at": moment.isoformat(),
        "window_days": int(window_days),
        "band_order": [str(row["band"]) for row in RELATIONSHIP_HEALTH_BANDS],
        "bands": [dict(row) for row in RELATIONSHIP_HEALTH_BANDS],
        "signals": [dict(row) for row in HEALTH_SIGNALS],
        "queue": truncated,
        "queue_length": len(truncated),
        "evaluated": len(queue),
        "truncated": len(queue) > len(truncated),
        "is_exhaustive": False,
        "unknown_user_ids": missing,
        "by_band": _count_by(queue, "band"),
        "note": (
            "a triage queue, not the health of the book. built from aggregate-safe "
            "reads over the rows that exist rather than N per-customer builds -- the "
            "expensive path runs eight engines per user and one of them calls a "
            "sentiment model. published is_exhaustive=False on purpose: a report that "
            "returned the twenty loudest customers and called itself comprehensive "
            "would be worse than one that admits it is a ranking. `missing` counts "
            "user ids with no matching row rather than listing them"
        ),
    }


def _touch(bucket: dict[str, Any], value: Any, moment: datetime) -> None:
    stamp = _aware(value)
    if stamp is None:
        return
    latest = bucket.get("latest_activity_at")
    if latest is None or stamp > latest:
        bucket["latest_activity_at"] = stamp
    first = bucket.get("first_seen_at")
    if first is None or stamp < first:
        bucket["first_seen_at"] = stamp


def _health_from_evidence(bucket: Mapping[str, Any], moment: datetime) -> dict[str, Any]:
    """Grade one customer from cheap evidence only.

    Churn risk is **absent** here rather than assumed good, so it is reported as
    uncountable. A queue built this way would otherwise put every customer at
    full health on churn, because the retention engine's band needs a per-customer
    build this function deliberately does not perform. Reporting it as unknown is
    the honest answer and the `signals` list says which rows are missing it.
    """
    latest = bucket.get("latest_activity_at")
    days_since = max(0, (moment - latest).days) if latest is not None else None
    pending = int(bucket.get("pending_bookings") or 0)
    cancelled = int(bucket.get("cancelled_bookings") or 0)
    completed = int(bucket.get("completed_bookings") or 0)
    open_complaints = int(bucket.get("open_complaints") or 0)
    contacts = int(bucket.get("chat_contacts") or 0)

    # Inverted into healthiness, matching POLARITY. `reliability` is the one that
    # arrives in the right polarity already.
    values = {
        # No churn band is available on this path, so this signal is excluded
        # below rather than defaulted.
        "churn_risk": 0.0,
        "open_complaints": 1.0 - (
            _clamp01(open_complaints / 3.0) if open_complaints else 0.0
        ),
        "chasing_effort": 1.0 - _clamp01(max(0.0, contacts - CHASE_TOLERANCE) / 20.0),
        "follow_through": 1.0 - _clamp01(pending / 5.0),
        "reliability": 1.0 - (
            (cancelled / (completed + cancelled)) if (completed + cancelled) else 0.0
        ),
        "dormancy": (
            0.0
            if days_since is None
            else (
                0.0
                if days_since >= DORMANCY_DAYS_SEVERE
                else 1.0
                - _clamp01(
                    (days_since - DORMANCY_DAYS_FOR_FATIGUE)
                    / float(DORMANCY_DAYS_SEVERE - DORMANCY_DAYS_FOR_FATIGUE)
                )
            )
        ),
    }
    signals: list[dict[str, Any]] = []
    total_weight = 0.0
    weighted = 0.0
    for spec in HEALTH_SIGNALS:
        signal_id = str(spec["signal_id"])
        weight = float(spec["weight"])
        countable = signal_id != "churn_risk"
        if not countable:
            signals.append(
                {
                    "signal_id": signal_id,
                    "weight": weight,
                    "value": None,
                    "countable": False,
                    "why": (
                        "not measured on this path: the retention churn band needs a "
                        "per-customer build, which this report deliberately does not "
                        "perform. assumed-good would report everyone as healthy"
                    ),
                }
            )
            continue
        value = _clamp01(values.get(signal_id, 0.0))
        total_weight += abs(weight)
        weighted += weight * value
        signals.append(
            {
                "signal_id": signal_id,
                "weight": weight,
                "value": round(value, 4),
                "countable": True,
            }
        )
    score = _clamp01(weighted / total_weight) if total_weight > 0 else 0.0
    has_history = bool(
        completed or cancelled or pending or open_complaints or contacts
    )
    band = resolve_health_band_for(score=score, has_history=has_history)
    overrides = apply_health_overrides(
        band=band,
        risk="",
        open_complaints=open_complaints,
        days_since=days_since,
    )
    band = str(overrides["band"])
    declared = RELATIONSHIP_HEALTH_BANDS_BY_ID.get(band, {})
    return {
        "user_id": int(bucket.get("user_id") or 0),
        "score": round(score, 4),
        "polarity": POLARITY,
        "arithmetic_band": resolve_health_band_for(score=score, has_history=has_history),
        "overrides_fired": overrides["overrides_fired"],
        "band": band,
        "label": str(declared.get("label") or band),
        "rank": int(declared.get("rank") or 0),
        "action": str(declared.get("action") or "none"),
        "sla_hours": declared.get("sla_hours"),
        "offer_window_hours": declared.get("offer_window_hours"),
        "signals": signals,
        "counts": {
            "open_complaints": open_complaints,
            "pending_bookings": pending,
            "cancelled_bookings": cancelled,
            "completed_bookings": completed,
            "chat_contacts": contacts,
            "days_since_activity": days_since,
        },
        "has_history": has_history,
        "churn_band_known": False,
    }


def _count_by(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row.get(key) or "")
        counts[name] = counts.get(name, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Validation and catalog
# ---------------------------------------------------------------------------


def validate_relationship_health() -> dict[str, Any]:
    """Check this module's tables against each other and against `retention`.

    Two checks matter more than the usual cross-references:

    * every ``INVERTED_RISK`` key must be a real ``retention.RETENTION_CHURN_BANDS``
      member. A missing key contributes ``0.0`` here -- which reports *every*
      customer as maximally healthy -- and that is the most dangerous way this
      table can fail.
    * no band's ``action`` may escalate a customer out of ``no_data``, and
      ``critical`` must be the only band with an SLA, because an SLA nobody is
      accountable for is decoration.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        known = {str(item) for item in retention.RETENTION_CHURN_BANDS}
        for band in sorted(INVERTED_RISK):
            if band not in known:
                errors.append(
                    f"INVERTED_RISK names {band!r}, which is not a "
                    f"retention.RETENTION_CHURN_BANDS member. A missing key "
                    f"contributes 0.0, which reports every customer as healthy"
                )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"could not read retention.RETENTION_CHURN_BANDS: {exc}")

    ranks = [int(row["rank"]) for row in RELATIONSHIP_HEALTH_BANDS]
    if ranks != sorted(ranks):
        errors.append("RELATIONSHIP_HEALTH_BANDS ranks are not ascending")
    if len(set(ranks)) != len(ranks):
        errors.append("RELATIONSHIP_HEALTH_BANDS repeats a rank")

    bands = {str(row["band"]) for row in RELATIONSHIP_HEALTH_BANDS}
    for required in ("no_data", "critical", "at_risk", "healthy"):
        if required not in bands:
            errors.append(f"RELATIONSHIP_HEALTH_BANDS omits the {required!r} band")

    for row in RELATIONSHIP_HEALTH_BANDS:
        band = str(row["band"])
        if band == "no_data" and str(row.get("action") or "none") != "none":
            errors.append(
                f"{band} has action {row['action']!r}: escalating a customer we "
                "have never heard from manufactures an emergency out of an "
                "absence of data"
            )
        if band != "critical" and row.get("sla_hours") is not None:
            errors.append(
                f"{band} declares an SLA of {row['sla_hours']}h. critical is the "
                "only band with one; an SLA nobody is accountable for is decoration "
                "and it makes the one that matters look like the rest"
            )

    with_sla = [str(row["band"]) for row in RELATIONSHIP_HEALTH_BANDS if row.get("sla_hours")]
    if with_sla != ["critical"]:
        errors.append(f"exactly one band should carry an SLA; {with_sla} do")

    signal_ids = [str(row["signal_id"]) for row in HEALTH_SIGNALS]
    if len(set(signal_ids)) != len(signal_ids):
        errors.append("HEALTH_SIGNALS repeats a signal_id")
    for spec in HEALTH_SIGNALS:
        if not spec.get("why"):
            warnings.append(
                f"HEALTH_SIGNALS[{spec['signal_id']}] has no `why`; a weight a "
                "reader cannot question is one nobody will question"
            )
        if not spec.get("source"):
            errors.append(
                f"HEALTH_SIGNALS[{spec['signal_id']}] names no source, so it cannot "
                "be traced to the rows it came from"
            )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "bands": len(RELATIONSHIP_HEALTH_BANDS),
        "signals": len(HEALTH_SIGNALS),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(RELATIONSHIP_HEALTH_BANDS)} bands and {len(HEALTH_SIGNALS)} signals"
        ),
    }


def build_relationship_health_catalog() -> dict[str, Any]:
    """The tables, published, so the policy is reviewable without reading code."""
    return {
        "catalog_version": RELATIONSHIP_HEALTH_CATALOG_VERSION,
        "bands": [dict(row) for row in RELATIONSHIP_HEALTH_BANDS],
        "signals": [dict(row) for row in HEALTH_SIGNALS],
        "sla_bands": list(SLA_BANDS),
        "offer_window_bands": list(OFFER_WINDOW_BANDS),
        "tolerances": {
            "chase": CHASE_TOLERANCE,
            "open_complaint": OPEN_COMPLAINT_TOLERANCE,
            "dormancy_days_for_fatigue": DORMANCY_DAYS_FOR_FATIGUE,
            "dormancy_days_severe": DORMANCY_DAYS_SEVERE,
        },
        "inverted_risk": dict(INVERTED_RISK),
        "note": (
            "health is not a status and never becomes one. it is allowed to fall "
            "where loyalty_status does not, and the two are reported together so a "
            "customer who is Trusted and critical at once is visible -- that "
            "combination is the one that deserves the fastest response. the rollup "
            "is a triage queue, not the health of the book: it reads cheap rows "
            "rather than building N reports, so churn risk is reported as "
            "unmeasured instead of assumed good"
        ),
    }