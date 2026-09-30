"""Predictive sentiment & automated service recovery.

Stage: "Predictive Sentiment & Automated Service Recovery" (proactive retention).

Ingests real-time interaction signals (recent chat rows + bookings + the latest
message sentiment), derives a *realtime* dissatisfaction context (score,
readiness, sentiment, churn/value signals, risk areas), and automatically
triggers config-driven recovery playbooks:

- ``credit_points``     — auto-issue goodwill loyalty points onto the user's
                          wallet and append a ``recovery_credit`` ledger row.
- ``escalate_ticket``   — record a senior-support escalation with a generated
                          ticket reference.
- ``adjust_policy_score`` — record a goodwill guardrail (access/customer score
                          deltas) and preview the adjusted policy snapshot when
                          the caller supplies one.

Playbooks are plain ``RECOVERY_PLAYBOOKS`` config rows evaluated by the shared
when-DSL engine (``app.rule_engine.evaluate_when``) with ``=formula`` param
resolution, so adding a recovery playbook is data, not code.

Explicit triggering is always available through the orchestration API. An
optional background sweep is gated by ``CSERVICE_AUTO_RECOVERY`` (default off)
so the default single-tenant operation and the test suite never change
behavior. The audit trail lives in ``models.RecoveryAction`` rows.

A second, additive layer sits on top of the original v1 core:

- ``RECOVERY_OUTREACH_PLAYBOOKS`` — proactive-reach playbooks (cancellation
  recovery, contact surges, warm re-engagement) that are *plans*, not side
  effects. They resolve a communication strategy, a callback moment, a save-offer
  preview and a human-review priority, and none of them send, book or issue
  anything on their own.
- ``RECOVERY_ACTION_REGISTRY`` — one flat name->handler map with a declared
  per-action policy in ``RECOVERY_ACTION_SPECS`` (writes? idempotent? budgeted?
  capped? reversible?). Replaces the orchestrator's if/elif dispatch chain, so a
  new action type is one spec row, one handler, one registry entry.
- ``RECOVERY_GUARD_RULES`` — the governance layer, and the reason it exists:
  the automated sweep is a *loop*. ``credit_points`` derives its amount from
  the current dissatisfaction score, and nothing in the original code bounded
  how often it could run, so a customer whose sentiment stayed negative was
  re-credited the same goodwill amount every pass, forever. Guards make the
  limits declarative: per-run, per-day, cooldown, and a daily budget per
  budgeted metric. A rejection is reported as ``skipped`` with the guard that
  said no, not as a failure.

Everything in the governance layer is additive: the three v1 core playbooks,
the ``recovery_playbooks_v1`` catalog version, the ``recovery_credit`` ledger
kind, the action statuses, and the ``matched_playbooks`` payload are unchanged.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import desc, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models, rule_engine
from app.rule_engine import evaluate_when, resolve_params
from app.schemas.chat import (
    DissatisfactionRecoveryReport,
    InteractionSummary,
    Sentiment,
)
from app.services.chat_analytics import (
    analyze_sentiment,
    build_dissatisfaction_recovery_report,
    build_summary,
)
from app.services.points_exchange import get_or_create_wallet
from app.services.policy_scoring import (
    PolicyScoreSnapshot,
    resolve_control_posture,
    resolve_policy_tier,
)

logger = logging.getLogger(__name__)

# Optional automated sweep (default off; workers are always opt-in in this app).
AUTO_RECOVERY_ENABLED = os.getenv("CSERVICE_AUTO_RECOVERY", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTO_RECOVERY_INTERVAL_SECONDS = float(os.getenv("CSERVICE_AUTO_RECOVERY_INTERVAL", "300"))

# Ledger kind used by the goodwill credit action (additive to the existing
# redeem_points / purchase_points kinds).
RECOVERY_CREDIT_KIND = "recovery_credit"

# Version of the *governance* layer, separate from the pinned v1 core catalog
# version. Adding a guard or an outreach playbook must not make it look like the
# pinned core contract changed.
RECOVERY_GOVERNANCE_VERSION = "recovery_governance_v1"


# ---------------------------------------------------------------------------
# Config-driven playbook table
# ---------------------------------------------------------------------------

RECOVERY_PLAYBOOKS: list[dict[str, Any]] = [
    {
        "playbook_id": "recovery_goodwill_points",
        "name": "Goodwill Points Credit",
        "description": (
            "Auto-issue goodwill loyalty points when a premium-value customer "
            "shows high/critical recovery readiness with negative sentiment."
        ),
        "priority": 10,
        "enabled": True,
        "when": {
            "all": [
                {"recovery_readiness": ["critical", "high"]},
                {"sentiment_label": "negative"},
                {"value_tier": ["premium"]},
            ]
        },
        "actions": [
            {
                "action": "credit_points",
                "params": {
                    "point_type": "loyalty_points",
                    "points": "=min(300, 25 + round(dissatisfaction_score * 2.5))",
                    "reference": "recovery:goodwill",
                },
            }
        ],
    },
    {
        "playbook_id": "recovery_ticket_escalation",
        "name": "Critical Ticket Escalation",
        "description": (
            "Escalate to senior support when recovery readiness is critical "
            "and churn risk is high."
        ),
        "priority": 20,
        "enabled": True,
        "when": {
            "all": [
                {"recovery_readiness": "critical"},
                {"churn_risk": "high"},
            ]
        },
        "actions": [
            {
                "action": "escalate_ticket",
                "params": {
                    "priority": "high",
                    "assignee": "senior_support",
                    "reason": "critical dissatisfaction with high churn risk",
                },
            }
        ],
    },
    {
        "playbook_id": "recovery_policy_guardrail",
        "name": "Policy Score Goodwill Guardrail",
        "description": (
            "Apply a temporary goodwill access-score lift so recovering "
            "premium/growth customers keep gated perks while dissatisfaction "
            "runs hot."
        ),
        "priority": 30,
        "enabled": True,
        "when": {
            "all": [
                {"recovery_readiness": ["high", "critical"]},
                {"value_tier": ["premium", "growth"]},
                {"not": {"churn_risk": "low"}},
            ]
        },
        "actions": [
            {
                "action": "adjust_policy_score",
                "params": {
                    "access_delta": "=min(10.0, round(dissatisfaction_score * 0.15, 2))",
                    "customer_delta": 2.0,
                    "reason": "recovery goodwill guardrail",
                },
            }
        ],
    },
]

# Version of the *core* playbook set. Pinned because downstream consumers key
# off the catalog version; the outreach set is versioned separately so adding a
# proactive-reach playbook never looks like a breaking change to the v1 core.
RECOVERY_CORE_PLAYBOOK_SET_VERSION = "recovery_playbooks_v1"
RECOVERY_OUTREACH_PLAYBOOK_SET_VERSION = "recovery_outreach_v1"


# ---------------------------------------------------------------------------
# Outreach playbook table (additive second set)
# ---------------------------------------------------------------------------
#
# Deliberately a separate table rather than more rows in ``RECOVERY_PLAYBOOKS``:
# these playbooks reach out to a customer, the core three move money and write
# scores, and conflating "we decided to offer something" with "we issued
# something" in one priority-ordered list is how a background sweep ends up
# mailing discounts nobody approved. Keeping them apart also means the v1 core
# catalog stays exactly the three playbooks its consumers pinned.
#
# None of these actions has a side effect. They produce a plan, and the plan is
# the deliverable. Priority continues after the core set (10/20/30) so the
# core wins when both match, which is also what ``guard_run_playbook_limit``
# relies on.
RECOVERY_OUTREACH_PLAYBOOKS: list[dict[str, Any]] = [
    {
        "playbook_id": "recovery_cancellation_save",
        "name": "Cancellation Save Outreach",
        "description": (
            "Reach out after repeated cancellations. Cancellations are the "
            "strongest single predictor in this table and are invisible to the "
            "sentiment signal: a customer who quietly abandons three bookings "
            "and never writes a negative sentence is not negative-sentiment, "
            "they are just gone."
        ),
        "priority": 40,
        "enabled": True,
        "when": {
            "all": [
                {"booking_cancelled": {"gte": 3}},
                {"not": {"recovery_readiness": ["low", "moderate"]}},
            ]
        },
        "actions": [
            {
                "action": "offer_save_incentive",
                "params": {"reason": "repeated booking cancellations"},
            },
            {
                "action": "notify_customer",
                "params": {
                    "locale": "global",
                },
            },
        ],
    },
    {
        "playbook_id": "recovery_contact_surge",
        "name": "Repeat Contact Surge",
        "description": (
            "Hand off to a human when the same customer keeps writing. A "
            "surge means the automated channel has already failed, and more "
            "automation on top of it is the exact behaviour that caused the "
            "surge."
        ),
        "priority": 50,
        "enabled": True,
        "when": {"repeated_messages": {"gte": 5}},
        "actions": [
            {
                "action": "flag_for_review",
                "params": {
                    "owner_team": "retention",
                    "reason": "repeat contact surge: the automated channel is not resolving this",
                },
            },
            {
                "action": "schedule_callback",
                "params": {},
            },
        ],
    },
    {
        "playbook_id": "recovery_warm_reengagement",
        "name": "Warm Re-engagement",
        "description": (
            "Stay in touch with a drifting customer who is not yet in crisis. "
            "Gated on neutral/positive sentiment so it cannot fire on top of a "
            "live incident and double-message someone who is already angry; the "
            "core set owns that case."
        ),
        "priority": 60,
        "enabled": True,
        "when": {
            "all": [
                {"recovery_readiness": ["low", "moderate"]},
                {"sentiment_label": ["neutral", "positive"]},
                {"repeated_messages": {"lt": 3}},
                # Re-engagement presumes there is a relationship to re-engage.
                # A customer with no interaction rows is not a drifting customer,
                # they are a customer the sweep has never heard from, and
                # greeting them as a lapsed account is how outreach becomes spam.
                {"messages_analyzed": {"gte": 1}},
            ]
        },
        "actions": [
            {
                "action": "notify_customer",
                "params": {"locale": "global"},
            },
        ],
    },
]

# Playbook set label -> rows. The set label travels with every match so a report
# reader can tell "the goodwill credit playbook fired" from "the save-offer
# playbook fired" without a second lookup.
RECOVERY_PLAYBOOK_SETS: dict[str, list[dict[str, Any]]] = {
    "core": RECOVERY_PLAYBOOKS,
    "outreach": RECOVERY_OUTREACH_PLAYBOOKS,
}
RECOVERY_PLAYBOOK_SET_VERSIONS: dict[str, str] = {
    "core": RECOVERY_CORE_PLAYBOOK_SET_VERSION,
    "outreach": RECOVERY_OUTREACH_PLAYBOOK_SET_VERSION,
}


# ---------------------------------------------------------------------------
# Realtime dissatisfaction indicators (pure)
# ---------------------------------------------------------------------------


def build_realtime_recovery_context(
    summary: InteractionSummary,
    sentiment: Optional[Sentiment],
    dissatisfaction: DissatisfactionRecoveryReport,
) -> dict[str, Any]:
    """Derive the live context a playbook ``when`` rule matches against.

    Everything here is computed from the *current* interaction window (rows
    passed in), not from persisted dashboard snapshots, so it behaves as a
    real-time dissatisfaction indicator feed.

    The second half of the returned mapping is *flattened* scalars. The original
    context only exposed ``booking_states`` as a nested counter, and the when-DSL
    has no dotted-path support, so ``booking_states.cancelled >= 3`` is not
    expressible -- every nested fact a playbook needs is also published as a flat
    key. The nested ``booking_states`` entry is kept because it is the field
    existing consumers read.
    """
    signals = list(getattr(dissatisfaction, "recovery_signals", None) or [])
    peak_intensity = max([float(getattr(signal, "intensity", 0.0) or 0.0) for signal in signals] or [0.0])
    metadata = dict(getattr(summary, "metadata", None) or {})
    booking_states = dict(metadata.get("booking_states", {}) or {})
    booking_counts = {str(state): int(count or 0) for state, count in booking_states.items()}
    booking_total = sum(booking_counts.values())
    booking_cancelled = int(booking_counts.get("cancelled", 0) or 0)
    booking_completed = int(booking_counts.get("completed", 0) or 0)
    # Everything that is neither cancelled nor completed is still live: pending,
    # confirmed, in_progress. Counting them as one bucket is deliberate -- the
    # question a playbook asks is "did they walk away or not", not which
    # fulfilment state they walked away from.
    booking_active = max(0, booking_total - booking_cancelled - booking_completed)
    risk_areas = [str(getattr(signal, "area", "")) for signal in signals]
    primary_risks = list(getattr(dissatisfaction, "primary_risks", None) or [])
    sentiment_label = str(sentiment.label) if sentiment else "neutral"
    return {
        "recovery_readiness": str(dissatisfaction.recovery_readiness),
        "dissatisfaction_score": round(float(dissatisfaction.dissatisfaction_score), 2),
        "sentiment_label": sentiment_label,
        "sentiment_score": round(float(getattr(sentiment, "score", 0.0) or 0.0), 4),
        "churn_risk": str(summary.churn_risk),
        "loyalty_score": round(float(summary.loyalty_score), 2),
        "monetization_readiness": round(float(summary.monetization_readiness), 2),
        "value_tier": str(summary.value_tier),
        "primary_risks": primary_risks,
        "risk_areas": risk_areas,
        "peak_intensity": round(peak_intensity, 4),
        "repeated_messages": int(metadata.get("repeated_messages", 0) or 0),
        "booking_states": booking_states,
        # --- flattened scalars (the DSL cannot address nested keys) ----------
        "messages_analyzed": int(getattr(summary, "messages_analyzed", 0) or 0),
        "bookings_analyzed": int(getattr(summary, "bookings_analyzed", 0) or 0),
        "booking_total": booking_total,
        "booking_cancelled": booking_cancelled,
        "booking_completed": booking_completed,
        "booking_active": booking_active,
        "primary_risk_count": len(primary_risks),
        "recovery_signal_count": len(signals),
        "risk_area_count": len(risk_areas),
        "distinct_risk_areas": len({area for area in risk_areas if area}),
        "has_negative_sentiment": sentiment_label == "negative",
    }


def recovery_lifecycle_stage(context: dict[str, Any]) -> str:
    """Resolve the recovery customer's lifecycle stage.

    The communication ladder keys off ``stage`` (``new``/``engaged``/``loyal``)
    and the recovery context is not built with that vocabulary, so the mapping
    lives here as its own table rather than leaking a fifth vocabulary into the
    when-DSL. First matching row wins; the order *is* the precedence, which is
    why ``loyal`` is tested before ``engaged``.
    """
    for rule in RECOVERY_STAGE_RULES:
        ok, _fields = evaluate_when(rule.get("when", {}), context)
        if ok:
            return str(rule["stage"])
    return RECOVERY_STAGE_DEFAULT


def evaluate_recovery_playbooks(
    context: dict[str, Any],
    *,
    effective_date: Any = None,
    playbook_sets: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
    """Return every enabled playbook whose ``when`` rule matches the context.

    Matched dicts include the full playbook spec plus ``matched_fields``
    (the field-level evidence the shared engine produced) and ``playbook_set``
    (which table the row came from).

    Matching is global rather than first-match-wins: a run that only stopped at
    the first match would silently drop the escalation whenever the credit
    playbook also matched, which is the exact case where both are needed. What
    bounds a run is ``guard_run_playbook_limit``, not matching order.
    """
    wanted = set(playbook_sets) if playbook_sets is not None else set(RECOVERY_PLAYBOOK_SETS)
    candidates: list[tuple[str, dict[str, Any]]] = []
    for set_name, rows in RECOVERY_PLAYBOOK_SETS.items():
        if set_name not in wanted:
            continue
        candidates.extend((set_name, row) for row in rows)
    matched: list[dict[str, Any]] = []
    for set_name, playbook in sorted(
        candidates, key=lambda item: int(item[1].get("priority", 100))
    ):
        if playbook.get("enabled", True) is False:
            continue
        ok, fields = evaluate_when(playbook.get("when", {}), context, effective_date=effective_date)
        if ok:
            entry = dict(playbook)
            entry["matched_fields"] = fields
            entry["playbook_set"] = set_name
            matched.append(entry)
    return matched


def plan_recovery_actions(
    context: dict[str, Any],
    *,
    effective_date: Any = None,
    playbook_sets: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
    """Flatten matched playbooks into one ordered, guardable action plan.

    The orchestrator used to resolve ``=formula`` params inline while looping,
    which made "what would this run do" unanswerable without running it. This
    is that answer: the exact ordered action list with resolved params and the
    budgeted spend each action would consume, available to the guard evaluator,
    to a dry run, and to an operator asking "why did nothing happen".

    Actions for an action name with no registry entry are *kept* in the plan and
    marked ``registered=False``. Dropping them would make an unrecognised action
    invisible; keeping them lets the guard layer and the analytics report it as
    registry drift instead.
    """
    plan: list[dict[str, Any]] = []
    for playbook in evaluate_recovery_playbooks(
        context, effective_date=effective_date, playbook_sets=playbook_sets
    ):
        for action in playbook.get("actions", []):
            action_name = str(action.get("action", ""))
            params = resolve_params(action.get("params"), context)
            spec = RECOVERY_ACTION_SPEC_BY_NAME.get(action_name)
            spend = 0.0
            if spec is not None and spec.get("spend_metric"):
                try:
                    spend = max(0.0, float(params.get(str(spec["spend_metric"]), 0.0) or 0.0))
                except (TypeError, ValueError):
                    spend = 0.0
            plan.append(
                {
                    "index": len(plan),
                    "playbook_id": str(playbook.get("playbook_id", "unknown")),
                    "playbook_name": str(playbook.get("name", "")),
                    "playbook_set": str(playbook.get("playbook_set", "core")),
                    "priority": int(playbook.get("priority", 100)),
                    "action": action_name,
                    "params": params,
                    "spend_metric": str(spec.get("spend_metric")) if spec and spec.get("spend_metric") else None,
                    "spend": round(spend, 2),
                    "registered": spec is not None,
                    "mutates_state": bool(spec.get("mutates_state")) if spec else False,
                }
            )
    return plan


def compute_realtime_dissatisfaction_indicators(
    summary: InteractionSummary,
    sentiment: Optional[Sentiment],
    dissatisfaction: DissatisfactionRecoveryReport,
    *,
    effective_date: Any = None,
) -> dict[str, Any]:
    """Expose the realtime indicator payload + predicted playbook matches."""
    context = build_realtime_recovery_context(summary, sentiment, dissatisfaction)
    matched = evaluate_recovery_playbooks(context, effective_date=effective_date)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "realtime_indicators": context,
        "auto_recovery_enabled": AUTO_RECOVERY_ENABLED,
        "playbook_matches": [
            {
                "playbook_id": entry["playbook_id"],
                "name": entry["name"],
                "priority": entry["priority"],
                "actions": [dict(action) for action in entry["actions"]],
                "matched_fields": entry["matched_fields"],
            }
            for entry in matched
        ],
    }


# ---------------------------------------------------------------------------
# Playbook actions
# ---------------------------------------------------------------------------


async def credit_recovery_points(
    db: AsyncSession,
    user_id: int,
    point_type: str,
    points: float,
    reference: str = "recovery",
) -> dict[str, Any]:
    """Credit points onto the wallet and append a ``recovery_credit`` ledger row."""
    wallet = await get_or_create_wallet(db, int(user_id), str(point_type))
    now = datetime.now(timezone.utc)
    credit = round(max(0.0, float(points)), 2)
    old_balance = float(getattr(wallet, "balance", 0.0) or 0.0)
    wallet.balance = round(old_balance + credit, 2)
    wallet.updated_at = now
    txn = models.PointsTransaction(
        user_id=int(user_id),
        point_type=str(point_type),
        kind=RECOVERY_CREDIT_KIND,
        points_delta=credit,
        currency="USD",
        currency_amount=0.0,
        rate=0.0,
        fee=0.0,
        reference=str(reference or "recovery"),
        created_at=now,
        updated_at=now,
    )
    db.add(txn)
    await db.flush()
    return {
        "kind": RECOVERY_CREDIT_KIND,
        "point_type": str(point_type),
        "points_credited": credit,
        "new_balance": round(float(getattr(wallet, "balance", 0.0) or 0.0), 2),
        "transaction_id": int(getattr(txn, "id", 0) or 0),
        "reference": str(reference or "recovery"),
    }


def build_escalation_result(
    user_id: int,
    params: dict[str, Any],
    sequence: int,
    *,
    complaint_reference: str = "",
) -> dict[str, Any]:
    """Describe an escalation. Prefer a real case reference when there is one.

    The ``ESC-{user}-{sequence:03d}`` form is kept only as a *fallback*, and it
    is a worse reference in every way that matters: ``sequence`` is a per-run
    in-process counter, so the first escalation for a user after a restart
    produced a string identical to every earlier one, and nothing ever resolved
    that string back to a queue or an owner. When a complaint case exists, its
    reference is the durable identity and this function should be reporting that
    one -- see ``_handle_escalate_ticket``, which opens the case.
    """
    reference = str(complaint_reference or "").strip()
    if not reference:
        reference = f"ESC-{int(user_id)}-{int(sequence):03d}"
    return {
        "ticket_reference": reference,
        "reference_is_durable": bool(complaint_reference),
        "priority": str(params.get("priority", "medium")),
        "assignee": str(params.get("assignee", "support")),
        "status": "created",
    }


def apply_policy_adjustment(
    snapshot: Optional[PolicyScoreSnapshot],
    params: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """Apply goodwill score deltas to a policy snapshot (preview only).

    ``=formula`` delta values are resolved against an empty scope (deltas are
    static numbers or formulas over constants). Returns
    ``(adjusted_preview, applied_deltas)``. The preview recomputes the
    tier/posture from the lifted scores using the canonical policy helpers;
    persisted scores are never mutated.
    """
    params = resolve_params(params or {}, {})
    clamp = lambda value: max(0.0, min(100.0, round(float(value), 2)))  # noqa: E731
    applied = {
        "access_delta": round(float(params.get("access_delta", 0.0) or 0.0), 2),
        "customer_delta": round(float(params.get("customer_delta", 0.0) or 0.0), 2),
        "system_delta": round(float(params.get("system_delta", 0.0) or 0.0), 2),
    }
    if snapshot is None:
        return None, applied
    adjusted_access = clamp(snapshot.access_score + applied["access_delta"])
    adjusted_customer = clamp(snapshot.customer_score + applied["customer_delta"])
    adjusted_system = clamp(snapshot.system_score + applied["system_delta"])
    # The preview reports the three clamped scores individually below, so a
    # rebuilt snapshot was constructed and thrown away on every call.
    tier = resolve_policy_tier(adjusted_access, adjusted_system)
    posture = resolve_control_posture(tier, adjusted_access, adjusted_system)
    return {
        "policy_tier": tier,
        "control_posture": posture,
        "access_score": adjusted_access,
        "customer_score": adjusted_customer,
        "system_score": adjusted_system,
        "preview_note": "recovery goodwill guardrail preview; persisted scores unchanged",
    }, applied



# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------
#
# The original dispatcher was an if/elif chain inside the orchestrator, which
# meant a new action type was a code edit in three places (the chain, the failure
# branch, and the catalog). Dispatch is now a dict lookup against
# ``RECOVERY_ACTION_REGISTRY`` and the per-action *policy* (does it write, is it
# idempotent, what does it cost, is it reversible) is declared in
# ``RECOVERY_ACTION_SPECS`` rather than inferred from reading the handler.
#
# The split matters because the guard layer below needs those facts about an
# action *before* deciding to run it, and a guard cannot read a property off a
# closure.

# Per-action declared policy. ``spend_metric`` is what a budget guard
# accumulates; a ``None`` metric means the action costs nothing budgeted and can
# therefore be governed by cooldown/cap only.
RECOVERY_ACTION_SPECS: list[dict[str, Any]] = [
    {
        "action": "credit_points",
        "handler": "credit_points",
        "title": "Goodwill points credit",
        "mutates_state": True,
        "idempotent": False,
        "spend_metric": "points",
        "max_per_day": 4,
        "cooldown_hours": 24,
        "reversible": True,
        "compensation": "debit_points",
        "description": (
            "Issue goodwill loyalty points onto the wallet and append a "
            f"``{RECOVERY_CREDIT_KIND}`` ledger row."
        ),
    },
    {
        "action": "escalate_ticket",
        "handler": "escalate_ticket",
        "title": "Senior-support escalation",
        "mutates_state": False,
        "idempotent": True,
        "spend_metric": None,
        "max_per_day": 8,
        "cooldown_hours": 2,
        "reversible": False,
        "compensation": None,
        "description": "Record a senior-support escalation with a generated ticket reference.",
    },
    {
        "action": "adjust_policy_score",
        "handler": "adjust_policy_score",
        "title": "Goodwill policy guardrail",
        "mutates_state": False,
        "idempotent": True,
        "spend_metric": None,
        "max_per_day": 4,
        "cooldown_hours": 12,
        "reversible": True,
        "compensation": "invert_delta",
        "description": "Preview an access/customer score lift; persisted scores are never mutated.",
    },
    {
        "action": "notify_customer",
        "handler": "notify_customer",
        "title": "Customer outreach",
        "mutates_state": False,
        "idempotent": True,
        "spend_metric": None,
        "max_per_day": 6,
        "cooldown_hours": 6,
        "reversible": False,
        "compensation": None,
        "description": (
            "Resolve the communication strategy for the customer (tone, channel, "
            "framing) via the shared precedence ladder and record the decision trail."
        ),
    },
    {
        "action": "schedule_callback",
        "handler": "schedule_callback",
        "title": "Callback plan",
        "mutates_state": False,
        "idempotent": True,
        "spend_metric": None,
        "max_per_day": 3,
        "cooldown_hours": 24,
        "reversible": False,
        "compensation": None,
        "description": "Produce a callback due date from the configured offset table.",
    },
    {
        "action": "offer_save_incentive",
        "handler": "offer_save_incentive",
        "title": "Save incentive",
        "mutates_state": False,
        "idempotent": True,
        "spend_metric": None,
        "max_per_day": 1,
        "cooldown_hours": 168,
        "reversible": False,
        "compensation": None,
        "description": "Compose a save-offer preview from the incentive table; issues nothing.",
    },
    {
        "action": "flag_for_review",
        "handler": "flag_for_review",
        "title": "Human review flag",
        "mutates_state": False,
        "idempotent": True,
        "spend_metric": None,
        "max_per_day": 5,
        "cooldown_hours": 12,
        "reversible": False,
        "compensation": None,
        "description": "Queue the customer for a human retention review with a priority.",
    },
]
RECOVERY_ACTION_SPEC_BY_NAME: dict[str, dict[str, Any]] = {
    str(spec["action"]): spec for spec in RECOVERY_ACTION_SPECS
}
RECOVERY_ACTION_NAMES: tuple[str, ...] = tuple(RECOVERY_ACTION_SPEC_BY_NAME)
# Actions whose handler touches the database. Kept as a derived list rather than
# a hand-maintained one so a new writing action cannot be added without also
# being declared as writing.
RECOVERY_STATEFUL_ACTIONS: tuple[str, ...] = tuple(
    str(spec["action"]) for spec in RECOVERY_ACTION_SPECS if spec["mutates_state"]
)
# A non-idempotent action with no cap and no cooldown is the unbounded-spend
# shape. Nothing in the registry should be in that state, so the analytics
# report asserts it rather than trusting it.
RECOVERY_GUARDED_FIELDS: tuple[str, ...] = ("max_per_day", "cooldown_hours")


# ---------------------------------------------------------------------------
# Outreach planning tables
# ---------------------------------------------------------------------------
#
# Config because each of these is a service-level decision, not a constant:
# "how soon do we call someone who is critical" and "how much do we offer to
# keep someone" are both policy an operator is expected to be able to change.

# Callback offsets by recovery readiness, in hours. First matching row wins.
RECOVERY_CALLBACK_PLANS: list[dict[str, Any]] = [
    {
        "plan_id": "callback_critical",
        "label": "Critical callback",
        "when": {"recovery_readiness": ["critical"]},
        "offset_hours": 2,
        "channel": "phone",
        "priority": "urgent",
        "owner_team": "senior_support",
    },
    {
        "plan_id": "callback_high",
        "label": "High-risk callback",
        "when": {"recovery_readiness": ["high"]},
        "offset_hours": 24,
        "channel": "phone",
        "priority": "high",
        "owner_team": "retention",
    },
    {
        "plan_id": "callback_standard",
        "label": "Standard callback",
        "when": {"recovery_readiness": ["moderate", "low"]},
        "offset_hours": 72,
        "channel": "email",
        "priority": "normal",
        "owner_team": "customer_success",
    },
]
RECOVERY_CALLBACK_DEFAULT: dict[str, Any] = {
    "plan_id": "",
    "channel": "email",
    "priority": "normal",
    "owner_team": "customer_success",
    "offset_hours": 0.0,
}

# Save-offer previews by readiness. ``points`` is a *suggestion* the outreach
# action renders; this action issues nothing, which is why it declares no spend
# metric and is not budgeted like ``credit_points``.
RECOVERY_SAVE_INCENTIVES: list[dict[str, Any]] = [
    {
        "offer_id": "save_critical",
        "name": "Critical save offer",
        "when": {"recovery_readiness": ["critical"]},
        "points": 500,
        "discount_percent": 25.0,
        "validity_hours": 72,
        "escalation": "human_review",
    },
    {
        "offer_id": "save_high",
        "name": "High-risk save offer",
        "when": {"recovery_readiness": ["high"]},
        "points": 250,
        "discount_percent": 15.0,
        "validity_hours": 168,
        "escalation": "none",
    },
    {
        "offer_id": "save_standard",
        "name": "Standard retention offer",
        "when": {"recovery_readiness": ["moderate", "low"]},
        "points": 75,
        "discount_percent": 5.0,
        "validity_hours": 336,
        "escalation": "none",
    },
]
RECOVERY_INCENTIVE_DEFAULT: dict[str, Any] = {
    "offer_id": "",
    "name": "",
    "points": 0.0,
    "discount_percent": 0.0,
    "validity_hours": 0.0,
    "escalation": "none",
}

# Human-review priority by recovery readiness. Ordered: the first matching row
# wins, so a table rather than a chained ternary keeps "how urgent is this
# customer's review" in the same place as the other retention policy tables.
RECOVERY_REVIEW_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "review_critical",
        "label": "Urgent retention review",
        "when": {"recovery_readiness": ["critical"]},
        "priority": "urgent",
        "sla_hours": 4,
    },
    {
        "rule_id": "review_high",
        "label": "High-priority retention review",
        "when": {"recovery_readiness": ["high"]},
        "priority": "high",
        "sla_hours": 24,
    },
    {
        "rule_id": "review_standard",
        "label": "Standard retention review",
        "when": {},
        "priority": "normal",
        "sla_hours": 72,
    },
]
RECOVERY_REVIEW_PRIORITIES: tuple[str, ...] = tuple(
    str(rule["priority"]) for rule in RECOVERY_REVIEW_RULES
)
RECOVERY_REVIEW_DEFAULT_RULE_ID = "review_standard"

# Recovery-readiness -> communication-ladder ``stage``. Ordered, first match
# wins. ``loyal`` is tested first because it is the strictest predicate.
RECOVERY_STAGE_RULES: list[dict[str, Any]] = [
    {
        "stage": "loyal",
        "when": {
            "all": [
                {"loyalty_score": {"gte": 80.0}},
                {"booking_completed": {"gte": 1}},
            ]
        },
    },
    {
        "stage": "new",
        "when": {
            "all": [
                {"messages_analyzed": {"lte": 0}},
                {"booking_total": {"lte": 0}},
            ]
        },
    },
    {
        "stage": "engaged",
        "when": {"messages_analyzed": {"gte": 1}},
    },
]
RECOVERY_STAGE_DEFAULT = "engaged"


# ---------------------------------------------------------------------------
# Guard rules
# ---------------------------------------------------------------------------
#
# These exist because the automated sweep is a loop, and a goodwill credit in a
# loop with no idempotency is an unbounded credit. ``CSERVICE_AUTO_RECOVERY``
# defaults the interval to 300s and ``credit_points`` computed its amount from
# the *current* dissatisfaction score, so a customer whose sentiment stayed
# negative was re-credited the same goodwill amount every pass, forever. Nothing
# in the original code bounded that: not the playbook table, not the action, not
# the orchestrator.
#
# A guard is a policy claim, so it lives in a table. Ordered; a guard that does
# not apply is reported as ``not_applicable`` rather than silently passing, so
# "nothing stopped this" is distinguishable from "nothing was checked".
RECOVERY_GUARD_RULES: list[dict[str, Any]] = [
    {
        "guard_id": "guard_run_playbook_limit",
        "check": "max_per_run",
        "subject": "playbook",
        "max": 3,
        "enabled": True,
        "reason": "run matched more playbooks than the configured per-run limit",
    },
    {
        "guard_id": "guard_run_action_limit",
        "check": "max_per_run",
        "subject": "action",
        "max": 6,
        "enabled": True,
        "reason": "run produced more actions than the configured per-run limit",
    },
    {
        "guard_id": "guard_action_cooldown",
        "check": "cooldown",
        "subject": "action",
        "enabled": True,
        "reason": "action already ran inside its cooldown window",
    },
    {
        "guard_id": "guard_action_daily_cap",
        "check": "daily_cap",
        "subject": "action",
        "enabled": True,
        "reason": "action reached its configured per-day cap",
    },
    {
        "guard_id": "guard_points_daily_budget",
        "check": "daily_budget",
        "subject": "metric",
        "metric": "points",
        "max": 750.0,
        "enabled": True,
        "reason": "daily budget for the metric would be exceeded",
    },
]
RECOVERY_GUARD_RULES_BY_ID: dict[str, dict[str, Any]] = {
    str(rule["guard_id"]): rule for rule in RECOVERY_GUARD_RULES
}
RECOVERY_GUARD_CHECKS: tuple[str, ...] = tuple(
    sorted({str(rule["check"]) for rule in RECOVERY_GUARD_RULES})
)
# Which historical rows a check counts, and why. A guard is only as good as its
# definition of "it already ran": counting a *failed* credit against the cooldown
# is correct (otherwise a persistently failing action retries every 300s) but
# counting it against the daily cap would be wrong (nothing was delivered).
RECOVERY_GUARD_HISTORY_RULES: dict[str, dict[str, Any]] = {
    "max_per_run": {
        "counted_statuses": [],
        "rationale": "per-run limits bound the plan, not the history",
    },
    "cooldown": {
        "counted_statuses": ["executed", "failed"],
        "rationale": "an attempt consumes the cooldown window, so a failing action cannot hot-loop",
    },
    "daily_cap": {
        "counted_statuses": ["executed"],
        "rationale": "the cap bounds how much was actually delivered",
    },
    "daily_budget": {
        "counted_statuses": ["executed"],
        "rationale": "budget is consumed only by delivered value",
    },
}
# Cooldown uses *attempts*; daily caps and budgets use the sliding day window.
# The cooldown is the longer window in every current spec, so the history query
# has to reach back at least this far or the cooldown silently never fires.
RECOVERY_GUARD_LOOKBACK_HOURS = 24.0
RECOVERY_GUARD_MAX_COOLDOWN_HOURS: float = max(
    float(spec.get("cooldown_hours", 0.0) or 0.0) for spec in RECOVERY_ACTION_SPECS
)
RECOVERY_GUARD_HISTORY_HOURS: float = max(
    RECOVERY_GUARD_LOOKBACK_HOURS, RECOVERY_GUARD_MAX_COOLDOWN_HOURS
)
# Skipped-action status. Distinct from ``failed``: a guard rejection is the
# system working, and reporting it as a failure would train operators to ignore
# failures. Paired with ``would_skip`` for dry runs.
RECOVERY_GUARD_SKIP_STATUS = "skipped"
RECOVERY_GUARD_DRY_RUN_SKIP_STATUS = "would_skip"
RECOVERY_GUARD_REASON_STATUS = "guard_rejected"
RECOVERY_GUARD_NOT_APPLICABLE = "not_applicable"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


class RecoveryActionContext:
    """Everything a handler needs, so every handler has one signature.

    Passing a context object rather than seven positional arguments is what lets
    ``RECOVERY_ACTION_REGISTRY`` stay a flat name->callable map instead of
    growing a signature every time a handler needs one more input.
    """

    __slots__ = (
        "db",
        "user_id",
        "params",
        "context",
        "policy_snapshot",
        "sequence",
        "dry_run",
        "now",
    )

    def __init__(
        self,
        *,
        db: Optional[AsyncSession] = None,
        user_id: int = 0,
        params: Optional[dict[str, Any]] = None,
        context: Optional[dict[str, Any]] = None,
        policy_snapshot: Optional[PolicyScoreSnapshot] = None,
        sequence: int = 0,
        dry_run: bool = False,
        now: Optional[datetime] = None,
    ) -> None:
        self.db = db
        self.user_id = int(user_id)
        self.params = dict(params or {})
        self.context = dict(context or {})
        self.policy_snapshot = policy_snapshot
        self.sequence = int(sequence)
        self.dry_run = bool(dry_run)
        self.now = now or datetime.now(timezone.utc)


async def _handle_credit_points(ctx: RecoveryActionContext) -> dict[str, Any]:
    return await credit_recovery_points(
        ctx.db,
        ctx.user_id,
        str(ctx.params.get("point_type", "loyalty_points")),
        float(ctx.params.get("points", 0.0) or 0.0),
        reference=str(ctx.params.get("reference", "recovery")),
    )


async def _handle_escalate_ticket(ctx: RecoveryActionContext) -> dict[str, Any]:
    """Open (or reuse) a real complaint case for this escalation.

    This is the fix for the surface's original hole. The action used to build
    an ``ESC-`` string from a per-run counter and write nothing, so an
    "escalation" was a log line nobody could act on and a reference that
    collided with every earlier one after a restart.

    Now a `dry_run` reports what *would* open and touches nothing, and a real
    run opens a genuine case: durable reference, category, severity from the
    configured category defaults, an SLA clock, a routing rule applied, and an
    ``opened`` event. Re-running the same playbook for the same customer
    reuses the live case rather than opening a second one, because two open
    cases for the same unresolved problem is how a queue becomes unreadable.

    The auto-escalation *sweep* (``services/complaints.py``) owns tier moves;
    this owns case creation. Splitting them means the background playbook path
    cannot escalate a case's tier on its own without the guard set having run.
    """
    from app.services import complaints as complaints_service

    if ctx.dry_run or ctx.db is None:
        route = complaints_service.resolve_route(
            {
                "category": str(ctx.params.get("category", complaints_service.DEFAULT_COMPLAINT_CATEGORY)),
                "severity": str(ctx.params.get("severity", "medium")),
            }
        )
        return {
            **build_escalation_result(ctx.user_id, ctx.params, ctx.sequence),
            "opened": False,
            "dry_run": True,
            "would_open_category": str(ctx.params.get("category", complaints_service.DEFAULT_COMPLAINT_CATEGORY)),
            "would_route_to_tier": str(route.get("to_tier", "")),
            "would_route_to_team": str(route.get("owner_team", "")),
            "note": "a dry run writes nothing; a real run opens a durable case",
        }

    # `commit=False`: the orchestrator owns the transaction and commits once at
    # the end, so a case must not become durable on its own here. See the note on
    # `open_complaint`.
    #
    # `find_open_case` issues a real SELECT, so the session's autoflush makes a
    # case opened earlier in this same transaction visible to it. That is what
    # stops a second escalation in one run from opening a duplicate -- no
    # separate bookkeeping of uncommitted rows is needed, and none is kept,
    # because a helper that only inspected `session.new` would miss every case
    # that had already been flushed and quietly claim to be a safety net.
    existing = await complaints_service.find_open_case(
        ctx.db, ctx.user_id, category=str(ctx.params.get("category", ""))
    )
    if existing is not None:
        return {
            **build_escalation_result(
                ctx.user_id, ctx.params, ctx.sequence, complaint_reference=str(existing.reference)
            ),
            "opened": False,
            "reused_existing_case": True,
            "case_id": int(existing.id),
            "status": str(existing.status),
        }

    opened = await complaints_service.open_complaint(
        ctx.db,
        ctx.user_id,
        category=str(ctx.params.get("category", complaints_service.DEFAULT_COMPLAINT_CATEGORY)),
        severity=str(ctx.params.get("severity", "") or ""),
        summary=str(ctx.params.get("reason", "escalated by a recovery playbook")),
        source="recovery_playbook",
        now=ctx.now,
        commit=False,
    )
    return {
        **build_escalation_result(
            ctx.user_id, ctx.params, ctx.sequence, complaint_reference=str(opened["reference"])
        ),
        "opened": True,
        "case_id": opened["id"],
        "category": opened["category"],
        "severity": opened["severity"],
        "tier": opened["tier"],
        "owner_team": opened["owner_team"],
        "response_due_at": opened["response_due_at"],
        "resolution_due_at": opened["resolution_due_at"],
    }


def _handle_adjust_policy_score(ctx: RecoveryActionContext) -> dict[str, Any]:
    preview, applied = apply_policy_adjustment(ctx.policy_snapshot, ctx.params)
    return {"applied_deltas": applied, "preview_generated": preview is not None, "preview": preview}


def _communication_context_from_recovery(context: dict[str, Any]) -> dict[str, Any]:
    """Adapt the recovery context to the keys the communication ladder reads.

    The two engines are deliberately not merged: recovery reasons about
    dissatisfaction, communication reasons about how to talk to someone, and a
    shared context would couple their config tables. This adapter is the whole
    coupling, and it is one direction and one place.
    """
    risks = [str(risk) for risk in (context.get("primary_risks") or [])]
    areas = [str(area) for area in (context.get("risk_areas") or [])]
    readiness = str(context.get("recovery_readiness", ""))
    # Recovery readiness is a 4-level scale; the communication policy table keys
    # off a churn risk_level. Mapping keeps the two vocabularies from bleeding.
    risk_level = {"critical": "critical", "high": "high", "moderate": "medium"}.get(readiness, "low")
    return {
        "stage": recovery_lifecycle_stage(context),
        "value_tier": context.get("value_tier", "standard"),
        "risk_level": risk_level,
        "churn_risk": context.get("churn_risk", "low"),
        "sentiment_label": context.get("sentiment_label", "neutral"),
        "monetization_readiness": context.get("monetization_readiness", 0.0),
        "top_issue_1": risks[0] if risks else (areas[0] if areas else ""),
        "top_issue_2": risks[1] if len(risks) > 1 else "",
    }


def resolve_recovery_outreach_strategy(
    context: dict[str, Any],
    *,
    locale: str = "global",
    admin_override: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Resolve the outreach strategy for a recovery context.

    Two things this now does that it did not before, both of which were silent
    wrongness rather than missing features.

    * **It runs the `communication_suppression` rule pack.** The pack was
      authored, exported, validated and catalogued, and had no caller anywhere
      in the app -- so `suppress_recent_complaint` and
      `suppress_negative_sentiment` had never suppressed anything in the life of
      the system. The pack is evaluated here on the same context, and a firing
      rule is reported as `suppressed` with its reason, which is how
      "this customer has complained twice in thirty days" finally has any
      effect at all.
    * **It accepts the admin override.** The recovery path never loaded it, so
      `notify_customer` inside a playbook could not see an override that
      `/chat/communication-strategy` reported for the very same customer. Two
      surfaces answering differently about one person is worse than either
      answer being absent.

    A fired suppression rule is reported *alongside* the resolved strategy
    rather than overwriting the channel. Silently replacing an operator-visible
    decision is the failure mode this module is otherwise built to avoid, and a
    conflict between "suppress this" and "this is a service recovery" is
    exactly the sort of thing a human should see.
    """
    from app.services.communication_strategy import resolve_communication_strategy

    comm_context = _communication_context_from_recovery(context)
    strategy = resolve_communication_strategy(
        comm_context,
        locale=locale or "global",
        admin_override=admin_override,
    )
    params = dict(strategy.get("params") or {})

    suppression = rule_engine.select_rules("communication_suppression", comm_context)
    fired = [
        item
        for item in suppression["fired"]
        if dict(item.get("params") or {}).get("suppress")
    ]
    channel = str(params.get("channel", "email_followup"))

    return {
        "channel": channel,
        "tone": str(params.get("tone", "professional")),
        "framing": str(params.get("framing", "resolution_first")),
        "reply_urgency": str(params.get("reply_urgency", "standard")),
        "strategy_id": str(strategy.get("profile_id", "")),
        "resolved_layer": str(strategy.get("resolved_layer", "")),
        "layer_label": str(strategy.get("layer_label", "")),
        "precedence": int(strategy.get("precedence", 0) or 0),
        "guidance": strategy.get("guidance", ""),
        "suppressed": bool(fired) and channel != "none",
        "suppression_pack": suppression["pack"],
        "suppression_rules": [str(item["id"]) for item in fired],
        "suppression_reason": str(dict(fired[0].get("params") or {}).get("reason", "")) if fired else "",
        "suppression_note": (
            "a fired suppression rule is reported next to the resolved strategy, "
            "not applied over it; a conflict between 'suppress this' and 'this is "
            "a service recovery' is for a human to settle"
        ),
    }


