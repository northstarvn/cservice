from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, desc, case
from app import deps, models
from app.schemas.chat import (
    ChatMessageIn,
    ChatMessageOut,
    Sentiment,
    ChatHistoryOut,
    InteractionSummary,
    SystemImprovementPack,
    InteractionTrendReport,
    RetentionCohortReport,
    RetentionCohortMember,
    RetentionCohortDrilldownReport,
    ChurnPrediction,
    LifecycleStageItem,
    LifecycleStageReport,
    RetentionSnapshotReport,
    RetentionSnapshotDelta,
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
    RetentionSnapshotAdminReport,
    UserRetentionSnapshotHealth,
    RetentionSnapshotComparisonReport,
    RetentionSnapshotMomentumReport,
    RetentionSnapshotVolatilityReport,
    RetentionSnapshotVolatilitySummary,
    RetentionSnapshotRiskProfile,
    RetentionSnapshotRecommendation,
    RetentionSnapshotActionPlan,
    RetentionSnapshotAuditReport,
    RetentionSnapshotAuditExport,
    RetentionSnapshotTypeBreakdownReport,
    RetentionSnapshotStalenessReport,
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
    RetentionSnapshotOperationsReport,
    RetentionCoverageReport,
    RetentionOperationalReport,
    TopicRankingReport,
    TopicPolicyDecisionReport,
    LoyaltyRecoveryReport,
    DissatisfactionRecoveryReport,
    RecoveryOutcomeReport,
    RecoveryOutcomeAggregateItem,
    RecoveryOutcomeAggregateReport,
    MonetizationCohortReport,
    WeightedSystemMonitoringReport,
    WeightedFocusItem,
    LoyaltyJourneyPlan,
    LoyaltyJourneyAdminReport,
)
from app.services.chat_analytics import (
    analyze_sentiment,
    build_churn_prediction,
    build_interaction_insights,
    build_loyalty_recovery_report,
    build_monetization_cohorts,
    build_retention_cohorts,
    build_retention_snapshot_operations_report,
    build_summary,
    build_system_improvement_pack,
    build_topic_policy_decision_report,
    build_topic_ranking_report,
    classify_lifecycle_stage,
    classify_loyalty_cohort,
    load_signal_trends,
    load_user_interaction_window,
)
from app.services.retention import (
    build_retention_coverage_report,
    build_retention_dashboard,
    build_retention_operational_report,
    build_retention_snapshot_admin_report,
    build_retention_snapshot_delta,
    build_retention_snapshot_report,
    build_retention_snapshot_trends,
    build_user_retention_snapshot_health,
    prune_and_report_retention_snapshots,
    prune_retention_snapshots,
)
from app.services.loyalty_journey import (
    build_loyalty_journey_admin_report,
    build_loyalty_journey_plan_for_user,
)
from app.services.retention_snapshots import (
    _build_retention_snapshot_action_plan,
    _build_retention_snapshot_audit_export,
    _build_retention_snapshot_audit_report,
    _build_retention_snapshot_comparison_report,
    _build_retention_snapshot_health_recommendation,
    _build_retention_snapshot_health_risk,
    _build_retention_snapshot_health_score,
    _build_retention_snapshot_health_summary,
    _build_retention_snapshot_momentum_report,
    _build_retention_snapshot_operations_automation,
    _build_retention_snapshot_operations_compliance,
    _build_retention_snapshot_operations_execution_state,
    _build_retention_snapshot_operations_go_no_go,
    _build_retention_snapshot_operations_launch_readiness,
    _build_retention_snapshot_operations_overview,
    _build_retention_snapshot_operations_posture,
    _build_retention_snapshot_operations_status,
    _build_retention_snapshot_recommendation,
    _build_retention_snapshot_risk_profile,
    _build_retention_snapshot_staleness_report,
    _build_retention_snapshot_staleness_trend,
    _build_retention_snapshot_type_breakdown,
    _build_retention_snapshot_volatility_report,
    _build_retention_snapshot_volatility_summary,
)
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import json

# Load environment variables from .env file
load_dotenv()

router = APIRouter()


_current_control_posture = deps.current_control_posture


