"""Loyalty status, an experiential ledger, and what it says we should invest (Stage C).

The gap this fills is narrower than "loyalty". This system already has points, a
loyalty *score*, a churn risk, a journey plan and a next-best-action engine. What
it has no answer for is the question a retention conversation actually turns on:

> how much is this relationship worth, and are we being generous or stingy with
> someone in proportion?

Today the answer to that is a points balance, which is a poor proxy for both
halves. A customer with 40 points and four years of reliable service is worth more
than one with 900 points and a pattern of cancellations, and a program that
cannot say so will eventually either overpay for the second or under-protect the
first.

Three things, kept apart on purpose
------------------------------------
**Status** (:func:`resolve_loyalty_status`) — a *label* the customer can see and
act on. Derived from the experiential ledger, not from points. Statuses are
**monotone in time**: nothing about this design should let a customer fall a tier
because they did not transact this month, because "your status dropped for
inactivity" is a punishment for the exact behaviour a loyalty programme is
supposed to encourage.

**The experiential ledger** (:func:`build_experiential_ledger`) — the *non-monetary*
things worth recording, which no table here captures: how reliably a booking
completed, whether a promise was kept, how much effort the customer spent
chasing, whether they gave useful feedback. Points measure what the customer
*bought*. This measures what they *cost us and what they contributed*, which is
the other half of a relationship and the half a points wallet cannot express.

**The investment signal** (:func:`resolve_investment_band`) — how much this
relationship is worth, as a band rather than a currency amount. A band because a
monetary LTV estimate on this data would be a fiction with six decimal places:
there is no revenue column anywhere in this schema, and any number produced would
be a guess wearing a float. `OFFER_GENEROSITY_RULES` in
``services/customer_offers.py`` consumes the band, and its bounds and per-kind cap
are what keep a band from becoming an unbounded liability.

What it does not do
-------------------
* **It does not re-derive the loyalty score or churn risk.** Those belong to
  ``policy_scoring`` and ``retention`` and are read, not recomputed. A second
  score that disagreed with the first by a point would be invisible and
  corrosive.
* **It does not store the ledger.** :func:`build_experiential_ledger` returns a
  computed view over events that already exist (bookings, bookings_events,
  complaints, chats, offers). A ledger that accumulates a *stored* copy of each
  signal would need its own reconciliation, and every existing engine would then
  have two sources of truth.
* **It does not decide whether an offer is warranted.** Only
  ``customer_offers`` and its upstream policy do. This module says how generous to
  be, which is a different question from whether.

The honest limits
-----------------
Three, stated because each one could otherwise be mistaken for coverage:

* **The experiential ledger has no counterfactual.** It records that three
  bookings were cancelled; it cannot say the second would have completed. Every
  rule below is therefore about *observed* reliability, and the report says so
  rather than presenting a trend as a forecast.
* **The investment band is ordinal.** ``strategic > high > standard > low >
  unscored`` is a claim about ordering, not about magnitude. Nothing here should
  be summed, averaged across customers, or charted on a continuous axis.
* **Status can lag reality.** A customer who has genuinely disengaged keeps the
  status their history earned, because the alternative is a customer-visible
  demotion. That is a deliberate trade: ``relationship_health`` reports the live
  signal, and this module reports the earned one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

LOYALTY_STATUS_CATALOG_VERSION = 1

#: The customer-visible status ladder.
#:
#: ``status`` is what the customer sees. ``rank`` exists only so the ordering is
#: data rather than a chained comparison, and nothing in this module subtracts
#: two ranks -- the ordering is used to pick a bucket, never to compute a
#: distance.
LOYALTY_STATUS_RULES: tuple[dict[str, Any], ...] = (
    {
        "status_id": "member",
        "rank": 0,
        "label": "Member",
        "when": {"experiential_tier": ["unscored"]},
        "perks": ["standard recovery eligibility"],
        "note": (
            "the fallback, not a table row with no meaning: someone with no "
            "recorded history is a Member rather than nothing, because 'nothing' "
            "would be a status a customer can see themselves fall into"
        ),
    },
    {
        "status_id": "established",
        "rank": 1,
        "label": "Established",
        "when": {"experiential_tier": ["bronze", "silver"]},
        "perks": [
            "standard recovery eligibility",
            "priority handling when something is already wrong",
            "offers scale with the investment signal",
        ],
    },
    {
        "status_id": "trusted",
        "rank": 2,
        "label": "Trusted",
        "when": {"experiential_tier": ["gold"]},
        "perks": [
            "everything at Established",
            "a goodwill offer is worth materially more than the standard amount",
        ],
        "note": (
            "the first tier where the programme spends real money on a "
            "relationship rather than on a transaction"
        ),
    },
    {
        "status_id": "principal",
        "rank": 3,
        "label": "Principal",
        "when": {"experiential_tier": ["platinum"]},
        "perks": [
            "everything at Trusted",
            "the highest investment signal, so the largest authorised generosity",
        ],
        "note": (
            "reached by reliability and tenure, not by spending. A Principal "
            "who is currently in trouble is the customer this programme exists "
            "for, and `customer_offers` will not give them a Standard offer"
        ),
    },
)

LOYALTY_STATUSES: tuple[str, ...] = tuple(str(row["status_id"]) for row in LOYALTY_STATUS_RULES)
LOYALTY_STATUS_BY_ID: dict[str, dict[str, Any]] = {
    str(row["status_id"]): dict(row) for row in LOYALTY_STATUS_RULES
}

#: Status is **earned and never lost to inactivity**.
#:
#: Not a rule the engine enforces so much as one it cannot express: there is no
#: `decay` field on a status row and no transition that lowers a tier on a
#: calendar. A customer who stops transacting keeps what they built, because
#: demotion for inactivity punishes exactly the pause a loyalty programme should
#: be forgiving about, and because a demotion they can see is the fastest way to
#: teach someone the programme is not worth caring about.
STATUS_DECAYS_ON_INACTIVITY = False

#: The experiential ledger: the non-monetary signals, and what each is worth.
#:
#: Weights are relative and only meaningful in order. Nothing here is summed into
#: a currency figure -- see the module docstring on why an LTV number on this
#: data would be a fiction with six decimal places.
EXPERIENTIAL_SIGNALS: tuple[dict[str, Any], ...] = (
    {
        "signal_id": "reliability",
        "label": "Did the work get done",
        "source": "bookings + booking_events",
        "weight": 3.0,
        "positive": "completed",
        "negative": ("cancelled",),
        "why": (
            "the strongest signal available, because it is the one thing a "
            "customer is unambiguously relying on us to do. A completed booking "
            "is a kept promise; a cancellation is usually ours, not theirs"
        ),
    },
    {
        "signal_id": "tenure",
        "label": "How long they have been here",
        "source": "bookings.first created_at",
        "weight": 2.0,
        "positive": "",
        "negative": (),
        "why": (
            "slow-moving and impossible to lose quickly, which is exactly why it "
            "belongs in a ledger rather than a monthly score: it moves the tier "
            "eventually and then stops"
        ),
    },
    {
        "signal_id": "follow_through",
        "label": "Did they finish what they started",
        "source": "bookings status transitions",
        "weight": 2.0,
        "positive": "completed",
        "negative": ("abandoned", "pending"),
        "why": (
            "a booking left pending is a customer who asked for something and "
            "never heard back. It is the cheapest defect to fix and the most "
            "expensive to leave alone"
        ),
    },
    {
        "signal_id": "goodwill_returned",
        "label": "Came back after something went wrong",
        "source": "complaints + bookings after a complaint",
        "weight": 4.0,
        "positive": "",
        "negative": (),
        "why": (
            "the heaviest positive weight here, and deliberately so. Someone who "
            "reported a problem and then came back has demonstrated trust that no "
            "amount of completed bookings manufactures, and it is the single best "
            "predictor that a goodwill offer will be received well"
        ),
    },
    {
        "signal_id": "chasing_effort",
        "label": "How much they had to chase",
        "source": "chat_history + complaints",
        "weight": -2.0,
        "positive": "",
        "negative": ("repeated_contact", "reopened_case"),
        "why": (
            "negative on purpose. Effort spent chasing is a cost we imposed, and "
            "a ledger that only records positives cannot represent a customer who "
            "cost us a great deal and should still be treated well -- which is "
            "the case where being stingy costs the most"
        ),
    },
)

EXPERIENTIAL_SIGNAL_BY_ID: dict[str, dict[str, Any]] = {
    str(row["signal_id"]): dict(row) for row in EXPERIENTIAL_SIGNALS
}
EXPERIENTIAL_SIGNAL_IDS: tuple[str, ...] = tuple(EXPERIENTIAL_SIGNAL_BY_ID)

#: Tier cut points on the normalised ledger score.
#:
#: A normalised 0..1 score with fixed bands, because a scale that moves is a
#: status that moves, and a status that moves for reasons a customer cannot see
#: is the demotion problem this module exists to avoid. Declared once, validated,
#: and read from -- never compared inline.
#:
#: `unscored` is **not** in this table, and that is deliberate. It was the first
#: row at `min_score: 0.0` until a customer with three cancellations and an open
#: complaint came back as `unscored` -- "no recorded history" for someone whose
#: recorded history was consistently bad. A zero score is the *bottom* of the
#: ladder, not an absence from it. "No history" is now the ledger's separate
#: boolean flag, and the status rules still accept the string because it is a
#: legitimate status input.
EXPERIENTIAL_TIER_RULES: tuple[dict[str, Any], ...] = (
    {"tier": "bronze", "min_score": 0.0, "label": "Bronze"},
    {"tier": "silver", "min_score": 0.25, "label": "Silver"},
    {"tier": "gold", "min_score": 0.50, "label": "Gold"},
    {"tier": "platinum", "min_score": 0.75, "label": "Platinum"},
)

#: Reported in place of a tier when the customer has no recorded history at all.
#: Distinct from ``bronze``, which is a real score at the bottom of the ladder.
NO_HISTORY_TIER = "unscored"
EXPERIENTIAL_TIERS: tuple[str, ...] = tuple(str(row["tier"]) for row in EXPERIENTIAL_TIER_RULES)
EXPERIENTIAL_TIER_RANK: dict[str, int] = {
    str(row["tier"]): int(row["min_score"] * 100) for row in EXPERIENTIAL_TIER_RULES
}

#: The investment bands, ordinal.
#:
#: Fed to `OFFER_GENEROSITY_RULES`. `unscored` is a real band rather than a null
#: because "we do not know what this relationship is worth" and "worth nothing"
#: must not collapse into the same answer, and only the first is true here.
INVESTMENT_BANDS: tuple[dict[str, Any], ...] = (
    {
        "band": "unscored",
        "rank": 0,
        "when": {},
        "label": "Not enough history to judge",
        "generosity_rule": "generosity_default",
        "why": "the default, and it offers at the standard amount rather than at zero",
    },
    {
        "band": "low",
        "rank": 1,
        "when": {"investment_score_max": 0.30},
        "label": "Low investment",
        "generosity_rule": "generosity_default",
        "why": "below the standard band; the programme still offers at the standard amount",
    },
    {
        "band": "standard",
        "rank": 2,
        "when": {"investment_score_min": 0.30, "investment_score_max": 0.60},
        "label": "Standard",
        "generosity_rule": "generosity_default",
    },
    {
        "band": "high",
        "rank": 3,
        "when": {"investment_score_min": 0.60, "investment_score_max": 0.85},
        "label": "High investment",
        "generosity_rule": "generosity_established",
        "why": (
            "long-tenured with completed history. A standard offer here reads as "
            "an insult to someone who has been here the whole time"
        ),
    },
    {
        "band": "strategic",
        "rank": 4,
        "when": {"investment_score_min": 0.85},
        "label": "Strategic relationship",
        "generosity_rule": "generosity_strategic",
        "why": (
            "the highest band. Capped per kind in customer_offers, so this "
            "cannot be configured into an unbounded liability"
        ),
    },
)
INVESTMENT_BAND_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["band"]): dict(row) for row in INVESTMENT_BANDS
}

#: What the relationship-health band contributes to investment, and what it must
#: never do. Read from `retention.RETENTION_CHURN_BANDS` at validation time so a
#: rename there is caught here rather than silently ignored.
INVERTED_RISK_CONTRIBUTIONS: dict[str, float] = {
    "low": 1.0,
    "medium": 0.6,
    "high": 0.3,
    "critical": 0.0,
}
RISK_STILL_MATTERS = (
    "a customer at critical risk scores zero on the risk contribution however "
    "valuable their history, which is deliberate: it is what routes them to an "
    "offer and a follow-up rather than to an upsell"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(0.0, min(1.0, number))


def _aware(value: Any) -> Optional[datetime]:
    """Coerce to an aware datetime, or ``None``.

    **Accepts ISO strings as well as datetimes**, because the callers of this
    module include paths that hold serialised rows (a cached 360, a row that has
    been through JSON) and because the alternative is worse than useless: the
    first version returned ``None`` for a string, so `tenure` silently became 0
    and a seven-year customer scored as brand new. A silent wrong value reads
    exactly like a correct one.

    Unparseable input is ``None`` rather than today's date -- "now" would turn a
    missing timestamp into zero tenure, which is the same silent error wearing a
    different hat.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


