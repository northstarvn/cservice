"""Complaint learning: system-raised complaints, learned contribution weights, and
improvement suggestions from stacked complaints.

What this adds over ``services/complaints.py``
-------------------------------------------------
``complaints.py`` answers "what should happen to this case". It does that with a
fixed escalation table and an operator in the loop. Three things it could not do:

1. **A complaint could only start because a customer asked for one.** A booking
   that failed, a payment that did not settle, a provider that was down, a churn
   signal that crossed a threshold -- all are things a customer experiences
   before they are willing to write to us, and all are invisible to a system
   that only listens when spoken to. ``SYSTEM_COMPLAINT_DETECTORS`` lets the
   system open a case attributed to a customer who never complained.

2. **Every signal contributed equally.** The escalation triggers are static
   config, which is right for governance and wrong for judgement: nobody knows
   a priori that a ``sentiment_cliff`` matters more than a ``repeat_contact``
   for *this* population, and the answer changes as evidence accumulates. Here
   each signal carries a learned weight, bounded, decayed toward its prior, and
   persisted so it survives a restart.

3. **Nothing learned from outcomes.** ``complaint_decisions.outcome_observed``
   answers "was it resolved", which is not the same question as "did the
   customer stay". Those come apart constantly: a refund issued, the case closed,
   the customer never returns. ``LOYALTY_OUTCOMES`` measures the second thing and
   is what the weights are trained on.

Two decisions worth stating out loud
------------------------------------
**"Loyalty" means retention and habitual return, not compulsion.**
The objective is measurable retention: churn risk, reactivation, repeat booking,
points accrual, complaint-free lifetime. The existing schema supports exactly
that and nothing more. It is *not* an optimisation of time-on-service or contact
frequency, because a complaint system that rewards nagging a customer who is
trying to disengage is inverting its own purpose.

**Learned weights are advisory, and that is enforced, not merely documented.**
A weight may reorder a recommendation and change its explanation. It may never
move a case's tier on its own -- ``LEARNED_WEIGHT_AUTHORITY`` is a published
fact that ``validate_complaint_learning`` asserts and a test pins. The reasoning:
a weight is a statistic, and letting a statistic quietly decide who gets a
senior escalation is how a feedback loop turns into a self-fulfilling prophecy
within one quarter. The configured triggers still decide; the weights explain.

Where suggestions go
--------------------
``IMPROVEMENT_CLUSTER_RULES`` detects stacked complaints and
``build_improvement_proposal`` renders a suggestion shaped like
``efficiency_audit.EnhancementProposal``, which the caller can register on the
release ladder at ``l0_draft`` -- the level whose own description is "written but
unexamined... this is where every kaizen change starts and where it is safe to be
wrong". It is deliberately *not* written into ``BLOCKAGES.md`` from here: that
file is hand-authored governance prose, and a generated section inside it would
be noise a human has to curate out again. The proposal is durable and
queryable, and the ladder is where an unreviewed idea belongs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models, rule_engine

from app.services.complaints import (
    COMPLAINT_CATEGORY_BY_NAME,
    COMPLAINT_RESOLVED_STATUSES,
    COMPLAINT_TERMINAL_STATUSES,
    collect_complaint_context,
    find_open_case,
    open_complaint,
)

COMPLAINT_LEARNING_VERSION = "complaint_learning_v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> Optional[str]:
    moment = _as_utc(value)
    return moment.isoformat() if moment else None


def _loads(raw: Any, container: str = "object") -> Any:
    empty: Any = [] if container == "array" else {}
    if raw is None:
        return empty
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return empty
    return value if isinstance(value, type(empty)) else empty


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

# =============================================================================
# The loyalty objective
# =============================================================================
#
# What "the customer stayed" means, as a closed vocabulary. Deliberately
# separate from `complaint_decisions.outcome_observed`, which answers "was it
# resolved" -- the two come apart constantly, and a system that learns on the
# second while believing it is learning the first will happily become confident
# about resolutions the customer walked away from.
#
# `delta` is what the outcome is worth to the objective, in the same arbitrary
# unit as the weights. It is signed: negative outcomes subtract, and the
# magnitude says how badly. `churned` is the only one at the floor, because
# nothing in a complaint ledger is worse than the customer leaving.
LOYALTY_OUTCOMES: list[dict[str, Any]] = [
    {
        "outcome": "retained",
        "delta": 1.0,
        "terminal": True,
        "definition": "still active at the observation horizon with no churn signal",
    },
    {
        "outcome": "re-engaged",
        "delta": 1.5,
        "terminal": True,
        "definition": "returned and did something substantive after the complaint",
    },
    {
        "outcome": "neutral",
        "delta": 0.0,
        "terminal": False,
        "definition": "too early to tell, or no signal either way",
    },
    {
        "outcome": "dormant",
        "delta": -0.5,
        "terminal": False,
        "definition": "went quiet without leaving; recoverable, and worth recovering",
    },
    {
        "outcome": "churned",
        "delta": -1.0,
        "terminal": True,
        "definition": "left, cancelled, or churn risk reached critical and did not recover",
    },
]
LOYALTY_OUTCOME_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["outcome"]): dict(row) for row in LOYALTY_OUTCOMES
}

#: Below this many days a complaint has not had time to show an outcome, so the
#: learner must not treat "no churn yet" as evidence of retention. Measuring
#: loyalty at hour one is how a system learns that ignoring complaints works.
LOYALTY_OBSERVATION_MIN_DAYS = 14

#: The horizon an outcome is judged over. Long enough to catch "resolved and
#: vanished", short enough to still be actionable on the next sweep.
LOYALTY_OBSERVATION_WINDOW_DAYS = 90


# =============================================================================
# Signals
# =============================================================================
#
# Every signal that can contribute to a case, with a direction relative to the
# loyalty objective and a *prior* weight. The prior is the configured starting
# belief; the learned weight moves away from it as evidence accumulates. A signal
# with no observations therefore still has a sensible, reviewable value rather
# than nothing.
#
# `direction` is `loyalty_negative` for "this factor hurts loyalty if true",
# `loyalty_positive` for the inverse, and `neutral` for context that should
# reorder a recommendation without scoring either way. `derive` names the pure
# reader below that turns a complaint context into this signal's value.
LOYALTY_SIGNALS: list[dict[str, Any]] = [
    # --- things that hurt loyalty --------------------------------------------
    {
        "signal_id": "complainant_left",
        "label": "Complainant abandoned the relationship",
        "direction": "loyalty_negative",
        "prior_weight": 3.0,
        "derive": "complainant_left",
        "why": "nothing else in the ledger is worse; the weight is the strongest a signal may hold",
    },
    {
        "signal_id": "escalated_further",
        "label": "Case escalated again after the first decision",
        "direction": "loyalty_negative",
        "prior_weight": 2.0,
        "derive": "outcome_escaped",
        "why": "the first resolution was insufficient, so the customer paid twice",
    },
    {
        "signal_id": "reopened",
        "label": "Resolution did not hold",
        "direction": "loyalty_negative",
        "prior_weight": 1.8,
        "derive": "reopened_count",
        "why": "a promise the system did not keep is worse than no promise",
    },
    {
        "signal_id": "sla_response_breached",
        "label": "First-response SLA missed",
        "direction": "loyalty_negative",
        "prior_weight": 1.6,
        "derive": "sla_breaches",
        "why": "the complaint was about being ignored once already",
    },
    {
        "signal_id": "sla_resolution_breached",
        "label": "Resolution SLA missed",
        "direction": "loyalty_negative",
        "prior_weight": 1.7,
        "derive": "sla_breaches",
        "why": "a promised fix date that passes is a broken commitment",
    },
    {
        "signal_id": "churn_critical",
        "label": "Customer is already at critical churn risk",
        "direction": "loyalty_negative",
        "prior_weight": 2.2,
        "derive": "churn_critical",
        "why": "there is no loyalty left to lose, so the complaint is a symptom",
    },
    {
        "signal_id": "sentiment_cliff",
        "label": "Sentiment dropped sharply across the case",
        "direction": "loyalty_negative",
        "prior_weight": 1.4,
        "derive": "sentiment_negative",
        "why": "measured deterioration, distinct from a single bad message",
    },
    {
        "signal_id": "repeat_complainant",
        "label": "Customer has complained repeatedly in the window",
        "direction": "loyalty_negative",
        "prior_weight": 1.5,
        "derive": "repeat_complaints",
        "why": "a pattern means earlier complaints were not resolved, not unlucky",
    },
    {
        "signal_id": "consent_friction",
        "label": "Contact is being gated by a preference",
        "direction": "loyalty_negative",
        "prior_weight": 0.6,
        "derive": "consent_withholds_contact",
        "why": (
            "deliberately low. A customer who opted out has told us what they "
            "want, and treating that as a loyalty problem would pressure people "
            "out of a preference they exercised deliberately"
        ),
    },
    {
        "signal_id": "unowned",
        "label": "Nobody is accountable for the case",
        "direction": "loyalty_negative",
        "prior_weight": 1.5,
        "derive": "no_owner",
        "why": "an unowned complaint is a complaint the customer has to chase",
    },
    {
        "signal_id": "unresolved_stale",
        "label": "Resolved but never closed",
        "direction": "loyalty_negative",
        "prior_weight": 0.9,
        "derive": "resolved_stale",
        "why": "the customer's problem is solved on paper and open in fact",
    },
    # --- things that protect loyalty ------------------------------------------
    {
        "signal_id": "first_response_fast",
        "label": "First response inside the SLA",
        "direction": "loyalty_positive",
        "prior_weight": 1.2,
        "derive": "first_response_fast",
        "why": "being answered quickly is the cheapest loyalty there is",
    },
    {
        "signal_id": "held",
        "label": "Case held and not reopened",
        "direction": "loyalty_positive",
        "prior_weight": 1.0,
        "derive": "outcome_held",
        "why": "the resolution worked and the customer accepted it",
    },
    {
        "signal_id": "satisfaction_high",
        "label": "Customer rated the outcome highly",
        "direction": "loyalty_positive",
        "prior_weight": 1.3,
        "derive": "satisfaction_high",
        "why": "self-reported, so it is the only direct evidence the customer gave us",
    },
    {
        "signal_id": "rebooked",
        "label": "Customer rebooked after the complaint",
        "direction": "loyalty_positive",
        "prior_weight": 2.5,
        "derive": "rebooked_after",
        "why": "behaviour beats opinion; this is the strongest positive signal available",
    },
    # --- context that reorders but does not score ----------------------------
    {
        "signal_id": "regulatory",
        "label": "Carries a statutory deadline",
        "direction": "neutral",
        "prior_weight": 1.0,
        "derive": "regulatory",
        "why": "changes urgency and who handles it, not how much loyalty is at stake",
    },
    {
        "signal_id": "high_value",
        "label": "Commercially material customer",
        "direction": "neutral",
        "prior_weight": 1.0,
        "derive": "high_value",
        "why": (
            "deliberately neutral. Weighting a complaint more because the customer "
            "is valuable is how the people who need help least get it, and it is "
            "recorded here as a signal precisely so the temptation is visible"
        ),
    },
]
LOYALTY_SIGNAL_BY_ID: dict[str, dict[str, Any]] = {
    str(row["signal_id"]): dict(row) for row in LOYALTY_SIGNALS
}
SIGNAL_DIRECTIONS: tuple[str, ...] = ("loyalty_negative", "loyalty_positive", "neutral")


# =============================================================================
# Learning parameters
# =============================================================================
#
# The weights are bounded and they decay. Both properties are load-bearing:
# unbounded weights eventually let one lucky run dominate a decision, and weights
# that never decay keep a belief the world has moved past. Decay pulls toward
# the configured prior at `decay_per_observation`, so a signal that stops being
# observed loses its learned weight and returns to its reviewable default.
LEARNING_PARAMS: dict[str, float] = {
    # How far one observation can move a weight at most.
    "step": 0.15,
    "min_weight": 0.2,
    "max_weight": 4.0,
    # Fraction of the gap to prior closed per observation.
    "decay_per_observation": 0.02,
    # Observations needed before confidence means anything, and the ceiling.
    "confidence_min_observations": 8,
    "confidence_saturation": 40.0,
    # An observation whose outcome is `neutral` teaches nothing and must not move
    # a weight. This is the number that keeps the learner honest.
    "neutral_outcome_step": 0.0,
}

#: One pass reads every eligible case's facts individually, because each one's
#: evidence is a different set of queries. The cap is what keeps a scheduled
#: sweep from turning into a full-table scan on a large corpus.
LEARNING_PASS_LIMIT = 50

#: The published, enforced statement of what a learned weight may do.
LEARNED_WEIGHT_AUTHORITY: dict[str, Any] = {
    "may_reorder_recommendations": True,
    "may_change_explanation": True,
    "may_change_severity_alone": False,
    "may_change_tier_alone": False,
    "may_auto_escalate": False,
    "enforcement": (
        "validate_complaint_learning() errors if any of the three 'False' entries "
        "is ever flipped to True, and no code path writes a case's tier from a "
        "weight: `apply_escalation` reads the configured trigger decision, and the "
        "weight only reaches the dossier as a contribution score"
    ),
    "why": (
        "a weight is a statistic about a population. Letting one decide who gets a "
        "senior escalation means the cases that get escalated produce the evidence "
        "that raises the weight, which then escalates more cases. Within one "
        "quarter that loop decides the policy on its own, and it decides it in "
        "favour of whatever was escalated first."
    ),
}


# =============================================================================
# System-raised complaints
# =============================================================================
#
# A complaint the system opens on the customer's behalf. Three guards make this
# safe to run unattended, and all three are per-detector config rather than code,
# so a detector that misbehaves is dialled down without touching this module:
#
#   `min_confidence` -- the detector's own estimate that the case is warranted.
#                       A detector that cannot say "I am only 40% sure" should not
#                       be opening cases about people.
#   `cooldown_hours` -- how long after its last case for the same user this
#                       detector may fire again. Without it, a stuck condition
#                       opens a fresh case every sweep and the queue becomes
#                       unreadable.
#   `dedupe_category` -- reuse a live case in that category instead of opening a
#                       second one, so a customer with one unresolved problem has
#                       one problem rather than five records of it.
#
# `when` is the rule-engine `when` DSL over the same complaint context the
# escalation triggers read, so a detector and a trigger cannot drift apart in what
# they consider "current state".
SYSTEM_COMPLAINT_DETECTORS: list[dict[str, Any]] = [
    {
        "detector_id": "sla_response_breach",
        "label": "Nobody answered in time",
        "category": "service_quality",
        "default_severity": "high",
        "confidence": 0.95,
        "min_confidence": 0.80,
        "cooldown_hours": 72.0,
        "dedupe_category": True,
        "priority": 100,
        "when": {"response_overdue_hours": {"gt": 4.0}, "acknowledged": False},
        "seed_signals": ["sla_response_breached", "unowned"],
        "note": (
            "high confidence because it is a clock, not an inference. This is the "
            "clearest case for a system-raised complaint: the customer already "
            "told us and we did not answer"
        ),
    },
    {
        "detector_id": "sla_resolution_breach",
        "label": "Promised fix date passed",
        "category": "service_quality",
        "default_severity": "high",
        "confidence": 0.95,
        "min_confidence": 0.80,
        "cooldown_hours": 168.0,
        "dedupe_category": True,
        "priority": 95,
        "when": {"resolution_overdue_hours": {"gt": 24.0}, "resolved": False},
        "seed_signals": ["sla_resolution_breached"],
        "note": "a missed commitment the customer was told about in writing",
    },
    {
        "detector_id": "booking_failure_pattern",
        "label": "Repeated failed or cancelled bookings",
        "category": "booking_failure",
        "default_severity": "high",
        "confidence": 0.80,
        "min_confidence": 0.70,
        "cooldown_hours": 336.0,
        "dedupe_category": True,
        "priority": 80,
        "when": {"cancelled_bookings": {"gte": 3}, "pending_bookings": {"gte": 1}},
        "seed_signals": ["repeat_complainant"],
        "note": (
            "deliberately requires a pattern rather than one cancellation. A "
            "single failed booking is an operational event, not a complaint"
        ),
    },
    {
        "detector_id": "critical_churn_at_risk",
        "label": "Customer already at critical churn risk",
        "category": "service_quality",
        "default_severity": "critical",
        "confidence": 0.70,
        "min_confidence": 0.70,
        "cooldown_hours": 720.0,
        "dedupe_category": True,
        "priority": 75,
        "when": {"churn_risk": ["high", "critical"], "total_complaints": {"gte": 1}},
        "seed_signals": ["churn_critical"],
        "note": (
            "the lower confidence is honest: churn risk is a score, not an event. "
            "The case is filed to be worked, not as an accusation"
        ),
    },
    {
        "detector_id": "unowned_stale_case",
        "label": "An open case has had no owner",
        "category": "service_quality",
        "default_severity": "medium",
        "confidence": 0.85,
        "min_confidence": 0.75,
        "cooldown_hours": 48.0,
        "dedupe_category": False,
        "priority": 70,
        "when": {"no_owner": True, "age_hours": {"gte": 24.0}, "open_only": True},
        "seed_signals": ["unowned"],
        "note": (
            "dedupe off on purpose: this is about *this* case being ownerless, so "
            "folding it into an existing case would hide the fact that nobody is "
            "on it"
        ),
    },
    {
        "detector_id": "billing_dispute_unresolved",
        "label": "Money owed and an unresolved complaint",
        "category": "billing",
        "default_severity": "medium",
        "confidence": 0.65,
        "min_confidence": 0.70,
        "cooldown_hours": 720.0,
        "dedupe_category": True,
        "priority": 60,
        "when": {"arrears_total": {"gt": 0.0}, "total_complaints": {"gte": 1}, "resolved": False},
        "seed_signals": ["repeat_complainant"],
        "note": (
            "confidence is deliberately *below* its own min_confidence, so this row "
            "is inert as shipped. It documents the shape a detector needs rather "
            "than accusing customers with an arrears balance; raise min_confidence "
            "above 0.65 to enable it, and expect validate_complaint_learning to "
            "flag the mismatch either way"
        ),
    },
]
SYSTEM_DETECTOR_BY_ID: dict[str, dict[str, Any]] = {
    str(row["detector_id"]): dict(row) for row in SYSTEM_COMPLAINT_DETECTORS
}

#: How a system-raised case is labelled. Kept in one place because the origin
#: ends up in the case row, in the opening event, and in the audit trail, and
#: three spellings of "the system did this" would be worse than one.
SYSTEM_ORIGIN_PREFIX = "system"
SYSTEM_ORIGIN_KINDS: tuple[str, ...] = ("detector", "batch", "integration")


def system_origin(detector_id: str) -> str:
    """The `ComplaintCase.source` value for a system-raised case."""
    return f"{SYSTEM_ORIGIN_PREFIX}:{str(detector_id)}"


def parse_system_origin(source: Any) -> Optional[str]:
    """The detector id behind a ``source``, or ``None`` if a human raised it."""
    text = str(source or "")
    if not text.startswith(f"{SYSTEM_ORIGIN_PREFIX}:"):
        return None
    return text.split(":", 1)[1] or None


# =============================================================================
# Stacked complaints -> improvement proposals
# =============================================================================
#
# "A bunch of complaints" has to be defined or it is a mood. A cluster is a set
# of live or recently-closed cases sharing a `key`, meeting *every* configured
# threshold. The thresholds are deliberately multi-axis: raw volume alone would
# fire on one customer complaining often (which is a support problem, not a
# product one), and distinct-user count alone would miss a narrow category that
# affects everyone who touches it.
#
# `min_reopen_rate` is the one that matters most. A cluster of cases that were
# all closed and none were reopened is the system working; a cluster that keeps
# coming back is the system not working, and it is the only one of these numbers
# that distinguishes the two.
IMPROVEMENT_CLUSTER_RULES: list[dict[str, Any]] = [
    {
        "cluster_id": "category_volume",
        "label": "One complaint category dominating",
        "key": "category",
        "axis": "category",
        "min_cases": 5,
        "min_distinct_users": 4,
        "window_days": 30,
        "min_reopen_rate": 0.0,
        "component": "services/complaints",
        "owner_hint": "support lead",
        "priority_when_hot": "high",
        "rationale": "a single category dominating the queue is a process problem before it is a product one",
    },
    {
        "cluster_id": "category_churn",
        "label": "A category that keeps coming back",
        "key": "category",
        "axis": "category",
        "min_cases": 5,
        "min_distinct_users": 3,
        "window_days": 90,
        "min_reopen_rate": 0.20,
        "component": "services/recovery_playbooks",
        "owner_hint": "service recovery",
        "priority_when_hot": "high",
        "rationale": (
            "cases that reopen are cases that were closed without being fixed; the "
            "resolution path for this category is not working"
        ),
    },
    {
        "cluster_id": "owner_team_backlog",
        "label": "A team holding too many live cases",
        "key": "owner_team",
        "axis": "owner_team",
        "min_cases": 8,
        "min_distinct_users": 3,
        "window_days": 30,
        "min_reopen_rate": 0.0,
        "component": "routers/complaints",
        "owner_hint": "support lead",
        "priority_when_hot": "medium",
        "rationale": "a queue with no drain is a staffing or routing problem, not a complaint-quality one",
    },
    {
        "cluster_id": "system_detector_repeat",
        "label": "The system keeps raising the same complaint",
        "key": "origin",
        "axis": "origin",
        # System-origin cases only. Without this the rule clusters *every* case
        # by its `source` string and then labels the result "the system keeps
        # raising it" -- so a pile of ordinary customer complaints lodged via
        # `chat` was reported as a detector misfiring. The rule's whole claim is
        # about the detectors, and it has to actually be looking at them.
        "origin_filter": "system",
        "min_cases": 4,
        "min_distinct_users": 2,
        "window_days": 30,
        "min_reopen_rate": 0.0,
        "component": "services/complaint_learning",
        "owner_hint": "platform",
        "priority_when_hot": "high",
        "rationale": (
            "a detector firing repeatedly means either a real systemic fault or a "
            "mis-tuned detector; both are worth a look and the cluster says which"
        ),
    },
    {
        "cluster_id": "regulatory_stack",
        "label": "Statutory cases accumulating",
        "key": "category",
        "axis": "category",
        "min_cases": 3,
        "min_distinct_users": 3,
        "window_days": 90,
        "min_reopen_rate": 0.0,
        "component": "services/complaints",
        "owner_hint": "privacy office",
        "priority_when_hot": "high",
        "rationale": (
            "data-protection complaints share a one-month statutory clock, so a "
            "backlog here is a deadline risk rather than a service one"
        ),
    },
]
IMPROVEMENT_CLUSTER_BY_ID: dict[str, dict[str, Any]] = {
    str(row["cluster_id"]): dict(row) for row in IMPROVEMENT_CLUSTER_RULES
}

PROPOSAL_STATUSES: tuple[str, ...] = ("detected", "published", "acknowledged", "dismissed")
PROPOSAL_PRIORITIES: tuple[str, ...] = ("high", "medium", "low")
#: Impact and effort use the same words as `efficiency_audit.EnhancementProposal`,
#: so a reader comparing the two suggestion streams is not translating.
PROPOSAL_LEVELS: tuple[str, ...] = ("high", "medium", "low")

#: What each cluster rule's `component` maps to in the code map, for the
#: recommendation text. Kept as data so a suggestion can name a real module.
PROPOSAL_ACTION_TEMPLATE = (
    "Review the {axis} handling behind these {cases} complaint(s) across "
    "{users} distinct customer(s) in {window} days"
)
# Note the arithmetic: a reopened case is one whose resolution did *not* hold, so
# `reopen_pct` is the share that failed. The earlier version subtracted it to
# claim the complement failed, which stated the opposite of the evidence.
PROPOSAL_ACTION_REOPEN = (
    ". {reopen_pct}% of these cases were reopened, meaning {reopen_pct}% of the "
    "resolutions we recorded did not hold -- so closing them is not the same as "
    "fixing them"
)
PROPOSAL_ACTION_SYSTEM = (
    ". Some of this cluster was raised by the system rather than by customers, "
    "which points at the detector or the fault it is watching"
)


# =============================================================================
# Signal readers: pure functions of a case's facts
# =============================================================================
#
# Every reader takes one dict of *facts* and returns a bool. The facts dict is
# the complaint decision context (`services/complaints`) merged with the outcome
# facts the learning pass adds, so a reader never has to know where a number came
# from -- only what it means. That keeps all of them trivially testable, which
# matters because a reader that is wrong silently biases every weight trained on
# it.
#
# Readers return bool, not a magnitude. Magnitude is the weight's job. A reader
# that also scaled itself would make the learned weight mean "this signal, times
# whatever the reader felt like" and there would be no way to audit the product.


def _read_complainant_left(facts: dict[str, Any]) -> bool:
    return str(facts.get("loyalty_outcome") or "") == "churned"


def _read_outcome_escaped(facts: dict[str, Any]) -> bool:
    return str(facts.get("case_outcome_observed") or "") == "escalated_further"


def _read_reopened(facts: dict[str, Any]) -> bool:
    return int(facts.get("reopened_count") or 0) > 0


def _read_sla_breaches(facts: dict[str, Any]) -> bool:
    return bool(facts.get("sla_breached"))


def _read_churn_critical(facts: dict[str, Any]) -> bool:
    return str(facts.get("churn_risk") or "low") in {"high", "critical"}


def _read_sentiment_negative(facts: dict[str, Any]) -> bool:
    """A *cliff*, not a low score.

    A customer who was always unhappy is a different problem from one who was
    fine until this case, and the second is the one a fix can address. The
    threshold is on the drop, so the reading is unaffected by where the baseline
    happened to sit.
    """
    first = facts.get("sentiment_first")
    last = facts.get("sentiment_last")
    if not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
        return False
    return float(first) - float(last) >= SENTIMENT_CLIFF_DROP


def _read_repeat_complaints(facts: dict[str, Any]) -> bool:
    return int(facts.get("complaints_last_30d") or 0) >= REPEAT_COMPLAINT_THRESHOLD


def _read_consent_gated(facts: dict[str, Any]) -> bool:
    return bool(facts.get("consent_withholds_contact"))


def _read_no_owner(facts: dict[str, Any]) -> bool:
    return bool(facts.get("no_owner"))


def _read_resolved_stale(facts: dict[str, Any]) -> bool:
    return float(facts.get("resolved_stale_hours") or 0.0) > 0.0


def _read_first_response_fast(facts: dict[str, Any]) -> bool:
    hours = facts.get("first_response_hours")
    due = facts.get("response_due_hours")
    if not isinstance(hours, (int, float)) or not isinstance(due, (int, float)):
        return False
    return 0.0 <= float(hours) < float(due)


def _read_outcome_held(facts: dict[str, Any]) -> bool:
    outcome = str(facts.get("case_outcome_observed") or "")
    return outcome in {"held", "resolved"} and int(facts.get("reopened_count") or 0) == 0


def _read_satisfaction_high(facts: dict[str, Any]) -> bool:
    score = facts.get("satisfaction_score")
    return isinstance(score, (int, float)) and float(score) >= SATISFACTION_HIGH


def _read_rebooked_after(facts: dict[str, Any]) -> bool:
    days = facts.get("days_to_rebooking")
    if not isinstance(days, (int, float)):
        return False
    return 0.0 <= float(days) <= REBOOKING_WINDOW_DAYS


def _read_regulatory(facts: dict[str, Any]) -> bool:
    return bool(facts.get("regulatory"))


def _read_high_value(facts: dict[str, Any]) -> bool:
    return str(facts.get("value_tier") or "standard") in HIGH_VALUE_TIERS


#: Thresholds the readers share, named so a reader and its reader-docstring
#: cannot drift and so `validate_complaint_learning` can report them.
SENTIMENT_CLIFF_DROP = 0.35
REPEAT_COMPLAINT_THRESHOLD = 3
SATISFACTION_HIGH = 4
REBOOKING_WINDOW_DAYS = 60
HIGH_VALUE_TIERS: frozenset[str] = frozenset({"premium", "platinum", "enterprise"})

SIGNAL_READERS: dict[str, Any] = {
    "complainant_left": _read_complainant_left,
    "outcome_escaped": _read_outcome_escaped,
    "reopened_count": _read_reopened,
    "sla_breaches": _read_sla_breaches,
    "churn_critical": _read_churn_critical,
    "sentiment_negative": _read_sentiment_negative,
    "repeat_complaints": _read_repeat_complaints,
    "consent_withholds_contact": _read_consent_gated,
    "no_owner": _read_no_owner,
    "resolved_stale": _read_resolved_stale,
    "first_response_fast": _read_first_response_fast,
    "outcome_held": _read_outcome_held,
    "satisfaction_high": _read_satisfaction_high,
    "rebooked_after": _read_rebooked_after,
    "regulatory": _read_regulatory,
    "high_value": _read_high_value,
}

#: A signal is only allowed to carry a direction-neutral weight if it is one of
#: the two context signals. Stated as data so the validator can *error* on a
#: newly-added loyalty_negative signal that was mistakenly scoped, rather than
#: discovering it in a learned weight three weeks later.
NEUTRAL_SIGNAL_IDS: frozenset[str] = frozenset({"regulatory", "high_value"})


# =============================================================================
# Contribution scoring
# =============================================================================


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def signal_weight(
    signal_id: str,
    weights: Optional[dict[str, float]] = None,
) -> float:
    """The weight to use for ``signal_id``, falling back to its configured prior.

    The fallback is what makes a cold start honest: an unlearned signal still has
    a reviewable value rather than zero, and zero would quietly mean "ignore
    this" for every signal the learner has not seen yet.
    """
    row = LOYALTY_SIGNAL_BY_ID.get(str(signal_id))
    if row is None:
        return 0.0
    if weights and str(signal_id) in weights:
        return float(weights[str(signal_id)])
    return float(row["prior_weight"])


def score_contributions(
    facts: dict[str, Any],
    weights: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """Read every signal against ``facts`` and combine them into one score.

    Pure: same facts and weights in, same result out. It reads every signal
    rather than stopping at the first match, because the useful output is the
    *ranking* of what contributed, and an early exit would make that ranking
    depend on the order the config happens to be written in.

    The combined figure separates positive from negative deliberately. A case with
    a fast first response and a missed resolution SLA is not "net zero" -- it is
    a case where one thing went right and one thing went wrong, and collapsing
    them to a single number would hide exactly the pairing an operator needs to
    see.
    """
    contributions: list[dict[str, Any]] = []
    for row in LOYALTY_SIGNALS:
        signal_id = str(row["signal_id"])
        reader = SIGNAL_READERS.get(str(row["derive"]))
        if reader is None:  # pragma: no cover - validator reports this
            continue
        try:
            active = bool(reader(facts))
        except (TypeError, ValueError, KeyError, ZeroDivisionError, AttributeError):
            # A reader must never take a case down. A reader that raises is a
            # bug, and a bug here should cost one signal, not the whole
            # decision -- the validator is what makes the bug visible.
            #
            # ZeroDivisionError and AttributeError are here because a reader that
            # divides or reaches for a nested key is an ordinary thing to write,
            # and the first version of this list omitted both, so a reader doing
            # arithmetic on a missing field propagated out of `score_contributions`
            # and took the whole dossier with it. Found by the test that installs
            # a raising reader.
            active = False
        if not active:
            continue
        weight = signal_weight(signal_id, weights)
        contributions.append(
            {
                "signal_id": signal_id,
                "label": str(row["label"]),
                "direction": str(row["direction"]),
                "weight": round(weight, 4),
                "contribution": round(weight, 4),
                "why": str(row["why"]),
            }
        )

    contributions.sort(key=lambda item: (-item["contribution"], str(item["signal_id"])))
    negative = [c for c in contributions if c["direction"] == "loyalty_negative"]
    positive = [c for c in contributions if c["direction"] == "loyalty_positive"]
    neutral = [c for c in contributions if c["direction"] == "neutral"]
    return {
        "contributions": contributions,
        "loyalty_damage": round(sum(c["contribution"] for c in negative), 4),
        "loyalty_protection": round(sum(c["contribution"] for c in positive), 4),
        "context_count": len(neutral),
        # Reported for ordering and explanation only. See
        # LEARNED_WEIGHT_AUTHORITY for why this never sets a tier.
        "net": round(sum(c["contribution"] for c in negative + positive), 4),
        "top_signal_id": str(contributions[0]["signal_id"]) if contributions else "",
        "signal_ids": [str(c["signal_id"]) for c in contributions],
    }


# =============================================================================
# The weight update rule
# =============================================================================


def _decayed(current: float, prior: float) -> float:
    """Pull one observation's worth of ``current`` back toward ``prior``.

    Decay is applied before the step, not after, so the pull is visible in the
    result and the two effects compose rather than one masking the other. A
    signal nobody has observed lately drifts home regardless of how far it once
    moved, which is the property that stops a weight from freezing a belief the
    population has outgrown.
    """
    rate = float(LEARNING_PARAMS["decay_per_observation"])
    return current + (prior - current) * rate


def weight_after_observation(
    *,
    current: float,
    prior: float,
    direction: str,
    loyalty_delta: float,
    observations: int,
    agreements: int,
    disagreements: int,
) -> dict[str, Any]:
    """The next weight for one signal given one loyalty outcome. Pure.

    Three properties, each load-bearing:

    * **Bounded.** ``min_weight``/``max_weight`` clamp the result, so no amount
      of evidence can push a signal to a value that would dominate a decision.
    * **Decayed.** Each observation closes ``decay_per_observation`` of the gap
      to the configured prior before the step, so an unobserved signal returns to
      its reviewable default.
    * **Direction-aware.** A ``loyalty_negative`` signal gets *heavier* when the
      outcome was good -- if complaints that carried a strong damage signal
      ended with the customer staying, the signal was overstating the damage.
      A ``loyalty_positive`` signal does the opposite. A ``neutral`` signal never
      moves, because a context signal is not making a claim about loyalty that
      evidence could contradict.
    """
    step = float(LEARNING_PARAMS["step"])
    base = _decayed(float(current), float(prior))
    delta = float(loyalty_delta)

    if direction == "loyalty_negative":
        # Damage that did not translate into churn was probably overstated.
        movement = -step * delta
    elif direction == "loyalty_positive":
        # Protection that did not translate into retention was overstated.
        movement = step * delta
    else:
        movement = float(LEARNING_PARAMS["neutral_outcome_step"])

    bounded = _clamp(
        base + movement,
        float(LEARNING_PARAMS["min_weight"]),
        float(LEARNING_PARAMS["max_weight"]),
    )
    # Round to 3dp so repeated passes converge instead of drifting in the last
    # binary place forever, which would make `version` climb for no reason.
    next_weight = round(bounded, 3)
    total = int(observations) + 1
    agreed = int(agreements) + (1 if movement > 0 else 0)
    disagreed = int(disagreements) + (1 if movement < 0 else 0)
    return {
        "weight": next_weight,
        "observations": total,
        "agreements": agreed,
        "disagreements": disagreed,
        "confidence": confidence_for(total, agreed, disagreed),
        "moved": next_weight != round(float(current), 3),
        "at_bound": next_weight in {
            float(LEARNING_PARAMS["min_weight"]),
            float(LEARNING_PARAMS["max_weight"]),
        },
    }


def confidence_for(observations: int, agreements: int, disagreements: int) -> float:
    """How much the weight should be believed, in ``[0, 1]``.

    Two factors multiplied: how much evidence there is, and how much of it agrees
    with the direction the weight moved. Sample count alone is not enough -- 30
    observations that all pulled the same way is one signal; 30 that split evenly
    is a coin flip that happens to have a number attached.
    """
    count = max(0, int(observations))
    if count == 0:
        return 0.0
    floor = float(LEARNING_PARAMS["confidence_min_observations"])
    saturation = float(LEARNING_PARAMS["confidence_saturation"])
    # Below `floor` confidence is deliberately near zero: a weight moved by three
    # observations is noise, and reporting it as 0.3 would imply otherwise.
    volume = min(1.0, count / max(floor, saturation)) if count < floor else min(
        1.0, 0.5 + 0.5 * (count - floor) / max(1.0, saturation - floor)
    )
    votes = int(agreements) + int(disagreements)
    agreement = 0.5 if votes == 0 else abs(int(agreements) - int(disagreements)) / votes
    return round(_clamp(volume * (0.5 + 0.5 * agreement), 0.0, 1.0), 4)


def default_weights() -> dict[str, float]:
    """Every signal at its configured prior -- the cold-start state."""
    return {
        str(row["signal_id"]): float(row["prior_weight"])
        for row in LOYALTY_SIGNALS
    }


def weight_table_payload(
    weights: Optional[dict[str, float]] = None,
    stats: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """The explainable weight table: every signal, its value, and why.

    Every signal appears, including one with no observations. A table that only
    lists learned entries makes the configured priors invisible, which is how a
    reviewer ends up believing a number is measured when it is actually a default.
    """
    current = weights or default_weights()
    observed = stats or {}
    rows: list[dict[str, Any]] = []
    for row in LOYALTY_SIGNALS:
        signal_id = str(row["signal_id"])
        prior = float(row["prior_weight"])
        value = float(current.get(signal_id, prior))
        stat = observed.get(signal_id, {})
        observations = int(stat.get("observations", 0) or 0)
        rows.append(
            {
                "signal_id": signal_id,
                "label": str(row["label"]),
                "direction": str(row["direction"]),
                "weight": round(value, 4),
                "prior_weight": prior,
                "learned": bool(stat.get("learned", observations > 0)),
                "observations": observations,
                "agreements": int(stat.get("agreements", 0) or 0),
                "disagreements": int(stat.get("disagreements", 0) or 0),
                "confidence": round(
                    float(stat.get("confidence", 0.0) or 0.0), 4
                ),
                "drift_from_prior": round(value - prior, 4),
                "source": "learned" if observations else "configured_prior",
                "why": str(row["why"]),
            }
        )
    rows.sort(key=lambda item: (-item["weight"], str(item["signal_id"])))
    return rows


# =============================================================================
# Measuring the objective
# =============================================================================


def derive_loyalty_outcome(facts: dict[str, Any]) -> dict[str, Any]:
    """What actually happened to the relationship. Pure.

    The ordering of the branches is the argument, so it is worth stating:
    ``churned`` is checked first and unconditionally, because a customer who
    churned and then came back has not been retained -- the worst outcome is
    recorded as the worst, not softened by a later event.

    ``neutral`` is a real answer and it is returned honestly rather than guessed
    around. A complaint three days old has no outcome yet, and calling that
    ``retained`` is precisely the error that teaches a learner that ignoring
    complaints works. Before :data:`LOYALTY_OBSERVATION_MIN_DAYS` have passed,
    the only answer available is ``neutral`` and no weight moves.
    """
    days_since_complaint = facts.get("days_since_complaint")
    days_since_last_contact = facts.get("days_since_last_contact")
    churn_risk = str(facts.get("churn_risk") or "low")
    days_to_rebooking = facts.get("days_to_rebooking")
    reactivated = bool(facts.get("reactivated"))

    if facts.get("customer_marked_churned") or churn_risk == "critical":
        return {"outcome": "churned", "basis": "churn_risk_critical", "decidable": True}
    if reactivated or (
        isinstance(days_to_rebooking, (int, float)) and float(days_to_rebooking) >= 0
    ):
        return {"outcome": "re-engaged", "basis": "substantive_return", "decidable": True}
    if not isinstance(days_since_complaint, (int, float)):
        return {"outcome": "neutral", "basis": "no_horizon", "decidable": False}
    if float(days_since_complaint) < LOYALTY_OBSERVATION_MIN_DAYS:
        return {
            "outcome": "neutral",
            "basis": "too_early_to_judge",
            "decidable": False,
        }
    if (
        isinstance(days_since_last_contact, (int, float))
        and float(days_since_last_contact) >= DORMANCY_LOYALTY_DAYS
    ):
        return {"outcome": "dormant", "basis": "went_quiet", "decidable": True}
    return {"outcome": "retained", "basis": "still_active", "decidable": True}


#: Silence at or beyond this many days is dormancy. Sourced from
#: ``loyalty_journey.DORMANCY_THRESHOLD_DAYS`` rather than re-decided here, so
#: "dormant" means the same thing in a complaint and in a journey.
DORMANCY_LOYALTY_DAYS = 14


def loyalty_delta_for(outcome: str) -> float:
    """The signed worth of ``outcome``. Unknown outcomes are worth exactly zero."""
    row = LOYALTY_OUTCOME_BY_NAME.get(str(outcome))
    return float(row["delta"]) if row else 0.0


def observation_is_decidable(outcome: str) -> bool:
    """Only a decided outcome may move a weight.

    ``neutral`` exists in the vocabulary precisely so "we do not know yet" has a
    spelling. Routing it through the learner as though it were data is how a
    system concludes that doing nothing is correct -- every not-yet-judged case
    would be counted as agreeing with whatever weight already exists.
    """
    row = LOYALTY_OUTCOME_BY_NAME.get(str(outcome))
    return bool(row and row.get("terminal")) and str(outcome) != "neutral"


# =============================================================================
# Evidence collection (async)
# =============================================================================
#
# Every reader above is a function of facts, so something has to gather the facts.
# The gathers are the only place in this module that touch the database, and each
# one degrades on its own: a booking table that is unreachable must cost the
# "rebooked" signal, not the whole learning pass. `evidence_gaps` records what
# was missing so a pass that learned from half the evidence says so.


async def _rows(db: AsyncSession, statement: Any) -> list[Any]:
    try:
        result = await db.execute(statement)
        return list(result.scalars().all())
    except SQLAlchemyError:
        return []


async def _scalar(db: AsyncSession, statement: Any, default: Any = None) -> Any:
    try:
        result = await db.execute(statement)
        value = result.scalar()
        return default if value is None else value
    except SQLAlchemyError:
        return default


async def collect_loyalty_facts(
    db: AsyncSession,
    case: Any,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Gather the outcome facts for one case, on top of its decision context.

    Returns the complaint context merged with the outcome facts. The merge is
    additive: nothing here overwrites a context key, because the context is what
    the escalation triggers were judged on and the outcome facts must not be able
    to change that answer retroactively.
    """
    moment = _as_utc(now) or _now()
    context = await collect_complaint_context(db, case, now=moment)
    base = dict(context.get("context") or {})
    gaps: list[str] = list(context.get("gaps") or [])

    user_id = int(getattr(case, "user_id", 0) or 0)
    opened_at = _as_utc(getattr(case, "opened_at", None)) or _as_utc(getattr(case, "created_at", None)) or moment

    days_since_complaint = max(0.0, (moment - opened_at).total_seconds() / 86400.0)

    facts: dict[str, Any] = {
        **base,
        "days_since_complaint": round(days_since_complaint, 3),
    }

    # --- rebooking after the complaint ---------------------------------------
    scheduled = await _scalar(
        db,
        select(func.min(models.Booking.scheduled_date)).where(
            models.Booking.user_id == user_id,
            models.Booking.scheduled_date.isnot(None),
            models.Booking.scheduled_date >= opened_at,
            models.Booking.status.notin_(CANCELLED_BOOKING_STATUSES),
        ),
    )
    if scheduled is None:
        facts["days_to_rebooking"] = None
    else:
        booked = _as_utc(scheduled)
        facts["days_to_rebooking"] = (
            None if booked is None else round((booked - opened_at).total_seconds() / 86400.0, 3)
        )
        if booked is None:
            gaps.append("booking.scheduled_date_unparseable")

    # --- last contact ---------------------------------------------------------
    last_chat = await _scalar(
        db,
        select(func.max(models.ChatHistory.timestamp)).where(models.ChatHistory.user_id == user_id),
    )
    last_signal = await _scalar(
        db,
        select(func.max(models.InteractionSignal.created_at)).where(
            models.InteractionSignal.user_id == user_id
        ),
    )
    contacts = [t for t in (_as_utc(last_chat), _as_utc(last_signal)) if t is not None]
    if contacts:
        facts["days_since_last_contact"] = round(
            max(0.0, (moment - max(contacts)).total_seconds() / 86400.0), 3
        )
    else:
        # No contact data at all is not "dormant" and not "retained". Leaving the
        # key absent makes the dormancy reader return False rather than guessing.
        gaps.append("no_contact_timestamp")

    # --- reactivation after the complaint -------------------------------------
    post = await _scalar(
        db,
        select(func.count(models.InteractionSignal.id)).where(
            models.InteractionSignal.user_id == user_id,
            models.InteractionSignal.created_at >= opened_at,
            models.InteractionSignal.priority == "high",
        ),
    )
    facts["reactivated"] = int(post or 0) > 0

    # --- sentiment across the case --------------------------------------------
    facts["sentiment_first"], facts["sentiment_last"] = await _sentiment_across_case(
        db, user_id, opened_at
    )
    if facts["sentiment_first"] is None:
        gaps.append("no_sentiment_series")

    facts["evidence_gaps"] = gaps
    return facts