def resolve_recovery_callback_plan(
    context: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pick the configured callback plan and resolve it to a due moment."""
    moment = now or datetime.now(timezone.utc)
    for plan in RECOVERY_CALLBACK_PLANS:
        ok, _fields = evaluate_when(plan.get("when", {}), context)
        if not ok:
            continue
        offset = float(plan["offset_hours"])
        return {
            "plan_id": str(plan["plan_id"]),
            "label": str(plan["label"]),
            "channel": str(plan["channel"]),
            "priority": str(plan["priority"]),
            "owner_team": str(plan["owner_team"]),
            "offset_hours": offset,
            "due_at": (moment + timedelta(hours=offset)).isoformat(),
        }
    return {**RECOVERY_CALLBACK_DEFAULT, "label": "", "due_at": moment.isoformat()}


def resolve_recovery_incentive(
    context: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Compose the save-offer preview for a recovery context. Issues nothing."""
    moment = now or datetime.now(timezone.utc)
    for offer in RECOVERY_SAVE_INCENTIVES:
        ok, _fields = evaluate_when(offer.get("when", {}), context)
        if not ok:
            continue
        validity = float(offer["validity_hours"])
        return {
            "offer_id": str(offer["offer_id"]),
            "name": str(offer["name"]),
            "points": float(offer["points"]),
            "discount_percent": float(offer["discount_percent"]),
            "validity_hours": validity,
            "expires_at": (moment + timedelta(hours=validity)).isoformat(),
            "escalation": str(offer["escalation"]),
            "issued": False,
        }
    return {**RECOVERY_INCENTIVE_DEFAULT, "expires_at": moment.isoformat(), "issued": False}


def resolve_recovery_review_priority(context: dict[str, Any]) -> dict[str, Any]:
    """Resolve the human-review priority from the configured table."""
    for rule in RECOVERY_REVIEW_RULES:
        ok, fields = evaluate_when(rule.get("when", {}), context)
        if ok:
            return {
                "rule_id": str(rule["rule_id"]),
                "label": str(rule["label"]),
                "priority": str(rule["priority"]),
                "sla_hours": int(rule["sla_hours"]),
                "matched_fields": fields,
            }
    return {
        "rule_id": RECOVERY_REVIEW_DEFAULT_RULE_ID,
        "label": "",
        "priority": "normal",
        "sla_hours": 72,
        "matched_fields": {},
    }


async def _handle_notify_customer(ctx: RecoveryActionContext) -> dict[str, Any]:
    """Resolve the outreach strategy for this customer and report the trail.

    Records the decision; sends nothing. The action is deliberately a plan, not a
    side effect, so a playbook can never surprise a customer from a background
    sweep.

    Now async solely so it can load the customer's admin override. It could not
    before, which is why a playbook's `notify_customer` and the standalone
    `/chat/communication-strategy` route could report two different strategies
    for one person. Loading it here makes the recovery path answer the same
    question the same way. A failure to load is reported rather than swallowed,
    so "we could not see the override" stays distinguishable from "there is
    none" -- the first is a bug, the second is a fact.
    """
    from app.services.communication_strategy import load_admin_override

    override: Optional[dict[str, Any]] = None
    override_error = ""
    if ctx.db is not None:
        try:
            override = await load_admin_override(ctx.db, int(ctx.user_id))
        except Exception as exc:  # pragma: no cover - defensive
            override_error = str(exc)
    resolved = resolve_recovery_outreach_strategy(
        ctx.context,
        locale=str(ctx.params.get("locale", "global")),
        admin_override=override,
    )
    return {
        **resolved,
        "dispatched": False,
        "admin_override_applied": override is not None,
        "admin_override_error": override_error,
    }


def _handle_schedule_callback(ctx: RecoveryActionContext) -> dict[str, Any]:
    return resolve_recovery_callback_plan(ctx.context, now=ctx.now)


def _handle_offer_save_incentive(ctx: RecoveryActionContext) -> dict[str, Any]:
    return resolve_recovery_incentive(ctx.context, now=ctx.now)


def _handle_flag_for_review(ctx: RecoveryActionContext) -> dict[str, Any]:
    review = resolve_recovery_review_priority(ctx.context)
    return {
        "review_id": f"REV-{ctx.user_id}-{ctx.sequence:03d}",
        "priority": review["priority"],
        "priority_source": review["rule_id"],
        "sla_hours": review["sla_hours"],
        "owner_team": str(ctx.params.get("owner_team", "retention")),
        "reason": str(ctx.params.get("reason", "automated recovery flag")),
        "queue": "retention_review",
    }


# Flat name -> handler. Adding an action type is one spec row plus one handler
# plus one entry here; no dispatch branch is touched.
RECOVERY_ACTION_REGISTRY: dict[str, Any] = {
    "credit_points": _handle_credit_points,
    "escalate_ticket": _handle_escalate_ticket,
    "adjust_policy_score": _handle_adjust_policy_score,
    "notify_customer": _handle_notify_customer,
    "schedule_callback": _handle_schedule_callback,
    "offer_save_incentive": _handle_offer_save_incentive,
    "flag_for_review": _handle_flag_for_review,
}
# Registry entries with no spec row (or the reverse). A non-empty value is
# registry drift, surfaced by the analytics report rather than hidden here.
RECOVERY_REGISTRY_DRIFT: dict[str, list[str]] = {
    "handlers_without_spec": sorted(set(RECOVERY_ACTION_REGISTRY) - set(RECOVERY_ACTION_SPEC_BY_NAME)),
    "specs_without_handler": sorted(set(RECOVERY_ACTION_SPEC_BY_NAME) - set(RECOVERY_ACTION_REGISTRY)),
}


# ---------------------------------------------------------------------------
# Guard evaluation (pure)
# ---------------------------------------------------------------------------


def _guard_rule(
    check: str,
    *,
    subject: str = "action",
    metric: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """The first rule for ``(check, subject, metric)``, or ``None`` if unconfigured.

    Looked up rather than indexed so a re-ordered or trimmed
    ``RECOVERY_GUARD_RULES`` table degrades into "that limit is not configured"
    instead of into a wrong limit.
    """
    for rule in RECOVERY_GUARD_RULES:
        if str(rule.get("check")) != check:
            continue
        if str(rule.get("subject", "action")) != subject:
            continue
        if metric is not None and str(rule.get("metric", "")) != metric:
            continue
        return rule
    return None


def _normalise_history_at(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def recovery_history_row(
    action: Any,
    *,
    playbook_id: str = "",
    status: str = "",
    at: Any = None,
    spend: Any = None,
    row_id: Any = None,
    reference: str = "",
    failure_reason: str = "",
) -> dict[str, Any]:
    """Build the flat history shape the guard layer and analytics consume.

    The guard evaluator takes plain dicts rather than ORM rows so the whole
    governance layer is testable with no database and so a caller can supply
    history from anywhere (the ledger, a replay fixture, a different store)
    without the guards caring.

    ``spend`` is only honoured for an action whose spec declares a
    ``spend_metric``. An unrecognised action carrying a number is a ledger
    shape change, not money a budget is allowed to spend.
    """
    action_name = str(action or "")
    spec = RECOVERY_ACTION_SPEC_BY_NAME.get(action_name)
    metric = str(spec.get("spend_metric") or "") if spec else ""
    try:
        raw_spend = float(spend or 0.0)
    except (TypeError, ValueError):
        raw_spend = 0.0
    return {
        "id": int(row_id or 0),
        "playbook_id": str(playbook_id or ""),
        "action": action_name,
        "status": str(status or ""),
        "at": _normalise_history_at(at),
        "spend_metric": metric or None,
        "spend": round(raw_spend, 2) if metric and raw_spend > 0.0 else 0.0,
        "reference": str(reference or ""),
        "failure_reason": str(failure_reason or ""),
        "registered": spec is not None,
    }


def _guard_rule_reasons(rule: dict[str, Any]) -> dict[str, Any]:
    """What a configured rule would say, for the report."""
    return {
        "guard_id": str(rule["guard_id"]),
        "check": str(rule["check"]),
        "subject": str(rule.get("subject", "action")),
        "metric": rule.get("metric"),
        "max": rule.get("max"),
        "enabled": bool(rule.get("enabled", True)),
        "reason": str(rule.get("reason", "")),
    }


def evaluate_recovery_guards(
    planned: list[dict[str, Any]],
    history: Optional[list[dict[str, Any]]] = None,
    *,
    now: Optional[datetime] = None,
    lookback_hours: float = RECOVERY_GUARD_LOOKBACK_HOURS,
    enforce: bool = True,
) -> dict[str, Any]:
    """Decide, per planned action, whether the guard layer allows it to run.

    Pure: takes a plan (``plan_recovery_actions``) and flat history rows
    (``recovery_history_row``) and returns exactly one decision per planned
    action, in plan order. Every limit it applies is read from
    ``RECOVERY_GUARD_RULES`` or ``RECOVERY_ACTION_SPECS``; there is no limit
    number in this function that is not configuration.

    ``enforce=False`` runs the identical evaluation and reports what *would*
    fire. That is what a dry run needs and what an operator asking "why did
    nothing happen" needs, so it is a flag rather than a second code path that
    could drift from the enforcing one.

    Two accounting details that are easy to get wrong and are therefore explicit:

    - The daily budget is committed *through* the plan, not tested per action
      against the limit. Two 500-point credits against a 750 budget must pass
      the first and block the second; a per-action test passes both.
    - A blocked action consumes no budget and no cap slot. Only an allowed
      action moves the post-run position.
    """
    moment = now or datetime.now(timezone.utc)
    window_start = moment - timedelta(hours=max(0.0, float(lookback_hours)))
    rows = list(history or [])
    counted = {
        check: set(str(status) for status in (rule.get("counted_statuses") or []))
        for check, rule in RECOVERY_GUARD_HISTORY_RULES.items()
    }

    # --- history rollup -----------------------------------------------------
    attempts: dict[str, list[datetime]] = {}
    delivered: dict[str, int] = {}
    spend_by_metric: dict[str, float] = {}
    history_actions: dict[str, int] = {}
    for row in rows:
        action_name = str(row.get("action", ""))
        history_actions[action_name] = history_actions.get(action_name, 0) + 1
        at = row.get("at")
        status = str(row.get("status", ""))
        if not isinstance(at, datetime):
            continue
        if status in counted.get("cooldown", set()):
            attempts.setdefault(action_name, []).append(at)
        if at < window_start:
            continue
        if status in counted.get("daily_cap", set()):
            delivered[action_name] = delivered.get(action_name, 0) + 1
        if status in counted.get("daily_budget", set()):
            metric = str(row.get("spend_metric") or "")
            value = float(row.get("spend", 0.0) or 0.0)
            if metric and value > 0.0:
                spend_by_metric[metric] = round(spend_by_metric.get(metric, 0.0) + value, 2)

    # --- rules --------------------------------------------------------------
    play_rule = _guard_rule("max_per_run", subject="playbook")
    act_rule = _guard_rule("max_per_run", subject="action")
    cooldown_rule = _guard_rule("cooldown")
    cap_rule = _guard_rule("daily_cap")
    play_limit = int(play_rule.get("max", 0) or 0) if play_rule else 0
    act_limit = int(act_rule.get("max", 0) or 0) if act_rule else 0
    play_enforced = bool(play_rule and play_rule.get("enabled", True) and play_limit > 0 and enforce)
    act_enforced = bool(act_rule and act_rule.get("enabled", True) and act_limit > 0 and enforce)
    cooldown_enforced = bool(cooldown_rule and cooldown_rule.get("enabled", True) and enforce)
    cap_enforced = bool(cap_rule and cap_rule.get("enabled", True) and enforce)

    budget_limits: dict[str, float] = {}
    for rule in RECOVERY_GUARD_RULES:
        if str(rule.get("check")) != "daily_budget" or not rule.get("enabled", True):
            continue
        budget_limits[str(rule.get("metric", ""))] = float(rule.get("max", 0.0) or 0.0)
    budget_enforced = bool(enforce)
    budget_committed = dict(spend_by_metric)

    # --- per-plan decisions -------------------------------------------------
    playbook_ordinal: dict[str, int] = {}
    for item in planned:
        playbook_id = str(item.get("playbook_id", ""))
        if playbook_id not in playbook_ordinal:
            playbook_ordinal[playbook_id] = len(playbook_ordinal)

    decisions: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    suppressed_playbooks: set[str] = set()
    consulted: set[str] = set()
    action_ordinal = 0

    def _base(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "index": int(item.get("index", len(decisions))),
            "playbook_id": str(item.get("playbook_id", "")),
            "playbook_set": str(item.get("playbook_set", "core")),
            "action": str(item.get("action", "")),
        }

    def _block(item: dict[str, Any], rule: dict[str, Any], detail: dict[str, Any]) -> None:
        consulted.add(str(rule["guard_id"]))
        entry = _base(item)
        entry.update(
            {
                "allowed": False,
                "guard_id": str(rule["guard_id"]),
                "status": RECOVERY_GUARD_REASON_STATUS,
                "reason": str(rule["reason"]),
                "detail": detail,
            }
        )
        decisions.append(entry)

    def _budget_block(item: dict[str, Any], metric: str, requested: float, limit: float) -> None:
        rule = _guard_rule("daily_budget", subject="metric", metric=metric)
        if rule is None:  # pragma: no cover - only if budget_enforced without a rule
            rule = {"guard_id": f"guard_{metric}_daily_budget", "reason": f"daily budget for {metric} exceeded"}
        _block(
            item,
            rule,
            {
                "metric": metric,
                "limit": limit,
                "already_committed": round(budget_committed.get(metric, 0.0), 2),
                "requested": round(requested, 2),
            },
        )

    for item in planned:
        action_name = str(item.get("action", ""))
        playbook_id = str(item.get("playbook_id", ""))
        action_ordinal += 1
        spec = RECOVERY_ACTION_SPEC_BY_NAME.get(action_name)

        # 0) Unknown action. With no spec there is no declared policy, so no
        # guarantee about frequency, cost or reversibility is possible. Blocked
        # with its own guard id rather than as an ordinary limit trip, because
        # the fix is a config entry, not a bigger limit.
        if spec is None:
            consulted.add("guard_unknown_action")
            entry = _base(item)
            entry.update(
                {
                    "allowed": False,
                    "guard_id": "guard_unknown_action",
                    "status": RECOVERY_GUARD_REASON_STATUS,
                    "reason": (
                        f"action {action_name!r} has no registry spec, so no policy "
                        "can be enforced for it"
                    ),
                    "detail": {"action": action_name, "registered": False},
                }
            )
            decisions.append(entry)
            continue

        # 1) per-run playbook limit
        ordinal = playbook_ordinal.get(playbook_id, 0)
        if play_rule:
            consulted.add(str(play_rule["guard_id"]))
        if play_enforced and ordinal >= play_limit:
            if playbook_id not in suppressed_playbooks:
                suppressed_playbooks.add(playbook_id)
                suppressed.append(
                    {
                        "playbook_id": playbook_id,
                        "guard_id": str(play_rule["guard_id"]),
                        "reason": str(play_rule["reason"]),
                    }
                )
            _block(item, play_rule, {"limit": play_limit, "playbook_ordinal": ordinal})
            continue

        # 2) per-run action limit
        if act_rule:
            consulted.add(str(act_rule["guard_id"]))
        if act_enforced and action_ordinal > act_limit:
            _block(item, act_rule, {"limit": act_limit, "action_ordinal": action_ordinal})
            continue

        # 3) cooldown. Attempts, not deliveries: an action that keeps failing
        # must not be retried on every sweep tick.
        if cooldown_rule:
            consulted.add(str(cooldown_rule["guard_id"]))
        cooldown_hours = float(spec.get("cooldown_hours", 0.0) or 0.0)
        last_attempt = max(attempts.get(action_name, []), default=None)
        hours_since = (
            round((moment - last_attempt).total_seconds() / 3600.0, 2) if last_attempt else None
        )
        if (
            cooldown_enforced
            and cooldown_hours > 0.0
            and hours_since is not None
            and hours_since < cooldown_hours
        ):
            _block(
                item,
                cooldown_rule,
                {
                    "cooldown_hours": cooldown_hours,
                    "hours_since_last": hours_since,
                    "last_run_at": last_attempt.isoformat(),
                },
            )
            continue

        # 4) per-day delivery cap
        if cap_rule:
            consulted.add(str(cap_rule["guard_id"]))
        cap = int(spec.get("max_per_day", 0) or 0)
        delivered_today = delivered.get(action_name, 0)
        if cap_enforced and cap > 0 and delivered_today >= cap:
            _block(item, cap_rule, {"limit": cap, "delivered_today": delivered_today})
            continue

        # 5) daily budget, committed through the plan
        metric = str(spec.get("spend_metric") or "")
        # Clamped at 0: a negative spend in a plan row would otherwise *release*
        # budget and let later actions exceed the limit. ``plan_recovery_actions``
        # already clamps, but this is a public entry point and the guard must not
        # depend on its caller's arithmetic.
        spend = max(0.0, round(float(item.get("spend", 0.0) or 0.0), 2))
        limit = float(budget_limits.get(metric, 0.0) or 0.0)
        if metric:
            budget_rule = _guard_rule("daily_budget", subject="metric", metric=metric)
            if budget_rule:
                consulted.add(str(budget_rule["guard_id"]))
        if budget_enforced and metric and limit > 0.0:
            already = round(budget_committed.get(metric, 0.0), 2)
            if round(already + spend, 2) > limit:
                _budget_block(item, metric, spend, limit)
                continue
            budget_committed[metric] = round(already + spend, 2)
        elif metric and spend > 0.0:
            budget_committed[metric] = round(budget_committed.get(metric, 0.0) + spend, 2)

        # allowed -> the post-run position now includes this action
        if cap > 0:
            delivered[action_name] = delivered_today + 1
        entry = _base(item)
        entry.update(
            {
                "allowed": True,
                "guard_id": "",
                "status": "allowed",
                "reason": "",
                "detail": {
                    "cooldown_hours": cooldown_hours,
                    "hours_since_last": hours_since,
                    "max_per_day": cap,
                    "delivered_today": delivered[action_name],
                    "spend_metric": metric or None,
                    "spend": spend,
                },
            }
        )
        decisions.append(entry)

    allowed = [entry for entry in decisions if entry["allowed"]]
    blocked = [entry for entry in decisions if not entry["allowed"]]
    rule_reports = [_guard_rule_reasons(rule) for rule in RECOVERY_GUARD_RULES]
    for report in rule_reports:
        report["consulted"] = report["guard_id"] in consulted
    return {
        "generated_at": moment,
        "enforced": bool(enforce),
        "lookback_hours": round(float(lookback_hours), 2),
        "planned_actions": len(planned),
        "allowed_actions": len(allowed),
        "blocked_actions": len(blocked),
        "playbooks_allowed": len({entry["playbook_id"] for entry in allowed}),
        "suppressed_playbooks": suppressed,
        "decisions": decisions,
        "rules": rule_reports,
        "budget": {
            metric: {
                "limit": limit,
                "already_committed": round(spend_by_metric.get(metric, 0.0), 2),
                "committed_with_plan": round(budget_committed.get(metric, 0.0), 2),
                "remaining": round(max(0.0, limit - budget_committed.get(metric, 0.0)), 2),
            }
            for metric, limit in sorted(budget_limits.items())
        },
        "usage": {
            name: {
                "cooldown_hours": float(spec.get("cooldown_hours", 0.0) or 0.0),
                "max_per_day": int(spec.get("max_per_day", 0) or 0),
                "attempts_recorded": len(attempts.get(name, [])),
                "delivered_today": delivered.get(name, 0),
                "last_run_at": (
                    max(attempts[name]).isoformat() if attempts.get(name) else None
                ),
                "history_rows": history_actions.get(name, 0),
            }
            for name, spec in RECOVERY_ACTION_SPEC_BY_NAME.items()
        },
        "history_rows": len(rows),
        "unregistered_actions": sorted(
            {name for name in history_actions if name not in RECOVERY_ACTION_SPEC_BY_NAME}
        ),
        "summary": (
            f"{len(allowed)}/{len(planned)} action(s) allowed; {len(blocked)} blocked"
            + (f"; {len(suppressed)} playbook(s) suppressed" if suppressed else "")
            + ("" if enforce else " (not enforced)")
        ),
    }


def build_recovery_governance_audit() -> dict[str, Any]:
    """Static audit of the governance layer itself.

    Pure config inspection: it answers "is the guard layer complete?" without
    running anything, which is the question an operator asks when a sweep is
    misbehaving and nobody wants to wait for the next pass to find out.
    """
    findings: list[dict[str, Any]] = []
    for name in RECOVERY_REGISTRY_DRIFT["handlers_without_spec"]:
        findings.append(
            {
                "severity": "critical",
                "code": "handler_without_spec",
                "action": name,
                "detail": "handler is dispatchable but declares no policy, so no guard can bound it",
            }
        )
    for name in RECOVERY_REGISTRY_DRIFT["specs_without_handler"]:
        findings.append(
            {
                "severity": "critical",
                "code": "spec_without_handler",
                "action": name,
                "detail": "spec declares a policy for an action that cannot be dispatched",
            }
        )
    for spec in RECOVERY_ACTION_SPECS:
        name = str(spec["action"])
        missing = [field for field in RECOVERY_GUARDED_FIELDS if not spec.get(field)]
        if spec.get("mutates_state") and missing:
            findings.append(
                {
                    "severity": "critical",
                    "code": "ungoverned_writing_action",
                    "action": name,
                    "detail": (
                        "action writes state and declares no "
                        + "/".join(missing)
                        + ", so an automated sweep can repeat it without bound"
                    ),
                }
            )
        elif missing:
            findings.append(
                {
                    "severity": "warning",
                    "code": "ungoverned_action",
                    "action": name,
                    "detail": (
                        "action declares no " + "/".join(missing) + ", so only the per-run limits apply"
                    ),
                }
            )
    playbook_actions = {
        str(action.get("action", ""))
        for rows in RECOVERY_PLAYBOOK_SETS.values()
        for playbook in rows
        for action in playbook.get("actions", [])
    }
    for name in sorted(playbook_actions - set(RECOVERY_ACTION_REGISTRY)):
        findings.append(
            {
                "severity": "critical",
                "code": "playbook_action_unroutable",
                "action": name,
                "detail": "a configured playbook references an action with no handler",
            }
        )
    for name in sorted(set(RECOVERY_ACTION_REGISTRY) - playbook_actions):
        findings.append(
            {
                "severity": "info",
                "code": "action_unreferenced",
                "action": name,
                "detail": (
                    "registered action is not referenced by any playbook; reachable "
                    "only by explicit request"
                ),
            }
        )
    severities = [str(finding["severity"]) for finding in findings]
    return {
        "generated_at": datetime.now(timezone.utc),
        "action_count": len(RECOVERY_ACTION_SPECS),
        "guard_count": len(RECOVERY_GUARD_RULES),
        "playbook_count": sum(len(rows) for rows in RECOVERY_PLAYBOOK_SETS.values()),
        "writing_actions": list(RECOVERY_STATEFUL_ACTIONS),
        "budgeted_metrics": sorted(
            {
                str(spec["spend_metric"])
                for spec in RECOVERY_ACTION_SPECS
                if spec.get("spend_metric")
            }
        ),
        "registry_drift": {key: list(value) for key, value in RECOVERY_REGISTRY_DRIFT.items()},
        "findings": findings,
        "counts_by_severity": {
            "critical": severities.count("critical"),
            "warning": severities.count("warning"),
            "info": severities.count("info"),
        },
        "healthy": not any(severity == "critical" for severity in severities),
        "summary": (
            f"{len(RECOVERY_ACTION_SPECS)} action(s), {len(RECOVERY_GUARD_RULES)} guard(s), "
            f"{severities.count('critical')} critical finding(s)"
        ),
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def _load_recent_chat_rows(db: AsyncSession, user_id: int, window_days: int) -> list[Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    result = await db.execute(
        select(models.ChatHistory)
        .where(models.ChatHistory.user_id == int(user_id))
        .where(models.ChatHistory.timestamp >= cutoff)
        .order_by(desc(models.ChatHistory.timestamp))
    )
    return list(result.scalars().all())


async def _load_recent_bookings(db: AsyncSession, user_id: int, window_days: int) -> list[Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    result = await db.execute(
        select(models.Booking)
        .where(models.Booking.user_id == int(user_id))
        .where(models.Booking.created_at >= cutoff)
        .order_by(desc(models.Booking.created_at))
    )
    return list(result.scalars().all())


async def _load_recovery_action_history(
    db: AsyncSession,
    user_id: int,
    since: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Load this customer's recent ``RecoveryAction`` rows as flat guard input.

    The window defaults to ``RECOVERY_GUARD_HISTORY_HOURS``, which is the longer
    of the daily window and the longest declared cooldown. Loading only the daily
    window would make every cooldown silently inert for any action whose cooldown
    exceeds it -- the failure mode where the guard reports "allowed" because it
    never saw the earlier run.

    ``result_json`` is parsed for the budgeted spend rather than recomputed, so
    the budget is reconciled against what the ledger row actually recorded.
    """
    window_start = (since or datetime.now(timezone.utc)) - timedelta(
        hours=RECOVERY_GUARD_HISTORY_HOURS
    )
    result = await db.execute(
        select(models.RecoveryAction)
        .where(models.RecoveryAction.user_id == int(user_id))
        .where(models.RecoveryAction.created_at >= window_start)
        .order_by(desc(models.RecoveryAction.created_at))
    )
    rows: list[dict[str, Any]] = []
    for record in result.scalars().all():
        try:
            stored = json.loads(getattr(record, "result_json", "") or "{}")
        except (TypeError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        rows.append(
            recovery_history_row(
                getattr(record, "action", ""),
                playbook_id=str(getattr(record, "playbook_id", "") or ""),
                status=str(getattr(record, "status", "") or ""),
                at=getattr(record, "created_at", None),
                spend=stored.get("points_credited"),
                row_id=getattr(record, "id", None),
                reference=str(getattr(record, "reference", "") or ""),
                failure_reason=str(getattr(record, "failure_reason", "") or ""),
            )
        )
    return rows


async def _load_recovery_context(
    db: AsyncSession,
    user_id: int,
    window_days: int,
    *,
    chat_rows: Optional[list[Any]] = None,
    bookings: Optional[list[Any]] = None,
    sentiment: Optional[Sentiment] = None,
    summary: Optional[InteractionSummary] = None,
    dissatisfaction: Optional[DissatisfactionRecoveryReport] = None,
) -> dict[str, Any]:
    """Resolve the realtime recovery context, loading only what is missing.

    Callers may pass a prebuilt ``summary``/``dissatisfaction`` (or rows +
    sentiment) to skip recomputation. Extracted from the orchestrator so the
    admin preview surfaces resolve their context through the identical path
    instead of a second, subtly different one.
    """
    if summary is None:
        if chat_rows is None:
            chat_rows = await _load_recent_chat_rows(db, int(user_id), window_days)
        if bookings is None:
            bookings = await _load_recent_bookings(db, int(user_id), window_days)
        if sentiment is None and chat_rows:
            first = chat_rows[0]
            first_message = getattr(first, "message", None)
            if first_message:
                sentiment = analyze_sentiment(first_message)
        summary = build_summary(int(user_id), list(chat_rows or []), list(bookings or []), sentiment)
    if dissatisfaction is None:
        dissatisfaction = build_dissatisfaction_recovery_report(summary, sentiment)
    return {
        "summary": summary,
        "sentiment": sentiment,
        "dissatisfaction": dissatisfaction,
        "chat_rows": list(chat_rows or []),
        "bookings": list(bookings or []),
        "context": await _with_complaint_history(
            db, int(user_id), build_realtime_recovery_context(summary, sentiment, dissatisfaction)
        ),
    }


async def _with_complaint_history(
    db: AsyncSession, user_id: int, context: dict[str, Any]
) -> dict[str, Any]:
    """Add the complaint count the `communication_suppression` pack needs.

    `suppress_recent_complaint` in `rule_engine.RULE_PACKS` has tested
    `complaints_last_30d >= 2` since it was written, and no producer anywhere
    emitted that key -- so the rule could not fire, and the pack had no caller
    besides its own catalog entry. A dead suppression rule is the worst kind of
    dead rule: it looks like a safeguard against pestering a customer who has
    already complained twice, and it has never once done so.

    The count is sourced from the complaint cases rather than inferred from
    signals, because "complained twice" is a fact about the complaint ledger and
    not a thing readable off a sentiment score. A failure to count leaves the
    key *absent* rather than setting it to zero, because `evaluate_when` fails
    closed on an absent field -- a transient database problem must not be able
    to masquerade as "no complaints" and silently switch the safeguard off.
    """
    enriched = dict(context)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        result = await db.execute(
            select(func.count(models.ComplaintCase.id)).where(
                models.ComplaintCase.user_id == int(user_id),
                models.ComplaintCase.opened_at >= cutoff,
            )
        )
        enriched["complaints_last_30d"] = int((result.first() or [0])[0] or 0)
    except SQLAlchemyError as exc:
        # Narrow on purpose. This was `except Exception`, and that is how a
        # missing `func` import shipped: the NameError was caught, recorded as
        # `complaints_last_30d_error`, and the key silently never appeared -- so
        # `suppress_recent_complaint` stayed dead while the code looked like it
        # was supplying the key. A database problem should degrade this feed; a
        # programming error must be loud.
        enriched["complaints_last_30d_error"] = str(exc)
    return enriched


async def run_recovery_playbooks(
    db: AsyncSession,
    user_id: int,
    *,
    chat_rows: Optional[list[Any]] = None,
    bookings: Optional[list[Any]] = None,
    sentiment: Optional[Sentiment] = None,
    summary: Optional[InteractionSummary] = None,
    dissatisfaction: Optional[DissatisfactionRecoveryReport] = None,
    window_days: int = 30,
    dry_run: bool = False,
    policy_snapshot: Optional[PolicyScoreSnapshot] = None,
    effective_date: Any = None,
    guard_history: Optional[list[dict[str, Any]]] = None,
    enforce_guards: bool = True,
    guard_lookback_hours: float = RECOVERY_GUARD_LOOKBACK_HOURS,
) -> dict[str, Any]:
    """Evaluate recovery playbooks against the realtime context and execute.

    Callers may pass prebuilt ``summary``/``dissatisfaction`` (or rows +
    sentiment) to skip recomputation; otherwise the orchestrator loads the
    user's recent interaction window itself.

    Guards run in both modes. A dry run that ignores them would answer "what
    would you send" while the real pass silently sends nothing, which is the
    opposite of what a dry run is for.

    A guard rejection is recorded like any other outcome, with status ``skipped``
    (``would_skip`` in a dry run) and ``failure_reason`` naming the guard. The
    audit row is the point: without it a customer who was correctly *not* re-
    credited looks identical to a customer the system forgot about.
    """
    now = datetime.now(timezone.utc)
    resolved = await _load_recovery_context(
        db,
        int(user_id),
        window_days,
        chat_rows=chat_rows,
        bookings=bookings,
        sentiment=sentiment,
        summary=summary,
        dissatisfaction=dissatisfaction,
    )
    context = resolved["context"]
    matched = evaluate_recovery_playbooks(context, effective_date=effective_date)
    plan = plan_recovery_actions(context, effective_date=effective_date)

    history = guard_history
    if history is None:
        history = await _load_recovery_action_history(db, int(user_id), now)
    guards = evaluate_recovery_guards(
        plan,
        history,
        now=now,
        lookback_hours=guard_lookback_hours,
        enforce=enforce_guards,
    )
    decisions = {int(entry["index"]): entry for entry in guards["decisions"]}

    executed_actions: list[dict[str, Any]] = []
    policy_adjustment_preview: Optional[dict[str, Any]] = None
    escalation_sequence = 0
    for item in plan:
        playbook_id = str(item.get("playbook_id", "unknown"))
        action_name = str(item.get("action", ""))
        params = item.get("params", {})
        decision = decisions.get(int(item.get("index", -1)), {})
        payload = {
            "playbook_id": playbook_id,
            "action": action_name,
            "params": params,
        }
        result: dict[str, Any] = {}
        failure_reason = ""
        guard: dict[str, Any] = {
            "allowed": bool(decision.get("allowed", True)),
            "guard_id": str(decision.get("guard_id", "")),
            "reason": str(decision.get("reason", "")),
            "detail": decision.get("detail", {}),
        }
        status = "executed"
        recorded: Optional[models.RecoveryAction] = None

        if not guard["allowed"]:
            # The system declining to act is not an error, so it gets its own
            # status rather than being reported as a failure.
            status = RECOVERY_GUARD_DRY_RUN_SKIP_STATUS if dry_run else RECOVERY_GUARD_SKIP_STATUS
            failure_reason = guard["reason"]
            result = {"guard_rejected": True, "guard_id": guard["guard_id"]}
        elif dry_run:
            status = "would_execute"
            result = {"preview": True}
        else:
            handler = RECOVERY_ACTION_REGISTRY.get(action_name)
            if handler is None:
                raise ValueError(f"unknown recovery action {action_name!r}")
            try:
                outcome = handler(
                    RecoveryActionContext(
                        db=db,
                        user_id=int(user_id),
                        params=params,
                        context=context,
                        policy_snapshot=policy_snapshot,
                        sequence=escalation_sequence + 1,
                        dry_run=dry_run,
                        now=now,
                    )
                )
                # Handlers are declared async or sync by whether they touch the
                # session; the registry does not care which, so dispatch does not
                # either. A sync handler is by far the common case (only
                # ``credit_points`` writes), and forcing every one of them async
                # would make the no-I/O actions lie about themselves.
                result = await outcome if inspect.isawaitable(outcome) else outcome
                if not isinstance(result, dict):  # pragma: no cover - handler contract
                    result = {"value": result}
                # Escalation references are a monotonic sequence over the run, so
                # only a dispatched escalation advances it. Incrementing here
                # rather than reading a counter out of the result keeps the
                # counter correct now that dispatch is registry-driven.
                if action_name == "escalate_ticket":
                    escalation_sequence += 1
                if action_name == "adjust_policy_score":
                    policy_adjustment_preview = result.get("preview")
                status = "executed"
            except Exception as exc:  # defensive: failed actions never break the sweep
                logger.warning("Recovery action %s/%s failed: %s", playbook_id, action_name, exc)
                status = "failed"
                failure_reason = str(exc)
                result = {}

        if not dry_run:
            recorded = models.RecoveryAction(
                user_id=int(user_id),
                playbook_id=playbook_id,
                action=action_name,
                status=status,
                payload_json=json.dumps(payload, default=str),
                result_json=json.dumps(result, default=str),
                reference=str(
                    result.get("reference") or result.get("ticket_reference") or ""
                ),
                failure_reason=failure_reason,
            )
            db.add(recorded)

        executed_actions.append(
            {
                "id": int(getattr(recorded, "id", 0) or 0),
                "playbook_id": playbook_id,
                "playbook_set": str(item.get("playbook_set", "core")),
                "action": action_name,
                "status": status,
                "payload": payload,
                "result": result,
                "reference": str(
                    result.get("reference") or result.get("ticket_reference") or ""
                ),
                "failure_reason": failure_reason,
                "guard": guard,
                "executed_at": now,
            }
        )

    if not dry_run and (executed_actions or matched):
        await db.commit()

    matched_payload = [
        {
            "playbook_id": str(entry.get("playbook_id", "")),
            "name": str(entry.get("name", "")),
            "priority": int(entry.get("priority", 100)),
            "description": str(entry.get("description", "")),
            "matched": True,
            "matched_fields": entry.get("matched_fields", {}),
            "playbook_set": str(entry.get("playbook_set", "core")),
            "actions": [{"action": a.get("action", ""), "params": a.get("params", {})} for a in entry.get("actions", [])],
        }
        for entry in matched
    ]
    mode = "would_execute" if dry_run else "executed"
    blocked = guards["blocked_actions"]
    summary_text = (
        f"{len(matched)} playbook(s) matched ({', '.join(m['playbook_id'] for m in matched_payload) or 'none'}); "
        f"{len(executed_actions)} action(s) {mode}"
        + (f"; {blocked} blocked by guard(s)" if blocked else "")
        + "."
    )
    return {
        "generated_at": now,
        "user_id": int(user_id),
        "window_days": window_days,
        "dry_run": bool(dry_run),
        "auto_recovery_enabled": AUTO_RECOVERY_ENABLED,
        "context": context,
        "matched_playbooks": matched_payload,
        "executed_actions": executed_actions,
        "policy_adjustment_preview": policy_adjustment_preview,
        "guards": guards,
        "plan": plan,
        "summary": summary_text,
    }


# ---------------------------------------------------------------------------
# Optional automated sweep (env-gated)
# ---------------------------------------------------------------------------


async def run_auto_recovery_pass(db: AsyncSession, *, window_days: int = 30) -> dict[str, Any]:
    """Scan every user's recent interaction window and trigger matched playbooks.

    ``outcomes`` is the unchanged shape (one row per user with a matching
    playbook). ``guarded`` is the additive counterpart for the users a guard
    stopped: without it, a sweep where every customer is correctly blocked for
    cooldown and nothing at all is delivered is indistinguishable from a sweep
    that found nothing to do.
    """
    result = await db.execute(select(models.User.id))
    user_ids = [int(uid) for uid in result.scalars().all()]
    outcomes: list[dict[str, Any]] = []
    guarded: list[dict[str, Any]] = []
    spend_by_metric: dict[str, float] = {}
    blocked_total = 0
    for user_id in user_ids:
        report = await run_recovery_playbooks(db, user_id, window_days=window_days)
        guards = report.get("guards") or {}
        blocked = int(guards.get("blocked_actions", 0) or 0)
        blocked_total += blocked
        for action in report["executed_actions"]:
            if action["status"] != "executed":
                continue
            metric = str(RECOVERY_ACTION_SPEC_BY_NAME.get(action["action"], {}).get("spend_metric") or "")
            value = round(float(action["result"].get("points_credited", 0.0) or 0.0), 2)
            if metric and value > 0.0:
                spend_by_metric[metric] = round(spend_by_metric.get(metric, 0.0) + value, 2)
        if not report["matched_playbooks"]:
            continue
        if blocked:
            guarded.append(
                {
                    "user_id": user_id,
                    "blocked_actions": blocked,
                    "playbooks": [item["playbook_id"] for item in report["matched_playbooks"]],
                    "guards": sorted(
                        {
                            str(action["guard"].get("guard_id", ""))
                            for action in report["executed_actions"]
                            if action["guard"] and not action["guard"].get("allowed")
                            and action["guard"].get("guard_id")
                        }
                    ),
                }
            )
        outcomes.append(
            {
                "user_id": user_id,
                "executed_actions": len(report["executed_actions"]),
                "playbooks": [item["playbook_id"] for item in report["matched_playbooks"]],
                "blocked_actions": blocked,
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "users_scanned": len(user_ids),
        "users_recovered": len(outcomes),
        "outcomes": outcomes,
        "guarded": guarded,
        "blocked_actions": blocked_total,
        "spend": {metric: value for metric, value in sorted(spend_by_metric.items())},
        "guard_rules_active": [
            str(rule["guard_id"])
            for rule in RECOVERY_GUARD_RULES
            if rule.get("enabled", True)
        ],
    }


async def auto_recovery_worker_forever(
    session_factory=None,
    interval_seconds: Optional[float] = None,
) -> None:
    """Long-running sweep worker; starts only when ``CSERVICE_AUTO_RECOVERY=1``.

    A failed pass is logged and skipped, so the worker never kills the process.
    """
    from app.db import SessionLocal  # late import keeps module imports acyclic

    factory = session_factory or SessionLocal
    interval = max(1.0, float(interval_seconds if interval_seconds is not None else AUTO_RECOVERY_INTERVAL_SECONDS))
    while True:
        try:
            async with factory() as session:
                await run_auto_recovery_pass(session)
            logger.debug("Auto recovery sweep completed")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Auto recovery sweep failed: %s", exc)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def build_recovery_playbook_catalog() -> dict[str, Any]:
    """Introspection payload for `/meta/scoring-catalog` and the admin endpoint.

    ``catalog_version`` and ``playbooks`` stay exactly the v1 core set: that pair
    is what downstream consumers pin on, and quietly widening a frozen version is
    how a pinned contract becomes a lie. The governance layer is published
    alongside under its own keys and its own version, so a client can tell "the
    catalog version changed" from "there is a new, separate subsystem".
    """
    def _rows(playbooks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "playbook_id": playbook["playbook_id"],
                "name": playbook["name"],
                "description": playbook["description"],
                "priority": playbook["priority"],
                "enabled": playbook.get("enabled", True),
                "when": playbook["when"],
                "actions": [dict(action) for action in playbook["actions"]],
            }
            for playbook in playbooks
        ]

    return {
        "catalog_version": RECOVERY_CORE_PLAYBOOK_SET_VERSION,
        "auto_recovery_enabled": AUTO_RECOVERY_ENABLED,
        "auto_recovery_env": "CSERVICE_AUTO_RECOVERY",
        "auto_recovery_interval_seconds": AUTO_RECOVERY_INTERVAL_SECONDS,
        "credit_action_kind": RECOVERY_CREDIT_KIND,
        "when_dsl": "shared rule_engine.evaluate_when (combinators, numeric/list/date ops)",
        "playbooks": _rows(RECOVERY_PLAYBOOKS),
        # --- additive governance layer (own version) -----------------------
        "governance_version": RECOVERY_GOVERNANCE_VERSION,
        "playbook_sets": {
            set_name: {
                "version": RECOVERY_PLAYBOOK_SET_VERSIONS[set_name],
                "playbook_count": len(rows),
                "playbook_ids": [str(row["playbook_id"]) for row in rows],
            }
            for set_name, rows in RECOVERY_PLAYBOOK_SETS.items()
        },
        "outreach_playbooks": _rows(RECOVERY_OUTREACH_PLAYBOOKS),
        "action_registry": [
            {
                "action": str(spec["action"]),
                "title": str(spec["title"]),
                "description": str(spec["description"]),
                "mutates_state": bool(spec["mutates_state"]),
                "idempotent": bool(spec["idempotent"]),
                "spend_metric": spec.get("spend_metric"),
                "max_per_day": int(spec.get("max_per_day", 0) or 0),
                "cooldown_hours": float(spec.get("cooldown_hours", 0.0) or 0.0),
                "reversible": bool(spec.get("reversible")),
                "compensation": spec.get("compensation"),
                "stateful": str(spec["action"]) in RECOVERY_STATEFUL_ACTIONS,
            }
            for spec in RECOVERY_ACTION_SPECS
        ],
        "guard_rules": [
            {
                "guard_id": str(rule["guard_id"]),
                "check": str(rule["check"]),
                "subject": str(rule.get("subject", "action")),
                "metric": rule.get("metric"),
                "max": rule.get("max"),
                "enabled": bool(rule.get("enabled", True)),
                "reason": str(rule.get("reason", "")),
                "counted_statuses": list(
                    RECOVERY_GUARD_HISTORY_RULES.get(str(rule["check"]), {}).get("counted_statuses", [])
                ),
            }
            for rule in RECOVERY_GUARD_RULES
        ],
        "guard_statuses": {
            "skip": RECOVERY_GUARD_SKIP_STATUS,
            "dry_run_skip": RECOVERY_GUARD_DRY_RUN_SKIP_STATUS,
            "reason_status": RECOVERY_GUARD_REASON_STATUS,
        },
        "guard_window": {
            "lookback_hours": RECOVERY_GUARD_LOOKBACK_HOURS,
            "history_hours": RECOVERY_GUARD_HISTORY_HOURS,
            "max_cooldown_hours": RECOVERY_GUARD_MAX_COOLDOWN_HOURS,
        },
        "callback_plans": [dict(plan) for plan in RECOVERY_CALLBACK_PLANS],
        "save_incentives": [dict(offer) for offer in RECOVERY_SAVE_INCENTIVES],
        "review_rules": [dict(rule) for rule in RECOVERY_REVIEW_RULES],
        "stage_rules": [dict(rule) for rule in RECOVERY_STAGE_RULES],
        "registry_drift": {
            key: list(value) for key, value in RECOVERY_REGISTRY_DRIFT.items()
        },
    }


# ---------------------------------------------------------------------------
# Recovery action analytics (pure)
# ---------------------------------------------------------------------------


def build_recovery_action_analytics(
    history: list[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    window_days: int = 7,
) -> dict[str, Any]:
    """Roll recovery action history up into the numbers an operator asks for.

    The audit table answers "what happened to this one customer"; this answers
    "is the automated recovery program working, and is it spending what we think
    it is". The distinction matters because the failure mode of an unbounded
    goodwill credit is invisible per-customer and obvious in aggregate.

    ``guarded_ratio`` is the headline: the share of attempts the guard layer
    stopped. A ratio near zero on a high-volume deployment means the limits are
    not real, and a ratio near one means the playbooks are firing far more
    often than the limits intend.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=max(1, int(window_days)))
    rows = [row for row in history if isinstance(row.get("at"), datetime) and row["at"] >= cutoff]

    by_status: dict[str, int] = {}
    by_action: dict[str, dict[str, Any]] = {}
    by_playbook: dict[str, int] = {}
    by_guard: dict[str, int] = {}
    by_day: dict[str, int] = {}
    spend_by_metric: dict[str, float] = {}
    failures: list[dict[str, Any]] = []
    registered = set(RECOVERY_ACTION_SPEC_BY_NAME)
    unregistered: set[str] = set()

    for row in rows:
        action_name = str(row.get("action", ""))
        status = str(row.get("status", "")) or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
        by_day[row["at"].date().isoformat()] = by_day.get(row["at"].date().isoformat(), 0) + 1
        playbook_id = str(row.get("playbook_id", "")) or "unknown"
        by_playbook[playbook_id] = by_playbook.get(playbook_id, 0) + 1
        if status == RECOVERY_GUARD_REASON_STATUS:
            guard_id = str(row.get("guard_id") or "unknown")
            by_guard[guard_id] = by_guard.get(guard_id, 0) + 1
        entry = by_action.setdefault(
            action_name,
            {
                "attempts": 0,
                "executed": 0,
                "failed": 0,
                "blocked": 0,
                "spend": 0.0,
                "spend_metric": row.get("spend_metric"),
                "registered": action_name in registered,
                "mutates_state": bool(
                    (RECOVERY_ACTION_SPEC_BY_NAME.get(action_name) or {}).get("mutates_state")
                ),
            },
        )
        entry["attempts"] += 1
        if status == "executed":
            entry["executed"] += 1
        elif status == "failed":
            entry["failed"] += 1
        elif status in {RECOVERY_GUARD_SKIP_STATUS, RECOVERY_GUARD_REASON_STATUS}:
            entry["blocked"] += 1
        if action_name not in registered:
            unregistered.add(action_name)
        metric = str(row.get("spend_metric") or "")
        value = round(float(row.get("spend", 0.0) or 0.0), 2)
        if metric and value > 0.0 and status == "executed":
            spend_by_metric[metric] = round(spend_by_metric.get(metric, 0.0) + value, 2)
            entry["spend"] = round(float(entry["spend"]) + value, 2)
        if status == "failed":
            failures.append(
                {
                    "id": int(row.get("id", 0) or 0),
                    "playbook_id": playbook_id,
                    "action": action_name,
                    "failure_reason": str(row.get("failure_reason", "")),
                    "at": row["at"].isoformat(),
                }
            )

    total = len(rows)
    blocked = by_status.get(RECOVERY_GUARD_SKIP_STATUS, 0) + by_status.get(RECOVERY_GUARD_REASON_STATUS, 0)
    executed = by_status.get("executed", 0)
    failed = by_status.get("failed", 0)
    budget_limit = 0.0
    for rule in RECOVERY_GUARD_RULES:
        if str(rule.get("check")) == "daily_budget" and str(rule.get("metric")) == "points":
            budget_limit = float(rule.get("max", 0.0) or 0.0)
    points_spent = float(spend_by_metric.get("points", 0.0))
    return {
        "generated_at": moment,
        "window_days": int(window_days),
        "rows_in_window": total,
        "rows_total": len(history),
        "by_status": dict(sorted(by_status.items())),
        "by_action": {name: by_action[name] for name in sorted(by_action)},
        "by_playbook": dict(sorted(by_playbook.items(), key=lambda item: (-item[1], item[0]))),
        "by_guard": dict(sorted(by_guard.items(), key=lambda item: (-item[1], item[0]))),
        "by_day": dict(sorted(by_day.items())),
        "spend": {
            metric: {
                "spent": value,
                "daily_budget": budget_limit if metric == "points" else None,
                "budget_utilisation": (
                    round(value / budget_limit, 4) if metric == "points" and budget_limit else None
                ),
            }
            for metric, value in sorted(spend_by_metric.items())
        },
        "executed": executed,
        "failed": failed,
        "blocked": blocked,
        "guarded_ratio": round(blocked / total, 4) if total else 0.0,
        "failure_ratio": round(failed / total, 4) if total else 0.0,
        "success_ratio": round(executed / total, 4) if total else 0.0,
        "recent_failures": failures[:10],
        "unregistered_actions": sorted(unregistered),
        "top_guards": [
            {"guard_id": guard_id, "count": count}
            for guard_id, count in sorted(by_guard.items(), key=lambda item: (-item[1], item[0]))[:5]
        ],
        "summary": (
            f"{total} row(s) over {window_days}d: {executed} executed, {blocked} blocked, "
            f"{failed} failed"
            + (f"; {points_spent:g} point(s) credited" if points_spent else "")
        ),
    }


def build_recovery_outreach_plan(
    context: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    locale: str = "global",
) -> dict[str, Any]:
    """Resolve the whole outreach story for a recovery context in one payload.

    Answers "if we decide to reach out to this customer, what does the plan look
    like" without running a single playbook, which is what an operator needs
    before approving an outreach and what the automated set uses internally.
    Deliberately issues nothing: it is a composed preview.
    """
    moment = now or datetime.now(timezone.utc)
    strategy = resolve_recovery_outreach_strategy(context, locale=locale)
    callback = resolve_recovery_callback_plan(context, now=moment)
    incentive = resolve_recovery_incentive(context, now=moment)
    review = resolve_recovery_review_priority(context)
    readiness = str(context.get("recovery_readiness", "low"))
    return {
        "generated_at": moment,
        "user_id": int(context.get("user_id", 0) or 0),
        "recovery_readiness": readiness,
        "lifecycle_stage": recovery_lifecycle_stage(context),
        "communication": {**strategy, "dispatched": False},
        "callback": {**callback, "scheduled": False},
        "incentive": incentive,
        "review": {
            "rule_id": review["rule_id"],
            "priority": review["priority"],
            "sla_hours": review["sla_hours"],
            "queued": False,
        },
        "issued": False,
        "primary_risks": [str(risk) for risk in (context.get("primary_risks") or [])],
        "risk_areas": [str(area) for area in (context.get("risk_areas") or [])],
        "summary": (
            f"readiness={readiness}: {strategy['channel']}/{strategy['tone']} outreach, "
            f"callback due {callback['due_at']} via {callback['owner_team']}, "
            f"review priority {review['priority']}; nothing issued"
        ),
    }


# ---------------------------------------------------------------------------
# Async entry points
# ---------------------------------------------------------------------------
#
# The analytics and planning above are pure and take context/rows. These load and
# delegate, so a router never learns which stage does the work.


async def build_recovery_guard_report_for_user(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
) -> dict[str, Any]:
    """Live guard verdicts for what this customer would do right now.

    Evaluated with ``enforce=False`` on purpose: this is the *preview* surface.
    It reports what the guards would say about the current context so an operator
    can see a pending cooldown or an exhausted budget before triggering a run,
    rather than after.
    """
    resolved = await _load_recovery_context(db, int(user_id), window_days)
    now = datetime.now(timezone.utc)
    plan = plan_recovery_actions(resolved["context"])
    history = await _load_recovery_action_history(db, int(user_id), now)
    evaluation = evaluate_recovery_guards(plan, history, now=now, enforce=False)
    usage = evaluate_recovery_guards(plan, history, now=now, enforce=True)
    return {
        "generated_at": now,
        "user_id": int(user_id),
        "window_days": int(window_days),
        "context": resolved["context"],
        "plan": plan,
        "evaluation": evaluation,
        "enforced_evaluation": usage,
        "audit": build_recovery_governance_audit(),
        "summary": evaluation["summary"],
    }


async def build_recovery_action_analytics_for_user(
    db: AsyncSession,
    user_id: int,
    window_days: int = 7,
) -> dict[str, Any]:
    """Rollup of this customer's recovery action history over a window."""
    now = datetime.now(timezone.utc)
    history = await _load_recovery_action_history(db, int(user_id), now)
    analytics = build_recovery_action_analytics(history, now=now, window_days=window_days)
    analytics["user_id"] = int(user_id)
    return analytics


async def build_recovery_outreach_plan_for_user(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
    *,
    locale: str = "global",
) -> dict[str, Any]:
    """Composed outreach preview for one customer. Issues nothing."""
    resolved = await _load_recovery_context(db, int(user_id), window_days)
    plan = build_recovery_outreach_plan(
        {**resolved["context"], "user_id": int(user_id)},
        now=datetime.now(timezone.utc),
        locale=locale,
    )
    plan["window_days"] = int(window_days)
    return plan
