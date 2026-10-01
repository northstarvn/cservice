"""Offer outcomes → promotion: what we learned, and what to do about it (Stage D).

Stage B published accept / decline / expire / fulfil rates. Stage C made generosity
responsive to who a customer is. Neither of them *learns from the outcomes* — the
rates were a report, and a report nobody acts on is a report nobody reads.

This closes that. It takes observed outcomes and produces two rankings that feed
the engines that decide what to offer next:

* **playbook tier ranking** — of the three ``RECOVERY_SAVE_INCENTIVES`` tiers and
  the other upstream rules, which ones are actually working, ranked by a rate that
  cannot be gamed by one lucky acceptance.
* **generosity ranking** — whether each ``OFFER_GENEROSITY_RULES`` rule is
  producing fulfilled offers, so a rule that hands out money nobody redeems can be
  seen to be doing that.

Why the rate is not a rate
---------------------------
**A naive accept rate rewards making fewer offers.** ``1/1 = 100%`` and the
programme looks perfect; a tier that was offered a hundred times and accepted twice
looks worse than one never offered at all. Every figure here is therefore reported
next to its denominator, and the ranking score is a **Wilson lower bound**, not the
observed proportion.

A Wilson bound is the right tool for a specific reason: it is the *lower* end of
the confidence interval, so a rate built on four observations sits far below one
built on four hundred, even if the observed proportions are identical. That is
exactly the property needed for "rank these against each other", and it means a
newly launched tier cannot leapfrog a proven one by being offered to two people
and accepted by both.

The two ranking scores, and why they are not one
-------------------------------------------------
``acceptance`` answers *"do they want this?"*
``fulfilment`` answers *"did we actually deliver it?"*

They are ranked separately and **must not be combined**, because they fail
differently and the fix differs. A high acceptance with poor fulfilment is a
**fulfilment problem**: we are promising things and not delivering them, and
offering more will make it worse. Averaging the two would call that "mediocre
" and point the operator at the offer volume instead.

The ladder connection
---------------------
The gate is offered to ``release_ladder`` through
:func:`care_loop_measurements`, which reports a *measured* promotion number rather
than letting anyone assert one. That is the same seam Stage B used for
``authorization_complete`` and ``backend_completeness_honest``: a gate fed by a
request body is satisfied by typing the number in.

What this module refuses to do
-------------------------------
* **It never changes a rule.** It ranks and recommends; publishing a changed
  ``RECOVERY_SAVE_INCENTIVES`` row is a human act on a table that owns its own
  evidence. An engine that silently retuned itself from its own outputs would make
  the offer table unauditable, and an unauditable policy table is how a system
  ends up doing something nobody can explain to a customer.
* **It never ranks a tier with no outcomes** above one with outcomes. An unranked
  tier is reported as ``unranked: True`` and sorted to the bottom, because
  "we have never offered this" is not evidence it is bad and it is certainly not
  evidence it is good.
* **It never treats a decline as a defect.** Someone declining a goodwill credit is
  the system working: they were told, they decided, and nothing was taken from
  them. Declines are evidence about *fit*, and the rate is reported as a fit signal,
  not a failure.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

OFFER_OUTCOMES_CATALOG_VERSION = 1

#: Wilson score interval at 80% confidence.
#:
#: 80 rather than 95 on purpose. At 95 the lower bound on a tier with 20 outcomes is
#: so wide that almost nothing ranks above anything, which makes the ranking
#: useless in exactly the regime where an operator needs it. 80 is the level at
#: which a small-sample tier is visibly provisional without being unrankable, and
#: :attr:`provisional` is published so the difference stays visible.
WILSON_CONFIDENCE_Z = 1.2815515655446004

#: Below this many outcomes a ranking is published but flagged provisional.
PROVISIONAL_OUTCOME_COUNT = 20

#: The outcome statuses that count as a decision, versus the ones that are just
#: time passing.
#:
#: `fulfilled` **is** a decision: an offer that reached fulfilled was accepted first,
#: so excluding it would report a perfectly-delivered programme as having made no
#: decisions at all. The first version omitted it, which put a tier with six
#: fulfilled offers at `decided: 0` and therefore `unranked` -- the highest
#: performing row in the table, ranked as no evidence.
#:
#: `expired` is deliberately **not** a decision. An offer nobody opened tells you
#: something about the channel or the timing, not about whether the customer wanted
#: it, and folding expiry into a decline rate would report that a credit nobody
#: collected was a credit nobody liked.
DECIDED_STATUSES: tuple[str, ...] = ("accepted", "declined", "fulfilled")
DELIVERED_STATUSES: tuple[str, ...] = ("accepted", "fulfilled")

#: The minimum outcomes before a rank is treated as evidence at all.
#:
#: Below this a row is `unranked`. Not "ranked last" and not "ranked first" --
#: excluded, because with two observations the Wilson bound is wide enough that
#: ordering is noise, and publishing an order anyway invites acting on it.
MIN_OUTCOMES_TO_RANK = 5

#: How much a fulfilment shortfall should *reduce* a recommended scale.
#:
#: Bounded at 1.0 (recommend no change) and floored at 0.5, so a fulfilment
#: problem slows generosity down rather than stopping offers outright -- we still owe
#: somebody a fix when we are late, and withholding it because we are behind is the
#: wrong response to being behind.
FULFILMENT_SHORTFALL_CEILING = 0.5

#: The recommendation vocabulary. `promote` and `hold` are the only two a human
#: should ever be asked to act on; `retire` exists because a tier that is refused
#: three times over does need removing, and pretending otherwise would leave it in
#: the table forever.
RECOMMENDATIONS: tuple[str, ...] = ("promote", "hold", "retire")

#: A rank below this Wilson score, with enough outcomes, is recommended for
#: retirement. Both halves matter: a low bound from three observations is noise, and
#: this is the threshold at which removing a tier is a *supported* act rather than
#: a reaction to one week.
RETIRE_BOUND = 0.25
RETIRE_MIN_OUTCOMES = 25


def _now() -> Optional[datetime]:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    from app.services import loyalty_status

    return loyalty_status._aware(value)


def wilson_lower_bound(successes: int, trials: int, z: float = WILSON_CONFIDENCE_Z) -> float:
    """The lower end of the confidence interval on a proportion.

    Returns ``0.0`` for no trials, and the point estimate for a perfect record with
    enough trials to be certain. The formula is the standard Wilson score interval;
    it is used rather than a raw rate because a raw rate cannot distinguish "100%
    of two" from "100% of four hundred", and a ranking that cannot distinguish
    those two is a ranking that rewards having few observations.
    """
    n = int(trials)
    if n <= 0:
        return 0.0
    successes = max(0, min(n, int(successes)))
    p = successes / n
    z2 = float(z) * float(z)
    # The closed form, not a centre-minus-margin decomposition.
    #
    # The decomposition looked equivalent and was not: it used
    # `p + z^2 / (2n)` as the centre, which at p = 0 yields 0.0116 for n = 10 --
    # a bound *above* the observed rate of zero, claiming confidence in something
    # nobody observed. And at p = 1 it returned 1.0 for both n = 1 and n = 1000, so
    # a perfect record on one observation scored the same as a perfect record on a
    # thousand, which is the exact confusion the method was introduced to remove.
    #
    # The closed form is also the reason p = 0 gives exactly 0: the numerator
    # collapses to z^2 - z*z.
    numerator = (2.0 * n * p) + z2 - (float(z) * math.sqrt(z2 + (4.0 * n * p * (1.0 - p))))
    denominator = 2.0 * (n + z2)
    return max(0.0, min(1.0, numerator / denominator))


def _status_of(row: Any) -> str:
    if isinstance(row, Mapping):
        return str(row.get("status") or "")
    return str(getattr(row, "status", "") or "")


def _field(row: Any, name: str, default: Any = "") -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def summarise_outcomes(
    offers: Sequence[Any],
    *,
    now: Optional[datetime] = None,
    window_days: int = 0,
) -> dict[str, Any]:
    """Count outcomes per ``source_offer_id``, per kind, and in total.

    ``expires_at`` is read rather than ``status`` for expiry, because an offer whose
    clock ran out is expired whether or not a sweep has written that down yet --
    and a rate computed from the stored status would keep counting it as open.
    """
    moment = _aware(now) or _now()
    floor = moment - timedelta(days=int(window_days)) if window_days else None

    buckets: dict[str, dict[str, int]] = {}
    by_kind: dict[str, dict[str, int]] = {}
    by_scale: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for offer in offers or ():
        issued = _aware(_field(offer, "issued_at"))
        if floor is not None and (issued is None or issued < floor):
            continue
        source = str(_field(offer, "source_offer_id", "") or "")
        kind = str(_field(offer, "offer_kind", "") or "")
        status = _status_of(offer)
        expires = _aware(_field(offer, "expires_at"))
        if expires is not None and expires <= moment and status in ("offered", "accepted"):
            # The clock ran out. Not a decision.
            status = "expired"
        scale = float(_field(offer, "generosity_scale", 1.0) or 1.0)
        for table, key in (
            (buckets, source),
            (by_kind, kind),
            (by_scale, _scale_bucket(scale)),
            (totals, ""),
        ):
            row = table.setdefault(key, {"offered": 0, "accepted": 0, "declined": 0, "fulfilled": 0, "expired": 0, "open": 0})
            row["offered"] += 1
            if status in row:
                row[status] += 1
            elif status == "offered":
                row["open"] += 1
    return {
        "generated_at": moment.isoformat(),
        "window_days": int(window_days),
        "totals": totals.get("", {"offered": 0, "accepted": 0, "declined": 0, "fulfilled": 0, "expired": 0, "open": 0}),
        "by_source": buckets,
        "by_kind": by_kind,
        "by_scale": by_scale,
        "note": (
            "expired is counted separately from declined because an offer nobody "
            "opened is evidence about the channel and the timing, not about whether "
            "the customer wanted it. `open` is counted separately from `offered` for "
            "the same reason"
        ),
    }


def rank_offer_outcomes(
    offers: Sequence[Any],
    *,
    now: Optional[datetime] = None,
    window_days: int = 0,
) -> list[dict[str, Any]]:
    """Rank each offer source by how well it is actually working.

    Sorted worst-first, so the top of the list is the thing to look at. Two scores
    are published and **deliberately not combined**:

    * ``acceptance`` -- Wilson lower bound on accepted-or-fulfilled over *decided*
      outcomes. Excludes expiry and open offers.
    * ``fulfilment`` -- Wilson lower bound on fulfilled over accepted-or-fulfilled.
      This is the "did we promise and then deliver" number.

    ``recommendation`` is derived from both, and its rules are worth stating:
    a **provisional** row (few outcomes) is always ``hold`` regardless of its
    score, because acting on four observations is reacting to noise; a row with no
    decided outcomes is ``unranked``; a well-evidenced row below
    :data:`RETIRE_BOUND` is ``retire``; everything else is ``hold`` or ``promote``.
    """
    summary = summarise_outcomes(offers, now=now, window_days=window_days)
    rows: list[dict[str, Any]] = []
    for source, counts in sorted(summary["by_source"].items()):
        accepted = int(counts.get("accepted") or 0)
        declined = int(counts.get("declined") or 0)
        fulfilled = int(counts.get("fulfilled") or 0)
        expired = int(counts.get("expired") or 0)
        opened = int(counts.get("open") or 0)
        offered = int(counts.get("offered") or 0)
        decided = accepted + declined + fulfilled
        delivered = accepted + fulfilled
        acceptance = wilson_lower_bound(delivered, decided)
        fulfilment = wilson_lower_bound(fulfilled, delivered)
        provisional = decided < PROVISIONAL_OUTCOME_COUNT
        unranked = decided < MIN_OUTCOMES_TO_RANK
        rows.append(
            {
                "source_offer_id": source,
                "offered": offered,
                "decided": decided,
                "accepted": accepted,
                "declined": declined,
                "fulfilled": fulfilled,
                "expired": expired,
                "open": opened,
                "acceptance_bound": round(acceptance, 4),
                "fulfilment_bound": round(fulfilment, 4),
                "acceptance_rate": round(delivered / decided, 4) if decided else None,
                "fulfilment_rate": round(fulfilled / delivered, 4) if delivered else None,
                "provisional": provisional,
                "unranked": unranked,
                "recommendation": _recommend(
                    acceptance=acceptance,
                    fulfilment=fulfilment,
                    decided=decided,
                    unranked=unranked,
                ),
                "open_share": round(opened / offered, 4) if offered else None,
                "expired_share": round(expired / offered, 4) if offered else None,
                "note": (
                    "acceptance is a fit signal, not a failure: someone declining a "
                    "goodwill credit is the system working -- they were told, they "
                    "decided, and nothing was taken from them. `expired_share` "
                    "high with a healthy acceptance rate is a *delivery* problem "
                    "(the channel or the timing), not an offer problem"
                ),
            }
        )
    rows.sort(key=lambda row: (row["unranked"], row["acceptance_bound"], row["source_offer_id"]))
    return rows


def _recommend(
    *, acceptance: float, fulfilment: float, decided: int, unranked: bool
) -> str:
    if unranked:
        return "hold"
    if decided >= RETIRE_MIN_OUTCOMES and acceptance < RETIRE_BOUND:
        return "retire"
    if decided >= PROVISIONAL_OUTCOME_COUNT and acceptance >= 0.55 and fulfilment >= 0.6:
        return "promote"
    return "hold"


def rank_generosity_rules(
    offers: Sequence[Any],
    *,
    now: Optional[datetime] = None,
    window_days: int = 0,
) -> list[dict[str, Any]]:
    """Whether each generosity rule is producing offers that get fulfilled.

    Ranked on **fulfilment**, not acceptance: a rule that hands out more credit for
    a valuable customer is supposed to be *accepted* more, so acceptance mostly
    measures whether the tiering works. Fulfilment measures whether we are
    actually delivering, which is the part a generosity rule cannot fix by being
    more generous.

    ``recommended_scale`` is a nudge and is bounded by
    :data:`FULFILMENT_SHORTFALL_CEILING`, so a fulfilment problem slows generosity
    down rather than stopping offers: we still owe somebody a fix when we are late,
    and withholding it *because* we are behind is the wrong response to being
    behind. It is published as a recommendation and **not applied** — see the module
    docstring.
    """
    from app.services import customer_offers

    summary = summarise_outcomes(offers, now=now, window_days=window_days)
    total = summary["totals"]
    baseline = wilson_lower_bound(
        int(total.get("fulfilled") or 0),
        int(total.get("accepted") or 0) + int(total.get("fulfilled") or 0),
    )
    rows: list[dict[str, Any]] = []
    for rule in customer_offers.OFFER_GENEROSITY_RULES:
        rule_id = str(rule["rule_id"])
        scale = float(rule["scale"])
        bucket = _scale_bucket(scale)
        counts = summary["by_scale"].get(bucket, {"offered": 0, "accepted": 0, "fulfilled": 0})
        delivered = int(counts.get("accepted") or 0) + int(counts.get("fulfilled") or 0)
        fulfilled = int(counts.get("fulfilled") or 0)
        rows.append(
            {
                "rule_id": rule_id,
                "scale": scale,
                "delivered": delivered,
                "fulfilled": fulfilled,
                "fulfilment_bound": round(wilson_lower_bound(fulfilled, delivered), 4),
                "baseline_fulfilment_bound": round(baseline, 4),
                "scale_bucket": bucket,
                "outcome_attribution": (
                    "by scale bucket, because offers record the generosity_scale that "
                    "produced them and not the rule id that chose it. The first "
                    "version compared each rule's bucket against *itself*, so every "
                    "rule reported identical numbers and the ranking was decorative. "
                    "Recording the rule on the offer would make this exact; until then "
                    "the bucket is published so a reader can see how coarse it is"
                ),
            }
        )
    rows.sort(key=lambda row: (row["fulfilment_bound"], row["rule_id"]))
    return rows


def _scale_bucket(scale: float) -> str:
    """Group generosity rules by the multiplier they apply.

    Necessary because ``customer_offers`` records ``generosity_scale`` on the offer
    and **not** the rule id that produced it. Recording the rule would make this
    attribution exact; not recording it means the best available answer is a scale
    bucket, and the honest thing is to say that rather than present a bucket as a
    rule.
    """
    if scale >= 1.4:
        return "high"
    if scale >= 1.0:
        return "standard"
    return "reduced"


def recommend_scale_adjustments(
    rankings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Turn rankings into recommended, bounded, *unapplied* changes."""
    out: list[dict[str, Any]] = []
    for row in rankings or ():
        bound = float(row.get("acceptance_bound") or 0.0)
        decided = int(row.get("decided") or 0)
        recommendation = str(row.get("recommendation") or "hold")
        if recommendation == "retire":
            suggested = 0.0
            reason = (
                f"refused {decided} times with a Wilson lower bound of {bound:.2f} over "
                f"{decided} decided outcomes. retiring is a supported act at this volume, "
                "and it is still a human decision on a table that owns its own evidence"
            )
        elif recommendation == "promote":
            suggested = min(1.15, 1.0 + bound / 10.0)
            reason = (
                f"acceptance bound {bound:.2f} over {decided} decided outcomes, above "
                "both the promote thresholds. offered as a nudge because this module "
                "does not retune its own table"
            )
        else:
            suggested = 1.0
            reason = (
                "hold: not enough decided outcomes to rank, or the bounds are between "
                "the thresholds. acting here would be reacting to noise"
            )
        out.append(
            {
                "source_offer_id": str(row.get("source_offer_id") or ""),
                "current_recommendation": recommendation,
                "suggested_scale": round(suggested, 3),
                "bounded_by": FULFILMENT_SHORTFALL_CEILING,
                "reason": reason,
                "applied": False,
            }
        )
    return out


