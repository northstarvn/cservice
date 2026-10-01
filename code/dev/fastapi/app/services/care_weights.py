"""Learned complaint weights → how we care for the next customer (Stage D).

`services/complaint_learning.py` learns per-signal weights from observed complaint
outcomes: bounded, decayed toward their prior, persisted so they survive a
restart. It publishes them through ``load_weight_map`` and, importantly, publishes
an **authority table** alongside:

``may_reorder_recommendations`` / ``may_change_explanation``
    true.
``may_change_severity_alone`` / ``may_change_tier_alone`` / ``may_auto_escalate``
    **false**, and ``validate_complaint_learning`` errors if any of those is ever
    flipped.

This module is the consumer those weights were learned for. Before it existed they
were read by a report and nothing else — the same shape as the
``communication_suppression`` rule pack that was authored, exported, validated,
catalogued and had no caller for its whole life.

What a learned weight is allowed to change here
------------------------------------------------
**Emphasis, not verdict.** A signal that keeps preceding complaints turning into
churn should make us *care differently* about the next customer showing that
signal — reach out sooner, say something about the delay, offer a little more
generously. It must not change what the complaint is worth, which tier the case
lands in, or whether anything escalates on its own. Those three are refusals
carried over from the authority table verbatim, and :func:`validate_care_weights`
re-asserts them by reading that table rather than by restating it, so a change
there has to be made in two places on purpose.

The dimensions a weight can move
--------------------------------
``recovery_emphasis``
    How hard the recovery engine works this customer. Bounded and floored at 1.0,
    so a learned weight can make us *more* attentive and never less: a system that
    can learn to care about somebody less is a system that will.
``generosity_nudge``
    A bounded multiplier handed to ``customer_offers``' generosity rules, which
    already carry their own bounds and per-kind cap. This module proposes; that one
    disposes.
``communication_frame``
    The framing of an unprompted message — acknowledged first, versus solution
    first. A `sentiment_cliff` weight should make us *acknowledge* rather than
    *explain*, because the first thing a customer who feels dismissed hears is
    another explanation.
``follow_up_hours``
    How soon somebody checks back. The one dimension where "less" is the dangerous
    direction, so it is floored.

It is also a proactive path
---------------------------
Changing the framing of an unprompted message is a form of contact, so
:func:`resolve_care_weights` asks :mod:`app.services.care_gate` and reports
``push_permitted``. That is registered as ``care_weight_outreach`` in that module's
proactive-path registry, and the registry's validator reads *this file's source* to
prove the call is really here — so removing the gate from this module breaks the
build rather than quietly restoring the defect.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

CARE_WEIGHTS_CATALOG_VERSION = 1

#: The four dimensions a learned weight may move, with their bounds.
#:
#: Bounds are declared here rather than left to the consumer, and every floor is
#: 1.0 or a positive hour count, so the direction of "worse" is always the one that
#: is clamped. A weight system that can learn to care about somebody less is a
#: weight system that eventually will.
CARE_DIMENSIONS: tuple[dict[str, Any], ...] = (
    {
        "dimension": "recovery_emphasis",
        "label": "How hard the recovery engine works this customer",
        "bounds": (1.0, 1.5),
        "neutral": 1.0,
        "default": 1.0,
        "consumer": "services/recovery_playbooks:plan_recovery_actions",
        "why": (
            "floored at 1.0: a learned weight may make us more attentive to a "
            "customer and never less. a system that can learn to care about "
            "somebody less is a system that will"
        ),
    },
    {
        "dimension": "generosity_nudge",
        "label": "A bounded multiplier on the goodwill amount",
        "bounds": (0.75, 1.25),
        "neutral": 1.0,
        "default": 1.0,
        "consumer": "services/customer_offers:resolve_offer_generosity",
        "why": (
            "deliberately two-sided, unlike recovery_emphasis: a nudge down is the "
            "point of a nudge, and a complaint with a low learned weight should not "
            "buy the same credit as one that keeps preceding churn. the band is "
            "narrow, it proposes rather than disposes, OFFER_GENEROSITY_RULES "
            "carries its own bounds and a per-kind cap, and the amount is recorded "
            "on the offer so it cannot move under one already accepted -- so the "
            "floor that matters is enforced where the money is"
        ),
    },
    {
        "dimension": "communication_frame",
        "label": "Acknowledged-first versus solution-first",
        "bounds": (0.0, 1.0),
        "neutral": 0.5,
        "default": 0.5,
        "consumer": "services/recovery_playbooks:resolve_recovery_outreach_strategy",
        "why": (
            "not a multiplier but a lean towards acknowledging first. a customer "
            "who feels dismissed hears another explanation as confirmation that "
            "they are the problem"
        ),
    },
    {
        "dimension": "follow_up_hours",
        "label": "How soon somebody checks back",
        "bounds": (4.0, 168.0),
        "neutral": 48.0,
        "default": 48.0,
        "consumer": "services/journey_orchestrator:STAGE_TIMEBOX_HOURS",
        "why": (
            "hours, so the bound is meaningful, and the floor is 4 because the "
            "critical band's SLA is 4 and nothing learned may quietly push a "
            "check-back past it"
        ),
    },
)
CARE_DIMENSION_BY_ID: dict[str, dict[str, Any]] = {
    str(row["dimension"]): dict(row) for row in CARE_DIMENSIONS
}

#: Which learned signals move which dimension, and in which direction.
#:
#: Read from ``complaint_learning.LOYALTY_SIGNALS`` by id rather than restating the
#: signal list, and validated against it, so a rename there is an error here rather
#: than a weight that silently stops applying.
#:
#: ``pull`` is the dimension the signal raises. A ``loyalty_positive`` signal
#: (someone rebooked after we fixed it) raises recovery emphasis, because it is
#: evidence that attending to this kind of customer works.
CARE_WEIGHT_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule_id": "care_from_left",
        "signal_id": "complainant_left",
        "dimension": "recovery_emphasis",
        "pull": 1.0,
        "floor_shift": 0.0,
        "reason": (
            "nothing else in the ledger is worse. if a complainant walks away, we "
            "were the relationship and we did not notice"
        ),
    },
    {
        "rule_id": "care_from_unresolved",
        "signal_id": "unresolved_stale",
        "dimension": "follow_up_hours",
        "pull": -1.0,
        "floor_shift": 0.0,
        "reason": (
            "a case that sat is the cheapest defect to fix and the most expensive "
            "to leave alone, so this pulls the check-back earlier"
        ),
    },
    {
        "rule_id": "care_from_unowned",
        "signal_id": "unowned",
        "dimension": "follow_up_hours",
        "pull": -1.0,
        "floor_shift": 0.0,
        "reason": (
            "an unowned case is our failure with nobody accountable for it, and "
            "the fastest way to catch that is to check sooner"
        ),
    },
    {
        "rule_id": "acknowledge_from_sentiment",
        "signal_id": "sentiment_cliff",
        "dimension": "communication_frame",
        "pull": 1.0,
        "floor_shift": 0.0,
        "reason": (
            "a sudden drop in sentiment means the customer already feels unheard. "
            "the first thing they should hear is an acknowledgement, not a "
            "resolution they did not ask for"
        ),
    },
    {
        "rule_id": "acknowledge_from_repeat",
        "signal_id": "repeat_complainant",
        "dimension": "communication_frame",
        "pull": 1.0,
        "floor_shift": 0.0,
        "reason": (
            "someone who has complained before and is complaining again has "
            "already heard our explanation once"
        ),
    },
    {
        "rule_id": "sla_faster_from_response",
        "signal_id": "sla_response_breached",
        "dimension": "follow_up_hours",
        "pull": -1.0,
        "floor_shift": 0.0,
        "reason": "we missed the response clock, so the next one is brought forward",
    },
    {
        "rule_id": "sla_faster_from_resolution",
        "signal_id": "sla_resolution_breached",
        "dimension": "follow_up_hours",
        "pull": -1.0,
        "floor_shift": 0.0,
        "reason": "the same, for resolution",
    },
    {
        "rule_id": "care_from_churn",
        "signal_id": "churn_critical",
        "dimension": "recovery_emphasis",
        "pull": 1.0,
        "floor_shift": 0.0,
        "reason": (
            "a complaint from somebody already predicted to leave is a complaint "
            "we are losing them over, which is the cheapest moment to still act"
        ),
    },
    {
        "rule_id": "generous_from_left",
        "signal_id": "complainant_left",
        "dimension": "generosity_nudge",
        "pull": 0.25,
        "floor_shift": 0.0,
        "reason": (
            "deliberately weak. the next customer who shows this signal is not the "
            "one who left, so this must not turn a complaint into a payment. the "
            "bounds and the per-kind cap in customer_offers are what stop it"
        ),
    },
    {
        "rule_id": "more_attention_from_held",
        "signal_id": "held",
        "dimension": "recovery_emphasis",
        "pull": 0.5,
        "floor_shift": 0.0,
        "reason": "an escalation that was held rather than resolved is our own delay",
    },
)

#: The three refusals, copied from ``complaint_learning.LEARNED_WEIGHT_AUTHORITY``
#: and re-asserted against it by the validator.
#:
#: Deliberately duplicated rather than imported, because the point of the check is
#: that this module *cannot* quietly grow a new power. If the authority table
#: upstream ever relaxes one of these, this module's validator fails until someone
#: decides here what a relaxed authority would mean -- which is the conversation
#: worth having.
REFUSED_LEARNED_POWERS: tuple[str, ...] = (
    "may_change_severity_alone",
    "may_change_tier_alone",
    "may_auto_escalate",
)


def _now() -> Optional[datetime]:
    return datetime.now(timezone.utc)


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    return max(low, min(high, float(value)))


def resolve_care_weights(
    weights: Optional[Mapping[str, float]] = None,
    *,
    context: Optional[Mapping[str, Any]] = None,
    preferences_map: Optional[Mapping[str, Any]] = None,
    consents: Optional[Mapping[str, bool]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Turn learned complaint weights into care emphasis for one customer.

    **Never raises and never widens what a weight may do.** A signal with no rule
    contributes nothing; a rule whose dimension is unknown is reported as
    ``unapplied`` rather than being dropped, because a rule pointing at a dimension
    that does not exist is a bug and silence hides it.

    Asks the care gate, because changing the framing of an unprompted message is
    a form of contact. It reports ``push_permitted`` and does not send: this module
    produces emphasis, and a surface that turns emphasis into an outbound message is
    the caller's decision.
    """
    from app.services import care_gate

    moment = now or _now()
    known = {str(key): float(value) for key, value in (weights or {}).items()}
    signal_meta = _signal_metadata()
    moved: list[dict[str, Any]] = []
    unapplied: list[dict[str, Any]] = []
    accumulator: dict[str, float] = {
        str(row["dimension"]): float(row["neutral"]) for row in CARE_DIMENSIONS
    }
    contributors: dict[str, list[str]] = {name: [] for name in accumulator}

    for rule in CARE_WEIGHT_RULES:
        rule_id = str(rule["rule_id"])
        signal_id = str(rule["signal_id"])
        dimension = str(rule["dimension"])
        if dimension not in accumulator:
            unapplied.append(
                {
                    "rule_id": rule_id,
                    "signal_id": signal_id,
                    "dimension": dimension,
                    "reason": "the dimension this rule moves does not exist",
                }
            )
            continue
        meta = signal_meta.get(signal_id)
        if meta is None:
            unapplied.append(
                {
                    "rule_id": rule_id,
                    "signal_id": signal_id,
                    "dimension": dimension,
                    "reason": (
                        "no such signal in complaint_learning.LOYALTY_SIGNALS; the "
                        "signal was renamed or retired and this rule is dead weight"
                    ),
                }
            )
            continue
        weight = known.get(signal_id)
        if weight is None:
            # No observation yet. Not an error and not zero -- an absent signal is
            # not evidence of a pattern, so the prior stands untouched.
            continue
        pull = float(rule["pull"])
        span = _span(dimension)
        delta = (weight - 1.0) * pull * span
        accumulator[dimension] += delta
        contributors[dimension].append(rule_id)
        moved.append(
            {
                "rule_id": rule_id,
                "signal_id": signal_id,
                "direction": meta["direction"],
                "weight": round(weight, 4),
                "dimension": dimension,
                "delta": round(delta, 4),
                "reason": str(rule["reason"]),
            }
        )

    resolved: dict[str, dict[str, Any]] = {}
    for dimension, value in accumulator.items():
        spec = CARE_DIMENSION_BY_ID[dimension]
        bounds = tuple(spec["bounds"])  # type: ignore[arg-type]
        clamped = _clamp(value, bounds)
        resolved[dimension] = {
            "value": round(clamped, 4),
            "raw": round(value, 4),
            "neutral": float(spec["neutral"]),
            "bounds": [float(bounds[0]), float(bounds[1])],
            "clamped": abs(clamped - value) > 1e-9,
            "at_floor": abs(clamped - float(bounds[0])) < 1e-9,
            "contributors": contributors[dimension],
            "consumer": str(spec["consumer"]),
            "why": str(spec["why"]),
        }

    gate = care_gate.consult(
        "care_weight_outreach",
        preferences_map,
        consents,
        purpose="recovery",
        channel=str((resolved.get("communication_frame") or {}).get("value") and "email" or ""),
    )

    return {
        "generated_at": moment.isoformat(),
        "weights_in": len(known),
        "signals_observed": sorted(known),
        "dimensions": resolved,
        "moved": moved,
        "unapplied": unapplied,
        "acknowledged_first": bool(
            (resolved.get("communication_frame") or {}).get("value", 0.5) > 0.5
        ),
        "recovery_emphasis": (resolved.get("recovery_emphasis") or {}).get("value", 1.0),
        "follow_up_hours": (resolved.get("follow_up_hours") or {}).get("value", 48.0),
        "push_permitted": bool(gate["push"]),
        "gate": gate,
        "refused_powers": list(REFUSED_LEARNED_POWERS),
        "note": (
            "emphasis, not verdict. a learned weight may change how hard we work "
            "this customer and how soon somebody checks back; it may not change "
            "what a complaint is worth, which tier a case lands in, or whether "
            "anything escalates on its own. those three refusals come from "
            "complaint_learning.LEARNED_WEIGHT_AUTHORITY and are re-asserted by the "
            "validator against that table, so relaxing one upstream fails here "
            "until someone decides what a relaxed authority means"
        ),
    }