_build_interaction_insights = build_interaction_insights


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
    capabilities = _build_ai_capabilities_catalog()
    endpoint_map = {
        "retention_snapshot_health_recommendation": "/chat/admin/snapshot-health-recommendation",
        "retention_snapshot_operations_report": "/chat/admin/snapshot-operations-report",
        "retention_snapshot_operations_status": "/chat/admin/snapshot-operations-status",
        "retention_snapshot_operations_compliance": "/chat/admin/snapshot-operations-compliance",
        "retention_snapshot_operations_posture": "/chat/admin/snapshot-operations-posture",
        "retention_snapshot_operations_automation": "/chat/admin/snapshot-operations-automation",
        "retention_snapshot_operations_execution_state": "/chat/admin/snapshot-operations-execution-state",
        "retention_snapshot_operations_launch_readiness": "/chat/admin/snapshot-operations-launch-readiness",
        "retention_snapshot_operations_gonogo": "/chat/admin/snapshot-operations-gonogo",
    }
    freshness_window_days = int(os.getenv("CSERVICE_CAPABILITY_FRESHNESS_DAYS", "30"))
    covered_endpoint_count = sum(1 for feature in endpoint_map if feature in {item["id"] for item in capabilities})
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
        "capabilities": capabilities,
        "coverage": {
            "total_capabilities": len(capabilities),
            "enabled_capabilities": sum(1 for item in capabilities if item.get("enabled")),
            "endpoint_coverage": round(covered_endpoint_count / max(len(endpoint_map), 1), 2),
            "freshness_window_days": freshness_window_days,
            "status": "ready" if covered_endpoint_count == len(endpoint_map) else "partial",
        },
        "roles": [
            {
                "id": "youth_conversion_intelligence",
                "study_target": "Younger demographics are less likely to make large purchases online.",
                "owner_hint": "growth and lifecycle",
            },
            {
                "id": "market_penetration_adoption",
                "study_target": "Higher internet penetration does not always mean higher e-commerce adoption.",
                "owner_hint": "regional strategy",
            },
            {
                "id": "device_experience_optimizer",
                "study_target": "More developed regions have lower engagement in mobile-only usage.",
                "owner_hint": "frontend and performance",
            },
            {
                "id": "cpc_economics_profiler",
                "study_target": "Lower average income can still produce higher CPC in advertising.",
                "owner_hint": "marketing analytics",
            },
            {
                "id": "older_adult_value_model",
                "study_target": "Older adults are not only growing in usage but also outperform younger groups in certain metrics.",
                "owner_hint": "retention and UX",
            },
            {
                "id": "low_penetration_engagement_engine",
                "study_target": "Regions with lower internet penetration can have users who are more engaged per capita.",
                "owner_hint": "mobile growth",
            },
        ],
        "endpoints": {
            "retention_snapshot_health_recommendation": "/chat/admin/snapshot-health-recommendation",
            "retention_snapshot_operations_report": "/chat/admin/snapshot-operations-report",
            "retention_snapshot_operations_status": "/chat/admin/snapshot-operations-status",
            "retention_snapshot_operations_compliance": "/chat/admin/snapshot-operations-compliance",
            "retention_snapshot_operations_posture": "/chat/admin/snapshot-operations-posture",
            "retention_snapshot_operations_automation": "/chat/admin/snapshot-operations-automation",
            "retention_snapshot_operations_execution_state": "/chat/admin/snapshot-operations-execution-state",
            "retention_snapshot_operations_launch_readiness": "/chat/admin/snapshot-operations-launch-readiness",
            "retention_snapshot_operations_gonogo": "/chat/admin/snapshot-operations-gonogo",
        },
    }


_load_user_interaction_window = load_user_interaction_window


def _build_summary(user_id: int, chat_rows: list[models.ChatHistory], bookings: list[models.Booking], sentiment: Sentiment | None) -> InteractionSummary:
    """Retained router-local name; delegates to the canonical service builder."""
    return build_summary(user_id, chat_rows, bookings, sentiment)