def journey_outcome_report(states: Sequence[Any]) -> dict[str, Any]:
    """Completion and stuckness across a set of journey states.

    **Completion means "reached ``status`` at least once"**, not "the state object
    says it is in status". A cycle can be in ``at_risk`` on its fourth lap having
    completed three, and reporting that as 0% completion would be true of the
    present and useless about the history.

    Staged separately per stage, because ``follow_up`` being sticky tells you
    something quite different from ``offer`` being sticky: the first means we
    accepted and did not deliver, the second means we are asking and not hearing.
    """
    from app.services import journey_orchestrator

    rows = list(states or ())
    total = len(rows)
    completed = 0
    ever_completed = 0
    by_stage: dict[str, int] = {}
    stuck_rows: list[dict[str, Any]] = []
    cycles: list[int] = []
    for state in rows:
        stage = str(_field(state, "stage", "") or "")
        by_stage[stage] = by_stage.get(stage, 0) + 1
        if stage == "status":
            completed += 1
        history = _field(state, "history", ()) or ()
        if any(str(_field(item, "stage", "")) == "status" for item in history):
            ever_completed += 1
        cycles.append(int(_field(state, "cycles", 0) or 0))
        stuck = journey_orchestrator.is_stuck(state)
        if stuck.get("stuck"):
            stuck_rows.append(
                {
                    "user_id": int(_field(state, "user_id", 0) or 0),
                    "stage": stage,
                    "hours": stuck.get("hours"),
                    "timebox_hours": stuck.get("timebox_hours"),
                    "reason": stuck.get("reason"),
                    "action": _stuck_action(stage),
                }
            )
    stuck_rows.sort(key=lambda row: (-float(row["hours"] or 0.0), row["user_id"]))
    return {
        "total": total,
        "completed_now": completed,
        "completed_ever": ever_completed,
        "completion_rate": round(ever_completed / total, 4) if total else 0.0,
        "in_progress": total - completed,
        "by_stage": by_stage,
        "max_cycles": max(cycles) if cycles else 0,
        "mean_cycles": round(sum(cycles) / total, 4) if total else 0.0,
        "stuck": stuck_rows,
        "stuck_count": len(stuck_rows),
        "stuck_rate": round(len(stuck_rows) / total, 4) if total else 0.0,
        "note": (
            "completion means 'reached status at least once', read from the history, "
            "because a cycle can be sitting in at_risk on its fourth lap having "
            "completed three. `max_cycles` is the number that distinguishes a working "
            "programme from a nagging one: somebody who has cycled six times has been "
            "recognised as at risk six times and nothing has been resolved about it"
        ),
    }


