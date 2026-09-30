"""Complaint handling: the case spine, the escalation engine, and the evidence
behind an escalation decision.

Why this module exists
----------------------
The complaint surface was three disconnected fragments with no spine:

* ``RecoveryOutcome`` carried ``escalation_path_json`` and a handoff blob
  hardcoded to ``{"status": "open", "owner": "support"}``, both rewritten on
  every read -- current state, no history.
* ``escalate_ticket`` minted ``ESC-{user_id}-{sequence:03d}`` from an
  in-process counter, so the first escalation for a user after a restart
  produced a reference identical to every earlier one. Nothing was written.
* The only complaint-aware policy rule, ``sensitive_complaint`` in
  ``communication_strategy``, matched topic strings that ``build_summary``
  cannot produce, so it never fired.

What a real complaint system owes the people involved
-----------------------------------------------------
ISO 10002:2018 asks for four things this module tries to make structural
rather than aspirational: an *open, effective and easy-to-use* process for the
complainant; *objective* treatment of each complaint; *analysis and evaluation*
of complaints to improve the service; and *accountability*, meaning a proper
audit trail of what was decided. Operational practice adds the rest: a durable
reference, a named owner, tiered routing, SLAs that are measured rather than
asserted, and an escalation record that survives the process that wrote it.

Design
------
Config tables, never branches, following the same seven-part skeleton the rest
of the backend uses:

1. ``COMPLAINT_*`` / ``ESCALATION_*`` tables, each with a ``_BY_ID`` index and
   a pinned ``COMPLAINTS_CATALOG_VERSION``.
2. A pure evaluator that returns a verdict and **never raises** --
   ``build_escalation_decision``.
3. Three-level guard severity folding to one word (``advisory|review|reject``
   -> ``accept|review|reject``), failing closed on an unknown operator *and* on
   a missing metric.
4. A structured escalation decision shaped like ``risk_evaluator.escalation_for``
   and ``policy_scoring.resolve_tier_escalation``, so ``escalated: False`` is a
   reasoned outcome rather than silence.
5. ``validate_complaints()`` separating *errors* (a configured rule cannot run)
   from *warnings* (something is configured but not doing anything).
6. ``build_complaints_catalog()`` returning the tables, their operator
   vocabulary, and their severity meanings.

Two decisions worth stating out loud
------------------------------------
**Auto-escalation is deliberately narrow.** Only two trigger kinds may carry
``authority: "auto"`` -- ``regulatory`` and ``sla_breach`` -- and
``validate_complaints`` raises an *error* for any other. That is the whole
mechanism: a clock or a legal deadline may act without a human in the loop,
because waiting for one is itself the harm; everything that depends on
judgment is advisory and needs an operator to accept it. The narrowed set is
data, so it can be widened deliberately rather than by accident.

**An admin may not weaken policy past a floor.** ``communication_strategy``
resolves ``admin_select`` first and unconditionally, so an admin override can
mask a ``critical_risk_outreach`` demand for ``immediate_call``. Here the floor
is explicit: an override may raise severity or pick a higher tier, never lower
either below what the engine computed, and a ``reject``-severity guard is not
waivable at all. Overrides and waivers require step-up, which is how
"admin" and "admin who re-authenticated for this" stay distinguishable in the
decision log.

Decision support
----------------
``build_decision_dossier`` assembles named evidence blocks from every source
the decision can legitimately draw on -- live case facts, conversation
signals, relationship and commercial exposure, policy posture, journey context,
*historical operator decisions*, external enrichment, communication
constraints, and governance state. Each block is separately addressable and
carries its own ``confidence``, ``citation`` and ``staleness_seconds``, because
a dossier that quietly presents week-old data as current is worse than one
that says it is week-old.

Precedent retrieval is case-based reasoning: ``build_precedent_bundle`` ranks
past closed cases by a config-driven similarity and, crucially, **down-weights
a precedent whose case was later reopened or escalated further**. A decision log
that cannot learn from being wrong is not institutional memory, it is a backlog.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models, rule_engine

from app.services.preferences import CONSENT_GATED_PURPOSES, CONSENT_PURPOSE_BY_NAME

# Pinned separately from the tables so adding a row is not a breaking change.
COMPLAINTS_CATALOG_VERSION = "complaints_v1"
COMPLAINT_ESCALATION_PACK_VERSION = "complaint_escalation_v1"

# Open by default: the SLA clock is the module's whole reason for existing, and
# a disabled-by-default clock is how a breach goes unnoticed for a quarter.
COMPLAINT_AUTO_ESCALATION_ENABLED = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    """Coerce a timestamp to an aware datetime, or ``None``.

    Three input shapes arrive here. A ``datetime`` from a mapped row, which
    SQLite hands back naive; an ISO string, because
    ``load_precedent_candidates`` builds its candidate dicts from
    ``case_to_dict`` -- which renders timestamps as ISO -- and those dicts are
    exactly what the pure scoring functions receive; and ``None``.

    The string case is not hypothetical. It was missing, and because
    ``_recency_score`` returns ``0.0`` for an unparseable timestamp rather than
    raising, the recency term silently contributed nothing to every precedent
    score: the ranking still looked plausible and the half-life was simply
    inert. Accepting the string is the fix; returning a value rather than
    failing loudly is the part worth being careful about.
    """
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
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _iso(value: Any) -> Optional[str]:
    moment = _as_utc(value)
    return moment.isoformat() if moment is not None else None


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


# =============================================================================
# Vocabularies
# =============================================================================
#
# Mirrored by ENUM_FIELD_SPECS in app/models.py. The duplication is deliberate
# and is already the declared pattern for `user_consent_events.purpose`: models
# must not import services, so a vocabulary two layers share is stated twice,
# and `validate_complaints` cross-checks the two against each other so the
# duplication cannot drift silently.

COMPLAINT_STATUSES: tuple[str, ...] = (
    "open",
    "acknowledged",
    "in_progress",
    "escalated",
    "resolved",
    "closed",
    "withdrawn",
)

#: Statuses that mean "nobody is obligated to do anything more". A case in any
#: other state is live, and the auto-escalation sweep only reads live ones.
COMPLAINT_TERMINAL_STATUSES: tuple[str, ...] = ("closed", "withdrawn")
#: Statuses where a resolution has been offered but not yet signed off.
COMPLAINT_RESOLVED_STATUSES: tuple[str, ...] = ("resolved",)

COMPLAINT_SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
COMPLAINT_SEVERITY_RANK: dict[str, int] = {
    name: rank for rank, name in enumerate(COMPLAINT_SEVERITIES)
}

COMPLAINT_CATEGORIES: list[dict[str, Any]] = [
    {
        "category": "service_quality",
        "label": "Service quality",
        "default_severity": "medium",
        "regulatory": False,
        "owner_team": "service_recovery",
        "description": "How the service was delivered, as distinct from what was charged for it.",
    },
    {
        "category": "billing",
        "label": "Billing",
        "default_severity": "medium",
        "regulatory": False,
        "owner_team": "billing_ops",
        "description": "A charge, an invoice, or a discrepancy between them.",
    },
    {
        "category": "refund",
        "label": "Refund",
        "default_severity": "medium",
        "regulatory": False,
        "owner_team": "billing_ops",
        "description": "A request for money back, including a refusal to refund.",
    },
    {
        "category": "privacy",
        "label": "Data protection",
        "default_severity": "high",
        "regulatory": True,
        "owner_team": "privacy_office",
        "description": (
            "A data-protection complaint. Treated as regulatory because it may "
            "become a supervisory-authority matter (GDPR Art. 77 and the UK "
            "equivalent), which carries its own deadline independent of ours."
        ),
    },
    {
        "category": "booking_failure",
        "label": "Booking failure",
        "default_severity": "high",
        "regulatory": False,
        "owner_team": "service_recovery",
        "description": "A booking that did not happen, or happened wrongly.",
    },
    {
        "category": "communication",
        "label": "Communication",
        "default_severity": "low",
        "regulatory": False,
        "owner_team": "service_recovery",
        "description": "How or whether we replied, rather than the underlying service.",
    },
    {
        "category": "access_tier",
        "label": "Access tier",
        "default_severity": "low",
        "regulatory": False,
        "owner_team": "service_recovery",
        "description": "A disagreement about what the customer's tier entitles them to.",
    },
    {
        "category": "other",
        "label": "Other",
        "default_severity": "low",
        "regulatory": False,
        "owner_team": "service_recovery",
        "description": "The configured catch-all. Exists so a category is never unknown.",
    },
]
COMPLAINT_CATEGORY_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["category"]): dict(row) for row in COMPLAINT_CATEGORIES
}
DEFAULT_COMPLAINT_CATEGORY = "other"

COMPLAINT_RESOLUTION_CODES: tuple[str, ...] = (
    "refunded",
    "credited",
    "corrected",
    "explained",
    "apology_offered",
    "no_action_required",
    "withdrawn_by_complainant",
    "unresolved",
)

COMPLAINT_EVENT_TYPES: tuple[str, ...] = (
    "opened",
    "acknowledged",
    "assigned",
    "escalated",
    "de_escalated",
    "note_added",
    "status_changed",
    "resolved",
    "closed",
    "reopened",
    "withdrawn",
    "sla_breached",
    "auto_escalated",
)

COMPLAINT_DECISIONS: tuple[str, ...] = (
    "escalate",
    "hold",
    "decline",
    "resolve",
    "assign",
    "reopen",
    "waive_guard",
    "offer_goodwill",
)

COMPLAINT_DECISION_OUTCOMES: tuple[str, ...] = (
    "proposed",
    "applied",
    "auto_applied",
    "overridden",
    "declined",
    "superseded",
)

#: What the case actually did after the decision. ``""`` is a first-class value
#: meaning "not known yet" and is why the column is a non-null string rather than
#: nullable -- a precedent is only evidence once this is filled in.
COMPLAINT_OUTCOMES_OBSERVED: tuple[str, ...] = (
    "",
    "held",
    "resolved",
    "reopened",
    "escalated_further",
    "complainant_left",
    "no_contact",
)


# =============================================================================
# Escalation tiers
# =============================================================================
#
# Doubles as the routing table: a tier names the team that holds cases at that
# level and what a case at that level is expected to be able to do. Ordering is
# list order, which is also the escalation order.
ESCALATION_TIERS: list[dict[str, Any]] = [
    {
        "tier": "tier_1",
        "rank": 1,
        "label": "Frontline",
        "default_owner_team": "service_recovery",
        "can_resolve": True,
        "goodwill_ceiling_points": 500,
        "description": "First line. Handles the documented, resolvable cases.",
    },
    {
        "tier": "tier_2",
        "rank": 2,
        "label": "Specialist",
        "default_owner_team": "service_recovery",
        "can_resolve": True,
        "goodwill_ceiling_points": 2000,
        "description": "Escalated from tier 1. Needs domain knowledge or a system change.",
    },
    {
        "tier": "tier_3",
        "rank": 3,
        "label": "Senior",
        "default_owner_team": "senior_review",
        "can_resolve": True,
        "goodwill_ceiling_points": 10000,
        "description": "Escalated from tier 2. Serious, systemic, or legally constrained.",
    },
    {
        "tier": "executive",
        "rank": 4,
        "label": "Executive",
        "default_owner_team": "executive_review",
        "can_resolve": True,
        "goodwill_ceiling_points": None,
        "description": "Named accountability. No ceiling, which is why nothing routes here by default.",
    },
]
ESCALATION_TIER_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["tier"]): dict(row) for row in ESCALATION_TIERS
}
ESCALATION_TIER_ORDER: tuple[str, ...] = tuple(ESCALATION_TIER_BY_NAME)
DEFAULT_ESCALATION_TIER = "tier_1"

ESCALATION_AUTHORITIES: tuple[str, ...] = ("auto", "advisory")

#: The *only* trigger kinds permitted to act without a human. Enforced as an
#: error by `validate_complaints`, so widening this requires editing the data
#: deliberately rather than adding one row with a typo.
AUTO_ESCALATION_TRIGGER_KINDS: tuple[str, ...] = ("regulatory", "sla_breach")


def tier_rank(tier: Any) -> int:
    """Numeric ordering for a tier, or ``0`` for anything unrecognised.

    Zero rather than one: an unknown tier must sort *below* tier_1 so that
    "escalate to at least the current tier" treats it as no information, not as
    a promotion.
    """
    return int(ESCALATION_TIER_BY_NAME.get(str(tier or ""), {}).get("rank", 0))


def tier_by_rank(rank: Any) -> str:
    """A tier name for a numeric rank, or ``tier_1`` for anything unrecognised.

    The inverse of :func:`tier_rank`, and it exists because ``rule_engine``'s
    ``=`` expressions accept only numeric constants. A trigger that wants a
    computed tier has to compute an *ordering* and convert it here.
    """
    try:
        wanted = int(rank)
    except (TypeError, ValueError):
        return DEFAULT_ESCALATION_TIER
    for row in ESCALATION_TIERS:
        if int(row["rank"]) == wanted:
            return str(row["tier"])
    return DEFAULT_ESCALATION_TIER


def highest_tier(*tiers: Any) -> str:
    """The furthest-along tier named. Used to apply the authority floor."""
    best = DEFAULT_ESCALATION_TIER
    for candidate in tiers:
        if tier_rank(candidate) > tier_rank(best):
            best = str(candidate)
    return best


# =============================================================================
# SLA matrix
# =============================================================================
#
# Response and resolution targets per severity. These are the numbers the
# auto-escalation sweep reads, so they are data rather than a constant buried
# in a comparison.
SLA_MATRIX: list[dict[str, Any]] = [
    {"severity": "critical", "response_hours": 1, "resolution_hours": 24, "note": "the customer is exposed to ongoing loss"},
    {"severity": "high", "response_hours": 4, "resolution_hours": 72, "note": "material harm, no ongoing exposure"},
    {"severity": "medium", "response_hours": 8, "resolution_hours": 168, "note": "inconvenience; a week to fix"},
    {"severity": "low", "response_hours": 24, "resolution_hours": 336, "note": "two weeks; batched with the weekly review"},
]
SLA_BY_SEVERITY: dict[str, dict[str, Any]] = {
    str(row["severity"]): dict(row) for row in SLA_MATRIX
}
DEFAULT_SLA: dict[str, Any] = {
    "severity": "medium",
    "response_hours": 8,
    "resolution_hours": 168,
    "note": "fallback when a severity has no SLA row",
}

#: Regulatory deadlines override the internal matrix. GDPR Art. 77 / UK GDPR
#: give a controller one month to respond to a data-protection complaint, and
#: that clock is not ours to extend.
REGULATORY_SLA: dict[str, Any] = {
    "response_hours": 24,
    "resolution_hours": 720,
    "calendar_days": 30,
    "authority": "supervisory_authority",
    "note": (
        "one calendar month from receipt, per GDPR Art. 12(3) applied to a "
        "complaint under Art. 77. Internal SLAs are tighter by design; this one "
        "is a legal floor and auto-escalation fires against whichever is nearer"
    ),
}


def resolve_sla(severity: Any, *, regulatory: bool = False) -> dict[str, Any]:
    """The response/resolution targets for a case, honouring the regulatory floor.

    When a case is regulatory we take the *nearer* of the internal target and
    the statutory one. An internal promise looser than the law would be the
    worst possible outcome: it would be recorded as on-time while the deadline
    that actually matters had already passed.
    """
    row = SLA_BY_SEVERITY.get(str(severity or ""), DEFAULT_SLA)
    response_hours = float(row["response_hours"])
    resolution_hours = float(row["resolution_hours"])
    basis = "internal_matrix"
    if regulatory:
        # Take the *nearer* of the two, then say which one actually won.
        # Reporting `basis: "regulatory_floor"` for every regulatory case would
        # be a lie in the common case: our internal targets are usually the
        # stricter ones, and a reader asking "did the statutory clock or ours
        # govern this?" deserves the true answer rather than the flattering one.
        statutory_response = float(REGULATORY_SLA["response_hours"])
        statutory_resolution = float(REGULATORY_SLA["resolution_hours"])
        response_from_statute = statutory_response < response_hours
        resolution_from_statute = statutory_resolution < resolution_hours
        response_hours = min(response_hours, statutory_response)
        resolution_hours = min(resolution_hours, statutory_resolution)
        if response_from_statute and resolution_from_statute:
            basis = "regulatory_floor"
        elif response_from_statute or resolution_from_statute:
            basis = "regulatory_floor_partial"
        else:
            basis = "internal_matrix_within_regulatory_limit"
    return {
        "severity": str(row["severity"]),
        "response_hours": response_hours,
        "resolution_hours": resolution_hours,
        "basis": basis,
        "regulatory_deadline_days": int(REGULATORY_SLA["calendar_days"]) if regulatory else None,
        "note": str(REGULATORY_SLA["note"]) if regulatory else str(row["note"]),
    }


# =============================================================================
# Routing
# =============================================================================
#
# Where a case goes before anyone escalates it. First match wins, in list
# order, so the list is ordered most-specific first. `to_tier` here is a
# *floor* -- a trigger may push a case higher, never lower.
ROUTING_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "route_privacy",
        "label": "Data protection goes to the privacy office",
        "when": {"category": "privacy"},
        "to_tier": "tier_3",
        "owner_team": "privacy_office",
        "priority": 100,
        "rationale": "a data-protection complaint can become a regulator matter on a fixed clock",
    },
    {
        "rule_id": "route_billing",
        "label": "Money goes to billing",
        "when": {"category": ["billing", "refund"]},
        "to_tier": "tier_1",
        "owner_team": "billing_ops",
        "priority": 80,
        "rationale": "a refund needs someone who can move money",
    },
    {
        "rule_id": "route_critical",
        "label": "Critical complaints skip the frontline",
        "when": {"severity": "critical"},
        "to_tier": "tier_2",
        "owner_team": "senior_review",
        "priority": 70,
        "rationale": "the frontline handles documented cases; a critical one is by definition not",
    },
    {
        "rule_id": "route_booking_failure",
        "label": "Booking failures go to service recovery",
        "when": {"category": "booking_failure"},
        "to_tier": "tier_1",
        "owner_team": "service_recovery",
        "priority": 50,
        "rationale": "the booking lifecycle owner has the context",
    },
]
DEFAULT_ROUTE: dict[str, Any] = {
    "rule_id": "route_default",
    "label": "",
    "to_tier": DEFAULT_ESCALATION_TIER,
    "owner_team": "service_recovery",
    "priority": 0,
    "rationale": "the configured catch-all, so a case is never unrouted",
}


def resolve_route(context: dict[str, Any]) -> dict[str, Any]:
    """First matching routing rule, or the default."""
    for row in ROUTING_RULES:
        matched, _fields = rule_engine.evaluate_when(row.get("when", {}), context)
        if matched:
            return dict(row)
    return dict(DEFAULT_ROUTE)


# =============================================================================
# Escalation triggers
# =============================================================================
#
# Shaped as a `rule_engine` pack so priority ordering, effective windows, the
# `when` DSL, `=`-parameter expressions and per-rule explanations all come from
# the existing engine instead of being reimplemented. `select_rules` accepts a
# pack dict directly, so no registration is needed and this table stays in the
# module that owns the vocabulary.
#
# `authority` is the important field:
#   "auto"     -- the engine may apply this without a human. Restricted to
#                 AUTO_ESCALATION_TRIGGER_KINDS.
#   "advisory" -- recommend only; an operator must accept.
ESCALATION_TRIGGERS: list[dict[str, Any]] = [
    {
        "trigger_id": "regulatory_privacy_deadline",
        "label": "Statutory deadline at risk",
        "kind": "regulatory",
        "enabled": True,
        "priority": 100,
        "effective_from": "2026-01-01",
        "effective_to": None,
        "authority": "auto",
        "regulatory": True,
        "required_step_up": "loa2",
        "reason": (
            "a data-protection complaint is inside its one-month statutory "
            "window, and waiting for an operator to notice is the failure mode "
            "the deadline exists to prevent"
        ),
        "when": {"category": "privacy", "regulatory_deadline_hours": {"lt": 24}},
        "params": {
            "to_tier": "tier_3",
            "owner_team": "privacy_office",
            "sla_response_hours": 24,
            "sla_resolution_hours": 720,
        },
    },
    {
        "trigger_id": "regulatory_privacy_open",
        "label": "Data-protection complaint is unowned",
        "kind": "regulatory",
        "enabled": True,
        "priority": 95,
        "effective_from": "2026-01-01",
        "effective_to": None,
        "authority": "auto",
        "regulatory": True,
        "required_step_up": "loa2",
        "reason": "a regulatory complaint must never sit in an empty queue",
        "when": {"category": "privacy", "age_hours": {"gte": 1}, "no_owner": True},
        "params": {"to_tier": "tier_3", "owner_team": "privacy_office"},
    },
    {
        "trigger_id": "sla_response_breached",
        "label": "First-response SLA breached",
        "kind": "sla_breach",
        "enabled": True,
        "priority": 90,
        "effective_from": "2026-01-01",
        "effective_to": None,
        "authority": "auto",
        "regulatory": False,
        "required_step_up": "loa1",
        "reason": "we told the customer when we would reply and we did not",
        "when": {"response_overdue_hours": {"gt": 0}},
        "params": {
            # A rank rather than a name: rule_engine's `=` expressions accept
            # only numeric constants, so a name-valued comparison is not
            # expressible. `severity_rank` carries the ordering and
            # `tier_by_rank` turns the answer back into a tier name.
            "to_tier_rank": "=2 if severity_rank >= 2 else 1",
            "owner_team": "service_recovery",
        },
    },
    {
        "trigger_id": "sla_resolution_breached",
        "label": "Resolution SLA breached",
        "kind": "sla_breach",
        "enabled": True,
        "priority": 85,
        "effective_from": "2026-01-01",
        "effective_to": None,
        "authority": "auto",
        "regulatory": False,
        "required_step_up": "loa1",
        "reason": "the promised fix date has passed",
        "when": {"resolution_overdue_hours": {"gt": 0}, "resolved": False},
        "params": {
            "to_tier_rank": "=2 if severity_rank >= 2 else 1",
            "owner_team": "senior_review",
        },
    },
    {
        "trigger_id": "repeat_complainant",
        "label": "Repeat complainant",
        "kind": "judgment",
        "enabled": True,
        "priority": 70,
        "effective_from": "2026-01-01",
        "effective_to": None,
        "authority": "advisory",
        "regulatory": False,
        "required_step_up": "loa2",
        "reason": "a third open case in 90 days is a pattern, not three coincidences",
        "when": {"open_complaints": {"gte": 3}},
        "params": {"to_tier": "tier_2", "owner_team": "senior_review"},
    },
    {
        "trigger_id": "high_value_churn",
        "label": "Severe complaint from a customer we are about to lose",
        "kind": "judgment",
        "enabled": True,
        "priority": 65,
        "effective_from": "2026-01-01",
        "effective_to": None,
        "authority": "advisory",
        "regulatory": False,
        "required_step_up": "loa2",
        "reason": "the relationship is worth more than the fix, and needs a named owner",
        "when": {
            "severity": ["high", "critical"],
            "churn_risk": ["high", "critical"],
        },
        "params": {"to_tier": "tier_2", "owner_team": "retention"},
    },
    {
        "trigger_id": "reopened_again",
        "label": "Reopened a second time",
        "kind": "judgment",
        "enabled": True,
        "priority": 60,
        "effective_from": "2026-01-01",
        "effective_to": None,
        "authority": "advisory",
        "regulatory": False,
        "required_step_up": "loa2",
        "reason": "the first resolution was wrong; someone senior should own the second attempt",
        "when": {"reopened_count": {"gte": 1}},
        "params": {"to_tier": "tier_2", "owner_team": "senior_review"},
    },
    {
        "trigger_id": "complainant_left",
        "label": "Customer with unresolved money and high churn",
        "kind": "judgment",
        "enabled": True,
        "priority": 55,
        "effective_from": "2026-01-01",
        "effective_to": None,
        "authority": "advisory",
        "regulatory": False,
        "required_step_up": "loa2",
        "reason": "outstanding balance plus a live complaint is a save-or-write-off decision",
        "when": {"arrears_total": {"gt": 0}, "churn_risk": ["high", "critical"]},
        "params": {"to_tier": "tier_2", "owner_team": "retention"},
    },
]
ESCALATION_TRIGGER_BY_ID: dict[str, dict[str, Any]] = {
    str(row["trigger_id"]): dict(row) for row in ESCALATION_TRIGGERS
}


def complaint_escalation_pack() -> dict[str, Any]:
    """The trigger table as a ``rule_engine`` pack.

    A projection, not a copy: the rules are the *same dicts* the module
    evaluates, so ``export_rule_pack`` and ``validate_when`` describe exactly
    what will run. `to_tier` and `owner_team` live in ``params`` because that is
    where the engine resolves ``=`` expressions, which is how
    ``sla_response_breached`` computes its target tier from severity.
    """
    return {
        "pack": "complaint_escalation",
        "version": COMPLAINT_ESCALATION_PACK_VERSION,
        "domain": "complaints",
        "priority": 120,
        "description": "Which complaint cases escalate, to where, and on whose authority.",
        "rules": [
            {
                "id": str(row["trigger_id"]),
                "enabled": bool(row.get("enabled", True)),
                "priority": int(row.get("priority", 0)),
                "effective_from": row.get("effective_from"),
                "effective_to": row.get("effective_to"),
                "when": row.get("when", {}),
                "params": row.get("params", {}),
            }
            for row in ESCALATION_TRIGGERS
        ],
    }


# =============================================================================
# Authority floor
# =============================================================================
#
# The floor an admin override may not cross. This exists because
# `communication_strategy` resolves `admin_select` first and unconditionally,
# which lets an admin's `quiet_email` override a `critical_risk_outreach` demand
# for `immediate_call`. Here the asymmetry is explicit and asymmetric on purpose:
# an override may *raise* severity or push *up* a tier, never lower either.
ESCALATION_OVERRIDE_RULES: dict[str, Any] = {
    "can_raise_severity": True,
    "can_lower_severity": False,
    "can_raise_tier": True,
    "can_lower_tier": False,
    "can_assign_owner": True,
    "waivable_guard_severities": ("advisory", "review"),
    "non_waivable_guard_severities": ("reject",),
    "required_step_up_for_override": "loa2",
    "required_step_up_for_waiver": "loa3",
    "note": (
        "admin is the top role in this schema; there is no superuser. "
        "Distinguishing 'an admin' from 'an admin who re-authenticated for this "
        "specific action' is done with the existing step-up ladder rather than "
        "with a new role, so a widening of privilege stays a policy change in "
        "deps.py rather than a second authority model in a service."
    ),
}


# =============================================================================
# Guards
# =============================================================================
#
# The same operator vocabulary and the same three severities as the
# decision-intelligence gate tables, so a threshold reads identically across
# subservices. `reject` is the "must not happen" class; it is not waivable.
ESCALATION_GUARD_OPS: dict[str, str] = {
    "gte": "metric >= threshold",
    "gt": "metric > threshold",
    "lte": "metric <= threshold",
    "lt": "metric < threshold",
    "eq": "metric == threshold",
    "neq": "metric != threshold",
    "in": "metric is one of threshold (a list)",
    "not_in": "metric is not one of threshold (a list)",
    "is_true": "metric is truthy; threshold ignored",
    "is_false": "metric is falsy; threshold ignored",
    "is_none": "metric is missing or None",
    "present": "the metric key exists at all",
}
ESCALATION_GUARD_SEVERITIES: tuple[str, ...] = ("advisory", "review", "reject")
ESCALATION_GUARD_SEVERITY_RANK: dict[str, int] = {
    name: rank for rank, name in enumerate(ESCALATION_GUARD_SEVERITIES)
}

# `holds` means "this guard is satisfied", i.e. **no problem** -- the same
# convention `evaluate_selection_guards` in services/topics.py uses, where
# `topic_length_cap` holds when the length is *within* the cap. Six of the
# seven rows below were first written holding on the problem instead, which
# inverted the entire verdict: a healthy case reported `reject` because every
# guard had "failed". A guard written backwards is worse than no guard, because
# it makes every decision look blocked and trains reviewers to ignore the list.
#
# So each row holds when the condition is FINE and stops holding when the named
# condition is present. The `rationale` states the problem, the operator states
# the absence of it.
ESCALATION_GUARDS: list[dict[str, Any]] = [
    {
        "guard_id": "regulatory_deadline_unreachable",
        "metric": "regulatory_hours_remaining",
        "op": "gte",
        "threshold": 0,
        "severity": "reject",
        "rationale": (
            "the statutory window has already closed. Nothing downstream can "
            "reopen it, so an escalation that pretends otherwise is misleading"
        ),
        # Scoped, because the metric is absent for a non-regulatory case and a
        # missing metric fails closed -- which would reject every ordinary
        # complaint. Same idea as TOPIC_SELECTION_GUARDS' `sources` narrowing.
        "applies_when": {"regulatory": True},
    },
    {
        "guard_id": "no_owner_assigned",
        "metric": "has_owner",
        "op": "is_true",
        "threshold": True,
        "severity": "review",
        "rationale": "an unowned case is not being worked, whatever the status says",
    },
    {
        "guard_id": "severity_understated_vs_dissatisfaction",
        "metric": "severity_understated_by",
        "op": "lt",
        "threshold": 2,
        "severity": "review",
        "rationale": (
            "the recorded severity is at least two bands below what the "
            "dissatisfaction score implies; usually a case opened before the "
            "signals had accumulated"
        ),
    },
    {
        "guard_id": "override_would_weaken_policy",
        "metric": "override_would_lower_tier",
        "op": "is_false",
        "threshold": False,
        "severity": "reject",
        "rationale": (
            "an admin override may not route a case below the tier policy "
            "requires. The floor is the whole point of recording it"
        ),
    },
    {
        "guard_id": "consent_gates_contact_only",
        "metric": "consent_withholds_contact",
        "op": "is_false",
        "threshold": False,
        "severity": "advisory",
        "rationale": (
            "a consent setting is gating outbound contact. Advisory, and it "
            "must stay advisory: a customer who reported a problem is not "
            "refused a fix because of a marketing preference. This mirrors the "
            "keep-as-is decision that the consent gate never suppresses the "
            "'service' or 'recovery' purposes"
        ),
    },
    {
        "guard_id": "duplicate_open_case",
        "metric": "duplicate_open_cases",
        "op": "lt",
        "threshold": 2,
        "severity": "advisory",
        "rationale": "another open case on the same category; merge or link rather than open a third",
    },
    {
        "guard_id": "resolved_but_not_closed",
        "metric": "resolved_stale_hours",
        "op": "lt",
        "threshold": 72,
        "severity": "advisory",
        "rationale": "resolved but never closed, so the case still counts against open-case metrics",
    },
]


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _op_holds(value: Any, op: str, threshold: Any) -> bool:
    """Apply one guard operator. Fails closed on an unknown operator name."""
    if op not in ESCALATION_GUARD_OPS:
        return False
    if op == "is_true":
        return bool(value)
    if op == "is_false":
        return not value
    if op == "is_none":
        return value is None
    if op == "present":
        return value is not None
    if op in {"in", "not_in"}:
        options = threshold if isinstance(threshold, (list, tuple, set)) else [threshold]
        inside = value in options
        return inside if op == "in" else not inside
    if op == "eq":
        return value == threshold
    if op == "neq":
        return value != threshold
    number = _number(value)
    limit = _number(threshold)
    if number is None or limit is None:
        return False
    if op == "gte":
        return number >= limit
    if op == "gt":
        return number > limit
    if op == "lte":
        return number <= limit
    if op == "lt":
        return number < limit
    return False


def evaluate_escalation_guards(
    metrics: dict[str, Any],
    *,
    guards: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Evaluate the guards, strongest severity first.

    Fails closed on a *missing* metric, not just a bad one: a guard that cannot
    see its evidence must not report that it passed. Every result keeps the
    reason, the observed value and the rationale, because a guard that cannot
    explain itself gets switched off.
    """
    results: list[dict[str, Any]] = []
    for row in guards or ESCALATION_GUARDS:
        # A guard may declare a scope. Without one, a guard whose metric is only
        # populated for some cases would fail closed on the others and reject
        # them for no stated reason -- so an out-of-scope guard is skipped and
        # reported as skipped rather than failed.
        scope = row.get("applies_when")
        if scope and not rule_engine.evaluate_when(scope, metrics)[0]:
            continue
        metric = str(row.get("metric", ""))
        op = str(row.get("op", ""))
        threshold = row.get("threshold")
        observed = metric in metrics and metrics[metric] is not None
        actual = metrics.get(metric)
        if not observed:
            holds = False
            reason = "metric_missing"
        else:
            holds = _op_holds(actual, op, threshold)
            reason = "ok" if holds else "threshold_not_met"
        severity = str(row.get("severity", "advisory"))
        if severity not in ESCALATION_GUARD_SEVERITY_RANK:
            severity = "advisory"
        results.append(
            {
                "guard_id": row.get("guard_id"),
                "metric": metric,
                "op": op,
                "op_meaning": ESCALATION_GUARD_OPS.get(op, "unknown operator"),
                "threshold": threshold,
                "actual": actual,
                "observed": observed,
                "severity": severity,
                "holds": holds,
                "reason": reason,
                "rationale": row.get("rationale", ""),
                "waivable": severity in ESCALATION_OVERRIDE_RULES["waivable_guard_severities"],
            }
        )
    results.sort(
        key=lambda result: (
            -ESCALATION_GUARD_SEVERITY_RANK.get(str(result["severity"]), 0),
            str(result["guard_id"]),
        )
    )
    return results