def _span(dimension: str) -> float:
    """Half the dimension's range, so a weight of 2.0 moves it by the full pull."""
    spec = CARE_DIMENSION_BY_ID[str(dimension)]
    low, high = (float(v) for v in spec["bounds"])  # type: ignore[misc]
    return max(1e-6, (high - low) / 2.0)


def _signal_metadata() -> dict[str, dict[str, Any]]:
    """Read ``complaint_learning.LOYALTY_SIGNALS`` rather than restating it.

    A local copy of that vocabulary would drift on the first rename, and a weight
    that silently stops applying is indistinguishable from a weight that was never
    wired up.
    """
    from app.services import complaint_learning

    return {
        str(row["signal_id"]): {
            "direction": str(row.get("direction") or "neutral"),
            "label": str(row.get("label") or row["signal_id"]),
            "prior_weight": float(row.get("prior_weight") or 1.0),
        }
        for row in complaint_learning.LOYALTY_SIGNALS
    }


async def load_care_weights_for_user(db: Any) -> dict[str, Any]:
    """Load the learned weight map and resolve care emphasis.

    Thin wrapper so a caller does not import two modules to ask one question. The
    weights are global (they are learned across the complaint population); **who**
    gets the emphasis is per-customer and comes from ``context``.
    """
    from app.services import complaint_learning

    weight_map = await complaint_learning.load_weight_map(db)
    return resolve_care_weights(weight_map)