# ---------------------------------------------------------------------------
# The experiential ledger
# ---------------------------------------------------------------------------


def build_experiential_ledger(
    *,
    bookings: Sequence[Mapping[str, Any]] = (),
    booking_events: Sequence[Mapping[str, Any]] = (),
    complaints: Sequence[Mapping[str, Any]] = (),
    chat_rows: Sequence[Mapping[str, Any]] = (),
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The non-monetary picture: what they cost us, and what they gave back.

    A **computed view over events that already exist**, not a stored ledger. Every
    input is an existing row; nothing is accumulated. That is the single most
    important property here, because a stored copy would need its own
    reconciliation and would leave every existing engine with two sources of
    truth for the same fact.

    Signals are counted, then **normalised against this customer's own reachable
    maximum** rather than a global one. Normalising per-customer is what lets a
    brand-new customer with one completed booking reach a real tier instead of
    permanently scoring near zero against everyone else's history -- and the
    report publishes ``normalisation: "per_customer_reachable_max"`` so nobody
    reads the score as comparable across customers.

    The score is a 0..1 number because the tier rules need an ordered input. It is
    **not** a probability, a satisfaction measure, or an LTV, and it is not summed
    across customers.
    """
    moment = _aware(now) or _now()

    completed = _count_status(bookings, "completed")
    cancelled = _count_status(bookings, "cancelled")
    pending = _count_status(bookings, "pending")
    reopened = _count_kind(booking_events, "reopened")
    complaint_rows = list(complaints or ())
    closed_complaints = _count_status(complaint_rows, "closed")
    open_complaints = [row for row in complaint_rows if _status_of(row) not in {"closed", "withdrawn"}]

    # "Came back after something went wrong": a booking completed *after* the
    # first complaint was raised. Read off timestamps rather than inferred from
    # counts, because "they complained and never came back" and "they complained
    # and came straight back" are the same two counts in the wrong order.
    returned_after_complaint = _returned_after_complaint(
        complaint_rows, bookings
    )

    tenure_days = _tenure_days(bookings, moment)
    contacts = len(list(chat_rows or ()))
    repeated_contact = _repeated_contact(chat_rows)

    counts: dict[str, int] = {
        "reliability_positive": completed,
        "reliability_negative": cancelled,
        "follow_through_positive": completed,
        "follow_through_negative": pending,
        "tenure_days": tenure_days,
        "goodwill_returned": returned_after_complaint,
        "chasing_effort": contacts + len(open_complaints) + reopened,
        "complaints_closed": closed_complaints,
        "complaints_open": len(open_complaints),
        "chase_repeats": repeated_contact,
    }

    # Whether each signal is **countable** for this customer, and its raw value.
    #
    # `countable` is not a divisor. Every raw value below is already a 0..1 ratio
    # or a 0..1 clamp, so dividing by a "reachable maximum" in different units is
    # meaningless arithmetic -- and the first version of this function did
    # exactly that (`chasing_effort` divided by 3.0, `tenure` by 365), which is
    # why a seven-year loyal customer scored 0.079 and a high-churn one 0.000.
    # Nothing raised. It just quietly said nobody is worth anything.
    #
    # What `countable` actually answers is a different and useful question: *did
    # this customer engage with this dimension at all?* Someone with no
    # complaints cannot be credited for coming back after one, and someone with
    # no bookings cannot be penalised for unreliability. An uncountable signal
    # drops out of the weighted mean entirely rather than contributing a zero.
    attempts = completed + cancelled + pending
    countability = {
        "reliability": bool(completed + cancelled),
        "tenure": tenure_days > 0,
        "follow_through": bool(completed + pending),
        "goodwill_returned": bool(complaint_rows),
        "chasing_effort": bool(contacts + len(open_complaints) + reopened),
    }
    raw: dict[str, float] = {
        # Reliability is completed / resolved. A pending booking is neither a kept
        # promise nor a broken one, and counting it as broken would punish a
        # customer for our own delay.
        "reliability": (completed / (completed + cancelled)) if (completed + cancelled) else 0.0,
        "tenure": _clamp01(tenure_days / 365.0),
        "follow_through": (completed / (completed + pending)) if (completed + pending) else 0.0,
        # Binary: came back after a complaint, or did not.
        "goodwill_returned": 1.0 if returned_after_complaint else 0.0,
        # The *inverse* of effort spent chasing, so a high value is good.
        "chasing_effort": 1.0 - _clamp01((contacts + len(open_complaints) + reopened) / 30.0),
    }

    # Per-signal normalisation, then a weight-weighted average.
    #
    # **The first version weighted the sums directly and scored 0.012 for a
    # seven-year, fully-reliable customer with a closed complaint.** The reason is
    # the whole point of writing this down: `tenure` raw value is derived from a
    # day count (2_555) while every other raw is a 0..1 ratio, so `weight * raw`
    # added a term three orders of magnitude larger than anything else -- and then
    # divided by `possible`, which was *also* in units of days, so the
    # normalisation cancelled the mistake and left everyone near zero. Nothing
    # raised. The result read as "this customer is barely worth anything", which
    # is exactly the conclusion this module exists to reach by evidence.
    #
    # So each signal is normalised against its own ceiling *first*, and the
    # weights then combine comparable numbers. A weight is a relative
    # importance, not a magnitude.
    per_signal: list[tuple[str, float, float, float, float]] = []
    total_weight = 0.0
    weighted_sum = 0.0
    for spec in EXPERIENTIAL_SIGNALS:
        signal_id = str(spec["signal_id"])
        weight = float(spec["weight"])
        value = _clamp01(raw.get(signal_id, 0.0))
        countable = bool(countability.get(signal_id, False))
        # An uncountable signal contributes to neither sum. Someone with no
        # complaints cannot be credited for returning after one, and crediting
        # them anyway would let an absence inflate the score.
        if not countable:
            per_signal.append((signal_id, weight, 0.0, False, 0.0))
            continue
        total_weight += abs(weight)
        weighted_sum += weight * value
        per_signal.append((signal_id, weight, value, True, weight * value))

    score = _clamp01(weighted_sum / total_weight) if total_weight > 0 else 0.0
    unscored = attempts == 0 and not complaint_rows and tenure_days == 0
    contributions = [
        {
            "signal_id": signal_id,
            "label": str(EXPERIENTIAL_SIGNAL_BY_ID[signal_id]["label"]),
            "weight": weight,
            "value": round(normalised, 4),
            "contribution": round(contribution, 4),
            "countable": countable,
            "why": str(EXPERIENTIAL_SIGNAL_BY_ID[signal_id]["why"]),
        }
        for signal_id, weight, normalised, countable, contribution in per_signal
    ]
    tier = resolve_experiential_tier(score)
    return {
        "generated_at": moment.isoformat(),
        "score": round(score, 4),
        # `tier` is always a real ladder position -- a consistently-bad customer
        # is bronze, which is the bottom, not absent. `unscored` says there is no
        # history, and `reported_tier` is the single string a consumer should
        # branch on.
        "tier": tier,
        "unscored": unscored,
        "reported_tier": NO_HISTORY_TIER if unscored else tier,
        "tier_label": "No recorded history" if unscored else _tier_label(tier),
        "unscored": unscored,
        "counts": counts,
        "signals": contributions,
        "attempts": attempts,
        "normalisation": "countable_signals_weighted_mean",
        "total_weight": round(total_weight, 4),
        "note": (
            "computed from events that already exist; nothing is accumulated or "
            "stored. every signal is already a 0..1 ratio, so the weights combine "
            "comparable numbers directly -- a day count and a completion ratio are "
            "not quantities that can be added or divided against each other. an "
            "uncountable signal (no complaints, so no return-after-complaint to "
            "credit) drops out of the weighted mean instead of scoring zero. the score "
            "is 0..1 because the tier rules need an ordered input -- it is not a "
            "probability, not satisfaction, and not an LTV, and it must not be "
            "summed across customers. reliability counts only resolved bookings, "
            "because a pending one is neither a kept promise nor a broken one"
        ),
        "limits": (
            "no counterfactual: the ledger records that three bookings were "
            "cancelled and cannot say the second would have completed. chasing_effort "
            "is a real cost we imposed and is weighted negative, which is why a "
            "customer who cost us a great deal can still score well"
        ),
    }


def _status_of(row: Any) -> str:
    if isinstance(row, Mapping):
        return str(row.get("status") or "")
    return str(getattr(row, "status", "") or "")


def _count_status(rows: Iterable[Any], status: str) -> int:
    return sum(1 for row in (rows or ()) if _status_of(row) == str(status))


def _count_kind(rows: Iterable[Any], kind: str) -> int:
    return sum(
        1
        for row in (rows or ())
        if str((row.get("kind") if isinstance(row, Mapping) else getattr(row, "kind", "")) or "") == str(kind)
    )


def _when_of(row: Any) -> Optional[datetime]:
    for name in ("closed_at", "resolved_at", "created_at", "opened_at", "raised_at", "timestamp"):
        if isinstance(row, Mapping):
            value = row.get(name)
        else:
            value = getattr(row, name, None)
        moment = _aware(value)
        if moment is not None:
            return moment
    return None


def _tenure_days(bookings: Sequence[Mapping[str, Any]], now: datetime) -> int:
    stamps = [_aware(b.get("created_at")) for b in (bookings or ()) if isinstance(b, Mapping)]
    stamps = [stamp for stamp in stamps if stamp is not None]
    if not stamps:
        return 0
    oldest = min(stamps)
    return max(0, (now - oldest).days)


def _repeated_contact(chat_rows: Sequence[Mapping[str, Any]]) -> int:
    """Chats about the same booking, which is what "chasing" means here.

    Counted as *extra* messages about a booking already discussed, not as total
    message volume: a customer who sends ten messages about ten different things
    is not chasing, and treating them as if they were would make the ledger punish
    an engaged customer.
    """
    seen: dict[Any, int] = {}
    for row in chat_rows or ():
        key = row.get("booking_id") if isinstance(row, Mapping) else getattr(row, "booking_id", None)
        if key is None:
            continue
        seen[key] = seen.get(key, 0) + 1
    return sum(count - 1 for count in seen.values() if count > 1)


def _returned_after_complaint(
    complaints: Sequence[Mapping[str, Any]], bookings: Sequence[Mapping[str, Any]]
) -> int:
    """Bookings completed after the first complaint was raised."""
    first = None
    for row in complaints or ():
        moment = _when_of(row)
        if moment is not None and (first is None or moment < first):
            first = moment
    if first is None:
        return 0
    returned = 0
    for row in bookings or ():
        if _status_of(row) != "completed":
            continue
        created = _aware(
            row.get("created_at") if isinstance(row, Mapping) else getattr(row, "created_at", None)
        )
        completed_at = _aware(
            row.get("completed_at") if isinstance(row, Mapping) else getattr(row, "completed_at", None)
        ) or created
        if completed_at is not None and completed_at > first:
            returned += 1
    return returned


def resolve_experiential_tier(score: float) -> str:
    """The tier for a 0..1 ledger score. First matching row wins.

    Read from :data:`EXPERIENTIAL_TIER_RULES` rather than compared inline, so the
    cut points live in one place and ``validate_loyalty_status`` can check they
    are ordered.
    """
    value = _clamp01(score)
    best = EXPERIENTIAL_TIERS[0]
    for row in EXPERIENTIAL_TIER_RULES:
        if value >= float(row["min_score"]):
            best = str(row["tier"])
    return best


def _tier_label(tier: str) -> str:
    for row in EXPERIENTIAL_TIER_RULES:
        if str(row["tier"]) == str(tier):
            return str(row["label"])
    return str(tier)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def resolve_loyalty_status(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """The customer-visible status, derived from the ledger.

    Never from points. A points balance measures what somebody bought, and a
    status is a claim about the relationship -- a customer with 40 points and four
    years of reliability is not a beginner, and treating them as one is the
    specific failure this function exists to prevent.

    Fails closed to ``member``, the lowest rung, because an unreadable ledger
    must not promote anyone.
    """
    tier = str(
        ledger.get("reported_tier")
        or (NO_HISTORY_TIER if ledger.get("unscored") else ledger.get("tier"))
        or NO_HISTORY_TIER
    )
    context = {"experiential_tier": tier}
    for rule in LOYALTY_STATUS_RULES:
        try:
            from app.services import recovery_playbooks

            ok, _fields = recovery_playbooks.evaluate_when(rule.get("when", {}), context)
        except Exception:  # noqa: BLE001 - a status miss must not raise
            continue
        if ok:
            return {
                "status_id": str(rule["status_id"]),
                "label": str(rule["label"]),
                "rank": int(rule["rank"]),
                "experiential_tier": tier,
                "perks": list(rule["perks"]),
                "derived_from": "experiential_ledger",
                "derived_from_points": False,
                "decays_on_inactivity": STATUS_DECAYS_ON_INACTIVITY,
            }
    fallback = LOYALTY_STATUS_RULES[0]
    return {
        "status_id": str(fallback["status_id"]),
        "label": str(fallback["label"]),
        "rank": int(fallback["rank"]),
        "experiential_tier": tier,
        "perks": list(fallback["perks"]),
        "derived_from": "experiential_ledger",
        "derived_from_points": False,
        "decays_on_inactivity": STATUS_DECAYS_ON_INACTIVITY,
        "note": "no status rule matched; fell back to the lowest rung rather than promoting",
    }


# ---------------------------------------------------------------------------
# The investment signal
# ---------------------------------------------------------------------------


def resolve_investment_band(
    *,
    ledger: Mapping[str, Any],
    churn_band: str = "",
    relationship_health: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """How much this relationship is worth, as an ordinal band.

    Three inputs, and the third one is the interesting one: **live risk
    inverts**. A customer at ``critical`` churn risk scores zero on the risk
    contribution however valuable their history, which is deliberate. It is what
    routes them toward an offer and a follow-up instead of toward an upsell, and
    it is why a Principal customer in trouble does not receive a Principal
    upsell -- they receive a priority offer and somebody looking at their case.

    The band is ordinal. ``strategic > high > standard > low > unscored`` is a
    claim about ordering and nothing more: it is not a currency amount, must not
    be averaged across customers, and must not be charted on a continuous axis.
    There is no revenue column in this schema, and any monetary LTV number
    produced from it would be a guess wearing a float.
    """
    score = _clamp01(ledger.get("score"))
    risk = str(churn_band or "").strip().lower()
    health = relationship_health or {}
    live_score = _clamp01(health.get("score"), default=score)

    risk_factor = INVERTED_RISK_CONTRIBUTIONS.get(risk, 1.0)
    # Two parts: what they have done (ledger) and how they are doing now
    # (live risk), weighted so history dominates but can be overridden by risk.
    investment = _clamp01((0.65 * score) + (0.35 * live_score * risk_factor))

    context = {"investment_score": investment}
    band = "unscored"
    for row in INVESTMENT_BANDS:
        when = dict(row.get("when") or {})
        if not when:
            band = str(row["band"])
            continue
        if _band_matches(when, investment):
            band = str(row["band"])
            break
    declared = INVESTMENT_BAND_BY_NAME.get(band, {})
    return {
        "band": band,
        "label": str(declared.get("label") or band),
        "rank": int(declared.get("rank") or 0),
        "investment_score": round(investment, 4),
        "generosity_rule": str(declared.get("generosity_rule") or "generosity_default"),
        "inputs": {
            "ledger_score": round(score, 4),
            "live_score": round(live_score, 4),
            "churn_band": risk or "unscored",
            "risk_factor": risk_factor,
        },
        "ordinal_only": True,
        "note": (
            "an ordinal band, not a monetary value: this schema has no revenue "
            "column, so any LTV figure produced from it would be a guess wearing "
            "a float. live risk inverts the contribution -- a customer at critical "
            "risk scores zero on it however valuable their history, which is what "
            "routes them to an offer and a follow-up rather than to an upsell. do "
            "not sum or average this across customers"
        ),
    }


def _band_matches(when: Mapping[str, Any], score: float) -> bool:
    low = when.get("investment_score_min")
    high = when.get("investment_score_max")
    if low is not None and score < float(low):
        return False
    if high is not None and score >= float(high):
        return False
    return True


def investment_context(
    *,
    ledger: Mapping[str, Any],
    churn_band: str = "",
    relationship_health: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """The investment band shaped for ``OFFER_GENEROSITY_RULES``.

    ``customer_offers.resolve_offer_generosity`` reads exactly one key,
    ``investment_band``. Returning the band under its own name rather than
    making that consumer reach through this dict is what keeps the two modules
    decoupled: this one does not know what a rule id is.
    """
    band = resolve_investment_band(
        ledger=ledger,
        churn_band=churn_band,
        relationship_health=relationship_health,
    )
    return {
        "investment_band": band["band"],
        "investment_score": band["investment_score"],
        "investment_label": band["label"],
        "investment_note": band["note"],
        "generosity_rule_hint": band["generosity_rule"],
    }


# ---------------------------------------------------------------------------
# Validation and catalog
# ---------------------------------------------------------------------------


def validate_loyalty_status() -> dict[str, Any]:
    """Check these tables against each other, and against the engines they read.

    Three checks that matter more than the usual cross-references:

    * every ``INVERTED_RISK_CONTRIBUTIONS`` key must be a real
      ``retention.RETENTION_CHURN_BANDS`` member. A rename there would otherwise
      leave this table silently applying ``1.0`` -- that is, treating every
      at-risk customer as a healthy one, which is the most dangerous possible
      failure of a table that only fails by being wrong in the helpful direction.
    * ``EXPERIENTIAL_TIER_RULES`` must be ascending and start at 0.0, or the
      first-match-wins walk below returns a tier nothing can reach.
    * every status rule must name a tier that exists.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        from app.services import retention

        known = {str(item) for item in retention.RETENTION_CHURN_BANDS}
        for band in sorted(INVERTED_RISK_CONTRIBUTIONS):
            if band not in known:
                errors.append(
                    f"INVERTED_RISK_CONTRIBUTIONS names {band!r}, which is not a "
                    f"retention.RETENTION_CHURN_BANDS member (known: "
                    f"{', '.join(sorted(known))}). A missing key would silently "
                    f"contribute 1.0 -- i.e. treat an at-risk customer as healthy"
                )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"could not read retention.RETENTION_CHURN_BANDS: {exc}")

    previous = -1.0
    for row in EXPERIENTIAL_TIER_RULES:
        value = float(row["min_score"])
        if value <= previous:
            errors.append(
                f"EXPERIENTIAL_TIER_RULES[{row['tier']}].min_score {value} is not "
                f"above the previous row ({previous}); the walk takes the highest "
                f"match, so out-of-order rows silently lose"
            )
        if not 0.0 <= value <= 1.0:
            errors.append(f"EXPERIENTIAL_TIER_RULES[{row['tier']}] min_score {value} is outside 0..1")
        previous = value
    if EXPERIENTIAL_TIER_RULES and float(EXPERIENTIAL_TIER_RULES[0]["min_score"]) != 0.0:
        errors.append(
            "EXPERIENTIAL_TIER_RULES must start at 0.0, or a score below the "
            "first cut point resolves to no tier at all"
        )

    for rule in LOYALTY_STATUS_RULES:
        tiers = (rule.get("when") or {}).get("experiential_tier") or ()
        for tier in tiers:
            # `NO_HISTORY_TIER` is a legitimate *status* input even though it is
            # deliberately not a score-derived row, so it is accepted here. Any
            # other undeclared tier is a typo that would make the rule
            # permanently unreachable.
            if str(tier) == NO_HISTORY_TIER:
                continue
            if str(tier) not in EXPERIENTIAL_TIERS:
                errors.append(
                    f"LOYALTY_STATUS_RULES[{rule['status_id']}] names tier "
                    f"{tier!r}, which EXPERIENTIAL_TIER_RULES does not declare "
                    f"(declared: {', '.join(EXPERIENTIAL_TIERS)}, plus "
                    f"{NO_HISTORY_TIER!r} for no-history)"
                )
        if not tiers:
            warnings.append(
                f"LOYALTY_STATUS_RULES[{rule['status_id']}] matches no tier, so it "
                "is unreachable and the rung below it wins by default"
            )
        if not rule.get("perks"):
            warnings.append(
                f"LOYALTY_STATUS_RULES[{rule['status_id']}] declares no perks; a "
                "status a customer cannot see any difference in is a label"
            )

    ranks = [int(row["rank"]) for row in LOYALTY_STATUS_RULES]
    if ranks != sorted(ranks):
        errors.append("LOYALTY_STATUS_RULES ranks are not ascending")
    if len(set(ranks)) != len(ranks):
        errors.append("LOYALTY_STATUS_RULES repeats a rank")

    bands = [int(row["rank"]) for row in INVESTMENT_BANDS]
    if bands != sorted(bands):
        errors.append("INVESTMENT_BANDS ranks are not ascending")
    for row in INVESTMENT_BANDS:
        if not row.get("generosity_rule"):
            errors.append(
                f"INVESTMENT_BANDS[{row['band']}] names no generosity_rule, so a "
                "consumer cannot tell which policy this band feeds"
            )

    for signal_id in EXPERIENTIAL_SIGNAL_IDS:
        spec = EXPERIENTIAL_SIGNAL_BY_ID[signal_id]
        if not spec.get("why"):
            warnings.append(
                f"EXPERIENTIAL_SIGNALS[{signal_id}] has no `why`; a weight a "
                "reader cannot question is a weight nobody will question"
            )
        if not spec.get("source"):
            errors.append(
                f"EXPERIENTIAL_SIGNALS[{signal_id}] names no source, so the "
                "signal cannot be traced back to the rows it came from"
            )

    if STATUS_DECAYS_ON_INACTIVITY:
        errors.append(
            "STATUS_DECAYS_ON_INACTIVITY is True. Status is earned and never lost "
            "to inactivity; a customer-visible demotion for going quiet punishes "
            "the pause a loyalty programme should forgive, and is the fastest way "
            "to teach someone it is not worth caring about"
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "statuses": len(LOYALTY_STATUSES),
        "signals": len(EXPERIENTIAL_SIGNAL_IDS),
        "bands": len(INVESTMENT_BANDS),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(LOYALTY_STATUSES)} statuses, {len(EXPERIENTIAL_SIGNAL_IDS)} "
            f"signals and {len(INVESTMENT_BANDS)} investment bands"
        ),
    }