def _build_system_improvement_pack(user_id: int, chat_rows: list[models.ChatHistory], bookings: list[models.Booking], sentiment: Sentiment | None) -> SystemImprovementPack:
    """Retained router-local name; delegates to the canonical service builder."""
    return build_system_improvement_pack(user_id, chat_rows, bookings, sentiment)


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


@router.get("/topic-ranking", response_model=TopicRankingReport)
async def topic_ranking(current_user: models.User = Depends(deps.get_current_user), db: AsyncSession = Depends(deps.get_db)):
    chat_rows, bookings = await _load_user_interaction_window(db, current_user.id, 30)
    messages = [row.message for row in chat_rows]
    return build_topic_ranking_report(messages, bookings, window_days=30, limit=5)


@router.get("/topic-policy-decisions", response_model=TopicPolicyDecisionReport)
async def topic_policy_decisions(current_user: models.User = Depends(deps.get_current_user), db: AsyncSession = Depends(deps.get_db)):
    chat_rows, bookings = await _load_user_interaction_window(db, current_user.id, 30)
    messages = [row.message for row in chat_rows]
    return build_topic_policy_decision_report(messages, bookings, window_days=30, limit=5)


_load_signal_trends = load_signal_trends


async def _store_interaction_signals(db: AsyncSession, user_id: int, pack: SystemImprovementPack) -> None:
    for item in pack.items:
        summary = pack.summary
        signal_payload = {
            "area": item.area,
            "priority": item.priority,
            "impact": item.impact,
            "rationale": item.rationale,
            "recommendation": item.recommendation,
            "owner_hint": item.owner_hint,
            "summary": {
                "loyalty_score": summary.loyalty_score,
                "monetization_readiness": summary.monetization_readiness,
                "churn_risk": summary.churn_risk,
                "repeated_messages": summary.metadata.get("repeated_messages", 0),
                "booking_states": dict(summary.metadata.get("booking_states", {})),
                "signal_strength": summary.metadata.get("signal_strength", 0.0),
            },
        }
        signal = models.InteractionSignal(
            user_id=user_id,
            source="chat",
            area=item.area,
            priority=item.priority,
            score=float(next((ins.score for ins in pack.summary.insights if ins.area == item.area), 0.0)),
            evidence=json.dumps(signal_payload),
            recommendation=item.recommendation,
        )
        db.add(signal)
    await db.commit()


async def _store_recovery_outcome(db: AsyncSession, user_id: int, recovery: DissatisfactionRecoveryReport) -> models.RecoveryOutcome:
    complaint_recurrence_count = sum(1 for signal in recovery.recovery_signals if signal.area in {"follow_up", "retention", "support", "handoff"})
    outcome = models.RecoveryOutcome(
        user_id=user_id,
        recovery_readiness=recovery.recovery_readiness,
        dissatisfaction_score=recovery.dissatisfaction_score,
        primary_risks_json=json.dumps(recovery.primary_risks),
        recovery_signals_json=json.dumps([
            {
                "area": signal.area,
                "intensity": signal.intensity,
                "evidence": signal.evidence,
                "evidence_summary": signal.evidence_summary,
                "recommended_action": signal.recommended_action,
            }
            for signal in recovery.recovery_signals
        ]),
        escalation_path_json=json.dumps([
            {
                "area": signal.area,
                "intensity": signal.intensity,
                "recommended_action": signal.recommended_action,
            }
            for signal in recovery.recovery_signals
            if signal.area in {"follow_up", "retention", "support", "handoff"}
        ]),
        handoff_outcome_json=json.dumps({
            "status": "open",
            "owner": "support",
            "review_state": recovery.recovery_readiness,
        }),
        follow_up_json=json.dumps({
            "complaint_recurrence_count": complaint_recurrence_count,
            "follow_up_count": len(recovery.recovery_signals),
            "acknowledged": recovery.recovery_readiness in {"low", "moderate"},
            "follow_up_completed": recovery.recovery_readiness == "low",
        }),
        action_plan=recovery.action_plan,
        acknowledged=recovery.recovery_readiness in {"low", "moderate"},
        acknowledged_at=datetime.now(timezone.utc) if recovery.recovery_readiness in {"low", "moderate"} else None,
        follow_up_completed=recovery.recovery_readiness == "low",
        follow_up_completed_at=datetime.now(timezone.utc) if recovery.recovery_readiness == "low" else None,
        source="chat_recovery",
    )
    db.add(outcome)
    await db.commit()
    await db.refresh(outcome)
    return outcome


