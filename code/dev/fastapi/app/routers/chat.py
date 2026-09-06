from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, desc
from app import deps, models
from app.schemas.chat import (
    ChatMessageIn,
    ChatMessageOut,
    Sentiment,
    ChatHistoryCreate,
    ChatHistoryOut,
    InteractionInsight,
    InteractionSummary,
    SystemImprovementItem,
    SystemImprovementPack,
    InteractionTrendItem,
    InteractionTrendReport,
    RetentionCohortItem,
    RetentionCohortReport,
    RetentionCohortMember,
    RetentionCohortDrilldownReport,
    ChurnPrediction,
    LifecycleStageItem,
    LifecycleStageReport,
    RetentionSnapshotItem,
    RetentionSnapshotReport,
    RetentionSnapshotDelta,
    RetentionSnapshotTrendItem,
    RetentionSnapshotTrendReport,
    RetentionDashboard,
    RetentionMaintenanceResult,
    RetentionMaintenanceReport,
    UserActivityReport,
    AdminActivityReport,
    ActivityTimelineItem,
    ActivityTimelineReport,
    RankedUserItem,
    RankedUserReport,
    AdminRetentionTrendItem,
    AdminRetentionTrendReport,
    RetentionSnapshotAdminItem,
    RetentionSnapshotAdminReport,
    UserRetentionSnapshotHealth,
    RetentionSnapshotComparisonItem,
    RetentionSnapshotComparisonReport,
    RetentionSnapshotMomentumItem,
    RetentionSnapshotMomentumReport,
    RetentionSnapshotVolatilityItem,
    RetentionSnapshotVolatilityReport,
    RetentionSnapshotVolatilitySummary,
    RetentionSnapshotRiskProfile,
    RetentionSnapshotRecommendation,
    RetentionSnapshotActionPlan,
    RetentionSnapshotAuditItem,
    RetentionSnapshotAuditReport,
    RetentionSnapshotAuditExport,
    RetentionSnapshotTypeBreakdownItem,
    RetentionSnapshotTypeBreakdownReport,
    RetentionSnapshotStalenessItem,
    RetentionSnapshotStalenessReport,
    RetentionSnapshotStalenessTrendItem,
    RetentionSnapshotStalenessTrendReport,
    RetentionSnapshotHealthScore,
    RetentionSnapshotHealthSummary,
    RetentionSnapshotHealthRisk,
    RetentionSnapshotHealthRecommendation,
    RetentionSnapshotOperationsOverview,
    RetentionSnapshotOperationsStatus,
    RetentionSnapshotOperationsCompliance,
    RetentionSnapshotOperationsPosture,
    RetentionSnapshotOperationsAutomation,
    RetentionSnapshotOperationsExecutionState,
    RetentionSnapshotOperationsLaunchReadiness,
    RetentionSnapshotOperationsGoNoGo,
    MonetizationCohortReport,
    WeightedSystemMonitoringReport,
    WeightedFocusItem,
)
from app.schemas.schemas import UserOut
from app.services.chat_analytics import build_monetization_cohorts
from fastapi.responses import JSONResponse
import requests,os
from dotenv import load_dotenv
from collections import Counter
from datetime import datetime, timedelta, timezone
import json

# Load environment variables from .env file
load_dotenv()

router = APIRouter()

HF_SENTIMENT_URL = "https://api-inference.huggingface.co/models/distilbert-base-uncased-finetuned-sst-2-english"
HF_API_TOKEN = os.getenv("HF_API_TOKEN")

RETENTION_KEYWORDS = {
    "slow": "response_speed",
    "delay": "response_speed",
    "wait": "response_speed",
    "confusing": "clarity",
    "unclear": "clarity",
    "hard": "friction",
    "bug": "reliability",
    "error": "reliability",
    "issue": "reliability",
    "cancel": "booking_flow",
    "refund": "trust",
    "price": "pricing",
    "expensive": "pricing",
    "repeat": "retention",
    "again": "retention",
    "recommend": "recommendations",
    "help": "support",
    "support": "support",
}

PREDEFINED_POLICY_AREAS = [
    "response_speed",
    "clarity",
    "reliability",
    "booking_flow",
    "support",
    "pricing",
    "retention",
    "sentiment_recovery",
    "onboarding",
    "notification_quality",
    "self_service",
    "handoff",
    "trust",
    "follow_up",
]

POLICY_CONFIGS = {
    "response_speed": {
        "recommendation": "Reduce wait times and make response status visible to users.",
        "next_step": "Add response-time tracking and auto-acknowledgements for long-running requests.",
        "priority": "high",
        "impact": "high",
        "owner_hint": "backend and ops",
    },
    "clarity": {
        "recommendation": "Rewrite confusing flows and simplify user-facing prompts.",
        "next_step": "Review the most repeated support phrases and shorten the required form copy.",
        "priority": "high",
        "impact": "high",
        "owner_hint": "product and content",
    },
    "reliability": {
        "recommendation": "Stabilize error-prone journeys before expanding features.",
        "next_step": "Log failure points by endpoint and surface recoverable errors to users.",
        "priority": "high",
        "impact": "high",
        "owner_hint": "backend engineering",
    },
    "booking_flow": {
        "recommendation": "Make booking changes easier and reduce abandonment in pending states.",
        "next_step": "Track booking drop-offs, cancellations, and time-to-confirmation.",
        "priority": "medium",
        "impact": "medium",
        "owner_hint": "product and frontend",
    },
    "support": {
        "recommendation": "Offer more proactive support and clearer escalation paths.",
        "next_step": "Add guided help for frequent questions and route high-friction cases sooner.",
        "priority": "medium",
        "impact": "medium",
        "owner_hint": "customer support",
    },
    "pricing": {
        "recommendation": "Clarify pricing and value messaging to reduce hesitation.",
        "next_step": "Test pricing explanations and highlight service outcomes more clearly.",
        "priority": "medium",
        "impact": "medium",
        "owner_hint": "product strategy",
    },
    "retention": {
        "recommendation": "Create a closed-loop retention workflow for unresolved interactions.",
        "next_step": "Trigger follow-up prompts when the same concern repeats across sessions.",
        "priority": "high",
        "impact": "high",
        "owner_hint": "growth and CRM",
    },
    "sentiment_recovery": {
        "recommendation": "Route negative sentiment into a recovery flow before the issue escalates.",
        "next_step": "Escalate negative messages to a fast human follow-up queue.",
        "priority": "high",
        "impact": "high",
        "owner_hint": "support and success",
    },
    "onboarding": {
        "recommendation": "Tighten onboarding so first-time users reach value faster.",
        "next_step": "Reduce the number of first-run steps and clarify the first success milestone.",
        "priority": "high",
        "impact": "high",
        "owner_hint": "product and onboarding",
    },
    "notification_quality": {
        "recommendation": "Make notifications more timely, relevant, and easier to act on.",
        "next_step": "Audit notification timing, duplication, and message usefulness.",
        "priority": "medium",
        "impact": "medium",
        "owner_hint": "product and communications",
    },
    "self_service": {
        "recommendation": "Expand self-service coverage so users can resolve common issues independently.",
        "next_step": "Document the top repeated requests and add guided resolution paths.",
        "priority": "medium",
        "impact": "medium",
        "owner_hint": "support operations",
    },
    "handoff": {
        "recommendation": "Improve handoff quality between automated and human support.",
        "next_step": "Pass context, history, and intent into escalation workflows.",
        "priority": "high",
        "impact": "high",
        "owner_hint": "support engineering",
    },
    "trust": {
        "recommendation": "Strengthen trust signals around reliability, refunds, and commitments.",
        "next_step": "Surface clearer status updates and promise handling policies.",
        "priority": "medium",
        "impact": "medium",
        "owner_hint": "product strategy",
    },
    "follow_up": {
        "recommendation": "Build stronger follow-up loops for unresolved customer needs.",
        "next_step": "Schedule proactive check-ins when issues remain open across sessions.",
        "priority": "high",
        "impact": "high",
        "owner_hint": "customer success",
    },
}

AI_CAPABILITIES = [
    {
        "id": "youth_conversion_intelligence",
        "domain": "customer_segment_behavior",
        "description": "Detect younger-user hesitation around large purchases and recommend trust-building nudges, smaller-entry offers, and social-proof content.",
        "study_theme": "Younger demographics are less likely to make large purchases online.",
        "signals": ["small cart value", "repeat short visits", "social referral traffic", "promo sensitivity"],
        "outputs": ["segment readiness score", "offer sizing guidance", "trust messaging recommendations"],
    },
    {
        "id": "market_penetration_adoption",
        "domain": "regional_ecommerce_adoption",
        "description": "Estimate whether internet reach is translating into actual commerce adoption and identify gaps in payments, logistics, and discovery.",
        "study_theme": "Higher internet penetration does not always mean higher e-commerce adoption.",
        "signals": ["traffic without conversion", "payment failure patterns", "shipping delays", "regional marketplace usage"],
        "outputs": ["adoption gap score", "market readiness notes", "channel prioritization"],
    },
    {
        "id": "device_experience_optimizer",
        "domain": "cross_device_engagement",
        "description": "Spot desktop-heavy or mobile-only behavior and recommend UX, performance, and content formatting changes for the dominant device path.",
        "study_theme": "More developed regions have lower engagement in mobile-only usage.",
        "signals": ["device mix", "browser usage", "session length by device", "desktop conversion lift"],
        "outputs": ["device strategy summary", "responsive UX recommendations", "performance priorities"],
    },
    {
        "id": "cpc_economics_profiler",
        "domain": "advertising_efficiency",
        "description": "Compare acquisition economics across regions and flag markets where low income does not imply low cost per click.",
        "study_theme": "Lower average income can still produce higher CPC in advertising.",
        "signals": ["cpc by geography", "conversion rate by geography", "auction pressure", "ad platform mix"],
        "outputs": ["regional media plan", "budget pressure alerts", "acquisition efficiency score"],
    },
    {
        "id": "older_adult_value_model",
        "domain": "age_segment_value",
        "description": "Identify older-adult engagement patterns and surface products, content, and support flows that improve confidence and completion rate.",
        "study_theme": "Older adults can outperform younger groups in certain metrics.",
        "signals": ["repeat usage", "help content usage", "higher completion rate", "support satisfaction"],
        "outputs": ["senior-friendly journey advice", "confidence-building guidance", "feature prioritization"],
    },
    {
        "id": "low_penetration_engagement_engine",
        "domain": "emerging_market_engagement",
        "description": "Detect high-intent engagement in low-penetration regions and recommend lightweight, mobile-first, and offline-aware experiences.",
        "study_theme": "Lower internet penetration can still mean higher engagement per capita.",
        "signals": ["per-capita engagement", "mobile network signals", "content depth", "messaging frequency"],
        "outputs": ["per-capita engagement score", "distribution priorities", "lightweight UX recommendations"],
    },
    {
        "id": "payment_logistics_intelligence",
        "domain": "commerce_enablement",
        "description": "Recommend local payment and logistics integrations based on regional conversion barriers and fulfillment reliability.",
        "study_theme": "Payments and logistics shape adoption across emerging markets.",
        "signals": ["checkout drop-off", "payment method preference", "delivery latency", "region-specific fulfillment failures"],
        "outputs": ["integration shortlist", "checkout risk alerts", "fulfillment guidance"],
    },
    {
        "id": "platform_channel_mapper",
        "domain": "channel_selection",
        "description": "Map audience behavior to the most relevant discovery and commerce channels, including social, marketplace, and search-led journeys.",
        "study_theme": "Youth, regional commerce, and retention all depend on the right platform mix.",
        "signals": ["traffic source mix", "social engagement", "marketplace referrals", "search conversion"],
        "outputs": ["channel fit score", "platform recommendation", "campaign routing suggestions"],
    },
    {
        "id": "retention_forecast_engine",
        "domain": "customer_retention",
        "description": "Forecast churn, repeat purchase likelihood, and follow-up urgency from interaction, booking, and sentiment patterns.",
        "study_theme": "Retention and follow-up are recurring themes across the study.",
        "signals": ["repeat messages", "booking completion", "negative sentiment", "resolution latency"],
        "outputs": ["churn risk", "next-best-action", "follow-up queue priority"],
    },
]