def escalation_verdict(
    metrics: dict[str, Any],
    *,
    guards: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """``accept`` / ``review`` / ``reject`` plus the guards that decided it.

    Advisory by construction, like every other verdict in this backend: the
    decision is written, the guards are reported, and the escalation path is
    unchanged. A non-waivable guard failing is the case where that matters
    most -- the record says the escalation should not have happened, and the
    operator has to say why it did.
    """
    results = evaluate_escalation_guards(metrics, guards=guards)
    failed = [result for result in results if not result["holds"]]
    rejected = [str(r["guard_id"]) for r in failed if r["severity"] == "reject"]
    review = [str(r["guard_id"]) for r in failed if r["severity"] == "review"]
    advisory = [str(r["guard_id"]) for r in failed if r["severity"] == "advisory"]
    if rejected:
        decision = "reject"
    elif review or advisory:
        decision = "review"
    else:
        decision = "accept"
    return {
        "decision": decision,
        "rejected_by": rejected,
        "needs_review_by": review,
        "advisory_by": advisory,
        "guards": results,
        "metrics": metrics,
        "waivable_failures": [
            str(r["guard_id"]) for r in failed if r["severity"] in ("advisory", "review")
        ],
        "non_waivable_failures": rejected,
        "override_note": (
            "a reject-severity guard is not waivable; the decision may still be "
            "recorded, and the guard failure is stored with it"
        ),
    }


# =============================================================================
# The escalation decision
# =============================================================================


def build_escalation_decision(
    context: dict[str, Any],
    *,
    current_tier: Any = DEFAULT_ESCALATION_TIER,
    overrides: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The escalation verdict, shaped like ``risk_evaluator.escalation_for``.

    The contract that matters: ``escalated: False`` is a **reasoned outcome**.
    The old path had no such shape at all -- ``escalate_ticket`` was a boolean
    action that wrote nothing -- so "we decided not to escalate" and "we never
    looked" were indistinguishable in the record. Here the not-escalated case
    carries the triggers that were considered, the guards that passed, and the
    tier the case stays at.

    `overrides` is the admin input. It is applied *after* the floor, and the
    floor's refusal to allow a downgrade is reported as ``override_applied:
    False`` with the reason, rather than being silently dropped.
    """
    moment = _as_utc(now) or _now()
    selection = rule_engine.select_rules(complaint_escalation_pack(), context, now=moment)

    fired: list[dict[str, Any]] = []
    for entry in selection["fired"]:
        config = ESCALATION_TRIGGER_BY_ID.get(str(entry["id"]), {})
        params = dict(entry.get("params") or {})
        # `to_tier` is a literal name; `to_tier_rank` is the computed form the
        # expression engine can actually express. Both end up as `to_tier`.
        to_tier = str(params.get("to_tier", "") or "")
        if not to_tier and params.get("to_tier_rank") is not None:
            to_tier = tier_by_rank(params.get("to_tier_rank"))
        fired.append(
            {
                "trigger_id": str(entry["id"]),
                "label": str(config.get("label", "")),
                "kind": str(config.get("kind", "judgment")),
                "authority": str(config.get("authority", "advisory")),
                "regulatory": bool(config.get("regulatory", False)),
                "required_step_up": str(config.get("required_step_up", "")),
                "priority": int(entry.get("priority", 0)),
                "reason": str(config.get("reason", "")),
                "to_tier": to_tier,
                "owner_team": str(params.get("owner_team", "")),
                "sla_response_hours": params.get("sla_response_hours"),
                "sla_resolution_hours": params.get("sla_resolution_hours"),
                "explanation": entry.get("explanation", {}),
            }
        )

    auto_fired = [item for item in fired if item["authority"] == "auto"]
    advisory_fired = [item for item in fired if item["authority"] == "advisory"]
    regulatory = any(item["regulatory"] for item in fired) or bool(context.get("regulatory"))

    target = current_tier
    owner_team = ""
    for item in fired:
        if item["to_tier"] and tier_rank(item["to_tier"]) > tier_rank(target):
            target = item["to_tier"]
        if item["owner_team"] and not owner_team:
            owner_team = item["owner_team"]

    escalated = bool(fired) and tier_rank(target) > tier_rank(current_tier)
    # An auto trigger that does not move the tier still *is* an escalation event
    # worth recording -- it reassigned ownership. Reporting it as "not escalated"
    # would understate what the engine did.
    acted = escalated or bool(auto_fired)

    # --- the floor -----------------------------------------------------------
    override_applied = False
    override_reason = ""
    override_blocked_reason = ""
    requested_tier = str((overrides or {}).get("to_tier", "") or "")
    requested_severity = str((overrides or {}).get("severity", "") or "")
    if overrides:
        wants_lower_tier = bool(requested_tier) and tier_rank(requested_tier) < tier_rank(target)
        wants_lower_severity = bool(
            requested_severity
        ) and COMPLAINT_SEVERITY_RANK.get(requested_severity, 0) < COMPLAINT_SEVERITY_RANK.get(
            str(context.get("severity", "")), 0
        )
        if wants_lower_tier and not ESCALATION_OVERRIDE_RULES["can_lower_tier"]:
            override_blocked_reason = "an override may not route a case below the tier policy requires"
        elif wants_lower_severity and not ESCALATION_OVERRIDE_RULES["can_lower_severity"]:
            override_blocked_reason = "an override may not lower severity below the engine's assessment"
        else:
            override_applied = True
            if requested_tier and tier_rank(requested_tier) > tier_rank(target):
                target = requested_tier
            if requested_severity and COMPLAINT_SEVERITY_RANK.get(requested_severity, 0) > COMPLAINT_SEVERITY_RANK.get(
                str(context.get("severity", "")), 0
            ):
                target = highest_tier(target, requested_tier)
            if str((overrides or {}).get("owner_team", "")):
                owner_team = str(overrides["owner_team"])
            override_reason = str((overrides or {}).get("reason", ""))

    metrics = dict(context)
    metrics["override_would_lower_tier"] = bool(
        requested_tier and tier_rank(requested_tier) < tier_rank(highest_tier(target, *(i["to_tier"] for i in fired)))
    )
    metrics.setdefault("has_owner", bool(context.get("owner_user_id")))
    metrics.setdefault("regulatory_hours_remaining", None)
    metrics.setdefault("severity_understated_by", 0)
    metrics.setdefault("duplicate_open_cases", 0)
    metrics.setdefault("resolved_stale_hours", 0)
    verdict = escalation_verdict(metrics)

    recommended = max(
        fired,
        key=lambda item: (1 if item["authority"] == "auto" else 0, int(item["priority"])),
        default=None,
    )

    return {
        "generated_at": moment.isoformat(),
        "escalated": escalated,
        "acted": acted,
        "auto_applied": acted and bool(auto_fired) and escalated,
        "requires_acceptance": bool(advisory_fired) and not auto_fired,
        "from_tier": str(current_tier),
        "to_tier": str(target),
        "owner_team": owner_team,
        "regulatory": regulatory,
        "reason": (
            str(recommended["reason"]) if recommended is not None
            else "no configured trigger matched; the case stays where it is"
        ),
        "recommended_trigger_id": str(recommended["trigger_id"]) if recommended is not None else "",
        "auto_triggers": [str(item["trigger_id"]) for item in auto_fired],
        "advisory_triggers": [str(item["trigger_id"]) for item in advisory_fired],
        "triggers_fired": fired,
        "triggers_considered": selection["skipped"],
        "considered_count": len(selection["skipped"]),
        "override_applied": override_applied,
        "override_reason": override_reason,
        "override_blocked_reason": override_blocked_reason,
        "verdict": verdict["decision"],
        "guards": verdict["guards"],
        "non_waivable_failures": verdict["non_waivable_failures"],
        "override_rules": dict(ESCALATION_OVERRIDE_RULES),
    }


# =============================================================================
# Precedent retrieval (case-based reasoning)
# =============================================================================
#
# Weights must sum to 1.0; `validate_complaints` checks it. The multipliers are
# the part that makes this learning rather than lookup: a precedent whose case
# was reopened or escalated further is *worse* evidence, and the bundle says so
# rather than quietly ranking it highly because the category matched.
PRECEDENT_WEIGHTS: dict[str, float] = {
    "category": 0.30,
    "severity": 0.15,
    "factors": 0.15,
    "resolution": 0.15,
    "recency": 0.15,
    "complainant_overlap": 0.10,
}

#: How much a precedent's *observed* outcome counts for. `resolved` and `held`
#: are the outcomes that vindicate a decision; `reopened` and `complainant_left`
#: are evidence the decision was wrong.
PRECEDENT_OUTCOME_MULTIPLIER: dict[str, float] = {
    "": 0.70,
    "held": 1.00,
    "resolved": 1.00,
    "no_contact": 0.55,
    "escalated_further": 0.55,
    "reopened": 0.30,
    "complainant_left": 0.25,
}

#: Half-life for the recency term, in days. Two years: long enough that a
#: seasonal pattern is still findable, short enough that a policy changed since
#: then is not cited as though it still applied.
PRECEDENT_RECENCY_HALF_LIFE_DAYS = 730.0
PRECEDENT_MIN_SIMILARITY = 0.25
DEFAULT_PRECEDENT_LIMIT = 5


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _recency_score(closed_at: Any, *, now: datetime) -> float:
    moment = _as_utc(closed_at)
    if moment is None:
        return 0.0
    age_days = max(0.0, (now - moment).total_seconds() / 86400.0)
    return 0.5 ** (age_days / PRECEDENT_RECENCY_HALF_LIFE_DAYS)


def score_precedent(
    candidate: dict[str, Any],
    subject: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Similarity of one past case to the case being decided.

    Pure, and it takes plain dicts, so it is testable without a database and
    the ranking is reproducible from a snapshot of the inputs.
    """
    moment = _as_utc(now) or _now()
    components: dict[str, float] = {}

    components["category"] = 1.0 if candidate.get("category") == subject.get("category") else 0.0
    components["severity"] = 1.0 if candidate.get("severity") == subject.get("severity") else 0.0
    components["factors"] = _jaccard(
        {str(k) for k, v in (candidate.get("factors") or {}).items() if v},
        {str(k) for k, v in (subject.get("factors") or {}).items() if v},
    )
    subject_resolution = str(subject.get("resolution_code", "") or "")
    components["resolution"] = (
        1.0 if subject_resolution and candidate.get("resolution_code") == subject_resolution else 0.0
    )
    components["recency"] = _recency_score(candidate.get("closed_at"), now=moment)
    recency_available = _as_utc(candidate.get("closed_at")) is not None
    # Whether this is the same person matters, and is deliberately a *small*
    # weight: what a decision was for a person is evidence about that person,
    # but it is thin evidence about a new case from a different one.
    components["complainant_overlap"] = (
        1.0 if candidate.get("user_id") is not None and candidate.get("user_id") == subject.get("user_id") else 0.0
    )

    similarity = sum(
        PRECEDENT_WEIGHTS.get(name, 0.0) * float(value) for name, value in components.items()
    )
    outcome = str(candidate.get("outcome_observed", "") or "")
    multiplier = PRECEDENT_OUTCOME_MULTIPLIER.get(outcome, 0.70)
    return {
        "complaint_id": candidate.get("complaint_id"),
        "reference": str(candidate.get("reference", "")),
        "category": str(candidate.get("category", "")),
        "severity": str(candidate.get("severity", "")),
        "resolution_code": str(candidate.get("resolution_code", "") or ""),
        "resolution_note": str(candidate.get("resolution_note", "") or ""),
        "decided_by_role": str(candidate.get("decided_by_role", "") or ""),
        "decided_at": _iso(candidate.get("decided_at")),
        "closed_at": _iso(candidate.get("closed_at")),
        "same_complainant": bool(components["complainant_overlap"]),
        # Explicit, because a candidate with no close date scores 0.0 on recency
        # -- the same number a decades-old case gets. Reporting the two
        # differently is the difference between "old precedent" and "we do not
        # know how old it is".
        "recency_available": recency_available,
        "outcome_observed": outcome,
        "outcome_multiplier": multiplier,
        "components": {name: round(float(value), 4) for name, value in sorted(components.items())},
        "similarity": round(similarity, 4),
        "score": round(similarity * multiplier, 4),
        "vindicated": outcome in ("held", "resolved"),
        "contradicted": outcome in ("reopened", "complainant_left", "escalated_further"),
        "rationale": _precedent_rationale(candidate, components, outcome),
    }


def _precedent_rationale(
    candidate: dict[str, Any],
    components: dict[str, float],
    outcome: str,
) -> str:
    """One sentence saying why this precedent is or is not evidence."""
    reference = str(candidate.get("reference", "") or "a past case")
    parts: list[str] = []
    if components.get("category"):
        parts.append(f"same category ({candidate.get('category')})")
    if components.get("severity"):
        parts.append(f"same severity ({candidate.get('severity')})")
    if components.get("factors", 0.0) >= 0.5:
        parts.append("overlapping case factors")
    if components.get("complainant_overlap"):
        parts.append("the same complainant")
    matched = "; ".join(parts) if parts else "partial factor overlap"
    decision = str(candidate.get("resolution_code", "") or "no recorded resolution")
    if outcome in ("reopened", "complainant_left"):
        verdict = "This case was later reopened or the complainant left, so treat it as a warning rather than a precedent"
    elif outcome == "escalated_further":
        verdict = "This case had to escalate again, so the first decision was insufficient"
    elif outcome in ("held", "resolved"):
        verdict = "This case held or resolved and was not reopened"
    else:
        verdict = "This case never reached a terminal state, so it is weak evidence"
    return f"{reference}: {matched}. Resolved as {decision}. {verdict}."


def build_precedent_bundle(
    subject: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_PRECEDENT_LIMIT,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Rank past cases against the case being decided.

    Every candidate is scored, then filtered by ``PRECEDENT_MIN_SIMILARITY`` --
    a "precedent" with a 0.05 similarity is noise dressed as evidence. The
    bundle also reports how many candidates were *contradicted* rather than
    only how many were supportive, because a decision-support surface that
    only ever shows confirming history is not decision support.
    """
    moment = _as_utc(now) or _now()
    scored = [score_precedent(candidate, subject, now=moment) for candidate in candidates]
    scored.sort(key=lambda row: (-float(row["score"]), str(row["reference"])))
    kept = [row for row in scored if float(row["score"]) >= PRECEDENT_MIN_SIMILARITY][: max(1, int(limit))]
    contradicted = [row for row in scored if row["contradicted"]]
    return {
        "generated_at": moment.isoformat(),
        "subject_reference": str(subject.get("reference", "") or ""),
        "weights": dict(PRECEDENT_WEIGHTS),
        "min_similarity": PRECEDENT_MIN_SIMILARITY,
        "recency_half_life_days": PRECEDENT_RECENCY_HALF_LIFE_DAYS,
        "candidates_considered": len(scored),
        "returned": len(kept),
        "precedents": kept,
        "contradicted_count": len(contradicted),
        "contradicted": [
            {
                "reference": row["reference"],
                "outcome_observed": row["outcome_observed"],
                "score": row["score"],
                "rationale": row["rationale"],
            }
            for row in sorted(contradicted, key=lambda r: -float(r["score"]))[:3]
        ],
        "note": (
            "a precedent whose case was reopened or escalated further is "
            "down-weighted and reported as contradicted; it is not hidden, "
            "because 'we tried this before and it failed' is the most useful "
            "thing a precedent bundle can tell an operator"
        ),
    }


# =============================================================================
# Decision feeds
# =============================================================================
#
# The registry of sources an escalation decision may draw on. Two fields do the
# real work: `required` (a missing required feed is reported as a gap, not
# quietly omitted) and `async_only` (external feeds must never sit on the
# request path -- the same rule `rule_engine.EXTERNAL_OPS` already enforces).
DECISION_FEEDS: list[dict[str, Any]] = [
    {
        "feed_id": "case_facts",
        "label": "Case facts",
        "kind": "internal",
        "source": "complaint_cases + complaint_events",
        "required": True,
        "async_only": False,
        "description": "The case's own state: status, age, owner, SLA clock, history depth.",
    },
    {
        "feed_id": "conversation",
        "label": "Conversation evidence",
        "kind": "internal",
        "source": "chat_analytics dissatisfaction + recovery signals",
        "required": True,
        "async_only": False,
        "description": "What the customer actually said, and how dissatisfied the signals say they are.",
    },
    {
        "feed_id": "relationship",
        "label": "Relationship context",
        "kind": "internal",
        "source": "retention snapshots + loyalty journey",
        "required": True,
        "async_only": False,
        "description": "Churn risk, loyalty score, value tier, lifecycle stage, tenure.",
    },
    {
        "feed_id": "policy_posture",
        "label": "Policy posture",
        "kind": "policy",
        "source": "policy_scoring snapshot",
        "required": True,
        "async_only": False,
        "description": "What we are permitted to offer. This bounds every remedy in the decision.",
    },
    {
        "feed_id": "commercial",
        "label": "Commercial exposure",
        "kind": "internal",
        "source": "arrears + points ledger",
        "required": False,
        "async_only": False,
        "description": "Outstanding balance and points at risk -- the save-or-write-off arithmetic.",
    },
    {
        "feed_id": "journey",
        "label": "Journey context",
        "kind": "internal",
        "source": "loyalty_journey + activity_tree",
        "required": False,
        "async_only": False,
        "description": "Where the customer is in the journey, and any behavioural anomaly.",
    },
    {
        "feed_id": "historical_decisions",
        "label": "Historical operator decisions",
        "kind": "historical",
        "source": "complaint_decisions + closed complaint_cases",
        "required": True,
        "async_only": False,
        "description": (
            "What previous operators decided on comparable cases, and what "
            "actually happened next. The only feed that is the organization's "
            "own memory rather than a description of the customer."
        ),
    },
    {
        "feed_id": "external",
        "label": "External signals",
        "kind": "external",
        "source": "app.enrichment + webhooks",
        "required": False,
        "async_only": True,
        "description": (
            "Optional outbound signals: provider status, disruption notices, "
            "regulatory watchlists. Background only -- the sync request path "
            "must never block on a third party."
        ),
    },
    {
        "feed_id": "communication_constraints",
        "label": "Communication constraints",
        "kind": "policy",
        "source": "communication_strategy + preferences + i18n",
        "required": False,
        "async_only": False,
        "description": "Locale, channel preference, contact window, and what consent permits.",
    },
    {
        "feed_id": "governance",
        "label": "Governance state",
        "kind": "policy",
        "source": "escalation guards + audit integrity",
        "required": True,
        "async_only": False,
        "description": "Which guards passed, which are blocking, and whether the audit chain is intact.",
    },
]
DECISION_FEED_BY_ID: dict[str, dict[str, Any]] = {
    str(row["feed_id"]): dict(row) for row in DECISION_FEEDS
}
DECISION_FEED_KINDS: tuple[str, ...] = ("internal", "historical", "policy", "external")

#: Above this age a feed is called out as stale. A dossier that presents
#: week-old relationship data as current is worse than one that admits it.
DECISION_FEED_STALE_AFTER_SECONDS: dict[str, int] = {
    "case_facts": 300,
    "conversation": 3600,
    "relationship": 86400,
    "policy_posture": 86400,
    "commercial": 3600,
    "journey": 86400,
    "historical_decisions": 0,
    "external": 300,
    "communication_constraints": 86400,
    "governance": 300,
}


def _feed_block(
    feed_id: str,
    *,
    available: bool,
    observed: Any = None,
    confidence: float = 1.0,
    citation: str = "",
    observed_at: Optional[datetime] = None,
    gap: str = "",
    note: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One evidence block, with its own provenance and staleness."""
    config = DECISION_FEED_BY_ID.get(feed_id, {})
    moment = _as_utc(now) or _now()
    stamp = _as_utc(observed_at)
    age_seconds = None if stamp is None else max(0.0, (moment - stamp).total_seconds())
    stale_after = DECISION_FEED_STALE_AFTER_SECONDS.get(feed_id)
    stale = None if age_seconds is None or stale_after is None else age_seconds > stale_after
    return {
        "feed_id": feed_id,
        "label": str(config.get("label", feed_id)),
        "kind": str(config.get("kind", "internal")),
        "source": str(config.get("source", "")),
        "required": bool(config.get("required", False)),
        "async_only": bool(config.get("async_only", False)),
        "available": bool(available),
        "observed": observed,
        "confidence": round(float(confidence), 3),
        "citation": citation,
        "observed_at": _iso(observed_at),
        "age_seconds": None if age_seconds is None else round(age_seconds, 1),
        "stale": stale,
        "gap": gap,
        "note": note or str(config.get("description", "")),
    }


def build_decision_dossier(
    feeds: dict[str, dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Assemble the evidence blocks, and say plainly what is missing.

    `feeds` maps ``feed_id`` to the keyword arguments of :func:`_feed_block`.
    A declared feed with no block is reported as a gap rather than dropped:
    a decision-support surface that silently omits the historical-decisions
    feed reads identically to one where there were no prior decisions, and
    those are very different situations.
    """
    moment = _as_utc(now) or _now()
    blocks = [
        _feed_block(feed_id, now=moment, **payload) for feed_id, payload in sorted(feeds.items())
    ]
    declared = [str(row["feed_id"]) for row in DECISION_FEEDS]
    present = {block["feed_id"] for block in blocks}
    missing_required = [
        feed_id
        for feed_id in declared
        if bool(DECISION_FEED_BY_ID.get(feed_id, {}).get("required")) and feed_id not in present
    ]
    unavailable_required = [
        block["feed_id"] for block in blocks if block["required"] and not block["available"]
    ]
    stale_feeds = [block["feed_id"] for block in blocks if block["stale"]]
    return {
        "generated_at": moment.isoformat(),
        "feeds": blocks,
        "declared_feeds": declared,
        "present_feeds": sorted(present),
        "missing_required_feeds": missing_required,
        "unavailable_required_feeds": unavailable_required,
        "stale_feeds": stale_feeds,
        "complete": not missing_required and not unavailable_required,
        "confidence": _dossier_confidence(blocks),
        "by_kind": _dossier_by_kind(blocks),
        "note": (
            "each block carries its own source, confidence and staleness; a "
            "required feed that could not be built is named rather than "
            "omitted, because an absent feed and an empty one are different "
            "claims about the world"
        ),
    }


def _dossier_confidence(blocks: list[dict[str, Any]]) -> float:
    """The lowest-confidence weighted block, with missing required feeds as zero.

    Deliberately a *minimum* rather than an average. Averaging lets a dozen
    confident internal feeds hide the one external feed that failed, which is
    exactly the case where the decision is shakiest.
    """
    if not blocks:
        return 0.0
    required = [block for block in blocks if block["required"]]
    pool = required or blocks
    worst = min(float(block["confidence"]) for block in pool)
    return round(min(1.0, worst), 3)


def _dossier_by_kind(blocks: list[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {kind: [] for kind in DECISION_FEED_KINDS}
    for block in blocks:
        grouped.setdefault(str(block["kind"]), []).append(str(block["feed_id"]))
    return grouped


# =============================================================================
# Validation and catalog
# =============================================================================


def validate_complaints() -> dict[str, Any]:
    """Cross-check the tables against each other and against ``models``.

    ``errors`` means a configured rule cannot run. ``warnings`` means something
    is configured but is not doing anything -- the failure mode that produced
    two dead rules in this codebase before (`suppress_recent_complaint`
    needed a context key nobody emitted, and `sensitive_complaint` matched
    topic strings `build_summary` cannot produce). Both are worth failing a
    test over, which is why this exists as a function rather than a comment.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    # --- auto-escalation may only be as wide as the policy allows -------------
    for row in ESCALATION_TRIGGERS:
        if str(row.get("authority")) != "auto":
            continue
        kind = str(row.get("kind", ""))
        if kind not in AUTO_ESCALATION_TRIGGER_KINDS:
            errors.append(
                {
                    "code": "auto_authority_widened",
                    "trigger_id": str(row.get("trigger_id")),
                    "detail": (
                        f"authority 'auto' is only permitted for "
                        f"{list(AUTO_ESCALATION_TRIGGER_KINDS)}; '{kind}' depends on "
                        "judgment and must be advisory"
                    ),
                }
            )
        if not str(row.get("reason", "")).strip():
            warnings.append(
                {
                    "code": "trigger_without_reason",
                    "trigger_id": str(row.get("trigger_id")),
                    "detail": "an auto-applied trigger with no stated reason is unreviewable",
                }
            )

    for row in ESCALATION_TRIGGERS:
        if str(row.get("authority", "")) not in ESCALATION_AUTHORITIES:
            errors.append(
                {
                    "code": "unknown_authority",
                    "trigger_id": str(row.get("trigger_id")),
                    "detail": f"authority must be one of {list(ESCALATION_AUTHORITIES)}",
                }
            )
        validation = rule_engine.validate_when(row.get("when", {}))
        if not validation.get("valid"):
            errors.append(
                {
                    "code": "invalid_when",
                    "trigger_id": str(row.get("trigger_id")),
                    "detail": "; ".join(str(err) for err in validation.get("errors", [])),
                }
            )
        for target in (row.get("params", {}) or {}).values():
            if isinstance(target, str) and target.startswith("="):
                try:
                    # Strip the "=" first, exactly as rule_engine._resolve_param
                    # does, so this check parses the same text the engine will.
                    # The scope is the produced-key set rather than an empty
                    # dict, which means an expression naming a context key
                    # nobody emits is caught here -- the same dead-config
                    # failure the `when`-clause check below looks for.
                    rule_engine.evaluate_expression(target[1:], _probe_scope())
                except ValueError as exc:
                    errors.append(
                        {
                            "code": "invalid_params_expression",
                            "trigger_id": str(row.get("trigger_id")),
                            "detail": str(exc),
                        }
                    )
                break

    # --- tiers referenced must exist -----------------------------------------
    for row in ESCALATION_TRIGGERS:
        for tier_key in ("to_tier",):
            value = str((row.get("params", {}) or {}).get(tier_key, "") or "")
            # A "=" prefix is a computed expression, resolved per case at
            # selection time; its value is not knowable here and the pack's own
            # `params` are what an operator reads. Skip it rather than
            # reporting a tier name as unknown.
            if value.startswith("="):
                continue
            if value and value not in ESCALATION_TIER_BY_NAME:
                errors.append(
                    {
                        "code": "unknown_tier",
                        "trigger_id": str(row.get("trigger_id")),
                        "detail": f"to_tier '{value}' is not in ESCALATION_TIERS",
                    }
                )
        # A computed rank must land inside the tier table, or `tier_by_rank`
        # silently falls back to tier_1 -- which is exactly the kind of
        # quietly-inert configuration this validator exists to catch.
        rank_expr = (row.get("params", {}) or {}).get("to_tier_rank")
        if isinstance(rank_expr, str) and rank_expr.startswith("="):
            valid_ranks = {int(t["rank"]) for t in ESCALATION_TIERS}
            for probe in range(0, len(COMPLAINT_SEVERITIES) + 2):
                probe_ctx = _probe_scope(severity_rank=probe)
                try:
                    resolved = rule_engine.evaluate_expression(rank_expr[1:], probe_ctx)
                except (ValueError, TypeError):
                    break  # already reported by the expression check above
                if int(resolved) not in valid_ranks:
                    errors.append(
                        {
                            "code": "tier_rank_out_of_range",
                            "trigger_id": str(row.get("trigger_id")),
                            "detail": (
                                f"to_tier_rank resolves to {resolved} at severity_rank "
                                f"{probe}, which is not a tier in ESCALATION_TIERS"
                            ),
                        }
                    )
                    break
    for row in ROUTING_RULES:
        value = str(row.get("to_tier", ""))
        if value and value not in ESCALATION_TIER_BY_NAME:
            errors.append({"code": "unknown_tier", "rule_id": str(row.get("rule_id")), "detail": f"to_tier '{value}' is not in ESCALATION_TIERS"})

    # --- SLA matrix must cover every severity ---------------------------------
    for severity in COMPLAINT_SEVERITIES:
        if severity not in SLA_BY_SEVERITY:
            errors.append(
                {
                    "code": "sla_missing",
                    "detail": f"severity '{severity}' has no SLA row, so its cases fall back to DEFAULT_SLA",
                }
            )
    for row in SLA_MATRIX:
        if str(row["severity"]) not in COMPLAINT_SEVERITIES:
            errors.append({"code": "sla_unknown_severity", "detail": str(row["severity"])})

    # --- precedent weights must be a distribution -----------------------------
    total = sum(PRECEDENT_WEIGHTS.values())
    if abs(total - 1.0) > 1e-9:
        errors.append(
            {
                "code": "precedent_weights_not_normalised",
                "detail": f"PRECEDENT_WEIGHTS sums to {total:.4f}, not 1.0; a 'similarity' that can exceed 1 is not a similarity",
            }
        )
    for outcome in COMPLAINT_OUTCOMES_OBSERVED:
        if outcome and outcome not in PRECEDENT_OUTCOME_MULTIPLIER:
            warnings.append(
                {
                    "code": "precedent_outcome_unweighted",
                    "detail": f"outcome '{outcome}' has no multiplier, so it defaults to 0.70",
                }
            )

    # --- guards must be well-formed ------------------------------------------
    for row in ESCALATION_GUARDS:
        if str(row.get("op")) not in ESCALATION_GUARD_OPS:
            errors.append({"code": "unknown_guard_op", "guard_id": str(row.get("guard_id")), "detail": str(row.get("op"))})
        if str(row.get("severity")) not in ESCALATION_GUARD_SEVERITIES:
            errors.append({"code": "unknown_guard_severity", "guard_id": str(row.get("guard_id")), "detail": str(row.get("severity"))})
        scope = row.get("applies_when")
        if scope:
            check = rule_engine.validate_when(scope)
            if not check.get("valid"):
                errors.append(
                    {
                        "code": "invalid_guard_scope",
                        "guard_id": str(row.get("guard_id")),
                        "detail": "; ".join(str(err) for err in check.get("errors", [])),
                    }
                )
    non_waivable = set(ESCALATION_OVERRIDE_RULES["non_waivable_guard_severities"])
    waivable = set(ESCALATION_OVERRIDE_RULES["waivable_guard_severities"])
    if non_waivable & waivable:
        errors.append({"code": "override_severity_overlap", "detail": "a guard severity is both waivable and not"})
    for guard_id in ("override_would_weaken_policy", "regulatory_deadline_unreachable"):
        row = next((g for g in ESCALATION_GUARDS if g["guard_id"] == guard_id), None)
        if row is None:
            errors.append({"code": "guard_missing", "detail": guard_id})
        elif str(row.get("severity")) in waivable:
            errors.append(
                {
                    "code": "floor_guard_waivable",
                    "detail": f"{guard_id} is the authority floor and must not be waivable",
                }
            )

    # --- cross-check against the schema's declared vocabularies ---------------
    # models.py may not import services, so this vocabulary is stated twice. This
    # is the check that keeps the duplication honest.
    for table, column, expected in (
        ("complaint_cases", "status", COMPLAINT_STATUSES),
        ("complaint_cases", "severity", COMPLAINT_SEVERITIES),
        ("complaint_cases", "tier", ESCALATION_TIER_ORDER),
        ("complaint_cases", "category", tuple(COMPLAINT_CATEGORY_BY_NAME)),
        ("complaint_cases", "resolution_code", COMPLAINT_RESOLUTION_CODES),
        ("complaint_events", "event_type", COMPLAINT_EVENT_TYPES),
        ("complaint_decisions", "decision", COMPLAINT_DECISIONS),
        ("complaint_decisions", "outcome", COMPLAINT_DECISION_OUTCOMES),
        ("complaint_decisions", "outcome_observed", COMPLAINT_OUTCOMES_OBSERVED),
    ):
        declared = set(models.enum_values_for(table, column))
        drift = declared.symmetric_difference(set(expected))
        if drift:
            errors.append(
                {
                    "code": "vocabulary_drift",
                    "detail": f"{table}.{column}: models declares {sorted(declared)}, service declares {sorted(expected)}; only in one: {sorted(drift)}",
                }
            )

    # --- dead-config detection ------------------------------------------------
    produced = _produced_context_keys()
    referenced: dict[str, list[str]] = {}
    for row in ESCALATION_TRIGGERS:
        for field in rule_engine.validate_when(row.get("when", {})).get("fields_referenced", []) or []:
            referenced.setdefault(str(field), []).append(str(row.get("trigger_id")))
    for field, trigger_ids in sorted(referenced.items()):
        if field.startswith("_") or field in produced:
            continue
        warnings.append(
            {
                "code": "context_key_never_produced",
                "detail": (
                    f"triggers {trigger_ids} test '{field}', which no producer emits, "
                    "so they can never fire"
                ),
            }
        )

    return {
        "version": COMPLAINTS_CATALOG_VERSION,
        "generated_at": _now().isoformat(),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "counts": {
            "categories": len(COMPLAINT_CATEGORIES),
            "tiers": len(ESCALATION_TIERS),
            "triggers": len(ESCALATION_TRIGGERS),
            "auto_triggers": sum(1 for row in ESCALATION_TRIGGERS if row.get("authority") == "auto"),
            "advisory_triggers": sum(1 for row in ESCALATION_TRIGGERS if row.get("authority") == "advisory"),
            "routing_rules": len(ROUTING_RULES),
            "guards": len(ESCALATION_GUARDS),
            "feeds": len(DECISION_FEEDS),
            "sla_rows": len(SLA_MATRIX),
        },
        "checked": [
            "auto-authority is confined to the permitted trigger kinds",
            "every trigger's when-clause validates",
            "every params expression parses",
            "every referenced tier exists",
            "every severity has an SLA row",
            "precedent weights sum to 1.0",
            "guard operators and severities are known",
            "the authority-floor guards are not waivable",
            "service vocabularies match the schema's declared ones",
            "no trigger tests a context key no producer emits",
        ],
    }


def _probe_scope(**overrides: Any) -> dict[str, Any]:
    """A context carrying every produced key at a plausible value.

    Used to check that a ``=`` expression both parses *and* evaluates. Parsing
    alone is not enough: ``=2 if severity_rank >= 2 else 1`` parses perfectly
    and then raises ``TypeError`` at selection time if nobody ever populates
    ``severity_rank``. Validating against real values is what turns a latent
    crash in the auto-escalation path into a startup-time error.
    """
    scope: dict[str, Any] = {
        "category": "service_quality",
        "severity": "medium",
        "severity_rank": 1,
        "status": "open",
        "tier": "tier_1",
        "age_hours": 2.0,
        "response_overdue_hours": 0.0,
        "resolution_overdue_hours": 0.0,
        "regulatory_deadline_hours": 720.0,
        "regulatory_hours_remaining": 720.0,
        "regulatory": False,
        "resolved": False,
        "no_owner": False,
        "has_owner": True,
        "open_complaints": 0,
        "total_complaints": 0,
        "complaints_last_30d": 0,
        "reopened_count": 0,
        "duplicate_open_cases": 0,
        "dissatisfaction_score": 0.0,
        "churn_risk": "low",
        "loyalty_score": 0.0,
        "value_tier": "standard",
        "lifecycle_stage": "new",
        "policy_tier": "standard",
        "control_posture": "observed",
        "arrears_total": 0.0,
        "points_balance": 0.0,
        "points_at_risk": 0.0,
        "pending_bookings": 0,
        "cancelled_bookings": 0,
        "locale": "global",
        "consent_withholds_contact": False,
        "satisfaction_score": None,
        "resolution_code": "",
        "resolved_stale_hours": 0.0,
        "severity_understated_by": 0,
        "override_would_lower_tier": False,
        "owner_user_id": None,
    }
    # Guard against drift: a key added to the produced set but not to the probe
    # would make the expression check silently weaker.
    missing = _produced_context_keys() - set(scope)
    if missing:  # pragma: no cover - defensive
        scope.update({key: None for key in sorted(missing)})
    scope.update(overrides)
    return scope


def _produced_context_keys() -> set[str]:
    """The context keys a complaint decision actually carries.

    Written out rather than derived, because the derivation would be a
    tautology -- reading the keys back out of the table that consumes them
    proves nothing. This list is the independent statement, and the warning in
    `validate_complaints` compares the two.
    """
    return {
        "category",
        "severity",
        "severity_rank",
        "status",
        "tier",
        "age_hours",
        "response_overdue_hours",
        "resolution_overdue_hours",
        "regulatory_deadline_hours",
        "regulatory_hours_remaining",
        "regulatory",
        "resolved",
        "no_owner",
        "has_owner",
        "open_complaints",
        "total_complaints",
        "complaints_last_30d",
        "reopened_count",
        "duplicate_open_cases",
        "dissatisfaction_score",
        "churn_risk",
        "loyalty_score",
        "value_tier",
        "lifecycle_stage",
        "policy_tier",
        "control_posture",
        "arrears_total",
        "points_balance",
        "points_at_risk",
        "pending_bookings",
        "cancelled_bookings",
        "locale",
        "consent_withholds_contact",
        "satisfaction_score",
        "resolution_code",
        "resolved_stale_hours",
        "severity_understated_by",
        "override_would_lower_tier",
        "owner_user_id",
    }


def build_complaints_catalog() -> dict[str, Any]:
    """The introspection payload: tables, vocabularies, and how to read them."""
    return {
        "version": COMPLAINTS_CATALOG_VERSION,
        "escalation_pack_version": COMPLAINT_ESCALATION_PACK_VERSION,
        "generated_at": _now().isoformat(),
        "statuses": list(COMPLAINT_STATUSES),
        "terminal_statuses": list(COMPLAINT_TERMINAL_STATUSES),
        "resolved_statuses": list(COMPLAINT_RESOLVED_STATUSES),
        "severities": list(COMPLAINT_SEVERITIES),
        "severity_rank": dict(COMPLAINT_SEVERITY_RANK),
        "categories": [dict(row) for row in COMPLAINT_CATEGORIES],
        "default_category": DEFAULT_COMPLAINT_CATEGORY,
        "resolution_codes": list(COMPLAINT_RESOLUTION_CODES),
        "event_types": list(COMPLAINT_EVENT_TYPES),
        "decisions": list(COMPLAINT_DECISIONS),
        "decision_outcomes": list(COMPLAINT_DECISION_OUTCOMES),
        "outcomes_observed": list(COMPLAINT_OUTCOMES_OBSERVED),
        "tiers": [dict(row) for row in ESCALATION_TIERS],
        "tier_order": list(ESCALATION_TIER_ORDER),
        "authorities": {
            "values": list(ESCALATION_AUTHORITIES),
            "auto_permitted_kinds": list(AUTO_ESCALATION_TRIGGER_KINDS),
            "note": (
                "only a statutory deadline or a breached SLA may act without a "
                "human; everything that needs judgment is advisory. This is "
                "enforced by validate_complaints, not by convention"
            ),
        },
        "override_rules": dict(ESCALATION_OVERRIDE_RULES),
        "triggers": [dict(row) for row in ESCALATION_TRIGGERS],
        "escalation_pack": complaint_escalation_pack(),
        "routing_rules": [dict(row) for row in ROUTING_RULES],
        "default_route": dict(DEFAULT_ROUTE),
        "sla_matrix": [dict(row) for row in SLA_MATRIX],
        "regulatory_sla": dict(REGULATORY_SLA),
        "guards": [dict(row) for row in ESCALATION_GUARDS],
        "guard_ops": dict(ESCALATION_GUARD_OPS),
        "guard_polarity": (
            "`holds` means the guard is satisfied, i.e. no problem present. A "
            "guard stops holding when the condition named in its rationale is "
            "true. A guard with an `applies_when` scope is skipped, not failed, "
            "when the scope does not match"
        ),
        "guard_severities": list(ESCALATION_GUARD_SEVERITIES),
        "guard_severity_meaning": {
            "advisory": "reported; a human may know something the feed does not",
            "review": "a human should look at this before acting knowingly",
            "reject": "should not happen; not waivable, and recorded if it does",
        },
        "feeds": [dict(row) for row in DECISION_FEEDS],
        "feed_kinds": list(DECISION_FEED_KINDS),
        "feed_stale_after_seconds": dict(DECISION_FEED_STALE_AFTER_SECONDS),
        "precedent": {
            "weights": dict(PRECEDENT_WEIGHTS),
            "outcome_multiplier": dict(PRECEDENT_OUTCOME_MULTIPLIER),
            "min_similarity": PRECEDENT_MIN_SIMILARITY,
            "recency_half_life_days": PRECEDENT_RECENCY_HALF_LIFE_DAYS,
            "default_limit": DEFAULT_PRECEDENT_LIMIT,
        },
        "auto_escalation_enabled": COMPLAINT_AUTO_ESCALATION_ENABLED,
        "validation": validate_complaints(),
        "note": (
            "config, not code: adding a category, tier, trigger, routing rule, "
            "guard or feed is a data change. The one thing that is deliberately "
            "not free-form is auto-escalation authority, which validate_complaints "
            "refuses to widen"
        ),
    }


# =============================================================================
# Persistence
# =============================================================================
#
# Two rules govern everything below.
#
# 1. **A read never writes.** The old recovery path had `GET /chat/recovery`
#    inserting a `RecoveryOutcome` on every hit, so "attempts" inflated with
#    traffic and `acknowledged` was derived from a score band rather than set by
#    anyone. Here the report endpoints are genuinely read-only, and the only
#    functions that write are the explicit transitions.
#
# 2. **Every transition writes an event.** Not a best-effort audit: the
#    `complaint_events` row is part of the same transaction as the case update,
#    so a case can never be in a state its timeline does not explain.


def _provisional_reference() -> str:
    """A unique placeholder for the pre-flush reference.

    `reference` is NOT NULL and unique, so something must be present before the
    row exists. A uuid is collision-free across concurrent transactions, and it
    is replaced by the durable id-derived reference before the commit, so the
    provisional value is never observable outside this transaction.
    """
    return f"CMP-P-{uuid.uuid4().hex[:12].upper()}"


def _final_reference(case_id: Any) -> str:
    """The durable reference, derived from the primary key.

    Monotonic and stable, which is exactly what the old
    ``ESC-{user_id}-{run_seq:03d}`` was not: that string came from an
    in-process counter, so a restart reissued ``ESC-42-001`` and a customer
    holding the old reference reached the wrong case.
    """
    return f"CMP-{int(case_id):06d}"


def reference_format() -> dict[str, Any]:
    return {
        "pattern": "CMP-{id:06d}",
        "provisional_pattern": "CMP-P-{uuid12}",
        "note": (
            "the reference is the case's primary key rendered, so it is unique, "
            "monotonic and stable across restarts. A customer quoting it reaches "
            "exactly one case"
        ),
    }


def case_to_dict(case: Any) -> dict[str, Any]:
    """A case row as a plain dict, with JSON columns decoded.

    Read-only and total: a half-populated row yields a dict with the fields it
    has rather than raising, because this runs on report paths.
    """
    def value(name: str, default: Any = None) -> Any:
        return getattr(case, name, default) if case is not None else default

    return {
        "id": value("id"),
        "user_id": value("user_id"),
        "reference": str(value("reference", "") or ""),
        "category": str(value("category", DEFAULT_COMPLAINT_CATEGORY)),
        "severity": str(value("severity", "medium")),
        "status": str(value("status", "open")),
        "tier": str(value("tier", DEFAULT_ESCALATION_TIER)),
        "owner_user_id": value("owner_user_id"),
        "owner_team": str(value("owner_team", "") or ""),
        "opened_at": _iso(value("opened_at")),
        "acknowledged_at": _iso(value("acknowledged_at")),
        "first_response_at": _iso(value("first_response_at")),
        "escalated_at": _iso(value("escalated_at")),
        "resolved_at": _iso(value("resolved_at")),
        "closed_at": _iso(value("closed_at")),
        "response_due_at": _iso(value("response_due_at")),
        "resolution_due_at": _iso(value("resolution_due_at")),
        "escalation_trigger_id": str(value("escalation_trigger_id", "") or ""),
        "auto_escalated": bool(value("auto_escalated", False)),
        "regulatory": bool(value("regulatory", False)),
        "resolution_code": str(value("resolution_code", "") or ""),
        "resolution_note": str(value("resolution_note", "") or ""),
        "satisfaction_score": value("satisfaction_score"),
        "reopened_count": int(value("reopened_count", 0) or 0),
        "factors": _loads(value("factors_json", "{}"), "object"),
        "summary": str(value("summary", "") or ""),
        "source": str(value("source", "chat") or "chat"),
        "created_at": _iso(value("created_at")),
        "updated_at": _iso(value("updated_at")),
    }


def event_to_dict(event: Any) -> dict[str, Any]:
    return {
        "id": getattr(event, "id", None),
        "complaint_id": getattr(event, "complaint_id", None),
        "event_type": str(getattr(event, "event_type", "") or ""),
        "from_status": str(getattr(event, "from_status", "") or ""),
        "to_status": str(getattr(event, "to_status", "") or ""),
        "from_tier": str(getattr(event, "from_tier", "") or ""),
        "to_tier": str(getattr(event, "to_tier", "") or ""),
        "actor_user_id": getattr(event, "actor_user_id", None),
        "actor_role": str(getattr(event, "actor_role", "") or ""),
        "detail": _loads(getattr(event, "detail_json", "{}"), "object"),
        "note": str(getattr(event, "note", "") or ""),
        "occurred_at": _iso(getattr(event, "created_at", None)),
    }


def decision_to_dict(decision: Any) -> dict[str, Any]:
    return {
        "id": getattr(decision, "id", None),
        "complaint_id": getattr(decision, "complaint_id", None),
        "decision": str(getattr(decision, "decision", "") or ""),
        "outcome": str(getattr(decision, "outcome", "") or ""),
        "from_tier": str(getattr(decision, "from_tier", "") or ""),
        "to_tier": str(getattr(decision, "to_tier", "") or ""),
        "decided_by_id": getattr(decision, "decided_by_id", None),
        "decided_by_role": str(getattr(decision, "decided_by_role", "") or ""),
        "step_up_level": str(getattr(decision, "step_up_level", "") or ""),
        "rationale": str(getattr(decision, "rationale", "") or ""),
        "factors": _loads(getattr(decision, "factors_json", "{}"), "object"),
        "precedent_refs": _loads(getattr(decision, "precedent_refs_json", "[]"), "array"),
        "auto_applied": bool(getattr(decision, "auto_applied", False)),
        "superseded_by_id": getattr(decision, "superseded_by_id", None),
        "outcome_observed": str(getattr(decision, "outcome_observed", "") or ""),
        "outcome_recorded_at": _iso(getattr(decision, "outcome_recorded_at", None)),
        "decided_at": _iso(getattr(decision, "created_at", None)),
    }


# =============================================================================
# Context assembly
# =============================================================================
#
# This is where the multi-source claim is either honoured or not. One pass
# over the case's own row plus every live source, producing two things
# together: the flat `when`-DSL context the triggers read, and the feed blocks
# the dossier shows. They are built together deliberately -- a context key with
# no corresponding feed is an unexplained number, and a feed with no
# corresponding key is evidence the decision cannot actually use.


async def _first_row(db: AsyncSession, statement: Any) -> Any:
    result = await db.execute(statement)
    return result.scalars().first()


async def _count(db: AsyncSession, statement: Any) -> int:
    result = await db.execute(statement)
    row = result.first()
    return int(row[0]) if row and row[0] is not None else 0


async def collect_complaint_context(
    db: AsyncSession,
    case: Any,
    *,
    now: Optional[datetime] = None,
    locale: str = "global",
) -> dict[str, Any]:
    """Assemble the trigger context and the decision-feed blocks for one case.

    Every source is read through a helper that catches its own failure, so a
    missing retention snapshot degrades that one feed rather than raising out of
    an endpoint. The ``gaps`` list is returned precisely so that degradation is
    reported instead of hidden: a decision taken on three of ten feeds is a
    different decision from one taken on ten of ten, and the record should say
    which it was.
    """
    # Every source below is read inside a `SQLAlchemyError` handler so one
    # unreachable table degrades that feed instead of the whole dossier. The
    # handler is deliberately narrow. It was `except Exception` first, and that
    # is how a missing `func` import in recovery_playbooks shipped unnoticed:
    # the NameError was swallowed, the context key it was supposed to produce
    # silently never appeared, and the rule depending on it stayed dead while
    # the code read as if it were supplying the key. A database fault is a
    # condition to report; a typo is a bug to raise.
    moment = _as_utc(now) or _now()
    payload = case_to_dict(case)
    user_id = payload.get("user_id")
    feeds: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []

    # --- case facts -----------------------------------------------------------
    opened = _as_utc(payload.get("opened_at")) or moment
    age_hours = max(0.0, (moment - opened).total_seconds() / 3600.0)
    response_due = _as_utc(payload.get("response_due_at"))
    resolution_due = _as_utc(payload.get("resolution_due_at"))
    responded = _as_utc(payload.get("first_response_at"))
    resolved = _as_utc(payload.get("resolved_at"))

    response_overdue = 0.0
    if response_due is not None and responded is None:
        response_overdue = max(0.0, (moment - response_due).total_seconds() / 3600.0)
    resolution_overdue = 0.0
    if resolution_due is not None and resolved is None:
        resolution_overdue = max(0.0, (moment - resolution_due).total_seconds() / 3600.0)

    regulatory = bool(payload.get("regulatory"))
    statutory_hours_remaining: Optional[float] = None
    regulatory_deadline_hours = float(REGULATORY_SLA["resolution_hours"])
    if regulatory:
        # Measured from open, not from when we noticed. Using the notice time
        # would hand ourselves back however long we took to look.
        elapsed = max(0.0, (moment - opened).total_seconds() / 3600.0)
        statutory_hours_remaining = max(0.0, regulatory_deadline_hours - elapsed)
        regulatory_deadline_hours = statutory_hours_remaining

    resolved_stale = 0.0
    if str(payload.get("status")) in COMPLAINT_RESOLVED_STATUSES and resolved is not None:
        resolved_stale = max(0.0, (moment - resolved).total_seconds() / 3600.0)

    feeds["case_facts"] = {
        "available": True,
        "observed": {
            "reference": payload["reference"],
            "status": payload["status"],
            "age_hours": round(age_hours, 2),
            "reopened_count": payload["reopened_count"],
            "has_owner": bool(payload.get("owner_user_id")) or bool(payload.get("owner_team")),
            "sla_response_overdue_hours": round(response_overdue, 2),
            "sla_resolution_overdue_hours": round(resolution_overdue, 2),
        },
        "confidence": 1.0,
        "citation": f"complaint_cases:{payload['reference']}",
        "observed_at": _as_utc(payload.get("updated_at")) or opened,
    }

    # --- complaint history (and the key the dead rule needed) ----------------
    complaints_last_30d = 0
    open_complaints = 0
    total_complaints = 0
    duplicate_open_cases = 0
    try:
        cutoff = moment - timedelta(days=30)
        complaints_last_30d = await _count(
            db,
            select(func.count(models.ComplaintCase.id)).where(
                models.ComplaintCase.user_id == user_id,
                models.ComplaintCase.opened_at >= cutoff,
            ),
        )
        total_complaints = await _count(
            db,
            select(func.count(models.ComplaintCase.id)).where(
                models.ComplaintCase.user_id == user_id
            ),
        )
        open_complaints = await _count(
            db,
            select(func.count(models.ComplaintCase.id)).where(
                models.ComplaintCase.user_id == user_id,
                ~models.ComplaintCase.status.in_(COMPLAINT_TERMINAL_STATUSES + COMPLAINT_RESOLVED_STATUSES),
            ),
        )
        duplicate_open_cases = await _count(
            db,
            select(func.count(models.ComplaintCase.id)).where(
                models.ComplaintCase.user_id == user_id,
                models.ComplaintCase.category == payload["category"],
                models.ComplaintCase.id != payload.get("id"),
                ~models.ComplaintCase.status.in_(COMPLAINT_TERMINAL_STATUSES + COMPLAINT_RESOLVED_STATUSES),
            ),
        )
        feeds["historical_decisions"] = {
            "available": True,
            "observed": {
                "complaints_last_30d": complaints_last_30d,
                "open_complaints": open_complaints,
                "total_complaints": total_complaints,
                "duplicate_open_cases": duplicate_open_cases,
            },
            "confidence": 1.0,
            "citation": "complaint_cases (counted for this user)",
            "observed_at": moment,
            "note": "volume of prior complaints; the precedent bundle adds what was decided",
        }
    except SQLAlchemyError as exc:
        gaps.append("historical_decisions")
        feeds["historical_decisions"] = {
            "available": False,
            "gap": f"complaint history unavailable: {exc}",
            "citation": "complaint_cases",
        }

    # --- conversation evidence ----------------------------------------------
    dissatisfaction = 0.0
    try:
        outcome = await _first_row(
            db,
            select(models.RecoveryOutcome)
            .where(models.RecoveryOutcome.user_id == user_id)
            .order_by(models.RecoveryOutcome.created_at.desc())
            .limit(1),
        )
        if outcome is not None:
            dissatisfaction = float(getattr(outcome, "dissatisfaction_score", 0.0) or 0.0)
        feeds["conversation"] = {
            "available": outcome is not None,
            "observed": {
                "dissatisfaction_score": dissatisfaction,
                "signals": len(_loads(getattr(outcome, "recovery_signals_json", "[]"), "array")) if outcome is not None else 0,
            },
            "confidence": 0.8 if outcome is not None else 0.0,
            "citation": "recovery_outcomes (latest)",
            "observed_at": _as_utc(getattr(outcome, "created_at", None)),
            "gap": "" if outcome is not None else "no recovery outcome recorded for this customer yet",
        }
    except SQLAlchemyError as exc:
        gaps.append("conversation")
        feeds["conversation"] = {"available": False, "gap": f"conversation signals unavailable: {exc}", "citation": "recovery_outcomes"}

    # --- relationship ---------------------------------------------------------
    churn_risk = "low"
    loyalty_score = 0.0
    lifecycle_stage = "new"
    try:
        snapshot = await _first_row(
            db,
            select(models.RetentionSnapshot)
            .where(models.RetentionSnapshot.user_id == user_id)
            .order_by(models.RetentionSnapshot.created_at.desc())
            .limit(1),
        )
        if snapshot is not None:
            churn_risk = str(getattr(snapshot, "churn_risk", "low") or "low")
            loyalty_score = float(getattr(snapshot, "loyalty_score", 0.0) or 0.0)
            lifecycle_stage = str(getattr(snapshot, "lifecycle_stage", "new") or "new")
        feeds["relationship"] = {
            "available": snapshot is not None,
            "observed": {
                "churn_risk": churn_risk,
                "loyalty_score": loyalty_score,
                "lifecycle_stage": lifecycle_stage,
            },
            "confidence": 0.9 if snapshot is not None else 0.0,
            "citation": "retention_snapshots (latest)",
            "observed_at": _as_utc(getattr(snapshot, "created_at", None)),
            "gap": "" if snapshot is not None else "no retention snapshot for this customer yet",
        }
    except SQLAlchemyError as exc:
        gaps.append("relationship")
        feeds["relationship"] = {"available": False, "gap": f"relationship context unavailable: {exc}", "citation": "retention_snapshots"}

    # --- policy posture -------------------------------------------------------
    policy_tier = "standard"
    control_posture = "constrained"
    try:
        score = await _first_row(
            db,
            select(models.CustomerPolicyScore).where(models.CustomerPolicyScore.user_id == user_id),
        )
        if score is not None:
            policy_tier = str(getattr(score, "policy_tier", "standard") or "standard")
            control_posture = str(getattr(score, "control_posture", "constrained") or "constrained")
        feeds["policy_posture"] = {
            "available": score is not None,
            "observed": {"policy_tier": policy_tier, "control_posture": control_posture},
            "confidence": 1.0 if score is not None else 0.5,
            "citation": "customer_policy_scores",
            "observed_at": _as_utc(getattr(score, "updated_at", None)),
            "gap": "" if score is not None else "no policy score on file; assuming the most constrained posture",
        }
    except SQLAlchemyError as exc:
        gaps.append("policy_posture")
        feeds["policy_posture"] = {"available": False, "gap": f"policy posture unavailable: {exc}", "citation": "customer_policy_scores"}

    # --- commercial exposure --------------------------------------------------
    arrears_total = 0.0
    points_balance = 0.0
    try:
        # Only *open* arrears are exposure. `principal` on a settled or waived
        # row is a historical fact about an agreement that closed, and summing
        # it would report money owed that is not owed -- which is the kind of
        # overstatement that quietly escalates every long-standing customer.
        arrears_rows = await db.execute(
            select(
                func.sum(models.ArrearsEntry.principal),
                func.sum(models.ArrearsEntry.interest_accrued),
            ).where(
                models.ArrearsEntry.user_id == user_id,
                models.ArrearsEntry.status == "open",
            )
        )
        row = arrears_rows.first()
        if row is not None:
            arrears_total = float(row[0] or 0.0) + float(row[1] or 0.0)
        wallet = await db.execute(
            select(func.sum(models.PointsWallet.balance)).where(models.PointsWallet.user_id == user_id)
        )
        points_balance = float((wallet.first() or [0.0])[0] or 0.0)
        feeds["commercial"] = {
            "available": True,
            "observed": {"arrears_total": round(arrears_total, 2), "points_balance": round(points_balance, 2)},
            "confidence": 1.0,
            "citation": "arrears_entries + points_wallets",
            "observed_at": moment,
        }
    except SQLAlchemyError as exc:
        gaps.append("commercial")
        feeds["commercial"] = {"available": False, "gap": f"commercial exposure unavailable: {exc}", "citation": "arrears_entries"}

    # --- journey / bookings ---------------------------------------------------
    pending_bookings = 0
    cancelled_bookings = 0
    try:
        pending_bookings = await _count(
            db,
            select(func.count(models.Booking.id)).where(
                models.Booking.user_id == user_id,
                models.Booking.status == models.BookingStatus.pending,
            ),
        )
        cancelled_bookings = await _count(
            db,
            select(func.count(models.Booking.id)).where(
                models.Booking.user_id == user_id,
                models.Booking.status == models.BookingStatus.cancelled,
            ),
        )
        feeds["journey"] = {
            "available": True,
            "observed": {"pending_bookings": pending_bookings, "cancelled_bookings": cancelled_bookings},
            "confidence": 1.0,
            "citation": "bookings",
            "observed_at": moment,
        }
    except SQLAlchemyError as exc:
        gaps.append("journey")
        feeds["journey"] = {"available": False, "gap": f"journey context unavailable: {exc}", "citation": "bookings"}

    # --- communication constraints -------------------------------------------
    consent_withholds = False
    try:
        profile = await _first_row(
            db,
            select(models.UserPreferenceProfile).where(models.UserPreferenceProfile.user_id == user_id),
        )
        consents = _loads(getattr(profile, "consents_json", "{}"), "object") if profile is not None else {}
        # The BLOCKAGES keep-as-is: the consent gate must never suppress a
        # service or recovery purpose. `gates_outreach` is the only thing that
        # counts here, and it is read from the config rather than hardcoded, so
        # a purpose that is ever added to CONSENT_GATED_PURPOSES is visible in
        # this guard rather than silently withholding a fix.
        consent_withholds = any(
            CONSENT_PURPOSE_BY_NAME.get(purpose, {}).get("gates_outreach")
            and not bool(consents.get(purpose, True))
            for purpose in CONSENT_GATED_PURPOSES
        )
        feeds["communication_constraints"] = {
            "available": profile is not None,
            "observed": {
                "locale": locale,
                "consent_withholds_contact": consent_withholds,
                "gated_purposes": list(CONSENT_GATED_PURPOSES),
            },
            "confidence": 1.0 if profile is not None else 0.7,
            "citation": "user_preference_profiles + CONSENT_GATED_PURPOSES",
            "observed_at": _as_utc(getattr(profile, "updated_at", None)),
            "gap": "" if profile is not None else "no preference profile; assuming default contact permission",
        }
    except SQLAlchemyError as exc:
        gaps.append("communication_constraints")
        feeds["communication_constraints"] = {"available": False, "gap": f"communication constraints unavailable: {exc}", "citation": "user_preference_profiles"}

    # --- external signals -----------------------------------------------------
    # Always reported as absent on this path. The `async_only` flag in
    # DECISION_FEEDS is the rule: an outbound provider call must never sit on a
    # request path, so this reads as an explicit "not consulted here" rather
    # than being omitted.
    feeds["external"] = {
        "available": False,
        "observed": {},
        "confidence": 0.0,
        "citation": "app.enrichment (background stage only)",
        "gap": "external signals are background-only and are not consulted on a request path",
        "note": "run the enrichment stage to populate; see ENRICHMENT_RULES",
    }

    # --- governance -----------------------------------------------------------
    feeds["governance"] = {
        "available": True,
        "observed": {
            "guards": len(ESCALATION_GUARDS),
            "non_waivable": list(ESCALATION_OVERRIDE_RULES["non_waivable_guard_severities"]),
            "authority_floor": {
                "can_lower_tier": ESCALATION_OVERRIDE_RULES["can_lower_tier"],
                "can_lower_severity": ESCALATION_OVERRIDE_RULES["can_lower_severity"],
            },
        },
        "confidence": 1.0,
        "citation": f"{COMPLAINTS_CATALOG_VERSION} guards",
        "observed_at": moment,
    }

    severity = str(payload.get("severity", "medium"))
    severity_rank = COMPLAINT_SEVERITY_RANK.get(severity, 0)
    # A case whose recorded severity is far below its measured dissatisfaction
    # is nearly always one opened before the signals accumulated. Surfacing the
    # gap as its own number lets a guard act on it rather than leaving the
    # reviewer to spot the inconsistency themselves.
    implied_severity = _severity_from_dissatisfaction(dissatisfaction)
    severity_understated_by = max(0, COMPLAINT_SEVERITY_RANK.get(implied_severity, 0) - severity_rank)

    context: dict[str, Any] = {
        "category": payload.get("category"),
        "severity": severity,
        "severity_rank": severity_rank,
        "status": payload.get("status"),
        "tier": payload.get("tier"),
        "age_hours": round(age_hours, 3),
        "response_overdue_hours": round(response_overdue, 3),
        "resolution_overdue_hours": round(resolution_overdue, 3),
        "regulatory_deadline_hours": round(regulatory_deadline_hours, 3),
        "regulatory_hours_remaining": None if statutory_hours_remaining is None else round(statutory_hours_remaining, 3),
        "regulatory": regulatory,
        "resolved": str(payload.get("status")) in COMPLAINT_RESOLVED_STATUSES,
        "no_owner": not payload.get("owner_user_id") and not payload.get("owner_team"),
        "has_owner": bool(payload.get("owner_user_id")) or bool(payload.get("owner_team")),
        "owner_user_id": payload.get("owner_user_id"),
        "open_complaints": open_complaints,
        "total_complaints": total_complaints,
        "complaints_last_30d": complaints_last_30d,
        "reopened_count": payload.get("reopened_count", 0),
        "duplicate_open_cases": duplicate_open_cases,
        "dissatisfaction_score": dissatisfaction,
        "churn_risk": churn_risk,
        "loyalty_score": loyalty_score,
        "lifecycle_stage": lifecycle_stage,
        "value_tier": lifecycle_stage,
        "policy_tier": policy_tier,
        "control_posture": control_posture,
        "arrears_total": arrears_total,
        "points_balance": points_balance,
        "points_at_risk": points_balance,
        "pending_bookings": pending_bookings,
        "cancelled_bookings": cancelled_bookings,
        "locale": locale,
        "consent_withholds_contact": consent_withholds,
        "satisfaction_score": payload.get("satisfaction_score"),
        "resolution_code": payload.get("resolution_code", ""),
        "resolved_stale_hours": round(resolved_stale, 3),
        "severity_understated_by": severity_understated_by,
    }
    return {"context": context, "feeds": feeds, "gaps": sorted(set(gaps)), "case": payload}


def _severity_from_dissatisfaction(score: float) -> str:
    """Map a dissatisfaction score onto a complaint severity band."""
    value = float(score or 0.0)
    if value >= 18:
        return "critical"
    if value >= 10:
        return "high"
    if value >= 5:
        return "medium"
    return "low"


# =============================================================================
# Case lifecycle
# =============================================================================
#
# Each transition is one function that mutates the case and writes its event in
# the same transaction. There is no generic `patch_case`, because the
# interesting part of a state machine is not the field write -- it is which
# timestamps move, which guard applies, and which decision record results.
# A generic setter would hide all three.


async def open_complaint(
    db: AsyncSession,
    user_id: int,
    *,
    category: str = DEFAULT_COMPLAINT_CATEGORY,
    severity: str = "",
    summary: str = "",
    source: str = "chat",
    regulatory: Optional[bool] = None,
    factors: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Open a case, set its SLA clock, and record the opening event.

    ``commit=False`` joins the caller's transaction instead of committing one.
    The recovery-playbook orchestrator runs a unit of work and commits once at
    the end, so a handler that committed on its own would break that: a case
    could be durable while the ``RecoveryAction`` row recording why it was
    opened was not, and "one commit per run" is a contract the orchestrator
    tests already pin. The router paths pass ``commit=True`` and are unaffected.

    The severity defaults to the category's configured severity, so a privacy
    complaint starts at `high` without anyone having to remember that it should.
    An explicit severity is honoured -- an operator who has seen the case knows
    more than the config does -- but the guard `severity_understated_vs_*` still
    reports a gap, so a downgrade is visible rather than silent.
    """
    moment = _as_utc(now) or _now()
    category_name = category if category in COMPLAINT_CATEGORY_BY_NAME else DEFAULT_COMPLAINT_CATEGORY
    category_row = COMPLAINT_CATEGORY_BY_NAME[category_name]
    severity_name = str(severity or category_row.get("default_severity", "medium"))
    if severity_name not in COMPLAINT_SEVERITY_RANK:
        severity_name = str(category_row.get("default_severity", "medium"))
    is_regulatory = bool(category_row.get("regulatory", False) if regulatory is None else regulatory)

    sla = resolve_sla(severity_name, regulatory=is_regulatory)
    route = resolve_route({"category": category_name, "severity": severity_name})

    case = models.ComplaintCase(
        user_id=int(user_id),
        reference=_provisional_reference(),
        category=category_name,
        severity=severity_name,
        status="open",
        tier=str(route.get("to_tier", DEFAULT_ESCALATION_TIER)),
        owner_user_id=None,
        owner_team=str(route.get("owner_team", "")),
        opened_at=moment,
        response_due_at=moment + timedelta(hours=float(sla["response_hours"])),
        resolution_due_at=moment + timedelta(hours=float(sla["resolution_hours"])),
        escalation_trigger_id="",
        auto_escalated=False,
        regulatory=is_regulatory,
        resolution_code="",
        resolution_note="",
        satisfaction_score=None,
        reopened_count=0,
        factors_json=json.dumps(dict(factors or {}), ensure_ascii=False, sort_keys=True),
        summary=str(summary or ""),
        source=str(source or "chat"),
        created_at=moment,
        updated_at=moment,
    )
    db.add(case)
    await db.flush()
    # The durable reference is the primary key, assigned once the row exists.
    case.reference = _final_reference(case.id)
    await db.flush()
    # Reload before reading the row back. `flush` expires every column the
    # database generates -- including `updated_at`, which `TimestampMixin`
    # declares with `onupdate=func.now()` even when a value was passed in -- so
    # serialising the case without a refresh raises `MissingGreenlet` on an
    # AsyncSession. This was invisible while the only caller committed (the
    # commit was followed by its own refresh) and bit the `commit=False` path
    # used by the recovery orchestrator, where it turned every escalation into a
    # silently recorded failure. `expire_on_commit=False` is set app-wide, so
    # refreshing here covers both paths.
    await db.refresh(case)

    db.add(
        models.ComplaintEvent(
            complaint_id=int(case.id),
            user_id=int(user_id),
            event_type="opened",
            from_status="",
            to_status="open",
            from_tier="",
            to_tier=str(case.tier),
            actor_user_id=None,
            actor_role="customer",
            detail_json=json.dumps(
                {
                    "category": category_name,
                    "severity": severity_name,
                    "regulatory": is_regulatory,
                    "sla": sla,
                    "route": {key: route.get(key) for key in ("rule_id", "to_tier", "owner_team")},
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            note=str(summary or ""),
            created_at=moment,
            updated_at=moment,
        )
    )
    if commit:
        await db.commit()
    payload = case_to_dict(case)

    # A second complaint in a category that already has a live case is reported,
    # not blocked. Blocking it would be `suppress_recent_complaint` applied to the
    # wrong place: complaining twice is legitimate, and a customer who cannot
    # complain a second time is exactly the failure this system exists to
    # prevent. The `duplicate_open_case` guard is advisory for the same reason.
    # Surfacing the link is what lets an operator see that these are one problem
    # rather than three.
    try:
        existing = await find_open_case(
            db, int(user_id), category=category_name, exclude_id=int(case.id)
        )
    except SQLAlchemyError:  # pragma: no cover - defensive
        existing = None
    payload["existing_open_case"] = (
        {"reference": str(existing.reference), "id": int(existing.id), "status": str(existing.status)}
        if existing is not None
        else None
    )
    return payload


async def _write_event(
    db: AsyncSession,
    case: Any,
    event_type: str,
    *,
    from_status: str = "",
    to_status: str = "",
    from_tier: str = "",
    to_tier: str = "",
    actor_user_id: Optional[int] = None,
    actor_role: str = "",
    detail: Optional[dict[str, Any]] = None,
    note: str = "",
    now: Optional[datetime] = None,
) -> None:
    moment = _as_utc(now) or _now()
    db.add(
        models.ComplaintEvent(
            complaint_id=int(case.id),
            user_id=int(case.user_id),
            event_type=event_type,
            from_status=str(from_status or ""),
            to_status=str(to_status or ""),
            from_tier=str(from_tier or ""),
            to_tier=str(to_tier or ""),
            actor_user_id=None if actor_user_id is None else int(actor_user_id),
            actor_role=str(actor_role or ""),
            detail_json=json.dumps(dict(detail or {}), ensure_ascii=False, sort_keys=True, default=str),
            note=str(note or ""),
            created_at=moment,
            updated_at=moment,
        )
    )


async def _load_case(db: AsyncSession, complaint_id: Optional[int] = None, reference: str = "") -> Any:
    statement = select(models.ComplaintCase)
    if complaint_id is not None:
        statement = statement.where(models.ComplaintCase.id == int(complaint_id))
    elif reference:
        statement = statement.where(models.ComplaintCase.reference == str(reference))
    else:
        raise ValueError("a complaint id or reference is required")
    result = await db.execute(statement)
    case = result.scalars().first()
    if case is None:
        raise LookupError("no such complaint case")
    return case


async def acknowledge_complaint(
    db: AsyncSession,
    complaint_id: int,
    *,
    actor_user_id: Optional[int] = None,
    actor_role: str = "agent",
    note: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """First human contact. This is what the SLA response clock waits for."""
    moment = _as_utc(now) or _now()
    case = await _load_case(db, complaint_id=complaint_id)
    if str(case.status) in COMPLAINT_TERMINAL_STATUSES:
        raise ValueError(f"case is {case.status} and cannot be acknowledged")
    previous = str(case.status)
    case.acknowledged_at = case.acknowledged_at or moment
    case.first_response_at = case.first_response_at or moment
    case.status = "in_progress" if previous == "acknowledged" else "acknowledged"
    if not case.owner_user_id:
        case.owner_user_id = None if actor_user_id is None else int(actor_user_id)
    case.updated_at = moment
    await _write_event(
        db,
        case,
        "acknowledged",
        from_status=previous,
        to_status=str(case.status),
        from_tier=str(case.tier),
        to_tier=str(case.tier),
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        note=note,
        now=moment,
    )
    await db.commit()
    await db.refresh(case)
    return case_to_dict(case)


async def assign_complaint(
    db: AsyncSession,
    complaint_id: int,
    *,
    owner_user_id: Optional[int] = None,
    owner_team: str = "",
    tier: str = "",
    actor_user_id: Optional[int] = None,
    actor_role: str = "agent",
    note: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Put a named human or a team on a case.

    Distinct from an escalation because it is the *opposite* move: escalation
    raises the tier, assignment fills the `no_owner_assigned` guard without
    changing who is responsible for the case at its current level. Both are
    recorded as decisions, because "who picked this up" is exactly the
    institutional memory a later operator wants when the same customer complains
    again.

    The tier may be named here, but only ever *raised*: routing a case downward
    through the assignment path would be a way around the authority floor that
    `apply_escalation` refuses.
    """
    moment = _as_utc(now) or _now()
    case = await _load_case(db, complaint_id=complaint_id)
    from_tier = str(case.tier)
    target_tier = from_tier
    if tier and tier_rank(tier) > tier_rank(from_tier):
        target_tier = str(tier)
    elif tier and tier != from_tier:
        raise ValueError(
            "assignment may not lower a case's tier; use the escalation path "
            "with an override so the floor is evaluated"
        )

    previous_owner = case.owner_user_id
    previous_team = str(case.owner_team or "")
    case.owner_user_id = None if owner_user_id is None else int(owner_user_id)
    if owner_team:
        case.owner_team = str(owner_team)
    case.tier = target_tier
    case.updated_at = moment
    await _write_event(
        db,
        case,
        "assigned",
        from_status=str(case.status),
        to_status=str(case.status),
        from_tier=from_tier,
        to_tier=target_tier,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        detail={
            "from_owner_user_id": previous_owner,
            "to_owner_user_id": case.owner_user_id,
            "from_owner_team": previous_team,
            "to_owner_team": case.owner_team,
        },
        note=note,
        now=moment,
    )
    await db.commit()
    await record_complaint_decision(
        db,
        int(case.id),
        decision="assign",
        outcome="applied",
        from_tier=from_tier,
        to_tier=target_tier,
        decided_by_id=actor_user_id,
        decided_by_role=actor_role,
        rationale=note,
        factors={"owner_team": str(case.owner_team or ""), "owner_user_id": case.owner_user_id},
        now=moment,
    )
    await db.refresh(case)
    return case_to_dict(case)


async def add_complaint_note(
    db: AsyncSession,
    complaint_id: int,
    note: str,
    *,
    actor_user_id: Optional[int] = None,
    actor_role: str = "agent",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """A timeline entry with no state change.

    Worth having as its own transition rather than a nullable variant of the
    others: "somebody said something" and "somebody did something" are different
    events, and collapsing them makes the timeline unreadable.
    """
    moment = _as_utc(now) or _now()
    case = await _load_case(db, complaint_id=complaint_id)
    await _write_event(
        db,
        case,
        "note_added",
        from_status=str(case.status),
        to_status=str(case.status),
        from_tier=str(case.tier),
        to_tier=str(case.tier),
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        note=note,
        now=moment,
    )
    await db.commit()
    return case_to_dict(case)


async def resolve_complaint(
    db: AsyncSession,
    complaint_id: int,
    *,
    resolution_code: str,
    resolution_note: str = "",
    actor_user_id: Optional[int] = None,
    actor_role: str = "agent",
    satisfaction_score: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Mark a case resolved. Not closed: the complainant can still come back.

    The gap between `resolved` and `closed` is the reopen window, and closing
    early is how "resolved" quietly becomes a synonym for "gone".
    """
    moment = _as_utc(now) or _now()
    if resolution_code not in COMPLAINT_RESOLUTION_CODES:
        raise ValueError(f"unknown resolution_code: {resolution_code}")
    case = await _load_case(db, complaint_id=complaint_id)
    if str(case.status) in COMPLAINT_TERMINAL_STATUSES:
        raise ValueError(f"case is {case.status} and cannot be resolved")
    previous = str(case.status)
    case.status = "resolved"
    case.resolution_code = str(resolution_code)
    case.resolution_note = str(resolution_note or "")
    case.resolved_at = moment
    if satisfaction_score is not None:
        case.satisfaction_score = int(satisfaction_score)
    case.updated_at = moment
    await _write_event(
        db,
        case,
        "resolved",
        from_status=previous,
        to_status="resolved",
        from_tier=str(case.tier),
        to_tier=str(case.tier),
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        detail={"resolution_code": str(resolution_code)},
        note=resolution_note,
        now=moment,
    )
    await db.commit()
    # "We refunded this and here is why" is a decision, not just a status
    # change. Without this row the decision log only ever held escalations, the
    # precedent bundle had nothing to retrieve, and the learning loop had no
    # resolutions to be vindicated or refuted by.
    await record_complaint_decision(
        db,
        int(case.id),
        decision="resolve",
        outcome="applied",
        from_tier=str(case.tier),
        to_tier=str(case.tier),
        decided_by_id=actor_user_id,
        decided_by_role=actor_role,
        rationale=resolution_note,
        factors={"resolution_code": str(resolution_code)},
        now=moment,
    )
    await db.refresh(case)
    return case_to_dict(case)


async def close_complaint(
    db: AsyncSession,
    complaint_id: int,
    *,
    actor_user_id: Optional[int] = None,
    actor_role: str = "agent",
    note: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    moment = _as_utc(now) or _now()
    case = await _load_case(db, complaint_id=complaint_id)
    previous = str(case.status)
    case.status = "closed"
    case.closed_at = moment
    case.updated_at = moment
    await _write_event(
        db,
        case,
        "closed",
        from_status=previous,
        to_status="closed",
        from_tier=str(case.tier),
        to_tier=str(case.tier),
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        note=note,
        now=moment,
    )
    await _judge_latest_decision(db, case, observed="resolved" if previous == "resolved" else "held", now=moment)
    await db.commit()
    await db.refresh(case)
    return case_to_dict(case)


async def withdraw_complaint(
    db: AsyncSession,
    complaint_id: int,
    *,
    actor_user_id: Optional[int] = None,
    actor_role: str = "customer",
    note: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The complainant withdrew. A real outcome, and counted as one."""
    moment = _as_utc(now) or _now()
    case = await _load_case(db, complaint_id=complaint_id)
    if str(case.status) in COMPLAINT_TERMINAL_STATUSES:
        raise ValueError(f"case is already {case.status}")
    previous = str(case.status)
    case.status = "withdrawn"
    case.resolution_code = "withdrawn_by_complainant" if not case.resolution_code else case.resolution_code
    case.closed_at = moment
    case.updated_at = moment
    await _write_event(
        db,
        case,
        "withdrawn",
        from_status=previous,
        to_status="withdrawn",
        from_tier=str(case.tier),
        to_tier=str(case.tier),
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        note=note,
        now=moment,
    )
    await _judge_latest_decision(db, case, observed="held", now=moment)
    await db.commit()
    await db.refresh(case)
    return case_to_dict(case)


async def reopen_complaint(
    db: AsyncSession,
    complaint_id: int,
    *,
    reason: str = "",
    actor_user_id: Optional[int] = None,
    actor_role: str = "customer",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The resolution did not hold.

    This is the most valuable single event in the table. It is what marks every
    decision made on the case as a bad precedent, and it is recorded as a
    distinct `outcome_observed` on those decisions rather than being lost in a
    status change.
    """
    moment = _as_utc(now) or _now()
    case = await _load_case(db, complaint_id=complaint_id)
    if str(case.status) in COMPLAINT_TERMINAL_STATUSES + COMPLAINT_RESOLVED_STATUSES:
        previous = str(case.status)
        case.status = "open"
        case.reopened_count = int(case.reopened_count or 0) + 1
        case.resolved_at = None
        case.closed_at = None
        case.resolution_due_at = moment + timedelta(
            hours=float(resolve_sla(str(case.severity), regulatory=bool(case.regulatory))["resolution_hours"])
        )
        case.updated_at = moment
        await _write_event(
            db,
            case,
            "reopened",
            from_status=previous,
            to_status="open",
            from_tier=str(case.tier),
            to_tier=str(case.tier),
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            detail={"reopened_count": case.reopened_count},
            note=reason,
            now=moment,
        )
        # A reopen is a fact about the past decisions on this case, so record
        # it against them before the case moves on -- that is what turns the
        # earlier resolution into evidence it was wrong.
        await _judge_latest_decision(db, case, observed="reopened", now=moment)
        await db.commit()
        await record_complaint_decision(
            db,
            int(case.id),
            decision="reopen",
            outcome="applied",
            from_tier=str(case.tier),
            to_tier=str(case.tier),
            decided_by_id=actor_user_id,
            decided_by_role=actor_role,
            rationale=reason,
            factors={"reopened_count": int(case.reopened_count or 0)},
            now=moment,
        )
        await db.refresh(case)
        return case_to_dict(case)
    raise ValueError(f"case is {case.status}; only a resolved or closed case can be reopened")


async def _judge_latest_decision(db: AsyncSession, case: Any, *, observed: str, now: datetime) -> None:
    """Stamp `outcome_observed` on the case's most recent decision.

    An append-only table that learns after the fact: the decision row is never
    rewritten, only annotated with what the case actually did. That is what
    makes a precedent vindicable or refutable, and it is why `reopened` is a
    first-class outcome rather than an absence of one.

    **Only the latest decision is judged, and a new judgement overwrites an
    earlier one.** Judging every unjudged row looked equivalent and was not: a
    `resolve` followed by a `close` stamped the resolution `resolved`, and the
    later `reopen` then had nothing to refute, so a decision the complainant had
    demonstrably rejected still counted as vindicated and the contradiction rate
    reported zero. The question each row answers is "what happened after *this*
    decision", and the answer is the case's latest terminal state -- so it is
    the latest decision that carries it, and a later terminal state overwrites
    an earlier one.
    """
    if observed not in COMPLAINT_OUTCOMES_OBSERVED:
        return
    result = await db.execute(
        select(models.ComplaintDecision)
        .where(
            models.ComplaintDecision.complaint_id == int(case.id),
            models.ComplaintDecision.outcome == "applied",
        )
        .order_by(models.ComplaintDecision.created_at.desc(), models.ComplaintDecision.id.desc())
        .limit(1)
    )
    decision = result.scalars().first()
    if decision is None:
        return
    decision.outcome_observed = str(observed)
    decision.outcome_recorded_at = now
    decision.updated_at = now


async def record_complaint_decision(
    db: AsyncSession,
    complaint_id: int,
    *,
    decision: str,
    outcome: str = "applied",
    from_tier: str = "",
    to_tier: str = "",
    decided_by_id: Optional[int] = None,
    decided_by_role: str = "",
    step_up_level: str = "",
    rationale: str = "",
    factors: Optional[dict[str, Any]] = None,
    precedent_refs: Optional[list[dict[str, Any]]] = None,
    auto_applied: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Append a decision, and mark any earlier open proposal superseded.

    Supersession is a real foreign key, not a convention: when a second decision
    reverses the first, the first row is *pointed at* by the second. A
    sequence of unreferenced "proposed" rows would be a queue, and a queue is
    not a record of what was decided.
    """
    moment = _as_utc(now) or _now()
    if decision not in COMPLAINT_DECISIONS:
        raise ValueError(f"unknown complaint decision: {decision}")
    if outcome not in COMPLAINT_DECISION_OUTCOMES:
        raise ValueError(f"unknown decision outcome: {outcome}")
    case = await _load_case(db, complaint_id=complaint_id)

    prior = await db.execute(
        select(models.ComplaintDecision)
        .where(
            models.ComplaintDecision.complaint_id == int(case.id),
            models.ComplaintDecision.outcome == "proposed",
        )
        .order_by(models.ComplaintDecision.created_at.desc())
    )
    row = models.ComplaintDecision(
        complaint_id=int(case.id),
        user_id=int(case.user_id),
        decision=str(decision),
        outcome=str(outcome),
        from_tier=str(from_tier or ""),
        to_tier=str(to_tier or ""),
        decided_by_id=None if decided_by_id is None else int(decided_by_id),
        decided_by_role=str(decided_by_role or ""),
        step_up_level=str(step_up_level or ""),
        rationale=str(rationale or ""),
        factors_json=json.dumps(dict(factors or {}), ensure_ascii=False, sort_keys=True, default=str),
        precedent_refs_json=json.dumps(list(precedent_refs or []), ensure_ascii=False, sort_keys=True, default=str),
        auto_applied=bool(auto_applied),
        superseded_by_id=None,
        outcome_observed="",
        outcome_recorded_at=None,
        created_at=moment,
        updated_at=moment,
    )
    db.add(row)
    await db.flush()
    for earlier in prior.scalars().all():
        earlier.superseded_by_id = int(row.id)
        earlier.outcome = "superseded"
        earlier.updated_at = moment
    await db.commit()
    await db.refresh(row)
    return decision_to_dict(row)


async def apply_escalation(
    db: AsyncSession,
    case: Any,
    decision: dict[str, Any],
    *,
    actor_user_id: Optional[int] = None,
    actor_role: str = "",
    rationale: str = "",
    precedent_refs: Optional[list[dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Move a case to a higher tier and owner, and record why.

    Refuses to apply a *downgrade*. The refusal is returned rather than raised,
    matching the module's rule that evaluators return a verdict: a caller that
    tried to weaken policy gets a structured "not applied, and here is the
    rule" instead of an exception it has to interpret.
    """
    moment = _as_utc(now) or _now()
    from_tier = str(decision.get("from_tier", case.tier))
    to_tier = str(decision.get("to_tier", case.tier))
    blocked = str(decision.get("override_blocked_reason", "") or "")

    downgrade_blocked = tier_rank(to_tier) < tier_rank(from_tier)
    if downgrade_blocked:
        blocked = blocked or "an escalation may not move a case to a lower tier"

    # An owner change counts as an application even when the tier does not move.
    # A medium-severity case breaching its response SLA computes `to_tier_rank
    # == 1`, equal to the tier it is already on, so a tier-only test would find
    # nothing to do and the case would keep whoever owned it before -- which is
    # the exact situation the `regulatory_privacy_open` trigger exists to
    # prevent. The action a trigger authorises is the routing, not only the
    # promotion.
    #
    # `applied` is therefore derived from what would change, not from the
    # engine's own `escalated` flag: seeding it from that flag made an
    # owner-only routing invisible, and the breach that produced it went
    # unhandled while the sweep reported a clean run.
    target_owner = str(decision.get("owner_team", "") or "")
    owner_changed = bool(target_owner) and target_owner != str(case.owner_team or "")
    tier_moved = tier_rank(to_tier) > tier_rank(from_tier)
    applied = not downgrade_blocked and (tier_moved or owner_changed)
    if not applied and not blocked:
        blocked = "no tier or owner change was warranted"

    if applied:
        if tier_moved:
            case.tier = to_tier
        case.status = "escalated" if str(case.status) in ("open", "acknowledged", "in_progress") else str(case.status)
        case.escalated_at = case.escalated_at or moment
        case.escalation_trigger_id = str(decision.get("recommended_trigger_id", "") or "")
        case.auto_escalated = bool(decision.get("auto_applied", False))
        if target_owner:
            case.owner_team = target_owner
        case.updated_at = moment

    await _write_event(
        db,
        case,
        "auto_escalated" if bool(decision.get("auto_applied", False)) and applied else "escalated",
        from_status=str(case.status),
        to_status=str(case.status),
        from_tier=from_tier,
        to_tier=str(case.tier),
        actor_user_id=actor_user_id,
        actor_role=actor_role or ("system" if decision.get("auto_applied") else "agent"),
        detail={
            "trigger_id": str(decision.get("recommended_trigger_id", "") or ""),
            "auto": bool(decision.get("auto_applied", False)),
            "applied": applied,
            "tier_moved": tier_moved,
            "owner_changed": owner_changed,
            "blocked_reason": blocked,
        },
        note=rationale or str(decision.get("reason", "")),
        now=moment,
    )
    record = await record_complaint_decision(
        db,
        int(case.id),
        decision="escalate",
        outcome="auto_applied" if bool(decision.get("auto_applied", False)) and applied else "applied",
        from_tier=from_tier,
        to_tier=str(case.tier),
        decided_by_id=actor_user_id,
        decided_by_role=actor_role or ("system" if decision.get("auto_applied") else "agent"),
        rationale=rationale or str(decision.get("reason", "")),
        factors={
            "triggers": [item.get("trigger_id") for item in decision.get("triggers_fired", [])],
            "verdict": str(decision.get("verdict", "")),
            "guards_failed": decision.get("non_waivable_failures", []),
        },
        precedent_refs=precedent_refs,
        auto_applied=bool(decision.get("auto_applied", False)),
        now=moment,
    )
    await db.commit()
    await db.refresh(case)
    return {
        "case": case_to_dict(case),
        "decision": record,
        "applied": applied,
        "tier_moved": tier_moved,
        "owner_changed": owner_changed,
        "blocked_reason": blocked,
    }


async def run_auto_escalation_sweep(
    db: AsyncSession,
    *,
    limit: int = 100,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Re-evaluate every live case and apply auto-authorised escalations.

    Only ``authority == "auto"`` triggers are applied here. Advisory triggers
    are computed and reported -- an operator gets the recommendation in the
    sweep result -- but nothing is written for them, because the whole point of
    narrowing auto authority to a deadline and a clock is that judgment calls
    wait for a person.

    Also stamps an `sla_breached` event the first time a case goes overdue, so
    the breach is a thing that happened at a moment rather than something a
    later reader infers from a timestamp.
    """
    moment = _as_utc(now) or _now()
    if not COMPLAINT_AUTO_ESCALATION_ENABLED:
        return {
            "generated_at": moment.isoformat(),
            "enabled": False,
            "examined": 0,
            "escalated": 0,
            "breaches_stamped": 0,
            "results": [],
            "note": "auto escalation is disabled; nothing was read or written",
        }

    live = select(models.ComplaintCase).where(
        ~models.ComplaintCase.status.in_(COMPLAINT_TERMINAL_STATUSES)
    )
    if limit > 0:
        live = live.order_by(models.ComplaintCase.resolution_due_at.asc()).limit(int(limit))
    result = await db.execute(live)
    cases = list(result.scalars().all())

    escalated = 0
    breaches = 0
    results: list[dict[str, Any]] = []
    for case in cases:
        collected = await collect_complaint_context(db, case, now=moment)
        decision = build_escalation_decision(
            collected["context"],
            current_tier=str(case.tier),
            now=moment,
        )

        # Stamp a breach once. `sla_breached` events already present mean we
        # have recorded it, and a second event per sweep would turn the
        # timeline into noise.
        overdue = collected["context"]["response_overdue_hours"] > 0 or collected["context"]["resolution_overdue_hours"] > 0
        if overdue:
            seen = await db.execute(
                select(func.count(models.ComplaintEvent.id)).where(
                    models.ComplaintEvent.complaint_id == int(case.id),
                    models.ComplaintEvent.event_type == "sla_breached",
                )
            )
            if int((seen.first() or [0])[0] or 0) == 0:
                await _write_event(
                    db,
                    case,
                    "sla_breached",
                    from_status=str(case.status),
                    to_status=str(case.status),
                    from_tier=str(case.tier),
                    to_tier=str(case.tier),
                    actor_role="system",
                    detail={
                        "response_overdue_hours": collected["context"]["response_overdue_hours"],
                        "resolution_overdue_hours": collected["context"]["resolution_overdue_hours"],
                    },
                    now=moment,
                )
                breaches += 1

        entry: dict[str, Any] = {
            "complaint_id": int(case.id),
            "reference": str(case.reference),
            "escalated": False,
            "auto_applied": False,
            "advisory_pending": list(decision["advisory_triggers"]),
            "triggers_fired": [item["trigger_id"] for item in decision["triggers_fired"]],
        }
        # The condition is `auto_triggers`, NOT `acted`.
        #
        # `acted` is true whenever the engine reached a verdict, which includes
        # the case where a purely *advisory* trigger fired and proposed a tier
        # move. Keying the write on that would let the sweep apply exactly the
        # judgement calls the auto-authority policy reserves for a human -- the
        # test `test_judgment_triggers_are_reported_but_not_applied` exists
        # because that bug shipped into this draft and the policy it violated
        # is the one the whole module is built around.
        if decision["auto_triggers"]:
            applied = await apply_escalation(
                db,
                case,
                decision,
                actor_role="system",
                now=moment,
            )
            escalated += 1 if applied["applied"] else 0
            entry["escalated"] = bool(applied["applied"])
            entry["auto_applied"] = bool(applied["applied"] and decision.get("auto_applied"))
            entry["to_tier"] = applied["case"]["tier"]
            entry["owner_team"] = applied["case"]["owner_team"]
            entry["trigger_id"] = decision["recommended_trigger_id"]
        elif decision["advisory_triggers"] and decision["escalated"]:
            # Reported so an operator sees there is a recommendation waiting,
            # with the tier it would have moved to.
            entry["would_escalate_to"] = decision["to_tier"]
        results.append(entry)

    await db.commit()
    return {
        "generated_at": moment.isoformat(),
        "enabled": True,
        "examined": len(cases),
        "escalated": escalated,
        "breaches_stamped": breaches,
        "advisory_pending": sum(1 for item in results if item["advisory_pending"]),
        "authority_note": (
            "only 'regulatory' and 'sla_breach' triggers are applied automatically; "
            "judgment triggers are reported in advisory_pending for an operator"
        ),
        "results": results,
    }


# =============================================================================
# Precedent retrieval, from the database
# =============================================================================


async def load_precedent_candidates(
    db: AsyncSession,
    subject: dict[str, Any],
    *,
    limit: int = 60,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Past closed cases that could plausibly be cited, as plain dicts.

    Candidates are drawn from the whole closed history rather than the same
    category alone. A `billing` complaint decided against a `privacy` precedent
    is not obviously wrong -- a refusal to refund is how a privacy case starts
    -- and filtering on category before scoring would hide exactly the
    cross-domain parallels an operator most needs. Scoring, not filtering, is
    what narrows the set; `PRECEDENT_MIN_SIMILARITY` does the discarding.
    """
    result = await db.execute(
        select(models.ComplaintCase)
        .where(models.ComplaintCase.status.in_(COMPLAINT_TERMINAL_STATUSES + COMPLAINT_RESOLVED_STATUSES))
        .order_by(models.ComplaintCase.closed_at.desc().nullslast(), models.ComplaintCase.created_at.desc())
        .limit(max(1, int(limit)) * 3)
    )
    rows = list(result.scalars().all())
    subject_id = subject.get("id")

    # The observed outcome of a candidate comes from the decisions made on it.
    outcomes: dict[int, str] = {}
    roles: dict[int, str] = {}
    decided_at: dict[int, Any] = {}
    if rows:
        ids = [int(row.id) for row in rows]
        decisions = await db.execute(
            select(
                models.ComplaintDecision.complaint_id,
                models.ComplaintDecision.outcome_observed,
                models.ComplaintDecision.decided_by_role,
                models.ComplaintDecision.created_at,
            ).where(models.ComplaintDecision.complaint_id.in_(ids))
        )
        for complaint_id, observed, role, created in decisions.all():
            key = int(complaint_id)
            if observed and observed not in outcomes:
                outcomes[key] = str(observed)
            if role and not roles.get(key):
                roles[key] = str(role)
            if created is not None and key not in decided_at:
                decided_at[key] = created

    candidates: list[dict[str, Any]] = []
    for row in rows:
        payload = case_to_dict(row)
        if payload.get("id") == subject_id:
            continue
        candidates.append(
            {
                "complaint_id": payload.get("id"),
                "user_id": payload.get("user_id"),
                "reference": payload.get("reference"),
                "category": payload.get("category"),
                "severity": payload.get("severity"),
                "resolution_code": payload.get("resolution_code"),
                "resolution_note": payload.get("resolution_note"),
                "closed_at": payload.get("closed_at"),
                "decided_at": _iso(decided_at.get(int(payload.get("id") or 0))),
                "decided_by_role": roles.get(int(payload.get("id") or 0), ""),
                "outcome_observed": outcomes.get(int(payload.get("id") or 0), ""),
                "factors": payload.get("factors", {}),
            }
        )
    return candidates


async def build_case_precedent_bundle(
    db: AsyncSession,
    case: Any,
    *,
    limit: int = DEFAULT_PRECEDENT_LIMIT,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Precedent bundle for a case, assembled from the real decision history."""
    moment = _as_utc(now) or _now()
    subject = case_to_dict(case)
    candidates = await load_precedent_candidates(db, subject, now=moment)
    return build_precedent_bundle(subject, candidates, limit=limit, now=moment)


# =============================================================================
# The decision-support entry point
# =============================================================================


async def build_escalation_decision_support(
    db: AsyncSession,
    case: Any,
    *,
    locale: str = "global",
    overrides: Optional[dict[str, Any]] = None,
    precedent_limit: int = DEFAULT_PRECEDENT_LIMIT,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Everything an operator needs to decide this case, and nothing hidden.

    The composition is: collect every source, assemble the dossier from it,
    score the precedent set, then run the escalation engine over the resulting
    context. The dossier is built *before* the decision rather than after,
    because the dossier is the evidence and the decision is a reading of it --
    a decision whose evidence was assembled to match its conclusion would be
    worth nothing.
    """
    moment = _as_utc(now) or _now()
    collected = await collect_complaint_context(db, case, now=moment, locale=locale)
    context = collected["context"]
    dossier = build_decision_dossier(collected["feeds"], now=moment)

    precedent = await build_case_precedent_bundle(db, case, limit=precedent_limit, now=moment)
    # The precedent bundle is a *finding about the history*, so it replaces the
    # bare volume count in that feed rather than sitting beside it. The count
    # stays, because "how many complaints" and "what did we decide last time"
    # answer different questions.
    for block in dossier["feeds"]:
        if block["feed_id"] == "historical_decisions":
            observed = dict(block.get("observed") or {})
            observed["precedent_count"] = precedent["returned"]
            observed["contradicted_precedents"] = precedent["contradicted_count"]
            block["observed"] = observed

    decision = build_escalation_decision(
        context,
        current_tier=str(case.tier),
        overrides=overrides,
        now=moment,
    )
    return {
        "generated_at": moment.isoformat(),
        "case": collected["case"],
        "catalog_version": COMPLAINTS_CATALOG_VERSION,
        "dossier": dossier,
        "precedent": precedent,
        "decision": decision,
        "context": context,
        "gaps": collected["gaps"],
        "summary": _decision_summary(dossier, decision, precedent),
    }


def _decision_summary(
    dossier: dict[str, Any],
    decision: dict[str, Any],
    precedent: dict[str, Any],
) -> dict[str, Any]:
    """The one-paragraph version, for a queue row or a notification."""
    lines: list[str] = []
    if decision["escalated"]:
        authority = "automatically" if decision["auto_applied"] else "on recommendation"
        lines.append(
            f"Escalate {decision['from_tier']} -> {decision['to_tier']} {authority} "
            f"({decision['reason']})."
        )
    else:
        lines.append(f"No escalation; {decision['reason']}.")
    if decision["advisory_triggers"]:
        lines.append(f"Awaiting a decision on: {', '.join(decision['advisory_triggers'])}.")
    if precedent["returned"]:
        contradicted = precedent["contradicted_count"]
        tail = f", {contradicted} of which went on to reopen" if contradicted else ""
        lines.append(f"{precedent['returned']} comparable past case(s){tail}.")
    if dossier["missing_required_feeds"] or dossier["unavailable_required_feeds"]:
        lines.append(
            f"Incomplete evidence: {sorted(set(dossier['missing_required_feeds'] + dossier['unavailable_required_feeds']))}."
        )
    return {
        "text": " ".join(lines),
        "decision": "escalate" if decision["escalated"] else "hold",
        "auto_applied": bool(decision["auto_applied"]),
        "requires_acceptance": bool(decision["requires_acceptance"]),
        "verdict": str(decision["verdict"]),
        "evidence_complete": bool(dossier["complete"]),
        "precedent_count": int(precedent["returned"]),
        "contradicted_precedents": int(precedent["contradicted_count"]),
    }


# =============================================================================
# Read surfaces
# =============================================================================
#
# All strictly read-only. The old recovery path inserted a row on a GET; nothing
# here writes, so hitting a report endpoint a thousand times does not
# manufacture a thousand "attempts" and a case's timestamps mean what they say.


async def list_complaints(
    db: AsyncSession,
    *,
    user_id: Optional[int] = None,
    status: str = "",
    severity: str = "",
    tier: str = "",
    owner_team: str = "",
    include_closed: bool = False,
    limit: int = 50,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    statement = select(models.ComplaintCase)
    if user_id is not None:
        statement = statement.where(models.ComplaintCase.user_id == int(user_id))
    if status:
        statement = statement.where(models.ComplaintCase.status == str(status))
    if severity:
        statement = statement.where(models.ComplaintCase.severity == str(severity))
    if tier:
        statement = statement.where(models.ComplaintCase.tier == str(tier))
    if owner_team:
        statement = statement.where(models.ComplaintCase.owner_team == str(owner_team))
    if not include_closed:
        statement = statement.where(~models.ComplaintCase.status.in_(COMPLAINT_TERMINAL_STATUSES))
    result = await db.execute(
        statement.order_by(models.ComplaintCase.created_at.desc()).limit(max(1, min(500, int(limit))))
    )
    moment = _as_utc(now) or _now()
    rows = []
    for row in result.scalars().all():
        payload = case_to_dict(row)
        payload["age_hours"] = _age_hours(payload.get("opened_at"), moment)
        payload["sla_response_overdue_hours"] = _overdue_hours(
            payload.get("response_due_at"), payload.get("first_response_at"), moment
        )
        payload["sla_resolution_overdue_hours"] = _overdue_hours(
            payload.get("resolution_due_at"), payload.get("resolved_at"), moment
        )
        rows.append(payload)
    return rows


def _age_hours(opened_at: Any, now: datetime) -> float:
    opened = _as_utc(opened_at)
    if opened is None:
        return 0.0
    return round(max(0.0, (now - opened).total_seconds() / 3600.0), 2)


def _overdue_hours(due_at: Any, satisfied_at: Any, now: datetime) -> float:
    due = _as_utc(due_at)
    if due is None or _as_utc(satisfied_at) is not None:
        return 0.0
    return round(max(0.0, (now - due).total_seconds() / 3600.0), 2)


async def build_complaint_timeline(db: AsyncSession, case: Any) -> list[dict[str, Any]]:
    result = await db.execute(
        select(models.ComplaintEvent)
        .where(models.ComplaintEvent.complaint_id == int(case.id))
        .order_by(models.ComplaintEvent.created_at.asc(), models.ComplaintEvent.id.asc())
    )
    return [event_to_dict(row) for row in result.scalars().all()]


async def build_complaint_decision_history(db: AsyncSession, case: Any) -> list[dict[str, Any]]:
    result = await db.execute(
        select(models.ComplaintDecision)
        .where(models.ComplaintDecision.complaint_id == int(case.id))
        .order_by(models.ComplaintDecision.created_at.asc(), models.ComplaintDecision.id.asc())
    )
    return [decision_to_dict(row) for row in result.scalars().all()]


async def get_complaint_case(
    db: AsyncSession,
    *,
    complaint_id: Optional[int] = None,
    reference: str = "",
) -> dict[str, Any]:
    """One case with its timeline and decision history. Read-only."""
    case = await _load_case(db, complaint_id=complaint_id, reference=reference)
    payload = case_to_dict(case)
    payload["timeline"] = await build_complaint_timeline(db, case)
    payload["decisions"] = await build_complaint_decision_history(db, case)
    return payload


async def build_sla_report(
    db: AsyncSession,
    *,
    limit: int = 200,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Which SLAs are running, which are breached, and which are close.

    Reported from the case rows rather than from a counter, so a breach that
    was stamped once and then never re-stamped is still visible here. A sweep
    that runs and finds nothing new must not be able to make a breach
    disappear.
    """
    moment = _as_utc(now) or _now()
    rows = await list_complaints(db, include_closed=False, limit=limit, now=moment)
    buckets = {
        "breached_response": [],
        "breached_resolution": [],
        "due_soon": [],
        "on_track": [],
    }
    soon_threshold = 4.0
    for row in rows:
        response_overdue = float(row.get("sla_response_overdue_hours", 0.0) or 0.0)
        resolution_overdue = float(row.get("sla_resolution_overdue_hours", 0.0) or 0.0)
        if response_overdue > 0:
            buckets["breached_response"].append(row["reference"])
        if resolution_overdue > 0:
            buckets["breached_resolution"].append(row["reference"])
        if response_overdue > 0 or resolution_overdue > 0:
            continue
        if not row.get("first_response_at") and row.get("response_due_at"):
            due = _as_utc(row["response_due_at"])
            if due is not None and 0 <= (due - moment).total_seconds() / 3600.0 <= soon_threshold:
                buckets["due_soon"].append(row["reference"])
                continue
        buckets["on_track"].append(row["reference"])
    return {
        "generated_at": moment.isoformat(),
        "catalog_version": COMPLAINTS_CATALOG_VERSION,
        "examined": len(rows),
        "open_cases": len(rows),
        "breached_response": buckets["breached_response"],
        "breached_resolution": buckets["breached_resolution"],
        "due_soon": buckets["due_soon"],
        "due_soon_threshold_hours": soon_threshold,
        "on_track": buckets["on_track"],
        "breach_count": len(set(buckets["breached_response"]) | set(buckets["breached_resolution"])),
        "sla_matrix": [dict(row) for row in SLA_MATRIX],
        "regulatory_sla": dict(REGULATORY_SLA),
        "note": (
            "derived from the case rows on every read, so it cannot be made to "
            "look compliant by not running the sweep"
        ),
    }


async def build_complaint_admin_report(
    db: AsyncSession,
    *,
    limit: int = 50,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Queue, SLA posture, volume and the precedence-learning signal."""
    moment = _as_utc(now) or _now()
    live = await list_complaints(db, include_closed=False, limit=limit, now=moment)

    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    by_team: dict[str, int] = {}
    for row in live:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + 1
        by_tier[row["tier"]] = by_tier.get(row["tier"], 0) + 1
        if row.get("owner_team"):
            by_team[row["owner_team"]] = by_team.get(row["owner_team"], 0) + 1

    # How often a decision proved wrong. This is the one number that says
    # whether the escalation engine is learning anything, and it is the reason
    # `outcome_observed` is written at all.
    learned = await db.execute(
        select(
            models.ComplaintDecision.outcome_observed,
            func.count(models.ComplaintDecision.id),
        ).group_by(models.ComplaintDecision.outcome_observed)
    )
    outcomes = {str(key or ""): int(count) for key, count in learned.all()}
    judged = sum(count for key, count in outcomes.items() if key)
    contradicted = outcomes.get("reopened", 0) + outcomes.get("complainant_left", 0) + outcomes.get("escalated_further", 0)
    contradiction_rate = round(contradicted / judged, 4) if judged else None

    unowned = [row["reference"] for row in live if not row.get("owner_user_id") and not row.get("owner_team")]
    breached = [
        row["reference"]
        for row in live
        if float(row.get("sla_response_overdue_hours", 0.0) or 0.0) > 0
        or float(row.get("sla_resolution_overdue_hours", 0.0) or 0.0) > 0
    ]

    return {
        "generated_at": moment.isoformat(),
        "catalog_version": COMPLAINTS_CATALOG_VERSION,
        "open_cases": len(live),
        "by_status": dict(sorted(by_status.items())),
        "by_severity": dict(sorted(by_severity.items(), key=lambda kv: -COMPLAINT_SEVERITY_RANK.get(kv[0], 0))),
        "by_tier": dict(sorted(by_tier.items(), key=lambda kv: -tier_rank(kv[0]))),
        "by_owner_team": dict(sorted(by_team.items(), key=lambda kv: -kv[1])),
        "unowned": unowned,
        "sla_breached": breached,
        "sla_breach_count": len(breached),
        "decision_outcomes_observed": outcomes,
        "decisions_judged": judged,
        "contradicted_decisions": contradicted,
        "contradiction_rate": contradiction_rate,
        "queue": live[: max(1, int(limit))],
        "validation": validate_complaints(),
        "note": (
            "contradiction_rate is the share of judged decisions whose case was "
            "later reopened, abandoned, or escalated again. It is the only number "
            "here that can tell you the policy is not working"
        ),
    }


async def find_open_case(
    db: AsyncSession,
    user_id: int,
    *,
    category: str = "",
    exclude_id: Optional[int] = None,
) -> Any:
    """The customer's live case in this category, if there is one.

    Used by the recovery playbook path so a repeat escalation for the same
    unresolved problem attaches to the existing case rather than opening a
    second one. Two open cases for one issue is how a queue becomes unreadable
    and how "this customer's repeat rate" stops being a meaningful number.
    """
    statement = select(models.ComplaintCase).where(
        models.ComplaintCase.user_id == int(user_id),
        ~models.ComplaintCase.status.in_(COMPLAINT_TERMINAL_STATUSES + COMPLAINT_RESOLVED_STATUSES),
    )
    if category:
        statement = statement.where(models.ComplaintCase.category == str(category))
    if exclude_id is not None:
        statement = statement.where(models.ComplaintCase.id != int(exclude_id))
    result = await db.execute(statement.order_by(models.ComplaintCase.created_at.desc()).limit(1))
    return result.scalars().first()
