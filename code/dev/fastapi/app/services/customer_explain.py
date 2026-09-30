"""Plain-language explanation of the decisions that affect a customer.

The gap this fills
------------------
``app/explainability.py`` narrates *infrastructure* decisions -- a risk score,
a canary comparison, a model promotion. Its ``customer`` audience is a
redaction profile over that same infra vocabulary, and its `FACTOR_PHRASING`
table maps `rule_id` prefixes like ``new_device`` and ``geo_velocity``. A
customer asking "why is my loyalty score 62?" gets a trace with a `policy_tier`
and no template that can render it, because no such template exists.

So this is a second, *business* vocabulary over the same promise, not a
replacement. It reads the decisions the customer already has (loyalty score,
churn risk, recovery readiness, policy posture, points, arrears) and states
each one as a claim with its evidence. The three properties that make it
useful rather than marketing copy:

1. **Every explanation carries the arithmetic.** ``factor`` rows carry the
   real number and the threshold it was compared against, so "your loyalty
   score is 62" is always followed by the three terms that produced it. An
   explanation that cannot be checked is an assertion.
2. **Nothing is softened.** A churn risk of ``high`` is described as a churn
   risk of ``high``. The tables carry a ``plain`` phrase per band and the
   phrase for ``high`` is not kinder than the word.
3. **Direction is a computed fact, not a writer's choice.** ``impact`` is
   derived from the arithmetic by :func:`_impact_of`, so a factor can never be
   described as "helping" when it subtracted from the score.

The one softening that *is* deliberate: scores are described in bands
(``comfortable`` / ``workable`` / ``at risk``) rather than as the number alone,
because a bare 0-100 number implies a precision the score does not have --
``build_summary`` subtracts capped heuristics, and the band is the honest unit.
The number still travels alongside it. Banding without the number would be the
dishonest version, and that is the one this module refuses to produce.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app import explainability

#: Version of the shipped phrase set. A client holding a stale version is told
#: the current one, because "your churn risk is high" rendered by a different
#: vocabulary than the score that produced it is a drift nobody would notice.
EXPLANATION_VOCABULARY_VERSION = "customer_explain_v1"

#: Which audiences a business explanation is written for. `customer` and
#: `agent` share the vocabulary and differ only in how much arithmetic is
#: shown -- an agent gets the thresholds, a customer gets the terms.
EXPLANATION_AUDIENCES: tuple[str, ...] = ("customer", "agent")
DEFAULT_EXPLANATION_AUDIENCE = "agent"


# ---------------------------------------------------------------------------
# Band vocabulary
# ---------------------------------------------------------------------------
# Numeric bands with a plain phrase. `direction` is the honest reading of the
# band and is not negotiable per-row: a band whose `direction` disagreed with
# the maths would be a bug in the table, so `validate_explanation_tables`
# checks it.

SCORE_BANDS: list[dict[str, Any]] = [
    {
        "band": "strong",
        "min": 80.0,
        "max": 100.0,
        "plain": "in good shape",
        "direction": "positive",
        "meaning": "This is working well. No action is needed from you.",
    },
    {
        "band": "healthy",
        "min": 65.0,
        "max": 80.0,
        "plain": "healthy",
        "direction": "positive",
        "meaning": "This is fine. We are keeping an eye on it.",
    },
    {
        "band": "workable",
        "min": 50.0,
        "max": 65.0,
        "plain": "workable, with room to improve",
        "direction": "neutral",
        "meaning": "Not a problem, but there is a specific thing that would make it better.",
    },
    {
        "band": "at_risk",
        "min": 30.0,
        "max": 50.0,
        "plain": "at risk",
        "direction": "negative",
        "meaning": "Something is going wrong. We are acting on it.",
    },
    {
        "band": "critical",
        "min": 0.0,
        "max": 30.0,
        "plain": "critical",
        "direction": "negative",
        "meaning": "This needs attention now. If we have not reached out, that is a miss on our side.",
    },
]

#: Band for a metric where a *low* number is good (dissatisfaction, incident
#: count). Kept as a separate table rather than a `higher_is_better` flag on
#: the one above: the thresholds genuinely differ, and a flag on one table
#: would hide that.
INVERSE_SCORE_BANDS: list[dict[str, Any]] = [
    {
        "band": "clear",
        "min": 0.0,
        "max": 5.0,
        "plain": "clear",
        "direction": "positive",
        "meaning": "No unresolved friction detected.",
    },
    {
        "band": "minor",
        "min": 5.0,
        "max": 10.0,
        "plain": "minor friction",
        "direction": "neutral",
        "meaning": "Something small is unresolved. Usually it clears on its own.",
    },
    {
        "band": "significant",
        "min": 10.0,
        "max": 18.0,
        "plain": "significant friction",
        "direction": "negative",
        "meaning": "A real problem. We are prioritising it.",
    },
    {
        "band": "severe",
        "min": 18.0,
        "max": 100.0,
        "plain": "severe friction",
        "direction": "negative",
        "meaning": "An open problem we consider urgent. You should expect contact.",
    },
]

#: Label -> what it means for the customer, in their words. One vocabulary, so
#: `churn_risk: high` and a `recovery_readiness` of `critical` cannot end up
#: described in two different registers by two different writers.
CHURN_RISK_PHRASES: dict[str, str] = {
    "low": (
        "We do not expect you to leave. Nothing needs your attention on this."
    ),
    "medium": (
        "There is a chance you disengage if nothing changes. The usual causes are a "
        "slow answer or an unresolved question."
    ),
    "high": (
        "We are treating this as a customer we could lose, and we are acting on it "
        "before reaching out to you commercially."
    ),
    "critical": (
        "We consider this an active risk of losing you. Recovery actions are running."
    ),
}

RECOVERY_READINESS_PHRASES: dict[str, str] = {
    "low": "No recovery work is needed. Your experience looks good.",
    "moderate": (
        "We are watching one open thread. If it does not clear on its own, we will "
        "follow up."
    ),
    "high": (
        "We have opened a recovery case. Expect contact, and expect something "
        "concrete rather than an apology."
    ),
    "critical": (
        "This is our highest recovery priority. A senior contact is assigned and a "
        "callback is scheduled."
    ),
}

POLICY_TIER_PHRASES: dict[str, str] = {
    "system-premium": "Your account is in our most trusted tier, which unlocks our widest access.",
    "customer-premium": (
        "Your account earns the trusted tier based on your own history, independent of "
        "the system-wide setting."
    ),
    "standard": "Your account is on the standard tier. Higher tiers unlock more access.",
}

CONTROL_POSTURE_PHRASES: dict[str, str] = {
    "high_trust": "We operate with full access on your behalf. Nothing is being withheld.",
    "customer_trusted": (
        "We act on your behalf with light supervision, based on your history."
    ),
    "observed": (
        "Some actions are watched before they take effect. This is normal for your "
        "tier and not a sign of a problem."
    ),
    "constrained": (
        "Some actions need review before they take effect. This is a safeguard applied "
        "by policy, not a judgement about you."
    ),
}

#: access band -> what it means for the customer. The four bands are exactly
#: what ``policy_scoring.ACCESS_BAND_RULES`` plus its ``default_band`` can
#: emit; ``validate_explanation_tables`` fails if that ever stops being true.
ACCESS_BAND_PHRASES: dict[str, str] = {
    "elite": "Your access score is in our top band. Nothing is restricted on your account.",
    "strong": "Your access score is high. Only the very highest-risk operations are gated.",
    "moderate": "A small number of higher-risk actions are gated until your standing improves.",
    "limited": (
        "Privileged actions are gated. Resolving open issues is the fastest way to "
        "lift this."
    ),
}

VALUE_TIER_PHRASES: dict[str, str] = {
    "premium": "You are in our highest-value group for service prioritisation.",
    "growth": "You are growing with us and eligible for expanded offers.",
    "standard": "You are on standard service levels.",
    "care": "You are receiving our attention-first care path.",
}

LIFECYCLE_STAGE_PHRASES: dict[str, str] = {
    "new": "You are new here. We are still getting you set up.",
    "engaged": "You are actively using the service.",
    "loyal": "You are a returning customer with a track record of completed work.",
    "recovering": "You are re-engaging after a quiet period.",
    "at_risk": "You are flagged for retention attention.",
    "none": "Your journey stage is not yet determined.",
}

#: What each recovery action *did for the customer*. This is the translation
#: layer that turns `credit_points` into "we credited you", and it is
#: deliberately written from the customer's side -- the internal name of the
#: action is not the sentence a customer should read.
RECOVERY_ACTION_CUSTOMER_IMPACT: dict[str, str] = {
    "credit_points": (
        "We added points to your balance as an apology. You can spend them or "
        "exchange them for money like any other points."
    ),
    "escalate_ticket": (
        "We escalated your case to a senior specialist, so you are not waiting in "
        "the normal queue."
    ),
    "adjust_policy_score": (
        "We temporarily raised your access score so the perks you are entitled to are "
        "not withheld while we fix the problem."
    ),
    "notify_customer": (
        "We decided how and when to reach you based on how you prefer to be "
        "contacted. This record is the decision; nothing was sent by it."
    ),
    "schedule_callback": (
        "We scheduled a callback so this gets a human rather than another automated "
        "reply."
    ),
    "offer_save_incentive": (
        "We prepared a retention offer for you. It is a preview -- nothing has been "
        "applied to your account."
    ),
    "flag_for_review": (
        "We flagged your case for a person to review. Automated handling was not "
        "resolving this."
    ),
}

#: Status -> plain phrase. A `skipped` action is the one that most needs honest
#: phrasing: a guard stopped it, and saying nothing would leave the customer
#: thinking they received something.
RECOVERY_STATUS_PHRASES: dict[str, str] = {
    "executed": "This was carried out.",
    "would_execute": "This would have been carried out, in preview mode only.",
    "skipped": (
        "We decided not to repeat this right now. Repeating it would have been "
        "excessive; it is recorded so the decision is visible."
    ),
    "failed": "We tried this and it did not work. It is logged so it can be retried properly.",
    "guard_rejected": (
        "Our own limits stopped this from running, which is the system protecting you "
        "from repeated automated actions."
    ),
    "unknown": "The outcome of this action was not recorded.",
}

#: Next steps a customer can actually take, by the subject of the decision.
#: A step is only listed when the corresponding decision is in a state that
#: warrants it, so this is a table plus a predicate rather than a fixed list.
NEXT_STEP_TEMPLATES: list[dict[str, Any]] = [
    {
        "step_id": "resolve_open_thread",
        "when_metric": "dissatisfaction",
        "applies_when": {"min": 10.0},
        "step": "Tell us what is still unresolved, even if you have said it before.",
        "owner": "customer",
    },
    {
        "step_id": "expect_callback",
        "when_metric": "recovery_readiness",
        "applies_when": {"in": ["high", "critical"]},
        "step": "A callback is scheduled. You do not need to chase it.",
        "owner": "us",
    },
    {
        "step_id": "spend_points",
        "when_metric": "points_balance",
        "applies_when": {"min": 1.0},
        "step": "You have points available. Check the exchange rate before converting.",
        "owner": "customer",
    },
    {
        "step_id": "confirm_booking_state",
        "when_metric": "pending_bookings",
        "applies_when": {"min": 1.0},
        "step": "You have a booking still pending confirmation. Confirming it is the fastest way to move forward.",
        "owner": "customer",
    },
    {
        "step_id": "adjust_preferences",
        "when_metric": "churn_risk",
        "applies_when": {"in": ["high", "critical"]},
        "step": "If we are contacting you too often or not enough, change it in your preferences.",
        "owner": "customer",
    },
    {
        "step_id": "review_policy_posture",
        "when_metric": "control_posture",
        "applies_when": {"in": ["observed", "constrained"]},
        "step": "Some actions need review under your current access standing. This usually lifts once open issues are resolved.",
        "owner": "us",
    },
]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _band_for(value: Any, table: list[dict[str, Any]]) -> dict[str, Any]:
    """The band row containing ``value``. Out-of-range clamps to the ends.

    Clamping rather than returning ``None`` is deliberate: an explanation that
    says "no band" for a score of 105 would tell the customer nothing, and a
    score above the top band is still in the top band as far as they are
    concerned. The *value* is reported alongside, so clamping is visible.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return dict(table[0]) | {"band": "unknown", "clamped": False, "value": value}
    for row in table:
        low = float(row["min"])
        high = float(row["max"])
        if low <= number < high:
            return {**row, "clamped": False, "value": round(number, 2)}
    if number < float(table[-1]["min"]):
        return {**table[-1], "clamped": True, "value": round(number, 2)}
    return {**table[0], "clamped": True, "value": round(number, 2)}