def _build_ai_capabilities_catalog() -> list[dict[str, object]]:
    return [
        {
            **capability,
            "priority": "high" if capability["id"] in {"retention_forecast_engine", "payment_logistics_intelligence", "device_experience_optimizer"} else "medium",
            "enabled": True,
        }
        for capability in AI_CAPABILITIES
    ]


def _build_capabilities_payload() -> dict[str, object]:
    return {
        "name": "CService Booking Backend",
        "version": os.getenv("CSERVICE_APP_VERSION", "0.1.0"),
        "environment": os.getenv("CSERVICE_ENV", os.getenv("ENV", "development")),
        "domain": "chat-retention",
        "features": [
            "youth_conversion_intelligence",
            "market_penetration_adoption",
            "device_experience_optimizer",
            "cpc_economics_profiler",
            "older_adult_value_model",
            "low_penetration_engagement_engine",
            "payment_logistics_intelligence",
            "platform_channel_mapper",
            "retention_forecast_engine",
            "retention_snapshot_health_reporting",
            "retention_snapshot_operations_status_reporting",
            "retention_snapshot_operations_compliance_reporting",
            "retention_snapshot_operations_posture_reporting",
            "retention_snapshot_operations_automation_reporting",
            "retention_snapshot_operations_execution_state_reporting",
            "retention_snapshot_operations_launch_readiness_reporting",
            "retention_snapshot_operations_gonogo_reporting",
        ],
        "capabilities": _build_ai_capabilities_catalog(),
        "endpoints": {
            "retention_snapshot_health_recommendation": "/chat/admin/snapshot-health-recommendation",
            "retention_snapshot_operations_status": "/chat/admin/snapshot-operations-status",
            "retention_snapshot_operations_compliance": "/chat/admin/snapshot-operations-compliance",
            "retention_snapshot_operations_posture": "/chat/admin/snapshot-operations-posture",
            "retention_snapshot_operations_automation": "/chat/admin/snapshot-operations-automation",
            "retention_snapshot_operations_execution_state": "/chat/admin/snapshot-operations-execution-state",
            "retention_snapshot_operations_launch_readiness": "/chat/admin/snapshot-operations-launch-readiness",
            "retention_snapshot_operations_gonogo": "/chat/admin/snapshot-operations-gonogo",
        },
    }


def _normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def _score_area(messages: list[str], bookings: list[models.Booking], area: str) -> tuple[float, list[str]]:
    evidence = []
    score = 0.0
    for message in messages:
        normalized = _normalize_text(message)
        if area == "response_speed" and any(word in normalized for word in ["slow", "delay", "wait"]):
            score += 1.0
            evidence.append(message)
        elif area == "clarity" and any(word in normalized for word in ["confusing", "unclear", "hard"]):
            score += 1.0
            evidence.append(message)
        elif area == "reliability" and any(word in normalized for word in ["bug", "error", "issue"]):
            score += 1.0
            evidence.append(message)
        elif area == "booking_flow" and any(word in normalized for word in ["cancel", "book", "schedule"]):
            score += 0.8
            evidence.append(message)
        elif area == "support" and any(word in normalized for word in ["help", "support"]):
            score += 0.8
            evidence.append(message)
        elif area == "pricing" and any(word in normalized for word in ["price", "expensive"]):
            score += 0.7
            evidence.append(message)

    if area == "booking_flow":
        cancelled_or_pending = [b for b in bookings if str(getattr(b.status, "value", b.status)) in {"pending", "cancelled"}]
        score += min(len(cancelled_or_pending) * 0.6, 2.4)
        if cancelled_or_pending:
            evidence.append(f"{len(cancelled_or_pending)} bookings are pending/cancelled")
    if area == "retention" and messages:
        repeated = max(0, len(messages) - len(set(messages)))
        score += min(repeated * 0.5, 2.0)
        if repeated:
            evidence.append(f"{repeated} repeated messages suggest unresolved needs")

    return score, evidence


def _build_interaction_insights(messages: list[str], bookings: list[models.Booking], sentiment: Sentiment | None) -> list[InteractionInsight]:
    areas = PREDEFINED_POLICY_AREAS
    ranking = []
    for area in areas:
        score, evidence = _score_area(messages, bookings, area)
        ranking.append((area, score, evidence))

    ranking.sort(key=lambda item: item[1], reverse=True)
    insights = []
    for area, score, evidence in ranking[:4]:
        if score <= 0:
            continue
        config = POLICY_CONFIGS.get(area, {
            "recommendation": "Create a closed-loop retention workflow for unresolved interactions.",
            "next_step": "Trigger follow-up prompts when the same concern repeats across sessions.",
            "priority": "high",
        })
        recommendation = config["recommendation"]
        next_step = config["next_step"]
        priority = config["priority"]

        evidence_list = list(dict.fromkeys(evidence))[:4]
        insights.append(
            InteractionInsight(
                area=area,
                priority=priority,
                score=round(float(score), 2),
                evidence=evidence_list,
                recommendation=recommendation,
                next_step=next_step,
            )
        )

    if sentiment and sentiment.label == "negative":
        insights.insert(
            0,
            InteractionInsight(
                area="sentiment",
                priority="high",
                score=round(float(sentiment.score), 2),
                evidence=[f"Negative sentiment detected with confidence {sentiment.score:.2f}"],
                recommendation="Prioritize recovery flows and proactive follow-up when sentiment turns negative.",
                next_step="Surface apology paths, escalation options, and fast human support.",
            ),
        )

    return insights


async def _load_user_interaction_window(db: AsyncSession, user_id: int, window_days: int = 30):
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    chat_query = (
        select(models.ChatHistory)
        .where(models.ChatHistory.user_id == user_id)
        .where(models.ChatHistory.timestamp >= cutoff)
        .order_by(desc(models.ChatHistory.timestamp))
    )
    booking_query = (
        select(models.Booking)
        .where(models.Booking.user_id == user_id)
        .where(models.Booking.created_at >= cutoff)
        .order_by(desc(models.Booking.created_at))
    )
    chat_result = await db.execute(chat_query)
    booking_result = await db.execute(booking_query)
    return chat_result.scalars().all(), booking_result.scalars().all()


def _build_summary(user_id: int, chat_rows: list[models.ChatHistory], bookings: list[models.Booking], sentiment: Sentiment | None) -> InteractionSummary:
    messages = [row.message for row in chat_rows]
    message_counter = Counter(_normalize_text(message) for message in messages if message)
    repeated_issues = [message for message, count in message_counter.items() if count > 1]
    insights = _build_interaction_insights(messages, bookings, sentiment)

    top_issues = []
    for item in insights[:3]:
        top_issues.append(item.area.replace("_", " "))
    if repeated_issues:
        top_issues.append("repeated concerns")

    strengths = []
    if bookings:
        completed = [b for b in bookings if str(getattr(b.status, "value", b.status)) == "completed"]
        if completed:
            strengths.append("users complete bookings successfully")
    if not sentiment or sentiment.label != "negative":
        strengths.append("no strong negative sentiment in the latest message")
    if len(messages) > 3:
        strengths.append("users are returning for follow-up interactions")

    loyalty_score = 100.0
    loyalty_score -= min(sum(item.score for item in insights), 35.0)
    loyalty_score -= min(len(repeated_issues) * 4.0, 12.0)
    if sentiment and sentiment.label == "negative":
        loyalty_score -= 10.0
    loyalty_score = max(0.0, round(loyalty_score, 2))

    recency_bonus = 0.0
    if chat_rows:
        recency_bonus += 6.0
    if len(chat_rows) >= 3:
        recency_bonus += 4.0
    completed_bookings = [b for b in bookings if str(getattr(b.status, "value", b.status)) == "completed"]
    confirmed_bookings = [b for b in bookings if str(getattr(b.status, "value", b.status)) == "confirmed"]
    cancelled_bookings = [b for b in bookings if str(getattr(b.status, "value", b.status)) == "cancelled"]
    if completed_bookings:
        recency_bonus += min(len(completed_bookings) * 8.0, 20.0)
    if confirmed_bookings:
        recency_bonus += min(len(confirmed_bookings) * 4.0, 12.0)
    if cancelled_bookings:
        recency_bonus -= min(len(cancelled_bookings) * 5.0, 15.0)
    if sentiment and sentiment.label == "positive":
        recency_bonus += 4.0

    pricing_signals = sum(1 for item in insights if item.area == "pricing")
    support_signals = sum(1 for item in insights if item.area in {"support", "clarity", "response_speed"})
    reliability_signals = sum(1 for item in insights if item.area == "reliability")

    monetization_readiness = loyalty_score
    monetization_readiness += recency_bonus
    monetization_readiness += min(pricing_signals * 6.0, 12.0)
    monetization_readiness += min(len(completed_bookings) * 3.0, 9.0)
    monetization_readiness -= min(support_signals * 4.0, 12.0)
    monetization_readiness -= min(reliability_signals * 5.0, 15.0)
    monetization_readiness = max(0.0, min(100.0, round(monetization_readiness, 2)))

    if monetization_readiness >= 85:
        value_tier = "premium"
    elif monetization_readiness >= 70:
        value_tier = "growth"
    elif monetization_readiness >= 50:
        value_tier = "standard"
    else:
        value_tier = "care"

    if loyalty_score >= 80 and monetization_readiness >= 80:
        customer_classification = "loyal high-value"
    elif loyalty_score >= 70 and monetization_readiness >= 60:
        customer_classification = "loyal growth-ready"
    elif loyalty_score >= 55 and monetization_readiness >= 55:
        customer_classification = "stable value"
    elif monetization_readiness >= 65:
        customer_classification = "conversion-ready"
    else:
        customer_classification = "needs attention"

    if loyalty_score >= 80:
        churn_risk = "low"
    elif loyalty_score >= 55:
        churn_risk = "medium"
    else:
        churn_risk = "high"

    return InteractionSummary(
        user_id=user_id,
        messages_analyzed=len(chat_rows),
        bookings_analyzed=len(bookings),
        churn_risk=churn_risk,
        loyalty_score=loyalty_score,
        monetization_readiness=monetization_readiness,
        value_tier=value_tier,
        customer_classification=customer_classification,
        top_issues=top_issues,
        strengths=strengths or ["interaction history is still too small to infer strong patterns"],
        insights=insights,
        metadata={
            "window_days": 30,
            "repeated_messages": len(repeated_issues),
            "booking_states": Counter(str(getattr(b.status, "value", b.status)) for b in bookings),
        },
        generated_at=datetime.now(timezone.utc),
    )