_classify_loyalty_cohort = classify_loyalty_cohort


async def _build_retention_cohorts(db: AsyncSession, window_days: int) -> RetentionCohortReport:
    return await build_retention_cohorts(db, window_days)


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
    return await build_churn_prediction(db, user_id, window_days)


_classify_lifecycle_stage = classify_lifecycle_stage


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
    await prune_retention_snapshots(db, user_id, window_days, keep)


async def _prune_retention_snapshots_with_report(
    db: AsyncSession,
    user_id: int,
    window_days: int,
    keep: int = 20,
) -> dict:
    return await prune_and_report_retention_snapshots(db, user_id, window_days, keep)


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
    return await build_retention_snapshot_admin_report(db, window_days)


async def _build_user_retention_snapshot_health(db: AsyncSession, user_id: int, window_days: int) -> UserRetentionSnapshotHealth:
    return await build_user_retention_snapshot_health(db, user_id, window_days)


async def _build_loyalty_recovery_dashboard(db: AsyncSession, user_id: int, window_days: int) -> LoyaltyRecoveryReport:
    chat_rows, bookings = await load_user_interaction_window(db, user_id, window_days)
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
    dissatisfaction, recommendation, action_plan, recovery_risk = build_loyalty_recovery_report(summary, latest_sentiment)
    return LoyaltyRecoveryReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        loyalty_score=summary.loyalty_score,
        churn_risk=summary.churn_risk,
        recovery_readiness=recovery_risk,
        dissatisfaction=dissatisfaction,
        retention_recommendation=recommendation,
        action_plan=action_plan,
    )