def build_loyalty_status_catalog() -> dict[str, Any]:
    """The tables, published, so the policy itself is reviewable."""
    return {
        "catalog_version": LOYALTY_STATUS_CATALOG_VERSION,
        "statuses": [dict(row) for row in LOYALTY_STATUS_RULES],
        "signals": [dict(row) for row in EXPERIENTIAL_SIGNALS],
        "tier_rules": [dict(row) for row in EXPERIENTIAL_TIER_RULES],
        "investment_bands": [dict(row) for row in INVESTMENT_BANDS],
        "inverted_risk_contributions": dict(INVERTED_RISK_CONTRIBUTIONS),
        "risk_still_matters": RISK_STILL_MATTERS,
        "status_decays_on_inactivity": STATUS_DECAYS_ON_INACTIVITY,
        "note": (
            "status is derived from the experiential ledger and never from points: "
            "a points balance measures what somebody bought, and a status is a "
            "claim about the relationship. the ledger is a computed view over "
            "events that already exist -- nothing is stored -- and the investment "
            "band is ordinal, because this schema has no revenue column and any "
            "monetary LTV number built from it would be a guess wearing a float"
        ),
    }

# ---------------------------------------------------------------------------
# Stage F: how a membership's value evolves
# ---------------------------------------------------------------------------