def band_for_score(value: Any) -> dict[str, Any]:
    return _band_for(value, SCORE_BANDS)


def band_for_friction(value: Any) -> dict[str, Any]:
    return _band_for(value, INVERSE_SCORE_BANDS)


def _impact_of(contribution: Any, *, higher_is_better: bool) -> str:
    """Direction of a term, from the sign of its contribution.

    Computed, not declared. A table that let a factor *say* it helped while the
    arithmetic subtracted from the score is the exact failure this function
    exists to make impossible.
    """
    try:
        number = float(contribution)
    except (TypeError, ValueError):
        return "neutral"
    if number == 0.0:
        return "neutral"
    positive = number > 0.0
    if higher_is_better:
        return "positive" if positive else "negative"
    return "negative" if positive else "positive"


def _phrase_for(mapping: dict[str, str], value: Any, fallback: str) -> str:
    return mapping.get(str(value or ""), fallback)


def _applies(step: dict[str, Any], metrics: dict[str, Any]) -> bool:
    """Does this next-step template apply to the current metrics?"""
    metric = str(step.get("when_metric", ""))
    if metric not in metrics:
        return False
    actual = metrics[metric]
    rule = step.get("applies_when") or {}
    if "min" in rule:
        try:
            if float(actual) < float(rule["min"]):
                return False
        except (TypeError, ValueError):
            return False
    if "max" in rule:
        try:
            if float(actual) > float(rule["max"]):
                return False
        except (TypeError, ValueError):
            return False
    if "in" in rule and str(actual) not in [str(item) for item in rule["in"]]:
        return False
    return True


