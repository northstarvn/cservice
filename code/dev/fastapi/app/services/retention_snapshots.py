from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas.chat import (
    RetentionSnapshotActionPlan,
    RetentionSnapshotAuditExport,
    RetentionSnapshotAuditItem,
    RetentionSnapshotAuditReport,
    RetentionSnapshotComparisonItem,
    RetentionSnapshotComparisonReport,
    RetentionSnapshotHealthRecommendation,
    RetentionSnapshotHealthRisk,
    RetentionSnapshotHealthScore,
    RetentionSnapshotHealthSummary,
    RetentionSnapshotMomentumItem,
    RetentionSnapshotMomentumReport,
    RetentionSnapshotOperationsAutomation,
    RetentionSnapshotOperationsCompliance,
    RetentionSnapshotOperationsExecutionState,
    RetentionSnapshotOperationsGoNoGo,
    RetentionSnapshotOperationsLaunchReadiness,
    RetentionSnapshotOperationsOverview,
    RetentionSnapshotOperationsPosture,
    RetentionSnapshotOperationsStatus,
    RetentionSnapshotRecommendation,
    RetentionSnapshotRiskProfile,
    RetentionSnapshotStalenessItem,
    RetentionSnapshotStalenessReport,
    RetentionSnapshotStalenessTrendItem,
    RetentionSnapshotStalenessTrendReport,
    RetentionSnapshotTypeBreakdownItem,
    RetentionSnapshotTypeBreakdownReport,
    RetentionSnapshotVolatilityItem,
    RetentionSnapshotVolatilityReport,
    RetentionSnapshotVolatilitySummary,
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
    fresh_query = (
        select(
            models.RetentionSnapshot.snapshot_type,
            func.count(models.RetentionSnapshot.id),
        )
        .where(models.RetentionSnapshot.created_at >= stale_cutoff)
        .where(models.RetentionSnapshot.created_at >= window_start)
        .group_by(models.RetentionSnapshot.snapshot_type)
    )
    rows = (await db.execute(stale_query)).all()
    fresh_rows = {}
    for row in (await db.execute(fresh_query)).all():
        snapshot_type = str(row[0])
        fresh_rows[snapshot_type] = int(row[1] or 0)
    items = [
        RetentionSnapshotStalenessItem(
            snapshot_type=str(snapshot_type),
            stale_snapshots=int(stale_snapshots or 0),
            fresh_snapshots=fresh_rows.get(str(snapshot_type), 0),
            staleness_rate=(int(stale_snapshots or 0) / max(1, int(stale_snapshots or 0) + fresh_rows.get(str(snapshot_type), 0))),
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
            fresh_snapshots=0,
            staleness_rate=1.0 if report.total_stale_snapshots else 0.0,
        ),
        RetentionSnapshotStalenessTrendItem(
            bucket="fresh",
            stale_snapshots=0,
            fresh_snapshots=sum(item.fresh_snapshots for item in report.items),
            staleness_rate=0.0,
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
        risk_level=overview_report.risk_level,
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
        risk_level=status_report.risk_level,
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
        risk_level=compliance_report.risk_level,
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
        automation=automation_ready,
        risk_level=posture_report.risk_level,
        overview=posture_report.overview,
    )


async def _build_retention_snapshot_operations_execution_state(db: AsyncSession, window_days: int, stale_after_days: int) -> RetentionSnapshotOperationsExecutionState:
    automation_report = await _build_retention_snapshot_operations_automation(db, window_days, stale_after_days)
    if automation_report.automation == "ready":
        execution_state = "go"
    elif automation_report.automation == "conditional":
        execution_state = "hold"
    else:
        execution_state = "block"
    return RetentionSnapshotOperationsExecutionState(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        execution_state=execution_state,
        risk_level=automation_report.risk_level,
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
        readiness=launch_readiness,
        risk_level=execution_report.risk_level,
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