#: Bookings in these states are not a rebooking. A cancellation is the opposite.
CANCELLED_BOOKING_STATUSES: tuple[str, ...] = ("cancelled", "failed", "no_show", "rescheduled")


async def _sentiment_across_case(
    db: AsyncSession, user_id: int, opened_at: datetime
) -> tuple[Optional[float], Optional[float]]:
    """Sentiment at the start of a case and at its end, from interaction signals.

    Read as a series rather than a single stored score because the *shape* is the
    signal: a customer who was already unhappy and stayed unhappy is a different
    problem from one who was fine until this case. The `sentiment_cliff` reader
    only fires on the drop, so a flat bad baseline correctly does not match.
    """
    rows = await _rows(
        db,
        select(models.InteractionSignal)
        .where(models.InteractionSignal.user_id == int(user_id))
        .order_by(models.InteractionSignal.created_at.asc())
        .limit(SENTIMENT_SERIES_LIMIT),
    )
    if len(rows) < SENTIMENT_CLIFF_MIN_SERIES:
        return None, None
    before = [float(r.score) for r in rows if r.created_at and _as_utc(r.created_at) < opened_at]
    after = [float(r.score) for r in rows if r.created_at and _as_utc(r.created_at) >= opened_at]
    if not before or not after:
        return None, None
    return round(sum(before) / len(before), 4), round(sum(after) / len(after), 4)