async def _build_retention_snapshot_report(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotReport:
    return await build_retention_snapshot_report(db, user_id, window_days)


async def _build_retention_snapshot_delta(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotDelta:
    return await build_retention_snapshot_delta(db, user_id, window_days)


async def _build_retention_snapshot_trends(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotTrendReport:
    return await build_retention_snapshot_trends(db, user_id, window_days)


async def _build_retention_dashboard(db: AsyncSession, user_id: int, window_days: int) -> RetentionDashboard:
    return await build_retention_dashboard(db, user_id, window_days)


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


@router.get("/chat/admin/snapshot-operations-report", response_model=RetentionSnapshotOperationsReport)
async def get_retention_snapshot_operations_report(
    window_days: int = Query(default=30, ge=7, le=365),
    stale_after_days: int = Query(default=14, ge=1, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await build_retention_snapshot_operations_report(db, window_days, stale_after_days)


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


@router.get("/chat/admin/retention-coverage", response_model=RetentionCoverageReport)
async def get_retention_coverage_report(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await build_retention_coverage_report(db, current_user.id, window_days)


@router.get("/chat/admin/retention-operations", response_model=RetentionOperationalReport)
async def get_retention_operational_report(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await build_retention_operational_report(db, current_user.id, window_days)


@router.get("/chat/admin/monetization-cohorts", response_model=MonetizationCohortReport)
async def get_monetization_cohorts(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await build_monetization_cohorts(db, window_days)


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
    if _current_control_posture(current_user) in {"observed", "constrained"}:
        summary.metadata["control_posture_note"] = "Controlled posture applied."

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


@router.get("/chat/recovery", response_model=RecoveryOutcomeReport)
async def get_chat_recovery(
    window_days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
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
    summary = _build_summary(current_user.id, chat_rows, bookings, latest_sentiment)
    dissatisfaction, retention_recommendation, action_plan, churn_risk = build_loyalty_recovery_report(summary, latest_sentiment)
    outcome = await _store_recovery_outcome(db, current_user.id, dissatisfaction)

    attempts_result = await db.execute(
        select(func.count(models.RecoveryOutcome.id)).where(models.RecoveryOutcome.user_id == current_user.id)
    )
    acknowledged_result = await db.execute(
        select(func.count(models.RecoveryOutcome.id)).where(
            models.RecoveryOutcome.user_id == current_user.id,
            models.RecoveryOutcome.acknowledged.is_(True),
        )
    )
    recovery_attempts = int(attempts_result.scalar() or 0)
    recovery_acknowledged = int(acknowledged_result.scalar() or 0) > 0
    complaint_recurrence_count = sum(1 for signal in dissatisfaction.recovery_signals if signal.area in {"follow_up", "retention", "support", "handoff"})
    time_to_acknowledge_minutes = None
    if outcome.acknowledged and outcome.acknowledged_at:
        time_to_acknowledge_minutes = round(max((outcome.acknowledged_at - outcome.created_at).total_seconds() / 60.0, 0.0), 2)

    return RecoveryOutcomeReport(
        generated_at=outcome.created_at,
        window_days=window_days,
        summary=summary,
        dissatisfaction=dissatisfaction,
        retention_recommendation=retention_recommendation,
        action_plan=action_plan,
        recovery_outcome={
            "id": outcome.id,
            "recovery_readiness": outcome.recovery_readiness,
            "dissatisfaction_score": outcome.dissatisfaction_score,
            "acknowledged": outcome.acknowledged,
            "acknowledged_at": outcome.acknowledged_at,
            "source": outcome.source,
            "follow_up_count": complaint_recurrence_count,
            "complaint_recurrence_count": complaint_recurrence_count,
            "time_to_acknowledge_minutes": time_to_acknowledge_minutes,
        },
        churn_risk=churn_risk,
        recovery_attempts=recovery_attempts,
        recovery_acknowledged=recovery_acknowledged,
        complaint_recurrence_count=complaint_recurrence_count,
        time_to_acknowledge_minutes=time_to_acknowledge_minutes,
    )


@router.get("/chat/admin/recovery-outcomes", response_model=RecoveryOutcomeAggregateReport)
async def get_recovery_outcome_aggregate(
    window_days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = (
        select(
            models.RecoveryOutcome.recovery_readiness,
            func.count(models.RecoveryOutcome.id),
            func.sum(case((models.RecoveryOutcome.acknowledged.is_(True), 1), else_=0)),
        )
        .where(models.RecoveryOutcome.created_at >= cutoff)
        .group_by(models.RecoveryOutcome.recovery_readiness)
    )
    result = await db.execute(query)
    rows = result.all()
    total_complaint_recurrences = sum(
        int(row[0] == "moderate") + int(row[0] == "high")
        for row in rows
    )
    items = [
        RecoveryOutcomeAggregateItem(
            recovery_readiness=str(recovery_readiness),
            attempts=int(attempts or 0),
            acknowledged=int(acknowledged or 0),
        )
        for recovery_readiness, attempts, acknowledged in rows
    ]
    total_attempts = sum(item.attempts for item in items)
    total_acknowledged = sum(item.acknowledged for item in items)
    return RecoveryOutcomeAggregateReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        total_attempts=total_attempts,
        total_acknowledged=total_acknowledged,
        total_complaint_recurrences=total_complaint_recurrences,
        items=items,
    )


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

    total_signals = sum(item["signal_count"] for item in priorities)
    top_priority = priorities[0]["area"] if priorities else "none"

    return {
        "user_id": current_user.id,
        "focus": "loyalty and retention improvements",
        "priorities": priorities,
        "summary": {
            "total_signals": total_signals,
            "tracked_areas": len(priorities),
            "top_priority": top_priority,
            "service_health": "ready" if priorities else "insufficient_signal_data",
        },
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


@router.get("/chat/recovery-dashboard", response_model=LoyaltyRecoveryReport)
async def get_recovery_dashboard(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    return await _build_loyalty_recovery_dashboard(db, current_user.id, window_days)


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


@router.get("/chat/loyalty-journey", response_model=LoyaltyJourneyPlan)
async def get_loyalty_journey(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await build_loyalty_journey_plan_for_user(db, current_user.id, window_days)


@router.get("/chat/admin/loyalty-journey", response_model=LoyaltyJourneyAdminReport)
async def get_loyalty_journey_admin(
    window_days: int = Query(default=30, ge=7, le=365),
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await build_loyalty_journey_admin_report(db, window_days, limit)