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
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
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
    """
    signals = list(getattr(dissatisfaction, "recovery_signals", None) or [])
    peak_intensity = max([float(getattr(signal, "intensity", 0.0) or 0.0) for signal in signals] or [0.0])
    metadata = dict(getattr(summary, "metadata", None) or {})
    booking_states = dict(metadata.get("booking_states", {}) or {})
    return {
        "recovery_readiness": str(dissatisfaction.recovery_readiness),
        "dissatisfaction_score": round(float(dissatisfaction.dissatisfaction_score), 2),
        "sentiment_label": str(sentiment.label) if sentiment else "neutral",
        "sentiment_score": round(float(getattr(sentiment, "score", 0.0) or 0.0), 4),
        "churn_risk": str(summary.churn_risk),
        "loyalty_score": round(float(summary.loyalty_score), 2),
        "monetization_readiness": round(float(summary.monetization_readiness), 2),
        "value_tier": str(summary.value_tier),
        "primary_risks": list(getattr(dissatisfaction, "primary_risks", None) or []),
        "risk_areas": [str(getattr(signal, "area", "")) for signal in signals],
        "peak_intensity": round(peak_intensity, 4),
        "repeated_messages": int(metadata.get("repeated_messages", 0) or 0),
        "booking_states": booking_states,
    }


def evaluate_recovery_playbooks(
    context: dict[str, Any],
    *,
    effective_date: Any = None,
) -> list[dict[str, Any]]:
    """Return every enabled playbook whose ``when`` rule matches the context.

    Matched dicts include the full playbook spec plus ``matched_fields``
    (the field-level evidence the shared engine produced).
    """
    matched: list[dict[str, Any]] = []
    for playbook in sorted(RECOVERY_PLAYBOOKS, key=lambda p: int(p.get("priority", 100))):
        if playbook.get("enabled", True) is False:
            continue
        ok, fields = evaluate_when(playbook.get("when", {}), context, effective_date=effective_date)
        if ok:
            entry = dict(playbook)
            entry["matched_fields"] = fields
            matched.append(entry)
    return matched


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


def build_escalation_result(user_id: int, params: dict[str, Any], sequence: int) -> dict[str, Any]:
    """Generate the escalation ticket reference for the logged action."""
    return {
        "ticket_reference": f"ESC-{int(user_id)}-{int(sequence):03d}",
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
    adjusted = replace(
        snapshot,
        access_score=adjusted_access,
        customer_score=adjusted_customer,
        system_score=adjusted_system,
    )
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
) -> dict[str, Any]:
    """Evaluate recovery playbooks against the realtime context and execute.

    Callers may pass prebuilt ``summary``/``dissatisfaction`` (or rows +
    sentiment) to skip recomputation; otherwise the orchestrator loads the
    user's recent interaction window itself.
    """
    now = datetime.now(timezone.utc)
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

    context = build_realtime_recovery_context(summary, sentiment, dissatisfaction)
    matched = evaluate_recovery_playbooks(context, effective_date=effective_date)

    executed_actions: list[dict[str, Any]] = []
    policy_adjustment_preview: Optional[dict[str, Any]] = None
    escalation_sequence = 0
    for playbook in matched:
        playbook_id = str(playbook.get("playbook_id", "unknown"))
        for action in playbook.get("actions", []):
            action_name = str(action.get("action", ""))
            params = resolve_params(action.get("params"), context)
            payload = {
                "playbook_id": playbook_id,
                "action": action_name,
                "params": params,
            }
            result: dict[str, Any] = {}
            failure_reason = ""
            status = "executed"
            recorded: Optional[models.RecoveryAction] = None
            if dry_run:
                status = "would_execute"
                result = {"preview": True}
            else:
                try:
                    if action_name == "credit_points":
                        result = await credit_recovery_points(
                            db,
                            int(user_id),
                            str(params.get("point_type", "loyalty_points")),
                            float(params.get("points", 0.0) or 0.0),
                            reference=str(params.get("reference", "recovery")),
                        )
                    elif action_name == "escalate_ticket":
                        escalation_sequence += 1
                        result = build_escalation_result(int(user_id), params, escalation_sequence)
                    elif action_name == "adjust_policy_score":
                        preview, applied = apply_policy_adjustment(policy_snapshot, params)
                        policy_adjustment_preview = preview
                        result = {"applied_deltas": applied, "preview_generated": preview is not None}
                    else:
                        raise ValueError(f"unknown recovery action {action_name!r}")
                    status = "executed"
                except Exception as exc:  # defensive: failed actions never break the sweep
                    logger.warning("Recovery action %s/%s failed: %s", playbook_id, action_name, exc)
                    status = "failed"
                    failure_reason = str(exc)
                    result = {}

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
                    "action": action_name,
                    "status": status,
                    "payload": payload,
                    "result": result,
                    "reference": str(
                        result.get("reference") or result.get("ticket_reference") or ""
                    ),
                    "failure_reason": failure_reason,
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
            "actions": [{"action": a.get("action", ""), "params": a.get("params", {})} for a in entry.get("actions", [])],
        }
        for entry in matched
    ]
    mode = "would_execute" if dry_run else "executed"
    summary_text = (
        f"{len(matched)} playbook(s) matched ({', '.join(m['playbook_id'] for m in matched_payload) or 'none'}); "
        f"{len(executed_actions)} action(s) {mode}."
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
        "summary": summary_text,
    }


# ---------------------------------------------------------------------------
# Optional automated sweep (env-gated)
# ---------------------------------------------------------------------------


async def run_auto_recovery_pass(db: AsyncSession, *, window_days: int = 30) -> dict[str, Any]:
    """Scan every user's recent interaction window and trigger matched playbooks."""
    result = await db.execute(select(models.User.id))
    user_ids = [int(uid) for uid in result.scalars().all()]
    outcomes: list[dict[str, Any]] = []
    for user_id in user_ids:
        report = await run_recovery_playbooks(db, user_id, window_days=window_days)
        if report["matched_playbooks"]:
            outcomes.append(
                {
                    "user_id": user_id,
                    "executed_actions": len(report["executed_actions"]),
                    "playbooks": [item["playbook_id"] for item in report["matched_playbooks"]],
                }
            )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "users_scanned": len(user_ids),
        "users_recovered": len(outcomes),
        "outcomes": outcomes,
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
    """Introspection payload for `/meta/scoring-catalog` and the admin endpoint."""
    return {
        "catalog_version": "recovery_playbooks_v1",
        "auto_recovery_enabled": AUTO_RECOVERY_ENABLED,
        "auto_recovery_env": "CSERVICE_AUTO_RECOVERY",
        "auto_recovery_interval_seconds": AUTO_RECOVERY_INTERVAL_SECONDS,
        "credit_action_kind": RECOVERY_CREDIT_KIND,
        "when_dsl": "shared rule_engine.evaluate_when (combinators, numeric/list/date ops)",
        "playbooks": [
            {
                "playbook_id": playbook["playbook_id"],
                "name": playbook["name"],
                "description": playbook["description"],
                "priority": playbook["priority"],
                "enabled": playbook.get("enabled", True),
                "when": playbook["when"],
                "actions": [dict(action) for action in playbook["actions"]],
            }
            for playbook in RECOVERY_PLAYBOOKS
        ],
    }