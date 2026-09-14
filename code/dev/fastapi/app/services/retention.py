from datetime import datetime, timezone, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas import chat as chat_schemas
from app.schemas.chat import (
    RetentionCoverageItem,
    RetentionCoverageReport,
    RetentionMaintenancePreview,
    RetentionMaintenancePreviewItem,
    RetentionOperationalItem,
    RetentionOperationalReport,
    RetentionSnapshotDelta,
    RetentionSnapshotItem,
    RetentionSnapshotReport,
    RetentionSnapshotTrendItem,
    RetentionSnapshotTrendReport,
    RetentionDashboard,
)
from app.services.chat_analytics import (
    analyze_sentiment,
    build_churn_prediction,
    build_retention_snapshot_operations_report,
    build_summary,
    load_user_interaction_window,
)
from app.services.topics import (
    TOPIC_CATALOG,
    TOPIC_SECTORS,
    TOPIC_THEME_GROUPS,
    build_topic_coverage_report,
    build_topic_intelligence_report,
    build_topic_portfolio_report,
    build_topic_theme_coverage,
)


def _retention_topic_context_from_summary(summary_text: str) -> list[str]:
    normalized = (summary_text or "").lower()
    topic_hints = {
        "booking status and confirmations": ["booking", "confirm", "status", "appointment"],
        "booking rescheduling and changes": ["reschedule", "change", "move", "update"],
        "cancellations and refunds": ["cancel", "refund", "reverse", "void"],
        "service quality and follow-up": ["quality", "follow up", "feedback", "issue"],
        "support escalation and handoff": ["escalate", "handoff", "urgent", "manager"],
        "customer sentiment and recovery": ["sentiment", "angry", "frustrated", "recover"],
        "FAQ and self-service guidance": ["how to", "faq", "help", "guide"],
        "routing and service assignment": ["route", "assign", "room", "match"],
        "service status and progress updates": ["progress", "status", "update", "where"],
        "issue reproduction and troubleshooting": ["reproduce", "troubleshoot", "steps", "diagnose"],
        "billing and payment questions": ["bill", "payment", "invoice", "charge"],
        "account access and profile help": ["account", "login", "password", "profile"],
        "queue status and response timing": ["queue", "wait", "response", "timing"],
        "service appointment preparation": ["prepare", "prep", "ready", "expect"],
        "same-day rescheduling and urgent changes": ["same day", "urgent", "today", "asap"],
        "no-show prevention and follow-up": ["no show", "missed", "remind", "follow up"],
        "routing and service assignment": ["route", "assign", "room", "match"],
        "service status and progress updates": ["progress", "status", "update", "where"],
        "handoff readiness and escalation context": ["handoff", "context", "escalation", "agent"],
        "service eligibility and requirements": ["eligible", "requirement", "qualify", "criteria"],
        "address and location details": ["address", "location", "site", "direction"],
        "arrival timing and eta updates": ["eta", "arrival", "when", "time"],
        "service exceptions and edge cases": ["exception", "special", "edge", "custom"],
        "workflow automation and task routing": ["automation", "workflow", "queue", "routing"],
        "room assignment and resource matching": ["room", "assignment", "resource", "match"],
        "capacity planning and slot allocation": ["capacity", "slot", "allocation", "demand"],
        "customer onboarding and first-time guidance": ["onboarding", "first time", "getting started", "setup"],
        "service preferences and customization": ["preference", "custom", "tailor", "recurring"],
        "accessibility and assistance needs": ["accessibility", "assistance", "accommodation", "support"],
        "policy explanation and entitlement review": ["policy", "entitlement", "rule", "explain"],
        "issue reproduction and troubleshooting": ["reproduce", "troubleshoot", "steps", "diagnose"],
        "billing disputes and charge review": ["dispute", "charge", "billing", "review"],
        "service follow-up and resolution tracking": ["follow-up", "resolution", "callback", "closed"],
        "customer feedback and survey response": ["survey", "feedback", "rate", "review"],
        "operational readiness and staffing coverage": ["staffing", "coverage", "readiness", "shift"],
    }
    matched = [topic for topic, keywords in topic_hints.items() if any(keyword in normalized for keyword in keywords)]
    return matched


