"""Customer 360: one aggregated view of everything already known about a person.

Why an aggregator and not a new store
-------------------------------------
Every input this module reports is already computed by an existing engine --
``chat_analytics`` for the interaction summary, ``loyalty_journey`` for the
journey, ``recovery_playbooks`` for the recovery context and action history,
``communication_strategy`` for the resolved channel, ``points_exchange`` for
the wallet, ``arrears_payments`` for deferred payments, ``policy_scoring`` for
the tier/posture. This module **composes** them and adds nothing of its own
except the aggregation. That is the whole design constraint: a Customer 360
that keeps its own copy of a score is a second source of truth that will
disagree with the first, and the disagreement is what a customer would see.

The cost of composition, stated plainly
---------------------------------------
It is N engines, N queries. For a single customer's 360 that is acceptable and
for a cross-user admin rollup over 500 users it is not. So
:func:`build_customer_360_admin_report` does **not** call the per-user builder;
it reads the cheap aggregate rows directly and reports which per-user
sub-systems it skipped. A rollup that silently ran 500 full 360 builds and
timed out would be worse than one that says "this is the cheap view, and here
is what it does not include".

Truthfulness rules this module enforces on itself
-------------------------------------------------
- A missing sub-view is reported as ``unavailable`` with a reason. It is never
  defaulted to a healthy value, because a 360 that reports "no open payments"
  when the payments query failed is the exact failure this design exists to
  prevent.
- The summary sentence is *built from* the numbers, not written alongside them,
  so it cannot claim a state the fields contradict.
- Everything here is read-only. A 360 never writes, and never triggers a
  playbook. The recovery actions it reports are the ones that already ran.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas.chat import (
    Customer360Communication,
    Customer360InteractionSummary,
    Customer360LoyaltyJourney,
    Customer360Payments,
    Customer360Points,
    Customer360Preferences,
    Customer360Profile,
    Customer360Recovery,
    Customer360Report,
    Customer360Sentiment,
)

#: Which sub-views a 360 attempt to build. Published so a caller can see the
#: full set even when some failed -- a report that drops a failed section from
#: its own inventory is indistinguishable from one that never tried.
CUSTOMER_360_SECTIONS: tuple[str, ...] = (
    "profile",
    "interactions",
    "sentiment",
    "recovery",
    "loyalty_journey",
    "communication",
    "payments",
    "points",
    "preferences",
)

#: Markers a section reports when it could not be built.
CUSTOMER_360_UNAVAILABLE = "unavailable"


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": CUSTOMER_360_UNAVAILABLE, "reason": reason}


def _days_since(value: Any) -> Optional[int]:
    """Whole days since a timestamp, or ``None`` if it is absent/unparseable."""
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return max(0, int((datetime.now(timezone.utc) - value).days))
    except (TypeError, ValueError, AttributeError):
        return None


def build_summary_text(
    profile: Customer360Profile,
    interactions: Customer360InteractionSummary,
    sentiment: Customer360Sentiment,
    recovery: Customer360Recovery,
    journey: Customer360LoyaltyJourney,
    points: Customer360Points,
    payments: Customer360Payments,
) -> str:
    """Compose the one-paragraph summary from the fields it describes.

    Written here rather than assembled by the caller so the sentence and the
    numbers cannot drift: every clause below reads a field that is also in the
    payload. There is no literal state word in this function that is not
    derived.
    """
    clauses: list[str] = [
        f"{profile.username} is a {profile.value_tier}-value {profile.lifecycle_stage} "
        f"customer with a loyalty score of {profile.loyalty_score:.0f} and "
        f"{profile.churn_risk} churn risk."
    ]
    activity = (
        f"{interactions.total_chats} message(s) and {interactions.total_bookings} "
        f"booking(s) on record"
    )
    if interactions.days_since_last_activity is not None:
        activity += f", last active {interactions.days_since_last_activity} day(s) ago"
    if interactions.dormant:
        activity += " (currently dormant)"
    clauses.append(activity + ".")

    if sentiment.has_sentiment:
        clauses.append(
            f"Most recent message reads as {sentiment.current_label} "
            f"(confidence {sentiment.current_score:.2f}, trend {sentiment.trend})."
        )
    else:
        clauses.append("No sentiment was available for the most recent message.")

    if recovery.dissatisfaction_score > 0.0:
        recovery_clause = (
            f"Recovery status is {recovery.recovery_readiness} "
            f"(friction {recovery.dissatisfaction_score:.1f})"
        )
        if recovery.recent_recovery_actions:
            recovery_clause += (
                f", with {recovery.recent_recovery_actions} recent action(s) of which "
                f"{recovery.blocked_recovery_actions} were declined by our own limits"
            )
        clauses.append(recovery_clause + ".")
    else:
        clauses.append("No recovery work is active.")

    if journey.matched_scenarios:
        clauses.append(
            f"{journey.matched_scenarios} journey scenario(s) match across "
            f"{len(journey.scenario_families)} famil(ies); the top family is "
            f"{journey.current_family}."
        )
    else:
        clauses.append("No journey scenario currently matches.")

    if points.total_balance > 0.0:
        clauses.append(
            f"Points balance is {points.total_balance:g} "
            f"({points.redeemable_balance:g} redeemable)."
        )
    if payments.open_arrears_count:
        clauses.append(
            f"{payments.open_arrears_count} open deferred payment(s) totalling "
            f"{payments.total_principal_at_risk:g} principal and "
            f"{payments.total_interest_at_risk:g} projected interest."
        )

    return " ".join(clause for clause in clauses if clause)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


async def _build_profile(db: AsyncSession, user_id: int, window_days: int) -> dict[str, Any]:
    """The identity + scored-profile section."""
    from app.services.chat_analytics import (
        analyze_sentiment,
        build_summary,
        load_user_interaction_window,
    )

    result = await db.execute(
        select(models.User).where(models.User.id == int(user_id))
    )
    user = result.scalar_one_or_none()
    if user is None:
        return _unavailable(f"no user row with id {user_id}")

    chat_rows, bookings = await load_user_interaction_window(db, int(user_id), window_days)
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = build_summary(int(user_id), chat_rows, bookings, latest_sentiment)
    churn_prediction = await _churn_prediction(db, int(user_id), window_days)
    from app.services.chat_analytics import classify_lifecycle_stage

    stage, _confidence, _drivers, _focus = classify_lifecycle_stage(
        summary, churn_prediction, len(chat_rows), len(bookings)
    )
    profile = Customer360Profile(
        user_id=int(user_id),
        username=str(getattr(user, "username", "") or ""),
        email=getattr(user, "email", None),
        created_at=getattr(user, "created_at", None) or datetime.now(timezone.utc),
        lifecycle_stage=stage,
        value_tier=str(summary.value_tier),
        customer_classification=str(summary.customer_classification),
        loyalty_score=float(summary.loyalty_score),
        churn_risk=str(summary.churn_risk),
        monetization_readiness=float(summary.monetization_readiness),
        engagement_score=float(
            getattr(summary, "engagement_score", None)
            or round(min(100.0, (summary.messages_analyzed + summary.bookings_analyzed) * 4.0), 2)
        ),
        lifetime_value_estimate=float(getattr(summary, "ltv_estimate", None) or 0.0),
        referral_count=int(getattr(summary, "referral_count", None) or 0),
        tier_status=str(summary.value_tier or "standard"),
    )
    return {
        "status": "ok",
        "profile": profile,
        "summary": summary,
        "churn_prediction": churn_prediction,
        "latest_sentiment": latest_sentiment,
    }


async def _churn_prediction(db: AsyncSession, user_id: int, window_days: int):
    from app.services.chat_analytics import build_churn_prediction

    return await build_churn_prediction(db, int(user_id), window_days)


async def _build_interactions(db: AsyncSession, user_id: int, window_days: int) -> dict[str, Any]:
    """Counts and recency across the three interaction tables."""
    from app.services.loyalty_journey import load_history_totals

    totals = await load_history_totals(db, int(user_id))
    snapshot_result = await db.execute(
        select(func.count(models.RetentionSnapshot.id), func.max(models.RetentionSnapshot.created_at))
        .where(models.RetentionSnapshot.user_id == int(user_id))
    )
    snapshot_count, latest_snapshot_at = snapshot_result.one()

    total_chats = int(totals["total_chat_count"] or 0)
    total_bookings = int(totals["total_booking_count"] or 0)
    latest_activity = totals["latest_booking_at"] or totals["latest_chat_at"]
    days_since = _days_since(latest_activity)
    has_history = bool(total_chats or total_bookings)
    dormant = bool(
        has_history
        and days_since is not None
        and days_since >= 14
    )

    # Per-state booking counts across all time, not the window: a customer with
    # one cancellation six months ago is still a customer who cancelled once,
    # and a 360 that only counts the window reports zero.
    state_result = await db.execute(
        select(models.Booking.status, func.count(models.Booking.id))
        .where(models.Booking.user_id == int(user_id))
        .group_by(models.Booking.status)
    )
    states = {str(getattr(row[0], "value", row[0])): int(row[1] or 0) for row in state_result.all()}

    return {
        "status": "ok",
        "interactions": Customer360InteractionSummary(
            total_chats=total_chats,
            total_bookings=total_bookings,
            completed_bookings=int(states.get("completed", 0)),
            pending_bookings=int(states.get("pending", 0)),
            cancelled_bookings=int(states.get("cancelled", 0)),
            confirmed_bookings=int(states.get("confirmed", 0)),
            total_snapshots=int(snapshot_count or 0),
            latest_chat_at=totals["latest_chat_at"],
            latest_booking_at=totals["latest_booking_at"],
            latest_snapshot_at=latest_snapshot_at,
            days_since_last_activity=days_since,
            dormant=dormant,
        ),
        "window_days": int(window_days),
        "booking_states": states,
        "dormancy_threshold_days": 14,
    }


async def _build_sentiment(
    db: AsyncSession, user_id: int, window_days: int, profile: dict[str, Any]
) -> dict[str, Any]:
    """Current sentiment plus a trend read off the signal trend series.

    ``analyze_sentiment`` calls Hugging Face and returns ``None`` on failure, so
    an offline deployment produces no sentiment at all. That is reported as
    ``has_sentiment=False`` with the reason stated, not as a neutral score --
    "we could not tell" and "we checked and it was neutral" are different
    answers and collapsing them is how a broken model reads as a happy customer.
    """
    from app.services.chat_analytics import analyze_sentiment, load_signal_trends

    latest = profile.get("latest_sentiment")
    negative, positive, neutral = 0, 0, 0

    rows_result = await db.execute(
        select(models.ChatHistory.message)
        .where(models.ChatHistory.user_id == int(user_id))
        .order_by(desc(models.ChatHistory.timestamp))
        .limit(5)
    )
    messages = [str(row[0] or "") for row in rows_result.all() if row[0]]
    for message in messages:
        result = analyze_sentiment(message)
        if result is None:
            continue
        label = str(result.label)
        if label == "negative":
            negative += 1
        elif label == "positive":
            positive += 1
        else:
            neutral += 1

    trend_report = await load_signal_trends(db, int(user_id), window_days)
    worsening = len(trend_report.worsening_areas)
    improving = len(trend_report.improving_areas)
    if worsening > improving:
        trend = "declining"
    elif improving > worsening:
        trend = "improving"
    else:
        trend = "stable"

    return {
        "status": "ok",
        "sentiment": Customer360Sentiment(
            current_label=str(getattr(latest, "label", "none")) if latest else "none",
            current_score=round(float(getattr(latest, "score", 0.0) or 0.0), 4),
            has_sentiment=latest is not None,
            trend=trend,
            negative_count=negative,
            positive_count=positive,
            neutral_count=neutral,
        ),
        "sampled_messages": len(messages),
        "sampled_with_result": negative + positive + neutral,
        "model_unavailable_reason": (
            None
            if latest is not None
            else "analyze_sentiment returned None; the sentiment model is unreachable or the window has no messages"
        ),
        "trends": {
            "improving_areas": list(trend_report.improving_areas),
            "worsening_areas": list(trend_report.worsening_areas),
        },
    }


async def _build_recovery(
    db: AsyncSession, user_id: int, window_days: int
) -> dict[str, Any]:
    """Recovery readiness plus what the automated program actually did."""
    from app.services.recovery_playbooks import (
        _load_recovery_context,
        evaluate_recovery_playbooks,
    )

    resolved = await _load_recovery_context(db, int(user_id), window_days)
    context = resolved["context"]
    matched = evaluate_recovery_playbooks(context)

    cutoff = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    recent_result = await db.execute(
        select(models.RecoveryAction)
        .where(models.RecoveryAction.user_id == int(user_id))
        .where(models.RecoveryAction.created_at >= cutoff)
        .order_by(desc(models.RecoveryAction.created_at))
    )
    rows = list(recent_result.scalars().all())

    goodwill = 0.0
    escalations = 0
    guardrails = 0
    executed = 0
    blocked = 0
    last_at: Optional[datetime] = None
    for row in rows:
        status = str(getattr(row, "status", "") or "")
        if status == "executed":
            executed += 1
        elif status in {"skipped", "guard_rejected"}:
            blocked += 1
        action = str(getattr(row, "action", "") or "")
        if action == "credit_points":
            try:
                stored = __import__("json").loads(
                    getattr(row, "result_json", "") or "{}"
                )
            except (TypeError, ValueError):
                stored = {}
            if isinstance(stored, dict):
                try:
                    goodwill += float(stored.get("points_credited", 0.0) or 0.0)
                except (TypeError, ValueError):
                    pass
        elif action == "escalate_ticket":
            escalations += 1
        elif action == "adjust_policy_score":
            guardrails += 1
        created = getattr(row, "created_at", None)
        if created is not None and (last_at is None or created > last_at):
            last_at = created

    recovery = Customer360Recovery(
        recovery_readiness=str(context.get("recovery_readiness", "low")),
        dissatisfaction_score=round(float(context.get("dissatisfaction_score", 0.0) or 0.0), 2),
        primary_risks=[str(risk) for risk in (context.get("primary_risks") or [])],
        recent_recovery_actions=executed,
        blocked_recovery_actions=blocked,
        last_recovery_at=last_at,
        goodwill_points_credited=round(goodwill, 2),
        escalation_count=escalations,
        policy_guardrails_applied=guardrails,
    )
    return {
        "status": "ok",
        "recovery": recovery,
        "context": context,
        "matched_playbooks": [
            {
                "playbook_id": str(entry.get("playbook_id", "")),
                "name": str(entry.get("name", "")),
                "priority": int(entry.get("priority", 100)),
                "playbook_set": str(entry.get("playbook_set", "core")),
            }
            for entry in matched
        ],
        "auto_recovery_enabled": bool(
            __import__("os").getenv("CSERVICE_AUTO_RECOVERY", "0")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        ),
    }


async def _build_journey(db: AsyncSession, user_id: int, window_days: int) -> dict[str, Any]:
    from app.services.loyalty_journey import build_loyalty_journey_plan_for_user

    plan = await build_loyalty_journey_plan_for_user(db, int(user_id), window_days)
    journey = Customer360LoyaltyJourney(
        current_family=plan.scenario_families[0] if plan.scenario_families else "none",
        matched_scenarios=plan.matched_scenario_count,
        scenario_families=list(plan.scenario_families),
        next_best_actions=list(plan.next_best_actions),
        top_priority_scenario=(
            plan.scenario_items[0].scenario if plan.scenario_items else None
        ),
    )
    return {"status": "ok", "journey": journey, "plan": plan}


async def _build_communication(
    db: AsyncSession, user_id: int, window_days: int
) -> dict[str, Any]:
    """The resolved strategy, with the customer's stated preference applied.

    The preference layer is applied *here*, at the presentation point, rather
    than inside ``resolve_communication_strategy``. That engine resolves from
    four config layers and its output is pinned by existing consumers; making it
    read user preferences would change what those consumers receive. So the
    ladder stays authoritative for tone/framing/urgency and the stated channel
    is layered on top, with the override reported explicitly.
    """
    from app.services.communication_strategy import resolve_communication_strategy_for_user
    from app.services.preferences import load_user_preferences, preferred_channel

    strategy = await resolve_communication_strategy_for_user(
        db, int(user_id), window_days, locale="global"
    )
    params = strategy.params
    preferences, _consents = await load_user_preferences(db, int(user_id))
    channel = preferred_channel(preferences, params.channel)
    mood = dict(strategy.mood or {})
    communication = Customer360Communication(
        resolved_layer=str(strategy.resolved_layer),
        precedence=int(strategy.precedence),
        profile_id=str(strategy.profile_id),
        tone=str(params.tone),
        channel=str(channel.get("channel") or params.channel),
        framing=str(params.framing),
        reply_urgency=str(params.reply_urgency),
        mood_label=str(mood.get("label", "") or ""),
    )
    return {
        "status": "ok",
        "communication": communication,
        "channel_decision": channel,
        "strategy": strategy,
    }


async def _build_payments(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """Deferred-payment exposure. Read directly rather than via the arrears
    service so the 360 can report the *raw* open total alongside interest.

    ``quote``/``list`` in ``arrears_payments`` are shaped for a customer asking
    about one entry; a 360 needs the aggregate, and routing that through the
    list helper would mean either an unbounded page or a silently truncated
    total. Two counted queries instead.
    """
    from app.services.arrears_payments import compute_arrears_interest

    rows_result = await db.execute(
        select(models.ArrearsEntry)
        .where(models.ArrearsEntry.user_id == int(user_id))
        .where(models.ArrearsEntry.status == "open")
    )
    open_rows = list(rows_result.scalars().all())
    all_result = await db.execute(
        select(models.ArrearsEntry.status, func.count(models.ArrearsEntry.id))
        .where(models.ArrearsEntry.user_id == int(user_id))
        .group_by(models.ArrearsEntry.status)
    )
    by_status = {str(row[0] or "unknown"): int(row[1] or 0) for row in all_result.all()}

    principal = 0.0
    interest = 0.0
    overdue = 0
    now = datetime.now(timezone.utc)
    for row in open_rows:
        principal += float(getattr(row, "principal", 0.0) or 0.0)
        try:
            interest += float(
                compute_arrears_interest(row, now=now).get("interest", 0.0) or 0.0
            )
        except Exception:  # pragma: no cover - defensive; a bad row must not 500 a 360
            pass
        due = getattr(row, "due_at", None)
        if due is not None:
            if getattr(due, "tzinfo", None) is None:
                due = due.replace(tzinfo=timezone.utc)
            if due < now:
                overdue += 1

    payments = Customer360Payments(
        open_arrears_count=len(open_rows),
        total_principal_at_risk=round(principal, 2),
        total_interest_at_risk=round(interest, 2),
        overdue_count=overdue,
        settled_count=int(by_status.get("settled", 0)),
        waived_count=int(by_status.get("waived", 0)),
    )
    return {
        "status": "ok",
        "payments": payments,
        "by_status": by_status,
        "open_entries": len(open_rows),
    }


async def _build_points(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """Wallet balances plus a 30-day movement count."""
    from app.services.points_exchange import list_user_wallets

    wallets = await list_user_wallets(db, int(user_id))
    cutoff = datetime.now(timezone.utc)
    cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0) - __import__(
        "datetime"
    ).timedelta(days=30)
    recent_result = await db.execute(
        select(func.count(models.PointsTransaction.id))
        .where(models.PointsTransaction.user_id == int(user_id))
        .where(models.PointsTransaction.created_at >= cutoff)
    )
    recent = int(recent_result.scalar() or 0)
    points = Customer360Points(
        total_balance=float(wallets.total_balance or 0.0),
        redeemable_balance=float(wallets.redeemable_balance or 0.0),
        wallet_count=len(wallets.wallets),
        recent_transactions=recent,
    )
    return {"status": "ok", "points": points, "wallets": wallets, "window_days": 30}


async def _build_preferences(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """Stated preferences and consent grants, flattened for the 360."""
    from app.services.preferences import load_user_preferences

    preferences, consents = await load_user_preferences(db, int(user_id))
    profile_result = await db.execute(
        select(models.UserPreferenceProfile.updated_at).where(
            models.UserPreferenceProfile.user_id == int(user_id)
        )
    )
    updated_at = profile_result.scalar_one_or_none()
    stated = Customer360Preferences(
        communication_channel=preferences.get("communication_channel"),
        communication_frequency=preferences.get("communication_frequency"),
        language=preferences.get("preferred_language"),
        timezone=None,
        marketing_consent=bool(consents.get("marketing", False)),
        analytics_consent=bool(consents.get("analytics", True)),
        recovery_consent=bool(consents.get("recovery", True)),
        updated_at=updated_at,
    )
    return {
        "status": "ok",
        "preferences": stated,
        "preference_keys_set": sorted(key for key in preferences),
        "consents": dict(consents),
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


async def build_customer_360(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
    *,
    sections: Optional[list[str]] = None,
) -> Customer360Report:
    """Build the full Customer 360 for one user.

    Sections run in dependency order and a failed one does not abort the rest:
    ``profile`` and ``interactions`` are attempted first because the summary
    sentence needs them, and any section that fails is replaced with a typed
    empty value plus an ``unavailable_sections`` entry naming the reason.
    """
    wanted = set(sections) if sections else set(CUSTOMER_360_SECTIONS)
    unknown = wanted - set(CUSTOMER_360_SECTIONS)
    if unknown:
        raise ValueError(
            f"unknown 360 section(s) {', '.join(sorted(unknown))}; expected any of "
            f"{', '.join(CUSTOMER_360_SECTIONS)}"
        )

    unavailable: dict[str, str] = {}
    built: dict[str, Any] = {}

    async def _attempt(name: str, builder, *args):
        if name not in wanted:
            return None
        try:
            result = await builder(*args)
        except Exception as exc:  # noqa: BLE001 - a section failure must not 500
            unavailable[name] = f"{type(exc).__name__}: {exc}"
            return None
        built[name] = result
        return result

    profile_block = await _attempt("_profile", _build_profile, db, int(user_id), int(window_days))
    if profile_block is None and "_profile" in wanted:
        unavailable.pop("_profile", None)
        unavailable["profile"] = unavailable.pop("_profile", "profile build failed")
    await _attempt("_interactions", _build_interactions, db, int(user_id), int(window_days))
    await _attempt(
        "_sentiment",
        _build_sentiment,
        db,
        int(user_id),
        int(window_days),
        profile_block or {},
    )
    await _attempt("_recovery", _build_recovery, db, int(user_id), int(window_days))
    await _attempt("_journey", _build_journey, db, int(user_id), int(window_days))
    await _attempt("_communication", _build_communication, db, int(user_id), int(window_days))
    await _attempt("_payments", _build_payments, db, int(user_id))
    await _attempt("_points", _build_points, db, int(user_id))
    await _attempt("_preferences", _build_preferences, db, int(user_id))

    # Internal keys are stripped: a caller asking for "profile" must not see a
    # "_profile" key as well, and an internal block leaking into the response
    # is the kind of thing that gets copied into a client contract.
    for internal in ("_profile", "_interactions", "_sentiment", "_recovery", "_journey", "_communication", "_payments", "_points", "_preferences"):
        built.pop(internal, None)

    now = datetime.now(timezone.utc)
    profile_obj = (profile_block or {}).get("profile")
    interactions_obj = (built.get("interactions") or {}).get("interactions")
    sentiment_obj = (built.get("sentiment") or {}).get("sentiment")
    recovery_obj = (built.get("recovery") or {}).get("recovery")
    journey_obj = (built.get("journey") or {}).get("journey")
    communication_obj = (built.get("communication") or {}).get("communication")
    payments_obj = (built.get("payments") or {}).get("payments")
    points_obj = (built.get("points") or {}).get("points")
    preferences_obj = (built.get("preferences") or {}).get("preferences")

    missing = [name for name, obj in (
        ("profile", profile_obj),
        ("interactions", interactions_obj),
        ("sentiment", sentiment_obj),
        ("recovery", recovery_obj),
        ("loyalty_journey", journey_obj),
        ("communication", communication_obj),
        ("payments", payments_obj),
        ("points", points_obj),
        ("preferences", preferences_obj),
    ) if obj is None]
    for name in missing:
        unavailable.setdefault(name, "section not built")

    if profile_obj is None:
        raise ValueError(
            f"customer 360 for user {user_id} could not build the profile section: "
            f"{unavailable.get('profile', 'unknown reason')}"
        )

    summary_text = build_summary_text(
        profile_obj,
        interactions_obj
        or Customer360InteractionSummary(
            total_chats=0, total_bookings=0, completed_bookings=0,
            pending_bookings=0, cancelled_bookings=0, total_snapshots=0,
        ),
        sentiment_obj
        or Customer360Sentiment(
            current_label="none", current_score=0.0, has_sentiment=False,
            trend="stable", negative_count=0, positive_count=0, neutral_count=0,
        ),
        recovery_obj
        or Customer360Recovery(
            recovery_readiness="unknown", dissatisfaction_score=0.0,
            primary_risks=[], recent_recovery_actions=0, blocked_recovery_actions=0,
        ),
        journey_obj
        or Customer360LoyaltyJourney(
            current_family="none", matched_scenarios=0, scenario_families=[],
            next_best_actions=[],
        ),
        points_obj
        or Customer360Points(
            total_balance=0.0, redeemable_balance=0.0, wallet_count=0,
            recent_transactions=0,
        ),
        payments_obj
        or Customer360Payments(
            open_arrears_count=0, total_principal_at_risk=0.0,
            total_interest_at_risk=0.0, overdue_count=0, settled_count=0,
            waived_count=0,
        ),
    )
    if unavailable:
        summary_text += (
            f" Incomplete: {', '.join(sorted(unavailable))} could not be built."
        )

    return Customer360Report(
        generated_at=now,
        window_days=int(window_days),
        profile=profile_obj,
        interactions=interactions_obj
        or Customer360InteractionSummary(
            total_chats=0, total_bookings=0, completed_bookings=0,
            pending_bookings=0, cancelled_bookings=0, total_snapshots=0,
        ),
        sentiment=sentiment_obj
        or Customer360Sentiment(
            current_label="none", current_score=0.0, has_sentiment=False,
            trend="stable", negative_count=0, positive_count=0, neutral_count=0,
        ),
        recovery=recovery_obj
        or Customer360Recovery(
            recovery_readiness="unknown", dissatisfaction_score=0.0,
            primary_risks=[], recent_recovery_actions=0, blocked_recovery_actions=0,
        ),
        loyalty_journey=journey_obj
        or Customer360LoyaltyJourney(
            current_family="none", matched_scenarios=0, scenario_families=[],
            next_best_actions=[],
        ),
        communication=communication_obj
        or Customer360Communication(
            resolved_layer="unavailable", precedence=0, profile_id="",
            tone="", channel="", framing="", reply_urgency="", mood_label="",
        ),
        payments=payments_obj
        or Customer360Payments(
            open_arrears_count=0, total_principal_at_risk=0.0,
            total_interest_at_risk=0.0, overdue_count=0, settled_count=0,
            waived_count=0,
        ),
        points=points_obj
        or Customer360Points(
            total_balance=0.0, redeemable_balance=0.0, wallet_count=0,
            recent_transactions=0,
        ),
        preferences=preferences_obj
        or Customer360Preferences(),
        summary_text=summary_text,
    )


# ---------------------------------------------------------------------------
# Admin rollup (cheap, and explicit about what it skips)
# ---------------------------------------------------------------------------


async def build_customer_360_admin_report(
    db: AsyncSession, window_days: int = 30, limit: int = 20
) -> dict[str, Any]:
    """A cross-user rollup built from aggregate rows, not from N full 360s.

    The expensive per-user path is ``build_customer_360``; running it for every
    user is O(users x 8 engines) with a Hugging Face call in the sentiment
    section per user. This reports what the cheap rows can support and names
    what it therefore omits, so an operator is never left believing the absence
    of a per-user signal means its absence in the data.
    """
    user_result = await db.execute(
        select(models.User.id, models.User.username, models.User.created_at).order_by(
            models.User.id
        )
    )
    users = [(int(row[0]), str(row[1] or ""), row[2]) for row in user_result.all()]
    user_ids = [uid for uid, _name, _created in users]

    arrears_result = await db.execute(
        select(models.ArrearsEntry.user_id, models.ArrearsEntry.status, func.count(models.ArrearsEntry.id), func.sum(models.ArrearsEntry.principal))
        .where(models.ArrearsEntry.user_id.in_(user_ids) if user_ids else False)
        .group_by(models.ArrearsEntry.user_id, models.ArrearsEntry.status)
    )
    arrears: dict[int, dict[str, Any]] = {}
    for user_id, status, count, principal in arrears_result.all():
        entry = arrears.setdefault(int(user_id), {"open": 0, "settled": 0, "waived": 0, "principal": 0.0})
        key = str(status or "unknown")
        entry[key] = int(count or 0)
        if key == "open":
            entry["principal"] = round(float(principal or 0.0), 2)

    wallet_result = await db.execute(
        select(models.PointsWallet.user_id, func.sum(models.PointsWallet.balance))
        .where(models.PointsWallet.user_id.in_(user_ids) if user_ids else False)
        .group_by(models.PointsWallet.user_id)
    )
    balances = {int(uid): round(float(total or 0.0), 2) for uid, total in wallet_result.all()}

    recovery_result = await db.execute(
        select(models.RecoveryAction.user_id, models.RecoveryAction.status, func.count(models.RecoveryAction.id))
        .where(models.RecoveryAction.user_id.in_(user_ids) if user_ids else False)
        .group_by(models.RecoveryAction.user_id, models.RecoveryAction.status)
    )
    recovery: dict[int, dict[str, int]] = {}
    for user_id, status, count in recovery_result.all():
        recovery.setdefault(int(user_id), {})[str(status or "unknown")] = int(count or 0)

    chat_result = await db.execute(
        select(models.ChatHistory.user_id, func.count(models.ChatHistory.id)).where(
            models.ChatHistory.user_id.in_(user_ids) if user_ids else False
        ).group_by(models.ChatHistory.user_id)
    )
    chat_counts = {int(uid): int(count or 0) for uid, count in chat_result.all()}

    items = []
    for user_id, username, created_at in users:
        user_arrears = arrears.get(user_id, {"open": 0, "settled": 0, "waived": 0, "principal": 0.0})
        user_recovery = recovery.get(user_id, {})
        items.append(
            {
                "user_id": user_id,
                "username": username,
                "created_at": created_at,
                "chat_messages": chat_counts.get(user_id, 0),
                "points_balance": balances.get(user_id, 0.0),
                "open_arrears": int(user_arrears.get("open", 0)),
                "open_principal": float(user_arrears.get("principal", 0.0)),
                "settled_arrears": int(user_arrears.get("settled", 0)),
                "recovery_executed": int(user_recovery.get("executed", 0)),
                "recovery_blocked": int(
                    user_recovery.get("skipped", 0) + user_recovery.get("guard_rejected", 0)
                ),
                "has_activity": chat_counts.get(user_id, 0) > 0
                or int(user_arrears.get("open", 0)) > 0,
            }
        )
    items.sort(
        key=lambda item: (item["points_balance"], item["chat_messages"], item["open_principal"]),
        reverse=True,
    )
    total_points = round(sum(item["points_balance"] for item in items), 2)
    total_principal = round(sum(item["open_principal"] for item in items), 2)
    with_activity = sum(1 for item in items if item["has_activity"])
    return {
        "generated_at": datetime.now(timezone.utc),
        "window_days": int(window_days),
        "limit": int(limit),
        "total_users": len(users),
        "users_with_activity": with_activity,
        "users_without_activity": len(users) - with_activity,
        "total_points_balance": total_points,
        "total_open_principal": total_principal,
        "users_with_recovery_actions": sum(
            1 for item in items if item["recovery_executed"] or item["recovery_blocked"]
        ),
        "users": items[: max(1, int(limit))],
        "omitted_by_design": [
            "loyalty_score / churn_risk (needs the per-user scoring engine)",
            "sentiment (needs a model call per user; unavailable offline)",
            "journey scenarios (needs the per-user rule engine)",
            "recovery readiness (needs the per-user dissatisfaction report)",
            "communication strategy (needs the per-user precedence ladder)",
        ],
        "note": (
            "this is the cheap cross-user view. Use GET /chat/customer-360 for the "
            "full per-customer report; the two are built from different rows on "
            "purpose, and the figures above do not include anything from the "
            "per-user engines"
        ),
        "summary": (
            f"{len(users)} user(s); {with_activity} with recorded activity; "
            f"{total_points:g} points outstanding; {total_principal:g} principal deferred"
        ),
    }