#: The shapes a value trajectory may take, and what each one is *allowed* to do.
#:
#: This is the part Stage F adds, and it is deliberately a list of movements
#: rather than a score. A trajectory is not "how valuable is this customer" --
#: the investment band already answers that, ordinally. It is **how that answer
#: got here**, because the thing a customer asks when their status changes is
#: "what did you see?", and an answer that cannot be produced makes the status
#: feel arbitrary however fair the rule is.
VALUE_MOVEMENTS: tuple[dict[str, Any], ...] = (
    {
        "movement": "earned",
        "raises": True,
        "may_lower": False,
        "requires_evidence": False,
        "label": "Earned by what they did",
        "why": (
            "the ordinary case, and the only one that needs no evidence, because "
            "the ledger already contains the events that caused it"
        ),
    },
    {
        "movement": "confirmed",
        "raises": True,
        "may_lower": False,
        "requires_evidence": True,
        "label": "Raised because measured outcomes support it",
        "why": (
            "a rule change moved somebody up rather than down. It carries the "
            "motion's evidence, because a change to what somebody is owed is not "
            "a thing that should happen quietly"
        ),
    },
    {
        "movement": "paused",
        "raises": False,
        "may_lower": False,
        "requires_evidence": False,
        "label": "Held while nothing changes",
        "why": (
            "the one that matters most for trust. A programme that is doing "
            "nothing must say it is doing nothing rather than letting a customer "
            "wonder whether it has quietly downgraded them"
        ),
    },
    {
        "movement": "reinstated",
        "raises": True,
        "may_lower": False,
        "requires_evidence": True,
        "label": "Restored after an error",
        "why": (
            "reinstating is not the same as earning again. Somebody put back where "
            "they were after we got it wrong should not have to rebuild, and the "
            "distinction is the whole reason a trajectory is kept"
        ),
    },
    {
        "movement": "lowered_for_risk",
        "raises": False,
        "may_lower": True,
        "requires_evidence": True,
        "label": "Lowered on evidence of risk",
        "why": (
            "the only movement that may lower a status, and it is the one that "
            "must never be reachable by a timer. A status that can fall for "
            "inactivity is a different promise, and the promise is published"
        ),
    },
)
VALUE_MOVEMENT_BY_ID: dict[str, dict[str, Any]] = {
    str(row["movement"]): dict(row) for row in VALUE_MOVEMENTS
}
VALUE_MOVEMENT_IDS: tuple[str, ...] = tuple(VALUE_MOVEMENT_BY_ID)