def build_retention_topic_signal_report(summary_text: str, user_id: int, window_days: int) -> dict[str, object]:
    matched_topics = _retention_topic_context_from_summary(summary_text)
    topic_portfolio = build_topic_portfolio_report(type("RetentionTopicSelection", (), {"topic": summary_text})())
    topic_intelligence = build_topic_intelligence_report(type("RetentionTopicSelection", (), {"topic": summary_text})())
    topic_theme_coverage = build_topic_theme_coverage(type("RetentionTopicSelection", (), {"topic": summary_text})())
    matched_keywords = list(topic_intelligence.matched_keywords)
    matched_themes = [item["theme"] for item in topic_theme_coverage if item["matched_count"]]
    catalog_topics = {item["topic"] for item in TOPIC_CATALOG}
    matched_topic_details = []
    items = []
    for topic in matched_topics[:8]:
        topic_portfolio = build_topic_portfolio_report(type("RetentionTopicSelection", (), {"topic": topic})())
        topic_theme_matches = [theme["theme"] for theme in topic_portfolio["theme_coverage"] if theme.get("coverage", 0.0) >= 0.0]
        matched_topic_details.append({"topic": topic, "themes": topic_theme_matches, "coverage_ratio": topic_portfolio["coverage_ratio"]})
        items.append(
            {
                "topic": topic,
                "matched_keywords": [keyword for keyword in matched_keywords if keyword in topic.lower() or keyword in (summary_text or "").lower()],
                "matched_themes": topic_theme_matches,
                "matched_sectors": [
                    sector["sector"]
                    for sector in TOPIC_SECTORS
                    if topic in sector["topics"]
                ] or (["retention_operations"] if topic_portfolio["matched_topics"] else []),
                "coverage_score": round(
                    min(
                        1.0,
                        topic_portfolio["coverage_ratio"]
                        + (len(matched_keywords) * 0.05)
                        + (len(matched_themes) * 0.03),
                    ),
                    2,
                ),
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc),
        "user_id": user_id,
        "window_days": window_days,
        "topic_context": summary_text,
        "dominant_topic": matched_topics[0] if matched_topics else None,
        "matched_themes": matched_themes,
        "matched_topic_details": matched_topic_details,
        "topic_catalog_size": len(catalog_topics),
        "topic_portfolio_coverage": topic_portfolio["coverage_ratio"],
        "topic_focus": matched_topics[:5],
        "topic_signal_depth": f"topics={len(matched_topics)}, themes={len(matched_themes)}, keywords={len(matched_keywords)}",
        "items": items,
        "summary": f"Topic context '{summary_text}' matched {len(matched_topics)} retention-relevant topics across {len(matched_themes)} themes with {len(matched_keywords)} keywords.",
    }


def _retention_snapshot_query(user_id: int, cutoff: datetime):
    return (
        select(models.RetentionSnapshot)
        .where(models.RetentionSnapshot.user_id == user_id)
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .order_by(desc(models.RetentionSnapshot.created_at))
    )


async def _load_retention_snapshots(db: AsyncSession, user_id: int, window_days: int):
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    result = await db.execute(_retention_snapshot_query(user_id, cutoff))
    return result.scalars().all()


async def prune_retention_snapshots(db: AsyncSession, user_id: int, window_days: int, keep: int = 20) -> None:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    for snapshot in snapshots[keep:]:
        await db.delete(snapshot)
    if len(snapshots) > keep:
        await db.commit()


async def count_retention_snapshots(db: AsyncSession, user_id: int, window_days: int) -> int:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    return len(snapshots)


async def count_retention_snapshots_by_type(db: AsyncSession, user_id: int, window_days: int) -> dict[str, int]:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    counts: dict[str, int] = {}
    for snapshot in snapshots:
        counts[snapshot.snapshot_type] = counts.get(snapshot.snapshot_type, 0) + 1
    return counts