# ---------------------------------------------------------------------------
# Validation and catalog
# ---------------------------------------------------------------------------


def validate_care_weights() -> dict[str, Any]:
    """Check this module against the authority table and the signal vocabulary.

    Three checks that matter more than the usual cross-references:

    * every rule names a signal that ``complaint_learning`` actually declares. A
      weight pointing at a renamed signal applies to nothing, and nothing looks
      wrong.
    * every dimension is **floored** at or above its neutral where raising effort is
      meant to be one-directional, and the floor for ``follow_up_hours`` is at most
      the ``critical`` band SLA. A learned weight may not quietly push a check-back
      past the one deadline anybody is accountable for.
    * all three refused powers are still refused **upstream**. This is read from
      ``LEARNED_WEIGHT_AUTHORITY`` rather than assumed, so relaxing one there fails
      here.
    """
    errors: list[str] = []
    warnings: list[str] = []

    signal_meta = _signal_metadata()
    for rule in CARE_WEIGHT_RULES:
        rule_id = str(rule["rule_id"])
        signal_id = str(rule["signal_id"])
        dimension = str(rule["dimension"])
        if signal_id not in signal_meta:
            errors.append(
                f"CARE_WEIGHT_RULES[{rule_id}] names signal {signal_id!r}, which "
                "complaint_learning.LOYALTY_SIGNALS does not declare. A weight "
                "pointing at a renamed signal applies to nothing and looks fine"
            )
        if dimension not in CARE_DIMENSION_BY_ID:
            errors.append(
                f"CARE_WEIGHT_RULES[{rule_id}] names dimension {dimension!r}, which "
                "CARE_DIMENSIONS does not declare"
            )
        if not str(rule.get("reason") or "").strip():
            warnings.append(
                f"CARE_WEIGHT_RULES[{rule_id}] has no `reason`; a weight a reader "
                "cannot question is one nobody will question"
            )
        try:
            float(rule["pull"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"CARE_WEIGHT_RULES[{rule_id}] has no numeric pull")

    signal_ids = [str(row["signal_id"]) for row in CARE_WEIGHT_RULES]
    if len(set(signal_ids)) < len(signal_ids):
        warnings.append(
            "two rules read the same signal into different dimensions. That can be "
            "deliberate, but it means the signal's weight is counted twice"
        )

    for dimension, spec in CARE_DIMENSION_BY_ID.items():
        low, high = (float(v) for v in spec["bounds"])  # type: ignore[misc]
        if low > high:
            errors.append(f"CARE_DIMENSIONS[{dimension}] bounds are inverted")
        neutral = float(spec["neutral"])
        if neutral < low or neutral > high:
            errors.append(
                f"CARE_DIMENSIONS[{dimension}] neutral {neutral} is outside its "
                f"bounds {low}..{high}, so the neutral case would clamp"
            )
        # `recovery_emphasis` is the one-directional one: it is *effort*, and a
        # learned weight that made us care about somebody less is a weight system
        # that eventually will.
        #
        # `generosity_nudge` is deliberately exempt. A nudge down is the point of a
        # nudge, the band is narrow (0.75-1.25), and the amount is still floored by
        # `customer_offers` with a per-kind cap on top -- so the real floor is
        # enforced where the money is, not here where there is none. Applying the
        # effort rule to it was over-reach, and it would have made the dimension
        # incapable of doing its job.
        if dimension == "recovery_emphasis" and low < 1.0:
            errors.append(
                f"CARE_DIMENSIONS[{dimension}] has a floor below 1.0; a learned "
                "weight could make us care about somebody less, and it will"
            )
        if dimension == "follow_up_hours":
            if low >= float(spec["default"]):
                errors.append(
                    "CARE_DIMENSIONS[follow_up_hours] has a floor at or above its "
                    "default, so a weight could never bring a check-back forward"
                )
            from app.services import relationship_health

            sla = relationship_health.RELATIONSHIP_HEALTH_BANDS_BY_ID["critical"]["sla_hours"]
            if low > float(sla):
                errors.append(
                    f"CARE_DIMENSIONS[follow_up_hours] floor {low}h is above the "
                    f"critical band's {sla}h SLA; nothing learned may quietly push "
                    "a check-back past the one deadline anybody is accountable for"
                )
        if not str(spec.get("why") or "").strip():
            warnings.append(f"CARE_DIMENSIONS[{dimension}] has no `why`")

    try:
        from app.services import complaint_learning

        authority = dict(complaint_learning.LEARNED_WEIGHT_AUTHORITY)
        for power in REFUSED_LEARNED_POWERS:
            if power not in authority:
                errors.append(
                    f"REFUSED_LEARNED_POWERS names {power!r}, which "
                    "complaint_learning.LEARNED_WEIGHT_AUTHORITY no longer declares"
                )
            elif bool(authority.get(power)):
                errors.append(
                    f"{power} is True upstream. This module refuses it by design, so "
                    "either the authority table was relaxed without a decision here, "
                    "or this module is claiming a power it must not have"
                )
        granted = sorted(
            name
            for name, value in authority.items()
            if bool(value) and name not in REFUSED_LEARNED_POWERS
        )
        if not granted:
            warnings.append(
                "LEARNED_WEIGHT_AUTHORITY grants no powers at all; if that is "
                "deliberate this module has nothing it may change, and if it is not "
                "then the refusals are wrong"
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"could not read complaint_learning.LEARNED_WEIGHT_AUTHORITY: {exc}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "dimensions": len(CARE_DIMENSIONS),
        "rules": len(CARE_WEIGHT_RULES),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(CARE_WEIGHT_RULES)} rules and {len(CARE_DIMENSIONS)} dimensions"
        ),
    }


def build_care_weights_catalog() -> dict[str, Any]:
    """The tables, published, so what a learned weight may do is reviewable."""
    return {
        "catalog_version": CARE_WEIGHTS_CATALOG_VERSION,
        "dimensions": [dict(row) for row in CARE_DIMENSIONS],
        "rules": [dict(row) for row in CARE_WEIGHT_RULES],
        "refused_powers": list(REFUSED_LEARNED_POWERS),
        "note": (
            "emphasis, not verdict. a learned complaint weight may change how hard "
            "we work a customer, how soon somebody checks back, how generously a "
            "goodwill offer is nudged, and whether an unprompted message leads with "
            "an acknowledgement. it may not change severity, tier, or whether "
            "anything escalates -- those three refusals come from "
            "complaint_learning.LEARNED_WEIGHT_AUTHORITY and the validator reads "
            "that table rather than restating it, so relaxing one upstream fails "
            "here. every dimension is floored in the direction that means worse"
        ),
    }