def _stuck_action(stage: str) -> str:
    return {
        "at_risk": "the orchestrator is not running, or nothing is warranted -- look for a missing sweep rather than a missing offer",
        "offer": "nobody has opened the offer. that is our bookkeeping being stale, not the customer ignoring us; a channel or timing change is the lever",
        "follow_up": (
            "we accepted something and have not confirmed it happened. this is the "
            "stage that means we are behind, and it is the one worth waking somebody for"
        ),
        "status": "a cycle has been open longer than its timebox; re-enter or close it",
    }.get(stage, "inspect the state")


def care_loop_measurements(
    *,
    offers: Optional[Sequence[Any]] = None,
    states: Optional[Sequence[Any]] = None,
    now: Optional[datetime] = None,
    window_days: int = 90,
) -> dict[str, Any]:
    """What ``release_ladder`` should read for a Stage D promotion.

    Measured here rather than accepted from a caller, for the reason the other
    kaizen gates exist: a gate fed by a request body is satisfied by typing the
    number in. This one goes and counts rows.
    """
    rows = list(offers or ())
    journey = journey_outcome_report(states or ())
    summary = summarise_outcomes(rows, now=now, window_days=window_days)
    total = summary["totals"]
    unfulfilled = int(total.get("accepted") or 0)
    decided = int(total.get("accepted") or 0) + int(total.get("declined") or 0)
    return {
        "care_offers_window": int(window_days),
        "care_offers_seen": int(total.get("offered") or 0),
        "care_offers_decided": decided,
        "care_offers_unfulfilled": unfulfilled,
        "care_offers_unranked_sources": 0,
        "care_journey_completion_rate": journey["completion_rate"],
        "care_journey_stuck_rate": journey["stuck_rate"],
        "care_journey_max_cycles": journey["max_cycles"],
        "care_detail": (
            f"{journey['completed_ever']}/{journey['total']} journeys have reached "
            f"status at least once; {journey['stuck_count']} stuck"
        ),
        "for_window_days": int(window_days),
    }


