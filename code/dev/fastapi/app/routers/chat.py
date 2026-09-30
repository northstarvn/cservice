from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import Optional
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
    RetentionClusterCoverageReport,
    RetentionForecastReport,
    RetentionHealthDecisionReport,
    RetentionHorizonSweepReport,
    TopicRankingReport,
    TopicPolicyDecisionReport,
    LoyaltyRecoveryReport,
    DissatisfactionRecoveryReport,
    RecoveryOutcomeReport,
    RecoveryOutcomeAggregateItem,
    RecoveryOutcomeAggregateReport,
    RecoveryPlaybookRunRequest,
    RecoveryPlaybookRunReport,
    RecoveryActionAnalyticsReport,
    RecoveryGuardReport,
    RecoveryOutreachPlan,
    MonetizationCohortReport,
    WeightedSystemMonitoringReport,
    WeightedFocusItem,
    LoyaltyJourneyPlan,
    LoyaltyJourneyAdminReport,
    ActivityTreeReport,
    CommunicationStrategyResult,
    CommunicationOverrideCreate,
    CommunicationAdminOverrideItem,
    CommunicationOverrideReport,
    CommunicationAdminStrategyReport,
    ArrearsEntryOut,
    ArrearsOpenRequest,
    ArrearsQuote,
    ArrearsListReport,
    ArrearsAdminReport,
    ArrearsSettleResult,
    ArrearsWaiveResult,
    PointsExchangeRates,
    PointsExchangeQuoteRequest,
    PointsExchangeQuote,
    PointsExchangeRequest,
    PointsExchangeResult,
    PointsWalletReport,
    PointsTransactionsReport,
    PointsAdminReport,
    Customer360Report,
    CustomerRecoveryStatus,
    PreferenceConsentReport,
    PreferenceUpdateRequest,
    PreferenceUpdateResponse,
    SelfServiceStatusReport,
    PolicyPostureSummary,
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
    build_retention_cluster_coverage_for_user,
    build_retention_coverage_report,
    build_retention_dashboard,
    build_retention_forecast_for_user,
    build_retention_health_report_for_user,
    build_retention_horizon_sweep_for_user,
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
from app.services.activity_tree import (
    build_activity_tree,
    build_self_activity_tree,
)
from app.services.communication_strategy import (
    build_communication_strategy_admin_report,
    delete_communication_admin_override,
    list_communication_admin_overrides,
    resolve_communication_strategy_for_user,
    set_communication_admin_override,
)
from app.services.arrears_payments import (
    build_arrears_admin_report,
    list_user_arrears,
    open_arrears_payment,
    quote_arrears_for_user,
    settle_arrears_entry,
    waive_arrears_interest,
)
from app.services.points_exchange import (
    build_points_exchange_admin_report,
    build_points_exchange_rates,
    execute_points_exchange_for_user,
    list_user_point_transactions,
    list_user_wallets,
    quote_points_exchange_for_user,
)
from app.services.policy_scoring import (
    build_customer_policy_snapshot,
)
from app.services.recovery_playbooks import (
    build_recovery_action_analytics_for_user,
    build_recovery_guard_report_for_user,
    build_recovery_outreach_plan_for_user,
    build_recovery_playbook_catalog,
    run_recovery_playbooks,
)
from app.services.customer_360 import (
    build_customer_360,
    build_customer_360_admin_report,
)
from app.services.customer_explain import (
    build_explainability_vocabulary_catalog,
    explain_churn_risk,
    explain_loyalty_score,
    explain_policy_posture,
    explain_recovery_status,
    explain_value_tier,
    validate_explanation_tables,
)
from app.services.preferences import (
    build_preference_consent_report,
    list_consent_events,
    preference_catalog,
    update_user_preferences,
)
from app.services.self_service import (
    build_customer_recovery_status,
    build_points_forecast,
    build_policy_posture_summary,
    build_self_service_status,
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


# --- retention policy surfaces (health banding / forecasting) ----------------
#
# These read the same snapshot rows the reports above do; what is new is that the
# verdict is produced by ``RETENTION_HEALTH_RULES`` / ``RETENTION_ANOMALY_RULES``
# / ``RETENTION_FORECAST_RULES`` and travels with the evidence that produced it.
# Retuning any of them is a table edit, not a redeploy of this router.


@router.get("/chat/admin/retention-health", response_model=RetentionHealthDecisionReport)
async def get_retention_health_decision(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Health band + series anomalies for a customer's snapshot series.

    ``effective_date`` is exposed as a query parameter so a decision can be
    reproduced as of a past date instead of only "now"; date-window rules in the
    shared engine bind it.
    """
    effective_date = Query(default=None, description="ISO-8601 date to evaluate date-window rules against")
    return await build_retention_health_report_for_user(
        db,
        current_user.id,
        window_days,
        effective_date=effective_date,
    )


@router.get("/chat/admin/retention-forecast", response_model=RetentionForecastReport)
async def get_retention_forecast(
    window_days: int = Query(default=30, ge=7, le=365),
    horizon_days: int = Query(default=30, ge=1, le=365),
    method: str | None = Query(default=None, description="forecast method; see catalog available_methods"),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Project loyalty and churn forward. Declines when the series is too thin.

    An unknown ``method`` is reported in ``reason`` with
    ``sufficient_data=false`` rather than 422, because the set of valid methods
    is configuration and a client holding a stale method name should be told what
    the current set is.
    """
    return await build_retention_forecast_for_user(
        db,
        current_user.id,
        window_days,
        horizon_days=horizon_days,
        method=method,
    )


@router.get("/chat/admin/retention-forecast-sweep", response_model=RetentionHorizonSweepReport)
async def get_retention_forecast_sweep(
    window_days: int = Query(default=30, ge=7, le=365),
    method: str | None = Query(default=None),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The same projection at every configured horizon, with its confidence decay."""
    return await build_retention_horizon_sweep_for_user(
        db, current_user.id, window_days, method=method
    )


@router.get("/chat/admin/retention-cluster-coverage", response_model=RetentionClusterCoverageReport)
async def get_retention_cluster_coverage(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Which retention topic clusters the customer's snapshots speak to — and which are silent."""
    return await build_retention_cluster_coverage_for_user(db, current_user.id, window_days)


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


@router.get("/chat/admin/recovery/playbooks")
async def get_recovery_playbook_catalog(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Expose the config-driven recovery playbook table (read-only, idempotent).

    ``playbooks`` and ``catalog_version`` stay the frozen v1 core set; the
    governance layer (action registry, guard rules, outreach playbooks) is
    published under its own keys and version so a client can tell a core change
    from a new subsystem.
    """
    return build_recovery_playbook_catalog()


@router.get("/chat/admin/recovery-guards", response_model=RecoveryGuardReport)
async def get_recovery_guard_report(
    window_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """What the guard layer would do for this customer right now, and why.

    Returns two evaluations of the same plan: ``evaluation`` with enforcement
    off (what the guards *would* say) and ``enforced_evaluation`` with it on
    (what would actually happen). Seeing both is the point -- an operator asking
    "why did nothing run" needs the difference between "a guard blocked this" and
    "nothing matched", and that difference is only visible when the guards are
    shown without suppressing what they would have allowed.

    Also carries the static governance audit, so an operator checking the limits
    does not have to make a second call.
    """
    return await build_recovery_guard_report_for_user(db, current_user.id, window_days)


@router.get("/chat/admin/recovery-analytics", response_model=RecoveryActionAnalyticsReport)
async def get_recovery_action_analytics(
    window_days: int = Query(default=7, ge=1, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Rollup of this customer's recovery action history over a window.

    ``guarded_ratio`` and ``budget_utilisation`` are the two numbers that matter:
    per-customer, a repeatedly re-credited goodwill balance reads as one
    generous action, and only the aggregate shows the spend was never bounded.
    """
    return await build_recovery_action_analytics_for_user(db, current_user.id, window_days)


@router.get("/chat/admin/recovery-outreach-plan", response_model=RecoveryOutreachPlan)
async def get_recovery_outreach_plan(
    window_days: int = Query(default=30, ge=7, le=365),
    locale: str = Query(default="global"),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Composed outreach preview: strategy, callback, incentive, review priority.

    Issues nothing. Every sub-plan is marked undispatched/unissued/unqueued, so
    the payload is safe to forward for approval.
    """
    return await build_recovery_outreach_plan_for_user(
        db, current_user.id, window_days, locale=locale
    )


@router.post("/chat/recovery/playbooks", response_model=RecoveryPlaybookRunReport)
async def run_user_recovery_playbooks(
    payload: RecoveryPlaybookRunRequest,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Auto-execute recovery playbooks against the user's realtime context.

    Computes realtime dissatisfaction indicators from the recent interaction
    window, matches them against the config-driven playbook table, and triggers
    the matched actions (credit points / escalate ticket / policy guardrail).
    Pass ``dry_run=true`` to preview matches without writing anything.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=payload.window_days)
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
    chat_rows = (await db.execute(chat_query)).scalars().all()
    bookings = (await db.execute(booking_query)).scalars().all()
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    policy_snapshot = await build_customer_policy_snapshot(db, current_user)
    return await run_recovery_playbooks(
        db,
        current_user.id,
        chat_rows=list(chat_rows),
        bookings=list(bookings),
        sentiment=latest_sentiment,
        window_days=payload.window_days,
        dry_run=payload.dry_run,
        policy_snapshot=policy_snapshot,
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


@router.get("/chat/activity-tree", response_model=ActivityTreeReport)
async def get_activity_tree_self(
    window_days: int = Query(default=30, ge=7, le=365),
    rank_by: str = Query(default="recency", pattern="^(recency|score)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=10, ge=1, le=50),
    kind: Optional[str] = Query(default=None, pattern="^(chat|booking)$"),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await build_self_activity_tree(
        db, current_user.id, window_days, rank_by=rank_by, order=order, limit=limit, kind=kind
    )


@router.get("/chat/admin/activity-tree", response_model=ActivityTreeReport)
async def get_activity_tree_admin(
    window_days: int = Query(default=30, ge=7, le=365),
    group_by: str = Query(
        default="lifecycle_stage",
        # Kept in step with ACTIVITY_TREE_GROUP_AXES. The pattern had not been
        # widened when `sentiment_range` and `engagement_score` were added to
        # that table, so both were reachable from the catalog and from the
        # engine but rejected with a 422 by the route -- configured but
        # unreachable, which is the worst state for a config-driven axis.
        pattern="^(lifecycle_stage|value_tier|customer_classification|churn_risk|journey_family|sentiment_range|engagement_score)$",
    ),
    rank_by: str = Query(
        default="loyalty_score",
        pattern="^(loyalty_score|monetization_readiness|signal_strength|churn_risk_score|activity_count)$",
    ),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=5, ge=1, le=50),
    min_loyalty: Optional[float] = Query(default=None, ge=0, le=100),
    max_loyalty: Optional[float] = Query(default=None, ge=0, le=100),
    churn_risk: Optional[str] = Query(default=None, pattern="^(low|medium|high)$"),
    sentiment: Optional[str] = Query(default=None, pattern="^(positive|negative|neutral|none)$"),
    anomalies_only: bool = Query(default=False),
    with_activities: bool = Query(default=False),
    activity_kind: Optional[str] = Query(default=None, pattern="^(chat|booking)$"),
    q: Optional[str] = Query(default=None, max_length=120),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await build_activity_tree(
        db,
        window_days,
        group_by=group_by,
        rank_by=rank_by,
        order=order,
        limit=limit,
        min_loyalty=min_loyalty,
        max_loyalty=max_loyalty,
        churn_risk=churn_risk,
        sentiment=sentiment,
        q=q,
        anomalies_only=anomalies_only,
        with_activities=with_activities,
        activity_kind=activity_kind,
    )


@router.get("/chat/communication-strategy", response_model=CommunicationStrategyResult)
async def get_communication_strategy_self(
    window_days: int = Query(default=30, ge=7, le=365),
    locale: Optional[str] = Query(default="global", max_length=16),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await resolve_communication_strategy_for_user(
        db,
        current_user.id,
        window_days,
        locale=locale,
        user_name=getattr(current_user, "username", ""),
    )


@router.get("/chat/admin/communication-strategy", response_model=CommunicationAdminStrategyReport)
async def get_communication_strategy_admin(
    window_days: int = Query(default=30, ge=7, le=365),
    limit: int = Query(default=20, ge=1, le=200),
    locale: Optional[str] = Query(default="global", max_length=16),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await build_communication_strategy_admin_report(
        db, window_days, limit, locale=locale
    )


@router.get("/chat/admin/communication-overrides", response_model=CommunicationOverrideReport)
async def list_communication_overrides_admin(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await list_communication_admin_overrides(db, limit)


@router.post("/chat/admin/communication-overrides", response_model=CommunicationAdminOverrideItem)
async def set_communication_override_admin(
    payload: CommunicationOverrideCreate,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    try:
        return await set_communication_admin_override(
            db,
            payload.user_id,
            payload.profile_id,
            getattr(current_user, "id", 0),
            payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.delete("/chat/admin/communication-overrides/{user_id}", response_model=CommunicationOverrideReport)
async def delete_communication_override_admin(
    user_id: int,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    removed = await delete_communication_admin_override(db, user_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No communication override exists for this user",
        )
    return await list_communication_admin_overrides(db, 50)


# ---------------------------------------------------------------------------
# Arrears payments (pay in arrears, policy-selected interest)
# ---------------------------------------------------------------------------


@router.get("/chat/payments/arrears/quote", response_model=ArrearsQuote)
async def quote_arrears_self(
    principal: float = Query(gt=0),
    defer_days: int = Query(default=30, ge=1, le=365),
    service_type: str = Query(default="consultation", max_length=50),
    currency: str = Query(default="USD", max_length=8),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await quote_arrears_for_user(
        db,
        current_user.id,
        principal,
        defer_days,
        service_type=service_type,
        currency=currency,
    )


@router.get("/chat/payments/arrears", response_model=ArrearsListReport)
async def list_user_arrears_self(
    status: Optional[str] = Query(default=None, pattern="^(open|settled|waived)$"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await list_user_arrears(db, current_user.id, status_filter=status, limit=limit)


@router.post("/chat/payments/arrears", response_model=ArrearsEntryOut)
async def open_arrears_self(
    payload: ArrearsOpenRequest,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    try:
        return await open_arrears_payment(
            db,
            current_user.id,
            payload.principal,
            payload.defer_days,
            service_type=payload.service_type,
            booking_id=payload.booking_id,
            reference=payload.reference,
            currency=payload.currency,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/chat/admin/payments/arrears", response_model=ArrearsAdminReport)
async def arrears_admin_report(
    status: Optional[str] = Query(default=None, pattern="^(open|settled|waived)$"),
    window_days: int = Query(default=30, ge=7, le=365),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await build_arrears_admin_report(
        db, status_filter=status, window_days=window_days, limit=limit
    )


@router.post("/chat/admin/payments/arrears/{entry_id}/settle", response_model=ArrearsSettleResult)
async def settle_arrears_admin(
    entry_id: int,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    try:
        result = await settle_arrears_entry(db, entry_id, settled_by=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Arrears entry not found")
    return result


@router.post("/chat/admin/payments/arrears/{entry_id}/waive-interest", response_model=ArrearsWaiveResult)
async def waive_arrears_admin(
    entry_id: int,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    try:
        result = await waive_arrears_interest(db, entry_id, waived_by=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Arrears entry not found")
    return result


# ---------------------------------------------------------------------------
# Points <-> money/currency exchange
# ---------------------------------------------------------------------------


@router.get("/chat/points/exchange/rates", response_model=PointsExchangeRates)
async def points_exchange_rates_self(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return build_points_exchange_rates()


@router.get("/chat/points/wallet", response_model=PointsWalletReport)
async def points_wallet_self(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await list_user_wallets(db, current_user.id)


@router.get("/chat/points/transactions", response_model=PointsTransactionsReport)
async def points_transactions_self(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await list_user_point_transactions(db, current_user.id, limit=limit)


@router.post("/chat/points/exchange/quote", response_model=PointsExchangeQuote)
async def points_exchange_quote_self(
    payload: PointsExchangeQuoteRequest,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    return await quote_points_exchange_for_user(
        db,
        current_user.id,
        payload.point_type,
        payload.direction,
        payload.amount,
        payload.currency,
    )


@router.post("/chat/points/exchange", response_model=PointsExchangeResult)
async def points_exchange_execute_self(
    payload: PointsExchangeRequest,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    try:
        return await execute_points_exchange_for_user(
            db,
            current_user.id,
            payload.point_type,
            payload.direction,
            payload.amount,
            payload.currency,
            reference=payload.reference,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/chat/admin/points/exchange", response_model=PointsAdminReport)
async def points_exchange_admin_report(
    window_days: int = Query(default=30, ge=7, le=365),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    return await build_points_exchange_admin_report(db, window_days=window_days, limit=limit)


# ---------------------------------------------------------------------------
# Stage A -- Trust & Visibility
# ---------------------------------------------------------------------------
#
# Stage A answers one question from the customer's side: "can I see what was
# done for me and why?" Five surfaces, each an aggregation of engines that
# already existed rather than a new source of truth:
#
#   /chat/customer-360            everything known, in one payload
#   /chat/me/recovery-status      what recovery decided and what it did
#   /chat/me/status               recovery + points forecast + policy posture
#   /chat/me/preferences          the preference & consent centre
#   /chat/me/explanations         the decisions about you, in plain language
#
# Every one is self-scoped. The admin equivalents are explicitly separate
# routes, so widening a self route to take a `user_id` is never an option and
# does not have to be reviewed as one.


@router.get("/chat/customer-360", response_model=Customer360Report)
async def get_customer_360(
    window_days: int = Query(default=30, ge=1, le=365),
    section: Optional[str] = Query(
        default=None,
        description=(
            "restrict the build to one section; comma-separated for several. "
            "An unknown section is a 422 rather than an empty payload."
        ),
        max_length=300,
    ),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Everything known about the signed-in customer, in one payload.

    This aggregates eight existing engines and stores nothing. A section that
    cannot be built is reported in the summary as incomplete rather than
    defaulted to a healthy value, so a failed payments query does not read as
    "no open payments".
    """
    sections = None
    if section:
        sections = [part.strip() for part in section.split(",") if part.strip()]
    try:
        return await build_customer_360(
            db, current_user.id, window_days, sections=sections
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("/chat/admin/customer-360")
async def get_customer_360_admin(
    window_days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Cross-user rollup from aggregate rows.

    Deliberately *not* N per-user 360 builds: that is O(users x 8 engines) with
    a model call per user in the sentiment section. The payload lists what it
    omits so an operator is not left reading absence of a signal as absence in
    the data.
    """
    return await build_customer_360_admin_report(db, window_days=window_days, limit=limit)


@router.get("/chat/me/recovery-status", response_model=CustomerRecoveryStatus)
async def get_own_recovery_status(
    window_days: int = Query(default=30, ge=1, le=365),
    include_preview: bool = Query(
        default=False,
        description="include the actions a run right now would take; off by default so a history read never counts unrun work",
    ),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """What automated recovery decided for this customer, and what it did.

    Includes the actions that were *deliberately not repeated* and names the
    guard that stopped them. Omitting those would make "our limits correctly
    prevented a duplicate credit" and "we forgot about you" render identically.

    Honours the `show_recovery_activity` preference, which defaults to on: a
    transparency feature hidden by default is only discoverable after the
    incident it would have explained.
    """
    return await build_customer_recovery_status(
        db, current_user.id, window_days, include_preview=include_preview
    )


@router.get("/chat/me/status", response_model=SelfServiceStatusReport)
async def get_own_status(
    window_days: int = Query(default=30, ge=1, le=365),
    include_preview: bool = Query(default=False),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """One dashboard: recovery status, points forecast, policy posture, journey.

    Each section is built by the same function its dedicated endpoint uses, so
    the two cannot disagree. A section that fails appears under `degraded` with
    the reason rather than being replaced with a plausible default.
    """
    return await build_self_service_status(
        db, current_user.id, window_days, include_preview=include_preview
    )


@router.get("/chat/me/points-forecast")
async def get_own_points_forecast(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Project the next 30 days of points movement from the last 30 days of it.

    `is_projection` is always true and `basis` states the arithmetic. A
    forecast on a surface a customer might act on that turned out to be a
    model prediction would be worse than no forecast at all.
    """
    return await build_points_forecast(db, current_user.id)


@router.get("/chat/me/policy-posture", response_model=PolicyPostureSummary)
async def get_own_policy_posture(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Tier, posture and access band, with what each one gates.

    Reads the stored policy score rather than recomputing it. A recomputed
    preview would be a second implementation of the score, and the two would
    disagree; with no score on record this reports that instead of inventing a
    default.
    """
    return await build_policy_posture_summary(db, current_user.id)


@router.get("/chat/me/preferences", response_model=PreferenceConsentReport)
async def get_own_preferences(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """The preference & consent centre, with every declared key and purpose.

    A key the customer has never set is reported with `set: false` and its
    shipped default, which is a different state from a key set to that same
    default. The resolver needs to tell them apart to know whether to fall
    through to its own ladder.
    """
    return await build_preference_consent_report(db, current_user.id)


@router.put("/chat/me/preferences", response_model=PreferenceUpdateResponse)
async def update_own_preferences(
    payload: PreferenceUpdateRequest,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Update preferences and/or consent grants.

    Partial success is reported, not rejected: a valid key and an unknown one in
    the same request both land and the response names which. A batch that
    validated all-or-nothing would make the settings screen brittle in a way
    the customer cannot fix.

    A `required` purpose cannot be withdrawn and is returned under
    `blocked_consents` with the reason. Withdrawing the basis needed to run the
    contract is withdrawing from the service, and the honest response to that
    is a delete request, not a silent switch.
    """
    return await update_user_preferences(
        db,
        current_user.id,
        payload.preferences,
        payload.consents,
        recorded_by_id=current_user.id,
    )


@router.get("/chat/me/consent-history")
async def get_own_consent_history(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """The append-only consent trail: every grant and every revocation.

    The profile row is overwritten on every edit, so it can show the current
    state but not when consent was first given. That question is what this
    trail exists to answer, and a re-confirmation that submits the value
    already stored deliberately does *not* append a row -- otherwise the log
    could not distinguish a fresh grant from a no-op.
    """
    return await list_consent_events(db, current_user.id, limit=limit)


@router.get("/chat/me/explanations")
async def get_own_explanations(
    window_days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """The decisions made about this customer, each with its arithmetic.

    Covers loyalty score, churn risk, recovery readiness, policy posture, value
    tier and lifecycle stage. A second, business vocabulary over
    `/meta/decisions/explanations`, which narrates infrastructure decisions;
    they are complementary rather than competing.

    `deduction_reproduced` reports whether the visible factors add up to the
    score on their own. It is a checkable claim rather than a reassurance, and
    it is false when the score carries a term this surface does not see.
    """
    chat_rows, bookings = await load_user_interaction_window(
        db, current_user.id, window_days
    )
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = build_summary(current_user.id, chat_rows, bookings, latest_sentiment)
    churn = await build_churn_prediction(db, current_user.id, window_days)

    loyalty = explain_loyalty_score(summary)
    churn_block = explain_churn_risk(summary, churn)
    tier = explain_value_tier(summary)

    stage, confidence, drivers, _focus = classify_lifecycle_stage(
        summary, churn, len(chat_rows), len(bookings)
    )
    from app.services.customer_explain import explain_lifecycle_stage

    posture_block = None
    try:
        policy_snapshot = await build_customer_policy_snapshot(db, current_user)
        posture_block = explain_policy_posture(policy_snapshot)
    except Exception as exc:  # noqa: BLE001
        posture_block = {
            "subject": "policy_posture",
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
        }

    recovery_block = None
    try:
        from app.services.recovery_playbooks import _load_recovery_context

        resolved = await _load_recovery_context(db, current_user.id, window_days)
        recovery_block = explain_recovery_status(resolved["context"])
    except Exception as exc:  # noqa: BLE001
        recovery_block = {
            "subject": "recovery_readiness",
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
        }

    explanations = [
        loyalty,
        churn_block,
        recovery_block,
        tier,
        explain_lifecycle_stage(stage, drivers=drivers),
    ]
    if posture_block is not None:
        explanations.append(posture_block)

    available = [
        block for block in explanations if block.get("status") != "unavailable"
    ]
    return {
        "generated_at": datetime.now(timezone.utc),
        "user_id": current_user.id,
        "window_days": window_days,
        "explanations": available,
        "unavailable": [
            {"subject": block.get("subject"), "reason": block.get("reason")}
            for block in explanations
            if block.get("status") == "unavailable"
        ],
        "next_actions": loyalty.get("next_steps", []),
        "summary": (
            f"{len(available)} decision(s) explained in plain language with their "
            "arithmetic; scores are banded because a bare 0-100 number implies a "
            "precision the score does not have, and the number travels alongside "
            "the band"
        ),
    }


@router.get("/chat/admin/explanation-vocabulary")
async def get_explanation_vocabulary(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The plain-language phrase and band tables, plus their validation report.

    `validate_explanation_tables` is report-only, like the i18n and audit-contract
    passes: these phrases are already being served to customers, so correcting
    one changes a string someone is reading. The interesting output is a
    non-empty finding list -- which phrase exists for a value the producing
    engine cannot emit, or which next-step condition can never be met.
    """
    return {
        "catalog": build_explainability_vocabulary_catalog(),
        "validation": validate_explanation_tables(),
    }


@router.get("/chat/admin/preference-catalog")
async def get_preference_catalog(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The declared preference key space and consent purposes.

    The same payload a customer receives, exposed read-only for an operator who
    needs to know what the centre can express before asking a customer to use
    it. Adding a key is a table edit; no migration is involved because both
    value columns are JSON blobs.
    """
    return preference_catalog()