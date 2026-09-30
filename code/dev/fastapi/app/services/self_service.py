"""Customer self-service status, and making recovery actions customer-visible.

The gap
-------
The recovery program is thoroughly auditable *internally*: every action is a
``RecoveryAction`` row, and ``/chat/admin/recovery-analytics`` rolls it up. None
of that reaches the person it happened to. A customer whose sentiment dropped
and who was credited 120 goodwill points has no way to learn that, which means
the goodwill reads as a mystery balance change rather than as an apology --
which defeats the point of issuing it.

What this module adds
---------------------
1. :func:`build_customer_recovery_status` -- the customer's own view: what
   recovery decided, what it did, and what it deliberately did not do.
2. :func:`build_self_service_status` -- one dashboard combining recovery, a
   points forecast, and the policy posture in plain language.

Three decisions that are choices, not omissions
-----------------------------------------------
**A blocked action is shown, not hidden.** A `skipped` action means a guard
stopped us repeating something. Omitting it would make "we correctly did not
spam you" and "we forgot about you" render identically. So skips appear in
their own list with the guard named, and the customer-facing reason is the
translation from :data:`~app.services.customer_explain.RECOVERY_STATUS_PHRASES`
rather than the internal "daily budget would be exceeded".

**The customer-facing view is gated on a stated preference, defaulting to on.**
``show_recovery_activity`` defaults ``True``. A transparency feature hidden by
default is only discoverable after an incident, which is the worst possible time
to look for it.

**The points forecast is derived from what already happened, never from a
prediction model.** It projects from the last 30 days of ledger movement,
scaled by the configured frequency, and reports ``basis`` and ``confidence``
with it. A forecast on this surface that a customer acts on and that turns out
to be a guess would be worse than no forecast.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas.chat import (
    CustomerRecoveryStatus,
    CustomerVisibleRecoveryAction,
    PolicyPostureSummary,
    PointsForecastItem,
    SelfServiceStatusReport,
)
from app.services import customer_explain

#: How far back the points forecast looks, and what it is scaled to.
POINTS_FORECAST_WINDOW_DAYS = 30
POINTS_FORECAST_HORIZON_DAYS = 30
#: Below this many ledger rows the projection is reported as `low` confidence
#: rather than omitted. A single recovery credit is real movement; projecting it
#: forward for a month would be a lie, so it is projected and labelled.
POINTS_FORECAST_MIN_ROWS = 1
#: A forecast is only offered for wallets that can actually be spent. A wallet
#: whose point type is not exchangeable gets ``no_exchange_rule`` rather than a
#: number the customer cannot use.
POINTS_FORECAST_HORIZON_MULTIPLIER = 1.0


def _parse_stored(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Customer-visible recovery
# ---------------------------------------------------------------------------


def to_visible_action(row: Any) -> CustomerVisibleRecoveryAction:
    """One ``RecoveryAction`` row as the customer may see it.

    The playbook *name* is looked up from the live config so the label cannot
    drift from the playbook that produced the row. An unrecognised
    ``playbook_id`` falls back to the raw id rather than to a blank, because a
    missing label is more honest than an invented one and a missing id would
    look like a data problem when it is a config removal.
    """
    from app.services.recovery_playbooks import RECOVERY_PLAYBOOK_SETS

    playbook_id = str(getattr(row, "playbook_id", "") or "")
    playbook_name = ""
    for rows in RECOVERY_PLAYBOOK_SETS.values():
        for entry in rows:
            if str(entry.get("playbook_id", "")) == playbook_id:
                playbook_name = str(entry.get("name", ""))
                break
        if playbook_name:
            break
    if not playbook_name:
        playbook_name = playbook_id or "unknown playbook"

    action_name = str(getattr(row, "action", "") or "")
    status = str(getattr(row, "status", "") or "unknown")
    result = _parse_stored(getattr(row, "result_json", ""))
    narrated = customer_explain.narrate_recovery_action(
        {
            "action": action_name,
            "playbook_id": playbook_id,
            "status": status,
            "result": result,
            "reference": str(getattr(row, "reference", "") or ""),
            "failure_reason": str(getattr(row, "failure_reason", "") or ""),
        }
    )
    return CustomerVisibleRecoveryAction(
        id=int(getattr(row, "id", 0) or 0),
        playbook_id=playbook_id,
        playbook_name=playbook_name,
        action=action_name,
        status=status,
        description=str(narrated.get("customer_impact", "")),
        outcome=str(narrated.get("outcome", "")),
        benefit_to_customer=str(narrated.get("summary", "")),
        executed_at=getattr(row, "created_at", None) or datetime.now(timezone.utc),
        reference=narrated.get("reference") or None,
    )


async def build_customer_recovery_status(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
    *,
    include_preview: bool = False,
) -> CustomerRecoveryStatus:
    """What recovery decided for this customer, and what it did about it.

    ``include_preview`` adds the *unmatched-or-matched-but-not-run* actions a
    run right now would take. It defaults off because a preview is a statement
    about the future and mixing it into a history read is how "12 recovery
    actions" comes to include 3 that never happened.
    """
    from app.services.recovery_playbooks import (
        _load_recovery_action_history,
        _load_recovery_context,
        evaluate_recovery_guards,
        plan_recovery_actions,
        resolve_recovery_callback_plan,
    )
    from app.services.preferences import load_user_preferences

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(window_days)))
    preferences, _consents = await load_user_preferences(db, int(user_id))
    show = bool(preferences.get("show_recovery_activity", True))

    resolved = await _load_recovery_context(db, int(user_id), window_days)
    context = resolved["context"]

    rows_result = await db.execute(
        select(models.RecoveryAction)
        .where(models.RecoveryAction.user_id == int(user_id))
        .where(models.RecoveryAction.created_at >= cutoff)
        .order_by(desc(models.RecoveryAction.created_at))
    )
    rows = list(rows_result.scalars().all())

    executed: list[CustomerVisibleRecoveryAction] = []
    skipped: list[CustomerVisibleRecoveryAction] = []
    goodwill = 0.0
    escalations = 0
    guardrails = 0
    for row in rows:
        status = str(getattr(row, "status", "") or "")
        if status == "executed":
            executed.append(to_visible_action(row))
        elif status in {"skipped", "guard_rejected", "would_execute", "failed"}:
            skipped.append(to_visible_action(row))
        action = str(getattr(row, "action", "") or "")
        if action == "credit_points" and status == "executed":
            stored = _parse_stored(getattr(row, "result_json", ""))
            try:
                goodwill += float(stored.get("points_credited", 0.0) or 0.0)
            except (TypeError, ValueError):
                pass
        elif action == "escalate_ticket" and status == "executed":
            escalations += 1
        elif action == "adjust_policy_score" and status == "executed":
            guardrails += 1

    if not show:
        # The preference says do not show this. Report the counts and the
        # aggregate benefit -- the customer still sees what happened to their
        # balance and their case -- but not the per-action narration.
        executed, skipped = [], []

    history = await _load_recovery_action_history(db, int(user_id), now)
    plan = plan_recovery_actions(context)
    guards = evaluate_recovery_guards(plan, history, now=now, enforce=False)
    allowed = [entry for entry in guards["decisions"] if entry.get("allowed")]

    matched_preview = [
        {
            "playbook_id": str(item.get("playbook_id", "")),
            "action": str(item.get("action", "")),
            "would_run": any(
                int(entry.get("index", -1)) == int(item.get("index", -2))
                for entry in allowed
            ),
            "guard_id": next(
                (
                    str(entry.get("guard_id", ""))
                    for entry in guards["decisions"]
                    if int(entry.get("index", -2)) == int(item.get("index", -3))
                ),
                "",
            ),
        }
        for item in plan
    ] if include_preview else []

    callback = resolve_recovery_callback_plan(context, now=now)
    next_review = None
    try:
        next_review = datetime.fromisoformat(str(callback.get("due_at")))
    except (TypeError, ValueError):
        next_review = None

    readiness = str(context.get("recovery_readiness", "low"))
    summary_parts = [
        f"Your recovery status is {readiness}.",
        f"{len(executed)} action(s) were carried out in the last {window_days} day(s).",
    ]
    if goodwill > 0.0:
        summary_parts.append(f"{round(goodwill, 2):g} goodwill points were credited to your balance.")
    if escalations:
        summary_parts.append(f"{escalations} case(s) were escalated to a senior specialist.")
    if guardrails:
        summary_parts.append(f"{guardrails} access adjustment(s) were applied while we worked on this.")
    if skipped:
        summary_parts.append(
            f"{len(skipped)} action(s) were deliberately not repeated; those are listed so the decision is visible."
        )
    if not show:
        summary_parts.append(
            "Per-action detail is hidden because your preference "
            "`show_recovery_activity` is off."
        )
    if not executed and not skipped:
        summary_parts.append("No recovery action has been needed.")

    return CustomerRecoveryStatus(
        generated_at=now,
        user_id=int(user_id),
        window_days=int(window_days),
        recovery_readiness=readiness,
        dissatisfaction_score=round(float(context.get("dissatisfaction_score", 0.0) or 0.0), 2),
        primary_risks=[str(risk) for risk in (context.get("primary_risks") or [])],
        matched_playbooks=[
            {
                "playbook_id": str(entry.get("playbook_id", "")),
                "name": str(entry.get("name", "")),
                "priority": int(entry.get("priority", 100)),
                "playbook_set": str(entry.get("playbook_set", "core")),
            }
            for entry in (resolved.get("matched") or [])
        ] if resolved.get("matched") else [
            {
                "playbook_id": str(item["playbook_id"]),
                "name": str(item["action"]),
                "priority": 0,
                "playbook_set": "preview",
            }
            for item in matched_preview
        ],
        executed_actions=executed,
        skipped_actions=skipped,
        goodwill_points_credited=round(goodwill, 2),
        tickets_escalated=escalations,
        policy_guardrails=guardrails,
        next_review_at=next_review,
        summary=" ".join(summary_parts),
    )


# ---------------------------------------------------------------------------
# Points forecast
# ---------------------------------------------------------------------------


async def build_points_forecast(
    db: AsyncSession, user_id: int
) -> dict[str, Any]:
    """Project the next 30 days of points movement from the last 30 days of it.

    The projection is a scaled repeat of observed movement, and it says so.
    ``basis`` carries the arithmetic, ``confidence`` reflects how many ledger
    rows the projection rests on, and a wallet with no exchange rule reports
    ``no_exchange_rule`` rather than a number the customer cannot spend.
    """
    from app.services.points_exchange import POINTS_EXCHANGE_RULES

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=POINTS_FORECAST_WINDOW_DAYS)
    exchangeable = {
        str(rule.get("point_type", ""))
        for rule in POINTS_EXCHANGE_RULES
        if rule.get("redeem", True) or rule.get("purchase", True)
    }

    rows_result = await db.execute(
        select(models.PointsWallet)
        .where(models.PointsWallet.user_id == int(user_id))
        .order_by(models.PointsWallet.point_type)
    )
    wallets = list(rows_result.scalars().all())
    items: list[PointsForecastItem] = []
    per_type: list[dict[str, Any]] = []
    notes: list[str] = []

    for wallet in wallets:
        point_type = str(getattr(wallet, "point_type", "") or "")
        balance = float(getattr(wallet, "balance", 0.0) or 0.0)
        txn_result = await db.execute(
            select(models.PointsTransaction.points_delta, models.PointsTransaction.kind)
            .where(models.PointsTransaction.user_id == int(user_id))
            .where(models.PointsTransaction.point_type == point_type)
            .where(models.PointsTransaction.created_at >= cutoff)
            .order_by(models.PointsTransaction.created_at)
        )
        rows = list(txn_result.all())
        earned = sum(float(delta or 0.0) for delta, _kind in rows if float(delta or 0.0) > 0.0)
        spent = abs(sum(float(delta or 0.0) for delta, _kind in rows if float(delta or 0.0) < 0.0))
        net = round(earned - spent, 2)
        scale = POINTS_FORECAST_HORIZON_DAYS / POINTS_FORECAST_WINDOW_DAYS
        projected = round(net * scale * POINTS_FORECAST_HORIZON_MULTIPLIER, 2)
        confidence = customer_explain.confidence_for(len(rows))

        if point_type not in exchangeable:
            notes.append(
                f"{point_type}: no exchange rule, so the projection is informational only"
            )
        items.append(
            PointsForecastItem(
                source=point_type,
                estimated_points=projected,
                timeframe=f"next {POINTS_FORECAST_HORIZON_DAYS} days",
                confidence=confidence,
            )
        )
        per_type.append(
            {
                "point_type": point_type,
                "current_balance": round(balance, 2),
                "earned_in_window": round(earned, 2),
                "spent_in_window": round(spent, 2),
                "net_in_window": net,
                "projected_change": projected,
                "projected_balance": round(balance + projected, 2),
                "rows_in_window": len(rows),
                "exchangeable": point_type in exchangeable,
                "confidence": confidence,
            }
        )

    if not rows:
        notes.append("no ledger rows in the window, so no projection is offered")

    return {
        "generated_at": now,
        "user_id": int(user_id),
        "window_days": POINTS_FORECAST_WINDOW_DAYS,
        "horizon_days": POINTS_FORECAST_HORIZON_DAYS,
        "method": "scaled_repeat_of_observed_movement",
        "basis": (
            f"net movement over {POINTS_FORECAST_WINDOW_DAYS} days, scaled to "
            f"{POINTS_FORECAST_HORIZON_DAYS} days"
        ),
        "is_projection": True,
        "items": items,
        "per_type": per_type,
        "notes": notes,
        "summary": (
            f"{len(per_type)} point type(s) projected over the next "
            f"{POINTS_FORECAST_HORIZON_DAYS} days from observed movement; this is a "
            "repeat of past behaviour, not a prediction of future activity"
        ),
    }


# ---------------------------------------------------------------------------
# Policy posture in plain language
# ---------------------------------------------------------------------------


async def build_policy_posture_summary(
    db: AsyncSession, user_id: int
) -> PolicyPostureSummary:
    """Tier, posture and band, with the restrictions and benefits each implies.

    Reads the customer's existing ``CustomerPolicyScore`` rather than
    recomputing. A preview recomputed here would be a second implementation of
    the score, and the two would disagree; if no score exists the section
    reports that instead of inventing a default.
    """
    from app.services.policy_scoring import resolve_access_band

    result = await db.execute(
        select(models.CustomerPolicyScore).where(
            models.CustomerPolicyScore.user_id == int(user_id)
        )
    )
    snapshot = result.scalar_one_or_none()
    if snapshot is None:
        return PolicyPostureSummary(
            tier="unscored",
            posture="unscored",
            access_band="unscored",
            access_score=0.0,
            customer_score=0.0,
            system_score=0.0,
            plain_language=(
                "No policy score exists for your account yet. One is created when "
                "you have enough interaction history for the scoring engine to have "
                "something to work with."
            ),
            restrictions=[],
            benefits=[],
        )

    tier = str(getattr(snapshot, "policy_tier", "standard"))
    posture = str(getattr(snapshot, "control_posture", "observed"))
    access = float(getattr(snapshot, "access_score", 0.0) or 0.0)
    band = resolve_access_band(access)

    restrictions: list[str] = []
    benefits: list[str] = []
    if posture == "high_trust":
        benefits = [
            "All gated actions run immediately on your behalf.",
            "No review step on privileged operations.",
        ]
    elif posture == "customer_trusted":
        benefits = [
            "Gated actions run immediately, with a light supervision trail.",
        ]
        restrictions = [
            "The highest-risk operations still require a review step.",
        ]
    elif posture == "observed":
        restrictions = [
            "Some actions are logged and reviewed before they take effect.",
            "Privileged operations may take longer to complete.",
        ]
        benefits = [
            "Routine actions run immediately.",
        ]
    else:  # constrained
        restrictions = [
            "Privileged actions require review before they take effect.",
            "Some benefits stay locked until your open issues are resolved.",
        ]
        benefits = [
            "Routine service actions are unaffected.",
        ]

    return PolicyPostureSummary(
        tier=tier,
        posture=posture,
        access_band=band,
        access_score=round(access, 2),
        customer_score=round(float(getattr(snapshot, "customer_score", 0.0) or 0.0), 2),
        system_score=round(float(getattr(snapshot, "system_score", 0.0) or 0.0), 2),
        plain_language=(
            f"You are on the {tier} tier with a {posture} control posture. "
            + customer_explain.CONTROL_POSTURE_PHRASES.get(
                posture, "Your control posture is not classified."
            )
        ),
        restrictions=restrictions,
        benefits=benefits,
    )


# ---------------------------------------------------------------------------
# Composed self-service dashboard
# ---------------------------------------------------------------------------


async def build_self_service_status(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
    *,
    include_preview: bool = False,
) -> SelfServiceStatusReport:
    """One dashboard: recovery status, points forecast, policy posture, journey.

    Composed from the three sections above plus the journey, each built through
    its own function so the individual endpoints and this one cannot disagree.
    A section that fails is reported in ``degraded`` rather than silently
    replaced with a healthy-looking default.
    """
    from app.services.loyalty_journey import build_loyalty_journey_plan_for_user

    degraded: list[dict[str, str]] = []
    recovery_status: Optional[CustomerRecoveryStatus] = None
    forecast: dict[str, Any] = {}
    posture: Optional[PolicyPostureSummary] = None
    journey = None
    communication = None

    try:
        recovery_status = await build_customer_recovery_status(
            db, int(user_id), window_days, include_preview=include_preview
        )
    except Exception as exc:  # noqa: BLE001
        degraded.append({"section": "recovery", "reason": f"{type(exc).__name__}: {exc}"})

    try:
        forecast = await build_points_forecast(db, int(user_id))
    except Exception as exc:  # noqa: BLE001
        degraded.append({"section": "points_forecast", "reason": f"{type(exc).__name__}: {exc}"})

    try:
        posture = await build_policy_posture_summary(db, int(user_id))
    except Exception as exc:  # noqa: BLE001
        degraded.append({"section": "policy_posture", "reason": f"{type(exc).__name__}: {exc}"})

    try:
        plan = await build_loyalty_journey_plan_for_user(db, int(user_id), window_days)
        from app.schemas.chat import Customer360LoyaltyJourney

        journey = Customer360LoyaltyJourney(
            current_family=plan.scenario_families[0] if plan.scenario_families else "none",
            matched_scenarios=plan.matched_scenario_count,
            scenario_families=list(plan.scenario_families),
            next_best_actions=list(plan.next_best_actions),
            top_priority_scenario=(
                plan.scenario_items[0].scenario if plan.scenario_items else None
            ),
        )
    except Exception as exc:  # noqa: BLE001
        degraded.append({"section": "journey", "reason": f"{type(exc).__name__}: {exc}"})

    try:
        from app.services.communication_strategy import resolve_communication_strategy_for_user
        from app.services.preferences import load_user_preferences, preferred_channel
        from app.schemas.chat import Customer360Communication

        strategy = await resolve_communication_strategy_for_user(
            db, int(user_id), window_days, locale="global"
        )
        preferences, _consents = await load_user_preferences(db, int(user_id))
        channel = preferred_channel(preferences, strategy.params.channel)
        communication = Customer360Communication(
            resolved_layer=str(strategy.resolved_layer),
            precedence=int(strategy.precedence),
            profile_id=str(strategy.profile_id),
            tone=str(strategy.params.tone),
            channel=str(channel.get("channel") or strategy.params.channel),
            framing=str(strategy.params.framing),
            reply_urgency=str(strategy.params.reply_urgency),
            mood_label=str(dict(strategy.mood or {}).get("label", "") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        degraded.append({"section": "communication", "reason": f"{type(exc).__name__}: {exc}"})

    if recovery_status is None:
        recovery_status = CustomerRecoveryStatus(
            generated_at=datetime.now(timezone.utc),
            user_id=int(user_id),
            window_days=int(window_days),
            recovery_readiness="unavailable",
            dissatisfaction_score=0.0,
            primary_risks=[],
            summary=(
                "Recovery status could not be built"
                + (f": {degraded[0]['reason']}" if degraded else "")
                + ". The figures below are unaffected."
            ),
        )
    if journey is None:
        from app.schemas.chat import Customer360LoyaltyJourney

        journey = Customer360LoyaltyJourney(
            current_family="unavailable",
            matched_scenarios=0,
            scenario_families=[],
            next_best_actions=[],
        )
    if communication is None:
        from app.schemas.chat import Customer360Communication

        communication = Customer360Communication(
            resolved_layer="unavailable",
            precedence=0,
            profile_id="",
            tone="",
            channel="",
            framing="",
            reply_urgency="",
            mood_label="",
        )
    if posture is None:
        posture = PolicyPostureSummary(
            tier="unavailable",
            posture="unavailable",
            access_band="unavailable",
            access_score=0.0,
            customer_score=0.0,
            system_score=0.0,
            plain_language="Policy posture could not be built.",
            restrictions=[],
            benefits=[],
        )

    next_actions: list[str] = []
    for action in journey.next_best_actions:
        next_actions.append(str(action))
    if recovery_status.next_review_at is not None:
        next_actions.append(
            f"Your next scheduled contact review is {recovery_status.next_review_at.isoformat()}"
        )
    if posture.posture in {"observed", "constrained"}:
        next_actions.append(
            "Some actions are gated under your current access standing; resolving open issues lifts this"
        )
    if forecast.get("items"):
        top = max(forecast["items"], key=lambda item: item.estimated_points)
        if top.estimated_points != 0.0:
            next_actions.append(
                f"At your current rate you will gain about {top.estimated_points:g} "
                f"{top.source} over the next {forecast['horizon_days']} days"
            )

    summary_parts = [recovery_status.summary]
    if posture.plain_language:
        summary_parts.append(posture.plain_language)
    if degraded:
        summary_parts.append(
            "Incomplete: " + ", ".join(entry["section"] for entry in degraded) + " could not be built."
        )

    return SelfServiceStatusReport(
        generated_at=datetime.now(timezone.utc),
        user_id=int(user_id),
        window_days=int(window_days),
        recovery_status=recovery_status,
        points_forecast=forecast,
        policy_posture=posture.model_dump(),
        loyalty_journey=journey,
        communication_preview=communication,
        next_actions=next_actions,
        summary=" ".join(summary_parts),
    )