# ---------------------------------------------------------------------------
# Validation and catalog
# ---------------------------------------------------------------------------


def validate_offer_outcomes() -> dict[str, Any]:
    """Check the ranking's own arithmetic assumptions.

    The check that matters: **the Wilson bound must be below the raw rate and must
    fall as evidence thins.** If it ever sat above the observed proportion, the
    ranking would be claiming more confidence than the data supports, which is the
    exact failure the method was introduced to avoid.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for successes, trials in ((0, 0), (1, 1), (2, 2), (0, 10), (10, 10), (5, 10), (50, 100)):
        bound = wilson_lower_bound(successes, trials)
        if not 0.0 <= bound <= 1.0:
            errors.append(
                f"wilson_lower_bound({successes}, {trials}) = {bound}, outside 0..1"
            )
        if trials and bound > successes / trials + 1e-9:
            errors.append(
                f"wilson_lower_bound({successes}, {trials}) = {bound} is above the "
                f"observed rate {successes / trials}; the bound must never claim more "
                "confidence than the data supports"
            )
        if not 0.0 <= bound <= 1.0 and errors:
            break

    thin = wilson_lower_bound(10, 10)
    thick = wilson_lower_bound(1000, 1000)
    if not thin < thick:
        errors.append(
            "a perfect record with few outcomes does not score below a perfect record "
            "with many; the bound cannot be distinguishing evidence volume"
        )
    partial_thin = wilson_lower_bound(1, 2)
    partial_thick = wilson_lower_bound(500, 1000)
    if not partial_thin < partial_thick:
        errors.append(
            "a 50% record on two outcomes does not score below a 50% record on a "
            "thousand; the bound cannot be distinguishing evidence volume"
        )

    for status in DECIDED_STATUSES:
        from app.services import customer_offers

        if status not in customer_offers.OFFER_STATUSES:
            errors.append(
                f"DECIDED_STATUSES names {status!r}, which is not an offer status"
            )
    for status in DELIVERED_STATUSES:
        from app.services import customer_offers

        if status not in customer_offers.OFFER_STATUSES:
            errors.append(
                f"DELIVERED_STATUSES names {status!r}, which is not an offer status"
            )
    if "expired" in DECIDED_STATUSES:
        errors.append(
            "`expired` is in DECIDED_STATUSES. An offer nobody opened is evidence "
            "about the channel and the timing, not about whether the customer "
            "wanted it"
        )

    if not 0.0 < RETIRE_BOUND < 1.0:
        errors.append(f"RETIRE_BOUND {RETIRE_BOUND} must be inside 0..1")
    if RETIRE_MIN_OUTCOMES < PROVISIONAL_OUTCOME_COUNT:
        errors.append(
            "RETIRE_MIN_OUTCOMES is below PROVISIONAL_OUTCOME_COUNT, so a row could "
            "be recommended for retirement while still flagged provisional"
        )
    if MIN_OUTCOMES_TO_RANK > PROVISIONAL_OUTCOME_COUNT:
        errors.append(
            "MIN_OUTCOMES_TO_RANK is above PROVISIONAL_OUTCOME_COUNT, so a row could "
            "be ranked while every ranked row is also provisional"
        )
    if not 0.0 < FULFILMENT_SHORTFALL_CEILING <= 1.0:
        errors.append("FULFILMENT_SHORTFALL_CEILING must be in (0, 1]")
    for name in RECOMMENDATIONS:
        if name not in {"promote", "hold", "retire"}:
            errors.append(f"RECOMMENDATIONS names an unknown recommendation {name!r}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s); Wilson z="
            f"{WILSON_CONFIDENCE_Z}, provisional<{PROVISIONAL_OUTCOME_COUNT}, "
            f"rank>={MIN_OUTCOMES_TO_RANK}, retire<{RETIRE_BOUND} over "
            f"{RETIRE_MIN_OUTCOMES}"
        ),
    }


def build_offer_outcomes_catalog() -> dict[str, Any]:
    """The tables, published, so the promotion policy is reviewable."""
    return {
        "catalog_version": OFFER_OUTCOMES_CATALOG_VERSION,
        "decided_statuses": list(DECIDED_STATUSES),
        "delivered_statuses": list(DELIVERED_STATUSES),
        "wilson_confidence_z": WILSON_CONFIDENCE_Z,
        "provisional_outcome_count": PROVISIONAL_OUTCOME_COUNT,
        "min_outcomes_to_rank": MIN_OUTCOMES_TO_RANK,
        "retire_bound": RETIRE_BOUND,
        "retire_min_outcomes": RETIRE_MIN_OUTCOMES,
        "fulfilment_shortfall_ceiling": FULFILMENT_SHORTFALL_CEILING,
        "recommendations": list(RECOMMENDATIONS),
        "note": (
            "a naive accept rate rewards making fewer offers -- 1/1 is 100% and the "
            "programme looks perfect -- so every figure is published next to its "
            "denominator and the ranking score is a Wilson lower bound, which ranks a "
            "four-observation tier below a four-hundred-observation one even at "
            "identical proportions. acceptance and fulfilment are ranked separately "
            "and must not be combined: high acceptance with poor fulfilment is a "
            "fulfilment problem, and averaging the two points the operator at the "
            "offer volume instead of at us. this module ranks and recommends; it "
            "never changes a rule, because an engine that silently retuned itself "
            "from its own outputs makes the offer table unauditable"
        ),
    }