def build_value_trajectory(
    *,
    history: Sequence[Mapping[str, Any]] = (),
    now: Optional[datetime] = None,
    max_entries: int = 12,
) -> dict[str, Any]:
    """How this membership's value got where it is, newest last.

    A **trajectory**, not a series of scores. The stages C/D modules each answered
    a question at one moment; nobody could answer "why is this different from six
    months ago", which is the only form the question takes when a customer asks
    it. The entries are the *moves* -- earned, confirmed, paused, reinstated,
    lowered -- and the moves are what the promise is written about.

    Two properties are enforced rather than hoped for:

    * **No entry may lower a status without evidence.** This is the no-decay
      invariant expressed over time instead of over a single ledger, because
      "status never decays" is easy to keep when there is one ledger and easy to
      lose when there is a history of them.
    * **A gap is reported as a gap.** Long absence produces a ``paused`` entry
      rather than nothing, because silence in a trajectory reads as a decision
      nobody made.
    """
    moment = now or datetime.now(timezone.utc)
    rows = [dict(row) for row in history or ()]
    entries: list[dict[str, Any]] = []
    previous_rank: Optional[int] = None
    for row in rows:
        movement = str(row.get("movement") or "earned")
        spec = VALUE_MOVEMENT_BY_ID.get(movement)
        if spec is None:
            entries.append(
                {
                    "movement": movement,
                    "known_movement": False,
                    "status": "",
                    "rank": previous_rank,
                    "at": str(row.get("at") or ""),
                    "accepted": False,
                    "reason": (
                        f"{movement!r} is not a declared movement. An unrecognised "
                        "move is refused rather than defaulted to 'earned', because "
                        "defaulting would let an invented entry into a promise"
                    ),
                    "requires_evidence": True,
                }
            )
            continue
        rank = row.get("rank")
        rank_value = int(rank) if isinstance(rank, (int, float)) and not isinstance(rank, bool) else previous_rank
        has_evidence = bool(row.get("evidence")) or bool(row.get("motion_id"))
        lowers = (
            rank_value is not None
            and previous_rank is not None
            and int(rank_value) < int(previous_rank)
        )
        reasons: list[str] = []
        if bool(spec["requires_evidence"]) and not has_evidence:
            reasons.append(
                f"{movement!r} requires evidence. A move that changes what "
                "somebody is owed, in either direction, has to be able to say "
                "what was seen"
            )
        if lowers and not bool(spec.get("may_lower")):
            reasons.append(
                f"{movement!r} lowered the rank from {previous_rank} to "
                f"{rank_value}, and it is not a movement allowed to lower. This is "
                "the no-decay invariant: a status that can fall for inactivity is a "
                "different promise, and the promise is published"
            )
        entries.append(
            {
                "movement": movement,
                "known_movement": True,
                "label": str(spec["label"]),
                "status": str(row.get("status") or ""),
                "rank": rank_value,
                "previous_rank": previous_rank,
                "raised": bool(spec["raises"]),
                "lowered": lowers,
                "at": str(row.get("at") or ""),
                "motion_id": str(row.get("motion_id") or ""),
                "evidence": list(row.get("evidence") or ()),
                "requires_evidence": bool(spec["requires_evidence"]),
                "has_evidence": has_evidence,
                "accepted": not reasons,
                "reason": "; ".join(reasons),
                "why": str(spec["why"]),
            }
        )
        if rank_value is not None and not reasons:
            previous_rank = int(rank_value)
    accepted = [row for row in entries if row.get("accepted")]
    # A gap is a fact about the trajectory, and reporting it as one is the point.
    last_at = ""
    for row in accepted:
        if row.get("at"):
            last_at = str(row["at"])
    return {
        "generated_at": moment.isoformat(),
        "entries": list(reversed(entries))[-int(max_entries):],
        "entry_count": len(entries),
        "refused": [row for row in entries if not row.get("accepted")],
        "current_rank": previous_rank,
        "last_change_at": last_at,
        # Only *accepted* lowerings count. An earlier version scanned every entry,
        # so a trajectory that correctly *refused* an unjustified demotion still
        # reported that a demotion had happened without evidence -- and the
        # validator then failed a build whose behaviour was right. A field that
        # reports refused events as if they occurred is worse than no field,
        # because it is read as a finding.
        "ever_lowered_without_evidence": any(
            row.get("accepted") and row.get("lowered") and not row.get("has_evidence")
            for row in entries
        ),
        "status_decays_on_inactivity": STATUS_DECAYS_ON_INACTIVITY,
        "membership_is_ordinal": True,
        "note": (
            "a trajectory of moves, not a series of scores. the investment band "
            "already answers 'how valuable', ordinally; this answers 'why is this "
            "different from six months ago', which is the only form the question "
            "takes when a customer asks. the band is never summed or averaged here"
        ),
    }