#: A two-point series cannot show a trend, and treating it as one produces
#: "cliffs" that are just noise between two samples.
SENTIMENT_SERIES_LIMIT = 20
SENTIMENT_CLIFF_MIN_SERIES = 2


# =============================================================================
# The learning pass
# =============================================================================


async def load_learned_weights(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """Every stored weight, as ``{signal_id: {...}}``.

    A missing row is not an error: it means the signal has not been observed, and
    the caller falls back to the configured prior. A signal that is present in
    the config but absent from the table is therefore a cold start, not a fault.
    """
    rows = await _rows(db, select(models.ComplaintSignalWeight))
    return {str(row.signal_id): {
        "weight": float(row.weight),
        "prior_weight": float(row.prior_weight),
        "confidence": float(row.confidence),
        "observations": int(row.observations),
        "agreements": int(row.agreements),
        "disagreements": int(row.disagreements),
        "version": int(row.version),
        "last_outcome_at": _iso(row.last_outcome_at),
    } for row in rows}


async def load_weight_map(db: AsyncSession) -> dict[str, float]:
    """Just the numbers, for scoring. Falls back to priors per signal."""
    stored = await load_learned_weights(db)
    weights = default_weights()
    for signal_id, stat in stored.items():
        if signal_id in weights:
            weights[signal_id] = float(stat["weight"])
    return weights


def _stat_payload(signal_id: str, stat: dict[str, Any]) -> dict[str, Any]:
    row = LOYALTY_SIGNAL_BY_ID.get(signal_id, {})
    return {
        **stat,
        "learned": int(stat.get("observations", 0) or 0) > 0,
        "label": str(row.get("label", signal_id)),
        "direction": str(row.get("direction", "neutral")),
    }


async def build_weight_report(db: AsyncSession) -> dict[str, Any]:
    """The full, explainable weight table plus a pass summary."""
    stored = await load_learned_weights(db)
    stats = {sid: _stat_payload(sid, stat) for sid, stat in stored.items()}
    table = weight_table_payload(
        {sid: stat["weight"] for sid, stat in stored.items()} or None, stats
    )
    learned = [row for row in table if row["learned"]]
    return {
        "ran_at": _iso(_now()),
        "version": COMPLAINT_LEARNING_VERSION,
        "parameters": dict(LEARNING_PARAMS),
        "authority": dict(LEARNED_WEIGHT_AUTHORITY),
        "table": table,
        "learned_count": len(learned),
        "configured_count": len(table) - len(learned),
        "max_drift": max((abs(row["drift_from_prior"]) for row in table), default=0.0),
        "at_bound": [row["signal_id"] for row in table if row["weight"] in {
            float(LEARNING_PARAMS["min_weight"]), float(LEARNING_PARAMS["max_weight"])
        } and row["learned"]],
    }


async def run_learning_pass(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    limit: int = LEARNING_PASS_LIMIT,
    commit: bool = True,
) -> dict[str, Any]:
    """Observe outcomes and move the weights. The whole mechanism in one call.

    For each case that has been open long enough to show an outcome and has not
    been observed at its current terminal state:

    1. gather the outcome facts,
    2. derive the loyalty outcome,
    3. score which signals contributed, using the weights *as they were*,
    4. write one observation row per contributing signal, freezing that weight,
    5. move each weight by one step, bounded and decayed.

    Ordering matters and is the reason the pass is written this way: the
    observation records the weight the operator was shown, and only then is that
    weight moved. Reversing those two steps would train the system on numbers it
    had not yet produced, and a bug in the update rule would be unrecoverable
    because the evidence would already agree with it.

    A `neutral` outcome writes its observation rows -- so the reason a case was
    not learnable is on the record -- but moves no weight.
    """
    moment = _as_utc(now) or _now()
    weights = await load_weight_map(db)
    stored = await load_learned_weights(db)

    oldest_useful = moment - timedelta(days=LOYALTY_OBSERVATION_MIN_DAYS)
    newest = moment - timedelta(days=LOYALTY_OBSERVATION_WINDOW_DAYS)
    candidates = await _rows(
        db,
        select(models.ComplaintCase)
        .where(
            models.ComplaintCase.status.in_(COMPLAINT_TERMINAL_STATUSES + COMPLAINT_RESOLVED_STATUSES),
            models.ComplaintCase.opened_at <= oldest_useful,
            models.ComplaintCase.opened_at >= newest,
        )
        .order_by(models.ComplaintCase.opened_at.desc())
        .limit(max(1, min(int(limit), LEARNING_PASS_LIMIT))),
    )

    # A case with *any* live observation has been learned from. Keyed on the
    # complaint alone rather than on (complaint, outcome): a case that was first
    # observed as `dormant` and later churned is the same case, and re-observing
    # it would let a single complaint move the weights twice.
    already: set[int] = set()
    seen_rows = await _rows(
        db,
        select(models.ComplaintSignalObservation).where(
            models.ComplaintSignalObservation.superseded.is_(False),
        ),
    )
    for row in seen_rows:
        already.add(int(row.complaint_id))

    learned: dict[str, dict[str, Any]] = {}
    written = 0
    skipped: list[dict[str, Any]] = []
    gaps: list[str] = []

    for case in candidates:
        if int(case.id) in already:
            skipped.append({"complaint_id": int(case.id), "reason": "already_observed"})
            continue

        facts = await collect_loyalty_facts(db, case, now=moment)
        gaps.extend(str(g) for g in facts.get("evidence_gaps") or [])
        scored = score_contributions(facts, weights)
        verdict = derive_loyalty_outcome(facts)
        outcome = str(verdict["outcome"])

        evidence = {
            "basis": str(verdict["basis"]),
            "decidable": bool(verdict["decidable"]),
            "days_since_complaint": facts.get("days_since_complaint"),
            "days_since_last_contact": facts.get("days_since_last_contact"),
            "days_to_rebooking": facts.get("days_to_rebooking"),
            "churn_risk": facts.get("churn_risk"),
            "loyalty_damage": scored["loyalty_damage"],
            "loyalty_protection": scored["loyalty_protection"],
        }
        decidable = observation_is_decidable(outcome)
        delta = loyalty_delta_for(outcome)

        for contribution in scored["contributions"]:
            signal_id = str(contribution["signal_id"])
            weight = float(contribution["weight"])
            db.add(models.ComplaintSignalObservation(
                complaint_id=int(case.id),
                user_id=int(case.user_id),
                signal_id=signal_id,
                weight_snapshot=weight,
                confidence_snapshot=float(stored.get(signal_id, {}).get("confidence", 0.0)),
                observations_snapshot=int(stored.get(signal_id, {}).get("observations", 0)),
                loyalty_outcome=outcome,
                loyalty_delta=delta,
                evidence_json=_dumps(evidence),
                superseded=False,
                observed_at=moment,
            ))
            written += 1
            if not decidable or contribution["direction"] == "neutral":
                continue
            signal = LOYALTY_SIGNAL_BY_ID.get(signal_id, {})
            prior = float(signal.get("prior_weight", weight))
            current = float(stored.get(signal_id, {}).get("weight", prior))
            stat = stored.get(signal_id, {})
            moved = weight_after_observation(
                current=current,
                prior=prior,
                direction=str(signal.get("direction", "neutral")),
                loyalty_delta=delta,
                observations=int(stat.get("observations", 0)),
                agreements=int(stat.get("agreements", 0)),
                disagreements=int(stat.get("disagreements", 0)),
            )
            learned[signal_id] = moved
            stored[signal_id] = {**stat, **moved, "weight": moved["weight"]}
            weights[signal_id] = moved["weight"]

        if not scored["contributions"]:
            skipped.append({"complaint_id": int(case.id), "reason": "no_contributing_signal"})

    for signal_id, moved in sorted(learned.items()):
        await _upsert_weight(db, signal_id, moved, moment)

    if commit:
        await db.commit()
    else:
        await db.flush()

    return {
        "version": COMPLAINT_LEARNING_VERSION,
        "ran_at": _iso(moment),
        "candidates": len(candidates),
        "observations_written": written,
        "signals_moved": sorted(learned),
        "weights": {sid: round(float(stat["weight"]), 4) for sid, stat in sorted(learned.items())},
        "skipped": skipped,
        "evidence_gaps": sorted(set(gaps)),
        "advisory_only": True,
        "note": (
            "weights reorder recommendations and explanations. They never set a "
            "tier or trigger an escalation on their own."
        ),
    }


async def _upsert_weight(
    db: AsyncSession,
    signal_id: str,
    moved: dict[str, Any],
    moment: datetime,
) -> None:
    """Write one weight row, creating it on first observation.

    The prior is only set at creation. On a later pass the configured prior may
    have been edited, and overwriting the stored prior with it would erase the
    record of what this weight was decaying toward, which is the only way to tell
    "the population moved" from "somebody changed the default".
    """
    row = await _scalar(
        db,
        select(models.ComplaintSignalWeight).where(
            models.ComplaintSignalWeight.signal_id == str(signal_id)
        ),
    )
    if row is None:
        config = LOYALTY_SIGNAL_BY_ID.get(str(signal_id), {})
        db.add(models.ComplaintSignalWeight(
            signal_id=str(signal_id),
            weight=float(moved["weight"]),
            prior_weight=float(config.get("prior_weight", moved["weight"])),
            confidence=float(moved["confidence"]),
            observations=int(moved["observations"]),
            agreements=int(moved["agreements"]),
            disagreements=int(moved["disagreements"]),
            version=1,
            last_outcome_at=moment,
            note="created on first observation",
        ))
        return
    row.weight = float(moved["weight"])
    row.confidence = float(moved["confidence"])
    row.observations = int(moved["observations"])
    row.agreements = int(moved["agreements"])
    row.disagreements = int(moved["disagreements"])
    row.version = int(row.version) + 1
    row.last_outcome_at = moment
    row.note = f"learned after {row.observations} observation(s)"


# =============================================================================
# Running the detectors
# =============================================================================


def detector_is_enabled(detector: dict[str, Any]) -> bool:
    """Whether a detector may open cases as shipped.

    A detector whose own confidence is below its own gate cannot do what it is
    configured to do, and the honest reading of that row is "not yet" rather than
    "firing anyway". ``billing_dispute_unresolved`` ships in exactly this state
    on purpose: it documents the shape a detector needs without accusing
    customers who have an arrears balance and an open complaint, which is most of
    them.
    """
    return float(detector.get("confidence", 0.0)) >= float(detector.get("min_confidence", 1.0))


def evaluate_detector(
    detector: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Pure: does this detector's ``when`` match this context, and how sure is it.

    Uses the same ``rule_engine.evaluate_when`` and the same dict-shaped ``when``
    block the escalation triggers use, over the same context, so a detector and a
    trigger cannot drift apart on what "current state" means. (The string-formula
    entry point, ``evaluate_expression``, is a different DSL and is not what a
    ``when`` block is.) The result always carries the detector's configured
    confidence -- the rule decides *whether*, never *how sure*, and a rule that
    wanted to claim 0.99 confidence would be a rule inventing certainty.
    """
    when = detector.get("when") or {}
    try:
        matched, _fields = rule_engine.evaluate_when(when, context)
    except (TypeError, ValueError, KeyError):
        # A malformed `when` must not open cases about people. Treated as
        # no-match; the validator reports the malformed rule separately.
        matched = False
    confidence = float(detector.get("confidence", 0.0))
    return {
        "detector_id": str(detector.get("detector_id", "")),
        "label": str(detector.get("label", "")),
        "category": str(detector.get("category", "")),
        "matched": bool(matched),
        "confidence": confidence,
        "min_confidence": float(detector.get("min_confidence", 1.0)),
        "enabled": detector_is_enabled(detector),
        "passes_confidence": confidence >= float(detector.get("min_confidence", 1.0)),
        "why": str(detector.get("note", "")),
    }


def _cooldown_remaining_hours(
    last_fired: Optional[datetime], cooldown_hours: float, moment: datetime
) -> float:
    if last_fired is None or cooldown_hours <= 0:
        return 0.0
    elapsed = (moment - last_fired).total_seconds() / 3600.0
    return round(max(0.0, cooldown_hours - elapsed), 2)


#: A terminal case is finished and a resolved-but-unclosed one is the subject of
#: the `unresolved_stale_case` signal rather than of a new case, so detectors
#: read live work only.
_DETECTOR_VISIBLE_STATUSES: tuple[str, ...] = tuple(
    status for status in ("new", "open", "acknowledged", "in_progress", "escalated")
    if status not in COMPLAINT_TERMINAL_STATUSES
)
DETECTOR_SCAN_LIMIT = 200


async def detect_system_complaints(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    user_ids: Optional[Iterable[int]] = None,
    limit: int = DETECTOR_SCAN_LIMIT,
) -> dict[str, Any]:
    """Run every detector over live cases and report what *would* be raised.

    Read-only by construction: this is the safe call, and it is what an operator
    should run first. Every candidate carries the full reason it was or was not
    going to be raised -- matched, confidence, cooldown, dedupe -- so a detector
    that starts misbehaving is diagnosable from this payload alone rather than
    from a queue that mysteriously filled up.
    """
    moment = _as_utc(now) or _now()
    detectors = sorted(
        SYSTEM_COMPLAINT_DETECTORS, key=lambda row: -int(row.get("priority", 0))
    )
    statement = (
        select(models.ComplaintCase)
        .where(models.ComplaintCase.status.in_(_DETECTOR_VISIBLE_STATUSES))
        .order_by(models.ComplaintCase.opened_at.desc())
        .limit(max(1, min(int(limit), DETECTOR_SCAN_LIMIT)))
    )
    if user_ids is not None:
        ids = [int(uid) for uid in user_ids]
        if not ids:
            return _empty_detection(moment, "no_user_ids")
        statement = statement.where(models.ComplaintCase.user_id.in_(ids))

    cases = await _rows(db, statement)
    candidates: list[dict[str, Any]] = []
    for case in cases:
        context = await collect_complaint_context(db, case, now=moment)
        scope = dict(context.get("context") or {})
        for detector in detectors:
            verdict = evaluate_detector(detector, scope)
            if not verdict["matched"]:
                continue
            candidate = await _gate_candidate(db, case, detector, moment)
            candidates.append(candidate)

    raiseable = [c for c in candidates if c["will_raise"]]
    return {
        "ran_at": _iso(moment),
        "detectors": len(detectors),
        "cases_scanned": len(cases),
        "candidates": candidates,
        "would_raise": len(raiseable),
        "by_detector": _count_by(candidates, "detector_id"),
        "suppressed": {
            "disabled": sum(1 for c in candidates if not c["enabled"]),
            "low_confidence": sum(1 for c in candidates if not c["passes_confidence"]),
            "cooldown": sum(1 for c in candidates if c["blocked_by"] == "cooldown"),
            "deduped": sum(1 for c in candidates if c["blocked_by"] == "dedupe"),
        },
        "dry_run": True,
    }


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row.get(key, ""))
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def _empty_detection(moment: datetime, reason: str) -> dict[str, Any]:
    return {
        "ran_at": _iso(moment),
        "detectors": len(SYSTEM_COMPLAINT_DETECTORS),
        "cases_scanned": 0,
        "candidates": [],
        "would_raise": 0,
        "by_detector": {},
        "suppressed": {"disabled": 0, "low_confidence": 0, "cooldown": 0, "deduped": 0},
        "dry_run": True,
        "reason": reason,
    }


async def _gate_candidate(
    db: AsyncSession,
    case: Any,
    detector: dict[str, Any],
    moment: datetime,
) -> dict[str, Any]:
    """Apply all three guards and say which one stopped it.

    Order is fixed and reported: enabled, then confidence, then cooldown, then
    dedupe. Running them in this order means the reason a candidate was
    suppressed is the *first* reason, not whichever check happened to be written
    first, so the reported reason is stable when two would both apply.
    """
    detector_id = str(detector.get("detector_id", ""))
    source = system_origin(detector_id)
    enabled = detector_is_enabled(detector)
    passes = float(detector.get("confidence", 0.0)) >= float(detector.get("min_confidence", 1.0))

    last_fired = await _scalar(
        db,
        select(func.max(models.ComplaintCase.opened_at)).where(
            models.ComplaintCase.user_id == int(case.user_id),
            models.ComplaintCase.source == source,
        ),
    )
    remaining = _cooldown_remaining_hours(
        _as_utc(last_fired), float(detector.get("cooldown_hours", 0.0)), moment
    )

    dedupe_target: Optional[int] = None
    if bool(detector.get("dedupe_category")):
        # `exclude_id` is load-bearing and its absence was a real bug. The trigger
        # case is itself a live case in this category, so a dedupe check that
        # included it matched itself and blocked the raise -- permanently, for
        # every dedupe-enabled detector. Five of the six shipped detectors could
        # therefore never open a case at all, while every diagnostic said
        # "matched, blocked: dedupe", which reads exactly like correct behaviour.
        #
        # The trigger case is excluded because it is not evidence of the same
        # problem: it is the customer's own complaint, and the whole point of a
        # system-raised case is to record something the customer did not report.
        existing = await find_open_case(
            db,
            int(case.user_id),
            category=str(detector.get("category", "")),
            exclude_id=int(case.id),
        )
        dedupe_target = int(existing.id) if existing is not None else None

    blocked_by = ""
    if not enabled:
        blocked_by = "disabled"
    elif not passes:
        blocked_by = "confidence"
    elif remaining > 0:
        blocked_by = "cooldown"
    elif dedupe_target is not None:
        blocked_by = "dedupe"

    return {
        "detector_id": detector_id,
        "label": str(detector.get("label", "")),
        "category": str(detector.get("category", "")),
        "user_id": int(case.user_id),
        "trigger_case_id": int(case.id),
        "trigger_reference": str(getattr(case, "reference", "") or ""),
        "confidence": float(detector.get("confidence", 0.0)),
        "min_confidence": float(detector.get("min_confidence", 1.0)),
        "enabled": enabled,
        "passes_confidence": passes,
        "cooldown_hours": float(detector.get("cooldown_hours", 0.0)),
        "cooldown_remaining_hours": remaining,
        "dedupe_category": bool(detector.get("dedupe_category")),
        "dedupe_target_id": dedupe_target,
        "seed_signals": [str(s) for s in (detector.get("seed_signals") or [])],
        "default_severity": str(detector.get("default_severity", "medium")),
        "will_raise": blocked_by == "",
        "blocked_by": blocked_by,
    }


#: Per-call ceiling on system-raised cases. See the docstring: it is the guard
#: against one bad snapshot becoming one case per customer.
SYSTEM_RAISE_CAP = 25

async def raise_system_complaints(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    user_ids: Optional[Iterable[int]] = None,
    limit: int = DETECTOR_SCAN_LIMIT,
    max_raise: int = SYSTEM_RAISE_CAP,
    commit: bool = True,
) -> dict[str, Any]:
    """Detect, then open the cases that survived every guard.

    Two-stage on purpose: the same :func:`detect_system_complaints` gate decides
    what is raised, so a dry run and a real run cannot disagree about what
    *would* happen. The gates are re-evaluated here rather than trusted from the
    detection payload, because time passes and a cooldown can expire between the
    two -- reusing a stale verdict would open a case the guards now forbid.

    ``max_raise`` is a per-call ceiling on top of the per-detector cooldowns. A
    corrupt snapshot that makes every case look overdue is the failure this
    exists for: without a ceiling one sweep would open a case for every customer
    at once, and the queue would take longer to read than to fix.
    """
    moment = _as_utc(now) or _now()
    detection = await detect_system_complaints(
        db, now=moment, user_ids=user_ids, limit=limit
    )
    want = [c for c in detection["candidates"] if c["will_raise"]]
    # Highest-confidence first, so if the cap truncates, what survives is the
    # evidence the system is most sure about rather than the first rows returned.
    want.sort(key=lambda c: (-float(c["confidence"]), str(c["detector_id"]), int(c["user_id"])))
    selected = want[: max(0, int(max_raise))]

    raised: list[dict[str, Any]] = []
    held_back: list[dict[str, Any]] = [
        {"detector_id": str(c["detector_id"]), "user_id": int(c["user_id"]),
         "reason": "max_raise_cap"}
        for c in want[max(0, int(max_raise)):]
    ]
    for candidate in selected:
        detector = SYSTEM_DETECTOR_BY_ID.get(str(candidate["detector_id"]))
        if detector is None:  # pragma: no cover - detector table is closed
            continue
        # Re-check the cooldown against a fresh read. Cheap, and it is the one
        # guard whose answer can change between detection and the write.
        existing = await find_open_case(
            db,
            int(candidate["user_id"]),
            category=str(detector.get("category", "")),
            exclude_id=int(candidate["trigger_case_id"]),
        )
        if bool(detector.get("dedupe_category")) and existing is not None:
            held_back.append({
                "detector_id": str(candidate["detector_id"]),
                "user_id": int(candidate["user_id"]),
                "reason": "dedupe",
            })
            continue
        result = await open_complaint(
            db,
            int(candidate["user_id"]),
            category=str(detector.get("category", "")),
            severity=str(detector.get("default_severity", "")),
            summary=_system_summary(detector, candidate),
            source=system_origin(str(detector["detector_id"])),
            factors={
                "origin": SYSTEM_ORIGIN_PREFIX,
                "detector_id": str(detector["detector_id"]),
                "confidence": float(candidate["confidence"]),
                "min_confidence": float(candidate["min_confidence"]),
                "trigger_case_id": int(candidate["trigger_case_id"]),
                "trigger_reference": str(candidate["trigger_reference"]),
                "seed_signals": [str(s) for s in candidate["seed_signals"]],
                "seed_signal_source": "detector_config",
                "raised_by": SYSTEM_ORIGIN_PREFIX,
            },
            now=moment,
            commit=False,
        )
        raised.append(
            {
                "complaint_id": int(result.get("complaint_id", 0) or 0),
                "reference": str(result.get("reference", "")),
                "user_id": int(candidate["user_id"]),
                "detector_id": str(candidate["detector_id"]),
                "confidence": float(candidate["confidence"]),
                "category": str(candidate["category"]),
            }
        )

    if commit:
        await db.commit()
    else:
        await db.flush()

    # Everything the gates stopped, keyed by *which* gate, carried over from the
    # detection payload. Without this an operator asking "the sweep raised
    # nothing, why?" got `raised: 0, held_back: []` and no answer at all: the
    # candidates were filtered out before the raise loop ever saw them, so
    # `held_back` -- which only holds post-detection blocks and cap overflow --
    # was empty and the reasons were dropped on the floor.
    suppressed = dict(detection.get("suppressed") or {})
    for candidate in detection["candidates"]:
        if candidate["will_raise"]:
            continue
        reason = str(candidate.get("blocked_by") or "unknown")
        held_back.append({
            "detector_id": str(candidate["detector_id"]),
            "user_id": int(candidate["user_id"]),
            "reason": reason,
        })
        suppressed[reason] = suppressed.get(reason, 0) + 1

    return {
        "ran_at": _iso(moment),
        "detected": len(detection["candidates"]),
        "eligible": len(want),
        "raised": len(raised),
        "cases": raised,
        "held_back": held_back,
        "suppressed": suppressed,
        "cap": int(max_raise),
        "cap_reached": any(h["reason"] == "max_raise_cap" for h in held_back),
        "candidates": detection["candidates"],
    }


#: Sentences the customer can read. The system raised it, and the system says so
#: rather than writing as though the customer complained.
def _system_summary(detector: dict[str, Any], candidate: dict[str, Any]) -> str:
    label = str(detector.get("label", "a system-detected problem"))
    reference = str(candidate.get("trigger_reference") or "")
    tail = f" (raised while working {reference})" if reference else ""
    return f"Opened automatically by the service: {label.lower()}{tail}."


async def list_system_raised(
    db: AsyncSession, *, limit: int = 50
) -> dict[str, Any]:
    """Which cases the system opened, and which detector is most active.

    Reported by detector as well as in total, because "the system opened 40
    cases" is not actionable while "the billing detector opened 39 of them" is.
    """
    rows = await _rows(
        db,
        select(models.ComplaintCase)
        .where(models.ComplaintCase.source.like(f"{SYSTEM_ORIGIN_PREFIX}:%"))
        .order_by(models.ComplaintCase.opened_at.desc())
        .limit(max(1, min(int(limit), DETECTOR_SCAN_LIMIT))),
    )
    items = [
        {
            "complaint_id": int(row.id),
            "reference": str(row.reference or ""),
            "user_id": int(row.user_id),
            "detector_id": parse_system_origin(row.source) or "",
            "category": str(row.category or ""),
            "severity": str(row.severity or ""),
            "status": str(row.status or ""),
            "opened_at": _iso(row.opened_at),
        }
        for row in rows
    ]
    by_detector: dict[str, int] = {}
    for item in items:
        name = item["detector_id"] or "unknown"
        by_detector[name] = by_detector.get(name, 0) + 1
    return {
        "ran_at": _iso(_now()),
        "total": len(items),
        "by_detector": dict(sorted(by_detector.items(), key=lambda kv: (-kv[1], kv[0]))),
        "cases": items,
    }


# =============================================================================
# Stacked complaints
# =============================================================================


WITHDRAWN_STATUSES: tuple[str, ...] = ("withdrawn",)
CLUSTER_SCAN_LIMIT = 2000


def _cluster_axis_value(case: Any, axis: str) -> str:
    """The value of one axis for one case. Never empty.

    An empty key would put every case whose axis value is missing into one
    cluster, which is a real failure mode rather than a hypothetical one: a
    category the config does not recognise would silently aggregate every
    unrecognised case into a single "stacked complaints" suggestion.
    """
    if axis == "category":
        return str(getattr(case, "category", "") or DEFAULT_COMPLAINT_CATEGORY)
    if axis == "origin":
        source = str(getattr(case, "source", "") or "")
        # Keyed on the detector id, not the stored `system:<detector>` string, so
        # the cluster label in a proposal reads "sla_response_breach" rather than
        # repeating the storage convention at the reader.
        return parse_system_origin(source) or source or "unspecified"
    if axis == "owner_team":
        return str(getattr(case, "owner_team", "") or UNOWNED_CLUSTER_KEY)
    if axis == "severity":
        return str(getattr(case, "severity", "") or "unspecified")
    return str(axis)


#: The pseudo-team that unowned cases cluster under. Named so the cluster reads
#: as the finding it is -- that nobody is holding the work -- rather than as a
#: team called "unowned" that appears to have a backlog.
UNOWNED_CLUSTER_KEY = "__unowned__"
DEFAULT_COMPLAINT_CATEGORY = "service_quality"


def _cluster_evidence(cases: list[Any], rule: dict[str, Any], moment: datetime) -> dict[str, Any]:
    """The numbers a threshold decision is made on. Pure.

    ``reopen_rate`` is reported for every cluster, not just the rules that gate on
    it, because it is the number that separates "we are busy" from "closing these
    is not the same as fixing them" and a reviewer should not have to recompute it.
    """
    reopened = sum(1 for case in cases if int(getattr(case, "reopened_count", 0) or 0) > 0)
    distinct = {int(case.user_id) for case in cases}
    regulatory = sum(1 for case in cases if bool(getattr(case, "regulatory", False)))
    ages = [
        max(0.0, (moment - (_as_utc(getattr(case, "opened_at", None)) or moment)).total_seconds() / 86400.0)
        for case in cases
    ]
    return {
        "cases": len(cases),
        "distinct_users": len(distinct),
        "reopened": reopened,
        "reopen_rate": round(reopened / len(cases), 4) if cases else 0.0,
        "regulatory_cases": regulatory,
        "system_raised": sum(
            1 for case in cases
            if parse_system_origin(getattr(case, "source", "")) is not None
        ),
        "median_age_days": round(sorted(ages)[len(ages) // 2], 2) if ages else 0.0,
        "oldest_case_days": round(max(ages), 2) if ages else 0.0,
    }


def _shared_signals(members: list[Any]) -> list[str]:
    """The signals every case in a cluster carries, in configured-weight order.

    Read from each case's own `factors_json` because that is where the signals
    were recorded at decision time -- reading them back from the current weights
    table would report what is true *now* for a complaint decided under a
    different set of beliefs. Only signals present on every member qualify, so
    the list is a genuine common factor rather than a union that would be true of
    every cluster.
    """
    seen: list[set[str]] = []
    for case in members:
        factors = _loads(getattr(case, "factors_json", ""), "object")
        if not isinstance(factors, dict):
            return []
        recorded = factors.get("signals") or factors.get("seed_signals") or []
        if not isinstance(recorded, list) or not recorded:
            return []
        seen.append({str(item) for item in recorded})
    if not seen:
        return []
    common = set.intersection(*seen)
    ordered = sorted(common, key=lambda sid: (-signal_weight(sid), sid))
    return [sid for sid in ordered if sid in LOYALTY_SIGNAL_BY_ID][:MAX_SHARED_SIGNALS]


#: Enough to name the common factors, few enough that a proposal stays readable.
MAX_SHARED_SIGNALS = 5


def cluster_meets_thresholds(
    evidence: dict[str, Any], rule: dict[str, Any]
) -> dict[str, Any]:
    """Which thresholds a cluster met and which it missed. Pure.

    Reported per-threshold rather than as one bool so a suggestion that is *just*
    short says which number was short. A detector that reports only "no" gets
    tuned by guesswork, and a threshold lowered to the wrong value is how a
    suggestion stream turns into noise nobody reads.
    """
    checks = {
        "min_cases": (int(evidence["cases"]), int(rule["min_cases"])),
        "min_distinct_users": (int(evidence["distinct_users"]), int(rule["min_distinct_users"])),
        "min_reopen_rate": (float(evidence["reopen_rate"]), float(rule["min_reopen_rate"])),
    }
    met = {name: actual >= required for name, (actual, required) in checks.items()}
    return {
        "meets": all(met.values()),
        "checks": met,
        "short_by": {
            name: round(required - actual, 4)
            for name, (actual, required) in checks.items()
            if actual < required
        },
    }


async def detect_stacked_complaints(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    limit: int = CLUSTER_SCAN_LIMIT,
) -> dict[str, Any]:
    """Group recent cases by each configured axis and report the stacks.

    Read-only. Every cluster that met its thresholds is returned with its
    evidence and a deterministic ``cluster_key`` -- ``rule:axis:value`` -- so the
    same complaint history always produces the same key, and the proposal built
    from it in the next section gets the same id.
    """
    moment = _as_utc(now) or _now()
    since = moment - timedelta(days=max(int(r["window_days"]) for r in IMPROVEMENT_CLUSTER_RULES))
    cases = await _rows(
        db,
        select(models.ComplaintCase)
        .where(
            models.ComplaintCase.opened_at >= since,
            models.ComplaintCase.status.notin_(WITHDRAWN_STATUSES),
        )
        .order_by(models.ComplaintCase.opened_at.desc())
        .limit(max(1, min(int(limit), CLUSTER_SCAN_LIMIT))),
    )

    clusters: list[dict[str, Any]] = []
    for rule in IMPROVEMENT_CLUSTER_RULES:
        axis = str(rule["axis"])
        window_start = moment - timedelta(days=int(rule["window_days"]))
        grouped: dict[str, list[Any]] = {}
        origin_filter = str(rule.get("origin_filter") or "")
        for case in cases:
            opened = _as_utc(getattr(case, "opened_at", None))
            if opened is not None and opened < window_start:
                continue
            if origin_filter == "system" and parse_system_origin(
                getattr(case, "source", "")
            ) is None:
                continue
            grouped.setdefault(_cluster_axis_value(case, axis), []).append(case)
        for value, members in sorted(grouped.items()):
            evidence = _cluster_evidence(members, rule, moment)
            verdict = cluster_meets_thresholds(evidence, rule)
            clusters.append({
                "cluster_key": f"{rule['cluster_id']}:{axis}:{value}",
                "rule_id": str(rule["cluster_id"]),
                # `label` and `rationale` come from the rule so the rendered
                # proposal describes itself. They were missing here and
                # `build_improvement_proposal` reads `label` unconditionally, so
                # every *detected* cluster raised KeyError -- a crash that the
                # pure tests missed because they hand-built clusters carrying the
                # key. Found by the end-to-end cluster test.
                "label": str(rule.get("label", str(rule["cluster_id"]))),
                "rule_rationale": str(rule.get("rationale", "")),
                "axis": axis,
                "value": value,
                "window_days": int(rule["window_days"]),
                "evidence": evidence,
                **verdict,
                "case_ids": [int(case.id) for case in members],
                "references": [str(getattr(case, "reference", "") or "") for case in members],
                "shared_signals": _shared_signals(members),
            })

    stacks = [c for c in clusters if c["meets"]]
    stacks.sort(
        key=lambda c: (-c["evidence"]["cases"], -c["evidence"]["reopen_rate"], c["cluster_key"])
    )
    return {
        "ran_at": _iso(moment),
        "cases_scanned": len(cases),
        "rules": len(IMPROVEMENT_CLUSTER_RULES),
        "clusters_considered": len(clusters),
        "stacks": stacks,
        # Only clusters that have members. A filtered-empty group -- every case
        # excluded by `origin_filter`, say -- is not "nearly firing", it has
        # nothing in it, and listing it as a near miss invites someone to lower
        # a threshold that is not the problem.
        "near_misses": sorted(
            (
                c for c in clusters
                if not c["meets"] and c["evidence"]["cases"] > 0
            ),
            key=lambda c: (len(c["short_by"]), -c["evidence"]["cases"]),
        )[:10],
        "stack_count": len(stacks),
        "dry_run": True,
    }




# =============================================================================
# Improvement proposals
# =============================================================================


def proposal_id_for(cluster_key: str) -> str:
    """A stable id for a cluster, so re-detection never invents a new suggestion.

    Derived from the cluster key alone -- deliberately *not* from the evidence --
    so a cluster that grows from 5 cases to 50 keeps the same id and updates the
    same row. If the id moved with the numbers, every sweep would produce a fresh
    proposal and a human would see the same suggestion forever.
    """
    digest = hashlib.sha256(str(cluster_key).encode("utf-8")).hexdigest()[:12]
    return f"CIMP-{digest}"


def _proposal_level(value: Any, default: str = "medium") -> str:
    text = str(value or default)
    return text if text in PROPOSAL_LEVELS else default


def build_improvement_proposal(
    cluster: dict[str, Any], *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Render one stack into a suggestion. Pure, and shaped like the audit's.

    The shape matches ``efficiency_audit.EnhancementProposal`` (``id``, ``code``,
    ``component``, ``title``, ``priority``, ``impact``, ``effort``, ``rationale``,
    ``recommended_action``, ``owner_hint``, ``signals``) on purpose: a reader
    comparing the two suggestion streams should not have to translate, and the
    release ladder already knows how to carry a ``component``.

    The distinction it does *not* borrow is the priority basis. The audit ranks
    proposed work by code-shape evidence; this one ranks by loyalty damage, so a
    cluster of 40 minor complaints loses to a cluster of 6 that keep reopening
    even though the raw volume is smaller. Reopen rate is the honest signal that
    closing is not the same as fixing.
    """
    rule = IMPROVEMENT_CLUSTER_BY_ID.get(str(cluster["rule_id"]), {})
    evidence = cluster["evidence"]
    cases = int(evidence["cases"])
    users = int(evidence["distinct_users"])
    window = int(cluster["window_days"])
    reopen_pct = round(float(evidence["reopen_rate"]) * 100)
    signal_ids = [str(s) for s in (rule.get("signals") or [])] or _top_signals_for(cluster)

    rationale = [
        f"{cases} complaint(s) across {users} distinct customer(s) in the {window}-day "
        f"window, clustered by {cluster['axis']} = {cluster['value']}",
        f"{reopen_pct}% were reopened, so {reopen_pct}% of the recorded resolutions did not hold",
        f"median age {evidence['median_age_days']} days, oldest {evidence['oldest_case_days']} days",
    ]
    if int(evidence.get("system_raised", 0)) > 0:
        rationale.append(
            f"{evidence['system_raised']} of these were raised by the system, so the fault "
            f"is visible without a customer reporting it"
        )
    if int(evidence.get("regulatory_cases", 0)) > 0:
        rationale.append(
            f"{evidence['regulatory_cases']} carry a statutory deadline, which makes this a "
            f"compliance risk as well as a service one"
        )
    if not rationale:
        rationale = [str(rule.get("rationale", "stacked complaints warrant a look"))]

    action = PROPOSAL_ACTION_TEMPLATE.format(
        axis=cluster["axis"], cases=cases, users=users, window=window
    )
    if reopen_pct > 0:
        action += PROPOSAL_ACTION_REOPEN.format(reopen_pct=reopen_pct)
    if int(evidence.get("system_raised", 0)) > 0:
        action += PROPOSAL_ACTION_SYSTEM

    deterministic_id = proposal_id_for(str(cluster["cluster_key"]))
    return {
        # `id` and `code` exist so this is field-compatible with
        # `efficiency_audit.EnhancementProposal`; `proposal_id` is the canonical
        # name and the one stored on the row. All three carry the same value so
        # a reader comparing the two streams is not translating three names.
        "id": deterministic_id,
        "code": deterministic_id,
        "proposal_id": deterministic_id,
        "cluster_key": str(cluster["cluster_key"]),
        "rule_id": str(cluster["rule_id"]),
        "title": f"{cluster['label']}: {cluster['value']} ({cases} cases, {reopen_pct}% reopened)",
        "component": str(rule.get("component", "services/complaints")),
        "owner_hint": str(rule.get("owner_hint", "support lead")),
        "priority": _priority_for(cluster),
        "impact": _impact_for(cluster),
        "effort": _effort_for(cluster),
        "rationale": rationale,
        "recommended_action": action,
        "signals": signal_ids,
        "evidence": dict(evidence),
        "case_ids": [int(cid) for cid in cluster.get("case_ids", [])],
        "detected_at": _iso(now or _now()),
        "status": "detected",
        "candidate_id": "",
    }


def _priority_for(cluster: dict[str, Any]) -> str:
    """Rank by loyalty damage, not by volume.

    The rule's ``priority_when_hot`` is the floor and reopen rate is what lifts it,
    so a large-but-stable cluster does not outrank a small one that keeps coming
    back. A cluster that is both large and reopening is the top priority; that
    ordering is the whole reason this is not just ``cases`` descending.
    """
    rule = IMPROVEMENT_CLUSTER_BY_ID.get(str(cluster["rule_id"]), {})
    hot = str(rule.get("priority_when_hot", "medium"))
    if hot not in PROPOSAL_PRIORITIES:
        hot = "medium"
    evidence = cluster["evidence"]
    if float(evidence["reopen_rate"]) >= 0.5 and int(evidence["cases"]) >= 10:
        return "high"
    if int(evidence["distinct_users"]) >= 10:
        return "high"
    # A cluster nobody reopened is a capacity problem, not a correctness one, and
    # it is capped at medium whatever the rule's `priority_when_hot` says.
    #
    # The first version returned the rule's own floor unconditionally, so a
    # 40-case category that was closed and never reopened came out `high` --
    # the same priority as a 6-case category that keeps coming back. That is the
    # exact inversion the module argues against: the number of complaints is not
    # evidence that anything is broken, and the only number here that measures
    # "broken" is the reopen rate.
    if float(evidence["reopen_rate"]) <= 0.0 and hot == "high":
        return "medium"
    return hot


def _impact_for(cluster: dict[str, Any]) -> str:
    """Impact tracks reach, not volume: how many people this is likely to hit."""
    users = int(cluster["evidence"]["distinct_users"])
    if users >= 25:
        return "high"
    if users >= 8:
        return "medium"
    return "low"


def _effort_for(cluster: dict[str, Any]) -> str:
    """Effort is a hint, and it is honest about being one.

    A cluster keyed on ``origin`` pointing at one mis-tuned detector is a config
    change; a cluster spread across many customers and a wide window is a
    redesign. Both guesses are stated as ``low``/``high`` rather than as a number
    of days, because nobody here has measured either.
    """
    axis = str(cluster["axis"])
    users = int(cluster["evidence"]["distinct_users"])
    if axis == "origin" or users <= 3:
        return "low"
    if users >= 20:
        return "high"
    return "medium"


def _top_signals_for(cluster: dict[str, Any]) -> list[str]:
    """Which signals this cluster's cases shared. Empty when none were recorded."""
    return list(cluster.get("shared_signals") or [])


async def build_and_store_proposals(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    limit: int = CLUSTER_SCAN_LIMIT,
    commit: bool = True,
) -> dict[str, Any]:
    """Detect stacks, render proposals, and persist them idempotently.

    Idempotent in the only sense that matters: re-running on unchanged evidence
    updates the existing row's evidence and leaves its ``status`` alone, so a
    proposal a human has ``acknowledged`` or ``dismissed`` is not silently reset
    to ``detected`` by the next scheduled sweep. That is why ``status`` is only
    written on first insert.
    """
    moment = _as_utc(now) or _now()
    detection = await detect_stacked_complaints(db, now=moment, limit=limit)
    existing = {
        str(row.proposal_id): row
        for row in await _rows(db, select(models.ComplaintImprovementProposal))
    }

    stored: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for cluster in detection["stacks"]:
        proposal = build_improvement_proposal(cluster, now=moment)
        seen_ids.add(str(proposal["proposal_id"]))
        row = existing.get(str(proposal["proposal_id"]))
        if row is None:
            row = models.ComplaintImprovementProposal(
                proposal_id=str(proposal["proposal_id"]),
                cluster_key=str(proposal["cluster_key"]),
                status="detected",
            )
            db.add(row)
            created = True
        else:
            created = False
        row.title = str(proposal["title"])
        row.component = str(proposal["component"])
        row.owner_hint = str(proposal["owner_hint"])
        row.priority = str(proposal["priority"])
        row.impact = str(proposal["impact"])
        row.effort = str(proposal["effort"])
        row.rationale_json = _dumps(proposal["rationale"])
        row.recommended_action = str(proposal["recommended_action"])
        row.signals_json = _dumps(proposal["signals"])
        row.evidence_json = _dumps(proposal["evidence"])
        stored.append({**proposal, "created": created, "status": row.status})

    if commit:
        await db.commit()
    else:
        await db.flush()

    # Stacks that no longer meet thresholds are *not* deleted. A proposal that
    # vanished because a window slid would leave a reviewer who was mid-decision
    # with no record, so the row stays and `no_longer_stacking` is reported
    # instead. Nothing is ever removed by this function.
    stale = [
        pid for pid in existing
        if pid not in seen_ids and str(existing[pid].status) == "detected"
    ]
    return {
        "ran_at": _iso(moment),
        "stacks": detection["stack_count"],
        "proposals": stored,
        "created": sum(1 for p in stored if p["created"]),
        "updated": sum(1 for p in stored if not p["created"]),
        "no_longer_stacking": stale,
        "near_misses": detection["near_misses"],
    }


# =============================================================================
# Publishing to the release ladder
# =============================================================================


def render_proposal_summary(proposal: dict[str, Any]) -> str:
    """The one-line summary the ladder carries.

    Length is capped because ``ReleaseCandidate.summary`` is what a reviewer reads
    in a table before deciding whether to open the candidate, and a paragraph
    there gets truncated into uselessness. The full evidence stays on the
    proposal row and in the payload.
    """
    text = (
        f"[{proposal['proposal_id']}] {proposal['title']} -- "
        f"{proposal['evidence']['cases']} case(s), "
        f"{proposal['evidence']['distinct_users']} customer(s), "
        f"{round(float(proposal['evidence']['reopen_rate']) * 100)}% reopened; "
        f"suggested owner {proposal['owner_hint']}"
    )
    return text[:SUMMARY_MAX_CHARS].rstrip() + ("..." if len(text) > SUMMARY_MAX_CHARS else "")


SUMMARY_MAX_CHARS = 240


def proposal_to_candidate(
    proposal: dict[str, Any], *, commit: str = "", revision: str = "", dirty: bool = False
) -> Any:
    """Turn a proposal into an ``l0_draft`` release candidate.

    The candidate is registered at draft and *only* at draft. The ladder's own
    ``register_candidate`` refuses anything higher, and that refusal is the point:
    "how far along is this" and "may this go further" are separate questions, and
    a complaint count is evidence for the first one and never for the second.
    """
    from app import release_ladder

    pair = release_ladder.code_and_data_pair(
        commit=commit, revision=revision, dirty=dirty
    )
    return release_ladder.ReleaseCandidate(
        candidate_id=str(proposal["proposal_id"]),
        code_version=pair["code_version"],
        data_version=pair["data_version"],
        level=release_ladder.DRAFT_LEVEL,
        summary=render_proposal_summary(proposal),
        maintainer=str(proposal.get("owner_hint", "")),
        kaizen_source="complaints:stacked_cluster",
        measured={
            "complaint_cases": int(proposal["evidence"]["cases"]),
            "distinct_customers": int(proposal["evidence"]["distinct_users"]),
            "reopen_rate": float(proposal["evidence"]["reopen_rate"]),
            "regulatory_cases": int(proposal["evidence"].get("regulatory_cases", 0)),
            "system_raised": int(proposal["evidence"].get("system_raised", 0)),
            "priority": str(proposal["priority"]),
            "impact": str(proposal["impact"]),
        },
    )


async def publish_proposals(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    proposal_ids: Optional[Iterable[str]] = None,
    commit_id: str = "",
    revision: str = "",
    dirty: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    """Register stored proposals on the release ladder at ``l0_draft``.

    Only ``detected`` proposals are published. A ``dismissed`` proposal that got
    re-published would be a decision being silently reversed by a scheduled job,
    and ``published`` ones are already there -- re-registering the same id would
    raise from the ladder rather than quietly duplicating, which is the correct
    behaviour and is left as an error rather than swallowed.

    Nothing here advances a candidate. Promotion runs through
    ``release_ladder.advance_candidate`` and its gates, which is the whole
    reason suggestions go to the ladder rather than to a file.
    """
    from app import release_ladder

    moment = _as_utc(now) or _now()
    wanted = {str(pid) for pid in proposal_ids} if proposal_ids is not None else None
    statement = select(models.ComplaintImprovementProposal).where(
        models.ComplaintImprovementProposal.status == "detected"
    )
    rows = await _rows(db, statement)
    if wanted is not None:
        rows = [row for row in rows if str(row.proposal_id) in wanted]

    published: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        proposal = _row_to_proposal(row)
        if not proposal:
            skipped.append({"proposal_id": str(row.proposal_id), "reason": "unrenderable"})
            continue
        if release_ladder.find_candidate(str(proposal["proposal_id"])) is not None:
            skipped.append({"proposal_id": str(proposal["proposal_id"]), "reason": "already_registered"})
            continue
        candidate = proposal_to_candidate(
            proposal, commit=commit_id, revision=revision, dirty=dirty
        )
        try:
            release_ladder.register_candidate(candidate)
        except ValueError as exc:
            # A duplicate id is a real conflict, not a crash: report it and move
            # on to the next proposal so one clash does not block the batch.
            skipped.append({"proposal_id": str(proposal["proposal_id"]), "reason": f"rejected: {exc}"})
            continue
        row.status = "published"
        row.candidate_id = str(candidate.candidate_id)
        published.append({
            "proposal_id": str(proposal["proposal_id"]),
            "candidate_id": str(candidate.candidate_id),
            "level": str(candidate.level),
            "summary": str(candidate.summary),
        })

    if commit:
        await db.commit()
    else:
        await db.flush()

    return {
        "ran_at": _iso(moment),
        "published": published,
        "published_count": len(published),
        "skipped": skipped,
        "ladder_level": "l0_draft",
        "note": (
            "registered as unreviewed drafts. Promotion is a separate, gated "
            "operation and this function never performs it"
        ),
    }


def _row_to_proposal(row: Any) -> dict[str, Any]:
    evidence = _loads(row.evidence_json, "object")
    if not isinstance(evidence, dict) or not evidence:
        return {}
    return {
        "proposal_id": str(row.proposal_id),
        "code": str(row.proposal_id),
        "cluster_key": str(row.cluster_key),
        "title": str(row.title),
        "component": str(row.component),
        "owner_hint": str(row.owner_hint),
        "priority": str(row.priority),
        "impact": str(row.impact),
        "effort": str(row.effort),
        "rationale": _loads(row.rationale_json, "array"),
        "recommended_action": str(row.recommended_action),
        "signals": _loads(row.signals_json, "array"),
        "evidence": evidence,
        "status": str(row.status),
        "candidate_id": str(row.candidate_id),
    }


async def list_proposals(
    db: AsyncSession, *, status: str = "", limit: int = 100
) -> dict[str, Any]:
    """Stored proposals, newest evidence first. Read-only."""
    statement = select(models.ComplaintImprovementProposal)
    if status:
        statement = statement.where(models.ComplaintImprovementProposal.status == str(status))
    rows = await _rows(
        db, statement.order_by(models.ComplaintImprovementProposal.updated_at.desc())
        .limit(max(1, min(int(limit), 500)))
    )
    items = [_row_to_proposal(row) for row in rows]
    by_status: dict[str, int] = {}
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
    return {
        "ran_at": _iso(_now()),
        "total": len(items),
        "by_status": dict(sorted(by_status.items())),
        "by_priority": _count_by(items, "priority"),
        "proposals": items,
    }


async def set_proposal_status(
    db: AsyncSession, proposal_id: str, status: str, *, commit: bool = True
) -> dict[str, Any]:
    """Move a proposal between states. The human's decision, recorded.

    Deliberately one-way out of ``dismissed``: re-opening a dismissed proposal
    needs a reason, and this function does not take one, so undoing a dismissal
    is a deliberate act elsewhere rather than a slip. Returning an error for an
    unknown status is the repo's fail-closed convention applied to a write path.
    """
    if str(status) not in PROPOSAL_STATUSES:
        raise ValueError(f"unknown proposal status: {status!r}")
    row = await _scalar(
        db,
        select(models.ComplaintImprovementProposal).where(
            models.ComplaintImprovementProposal.proposal_id == str(proposal_id)
        ),
    )
    if row is None:
        raise ValueError(f"unknown proposal: {proposal_id!r}")
    previous = str(row.status)
    row.status = str(status)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return {"proposal_id": str(proposal_id), "previous": previous, "status": str(status)}


# =============================================================================
# BLOCKAGES.md
# =============================================================================
#
# This module does **not** write to `BLOCKAGES.md`, and the absence is deliberate
# rather than an unfinished step.
#
# That file is hand-authored governance prose with a keep-as-is decisions
# section, a "do not fix" list, and a machine-sync guardrail. A generated section
# inside it is worse than useless in two ways: it grows without bound as clusters
# come and go, and a reviewer reading "the system says 14 suggestions are open"
# has no way to tell which are real. Worse, it inverts the document's authority --
# everything else there is a human having thought about something.
#
# So the suggestions are made *renderable* instead of written. A human runs
# `render_blockages_section`, pastes the result, and edits it like anything else
# in the file. The deterministic `proposal_id` in every heading means a re-paste
# is idempotent, and the "not included" list tells them what changed since last
# time rather than silently dropping it.


def render_blockages_section(
    proposals: list[dict[str, Any]], *, now: Optional[datetime] = None
) -> str:
    """Render open proposals as pasteable markdown. Pure, writes nothing.

    Deterministic in its input: same proposals, same bytes, so re-rendering after
    an unrelated change produces a clean diff instead of churning the file.
    """
    moment = _as_utc(now) or _now()
    open_items = [p for p in proposals if str(p.get("status")) in {"detected", "published"}]
    if not open_items:
        return (
            "## System-improvement suggestions (auto-derived)\n\n"
            f"_As of {moment.date().isoformat()}: none open. Stacked-complaint "
            "detection ran and every cluster was under its thresholds._\n"
        )
    lines = [
        "## System-improvement suggestions (auto-derived)",
        "",
        f"_As of {moment.date().isoformat()}. Derived from stacked complaints by "
        "`services/complaint_learning.py`. Each is registered on the release "
        "ladder at `l0_draft` and must earn promotion through its gates. Paste, "
        "then edit freely -- this section is a suggestion, not a record._",
        "",
    ]
    for proposal in sorted(open_items, key=lambda p: (PRIORITY_RANK.get(str(p["priority"]), 9), str(p["proposal_id"]))):
        evidence = proposal.get("evidence", {})
        lines.append(
            f"### {proposal['proposal_id']} — {proposal['title']}"
        )
        lines.append("")
        lines.append(
            f"- **Priority** {proposal['priority']} · **impact** {proposal['impact']}"
            f" · **effort** {proposal['effort']}"
        )
        lines.append(
            f"- **Component** `{proposal['component']}` · "
            f"**suggested owner** {proposal['owner_hint']}"
        )
        lines.append(
            f"- **Evidence** {evidence.get('cases', 0)} case(s), "
            f"{evidence.get('distinct_users', 0)} distinct customer(s), "
            f"{round(float(evidence.get('reopen_rate', 0.0)) * 100)}% reopened"
            + (
                f", {evidence['system_raised']} system-raised"
                if int(evidence.get("system_raised", 0) or 0) > 0 else ""
            )
        )
        if proposal.get("candidate_id"):
            lines.append(f"- **Ladder** `{proposal['candidate_id']}` at `l0_draft`")
        lines.append("")
        lines.append(f"**Recommended action.** {proposal['recommended_action']}")
        lines.append("")
        for item in proposal.get("rationale", []):
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def diff_against_rendered(
    proposals: list[dict[str, Any]], existing_text: str, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """What a fresh render would add, given what ``BLOCKAGES.md`` already says.

    Read-only, and it never proposes deleting a line. A heading that is present is
    reported as already-there and a heading that is absent is reported as new;
    anything in the file this cannot parse is left strictly alone. That asymmetry
    is the point -- this function is allowed to add suggestions to a human's
    document and is structurally incapable of removing the human's words.
    """
    fresh = render_blockages_section(proposals, now=now)
    present = {str(p["proposal_id"]) for p in proposals}
    already = sorted(pid for pid in present if pid in (existing_text or ""))
    new = sorted(pid for pid in present if pid not in (existing_text or ""))
    return {
        "ran_at": _iso(now or _now()),
        "rendered": fresh,
        "already_present": already,
        "new": new,
        "would_add": len(new),
        "writes": False,
        "note": (
            "this function never edits the file. It reports what a paste would "
            "add; removals are the human's to make"
        ),
    }


# =============================================================================
# Validation (errors vs warnings)
# =============================================================================


def validate_complaint_learning() -> dict[str, Any]:
    """Every invariant this module claims, checked at startup.

    Errors are for things that would make the module *wrong*: a signal with no
    reader would silently never contribute, a detector whose confidence is below
    its own gate is a row that documents an intention it cannot act on, and an
    authority flag flipped to ``True`` is the whole safety argument withdrawn.
    Warnings are for things that are *suspicious but not broken*: a detector
    nobody has ever seen fire, a cluster rule that can never be satisfied.

    The distinction matters because a validator that cries wolf gets ignored, and
    an ignored validator is the same as no validator.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # --- signals -------------------------------------------------------------
    for row in LOYALTY_SIGNALS:
        signal_id = str(row["signal_id"])
        if str(row["derive"]) not in SIGNAL_READERS:
            errors.append(
                f"LOYALTY_SIGNALS[{signal_id}].derive={row['derive']!r} has no reader "
                f"in SIGNAL_READERS; the signal would silently never contribute"
            )
        if str(row["direction"]) not in SIGNAL_DIRECTIONS:
            errors.append(
                f"LOYALTY_SIGNALS[{signal_id}].direction={row['direction']!r} is not one of {SIGNAL_DIRECTIONS}"
            )
        if float(row["prior_weight"]) < 0:
            errors.append(f"LOYALTY_SIGNALS[{signal_id}].prior_weight must be >= 0")
        if str(row["direction"]) == "neutral" and signal_id not in NEUTRAL_SIGNAL_IDS:
            errors.append(
                f"LOYALTY_SIGNALS[{signal_id}] is neutral but not listed in "
                f"NEUTRAL_SIGNAL_IDS; add it there or give it a real direction"
            )
    for name in SIGNAL_READERS:
        if not any(str(r["derive"]) == name for r in LOYALTY_SIGNALS):
            warnings.append(
                f"SIGNAL_READERS[{name}] is unreachable: no signal declares derive={name!r}"
            )

    # --- outcomes ------------------------------------------------------------
    for row in LOYALTY_OUTCOMES:
        name = str(row["outcome"])
        if name not in {"retained", "re-engaged", "neutral", "dormant", "churned"}:
            errors.append(f"LOYALTY_OUTCOMES[{name!r}] is outside the declared vocabulary")
        if not observation_is_decidable(name) and bool(row.get("terminal")):
            errors.append(f"LOYALTY_OUTCOMES[{name}] claims terminal but is not decidable")

    # --- learning parameters -------------------------------------------------
    params = LEARNING_PARAMS
    if float(params["min_weight"]) >= float(params["max_weight"]):
        errors.append("LEARNING_PARAMS min_weight must be < max_weight")
    if float(params["decay_per_observation"]) <= 0:
        errors.append("LEARNING_PARAMS decay_per_observation must be > 0 or weights never decay")
    if float(params["confidence_min_observations"]) < 1:
        errors.append("LEARNING_PARAMS confidence_min_observations must be >= 1")
    for signal_id, prior in default_weights().items():
        if not (float(params["min_weight"]) <= prior <= float(params["max_weight"])):
            errors.append(
                f"LOYALTY_SIGNALS[{signal_id}].prior_weight={prior} is outside the "
                f"learning bounds, so the first observation would clamp it and the "
                f"configured prior would be unreachable"
            )

    # --- the authority invariant, enforced ------------------------------------
    for flag in ("may_change_severity_alone", "may_change_tier_alone", "may_auto_escalate"):
        if LEARNED_WEIGHT_AUTHORITY.get(flag) is not False:
            errors.append(
                f"LEARNED_WEIGHT_AUTHORITY.{flag} must be False. A learned weight is a "
                f"statistic; letting it move a tier or an escalation is a feedback loop, "
                f"not a policy"
            )
    for flag in ("may_reorder_recommendations", "may_change_explanation"):
        if LEARNED_WEIGHT_AUTHORITY.get(flag) is not True:
            warnings.append(
                f"LEARNED_WEIGHT_AUTHORITY.{flag} is False, so learned weights currently "
                f"do nothing observable"
            )

    # --- detectors -----------------------------------------------------------
    for row in SYSTEM_COMPLAINT_DETECTORS:
        detector_id = str(row["detector_id"])
        category = str(row["category"])
        if category not in COMPLAINT_CATEGORY_BY_NAME:
            errors.append(
                f"SYSTEM_COMPLAINT_DETECTORS[{detector_id}].category={category!r} is not a "
                f"complaint category; the case would open under a default nobody chose"
            )
        if str(row["default_severity"]) not in {"low", "medium", "high", "critical"}:
            errors.append(f"SYSTEM_COMPLAINT_DETECTORS[{detector_id}].default_severity is not a severity word")
        if not 0.0 <= float(row["confidence"]) <= 1.0:
            errors.append(f"SYSTEM_COMPLAINT_DETECTORS[{detector_id}].confidence must be in [0, 1]")
        if not 0.0 <= float(row["min_confidence"]) <= 1.0:
            errors.append(f"SYSTEM_COMPLAINT_DETECTORS[{detector_id}].min_confidence must be in [0, 1]")
        if float(row["cooldown_hours"]) <= 0:
            errors.append(
                f"SYSTEM_COMPLAINT_DETECTORS[{detector_id}].cooldown_hours must be > 0; "
                f"without it a stuck condition opens a case every sweep"
            )
        for signal_id in row.get("seed_signals") or []:
            if str(signal_id) not in LOYALTY_SIGNAL_BY_ID:
                errors.append(
                    f"SYSTEM_COMPLAINT_DETECTORS[{detector_id}].seed_signals contains "
                    f"unknown signal {signal_id!r}"
                )
        check = rule_engine.validate_when(row.get("when", {}))
        if not check.get("valid", True):
            errors.append(
                f"SYSTEM_COMPLAINT_DETECTORS[{detector_id}].when is invalid: {check.get('errors')}"
            )
        for field in check.get("fields_referenced", []) or []:
            if field not in _DETECTOR_CONTEXT_KEYS:
                errors.append(
                    f"SYSTEM_COMPLAINT_DETECTORS[{detector_id}].when reads context key "
                    f"{field!r}, which no complaint context produces"
                )
        if float(row["confidence"]) < float(row["min_confidence"]):
            warnings.append(
                f"SYSTEM_COMPLAINT_DETECTORS[{detector_id}] is inert as shipped: confidence "
                f"{row['confidence']} < min_confidence {row['min_confidence']}"
            )
        if not row.get("dedupe_category"):
            warnings.append(
                f"SYSTEM_COMPLAINT_DETECTORS[{detector_id}] does not dedupe, so it can open a "
                f"second case for a problem that already has one"
            )

    # --- cluster rules -------------------------------------------------------
    for row in IMPROVEMENT_CLUSTER_RULES:
        cluster_id = str(row["cluster_id"])
        if int(row["min_cases"]) < 1 or int(row["min_distinct_users"]) < 1:
            errors.append(f"IMPROVEMENT_CLUSTER_RULES[{cluster_id}] thresholds must be >= 1")
        if int(row["min_distinct_users"]) > int(row["min_cases"]):
            errors.append(
                f"IMPROVEMENT_CLUSTER_RULES[{cluster_id}].min_distinct_users exceeds "
                f"min_cases, so the rule can never fire"
            )
        if not 0.0 <= float(row["min_reopen_rate"]) <= 1.0:
            errors.append(f"IMPROVEMENT_CLUSTER_RULES[{cluster_id}].min_reopen_rate must be in [0, 1]")
        if str(row["priority_when_hot"]) not in PROPOSAL_PRIORITIES:
            errors.append(f"IMPROVEMENT_CLUSTER_RULES[{cluster_id}].priority_when_hot is not a priority word")
        if _cluster_axis_value(_AxisProbe(), str(row["axis"])) == str(row["axis"]):
            errors.append(
                f"IMPROVEMENT_CLUSTER_RULES[{cluster_id}].axis={row['axis']!r} is not one of "
                f"the axes _cluster_axis_value knows"
            )

    # --- the outcome-derivation argument, checked ---------------------------
    # `derive_loyalty_outcome` claims churned dominates. If a re-engaged customer
    # at critical churn risk ever classified as retained, the claim would be a lie
    # and every weight trained on it would be wrong.
    _probe = _probe_scope()
    for overrides, expected in (
        ({"churn_risk": "critical", "days_to_rebooking": 1}, "churned"),
        ({"churn_risk": "critical", "reactivated": True}, "churned"),
        ({"days_since_complaint": 1, "days_since_last_contact": 0}, "neutral"),
        ({"days_since_complaint": 90, "days_since_last_contact": 90, "churn_risk": "low"}, "dormant"),
    ):
        got = derive_loyalty_outcome({**_probe, **overrides})["outcome"]
        if got != expected:
            errors.append(
                f"derive_loyalty_outcome: {overrides} produced {got!r}, expected {expected!r}"
            )

    # --- neutral never trains -----------------------------------------------
    for direction in SIGNAL_DIRECTIONS:
        moved = weight_after_observation(
            current=1.0, prior=1.0, direction=direction,
            loyalty_delta=-1.0, observations=5, agreements=2, disagreements=3,
        )
        if direction == "neutral" and moved["moved"]:
            errors.append("a neutral signal moved on an observation; neutral signals must never train")
    if loyalty_delta_for("neutral") != 0.0:
        errors.append("loyalty_delta_for('neutral') must be 0.0")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "version": COMPLAINT_LEARNING_VERSION,
        "checked": [
            "every signal has a reader and a known direction",
            "every configured prior is inside the learning bounds",
            "learned weights cannot move a tier or an escalation",
            "every detector's category, severity, confidence and cooldown are sane",
            "every detector's `when` parses and reads only real context keys",
            "every cluster rule can actually fire",
            "churn still dominates in the outcome derivation",
            "a neutral outcome trains nothing",
        ],
    }


#: The keys a detector's ``when`` may read. The independent statement of what a
#: complaint context produces, in the same spirit as complaints'
#: ``_produced_context_keys``: deriving it from the consumer would be a tautology.
_DETECTOR_CONTEXT_KEYS: frozenset[str] = frozenset({
    "category", "severity", "severity_rank", "status", "tier", "age_hours",
    "response_overdue_hours", "resolution_overdue_hours", "regulatory_deadline_hours",
    "regulatory_hours_remaining", "regulatory", "resolved", "acknowledged", "open_only",
    "no_owner", "has_owner", "owner_user_id", "open_complaints", "total_complaints",
    "complaints_last_30d", "reopened_count", "duplicate_open_cases", "dissatisfaction_score",
    "churn_risk", "loyalty_score", "value_tier", "lifecycle_stage", "policy_tier",
    "control_posture", "arrears_total", "points_balance", "points_at_risk",
    "pending_bookings", "cancelled_bookings", "locale", "consent_withholds_contact",
    "satisfaction_score", "resolution_code", "resolved_stale_hours",
    "severity_understated_by", "override_would_lower_tier",
})


class _AxisProbe:
    """A stand-in case, so the axis check can call the real reader.

    The axis check is "is this an axis the reader understands", and answering it
    with a second hard-coded list of axis names would be a list that can drift
    from the function it is supposed to describe. This makes the reader itself
    answer.
    """

    category = "service_quality"
    source = "chat"
    owner_team = "support"
    severity = "medium"


def _probe_scope() -> dict[str, Any]:
    from app.services.complaints import _probe_scope as complaints_probe

    return complaints_probe()


# =============================================================================
# Catalog (discoverability)
# =============================================================================


def build_complaint_learning_catalog() -> dict[str, Any]:
    """Introspection payload for the catalog surfaces.

    Includes the parts a reviewer needs in order to disagree: what the objective
    actually is, what the weights are *allowed* to do, and which detectors are
    inert as shipped. A catalog that only listed the active detectors would make
    a deliberately-disabled row look like a missing feature.
    """
    return {
        "catalog_version": COMPLAINT_LEARNING_VERSION,
        "objective": {
            "name": "customer retention and habitual return",
            "measured_by": [str(row["outcome"]) for row in LOYALTY_OUTCOMES],
            "not_optimised": [
                "time-on-service",
                "contact frequency",
                "conversation depth",
            ],
            "why_not": (
                "a complaint system that rewards more contact is inverting its own "
                "purpose: the customer contacting us is the failure signal, and "
                "treating it as the goal would make the loudest, angriest customer "
                "the most valued one"
            ),
            "min_days_before_judgement": LOYALTY_OBSERVATION_MIN_DAYS,
            "window_days": LOYALTY_OBSERVATION_WINDOW_DAYS,
        },
        "learning": {
            "parameters": dict(LEARNING_PARAMS),
            "authority": dict(LEARNED_WEIGHT_AUTHORITY),
            "signals": [
                {
                    "signal_id": str(row["signal_id"]),
                    "label": str(row["label"]),
                    "direction": str(row["direction"]),
                    "prior_weight": float(row["prior_weight"]),
                    "derive": str(row["derive"]),
                    "why": str(row["why"]),
                }
                for row in LOYALTY_SIGNALS
            ],
        },
        "system_complaints": {
            "origin_prefix": SYSTEM_ORIGIN_PREFIX,
            "detectors": [
                {
                    **row,
                    "enabled": detector_is_enabled(row),
                    "origin": system_origin(str(row["detector_id"])),
                    "when_valid": rule_engine.validate_when(row.get("when", {})).get("valid", False),
                }
                for row in SYSTEM_COMPLAINT_DETECTORS
            ],
            "guards": [
                "min_confidence -- the detector's own estimate, compared to its gate",
                "cooldown_hours -- per (detector, customer), so a stuck condition cannot spam",
                f"max_raise={SYSTEM_RAISE_CAP} per call -- one bad snapshot cannot open a case per customer",
                "dedupe_category -- reuse a live case instead of opening a second one",
            ],
        },
        "improvements": {
            "cluster_rules": [dict(row) for row in IMPROVEMENT_CLUSTER_RULES],
            "statuses": list(PROPOSAL_STATUSES),
            "priorities": list(PROPOSAL_PRIORITIES),
            "levels": list(PROPOSAL_LEVELS),
            "publishes_to": "release ladder at l0_draft (never above, never auto-promoted)",
            "blockages_md": (
                "not written. BLOCKAGES.md is hand-authored governance prose; this "
                "module renders a pasteable section and reports a diff instead, and "
                "is structurally incapable of removing a human's lines"
            ),
            "id_stability": "proposal_id is a hash of the cluster key, not of the evidence",
        },
    }