async def build_retention_snapshot_summary(db: AsyncSession, user_id: int, window_days: int) -> chat_schemas.RetentionHealthReport:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    if not snapshots:
        return chat_schemas.RetentionHealthReport(
            generated_at=datetime.now(timezone.utc),
            user_id=user_id,
            window_days=window_days,
            total_snapshots=0,
            average_loyalty_score=0.0,
            average_churn_risk_score=0.0,
            counts_by_type=[],
            dominant_snapshot_type=None,
            coverage=0.0,
            snapshot_report=RetentionSnapshotReport(
                generated_at=datetime.now(timezone.utc),
                window_days=window_days,
                snapshots=[],
            ),
            trend_report=RetentionSnapshotTrendReport(
                generated_at=datetime.now(timezone.utc),
                window_days=window_days,
                trends=[],
            ),
        )

    counts_by_type: dict[str, int] = {}
    loyalty_scores = [snapshot.loyalty_score for snapshot in snapshots]
    churn_scores = [{"low": 0.0, "medium": 1.0, "high": 2.0, "critical": 3.0}.get(snapshot.churn_risk, 0.0) for snapshot in snapshots]
    for snapshot in snapshots:
        counts_by_type[snapshot.snapshot_type] = counts_by_type.get(snapshot.snapshot_type, 0) + 1

    dominant_snapshot_type = max(counts_by_type, key=counts_by_type.get) if counts_by_type else None
    snapshot_report = RetentionSnapshotReport(
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
    trend_report = RetentionSnapshotTrendReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        trends=[],
    )
    return chat_schemas.RetentionHealthReport(
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        window_days=window_days,
        total_snapshots=len(snapshots),
        average_loyalty_score=round(sum(loyalty_scores) / max(len(loyalty_scores), 1), 2),
        average_churn_risk_score=round(sum(churn_scores) / max(len(churn_scores), 1), 2),
        counts_by_type=[
            chat_schemas.RetentionHealthTypeCount(snapshot_type=snapshot_type, count=count)
            for snapshot_type, count in sorted(counts_by_type.items(), key=lambda item: item[0])
        ],
        dominant_snapshot_type=dominant_snapshot_type,
        coverage=0.0,
        snapshot_report=snapshot_report,
        trend_report=trend_report,
    )


async def build_retention_snapshot_report(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotReport:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
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
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    current_snapshot = snapshots[0] if snapshots else None
    previous_snapshot = snapshots[1] if len(snapshots) > 1 else None

    if current_snapshot is None:
        return RetentionSnapshotDelta(
            user_id=user_id,
            window_days=window_days,
            previous_snapshot_id=None,
            current_snapshot_id=None,
            loyalty_score_delta=0.0,
            churn_risk_delta="none",
            lifecycle_stage_delta="none",
            churn_risk_changed=False,
            lifecycle_stage_changed=False,
            previous_created_at=None,
            current_created_at=None,
            generated_at=datetime.now(timezone.utc),
        )

    churn_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    loyalty_delta = current_snapshot.loyalty_score - (previous_snapshot.loyalty_score if previous_snapshot else current_snapshot.loyalty_score)
    current_churn_rank = churn_rank.get(current_snapshot.churn_risk, 0)
    previous_churn_rank = churn_rank.get(previous_snapshot.churn_risk, 0) if previous_snapshot else current_churn_rank
    current_stage = current_snapshot.lifecycle_stage or "unknown"
    previous_stage = previous_snapshot.lifecycle_stage if previous_snapshot else current_stage

    return RetentionSnapshotDelta(
        user_id=user_id,
        window_days=window_days,
        previous_snapshot_id=previous_snapshot.id if previous_snapshot else None,
        current_snapshot_id=current_snapshot.id,
        loyalty_score_delta=round(loyalty_delta, 2),
        churn_risk_delta=str(current_churn_rank - previous_churn_rank),
        lifecycle_stage_delta=(f"{previous_stage} -> {current_stage}" if previous_stage != current_stage else "none"),
        churn_risk_changed=current_churn_rank != previous_churn_rank,
        lifecycle_stage_changed=previous_stage != current_stage,
        previous_created_at=previous_snapshot.created_at if previous_snapshot else None,
        current_created_at=current_snapshot.created_at,
        generated_at=datetime.now(timezone.utc),
    )


async def build_retention_snapshot_trends(db: AsyncSession, user_id: int, window_days: int) -> RetentionSnapshotTrendReport:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    churn_rank = {"low": 0.0, "medium": 1.0, "high": 2.0, "critical": 3.0}
    grouped: dict[str, list[models.RetentionSnapshot]] = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.snapshot_type, []).append(snapshot)

    trends = [
        RetentionSnapshotTrendItem(
            snapshot_type=snapshot_type,
            count=len(items),
            avg_loyalty_score=round(sum(item.loyalty_score for item in items) / max(len(items), 1), 2),
            avg_churn_risk_score=round(sum(churn_rank.get(item.churn_risk, 0.0) for item in items) / max(len(items), 1), 2),
            latest_created_at=max((item.created_at for item in items), default=None),
        )
        for snapshot_type, items in sorted(grouped.items())
    ]
    return RetentionSnapshotTrendReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        trends=trends,
    )