def _build_system_improvement_pack(user_id: int, chat_rows: list[models.ChatHistory], bookings: list[models.Booking], sentiment: Sentiment | None) -> SystemImprovementPack:
    messages = [row.message for row in chat_rows]
    summary = _build_summary(user_id, chat_rows, bookings, sentiment)
    insights = summary.insights
    item_map = {
        **{area: {"impact": config["impact"], "owner_hint": config["owner_hint"]} for area, config in POLICY_CONFIGS.items()},
        "sentiment": {
            "impact": "high",
            "owner_hint": "support and success",
        },
    }

    items: list[SystemImprovementItem] = []
    for insight in insights[:5]:
        config = item_map.get(insight.area, {"impact": "medium", "owner_hint": "product"})
        items.append(
            SystemImprovementItem(
                area=insight.area,
                priority=insight.priority,
                impact=config["impact"],
                rationale=insight.evidence or ["Pattern detected in recent interactions"],
                recommendation=insight.recommendation,
                owner_hint=config["owner_hint"],
            )
        )

    if not items:
        items = [
            SystemImprovementItem(
                area="data_collection",
                priority="medium",
                impact="medium",
                rationale=["Not enough interaction data yet"],
                recommendation="Collect more chat and booking events before making major changes.",
                owner_hint="analytics and product",
            )
        ]

    focus = "loyalty and repeat-usage improvements"
    if summary.churn_risk == "high":
        focus = "urgent churn prevention"
    elif summary.churn_risk == "low":
        focus = "conversion-to-loyalty optimization"

    return SystemImprovementPack(
        user_id=user_id,
        generated_at=datetime.now(timezone.utc),
        focus=focus,
        items=items,
        summary=summary,
    )


def _build_weighted_system_monitoring(summary: InteractionSummary) -> WeightedSystemMonitoringReport:
    areas = []
    for area, weight, importance, focus in [
        ("reliability", 1.0, "high", "stabilize error-prone journeys first"),
        ("response_speed", 0.95, "high", "reduce waiting and improve perceived responsiveness"),
        ("customer_activity", 0.9, "high", "protect engagement and repeat usage"),
        ("retention", 0.85, "high", "close the loop on unresolved concerns"),
    ]:
        rationale = []
        if area == "reliability":
            rationale.append("Reliability is the strongest driver of churn prevention.")
            if any(issue in {"reliability", "support", "clarity"} for issue in summary.top_issues):
                rationale.append("Recent issues point to stability and support friction.")
        elif area == "response_speed":
            rationale.append("Slower responses reduce trust and satisfaction.")
            if "response_speed" in summary.top_issues:
                rationale.append("Response speed appears in the summary top issues.")
        elif area == "customer_activity":
            rationale.append("Returning activity is a proxy for healthy engagement.")
            if summary.messages_analyzed > 0 or summary.bookings_analyzed > 0:
                rationale.append("There is recent interaction history to monitor.")
        else:
            rationale.append("Retention improves when unresolved needs are followed up consistently.")
            if summary.churn_risk in {"medium", "high"}:
                rationale.append("Churn risk indicates follow-up is important.")

        areas.append(
            WeightedFocusItem(
                area=area,
                weight=weight,
                importance=importance,
                rationale=rationale,
                focus=focus,
            )
        )

    return WeightedSystemMonitoringReport(
        user_id=summary.user_id,
        generated_at=summary.generated_at,
        scope="whole_system_and_customer_activity",
        items=areas,
        summary=summary,
    )


def _normalize_area_label(area: str) -> str:
    return (area or "").strip().lower()


async def _load_signal_trends(db: AsyncSession, user_id: int, window_days: int) -> InteractionTrendReport:
    now = datetime.now(timezone.utc)
    current_cutoff = now - timedelta(days=window_days)
    previous_cutoff = now - timedelta(days=window_days * 2)

    current_query = (
        select(models.InteractionSignal.area, func.avg(models.InteractionSignal.score))
        .where(models.InteractionSignal.user_id == user_id)
        .where(models.InteractionSignal.created_at >= current_cutoff)
        .group_by(models.InteractionSignal.area)
    )
    previous_query = (
        select(models.InteractionSignal.area, func.avg(models.InteractionSignal.score))
        .where(models.InteractionSignal.user_id == user_id)
        .where(models.InteractionSignal.created_at >= previous_cutoff)
        .where(models.InteractionSignal.created_at < current_cutoff)
        .group_by(models.InteractionSignal.area)
    )

    current_result = await db.execute(current_query)
    previous_result = await db.execute(previous_query)

    current_scores = {_normalize_area_label(area): float(score or 0.0) for area, score in current_result.all()}
    previous_scores = {_normalize_area_label(area): float(score or 0.0) for area, score in previous_result.all()}

    all_areas = sorted(set(current_scores) | set(previous_scores))
    trends = []
    improving_areas = []
    worsening_areas = []

    for area in all_areas:
        current_score = round(current_scores.get(area, 0.0), 2)
        previous_score = round(previous_scores.get(area, 0.0), 2)
        delta = round(current_score - previous_score, 2)
        if delta < 0:
            direction = "improving"
            improving_areas.append(area)
        elif delta > 0:
            direction = "worsening"
            worsening_areas.append(area)
        else:
            direction = "stable"

        trends.append(
            InteractionTrendItem(
                area=area,
                current_score=current_score,
                previous_score=previous_score,
                delta=delta,
                direction=direction,
            )
        )

    return InteractionTrendReport(
        user_id=user_id,
        window_days=window_days,
        trends=trends,
        improving_areas=improving_areas,
        worsening_areas=worsening_areas,
        generated_at=now,
    )


async def _store_interaction_signals(db: AsyncSession, user_id: int, pack: SystemImprovementPack) -> None:
    for item in pack.items:
        signal = models.InteractionSignal(
            user_id=user_id,
            source="chat",
            area=item.area,
            priority=item.priority,
            score=float(next((ins.score for ins in pack.summary.insights if ins.area == item.area), 0.0)),
            evidence=json.dumps(item.rationale),
            recommendation=item.recommendation,
        )
        db.add(signal)
    await db.commit()


def _classify_loyalty_cohort(loyalty_score: float, signal_score: float) -> tuple[str, str, str]:
    if loyalty_score >= 80 and signal_score < 2:
        return "champions", "low", "double down on proactive retention and referral nudges"
    if loyalty_score >= 60:
        return "stable", "medium", "reduce friction and reinforce successful journeys"
    if loyalty_score >= 40:
        return "at risk", "high", "prioritize recovery, support follow-up, and friction removal"
    return "critical", "high", "immediate outreach and issue-resolution workflows"


async def _build_retention_cohorts(db: AsyncSession, window_days: int) -> RetentionCohortReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    user_query = select(models.User.id)
    user_result = await db.execute(user_query)
    user_ids = [row[0] for row in user_result.all()]

    cohorts: dict[str, dict[str, float | int | str]] = {}
    for user_id in user_ids:
        chat_query = (
            select(models.ChatHistory)
            .where(models.ChatHistory.user_id == user_id)
            .where(models.ChatHistory.timestamp >= cutoff)
            .order_by(desc(models.ChatHistory.timestamp))
        )
        booking_query = (
            select(models.Booking)
            .where(models.Booking.user_id == user_id)
            .where(models.Booking.created_at >= cutoff)
            .order_by(desc(models.Booking.created_at))
        )
        signal_query = (
            select(models.InteractionSignal)
            .where(models.InteractionSignal.user_id == user_id)
            .where(models.InteractionSignal.created_at >= cutoff)
        )

        chat_rows = (await db.execute(chat_query)).scalars().all()
        bookings = (await db.execute(booking_query)).scalars().all()
        signals = (await db.execute(signal_query)).scalars().all()

        latest_chat_message = getattr(chat_rows[0], "message", None) if chat_rows else None
        latest_sentiment = analyze_sentiment(latest_chat_message) if latest_chat_message else None
        summary = _build_summary(user_id, chat_rows, bookings, latest_sentiment)
        signal_values = [float(getattr(signal, "score", 0.0) or 0.0) for signal in signals]
        signal_score = round(sum(signal_values) / max(len(signal_values), 1), 2)
        cohort_name, primary_risk, recommended_action = _classify_loyalty_cohort(summary.loyalty_score, signal_score)

        if cohort_name not in cohorts:
            cohorts[cohort_name] = {
                "user_count": 0,
                "total_loyalty": 0.0,
                "total_signal": 0.0,
                "primary_risk": primary_risk,
                "recommended_action": recommended_action,
            }

        cohorts[cohort_name]["user_count"] += 1
        cohorts[cohort_name]["total_loyalty"] += summary.loyalty_score
        cohorts[cohort_name]["total_signal"] += signal_score

    cohort_items = []
    for cohort_name, values in sorted(cohorts.items(), key=lambda item: item[1]["user_count"], reverse=True):
        user_count = int(values["user_count"])
        cohort_items.append(
            RetentionCohortItem(
                cohort=cohort_name,
                user_count=user_count,
                avg_loyalty_score=round(float(values["total_loyalty"]) / max(user_count, 1), 2),
                avg_signal_score=round(float(values["total_signal"]) / max(user_count, 1), 2),
                primary_risk=str(values["primary_risk"]),
                recommended_action=str(values["recommended_action"]),
            )
        )

    return RetentionCohortReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        cohorts=cohort_items,
    )


async def _build_retention_cohort_drilldown(db: AsyncSession, window_days: int, cohort_name: str) -> RetentionCohortDrilldownReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    user_rows = (await db.execute(select(models.User.id, models.User.username))).all()
    members: list[RetentionCohortMember] = []
    normalized_target = cohort_name.strip().lower()

    for user_id, username in user_rows:
        chat_query = (
            select(models.ChatHistory)
            .where(models.ChatHistory.user_id == user_id)
            .where(models.ChatHistory.timestamp >= cutoff)
            .order_by(desc(models.ChatHistory.timestamp))
        )
        booking_query = (
            select(models.Booking)
            .where(models.Booking.user_id == user_id)
            .where(models.Booking.created_at >= cutoff)
            .order_by(desc(models.Booking.created_at))
        )
        signal_query = (
            select(models.InteractionSignal)
            .where(models.InteractionSignal.user_id == user_id)
            .where(models.InteractionSignal.created_at >= cutoff)
        )

        chat_rows = (await db.execute(chat_query)).scalars().all()
        bookings = (await db.execute(booking_query)).scalars().all()
        signals = (await db.execute(signal_query)).scalars().all()

        latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
        summary = _build_summary(user_id, chat_rows, bookings, latest_sentiment)
        signal_values = [float(getattr(signal, "score", 0.0) or 0.0) for signal in signals]
        signal_score = round(sum(signal_values) / max(len(signal_values), 1), 2)
        member_cohort, primary_risk, _ = _classify_loyalty_cohort(summary.loyalty_score, signal_score)

        if member_cohort.lower() == normalized_target:
            members.append(
                RetentionCohortMember(
                    user_id=int(user_id),
                    username=str(username),
                    cohort=member_cohort,
                    loyalty_score=summary.loyalty_score,
                    signal_score=signal_score,
                    primary_risk=primary_risk,
                )
            )

    members.sort(key=lambda item: (item.loyalty_score, item.signal_score), reverse=True)

    return RetentionCohortDrilldownReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        cohort=cohort_name,
        members=members,
    )


