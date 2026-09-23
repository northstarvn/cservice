"""Data-driven "next-best-action" engine for the new-customer -> loyal-customer journey.

Hypothesis
----------
A new customer becomes a loyal one by progressing through a small set of
recurring scenarios. Each scenario is a situation (bookings, chats, sentiment,
stage, dormancy, churn risk) plus the actions that most reliably push the
customer one step closer to loyalty. New -> loyal pathways we cater for:

onboarding
    welcome/discovery for brand-new users, and first-value activation so the
    new customer actually completes their first service.
activation
    pending first booking (abandonment risk) and repeated questions around a
    booking that is not moving forward.
delivery
    confirmed-but-not-yet-completed bookings (in-flight status visibility) and
    post-service delight moments after a completed booking (review, referral).
habit
    converting a first completed booking into a repeat-booking habit and
    loyalty-program enrollment.
recovery
    cancelled bookings, negative sentiment, and repeated concerns — the
    "save the customer before they leave" scenarios.
retention
    follow-up gaps (open loops never closed) and dormancy win-back for
    customers who already experienced value but went quiet.
churn
    high-churn customers get save offers and immediate outreach before any
    monetization attempt.
trust
    cancelled bookings with no completed history → trust assurance and
    transparent commitments instead of offers.
monetization
    expansion-ready (loyal + high readiness) and growth cross-sell for users
    who already completed bookings.
data
    when total signals are too thin, collect more data and hold aggressive
    offers rather than guessing wrong.

Implementation
--------------
`LOYALTY_SCENARIO_CATALOG` is a data table, not code: each rule declares a
`when` condition DSL evaluated against a `JourneyContext`. Adding a new
scenario (or a new tier/timing for an existing one) is a config change only.

The condition DSL supports:
  - scalar value  -> equality (context == value), or membership when the
    context field is a list ("top_issues": "follow up").
  - list value    -> membership (context in values).
  - dict value    -> numeric comparisons, keys: gte, gt, lte, lt, eq, ne
    ("completed_bookings": {"lte": 0}).

Unknown condition fields fail safe (never match), and the catalog self-check
tests reject typos at test time.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas.chat import (
    ChurnPrediction,
    InteractionSummary,
    LoyaltyJourneyAdminItem,
    LoyaltyJourneyAdminReport,
    LoyaltyJourneyPlan,
    LoyaltyJourneyScenarioItem,
    Sentiment,
)
from app.services.chat_analytics import (
    analyze_sentiment,
    build_churn_prediction,
    build_summary,
    classify_lifecycle_stage,
    load_user_interaction_window,
)

# How long (in days) with no activity before a customer with history is treated
# as dormant and routed into the win-back scenario.
DORMANCY_THRESHOLD_DAYS = 14

PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
PRIORITY_WEIGHT: dict[str, int] = {"high": 3, "medium": 2, "low": 1}

_NUMERIC_OPS = {
    "gte": operator.ge,
    "gt": operator.gt,
    "lte": operator.le,
    "lt": operator.lt,
    "eq": operator.eq,
    "ne": operator.ne,
}


# ---------------------------------------------------------------------------
# Scenario catalog (the hypothesis, machine-readable)
# ---------------------------------------------------------------------------
# Each rule:
#   scenario   unique id
#   family     grouping shown in admin rollups
#   goal       what this scenario does for the journey
#   priority   high/medium/low (sorting + admin priority_rank)
#   owner_hint which function owns executing the actions
#   when       condition DSL over JourneyContext fields (all must hold)
#   actions    ordered next-best actions for the customer
#   kpis       the outcomes this scenario should move
#   when_hint  human readable trigger description (used as evidence)
LOYALTY_SCENARIO_CATALOG: list[dict[str, object]] = [
    {
        "scenario": "discovery_and_welcome",
        "family": "onboarding",
        "goal": "Give brand-new customers a clear, low-friction introduction to the service.",
        "priority": "high",
        "owner_hint": "product and onboarding",
        "when": {"lifecycle_stage": "new", "has_history": False},
        "actions": [
            "Send a welcome sequence that explains the first booking in 3 steps",
            "Collect channel, language, and timing preferences",
            "Offer a guided walkthrough with a first-success milestone",
        ],
        "kpis": ["first-chat-to-first-booking rate", "welcome completion rate"],
        "when_hint": "brand-new customer with no interaction history",
    },
    {
        "scenario": "first_value_activation",
        "family": "onboarding",
        "goal": "Move the new customer to their first successful service completion.",
        "priority": "high",
        "owner_hint": "product and onboarding",
        "when": {
            "lifecycle_stage": ["new", "engaged"],
            "completed_bookings": {"lte": 0},
            "signal_total": {"gte": 1},
        },
        "actions": [
            "Remove first-booking friction with a guided booking checklist",
            "Define a clear first-success milestone and celebrate it on completion",
            "Offer an assistant prompt for the first booking attempt",
        ],
        "kpis": ["time-to-first-value", "first-booking completion rate"],
        "when_hint": "active but the first service has not been completed yet",
    },
    {
        "scenario": "first_booking_pending",
        "family": "activation",
        "goal": "Stop the first booking from stalling in a pending state.",
        "priority": "high",
        "owner_hint": "product and frontend",
        "when": {"pending_bookings": {"gte": 1}, "completed_bookings": {"lte": 0}},
        "actions": [
            "Confirm booking details and clarify exactly what happens next",
            "Surface a visible status timeline so the wait is predictable",
            "Send an abandoned-cart style recovery nudge for the pending booking",
        ],
        "kpis": ["pending-to-confirmed rate", "first-booking drop-off"],
        "when_hint": "first booking is still pending",
    },
    {
        "scenario": "booking_abandonment_risk",
        "family": "activation",
        "goal": "Detect booking abandonment risk early and re-engage before the customer gives up.",
        "priority": "medium",
        "owner_hint": "support operations",
        "when": {"pending_bookings": {"gte": 1}, "repeated_messages": {"gte": 1}},
        "actions": [
            "Offer a callback or proactive ETA update for the stuck booking",
            "Audit the booking flow for the friction the repetition points at",
            "Escalate to a human when the same question repeats a third time",
        ],
        "kpis": ["pended-booking resolution time", "repeat-question rate on bookings"],
        "when_hint": "pending booking plus repeated customer questions",
    },
    {
        "scenario": "delivery_in_flight",
        "family": "delivery",
        "goal": "Keep confirmed bookings on track with proactive status visibility.",
        "priority": "medium",
        "owner_hint": "service operations",
        "when": {"confirmed_bookings": {"gte": 1}, "completed_bookings": {"lte": 0}},
        "actions": [
            "Send proactive status updates at each service milestone",
            "Make response-time and SLA expectations visible",
            "Attach a pre-service preparation checklist",
        ],
        "kpis": ["on-time completion rate", "pre-service questions answered proactively"],
        "when_hint": "confirmed booking not yet completed",
    },
    {
        "scenario": "post_service_delight",
        "family": "delivery",
        "goal": "Turn a successfully completed service into a delight moment and social proof.",
        "priority": "low",
        "owner_hint": "customer success",
        "when": {"completed_bookings": {"gte": 1}, "sentiment_label": ["positive", "neutral", "none"]},
        "actions": [
            "Send a thank-you plus a review request at the peak of satisfaction",
            "Prompt a referral at the moment of completion",
            "Share a social-proof highlight with the customer",
        ],
        "kpis": ["post-service review rate", "referral conversion"],
        "when_hint": "service completed with non-negative sentiment",
    },
    {
        "scenario": "completion_to_return",
        "family": "habit",
        "goal": "Convert a happy one-time customer into a repeat-booking habit.",
        "priority": "low",
        "owner_hint": "growth and CRM",
        "when": {"completed_bookings": {"gte": 1}, "loyalty_score": {"gte": 70}},
        "actions": [
            "Offer a repeat-booking incentive for the next service",
            "Invite the customer into a loyalty program or membership tier",
            "Enable one-tap rebooking from saved preferences",
        ],
        "kpis": ["repeat-booking rate", "loyalty program enrollment"],
        "when_hint": "at least one completed booking with high loyalty score",
    },
    {
        "scenario": "service_recovery",
        "family": "recovery",
        "goal": "Rescue cancelled bookings before they become churn.",
        "priority": "high",
        "owner_hint": "support and success",
        "when": {"cancelled_bookings": {"gte": 1}},
        "actions": [
            "Ask for the cancellation reason via a lightweight callback",
            "Make rebooking one-click and offer a goodwill token",
            "Fix the root cause before any monetization attempt",
        ],
        "kpis": ["cancellation recovery rate", "rebooking rate after cancellation"],
        "when_hint": "customer cancelled a booking in the window",
    },
    {
        "scenario": "negative_sentiment_recovery",
        "family": "recovery",
        "goal": "Intercept negative sentiment immediately before it escalates.",
        "priority": "high",
        "owner_hint": "support and success",
        "when": {"sentiment_label": "negative"},
        "actions": [
            "Acknowledge the issue and route to a fast human follow-up",
            "Offer an apology path and clear escalation options",
            "Close the loop explicitly when the issue is resolved",
        ],
        "kpis": ["negative-to-resolved time", "repeat negative interactions"],
        "when_hint": "latest message carries negative sentiment",
    },
    {
        "scenario": "repeated_concern_loop",
        "family": "recovery",
        "goal": "Close the loop on concerns that keep repeating across messages.",
        "priority": "high",
        "owner_hint": "customer success",
        "when": {"repeated_messages": {"gte": 2}},
        "actions": [
            "Track the repeated concern as a closed-loop case with a single owner",
            "Confirm understanding and a concrete resolution date",
            "Escalate at the third repetition to avoid compounding frustration",
        ],
        "kpis": ["repeated-concern resolution rate", "repetition count per customer"],
        "when_hint": "the same concern repeats across messages",
    },
    {
        "scenario": "follow_up_gap",
        "family": "retention",
        "goal": "Close follow-up gaps where conversations happen but nothing is resolved.",
        "priority": "medium",
        "owner_hint": "customer success",
        "when": {"top_issues": "follow up", "completed_bookings": {"lte": 0}},
        "actions": [
            "Schedule a proactive check-in rather than waiting for the customer",
            "Assign callback ownership with a time commitment",
            "Confirm resolution in a follow-up message once fixed",
        ],
        "kpis": ["follow-up completion rate", "unresolved-loop count"],
        "when_hint": "follow-up language detected with no completed service",
    },
    {
        "scenario": "dormancy_winback",
        "family": "retention",
        "goal": "Win back customers who experienced value but went quiet.",
        "priority": "medium",
        "owner_hint": "growth and CRM",
        "when": {"dormant": True, "had_bookings": True},
        "actions": [
            "Launch a win-back campaign highlighting what is new since they left",
            "Offer a low-friction return path with their saved preferences",
            "Re-engage with a targeted value-reminder message",
        ],
        "kpis": ["win-back reactivation rate", "time-to-return for dormant customers"],
        "when_hint": "dormant customer with a successful service history",
    },
    {
        "scenario": "churn_risk_save",
        "family": "churn",
        "goal": "Save high-churn customers with immediate outreach and issue resolution first.",
        "priority": "high",
        "owner_hint": "retention ops",
        "when": {"at_risk": True},
        "actions": [
            "Trigger immediate retention outreach with a save offer",
            "Prioritize issue resolution above any upsell attempt",
            "Reaffirm the value already delivered to the customer",
        ],
        "kpis": ["high-churn save rate", "outreach response time"],
        "when_hint": "churn risk is high (score or prediction critical)",
    },
    {
        "scenario": "trust_building",
        "family": "trust",
        "goal": "Rebuild trust after cancellations before any new commitment is asked.",
        "priority": "medium",
        "owner_hint": "product strategy",
        "when": {"cancelled_bookings": {"gte": 1}, "completed_bookings": {"lte": 0}},
        "actions": [
            "Surface trust assurance: guarantees, refund clarity, and commitments",
            "Make status updates and promise handling transparent",
            "Offer secure, flexible rebooking terms",
        ],
        "kpis": ["post-cancellation trust recovery", "rebooking intent"],
        "when_hint": "cancelled booking with no completed service history",
    },
    {
        "scenario": "expansion_ready",
        "family": "monetization",
        "goal": "Expand value for loyal high-readiness customers through referrals and premium handling.",
        "priority": "low",
        "owner_hint": "product strategy",
        "when": {"loyalty_score": {"gte": 80}, "monetization_readiness": {"gte": 80}, "completed_bookings": {"gte": 1}},
        "actions": [
            "Invite to a referral program with mutual rewards",
            "Offer VIP priority handling and a premium membership tier",
            "Test tailored upsell and cross-sell with this high-trust segment",
        ],
        "kpis": ["referral rate", "premium tier adoption", "expansion revenue"],
        "when_hint": "loyal, high-value customer ready for expansion",
    },
    {
        "scenario": "growth_cross_sell",
        "family": "monetization",
        "goal": "Cross-sell adjacent services to customers who already completed bookings.",
        "priority": "low",
        "owner_hint": "product strategy",
        "when": {"monetization_readiness": {"gte": 70}, "completed_bookings": {"gte": 1}},
        "actions": [
            "Recommend a complementary service tailored to their history",
            "Bundle the next visit with a value-expansion offer",
            "Measure offer response before scaling",
        ],
        "kpis": ["cross-sell conversion", "bundle adoption"],
        "when_hint": "growth-ready customer with completed bookings",
    },
    {
        "scenario": "data_sufficiency",
        "family": "data",
        "goal": "Avoid wrong offers when signals are too thin to act on.",
        "priority": "low",
        "owner_hint": "analytics and product",
        "when": {"signal_total": {"lte": 1}, "completed_bookings": {"lte": 0}},
        "actions": [
            "Enrich signal collection with guided activation interaction",
            "Hold aggressive offers until more evidence accumulates",
            "Run a light check-in to gather one more data point",
        ],
        "kpis": ["signal coverage growth", "offer accuracy"],
        "when_hint": "too few chat/booking signals to infer patterns",
    },
]

SCENARIO_ORDER: dict[str, int] = {
    str(rule["scenario"]): index for index, rule in enumerate(LOYALTY_SCENARIO_CATALOG)
}


# ---------------------------------------------------------------------------
# Journey context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JourneyContext:
    """Computed, JSON-safe picture of where the customer is in the journey.

    All fields are plain primitives or lists so the catalog conditions can be
    evaluated generically and serialized for evidence without conversion.
    """
    user_id: int
    lifecycle_stage: str
    loyalty_score: float
    churn_risk: str
    monetization_readiness: float
    value_tier: str
    customer_classification: str
    sentiment_label: str
    has_sentiment: bool
    chat_count: int
    booking_count: int
    completed_bookings: int
    confirmed_bookings: int
    pending_bookings: int
    cancelled_bookings: int
    repeated_messages: int
    signal_count: int
    signal_strength: float
    risk_level: str
    risk_score: float
    at_risk: bool
    has_history: bool
    had_bookings: bool
    dormant: bool
    days_since_last_activity: Optional[int]
    top_issues: list[str] = field(default_factory=list)
    signal_total: int = 0


# ---------------------------------------------------------------------------
# Condition DSL
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return value


def _matches_rule(rule_value: Any, context_value: Any) -> bool:
    if isinstance(rule_value, dict):
        for op_name, threshold in rule_value.items():
            op = _NUMERIC_OPS.get(op_name)
            if op is None or context_value is None:
                return False
            try:
                if not op(context_value, threshold):
                    return False
            except TypeError:
                return False
        return True
    if isinstance(context_value, (list, tuple, set)):
        return _json_safe(rule_value) in list(context_value)
    if isinstance(rule_value, (list, tuple, set, frozenset)):
        return context_value in rule_value
    if isinstance(rule_value, bool):
        return bool(context_value) == rule_value
    return context_value == rule_value


def evaluate_scenario_when(when: dict[str, object], context: JourneyContext) -> tuple[bool, dict[str, object]]:
    """Evaluate every condition in `when` against `context` (AND semantics).

    Returns (matched, matched_fields) where matched_fields maps each condition
    field to the context value that satisfied it. Unknown fields fail the whole
    scenario (fail-safe) — catalog self-check tests catch typos.
    """
    matched_fields: dict[str, object] = {}
    for field_name, rule_value in when.items():
        if not hasattr(context, field_name):
            return False, {}
        context_value = getattr(context, field_name)
        if not _matches_rule(rule_value, context_value):
            return False, {}
        matched_fields[field_name] = _json_safe(context_value)
    return True, matched_fields


def match_loyalty_scenarios(context: JourneyContext) -> list[dict[str, object]]:
    """Return every catalog scenario whose conditions hold, enriched with `matched_conditions`."""
    matched: list[dict[str, object]] = []
    for rule in LOYALTY_SCENARIO_CATALOG:
        ok, matched_fields = evaluate_scenario_when(dict(rule["when"]), context)
        if ok:
            matched.append({**rule, "matched_conditions": matched_fields})
    matched.sort(
        key=lambda rule: (
            PRIORITY_RANK.get(str(rule.get("priority")), 2),
            SCENARIO_ORDER.get(str(rule.get("scenario")), 999),
        )
    )
    return matched


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def _booking_state_count(summary: InteractionSummary, status: str) -> int:
    metadata = getattr(summary, "metadata", None) or {}
    booking_states = metadata.get("booking_states", {}) or {}
    try:
        return int(booking_states.get(status, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _days_since(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return max(0, int((datetime.now(timezone.utc) - value).days))
    except (TypeError, ValueError):
        return None


def build_journey_context(
    summary: InteractionSummary,
    churn_prediction: ChurnPrediction,
    chat_rows: list[models.ChatHistory],
    bookings: list[models.Booking],
    sentiment: Optional[Sentiment],
    *,
    total_chat_count: int = 0,
    total_booking_count: int = 0,
    latest_chat_at: Optional[datetime] = None,
    latest_booking_at: Optional[datetime] = None,
) -> JourneyContext:
    chat_count = len(chat_rows)
    booking_count = len(bookings)
    has_history = int(total_chat_count or 0) > 0 or int(total_booking_count or 0) > 0
    had_bookings = int(total_booking_count or 0) > 0

    latest_activity_at = latest_booking_at or latest_chat_at
    days_since_last_activity = _days_since(latest_activity_at)
    inactive_in_window = chat_count == 0 and booking_count == 0
    dormant = (
        has_history
        and inactive_in_window
        and days_since_last_activity is not None
        and days_since_last_activity >= DORMANCY_THRESHOLD_DAYS
    )

    stage, _confidence, _drivers, _focus = classify_lifecycle_stage(
        summary, churn_prediction, chat_count, booking_count
    )
    metadata = getattr(summary, "metadata", None) or {}
    try:
        repeated_messages = int(metadata.get("repeated_messages", 0) or 0)
        signal_strength = float(metadata.get("signal_strength", 0.0) or 0.0)
    except (TypeError, ValueError):
        repeated_messages = int(metadata.get("repeated_messages", 0) or 0)
        signal_strength = 0.0

    return JourneyContext(
        user_id=summary.user_id,
        lifecycle_stage=stage,
        loyalty_score=float(summary.loyalty_score),
        churn_risk=str(summary.churn_risk),
        monetization_readiness=float(summary.monetization_readiness),
        value_tier=str(summary.value_tier),
        customer_classification=str(summary.customer_classification),
        sentiment_label=sentiment.label if sentiment else "none",
        has_sentiment=sentiment is not None,
        chat_count=chat_count,
        booking_count=booking_count,
        completed_bookings=_booking_state_count(summary, "completed"),
        confirmed_bookings=_booking_state_count(summary, "confirmed"),
        pending_bookings=_booking_state_count(summary, "pending"),
        cancelled_bookings=_booking_state_count(summary, "cancelled"),
        repeated_messages=repeated_messages,
        signal_count=len(summary.insights),
        signal_strength=signal_strength,
        risk_level=str(churn_prediction.risk_level),
        risk_score=float(churn_prediction.risk_score),
        at_risk=str(summary.churn_risk) == "high" or str(churn_prediction.risk_level) in {"high", "critical"},
        has_history=has_history,
        had_bookings=had_bookings,
        dormant=dormant,
        days_since_last_activity=days_since_last_activity,
        top_issues=list(summary.top_issues),
        signal_total=chat_count + booking_count,
    )


def resolve_top_journey_family(
    summary: InteractionSummary,
    churn_prediction: ChurnPrediction,
    chat_rows: list[models.ChatHistory],
    bookings: list[models.Booking],
    sentiment: Optional[Sentiment],
    *,
    total_chat_count: int = 0,
    total_booking_count: int = 0,
    latest_chat_at: Optional[datetime] = None,
    latest_booking_at: Optional[datetime] = None,
) -> str:
    """Return the top-priority journey family for a user, or "none".

    Lightweight version of `build_loyalty_journey_plan` that only resolves the
    dominant family — used by monitoring surfaces that need one label per user
    (e.g. the activity-tree grouping axis).
    """
    context = build_journey_context(
        summary,
        churn_prediction,
        chat_rows,
        bookings,
        sentiment,
        total_chat_count=total_chat_count,
        total_booking_count=total_booking_count,
        latest_chat_at=latest_chat_at,
        latest_booking_at=latest_booking_at,
    )
    matched = match_loyalty_scenarios(context)
    if not matched:
        return "none"
    return str(matched[0]["family"])


# ---------------------------------------------------------------------------
# Plan / report builders
# ---------------------------------------------------------------------------


def build_loyalty_journey_plan(
    user_id: int,
    window_days: int,
    summary: InteractionSummary,
    churn_prediction: ChurnPrediction,
    context: JourneyContext,
) -> LoyaltyJourneyPlan:
    matched = match_loyalty_scenarios(context)
    scenario_items: list[LoyaltyJourneyScenarioItem] = []
    for rule in matched:
        matched_conditions = dict(rule.get("matched_conditions", {}))
        evidence = [str(rule.get("when_hint", rule["scenario"]))]
        evidence += [f"{field}={_json_safe(value)}" for field, value in matched_conditions.items()]
        scenario_items.append(
            LoyaltyJourneyScenarioItem(
                scenario=str(rule["scenario"]),
                family=str(rule["family"]),
                goal=str(rule["goal"]),
                priority=str(rule["priority"]),
                owner_hint=str(rule["owner_hint"]),
                actions=[str(action) for action in rule["actions"]],
                kpis=[str(kpi) for kpi in rule["kpis"]],
                matched_conditions=matched_conditions,
                evidence=evidence,
            )
        )

    scenario_families = list(dict.fromkeys(item.family for item in scenario_items))
    next_best_actions: list[str] = []
    for item in scenario_items:
        for action in item.actions:
            if action not in next_best_actions:
                next_best_actions.append(action)
    next_best_actions = next_best_actions[:7]

    top_family = scenario_families[0] if scenario_families else "none"
    summary_text = (
        f"stage={context.lifecycle_stage}, matched={len(scenario_items)} scenarios across "
        f"{len(scenario_families)} families (top_family={top_family}), "
        f"next_best_actions={len(next_best_actions)}, "
        f"loyalty={summary.loyalty_score:.1f}, churn_risk={summary.churn_risk}, "
        f"monetization_readiness={summary.monetization_readiness:.1f}"
    )

    return LoyaltyJourneyPlan(
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        window_days=window_days,
        lifecycle_stage=context.lifecycle_stage,
        churn_risk=str(context.churn_risk),
        loyalty_score=context.loyalty_score,
        monetization_readiness=context.monetization_readiness,
        matched_scenario_count=len(scenario_items),
        scenario_families=scenario_families,
        next_best_actions=next_best_actions,
        scenario_items=scenario_items,
        summary_text=summary_text,
    )


async def load_history_totals(db: AsyncSession, user_id: int) -> dict[str, object]:
    """Total chat/booking counts and latest activity timestamps across all time."""
    chat_count_result = await db.execute(
        select(func.count(models.ChatHistory.id)).where(models.ChatHistory.user_id == user_id)
    )
    booking_count_result = await db.execute(
        select(func.count(models.Booking.id)).where(models.Booking.user_id == user_id)
    )
    latest_chat_result = await db.execute(
        select(func.max(models.ChatHistory.timestamp)).where(models.ChatHistory.user_id == user_id)
    )
    latest_booking_result = await db.execute(
        select(func.max(models.Booking.created_at)).where(models.Booking.user_id == user_id)
    )
    return {
        "total_chat_count": int(chat_count_result.scalar() or 0),
        "total_booking_count": int(booking_count_result.scalar() or 0),
        "latest_chat_at": latest_chat_result.scalar(),
        "latest_booking_at": latest_booking_result.scalar(),
    }


async def _load_history_totals(db: AsyncSession, user_id: int) -> dict[str, object]:
    return await load_history_totals(db, user_id)


async def build_loyalty_journey_plan_for_user(
    db: AsyncSession, user_id: int, window_days: int
) -> LoyaltyJourneyPlan:
    chat_rows, bookings = await load_user_interaction_window(db, user_id, window_days)
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
    churn_prediction = await build_churn_prediction(db, user_id, window_days)
    totals = await _load_history_totals(db, user_id)
    context = build_journey_context(
        summary,
        churn_prediction,
        chat_rows,
        bookings,
        latest_sentiment,
        total_chat_count=int(totals["total_chat_count"]),
        total_booking_count=int(totals["total_booking_count"]),
        latest_chat_at=totals["latest_chat_at"],
        latest_booking_at=totals["latest_booking_at"],
    )
    return build_loyalty_journey_plan(user_id, window_days, summary, churn_prediction, context)


async def build_loyalty_journey_admin_report(
    db: AsyncSession, window_days: int, limit: int = 20
) -> LoyaltyJourneyAdminReport:
    user_rows = await db.execute(select(models.User.id, models.User.username))
    users = [(int(row[0]), str(row[1])) for row in user_rows.all()]

    items: list[LoyaltyJourneyAdminItem] = []
    family_counts: dict[str, int] = {}
    total_matched = 0
    for user_id, username in users:
        plan = await build_loyalty_journey_plan_for_user(db, user_id, window_days)
        matched = plan.scenario_items
        priority_rank = sum(PRIORITY_WEIGHT.get(item.priority, 1) for item in matched)
        for family in plan.scenario_families:
            family_counts[family] = family_counts.get(family, 0) + 1
        total_matched += len(matched)
        top_action = matched[0].actions[0] if matched and matched[0].actions else "Collect more signals"
        items.append(
            LoyaltyJourneyAdminItem(
                user_id=user_id,
                username=username,
                lifecycle_stage=plan.lifecycle_stage,
                churn_risk=plan.churn_risk,
                matched_scenario_count=len(matched),
                scenario_families=list(plan.scenario_families),
                top_action=top_action,
                priority_rank=priority_rank,
            )
        )

    items.sort(key=lambda item: item.priority_rank, reverse=True)
    users_with_scenarios = sum(1 for item in items if item.matched_scenario_count)
    top_family = max(family_counts, key=family_counts.get) if family_counts else None
    return LoyaltyJourneyAdminReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        total_users=len(users),
        users_with_scenarios=users_with_scenarios,
        total_matched=total_matched,
        coverage_by_family=family_counts,
        top_family=top_family,
        users=items[: max(1, int(limit))],
    )


# ---------------------------------------------------------------------------
# Catalog discovery
# ---------------------------------------------------------------------------


def build_loyalty_scenario_catalog() -> dict[str, object]:
    """Return the live journey-scenario rules as a JSON-friendly catalog.

    Operators stay intact so admin surfaces can display/validate the rules, but
    every entry also lists the resolved `condition_fields` against the context
    contract. New scenarios added to `LOYALTY_SCENARIO_CATALOG` automatically
    appear here and in the matching engine.
    """
    scenarios: list[dict[str, object]] = []
    for rule in LOYALTY_SCENARIO_CATALOG:
        scenarios.append(
            {
                "scenario": rule["scenario"],
                "family": rule["family"],
                "goal": rule["goal"],
                "priority": rule["priority"],
                "owner_hint": rule["owner_hint"],
                "actions": list(rule["actions"]),
                "kpis": list(rule["kpis"]),
                "when": dict(rule["when"]),
                "when_hint": rule["when_hint"],
                "condition_fields": sorted(rule["when"].keys()),
            }
        )
    families = list(dict.fromkeys(str(rule["family"]) for rule in LOYALTY_SCENARIO_CATALOG))
    return {
        "catalog_version": "loyalty_journey_v1",
        "total_scenarios": len(scenarios),
        "families": families,
        "priority_levels": ["high", "medium", "low"],
        "context_fields": sorted(JourneyContext.__dataclass_fields__),
        "dormancy_threshold_days": DORMANCY_THRESHOLD_DAYS,
        "scenarios": scenarios,
    }