async def build_retention_dashboard(db: AsyncSession, user_id: int, window_days: int) -> RetentionDashboard:
    snapshot_report = await build_retention_snapshot_report(db, user_id, window_days)
    snapshot_delta = await build_retention_snapshot_delta(db, user_id, window_days)
    snapshot_trends = await build_retention_snapshot_trends(db, user_id, window_days)
    summary = await build_summary(db, user_id, window_days)
    churn_prediction = await build_churn_prediction(db, user_id, window_days)
    snapshot_operations_report = await build_retention_snapshot_operations_report(db, user_id, window_days)
    topic_signal_report = build_retention_topic_signal_report(summary.summary, user_id, window_days)

    return RetentionDashboard(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        summary=summary,
        churn_prediction=churn_prediction,
        snapshot_report=snapshot_report,
        snapshot_delta=snapshot_delta,
        snapshot_trends=snapshot_trends,
        snapshot_operations_report=snapshot_operations_report,
        topic_signal_report=topic_signal_report,
        trend_coverage=round(len(snapshot_trends.trends) / max(len(snapshot_report.snapshots), 1), 2),
        delta_coverage=1.0 if snapshot_report.snapshots else 0.0,
    )


async def build_retention_recommendations(db: AsyncSession, user_id: int, window_days: int) -> list[dict[str, object]]:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    if not snapshots:
        return [
            {
                "priority": "high",
                "area": "coverage",
                "recommendation": "Capture more retention snapshots before evaluating trends.",
                "evidence": "No snapshots were found in the selected window; topic coverage cannot be estimated yet.",
            }
        ]

    counts_by_type = await count_retention_snapshots_by_type(db, user_id, window_days)
    recommendations: list[dict[str, object]] = []
    dominant_type = max(counts_by_type, key=counts_by_type.get) if counts_by_type else None
    topic_portfolio = build_topic_portfolio_report(None)
    topic_theme_count = len(topic_portfolio["theme_coverage"])
    topic_catalog_count = len(TOPIC_CATALOG)

    if len(snapshots) < 5:
        recommendations.append(
            {
                "priority": "high",
                "area": "sample-size",
                "recommendation": "Collect more snapshots to make retention trends reliable and topic-aware.",
                "evidence": f"Only {len(snapshots)} snapshots are available across {len(counts_by_type)} types, {topic_theme_count} tracked themes, and {topic_catalog_count} catalog topics.",
            }
        )

    if dominant_type:
        recommendations.append(
            {
                "priority": "medium",
                "area": "dominant-type",
                "recommendation": f"Review the {dominant_type} snapshot path for repeated friction and topic clustering.",
                "evidence": f"{dominant_type} is the most common snapshot type in the window; topic portfolio coverage is {topic_portfolio['coverage_ratio']:.2f} across {len(topic_portfolio['theme_coverage'])} themes.",
            }
        )

    average_loyalty = sum(snapshot.loyalty_score for snapshot in snapshots) / max(len(snapshots), 1)
    average_churn = sum({"low": 0.0, "medium": 1.0, "high": 2.0, "critical": 3.0}.get(snapshot.churn_risk, 0.0) for snapshot in snapshots) / max(len(snapshots), 1)
    if average_churn >= 2.0:
        recommendations.append(
            {
                "priority": "high",
                "area": "churn-risk",
                "recommendation": "Trigger proactive recovery steps for the highest-risk users and route by topic.",
                "evidence": f"Average churn risk score is {round(average_churn, 2)} with topic catalog coverage at {topic_portfolio['coverage_ratio']:.2f} and {len(topic_portfolio['matched_topics'])} matched topics.",
            }
        )
    if average_loyalty < 50:
        recommendations.append(
            {
                "priority": "high",
                "area": "loyalty",
                "recommendation": "Add a retention recovery loop to improve loyalty signals and match topic themes.",
                "evidence": f"Average loyalty score is {round(average_loyalty, 2)}; theme coverage spans {topic_theme_count} groups and {len(topic_portfolio['matched_topics'])} matched topics.",
            }
        )

    return recommendations


async def build_retention_coverage_report(db: AsyncSession, user_id: int, window_days: int) -> RetentionCoverageReport:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    total = len(snapshots)
    snapshot_types = sorted({snapshot.snapshot_type for snapshot in snapshots})
    latest_snapshot = snapshots[0] if snapshots else None
    topic_selection = None
    if latest_snapshot and getattr(latest_snapshot, "summary_json", ""):
        topic_text = str(latest_snapshot.summary_json)
        topic_selection = type("RetentionTopicSelection", (), {"topic": topic_text})()
    topic_portfolio = build_topic_portfolio_report(topic_selection)
    topic_intelligence = build_topic_intelligence_report(topic_selection)
    items = [
        RetentionCoverageItem(
            label=snapshot_type,
            count=sum(1 for snapshot in snapshots if snapshot.snapshot_type == snapshot_type),
            ratio=round(sum(1 for snapshot in snapshots if snapshot.snapshot_type == snapshot_type) / max(total, 1), 2),
        )
        for snapshot_type in snapshot_types
    ]
    return RetentionCoverageReport(
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        window_days=window_days,
        total_snapshots=total,
        items=items,
        topic_coverage_ratio=topic_portfolio["coverage_ratio"],
        summary=(
            "Retention coverage is distributed across the available snapshot types."
            if items
            else "No retention snapshots available for the selected window."
        )
        + f" Topic coverage: {topic_portfolio['coverage_ratio']:.2f}; matched topics: {len(topic_intelligence.matched_keywords)}; themes: {len(topic_portfolio['theme_coverage'])}.",
    )