async def _build_churn_prediction(db: AsyncSession, user_id: int, window_days: int) -> ChurnPrediction:
    chat_rows, bookings = await _load_user_interaction_window(db, user_id, window_days)
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = _build_summary(user_id, chat_rows, bookings, latest_sentiment)
    trends = await _load_signal_trends(db, user_id, window_days)

    warning_reasons = []
    next_best_actions = []
    risk_score = 0.0

    if summary.churn_risk == "high":
        risk_score += 45.0
        warning_reasons.append("Current loyalty score indicates high churn risk")
        next_best_actions.append("Trigger immediate retention outreach")
    elif summary.churn_risk == "medium":
        risk_score += 25.0
        warning_reasons.append("Current loyalty score indicates medium churn risk")
        next_best_actions.append("Reduce friction in the highest-scoring pain points")

    if latest_sentiment and latest_sentiment.label == "negative":
        risk_score += min(latest_sentiment.score * 20.0, 20.0)
        warning_reasons.append("Latest interaction has negative sentiment")
        next_best_actions.append("Offer a fast recovery path and human follow-up")

    worsening_count = len(trends.worsening_areas)
    improving_count = len(trends.improving_areas)
    if worsening_count:
        risk_score += min(worsening_count * 8.0, 20.0)
        warning_reasons.append(f"{worsening_count} areas are worsening across recent signal windows")
    if improving_count:
        risk_score -= min(improving_count * 4.0, 12.0)

    repeated_concerns = int(summary.metadata.get("repeated_messages", 0))
    if repeated_concerns:
        risk_score += min(repeated_concerns * 3.0, 15.0)
        warning_reasons.append("The same concern appears multiple times in recent chats")

    pending_bookings = 0
    for status_count in summary.metadata.get("booking_states", {}).items():
        if status_count[0] in {"pending", "cancelled"}:
            pending_bookings += int(status_count[1])
    if pending_bookings:
        risk_score += min(pending_bookings * 5.0, 15.0)
        warning_reasons.append("There are unresolved or cancelled bookings in the recent window")
        next_best_actions.append("Clean up booking friction and clarify next steps")

    if not next_best_actions:
        next_best_actions = [
            "Keep monitoring signals and maintain the current retention path",
            "Continue reviewing support and booking friction patterns",
        ]

    risk_score = max(0.0, min(100.0, round(risk_score, 2)))
    if risk_score >= 70:
        risk_level = "critical"
    elif risk_score >= 45:
        risk_level = "high"
    elif risk_score >= 25:
        risk_level = "medium"
    else:
        risk_level = "low"

    if not warning_reasons:
        warning_reasons = ["No strong churn signals detected in the current analysis window"]

    return ChurnPrediction(
        user_id=user_id,
        risk_score=risk_score,
        risk_level=risk_level,
        warning_reasons=warning_reasons,
        next_best_actions=list(dict.fromkeys(next_best_actions))[:5],
        generated_at=datetime.now(timezone.utc),
    )


def _classify_lifecycle_stage(summary: InteractionSummary, churn_prediction: ChurnPrediction, chat_count: int, booking_count: int) -> tuple[str, float, list[str], list[str]]:
    drivers = []
    retention_focus = []
    confidence = 0.5

    if chat_count == 0 and booking_count == 0:
        return "new", 0.9, ["No interaction history yet"], ["Onboarding", "First-success journey"]

    if summary.churn_risk == "high" or churn_prediction.risk_level in {"high", "critical"}:
        drivers.append("High churn risk")
        retention_focus.append("Immediate recovery")
        confidence += 0.25
        stage = "at_risk"
    elif summary.churn_risk == "medium" or churn_prediction.risk_level == "medium":
        drivers.append("Moderate churn risk")
        retention_focus.append("Remove friction")
        confidence += 0.2
        stage = "recovering"
    elif summary.loyalty_score >= 80 and booking_count >= 2:
        drivers.append("Strong loyalty score")
        drivers.append("Repeated successful bookings")
        retention_focus.append("Referral and expansion offers")
        retention_focus.append("Proactive delight moments")
        confidence += 0.25
        stage = "loyal"
    elif chat_count >= 3 or booking_count >= 1:
        drivers.append("Active engagement in the current window")
        retention_focus.append("Activation and habit formation")
        confidence += 0.15
        stage = "engaged"
    else:
        drivers.append("Light interaction history")
        retention_focus.append("Onboarding and first value")
        stage = "new"

    if summary.insights:
        drivers.append(f"Top friction area: {summary.insights[0].area}")
        confidence += min(summary.insights[0].score / 20.0, 0.1)

    return stage, round(min(confidence, 0.99), 2), drivers[:4], retention_focus[:4]


async def _build_lifecycle_stage_report(db: AsyncSession, window_days: int) -> LifecycleStageReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    user_rows = await db.execute(select(models.User.id))
    user_ids = [row[0] for row in user_rows.all()]

    stages: list[LifecycleStageItem] = []
    for user_id in user_ids:
        chat_rows, bookings = await _load_user_interaction_window(db, user_id, window_days)
        signal_query = (
            select(models.InteractionSignal)
            .where(models.InteractionSignal.user_id == user_id)
            .where(models.InteractionSignal.created_at >= cutoff)
        )
        signal_rows = (await db.execute(signal_query)).scalars().all()
        latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
        summary = _build_summary(user_id, chat_rows, bookings, latest_sentiment)
        churn_prediction = await _build_churn_prediction(db, user_id, window_days)
        stage, confidence, drivers, retention_focus = _classify_lifecycle_stage(
            summary,
            churn_prediction,
            len(chat_rows),
            len(bookings),
        )

        if signal_rows:
            drivers.append(f"{len(signal_rows)} stored signals")

        stages.append(
            LifecycleStageItem(
                user_id=user_id,
                stage=stage,
                confidence=confidence,
                drivers=drivers,
                retention_focus=retention_focus,
                churn_prediction=churn_prediction,
            )
        )

    return LifecycleStageReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stages=stages,
    )


async def _save_retention_snapshot(
    db: AsyncSession,
    user_id: int,
    window_days: int,
    snapshot_type: str,
    summary: InteractionSummary,
    lifecycle_stage: str,
) -> models.RetentionSnapshot:
    snapshot = models.RetentionSnapshot(
        user_id=user_id,
        snapshot_type=snapshot_type,
        window_days=window_days,
        loyalty_score=summary.loyalty_score,
        churn_risk=summary.churn_risk,
        lifecycle_stage=lifecycle_stage,
        summary_json=json.dumps(
            {
                "messages_analyzed": summary.messages_analyzed,
                "bookings_analyzed": summary.bookings_analyzed,
                "top_issues": summary.top_issues,
                "strengths": summary.strengths,
                "metadata": summary.metadata,
            }
        ),
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


async def _prune_retention_snapshots(db: AsyncSession, user_id: int, window_days: int, keep: int = 20) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = (
        select(models.RetentionSnapshot)
        .where(models.RetentionSnapshot.user_id == user_id)
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .order_by(desc(models.RetentionSnapshot.created_at))
    )
    result = await db.execute(query)
    snapshots = result.scalars().all()
    for snapshot in snapshots[keep:]:
        await db.delete(snapshot)
    if len(snapshots) > keep:
        await db.commit()


async def _prune_retention_snapshots_with_report(
    db: AsyncSession,
    user_id: int,
    window_days: int,
    keep: int = 20,
) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = (
        select(models.RetentionSnapshot)
        .where(models.RetentionSnapshot.user_id == user_id)
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .order_by(desc(models.RetentionSnapshot.created_at))
    )
    result = await db.execute(query)
    snapshots = result.scalars().all()
    removed = 0

    for snapshot in snapshots[keep:]:
        await db.delete(snapshot)
        removed += 1

    if removed:
        await db.commit()

    return {
        "user_id": user_id,
        "window_days": window_days,
        "removed_snapshots": removed,
        "kept_snapshots": min(len(snapshots), keep),
        "generated_at": datetime.now(timezone.utc),
    }


async def _build_retention_maintenance_report(db: AsyncSession, window_days: int, keep: int = 20) -> RetentionMaintenanceReport:
    user_rows = await db.execute(select(models.User.id))
    user_ids = [row[0] for row in user_rows.all()]
    results = []

    for user_id in user_ids:
        result = await _prune_retention_snapshots_with_report(db, user_id=user_id, window_days=window_days, keep=keep)
        results.append(RetentionMaintenanceResult(**result))

    return RetentionMaintenanceReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        keep=keep,
        total_users=len(user_ids),
        results=results,
    )


async def _build_user_activity_report(db: AsyncSession, user_id: int, window_days: int) -> UserActivityReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    chat_query = (
        select(func.count(models.ChatHistory.id), func.max(models.ChatHistory.timestamp))
        .where(models.ChatHistory.user_id == user_id)
        .where(models.ChatHistory.timestamp >= cutoff)
    )
    booking_query = (
        select(func.count(models.Booking.id), func.max(models.Booking.created_at))
        .where(models.Booking.user_id == user_id)
        .where(models.Booking.created_at >= cutoff)
    )
    snapshot_query = (
        select(func.count(models.RetentionSnapshot.id), func.max(models.RetentionSnapshot.created_at))
        .where(models.RetentionSnapshot.user_id == user_id)
        .where(models.RetentionSnapshot.created_at >= cutoff)
    )

    chat_result = await db.execute(chat_query)
    booking_result = await db.execute(booking_query)
    snapshot_result = await db.execute(snapshot_query)

    chat_count, latest_chat_at = chat_result.one()
    booking_count, latest_booking_at = booking_result.one()
    snapshot_count, latest_snapshot_at = snapshot_result.one()

    return UserActivityReport(
        user_id=user_id,
        window_days=window_days,
        chat_messages=int(chat_count or 0),
        bookings=int(booking_count or 0),
        snapshots=int(snapshot_count or 0),
        latest_chat_at=latest_chat_at,
        latest_booking_at=latest_booking_at,
        latest_snapshot_at=latest_snapshot_at,
        generated_at=datetime.now(timezone.utc),
    )