def next_steps_for(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """The next steps whose conditions hold, in template order.

    Order is the declared table order, not a severity sort: a customer reading
    "tell us what is unresolved" above "you have points available" is reading a
    prioritised list, and re-sorting it by a metric name nobody can act on would
    make the ordering arbitrary.
    """
    return [
        {
            "step_id": str(step["step_id"]),
            "step": str(step["step"]),
            "owner": str(step.get("owner", "us")),
            "because": f"{step['when_metric']}={metrics.get(step['when_metric'])}",
        }
        for step in NEXT_STEP_TEMPLATES
        if _applies(step, metrics)
    ]


def confidence_for(evidence_count: int, *, has_model_version: bool = False) -> str:
    """Confidence band from how much evidence stood behind the decision.

    Explicitly *not* derived from the score's magnitude. "We are confident"
    means "we had evidence", and a score of 3 computed from twelve signals is a
    more confident statement of a bad thing than a score of 50 computed from
    one. A confidence derived from the score would reward the model for
    producing extreme values.
    """
    count = max(0, int(evidence_count))
    if has_model_version:
        count += 2
    if count >= 5:
        return "high"
    if count >= 2:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Explanations
# ---------------------------------------------------------------------------


def explain_loyalty_score(
    summary: Any,
    *,
    audience: str = DEFAULT_EXPLANATION_AUDIENCE,
) -> dict[str, Any]:
    """Explain the loyalty score from the insights that produced it.

    Reads ``summary.insights`` rather than recomputing the score. That is a
    deliberate asymmetry: ``build_summary`` subtracts capped heuristics from
    100, and restating the formula here would create a second implementation
    that drifts. The explanation therefore reports the *inputs* it can see and
    labels the total as computed elsewhere, rather than pretending to verify it.
    """
    insights = list(getattr(summary, "insights", None) or [])
    metadata = dict(getattr(summary, "metadata", None) or {})
    score = float(getattr(summary, "loyalty_score", 0.0) or 0.0)
    band = band_for_score(score)

    # The total deduction each insight represents, capped the same way
    # build_summary caps it. Recomputing the *shape* of the deduction is safe
    # because it is read off the same capped per-area scores; the arithmetic
    # itself is not re-derived, and `deduction_reproduced` says whether the
    # visible terms add up on their own.
    insight_deduction = min(sum(float(i.score) for i in insights), 35.0)
    repeated = int(metadata.get("repeated_messages", 0) or 0)
    repeat_deduction = min(repeated * 4.0, 12.0)
    factors: list[dict[str, Any]] = []
    for insight in insights:
        contribution = -min(float(insight.score), 35.0)
        factors.append(
            {
                "factor": str(insight.area),
                "impact": _impact_of(contribution, higher_is_better=True),
                "weight": round(abs(contribution), 2),
                "explanation": (
                    f"We detected {str(insight.area).replace('_', ' ')} in your "
                    f"recent interactions, which reduced your loyalty score."
                ),
                "evidence": str(insight.evidence_summary or ""),
            }
        )
    if repeated:
        deduction = -repeat_deduction
        factors.append(
            {
                "factor": "repeated_concerns",
                "impact": _impact_of(deduction, higher_is_better=True),
                "weight": round(repeat_deduction, 2),
                "explanation": (
                    f"You raised the same concern {repeated} time(s). Repeating an "
                    "unresolved issue reduces the score."
                ),
                "evidence": f"repeated_messages={repeated}",
            }
        )
    sentiment_reported = False
    for insight in insights:
        if str(insight.area) == "sentiment":
            sentiment_reported = True
            factors.insert(
                0,
                {
                    "factor": "negative_sentiment",
                    "impact": "negative",
                    "weight": 10.0,
                    "explanation": (
                        "Your most recent message read as unhappy. That reduces the "
                        "score until the issue is resolved."
                    ),
                    "evidence": str(insight.evidence_summary or ""),
                },
            )
            break
    factors.sort(key=lambda item: item["weight"], reverse=True)
    if audience == "customer":
        factors = factors[:3]

    visible_deductions = round(-sum(f["weight"] for f in factors), 2)
    return {
        "subject": "loyalty_score",
        "audience": audience,
        "value": score,
        "band": band["band"],
        "band_phrase": band["plain"],
        "direction": band["direction"],
        "meaning": band["meaning"],
        "factors": factors,
        "evidence_count": len(insights) + int(bool(repeated)) + int(sentiment_reported),
        "deduction_visible": visible_deductions,
        "deduction_reproduced": abs(
            visible_deductions - round(insight_deduction + repeat_deduction + (10.0 if sentiment_reported else 0.0), 2)
        ) < 0.05,
        "sentiment_reported": sentiment_reported,
        "summary": (
            f"Your loyalty score is {score:.0f} out of 100, which we read as "
            f"{band['plain']}. {band['meaning']}"
        ),
        "next_steps": next_steps_for(
            {
                "loyalty_score": score,
                "dissatisfaction": float(metadata.get("dissatisfaction_score", 0.0) or 0.0),
            }
        ),
    }


def explain_churn_risk(
    summary: Any,
    churn_prediction: Any = None,
    *,
    audience: str = DEFAULT_EXPLANATION_AUDIENCE,
) -> dict[str, Any]:
    """Explain the churn-risk label, with the risk score's terms when available."""
    risk = str(getattr(summary, "churn_risk", "low"))
    risk_score = getattr(churn_prediction, "risk_score", None)
    warning_reasons = list(getattr(churn_prediction, "warning_reasons", None) or [])
    factors = [
        {
            "factor": f"reason_{index + 1}",
            "impact": "negative",
            "weight": 1.0,
            "explanation": str(reason),
            "evidence": str(reason),
        }
        for index, reason in enumerate(warning_reasons)
    ]
    band = band_for_score(
        float(risk_score) if risk_score is not None else float(getattr(summary, "loyalty_score", 0.0) or 0.0)
    )
    return {
        "subject": "churn_risk",
        "audience": audience,
        "value": risk,
        "risk_score": risk_score,
        "band": band["band"],
        "direction": band["direction"],
        "factors": factors if audience != "customer" else factors[:3],
        "evidence_count": len(warning_reasons),
        "meaning": _phrase_for(CHURN_RISK_PHRASES, risk, "This is not yet classified."),
        "summary": (
            f"Your churn risk is {risk}. "
            + _phrase_for(CHURN_RISK_PHRASES, risk, "")
        ),
        "next_steps": next_steps_for({"churn_risk": risk, "dissatisfaction": 0.0}),
    }


def explain_recovery_status(
    context: dict[str, Any],
    *,
    audience: str = DEFAULT_EXPLANATION_AUDIENCE,
) -> dict[str, Any]:
    """Explain recovery readiness from the realtime dissatisfaction context."""
    readiness = str(context.get("recovery_readiness", "low"))
    score = float(context.get("dissatisfaction_score", 0.0) or 0.0)
    band = band_for_friction(score)
    risks = [str(risk) for risk in (context.get("primary_risks") or [])]
    factors = [
        {
            "factor": str(risk).replace(" ", "_"),
            "impact": "negative",
            "weight": 1.0,
            "explanation": f"We detected {risk} as an open risk in your recent interactions.",
            "evidence": str(risk),
        }
        for risk in risks
    ]
    factors.append(
        {
            "factor": "dissatisfaction_score",
            "impact": band["direction"],
            "weight": round(score, 2),
            "explanation": (
                f"Your overall friction score is {score:.1f}, which we read as "
                f"{band['plain']}."
            ),
            "evidence": band["meaning"],
        }
    )
    if audience == "customer":
        factors = factors[:3]
    return {
        "subject": "recovery_readiness",
        "audience": audience,
        "value": readiness,
        "friction_score": score,
        "band": band["band"],
        "direction": band["direction"],
        "factors": factors,
        "evidence_count": len(risks) + 1,
        "meaning": _phrase_for(RECOVERY_READINESS_PHRASES, readiness, ""),
        "summary": (
            f"Your recovery status is {readiness}. "
            + _phrase_for(RECOVERY_READINESS_PHRASES, readiness, "")
        ),
        "next_steps": next_steps_for(
            {"recovery_readiness": readiness, "dissatisfaction": score}
        ),
    }


def explain_policy_posture(
    snapshot: Any,
    *,
    audience: str = DEFAULT_EXPLANATION_AUDIENCE,
) -> dict[str, Any]:
    """Explain the policy tier / posture / band, plus what each gates.

    ``access_band`` is not on the snapshot dataclass, so it is resolved with the
    canonical helper rather than approximated from the access score -- the
    published number and the published band have to come from the same
    resolution path or they can disagree for a customer reading both.
    """
    from app.services.policy_scoring import resolve_access_band

    tier = str(getattr(snapshot, "policy_tier", "standard"))
    posture = str(getattr(snapshot, "control_posture", "observed"))
    access = float(getattr(snapshot, "access_score", 0.0) or 0.0)
    band = resolve_access_band(access)
    factors = [
        {
            "factor": "access_score",
            "impact": _impact_of(access, higher_is_better=True),
            "weight": round(access, 2),
            "explanation": (
                f"Your access score is {access:.1f} out of 100. It is what decides "
                "which tier you are in and which actions are gated."
            ),
            "evidence": _phrase_for(ACCESS_BAND_PHRASES, band, ""),
        },
        {
            "factor": "customer_score",
            "impact": _impact_of(
                float(getattr(snapshot, "customer_score", 0.0) or 0.0),
                higher_is_better=True,
            ),
            "weight": round(float(getattr(snapshot, "customer_score", 0.0) or 0.0), 2),
            "explanation": (
                "Your customer score reflects your history with us and contributes to "
                "the access score."
            ),
            "evidence": str(getattr(snapshot, "summary", "") or ""),
        },
        {
            "factor": "system_score",
            "impact": "neutral",
            "weight": round(float(getattr(snapshot, "system_score", 0.0) or 0.0), 2),
            "explanation": (
                "The system-wide trust level. It sets the ceiling for your tier, so it "
                "can cap you but never penalise you below the standard floor."
            ),
            "evidence": "system-wide setting, not specific to you",
        },
    ]
    if audience == "customer":
        factors = [factor for factor in factors if factor["factor"] != "system_score"] or factors[:1]
    return {
        "subject": "policy_posture",
        "audience": audience,
        "value": {"policy_tier": tier, "control_posture": posture, "access_band": band},
        "tier": tier,
        "posture": posture,
        "access_band": band,
        "factors": factors,
        "evidence_count": 3,
        "tier_phrase": _phrase_for(POLICY_TIER_PHRASES, tier, ""),
        "posture_phrase": _phrase_for(CONTROL_POSTURE_PHRASES, posture, ""),
        "band_phrase": _phrase_for(ACCESS_BAND_PHRASES, band, ""),
        "summary": (
            f"You are on the {tier} tier with a {posture} control posture. "
            + _phrase_for(CONTROL_POSTURE_PHRASES, posture, "")
        ),
        "next_steps": next_steps_for(
            {"control_posture": posture, "access_score": access}
        ),
    }


def explain_value_tier(
    summary: Any,
    *,
    audience: str = DEFAULT_EXPLANATION_AUDIENCE,
) -> dict[str, Any]:
    tier = str(getattr(summary, "value_tier", "standard"))
    readiness = float(getattr(summary, "monetization_readiness", 0.0) or 0.0)
    band = band_for_score(readiness)
    return {
        "subject": "value_tier",
        "audience": audience,
        "value": tier,
        "metric": readiness,
        "band": band["band"],
        "direction": band["direction"],
        "factors": [
            {
                "factor": "monetization_readiness",
                "impact": _impact_of(readiness, higher_is_better=True),
                "weight": round(readiness, 2),
                "explanation": (
                    f"Your expansion readiness is {readiness:.0f} out of 100. This is "
                    "what the value tier is derived from; it is not a judgement about "
                    "how much we want to spend on you."
                ),
                "evidence": band["meaning"],
            }
        ],
        "evidence_count": 1,
        "meaning": _phrase_for(VALUE_TIER_PHRASES, tier, ""),
        "summary": (
            f"You are in the {tier} value group. "
            + _phrase_for(VALUE_TIER_PHRASES, tier, "")
        ),
        "next_steps": [],
    }


def explain_lifecycle_stage(
    stage: str,
    *,
    drivers: Optional[list[str]] = None,
    audience: str = DEFAULT_EXPLANATION_AUDIENCE,
) -> dict[str, Any]:
    factors = [
        {
            "factor": f"driver_{index + 1}",
            "impact": "neutral",
            "weight": 1.0,
            "explanation": str(driver),
            "evidence": str(driver),
        }
        for index, driver in enumerate(drivers or [])
    ]
    return {
        "subject": "lifecycle_stage",
        "audience": audience,
        "value": str(stage),
        "factors": factors,
        "evidence_count": len(factors),
        "meaning": _phrase_for(LIFECYCLE_STAGE_PHRASES, stage, ""),
        "summary": (
            f"You are at the {stage} stage. "
            + _phrase_for(LIFECYCLE_STAGE_PHRASES, stage, "")
        ),
        "next_steps": [],
    }


# ---------------------------------------------------------------------------
# Recovery-action narration
# ---------------------------------------------------------------------------


def narrate_recovery_action(action: dict[str, Any]) -> dict[str, Any]:
    """Turn one recorded recovery action into what it means for the customer.

    ``status`` gets the most care here. A ``skipped`` action has a
    ``failure_reason`` naming the guard, and that reason is a *system* fact
    ("daily budget would be exceeded") that means nothing to a customer. The
    phrase in :data:`RECOVERY_STATUS_PHRASES` is what it actually means for
    them; the raw reason is retained under ``internal_reason`` so an agent can
    still see it, and the customer-facing text is not merely the internal one
    with punctuation removed.
    """
    action_name = str(action.get("action", "") or "unknown")
    status = str(action.get("status", "") or "unknown")
    result = dict(action.get("result") or {})
    playbook_id = str(action.get("playbook_id", "") or "")
    playbook = explainability.NARRATIVE_TEMPLATES.get("recovery_action", "")

    points = result.get("points_credited")
    outcome_detail = ""
    if points is not None:
        try:
            outcome_detail = f" {round(float(points), 2):g} points were added to your balance."
        except (TypeError, ValueError):
            outcome_detail = ""
    reference = str(action.get("reference", "") or result.get("ticket_reference", "") or "")
    if reference and not outcome_detail:
        outcome_detail = f" Your reference is {reference}."

    return {
        "action": action_name,
        "playbook_id": playbook_id,
        "status": status,
        "customer_impact": _phrase_for(
            RECOVERY_ACTION_CUSTOMER_IMPACT,
            action_name,
            "A recovery action was recorded for your account.",
        ),
        "outcome": _phrase_for(RECOVERY_STATUS_PHRASES, status, ""),
        "outcome_detail": outcome_detail.strip(),
        "reference": reference,
        "internal_reason": str(action.get("failure_reason", "") or ""),
        "summary": " ".join(
            part
            for part in [
                _phrase_for(RECOVERY_ACTION_CUSTOMER_IMPACT, action_name, ""),
                _phrase_for(RECOVERY_STATUS_PHRASES, status, ""),
                outcome_detail.strip(),
            ]
            if part
        ).strip()
        or f"Recovery action {action_name} recorded as {status}.",
        "template_available": bool(playbook),
    }


# ---------------------------------------------------------------------------
# Validation + catalog
# ---------------------------------------------------------------------------

#: Codes ``validate_explanation_tables`` can raise. A finding is an error only
#: when the shipped table is wrong; everything else is a warning.
EXPLANATION_CODES: dict[str, str] = {
    "band_direction_mismatch": "a band's direction disagrees with its numeric range",
    "band_range_overlap": "two bands claim the same numeric range",
    "band_gap": "the bands leave a numeric gap with no row to cover it",
    "unknown_metric": "a next-step template names a metric no explanation reports",
    "unreachable_phrase": "a phrase table has an entry no producer can emit",
    "missing_phrase": "a producer can emit a value the phrase table does not cover",
    "unreachable_step": "a next-step template can never apply",
    "impact_direction_mismatch": "a factor's impact disagrees with the sign of its weight",
}


def _producible_labels(table: dict[str, str], producer: str) -> list[str]:
    """The labels a producer can actually emit, per ``producer``."""
    if producer == "churn_risk":
        return ["low", "medium", "high", "critical"]
    if producer == "recovery_readiness":
        return ["low", "moderate", "high", "critical"]
    if producer == "control_posture":
        try:
            from app.services.policy_scoring import (
                CONTROL_POSTURE_RULES,
                CONTROL_POSTURE_TIER_MAP,
                POLICY_SCORING_OPS,
            )

            # Postures come from three places: the explicit rule table, the tier
            # map for the two premium tiers, and the default for everything the
            # rules do not match. All three are producible, and reading only the
            # rule table made the default look unreachable.
            labels = [str(row.get("posture", "")) for row in CONTROL_POSTURE_RULES]
            labels.extend(str(value) for value in CONTROL_POSTURE_TIER_MAP.values())
            default_posture = POLICY_SCORING_OPS.get("default_posture")
            if default_posture:
                labels.append(str(default_posture))
            return labels
        except Exception:  # pragma: no cover - import-time guard only
            return []
    if producer == "access_band":
        try:
            from app.services.policy_scoring import (
                ACCESS_BAND_RULES,
                POLICY_SCORING_OPS,
            )

            # The default band is producible too: `resolve_access_band` falls
            # back to `POLICY_SCORING_OPS["default_band"]` when no rule matches.
            # Reading only ACCESS_BAND_RULES therefore reported `limited` as an
            # unreachable phrase -- the same "the check read a partial view"
            # defect the i18n and audit-contract passes each found once.
            labels = [str(row.get("band", "")) for row in ACCESS_BAND_RULES]
            default_band = POLICY_SCORING_OPS.get("default_band")
            if default_band:
                labels.append(str(default_band))
            return labels
        except Exception:  # pragma: no cover - import-time guard only
            return []
    if producer == "policy_tier":
        return ["system-premium", "customer-premium", "standard"]
    if producer == "value_tier":
        return ["premium", "growth", "standard", "care"]
    if producer == "lifecycle_stage":
        return ["new", "engaged", "loyal", "recovering", "at_risk", "none"]
    if producer == "recovery_action":
        try:
            from app.services.recovery_playbooks import RECOVERY_ACTION_SPEC_BY_NAME

            return sorted(RECOVERY_ACTION_SPEC_BY_NAME)
        except Exception:  # pragma: no cover
            return []
    if producer == "recovery_status":
        try:
            from app.services.recovery_playbooks import (
                RECOVERY_GUARD_DRY_RUN_SKIP_STATUS,
                RECOVERY_GUARD_REASON_STATUS,
                RECOVERY_GUARD_SKIP_STATUS,
            )

            return [
                "executed",
                "would_execute",
                RECOVERY_GUARD_SKIP_STATUS,
                RECOVERY_GUARD_REASON_STATUS,
                "failed",
                "unknown",
            ]
        except Exception:  # pragma: no cover
            return ["executed", "failed", "unknown"]
    return []


#: phrase table name -> (producer, expected labels, whether labels are closed)
PHRASE_TABLE_BINDINGS: dict[str, dict[str, Any]] = {
    "CHURN_RISK_PHRASES": {"producer": "churn_risk", "closed": True},
    "RECOVERY_READINESS_PHRASES": {"producer": "recovery_readiness", "closed": True},
    "POLICY_TIER_PHRASES": {"producer": "policy_tier", "closed": True},
    "CONTROL_POSTURE_PHRASES": {"producer": "control_posture", "closed": True},
    "ACCESS_BAND_PHRASES": {"producer": "access_band", "closed": True},
    "VALUE_TIER_PHRASES": {"producer": "value_tier", "closed": True},
    "LIFECYCLE_STAGE_PHRASES": {"producer": "lifecycle_stage", "closed": True},
    "RECOVERY_ACTION_CUSTOMER_IMPACT": {"producer": "recovery_action", "closed": True},
    "RECOVERY_STATUS_PHRASES": {"producer": "recovery_status", "closed": True},
}

PHRASE_TABLES: dict[str, dict[str, str]] = {
    "CHURN_RISK_PHRASES": CHURN_RISK_PHRASES,
    "RECOVERY_READINESS_PHRASES": RECOVERY_READINESS_PHRASES,
    "POLICY_TIER_PHRASES": POLICY_TIER_PHRASES,
    "CONTROL_POSTURE_PHRASES": CONTROL_POSTURE_PHRASES,
    "ACCESS_BAND_PHRASES": ACCESS_BAND_PHRASES,
    "VALUE_TIER_PHRASES": VALUE_TIER_PHRASES,
    "LIFECYCLE_STAGE_PHRASES": LIFECYCLE_STAGE_PHRASES,
    "RECOVERY_ACTION_CUSTOMER_IMPACT": RECOVERY_ACTION_CUSTOMER_IMPACT,
    "RECOVERY_STATUS_PHRASES": RECOVERY_STATUS_PHRASES,
}

#: Metrics a next-step template may name, and which explanation reports each.
STEP_METRIC_PRODUCERS: dict[str, str] = {
    "dissatisfaction": "recovery_readiness",
    "recovery_readiness": "recovery_status",
    "points_balance": "points",
    "pending_bookings": "interactions",
    "churn_risk": "churn_risk",
    "control_posture": "policy_posture",
    "loyalty_score": "loyalty_score",
}


def validate_explanation_tables() -> dict[str, Any]:
    """Check the shipped phrase and band tables against what the code emits.

    Report-only in the same sense as the i18n and audit-contract passes: a
    wrong phrase is a wording bug, and "fixing" it silently changes a string a
    customer is already reading. The interesting output is a non-empty finding
    list naming which producer and which label disagree.
    """
    findings: list[dict[str, Any]] = []

    for table_name, table in (("SCORE_BANDS", SCORE_BANDS), ("INVERSE_SCORE_BANDS", INVERSE_SCORE_BANDS)):
        seen: list[tuple[float, float, str]] = []
        for row in table:
            low, high = float(row["min"]), float(row["max"])
            if high <= low:
                findings.append(
                    {
                        "severity": "error",
                        "code": "band_range_overlap",
                        "table": table_name,
                        "band": row["band"],
                        "detail": f"band {row['band']!r} has max <= min",
                    }
                )
                continue
            for other_low, other_high, other_name in seen:
                if low < other_high and other_low < high:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "band_range_overlap",
                            "table": table_name,
                            "band": row["band"],
                            "detail": f"band {row['band']!r} overlaps {other_name!r}",
                        }
                    )
            seen.append((low, high, str(row["band"])))
            for neighbour_low, neighbour_high, _name in seen[:-1]:
                if abs(high - neighbour_high) < 1e-9 and abs(low - neighbour_low) < 1e-9:
                    continue
        # Gap check between consecutive bands, sorted by their lower bound.
        ordered = sorted(table, key=lambda row: float(row["min"]))
        for index in range(1, len(ordered)):
            previous_high = float(ordered[index - 1]["max"])
            current_low = float(ordered[index]["min"])
            if abs(previous_high - current_low) > 1e-9:
                findings.append(
                    {
                        "severity": "error",
                        "code": "band_gap",
                        "table": table_name,
                        "band": ordered[index]["band"],
                        "detail": (
                            f"gap between {ordered[index - 1]['band']!r} (max "
                            f"{previous_high}) and {ordered[index]['band']!r} (min {current_low})"
                        ),
                    }
                )
        for row in table:
            low, high = float(row["min"]), float(row["max"])
            direction = str(row["direction"])
            if direction not in {"positive", "neutral", "negative"}:
                findings.append(
                    {
                        "severity": "error",
                        "code": "band_direction_mismatch",
                        "table": table_name,
                        "band": row["band"],
                        "detail": f"direction {direction!r} is not a permitted value",
                    }
                )
            if "plain" not in row or not str(row["plain"]).strip():
                findings.append(
                    {
                        "severity": "error",
                        "code": "missing_phrase",
                        "table": table_name,
                        "band": row["band"],
                        "detail": "band has no plain-language phrase, so the score would be served bare",
                    }
                )
            if "meaning" not in row or not str(row["meaning"]).strip():
                findings.append(
                    {
                        "severity": "error",
                        "code": "missing_phrase",
                        "table": table_name,
                        "band": row["band"],
                        "detail": "band has no meaning sentence, so the score would be served unlabelled",
                    }
                )

        # Monotonicity is the real invariant here, and it is a property of the
        # table rather than of any hardcoded threshold: within one table,
        # `direction` must not go back towards "better" as the number grows.
        # An earlier version of this check compared each band's midpoint against
        # magic numbers (0.3 / 0.5 / 18.0), which flagged four correct bands and
        # proved nothing about the rows it did flag. Monotonicity cannot produce
        # a false positive: a table whose directions really are non-monotonic
        # *is* mislabelled, whatever the numbers are.
        rank = {"negative": -1, "neutral": 0, "positive": 1}
        for index in range(1, len(ordered)):
            previous = str(ordered[index - 1]["direction"])
            current = str(ordered[index]["direction"])
            if previous not in rank or current not in rank:
                continue
            if table_name == "SCORE_BANDS":
                # higher number is better: direction must not decrease
                regressed = rank[current] < rank[previous]
            else:
                # higher number is worse: direction must not increase
                regressed = rank[current] > rank[previous]
            if regressed:
                findings.append(
                    {
                        "severity": "error",
                        "code": "band_direction_mismatch",
                        "table": table_name,
                        "band": ordered[index]["band"],
                        "detail": (
                            f"band {ordered[index]['band']!r} declares direction "
                            f"{current!r} but {ordered[index - 1]['band']!r} above it "
                            f"declares {previous!r}; direction regresses as the value "
                            "moves away from the good end"
                        ),
                    }
                )

    for table_name, binding in PHRASE_TABLE_BINDINGS.items():
        table = PHRASE_TABLES[table_name]
        producible = _producible_labels(
            table,
            str(binding["producer"]),
        )
        for label in sorted(set(table) - set(producible)):
            findings.append(
                {
                    "severity": "warning",
                    "code": "unreachable_phrase",
                    "table": table_name,
                    "label": label,
                    "detail": (
                        f"phrase exists for {label!r} but {binding['producer']} cannot "
                        "emit it"
                    ),
                }
            )
        for label in sorted(set(producible) - set(table)):
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_phrase",
                    "table": table_name,
                    "label": label,
                    "detail": (
                        f"{binding['producer']} can emit {label!r} and no phrase covers it, "
                        "so the fallback text would be served instead"
                    ),
                }
            )

    for step in NEXT_STEP_TEMPLATES:
        metric = str(step["when_metric"])
        if metric not in STEP_METRIC_PRODUCERS:
            findings.append(
                {
                    "severity": "error",
                    "code": "unknown_metric",
                    "step_id": step["step_id"],
                    "detail": f"step names metric {metric!r} which no explanation reports",
                }
            )
            continue
        rule = step.get("applies_when") or {}
        if not rule:
            findings.append(
                {
                    "severity": "error",
                    "code": "unreachable_step",
                    "step_id": step["step_id"],
                    "detail": "step has no applies_when condition, so it can never apply",
                }
            )
            continue
        # A threshold outside the band table's range is a step that can never
        # fire. Checked against the band table the metric's producer uses.
        if "min" in rule and metric == "dissatisfaction":
            if float(rule["min"]) > float(INVERSE_SCORE_BANDS[-1]["max"]):
                findings.append(
                    {
                        "severity": "warning",
                        "code": "unreachable_step",
                        "step_id": step["step_id"],
                        "detail": f"min {rule['min']} exceeds the maximum reported value",
                    }
                )
        if "min" in rule and metric in {"loyalty_score", "churn_risk"}:
            if float(rule["min"]) > 100.0:
                findings.append(
                    {
                        "severity": "warning",
                        "code": "unreachable_step",
                        "step_id": step["step_id"],
                        "detail": f"min {rule['min']} exceeds the 0-100 score range",
                    }
                )

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc),
        "vocabulary_version": EXPLANATION_VOCABULARY_VERSION,
        "score_bands": len(SCORE_BANDS),
        "inverse_score_bands": len(INVERSE_SCORE_BANDS),
        "phrase_tables": len(PHRASE_TABLES),
        "phrases": sum(len(table) for table in PHRASE_TABLES.values()),
        "next_step_templates": len(NEXT_STEP_TEMPLATES),
        "audiences": list(EXPLANATION_AUDIENCES),
        "default_audience": DEFAULT_EXPLANATION_AUDIENCE,
        "codes": dict(EXPLANATION_CODES),
        "findings": findings,
        "counts_by_severity": counts,
        "ok": counts.get("error", 0) == 0,
        "note": (
            "report-only: the phrases in these tables are already being served, so "
            "correcting one changes a string a customer is reading. A missing phrase "
            "is an error because the fallback text would be served silently; a "
            "directional heuristic mismatch is a warning because the heuristic is "
            "looser than the band definitions it reads"
        ),
    }