async def build_retention_operational_report(db: AsyncSession, user_id: int, window_days: int) -> RetentionOperationalReport:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    counts_by_type = await count_retention_snapshots_by_type(db, user_id, window_days)
    total = len(snapshots)
    dominant_type = max(counts_by_type, key=counts_by_type.get) if counts_by_type else None
    topic_portfolio = build_topic_portfolio_report(None)
    items = [
        RetentionOperationalItem(
            name="snapshot_volume",
            status="healthy" if total >= 5 else "watch",
            detail=f"{total} snapshots in the selected window across {len(counts_by_type)} snapshot types.",
            owner_hint="retention analytics",
        ),
        RetentionOperationalItem(
            name="dominant_snapshot_type",
            status="healthy" if dominant_type else "empty",
            detail=f"Most common type: {dominant_type or 'none'}; topic portfolio coverage is {topic_portfolio['coverage_ratio']:.2f}.",
            owner_hint="customer success",
        ),
        RetentionOperationalItem(
            name="trend_signal",
            status="healthy" if total >= 3 else "watch",
            detail=("Trend reporting has enough signal to support action." if total >= 3 else "Collect more data before relying on trend reporting.") + f" Theme coverage count: {len(topic_portfolio['theme_coverage'])}.",
            owner_hint="product analytics",
        ),
        RetentionOperationalItem(
            name="topic_coverage",
            status="healthy" if topic_portfolio["coverage_ratio"] >= 0.5 else "watch",
            detail=f"Topic coverage ratio is {topic_portfolio['coverage_ratio']:.2f} with {len(topic_portfolio['matched_topics'])} matched topics and {len(topic_portfolio['theme_coverage'])} theme buckets.",
            owner_hint="retention analytics",
        ),
    ]
    return RetentionOperationalReport(
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        window_days=window_days,
        items=items,
        summary=("Retention operations are stable enough for action." if total >= 3 else "Retention operations need more data before strong conclusions.") + f" Topic portfolio coverage is {topic_portfolio['coverage_ratio']:.2f} across {len(topic_portfolio['theme_coverage'])} themes.",
    )


async def build_retention_maintenance_preview(db: AsyncSession, user_id: int, window_days: int, keep: int = 20) -> dict[str, int | datetime | list[dict[str, object]]]:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    retained = snapshots[:keep]
    stale = snapshots[keep:]
    churn_by_type = {
        snapshot_type: sum(1 for item in stale if item.snapshot_type == snapshot_type)
        for snapshot_type in sorted({item.snapshot_type for item in snapshots})
    }
    return {
        "generated_at": datetime.now(timezone.utc),
        "user_id": user_id,
        "window_days": window_days,
        "keep": keep,
        "total_snapshots": len(snapshots),
        "retained_snapshots": len(retained),
        "stale_snapshots": len(stale),
        "retention_ratio": round(len(retained) / max(len(snapshots), 1), 2),
        "topic_coverage_ratio": build_topic_portfolio_report(None)["coverage_ratio"],
        "stale_by_type": [
            {"snapshot_type": snapshot_type, "count": count}
            for snapshot_type, count in sorted(churn_by_type.items())
        ],
    }


async def build_typed_retention_maintenance_preview(db: AsyncSession, user_id: int, window_days: int, keep: int = 20) -> RetentionMaintenancePreview:
    preview = await build_retention_maintenance_preview(db, user_id, window_days, keep=keep)
    return RetentionMaintenancePreview(
        generated_at=preview["generated_at"],
        user_id=user_id,
        window_days=window_days,
        keep=keep,
        total_snapshots=int(preview["total_snapshots"]),
        retained_snapshots=int(preview["retained_snapshots"]),
        stale_snapshots=int(preview["stale_snapshots"]),
        stale_by_type=[
            RetentionMaintenancePreviewItem(**item)
            for item in preview["stale_by_type"]
        ],
    )