async def _build_admin_activity_report(db: AsyncSession, window_days: int) -> AdminActivityReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    total_users_result = await db.execute(select(func.count(models.User.id)))
    total_chat_messages_result = await db.execute(select(func.count(models.ChatHistory.id)))
    total_bookings_result = await db.execute(select(func.count(models.Booking.id)))
    total_snapshots_result = await db.execute(select(func.count(models.RetentionSnapshot.id)))

    recent_chats_result = await db.execute(
        select(func.count(models.ChatHistory.id)).where(models.ChatHistory.timestamp >= cutoff)
    )
    recent_bookings_result = await db.execute(
        select(func.count(models.Booking.id)).where(models.Booking.created_at >= cutoff)
    )
    recent_snapshots_result = await db.execute(
        select(func.count(models.RetentionSnapshot.id)).where(models.RetentionSnapshot.created_at >= cutoff)
    )

    return AdminActivityReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        total_users=int(total_users_result.scalar() or 0),
        total_chat_messages=int(total_chat_messages_result.scalar() or 0),
        total_bookings=int(total_bookings_result.scalar() or 0),
        total_snapshots=int(total_snapshots_result.scalar() or 0),
        recent_chats=int(recent_chats_result.scalar() or 0),
        recent_bookings=int(recent_bookings_result.scalar() or 0),
        recent_snapshots=int(recent_snapshots_result.scalar() or 0),
    )


async def _build_activity_timeline(db: AsyncSession, user_id: int, window_days: int) -> ActivityTimelineReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    chat_query = (
        select(models.ChatHistory)
        .where(models.ChatHistory.user_id == user_id)
        .where(models.ChatHistory.timestamp >= cutoff)
        .order_by(desc(models.ChatHistory.timestamp))
    )
    booking_query = (
        select(models.Booking)
        .where(models.Booking.user_id == user_id)
        .where(models.Booking.created_at >= cutoff)
        .order_by(desc(models.Booking.created_at))
    )
    snapshot_query = (
        select(models.RetentionSnapshot)
        .where(models.RetentionSnapshot.user_id == user_id)
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .order_by(desc(models.RetentionSnapshot.created_at))
    )

    chat_rows = (await db.execute(chat_query)).scalars().all()
    booking_rows = (await db.execute(booking_query)).scalars().all()
    snapshot_rows = (await db.execute(snapshot_query)).scalars().all()

    items = []
    for row in chat_rows:
        items.append(
            ActivityTimelineItem(
                kind="chat",
                created_at=row.timestamp,
                summary=row.message[:120],
                reference_id=row.id,
            )
        )
    for row in booking_rows:
        items.append(
            ActivityTimelineItem(
                kind="booking",
                created_at=row.created_at,
                summary=f"{getattr(row.status, 'value', row.status)} booking: {row.title or row.service_type}",
                reference_id=row.id,
            )
        )
    for row in snapshot_rows:
        items.append(
            ActivityTimelineItem(
                kind="snapshot",
                created_at=row.created_at,
                summary=f"{row.snapshot_type} snapshot ({row.lifecycle_stage})",
                reference_id=row.id,
            )
        )

    items.sort(key=lambda item: item.created_at, reverse=True)

    return ActivityTimelineReport(
        user_id=user_id,
        window_days=window_days,
        generated_at=datetime.now(timezone.utc),
        items=items,
    )


async def _build_ranked_users_report(db: AsyncSession, window_days: int, limit: int) -> RankedUserReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    query = (
        select(
            models.User.id,
            models.User.username,
            func.count(models.ChatHistory.id),
            func.count(models.Booking.id),
            func.count(models.RetentionSnapshot.id),
        )
        .outerjoin(models.ChatHistory, models.ChatHistory.user_id == models.User.id)
        .outerjoin(models.Booking, models.Booking.user_id == models.User.id)
        .outerjoin(models.RetentionSnapshot, models.RetentionSnapshot.user_id == models.User.id)
        .where(
            (models.ChatHistory.timestamp >= cutoff)
            | (models.ChatHistory.id.is_(None))
            | (models.Booking.created_at >= cutoff)
            | (models.Booking.id.is_(None))
            | (models.RetentionSnapshot.created_at >= cutoff)
            | (models.RetentionSnapshot.id.is_(None))
        )
        .group_by(models.User.id, models.User.username)
    )

    rows = (await db.execute(query)).all()
    ranked = []
    for user_id, username, chat_count, booking_count, snapshot_count in rows:
        chat_total = int(chat_count or 0)
        booking_total = int(booking_count or 0)
        snapshot_total = int(snapshot_count or 0)
        ranked.append(
            RankedUserItem(
                user_id=int(user_id),
                username=str(username),
                chat_messages=chat_total,
                bookings=booking_total,
                snapshots=snapshot_total,
                activity_score=chat_total + booking_total * 2 + snapshot_total,
            )
        )

    ranked.sort(key=lambda item: (item.activity_score, item.chat_messages, item.bookings), reverse=True)

    return RankedUserReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        limit=limit,
        users=ranked[:limit],
    )


async def _build_admin_retention_trend_report(db: AsyncSession, window_days: int) -> AdminRetentionTrendReport:
    now = datetime.now(timezone.utc)
    current_cutoff = now - timedelta(days=window_days)
    previous_cutoff = current_cutoff - timedelta(days=window_days)

    current_snapshot_count = int(
        (await db.execute(
            select(func.count(models.RetentionSnapshot.id)).where(models.RetentionSnapshot.created_at >= current_cutoff)
        )).scalar() or 0
    )
    previous_snapshot_count = int(
        (await db.execute(
            select(func.count(models.RetentionSnapshot.id))
            .where(models.RetentionSnapshot.created_at >= previous_cutoff)
            .where(models.RetentionSnapshot.created_at < current_cutoff)
        )).scalar() or 0
    )

    current_chat_count = int(
        (await db.execute(
            select(func.count(models.ChatHistory.id)).where(models.ChatHistory.timestamp >= current_cutoff)
        )).scalar() or 0
    )
    previous_chat_count = int(
        (await db.execute(
            select(func.count(models.ChatHistory.id))
            .where(models.ChatHistory.timestamp >= previous_cutoff)
            .where(models.ChatHistory.timestamp < current_cutoff)
        )).scalar() or 0
    )

    current_booking_count = int(
        (await db.execute(
            select(func.count(models.Booking.id)).where(models.Booking.created_at >= current_cutoff)
        )).scalar() or 0
    )
    previous_booking_count = int(
        (await db.execute(
            select(func.count(models.Booking.id))
            .where(models.Booking.created_at >= previous_cutoff)
            .where(models.Booking.created_at < current_cutoff)
        )).scalar() or 0
    )

    return AdminRetentionTrendReport(
        generated_at=now,
        window_days=window_days,
        trends=[
            AdminRetentionTrendItem(
                area="snapshots",
                current_window=current_snapshot_count,
                previous_window=previous_snapshot_count,
                delta=current_snapshot_count - previous_snapshot_count,
            ),
            AdminRetentionTrendItem(
                area="chats",
                current_window=current_chat_count,
                previous_window=previous_chat_count,
                delta=current_chat_count - previous_chat_count,
            ),
            AdminRetentionTrendItem(
                area="bookings",
                current_window=current_booking_count,
                previous_window=previous_booking_count,
                delta=current_booking_count - previous_booking_count,
            ),
        ],
    )


async def _build_retention_snapshot_admin_report(db: AsyncSession, window_days: int) -> RetentionSnapshotAdminReport:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    total_snapshots = int(
        (await db.execute(
            select(func.count(models.RetentionSnapshot.id)).where(models.RetentionSnapshot.created_at >= cutoff)
        )).scalar() or 0
    )

    grouped_query = (
        select(
            models.RetentionSnapshot.snapshot_type,
            func.count(models.RetentionSnapshot.id),
            func.avg(models.RetentionSnapshot.loyalty_score),
            func.max(models.RetentionSnapshot.created_at),
        )
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .group_by(models.RetentionSnapshot.snapshot_type)
        .order_by(desc(func.count(models.RetentionSnapshot.id)))
    )
    grouped_result = await db.execute(grouped_query)

    items = [
        RetentionSnapshotAdminItem(
            snapshot_type=str(snapshot_type),
            count=int(count_value or 0),
            avg_loyalty_score=round(float(avg_loyalty or 0.0), 2),
            latest_created_at=latest_created_at,
        )
        for snapshot_type, count_value, avg_loyalty, latest_created_at in grouped_result.all()
    ]

    return RetentionSnapshotAdminReport(
        generated_at=now,
        window_days=window_days,
        total_snapshots=total_snapshots,
        items=items,
    )


async def _build_user_retention_snapshot_health(db: AsyncSession, user_id: int, window_days: int) -> UserRetentionSnapshotHealth:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = (
        select(models.RetentionSnapshot)
        .where(models.RetentionSnapshot.user_id == user_id)
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .order_by(desc(models.RetentionSnapshot.created_at))
    )
    result = await db.execute(query)
    snapshots = result.scalars().all()

    snapshot_count = len(snapshots)
    avg_loyalty_score = round(sum(snapshot.loyalty_score for snapshot in snapshots) / max(snapshot_count, 1), 2)

    latest = snapshots[0] if snapshots else None

    return UserRetentionSnapshotHealth(
        user_id=user_id,
        window_days=window_days,
        snapshot_count=snapshot_count,
        latest_snapshot_type=latest.snapshot_type if latest else None,
        latest_lifecycle_stage=latest.lifecycle_stage if latest else None,
        avg_loyalty_score=avg_loyalty_score,
        generated_at=datetime.now(timezone.utc),
    )


async def _build_retention_snapshot_comparison_report(db: AsyncSession, window_days: int) -> RetentionSnapshotComparisonReport:
    now = datetime.now(timezone.utc)
    current_cutoff = now - timedelta(days=window_days)
    previous_cutoff = current_cutoff - timedelta(days=window_days)

    current_query = (
        select(
            models.RetentionSnapshot.snapshot_type,
            func.count(models.RetentionSnapshot.id),
        )
        .where(models.RetentionSnapshot.created_at >= current_cutoff)
        .group_by(models.RetentionSnapshot.snapshot_type)
    )
    previous_query = (
        select(
            models.RetentionSnapshot.snapshot_type,
            func.count(models.RetentionSnapshot.id),
        )
        .where(models.RetentionSnapshot.created_at >= previous_cutoff)
        .where(models.RetentionSnapshot.created_at < current_cutoff)
        .group_by(models.RetentionSnapshot.snapshot_type)
    )

    current_rows = (await db.execute(current_query)).all()
    previous_rows = (await db.execute(previous_query)).all()

    current_counts = {str(snapshot_type): int(count_value or 0) for snapshot_type, count_value in current_rows}
    previous_counts = {str(snapshot_type): int(count_value or 0) for snapshot_type, count_value in previous_rows}

    all_types = sorted(set(current_counts) | set(previous_counts))
    comparisons = [
        RetentionSnapshotComparisonItem(
            snapshot_type=snapshot_type,
            current_window=current_counts.get(snapshot_type, 0),
            previous_window=previous_counts.get(snapshot_type, 0),
            delta=current_counts.get(snapshot_type, 0) - previous_counts.get(snapshot_type, 0),
        )
        for snapshot_type in all_types
    ]

    return RetentionSnapshotComparisonReport(
        generated_at=now,
        window_days=window_days,
        comparisons=comparisons,
    )