def build_explainability_vocabulary_catalog() -> dict[str, Any]:
    """Introspection payload for ``/meta/scoring-catalog``."""
    return {
        "vocabulary_version": EXPLANATION_VOCABULARY_VERSION,
        "score_bands": [dict(row) for row in SCORE_BANDS],
        "inverse_score_bands": [dict(row) for row in INVERSE_SCORE_BANDS],
        "phrase_tables": {
            name: dict(table) for name, table in sorted(PHRASE_TABLES.items())
        },
        "phrase_bindings": {
            name: dict(binding) for name, binding in sorted(PHRASE_TABLE_BINDINGS.items())
        },
        "next_step_templates": [dict(row) for row in NEXT_STEP_TEMPLATES],
        "step_metric_producers": dict(sorted(STEP_METRIC_PRODUCERS.items())),
        "audiences": list(EXPLANATION_AUDIENCES),
        "default_audience": DEFAULT_EXPLANATION_AUDIENCE,
        "explains": [
            "loyalty_score",
            "churn_risk",
            "recovery_readiness",
            "policy_posture",
            "value_tier",
            "lifecycle_stage",
            "recovery_action",
        ],
        "note": (
            "a second, business vocabulary over the decisions a customer already "
            "has. app/explainability.py narrates infrastructure decisions and this "
            "narrates the ones a customer would ask about; they are complementary, "
            "not competing"
        ),
    }
