from datetime import datetime, timezone, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.services.chat_analytics import (
    analyze_sentiment,
    build_churn_prediction,
    build_retention_snapshot_summary as _unused,
    build_summary,
    load_user_interaction_window,
    build_retention_snapshot_operations_report,
)
from app.schemas.chat import (
    RetentionDashboard,
    RetentionSnapshotDelta,
    RetentionSnapshotItem,
    RetentionSnapshotReport,
    RetentionSnapshotTrendItem,
    RetentionSnapshotTrendReport,
    RetentionSnapshotOperationsReport,
)


async def prune_retention_snapshots(db: AsyncSession, user_id: int, window_days: int, keep: int = 20) -> None:
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


async def prune_and_report_retention_snapshots(
    db: AsyncSession,
    user_id: int,
    window_days: int,
    keep: int = 20,
) -> dict[str, int | datetime]:
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


async def build_retention_snapshot_report(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotReport:
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


async def build_retention_snapshot_delta(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotDelta:
    report = await build_retention_snapshot_report(db, user_id, window_days)
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
        churn_risk_delta=f"{previous.churn_risk if previous else current.churn_risk}->{current.churn_risk}",
        lifecycle_stage_delta=f"{previous.lifecycle_stage if previous else current.lifecycle_stage}->{current.lifecycle_stage}",
        churn_risk_changed=bool(previous and previous.churn_risk != current.churn_risk),
        lifecycle_stage_changed=bool(previous and previous.lifecycle_stage != current.lifecycle_stage),
        previous_created_at=previous.created_at if previous else None,
        current_created_at=current.created_at,
        generated_at=datetime.now(timezone.utc),
    )


async def build_retention_snapshot_trends(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotTrendReport:
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


async def build_retention_dashboard(db: AsyncSession, user_id: int, window_days: int) -> RetentionDashboard:
    chat_rows, bookings = await load_user_interaction_window(db, user_id, window_days)
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
    churn_prediction = await build_churn_prediction(db, user_id, window_days)
    snapshot_report = await build_retention_snapshot_report(db, user_id, window_days)
    snapshot_delta = await build_retention_snapshot_delta(db, user_id, window_days)
    snapshot_trends = await build_retention_snapshot_trends(db, user_id, window_days)
    snapshot_operations_report = await build_retention_snapshot_operations_report(db, window_days)
    trend_coverage = round(len(snapshot_trends.trends) / max(1, len(snapshot_report.snapshots)), 2)
    delta_coverage = 1.0 if snapshot_delta.current_snapshot_id is not None else 0.0
    return RetentionDashboard(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        summary=summary,
        churn_prediction=churn_prediction,
        snapshot_report=snapshot_report,
        snapshot_delta=snapshot_delta,
        snapshot_trends=snapshot_trends,
        snapshot_operations_report=snapshot_operations_report,
        trend_coverage=trend_coverage,
        delta_coverage=delta_coverage,
    )