async def _build_retention_snapshot_momentum_report(db: AsyncSession, window_days: int) -> RetentionSnapshotMomentumReport:
    report = await _build_retention_snapshot_comparison_report(db, window_days)
    items = []
    for comparison in report.comparisons:
        if comparison.delta > 0:
            direction = "up"
        elif comparison.delta < 0:
            direction = "down"
        else:
            direction = "flat"
        items.append(
            RetentionSnapshotMomentumItem(
                snapshot_type=comparison.snapshot_type,
                direction=direction,
                momentum=abs(comparison.delta),
            )
        )

    return RetentionSnapshotMomentumReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        items=items,
    )


async def _build_retention_snapshot_volatility_report(db: AsyncSession, window_days: int) -> RetentionSnapshotVolatilityReport:
    report = await _build_retention_snapshot_comparison_report(db, window_days)
    items = []
    for comparison in report.comparisons:
        volatility = abs(comparison.delta)
        if volatility >= 3:
            classification = "high"
        elif volatility >= 1:
            classification = "moderate"
        else:
            classification = "stable"
        items.append(
            RetentionSnapshotVolatilityItem(
                snapshot_type=comparison.snapshot_type,
                volatility=volatility,
                classification=classification,
            )
        )

    return RetentionSnapshotVolatilityReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        items=items,
    )


async def _build_retention_snapshot_volatility_summary(db: AsyncSession, window_days: int) -> RetentionSnapshotVolatilitySummary:
    report = await _build_retention_snapshot_volatility_report(db, window_days)
    counts = {"stable": 0, "moderate": 0, "high": 0}
    for item in report.items:
        counts[item.classification] = counts.get(item.classification, 0) + 1

    return RetentionSnapshotVolatilitySummary(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stable=counts["stable"],
        moderate=counts["moderate"],
        high=counts["high"],
    )


async def _build_retention_snapshot_risk_profile(db: AsyncSession, window_days: int) -> RetentionSnapshotRiskProfile:
    summary = await _build_retention_snapshot_volatility_summary(db, window_days)
    score = summary.moderate * 2 + summary.high * 4
    if score >= 8:
        risk_level = "critical"
    elif score >= 4:
        risk_level = "high"
    elif score >= 1:
        risk_level = "moderate"
    else:
        risk_level = "low"

    return RetentionSnapshotRiskProfile(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        risk_level=risk_level,
        score=score,
    )


async def _build_retention_snapshot_recommendation(db: AsyncSession, window_days: int) -> RetentionSnapshotRecommendation:
    profile = await _build_retention_snapshot_risk_profile(db, window_days)
    if profile.risk_level == "critical":
        recommendation = "Escalate retention outreach and review high-volatility snapshot types immediately."
    elif profile.risk_level == "high":
        recommendation = "Prioritize stabilization work and investigate the most volatile snapshot types."
    elif profile.risk_level == "moderate":
        recommendation = "Monitor snapshot trends closely and tighten the follow-up loop."
    else:
        recommendation = "Maintain the current retention cadence and keep monitoring the baseline."

    return RetentionSnapshotRecommendation(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        risk_level=profile.risk_level,
        recommendation=recommendation,
    )


async def _build_retention_snapshot_action_plan(db: AsyncSession, window_days: int) -> RetentionSnapshotActionPlan:
    recommendation = await _build_retention_snapshot_recommendation(db, window_days)
    if recommendation.risk_level == "critical":
        action = "Open incident review, assign owners, and start outreach within 24 hours."
    elif recommendation.risk_level == "high":
        action = "Schedule a stabilization review and prepare a follow-up plan this week."
    elif recommendation.risk_level == "moderate":
        action = "Review the top snapshot types and confirm monitoring thresholds."
    else:
        action = "Keep monitoring the current retention baseline and revisit next cycle."

    return RetentionSnapshotActionPlan(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        risk_level=recommendation.risk_level,
        action=action,
    )


async def _build_retention_snapshot_audit_report(db: AsyncSession, window_days: int) -> RetentionSnapshotAuditReport:
    window_start = datetime.now(timezone.utc) - timedelta(days=window_days)
    audit_query = (
        select(
            models.RetentionSnapshot.snapshot_type,
            func.count(models.RetentionSnapshot.id),
            func.count(func.distinct(models.RetentionSnapshot.user_id)),
            func.max(models.RetentionSnapshot.created_at),
        )
        .where(models.RetentionSnapshot.created_at >= window_start)
        .group_by(models.RetentionSnapshot.snapshot_type)
        .order_by(models.RetentionSnapshot.snapshot_type)
    )
    rows = (await db.execute(audit_query)).all()
    items = [
        RetentionSnapshotAuditItem(
            snapshot_type=str(snapshot_type),
            total_snapshots=int(total_snapshots or 0),
            unique_users=int(unique_users or 0),
            latest_created_at=latest_created_at,
        )
        for snapshot_type, total_snapshots, unique_users, latest_created_at in rows
    ]
    return RetentionSnapshotAuditReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        total_snapshots=sum(item.total_snapshots for item in items),
        items=items,
    )


async def _build_retention_snapshot_audit_export(db: AsyncSession, window_days: int) -> RetentionSnapshotAuditExport:
    report = await _build_retention_snapshot_audit_report(db, window_days)
    snapshot_types = [item.snapshot_type for item in report.items]
    summary = f"{report.total_snapshots} snapshots across {len(snapshot_types)} types"
    return RetentionSnapshotAuditExport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        summary=summary,
        snapshot_types=snapshot_types,
        total_snapshots=report.total_snapshots,
    )


async def _build_retention_snapshot_type_breakdown(db: AsyncSession, window_days: int, snapshot_type: str) -> RetentionSnapshotTypeBreakdownReport:
    window_start = datetime.now(timezone.utc) - timedelta(days=window_days)
    base_query = (
        select(
            models.RetentionSnapshot.snapshot_type,
            func.count(models.RetentionSnapshot.id),
            func.count(func.distinct(models.RetentionSnapshot.user_id)),
        )
        .where(models.RetentionSnapshot.created_at >= window_start)
        .where(models.RetentionSnapshot.snapshot_type == snapshot_type)
        .group_by(models.RetentionSnapshot.snapshot_type)
    )
    rows = (await db.execute(base_query)).all()
    items = [
        RetentionSnapshotTypeBreakdownItem(
            snapshot_type=str(row_snapshot_type),
            total_snapshots=int(total_snapshots or 0),
            unique_users=int(unique_users or 0),
        )
        for row_snapshot_type, total_snapshots, unique_users in rows
    ]
    total_snapshots = sum(item.total_snapshots for item in items)
    unique_users = max((item.unique_users for item in items), default=0)
    return RetentionSnapshotTypeBreakdownReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        snapshot_type=snapshot_type,
        total_snapshots=total_snapshots,
        unique_users=unique_users,
        items=items,
    )


async def _build_retention_snapshot_staleness_report(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotStalenessReport:
    window_start = datetime.now(timezone.utc) - timedelta(days=window_days)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_after_days)
    stale_query = (
        select(
            models.RetentionSnapshot.snapshot_type,
            func.count(models.RetentionSnapshot.id),
            func.max(models.RetentionSnapshot.created_at),
        )
        .where(models.RetentionSnapshot.created_at >= window_start)
        .where(models.RetentionSnapshot.created_at <= stale_cutoff)
        .group_by(models.RetentionSnapshot.snapshot_type)
        .order_by(models.RetentionSnapshot.snapshot_type)
    )
    rows = (await db.execute(stale_query)).all()
    items = [
        RetentionSnapshotStalenessItem(
            snapshot_type=str(snapshot_type),
            stale_snapshots=int(stale_snapshots or 0),
            latest_created_at=latest_created_at,
        )
        for snapshot_type, stale_snapshots, latest_created_at in rows
    ]
    return RetentionSnapshotStalenessReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        total_stale_snapshots=sum(item.stale_snapshots for item in items),
        items=items,
    )


async def _build_retention_snapshot_staleness_trend(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotStalenessTrendReport:
    report = await _build_retention_snapshot_staleness_report(db, window_days, stale_after_days)
    items = [
        RetentionSnapshotStalenessTrendItem(
            bucket="stale",
            stale_snapshots=report.total_stale_snapshots,
        ),
        RetentionSnapshotStalenessTrendItem(
            bucket="fresh",
            stale_snapshots=0,
        ),
    ]
    return RetentionSnapshotStalenessTrendReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        items=items,
    )