def validate_value_evolution() -> dict[str, Any]:
    """Check the movement table against the no-decay invariant, structurally.

    The check that matters is the first one. If any movement that ``may_lower``
    is also reachable without evidence, the invariant is a comment rather than a
    constraint, and it would be violated by the next person to add a row.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if STATUS_DECAYS_ON_INACTIVITY is not False:
        errors.append(
            "STATUS_DECAYS_ON_INACTIVITY is no longer False. That is the promise "
            "this whole module is built to protect, and it was changed by editing "
            "a constant rather than by deciding to change a promise"
        )
    for row in VALUE_MOVEMENTS:
        movement = str(row["movement"])
        if bool(row.get("may_lower")) and not bool(row.get("requires_evidence")):
            errors.append(
                f"VALUE_MOVEMENTS[{movement}] may lower a status without evidence. "
                "A demotion nobody can justify is the one thing a loyalty promise "
                "cannot survive"
            )
        if not str(row.get("why") or "").strip():
            warnings.append(
                f"VALUE_MOVEMENTS[{movement}] has no `why`; a move a reader cannot "
                "question is one that gets added to"
            )
    lowerers = [r for r in VALUE_MOVEMENTS if r.get("may_lower")]
    if not any(str(r["movement"]) == "lowered_for_risk" for r in lowerers):
        errors.append(
            "no movement may lower a status except the declared risk movement. If "
            "the list is empty then a status can never fall, which is a different "
            "product and should be said out loud rather than arrived at by omission"
        )
    if "paused" not in VALUE_MOVEMENT_BY_ID:
        errors.append(
            "there is no `paused` movement. A programme that is doing nothing must "
            "say it is doing nothing; silence in a trajectory reads as a decision "
            "nobody made"
        )

    # And the behaviour, not only the table.
    lowered_without_evidence = build_value_trajectory(
        history=[
            {"movement": "earned", "rank": 2, "at": "2026-01-01"},
            {"movement": "earned", "rank": 0, "at": "2026-06-01"},
        ]
    )
    if not lowered_without_evidence_ok(lowered_without_evidence):
        errors.append(
            "a trajectory that lowers a status without evidence was accepted. The "
            "invariant is enforced in the code path, not only in the table"
        )
    refused_evidence = build_value_trajectory(
        history=[
            {"movement": "earned", "rank": 2, "at": "2026-01-01"},
            {"movement": "confirmed", "rank": 3, "at": "2026-06-01"},
        ]
    )
    if not refused_evidence["refused"]:
        errors.append(
            "a `confirmed` move with no evidence was accepted. Moves that change "
            "what somebody is owed have to be able to say what was seen"
        )
    invented = build_value_trajectory(history=[{"movement": "promoted_out_of_kindness"}])
    if not invented["refused"]:
        errors.append(
            "an undeclared movement was accepted. Defaulting an unknown move to "
            "'earned' would let an invented entry into a published promise"
        )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "movements": len(VALUE_MOVEMENTS),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(VALUE_MOVEMENTS)} value movements; status decays on inactivity: "
            f"{STATUS_DECAYS_ON_INACTIVITY}"
        ),
    }


def lowered_without_evidence_ok(trajectory: Mapping[str, Any]) -> bool:
    """True when a trajectory correctly *refused* an unjustified demotion.

    Named as a predicate rather than inlined so
    :func:`validate_value_evolution` reads as an assertion about behaviour
    instead of a pile of list indexing.
    """
    return bool(trajectory.get("refused")) and not bool(
        trajectory.get("ever_lowered_without_evidence")
    )