async def _build_retention_snapshot_health_score(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotHealthScore:
    report = await _build_retention_snapshot_staleness_report(db, window_days, stale_after_days)
    score = max(0, 100 - (report.total_stale_snapshots * 10))
    if score >= 80:
        status = "healthy"
    elif score >= 50:
        status = "watch"
    else:
        status = "critical"
    return RetentionSnapshotHealthScore(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        score=score,
        status=status,
    )


async def _build_retention_snapshot_health_summary(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotHealthSummary:
    score_report = await _build_retention_snapshot_health_score(db, window_days, stale_after_days)
    summary = f"Snapshot freshness is {score_report.status} with a score of {score_report.score}."
    return RetentionSnapshotHealthSummary(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        score=score_report.score,
        status=score_report.status,
        summary=summary,
    )


async def _build_retention_snapshot_health_risk(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotHealthRisk:
    score_report = await _build_retention_snapshot_health_score(db, window_days, stale_after_days)
    if score_report.score >= 80:
        risk_level = "low"
    elif score_report.score >= 50:
        risk_level = "medium"
    else:
        risk_level = "high"
    return RetentionSnapshotHealthRisk(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        score=score_report.score,
        status=score_report.status,
        risk_level=risk_level,
    )


async def _build_retention_snapshot_health_recommendation(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotHealthRecommendation:
    risk_report = await _build_retention_snapshot_health_risk(db, window_days, stale_after_days)
    if risk_report.risk_level == "low":
        recommendation = "Keep monitoring the current snapshot cadence."
    elif risk_report.risk_level == "medium":
        recommendation = "Review stale snapshot drivers and check for drift this week."
    else:
        recommendation = "Escalate freshness review and assign immediate snapshot cleanup."
    return RetentionSnapshotHealthRecommendation(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        score=risk_report.score,
        status=risk_report.status,
        risk_level=risk_report.risk_level,
        recommendation=recommendation,
    )


async def _build_retention_snapshot_operations_overview(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotOperationsOverview:
    recommendation_report = await _build_retention_snapshot_health_recommendation(db, window_days, stale_after_days)
    if recommendation_report.risk_level == "low":
        overview = "Snapshot operations are healthy and need only routine monitoring."
    elif recommendation_report.risk_level == "medium":
        overview = "Snapshot operations need a targeted review to prevent drift."
    else:
        overview = "Snapshot operations require immediate attention and cleanup."
    return RetentionSnapshotOperationsOverview(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        score=recommendation_report.score,
        status=recommendation_report.status,
        risk_level=recommendation_report.risk_level,
        recommendation=recommendation_report.recommendation,
        overview=overview,
    )


async def _build_retention_snapshot_operations_status(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotOperationsStatus:
    overview_report = await _build_retention_snapshot_operations_overview(db, window_days, stale_after_days)
    if overview_report.risk_level == "low":
        status = "operational"
    elif overview_report.risk_level == "medium":
        status = "watch"
    else:
        status = "needs_attention"
    return RetentionSnapshotOperationsStatus(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        status=status,
        overview=overview_report.overview,
    )


async def _build_retention_snapshot_operations_compliance(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotOperationsCompliance:
    status_report = await _build_retention_snapshot_operations_status(db, window_days, stale_after_days)
    if status_report.status == "operational":
        compliance = "compliant"
    elif status_report.status == "watch":
        compliance = "review"
    else:
        compliance = "non_compliant"
    return RetentionSnapshotOperationsCompliance(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        compliance=compliance,
        overview=status_report.overview,
    )


async def _build_retention_snapshot_operations_posture(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotOperationsPosture:
    compliance_report = await _build_retention_snapshot_operations_compliance(db, window_days, stale_after_days)
    if compliance_report.compliance == "compliant":
        posture = "stable"
    elif compliance_report.compliance == "review":
        posture = "caution"
    else:
        posture = "escalate"
    return RetentionSnapshotOperationsPosture(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        posture=posture,
        overview=compliance_report.overview,
    )


async def _build_retention_snapshot_operations_automation(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotOperationsAutomation:
    posture_report = await _build_retention_snapshot_operations_posture(db, window_days, stale_after_days)
    if posture_report.posture == "stable":
        automation_ready = "ready"
    elif posture_report.posture == "caution":
        automation_ready = "conditional"
    else:
        automation_ready = "not_ready"
    return RetentionSnapshotOperationsAutomation(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        automation_ready=automation_ready,
        overview=posture_report.overview,
    )


async def _build_retention_snapshot_operations_execution_state(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotOperationsExecutionState:
    automation_report = await _build_retention_snapshot_operations_automation(db, window_days, stale_after_days)
    if automation_report.automation_ready == "ready":
        execution_state = "go"
    elif automation_report.automation_ready == "conditional":
        execution_state = "hold"
    else:
        execution_state = "block"
    return RetentionSnapshotOperationsExecutionState(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        execution_state=execution_state,
        overview=automation_report.overview,
    )


async def _build_retention_snapshot_operations_launch_readiness(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotOperationsLaunchReadiness:
    execution_report = await _build_retention_snapshot_operations_execution_state(db, window_days, stale_after_days)
    if execution_report.execution_state == "go":
        launch_readiness = "launch_ready"
    elif execution_report.execution_state == "hold":
        launch_readiness = "launch_pending"
    else:
        launch_readiness = "launch_blocked"
    return RetentionSnapshotOperationsLaunchReadiness(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        launch_readiness=launch_readiness,
        overview=execution_report.overview,
    )


async def _build_retention_snapshot_operations_go_no_go(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotOperationsGoNoGo:
    launch_report = await _build_retention_snapshot_operations_launch_readiness(db, window_days, stale_after_days)
    if launch_report.launch_readiness == "launch_ready":
        decision = "go"
    elif launch_report.launch_readiness == "launch_pending":
        decision = "hold"
    else:
        decision = "block"
    return RetentionSnapshotOperationsGoNoGo(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        decision=decision,
        overview=launch_report.overview,
    )


async def _build_retention_snapshot_report(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = (
        select(models.RetentionSnapshot)
        .where(models.RetentionSnapshot.user_id == user_id)
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .order_by(desc(models.RetentionSnapshot.created_at))
    )
    result = await db.execute(query)
    snapshots = result.scalars().all()
    return RetentionSnapshotReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        snapshots=[
            RetentionSnapshotItem(
                id=snapshot.id,
                user_id=snapshot.user_id,
                snapshot_type=snapshot.snapshot_type,
                window_days=snapshot.window_days,
                loyalty_score=snapshot.loyalty_score,
                churn_risk=snapshot.churn_risk,
                lifecycle_stage=snapshot.lifecycle_stage,
                summary_json=snapshot.summary_json,
                created_at=snapshot.created_at,
            )
            for snapshot in snapshots
        ],
    )


async def _build_retention_snapshot_delta(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotDelta:
    report = await _build_retention_snapshot_report(db, user_id, window_days)
    current = report.snapshots[0] if report.snapshots else None
    previous = report.snapshots[1] if len(report.snapshots) > 1 else None

    if not current:
        return RetentionSnapshotDelta(
            user_id=user_id,
            window_days=window_days,
            previous_snapshot_id=None,
            current_snapshot_id=None,
            loyalty_score_delta=0.0,
            churn_risk_changed=False,
            lifecycle_stage_changed=False,
            previous_created_at=None,
            current_created_at=None,
            generated_at=datetime.now(timezone.utc),
        )

    return RetentionSnapshotDelta(
        user_id=user_id,
        window_days=window_days,
        previous_snapshot_id=previous.id if previous else None,
        current_snapshot_id=current.id,
        loyalty_score_delta=round(current.loyalty_score - (previous.loyalty_score if previous else current.loyalty_score), 2),
        churn_risk_changed=bool(previous and previous.churn_risk != current.churn_risk),
        lifecycle_stage_changed=bool(previous and previous.lifecycle_stage != current.lifecycle_stage),
        previous_created_at=previous.created_at if previous else None,
        current_created_at=current.created_at,
        generated_at=datetime.now(timezone.utc),
    )


async def _build_retention_snapshot_trends(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotTrendReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = (
        select(models.RetentionSnapshot)
        .where(models.RetentionSnapshot.user_id == user_id)
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .order_by(desc(models.RetentionSnapshot.created_at))
    )
    result = await db.execute(query)
    snapshots = result.scalars().all()

    grouped: dict[str, list[models.RetentionSnapshot]] = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.snapshot_type, []).append(snapshot)

    trend_items = []
    for snapshot_type, items in sorted(grouped.items()):
        avg_loyalty = sum(item.loyalty_score for item in items) / max(len(items), 1)
        avg_churn = sum({"low": 0.0, "medium": 1.0, "high": 2.0, "critical": 3.0}.get(item.churn_risk, 0.0) for item in items) / max(len(items), 1)
        trend_items.append(
            RetentionSnapshotTrendItem(
                snapshot_type=snapshot_type,
                count=len(items),
                avg_loyalty_score=round(avg_loyalty, 2),
                avg_churn_risk_score=round(avg_churn, 2),
                latest_created_at=items[0].created_at if items else None,
            )
        )

    return RetentionSnapshotTrendReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        trends=trend_items,
    )


async def _build_retention_dashboard(db: AsyncSession, user_id: int, window_days: int) -> RetentionDashboard:
    chat_rows, bookings = await _load_user_interaction_window(db, user_id, window_days)
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = _build_summary(user_id, chat_rows, bookings, latest_sentiment)
    churn_prediction = await _build_churn_prediction(db, user_id, window_days)
    snapshot_report = await _build_retention_snapshot_report(db, user_id, window_days)
    snapshot_delta = await _build_retention_snapshot_delta(db, user_id, window_days)
    snapshot_trends = await _build_retention_snapshot_trends(db, user_id, window_days)
    return RetentionDashboard(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        summary=summary,
        churn_prediction=churn_prediction,
        snapshot_report=snapshot_report,
        snapshot_delta=snapshot_delta,
        snapshot_trends=snapshot_trends,
    )


@router.post("/retention/maintenance", response_model=RetentionMaintenanceResult)
async def retention_maintenance(
    window_days: int = Query(30, ge=1, le=365),
    keep: int = Query(20, ge=0, le=500),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    result = await _prune_retention_snapshots_with_report(db, current_user.id, window_days, keep=keep)
    return RetentionMaintenanceResult(**result)


@router.post("/retention/maintenance/report", response_model=RetentionMaintenanceReport)
async def retention_maintenance_report(
    window_days: int = Query(30, ge=1, le=365),
    keep: int = Query(20, ge=0, le=500),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    return await _build_retention_maintenance_report(db, window_days=window_days, keep=keep)


@router.get("/meta/capabilities")
async def chat_capabilities():
    return _build_capabilities_payload()


@router.get("/chat/activity", response_model=UserActivityReport)
async def get_user_activity_report(
    window_days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await _build_user_activity_report(db, current_user.id, window_days)


@router.get("/chat/admin-activity", response_model=AdminActivityReport)
async def get_admin_activity_report(
    window_days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_admin_activity_report(db, window_days)


@router.get("/chat/activity/timeline", response_model=ActivityTimelineReport)
async def get_activity_timeline(
    window_days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await _build_activity_timeline(db, current_user.id, window_days)


@router.get("/chat/admin/users", response_model=RankedUserReport)
async def get_ranked_users_report(
    window_days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=10, ge=1, le=100),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_ranked_users_report(db, window_days, limit)


@router.get("/chat/admin/retention-trends", response_model=AdminRetentionTrendReport)
async def get_admin_retention_trend_report(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_admin_retention_trend_report(db, window_days)


@router.get("/chat/admin/snapshot-summary", response_model=RetentionSnapshotAdminReport)
async def get_retention_snapshot_admin_report(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_admin_report(db, window_days)


@router.get("/chat/snapshots/health", response_model=UserRetentionSnapshotHealth)
async def get_user_retention_snapshot_health(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await _build_user_retention_snapshot_health(db, current_user.id, window_days)


@router.get("/chat/admin/snapshot-comparison", response_model=RetentionSnapshotComparisonReport)
async def get_retention_snapshot_comparison_report(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_comparison_report(db, window_days)


@router.get("/chat/admin/snapshot-momentum", response_model=RetentionSnapshotMomentumReport)
async def get_retention_snapshot_momentum_report(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_momentum_report(db, window_days)


@router.get("/chat/admin/snapshot-volatility", response_model=RetentionSnapshotVolatilityReport)
async def get_retention_snapshot_volatility_report(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_volatility_report(db, window_days)


@router.get("/chat/admin/snapshot-volatility/summary", response_model=RetentionSnapshotVolatilitySummary)
async def get_retention_snapshot_volatility_summary(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_volatility_summary(db, window_days)


@router.get("/chat/admin/snapshot-risk-profile", response_model=RetentionSnapshotRiskProfile)
async def get_retention_snapshot_risk_profile(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_risk_profile(db, window_days)


@router.get("/chat/admin/snapshot-recommendation", response_model=RetentionSnapshotRecommendation)
async def get_retention_snapshot_recommendation(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_recommendation(db, window_days)


@router.get("/chat/admin/snapshot-action-plan", response_model=RetentionSnapshotActionPlan)
async def get_retention_snapshot_action_plan(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_action_plan(db, window_days)


@router.get("/chat/admin/snapshot-audit", response_model=RetentionSnapshotAuditReport)
async def get_retention_snapshot_audit_report(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_audit_report(db, window_days)


@router.get("/chat/admin/snapshot-audit/export", response_model=RetentionSnapshotAuditExport)
async def get_retention_snapshot_audit_export(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_audit_export(db, window_days)


@router.get("/chat/admin/snapshot-type-breakdown", response_model=RetentionSnapshotTypeBreakdownReport)
async def get_retention_snapshot_type_breakdown(
    snapshot_type: str = Query(..., min_length=1),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_type_breakdown(db, window_days, snapshot_type)


@router.get("/chat/admin/snapshot-staleness", response_model=RetentionSnapshotStalenessReport)
async def get_retention_snapshot_staleness_report(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_staleness_report(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-staleness/trend", response_model=RetentionSnapshotStalenessTrendReport)
async def get_retention_snapshot_staleness_trend(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_staleness_trend(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-health-score", response_model=RetentionSnapshotHealthScore)
async def get_retention_snapshot_health_score(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_health_score(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-health-summary", response_model=RetentionSnapshotHealthSummary)
async def get_retention_snapshot_health_summary(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_health_summary(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-health-risk", response_model=RetentionSnapshotHealthRisk)
async def get_retention_snapshot_health_risk(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_health_risk(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-health-recommendation", response_model=RetentionSnapshotHealthRecommendation)
async def get_retention_snapshot_health_recommendation(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_health_recommendation(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-operations-overview", response_model=RetentionSnapshotOperationsOverview)
async def get_retention_snapshot_operations_overview(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_operations_overview(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-operations-status", response_model=RetentionSnapshotOperationsStatus)
async def get_retention_snapshot_operations_status(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_operations_status(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-operations-compliance", response_model=RetentionSnapshotOperationsCompliance)
async def get_retention_snapshot_operations_compliance(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_operations_compliance(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-operations-posture", response_model=RetentionSnapshotOperationsPosture)
async def get_retention_snapshot_operations_posture(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_operations_posture(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-operations-automation", response_model=RetentionSnapshotOperationsAutomation)
async def get_retention_snapshot_operations_automation(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_operations_automation(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-operations-execution-state", response_model=RetentionSnapshotOperationsExecutionState)
async def get_retention_snapshot_operations_execution_state(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_operations_execution_state(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-operations-launch-readiness", response_model=RetentionSnapshotOperationsLaunchReadiness)
async def get_retention_snapshot_operations_launch_readiness(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_operations_launch_readiness(db, window_days, stale_after_days)


@router.get("/chat/admin/snapshot-operations-gonogo", response_model=RetentionSnapshotOperationsGoNoGo)
async def get_retention_snapshot_operations_go_no_go(
    stale_after_days: int = Query(default=14, ge=1, le=365),
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_snapshot_operations_go_no_go(db, window_days, stale_after_days)



@router.get("/chat/admin/monetization-cohorts", response_model=MonetizationCohortReport)
async def get_monetization_cohorts(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await build_monetization_cohorts(db, window_days)

def analyze_sentiment(text: str):
    headers = {"Authorization": f"Bearer {HF_API_TOKEN}"} if HF_API_TOKEN else {}
    try:
        response = requests.post(HF_SENTIMENT_URL, headers=headers, json={"inputs": text}, timeout=10)
        response.raise_for_status()
        result = response.json()
        label = result[0][0]["label"].lower()
        score = result[0][0]["score"]
        return Sentiment(label=label, score=score)
    except Exception:
        return None

@router.post("/chat", response_model=ChatMessageOut)
async def chat_message(
    payload: ChatMessageIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    user_message = payload.message
    if not user_message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message required"
        )

    ai_response = f"You said: '{user_message}'. How else can I help you?"

    sentiment = analyze_sentiment(user_message)
    chat_rows, bookings = await _load_user_interaction_window(db, current_user.id)
    insights = _build_interaction_insights([row.message for row in chat_rows] + [user_message], bookings, sentiment)
    summary = _build_summary(current_user.id, chat_rows, bookings, sentiment)
    improvement_pack = _build_system_improvement_pack(current_user.id, chat_rows, bookings, sentiment)

    suggestions = [
        "Review the top retention risks",
        "See which interaction patterns repeat",
        "Check booking friction signals",
    ]

    vector = [
        round(min(len(chat_rows) / 10.0, 1.0), 2),
        round(min(len(bookings) / 10.0, 1.0), 2),
        round(summary.loyalty_score / 100.0, 2),
    ]

    # Save chat history
    chat_entry = models.ChatHistory(
        user_id=current_user.id if current_user else None,
        message=user_message,
        response=ai_response
    )
    db.add(chat_entry)
    await db.commit()
    await db.refresh(chat_entry)

    await _store_interaction_signals(db, current_user.id, improvement_pack)
    lifecycle_stage = "new"
    if summary.churn_risk == "high":
        lifecycle_stage = "at_risk"
    elif summary.churn_risk == "medium":
        lifecycle_stage = "recovering"
    elif summary.loyalty_score >= 80:
        lifecycle_stage = "loyal"
    elif len(chat_rows) >= 3 or len(bookings) >= 1:
        lifecycle_stage = "engaged"
    await _save_retention_snapshot(db, current_user.id, 30, "chat_response", summary, lifecycle_stage)
    await _prune_retention_snapshots(db, current_user.id, 30, keep=20)

    return ChatMessageOut(
        text=ai_response,
        sentiment=sentiment,
        suggestions=suggestions,
        vector=vector,
        insights=insights,
        summary=summary,
        improvement_pack=improvement_pack,
    )

@router.get("/chat/history", response_model=list[ChatHistoryOut])
async def get_chat_history(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    q = select(models.ChatHistory).where(models.ChatHistory.user_id == current_user.id).order_by(models.ChatHistory.timestamp.desc())
    res = await db.execute(q)
    return res.scalars().all()


@router.get("/chat/insights", response_model=InteractionSummary)
async def get_chat_insights(
    window_days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    chat_query = (
        select(models.ChatHistory)
        .where(models.ChatHistory.user_id == current_user.id)
        .where(models.ChatHistory.timestamp >= cutoff)
        .order_by(desc(models.ChatHistory.timestamp))
    )
    booking_query = (
        select(models.Booking)
        .where(models.Booking.user_id == current_user.id)
        .where(models.Booking.created_at >= cutoff)
        .order_by(desc(models.Booking.created_at))
    )

    chat_result = await db.execute(chat_query)
    booking_result = await db.execute(booking_query)
    chat_rows = chat_result.scalars().all()
    bookings = booking_result.scalars().all()

    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    return _build_summary(current_user.id, chat_rows, bookings, latest_sentiment)


@router.get("/chat/signals")
async def get_interaction_signals(
    limit: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    query = (
        select(models.InteractionSignal)
        .where(models.InteractionSignal.user_id == current_user.id)
        .order_by(desc(models.InteractionSignal.created_at))
        .limit(limit)
    )
    result = await db.execute(query)
    signals = result.scalars().all()
    return [
        {
            "id": signal.id,
            "user_id": signal.user_id,
            "source": signal.source,
            "area": signal.area,
            "priority": signal.priority,
            "score": signal.score,
            "evidence": signal.evidence,
            "recommendation": signal.recommendation,
            "created_at": signal.created_at,
        }
        for signal in signals
    ]


@router.get("/chat/system-priorities")
async def get_system_priorities(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    query = (
        select(models.InteractionSignal.area, func.count(models.InteractionSignal.id), func.avg(models.InteractionSignal.score))
        .where(models.InteractionSignal.user_id == current_user.id)
        .group_by(models.InteractionSignal.area)
        .order_by(desc(func.avg(models.InteractionSignal.score)))
    )
    result = await db.execute(query)
    rows = result.all()

    priorities = []
    for area, count_value, avg_score in rows:
        if area == "response_speed":
            recommendation = "Invest in faster handling and clear waiting states."
        elif area == "clarity":
            recommendation = "Simplify confusing paths and shorten instructions."
        elif area == "reliability":
            recommendation = "Fix error-prone journeys and add more resilient fallbacks."
        elif area == "booking_flow":
            recommendation = "Reduce booking friction and improve confirmation visibility."
        elif area == "support":
            recommendation = "Expose quicker support and guided help for repeat questions."
        elif area == "pricing":
            recommendation = "Clarify pricing/value tradeoffs and reduce purchase hesitation."
        else:
            recommendation = "Create stronger follow-up loops for unresolved interactions."

        priorities.append(
            {
                "area": area,
                "signal_count": count_value,
                "avg_score": round(float(avg_score or 0.0), 2),
                "recommendation": recommendation,
            }
        )

    return {
        "user_id": current_user.id,
        "focus": "loyalty and retention improvements",
        "priorities": priorities,
    }


@router.get("/chat/trends", response_model=InteractionTrendReport)
async def get_interaction_trends(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    return await _load_signal_trends(db, current_user.id, window_days)


@router.get("/chat/retention-cohorts", response_model=RetentionCohortReport)
async def get_retention_cohorts(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    return await _build_retention_cohorts(db, window_days)


@router.get("/chat/retention-cohorts/{cohort}", response_model=RetentionCohortDrilldownReport)
async def get_retention_cohort_drilldown(
    cohort: str,
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await _build_retention_cohort_drilldown(db, window_days, cohort)


@router.get("/chat/churn-prediction", response_model=ChurnPrediction)
async def get_churn_prediction(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    return await _build_churn_prediction(db, current_user.id, window_days)


@router.get("/chat/lifecycle-stages", response_model=LifecycleStageReport)
async def get_lifecycle_stages(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    return await _build_lifecycle_stage_report(db, window_days)


@router.get("/chat/snapshots", response_model=RetentionSnapshotReport)
async def get_retention_snapshots(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    return await _build_retention_snapshot_report(db, current_user.id, window_days)


@router.get("/chat/snapshots/delta", response_model=RetentionSnapshotDelta)
async def get_retention_snapshot_delta(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    return await _build_retention_snapshot_delta(db, current_user.id, window_days)


@router.get("/chat/snapshots/trends", response_model=RetentionSnapshotTrendReport)
async def get_retention_snapshot_trends(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    return await _build_retention_snapshot_trends(db, current_user.id, window_days)


@router.get("/chat/retention-dashboard", response_model=RetentionDashboard)
async def get_retention_dashboard(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    return await _build_retention_dashboard(db, current_user.id, window_days)


@router.get("/chat/improvement-pack", response_model=SystemImprovementPack)
async def get_improvement_pack(
    window_days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    chat_query = (
        select(models.ChatHistory)
        .where(models.ChatHistory.user_id == current_user.id)
        .where(models.ChatHistory.timestamp >= cutoff)
        .order_by(desc(models.ChatHistory.timestamp))
    )
    booking_query = (
        select(models.Booking)
        .where(models.Booking.user_id == current_user.id)
        .where(models.Booking.created_at >= cutoff)
        .order_by(desc(models.Booking.created_at))
    )

    chat_result = await db.execute(chat_query)
    booking_result = await db.execute(booking_query)
    chat_rows = chat_result.scalars().all()
    bookings = booking_result.scalars().all()
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    return _build_system_improvement_pack(current_user.id, chat_rows, bookings, latest_sentiment)