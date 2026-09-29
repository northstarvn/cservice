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

# --- Configurable operations policy --------------------------------------------
#
# Every threshold, weight, band, and canned sentence in this module used to be a
# literal buried in an ``if/elif`` chain, so retuning the operational posture
# ("treat 5+ as high volatility", "a score of 70 is still watch") meant editing
# twelve functions and hoping all twelve were updated. They now live in tables
# evaluated through one resolver, and every default below reproduces the previous
# literal exactly — the shipped policy is unchanged, it is simply data now.
#
# Band convention: ordered highest-threshold first, first match wins, and a value
# below every ``min`` takes the table's declared default. That makes a new band a
# one-line insert rather than a new branch, and it is why adding a level can never
# silently fall through to an unrelated fallback the way an ``elif`` chain can.

# Volatility classification of |current - previous| snapshot counts.
VOLATILITY_BANDS: list[dict[str, object]] = [
    {"classification": "high", "min": 3},
    {"classification": "moderate", "min": 1},
]
VOLATILITY_DEFAULT = "stable"
# The three classes the summary counts; adding a band without adding it here is
# reported by ``validate_snapshot_ops_policy`` rather than silently uncounted.
VOLATILITY_CLASSES: tuple[str, ...] = ("stable", "moderate", "high")

# How much each volatile class contributes to the risk score.
VOLATILITY_WEIGHTS: dict[str, int] = {"moderate": 2, "high": 4}

# Risk level from the weighted volatility score.
RISK_LEVEL_BANDS: list[dict[str, object]] = [
    {"risk_level": "critical", "min": 8},
    {"risk_level": "high", "min": 4},
    {"risk_level": "moderate", "min": 1},
]
RISK_LEVEL_DEFAULT = "low"

RECOMMENDATION_BY_RISK: dict[str, str] = {
    "critical": "Escalate retention outreach and review high-volatility snapshot types immediately.",
    "high": "Prioritize stabilization work and investigate the most volatile snapshot types.",
    "moderate": "Monitor snapshot trends closely and tighten the follow-up loop.",
    "low": "Maintain the current retention cadence and keep monitoring the baseline.",
}

ACTION_BY_RISK: dict[str, str] = {
    "critical": "Open incident review, assign owners, and start outreach within 24 hours.",
    "high": "Schedule a stabilization review and prepare a follow-up plan this week.",
    "moderate": "Review the top snapshot types and confirm monitoring thresholds.",
    "low": "Keep monitoring the current retention baseline and revisit next cycle.",
}

# Freshness health: 100 minus a fixed penalty per stale snapshot.
HEALTH_PENALTY_PER_STALE = 10
HEALTH_SCORE_FLOOR = 0
HEALTH_STATUS_BANDS: list[dict[str, object]] = [
    {"status": "healthy", "min": 80},
    {"status": "watch", "min": 50},
]
HEALTH_STATUS_DEFAULT = "critical"
HEALTH_STATUSES: tuple[str, ...] = ("healthy", "watch", "critical")

# Same score, read as blast radius rather than freshness.
HEALTH_RISK_BANDS: list[dict[str, object]] = [
    {"risk_level": "low", "min": 80},
    {"risk_level": "medium", "min": 50},
]
HEALTH_RISK_DEFAULT = "high"

HEALTH_RECOMMENDATION_BY_RISK: dict[str, str] = {
    "low": "Keep monitoring the current snapshot cadence.",
    "medium": "Review stale snapshot drivers and check for drift this week.",
    "high": "Escalate freshness review and assign immediate snapshot cleanup.",
}

# The operational ladder. Each stage is a pure relabelling of the previous one,
# so the whole chain is described by seven lookup tables rather than seven
# ``if/elif`` blocks; a stage can be retargeted, inserted, or removed as data.
OPERATIONS_OVERVIEW_BY_RISK: dict[str, str] = {
    "low": "Snapshot operations are healthy and need only routine monitoring.",
    "medium": "Snapshot operations need a targeted review to prevent drift.",
    "high": "Snapshot operations require immediate attention and cleanup.",
}
OPERATIONS_STATUS_BY_RISK: dict[str, str] = {
    "low": "operational",
    "medium": "watch",
    "high": "needs_attention",
}
COMPLIANCE_BY_STATUS: dict[str, str] = {
    "operational": "compliant",
    "watch": "review",
    "needs_attention": "non_compliant",
}
POSTURE_BY_COMPLIANCE: dict[str, str] = {
    "compliant": "stable",
    "review": "caution",
    "non_compliant": "escalate",
}
AUTOMATION_BY_POSTURE: dict[str, str] = {
    "stable": "ready",
    "caution": "conditional",
    "escalate": "not_ready",
}
EXECUTION_BY_AUTOMATION: dict[str, str] = {
    "ready": "go",
    "conditional": "hold",
    "not_ready": "block",
}
READINESS_BY_EXECUTION: dict[str, str] = {
    "go": "launch_ready",
    "hold": "launch_pending",
    "block": "launch_blocked",
}
DECISION_BY_READINESS: dict[str, str] = {
    "launch_ready": "go",
    "launch_pending": "hold",
    "launch_blocked": "block",
}

# Direction of a comparison delta, keyed by its sign.
MOMENTUM_DIRECTIONS: dict[str, str] = {"positive": "up", "negative": "down", "zero": "flat"}

# The operations ladder as an ordered declaration. Each stage relabels the
# previous stage's verdict, so the whole chain is a fold over this list rather
# than a chain of hand-written functions: inserting, removing, or reordering a
# stage is a data edit, and ``validate_snapshot_ops_policy`` can prove the chain
# is closed (every stage's input is produced by an earlier stage) instead of
# trusting that seven dicts line up.
SNAPSHOT_OPS_STAGES: list[dict[str, str]] = [
    {"stage": "status", "source": "risk_level", "table": "OPERATIONS_STATUS_BY_RISK"},
    {"stage": "compliance", "source": "status", "table": "COMPLIANCE_BY_STATUS"},
    {"stage": "posture", "source": "compliance", "table": "POSTURE_BY_COMPLIANCE"},
    {"stage": "automation", "source": "posture", "table": "AUTOMATION_BY_POSTURE"},
    {"stage": "execution_state", "source": "automation", "table": "EXECUTION_BY_AUTOMATION"},
    {"stage": "readiness", "source": "execution_state", "table": "READINESS_BY_EXECUTION"},
    {"stage": "decision", "source": "readiness", "table": "DECISION_BY_READINESS"},
]

# The stage tables, resolved by name, so the fold above is data-driven.
OPERATIONS_STAGE_TABLES: dict[str, dict[str, str]] = {
    "OPERATIONS_STATUS_BY_RISK": OPERATIONS_STATUS_BY_RISK,
    "COMPLIANCE_BY_STATUS": COMPLIANCE_BY_STATUS,
    "POSTURE_BY_COMPLIANCE": POSTURE_BY_COMPLIANCE,
    "AUTOMATION_BY_POSTURE": AUTOMATION_BY_POSTURE,
    "EXECUTION_BY_AUTOMATION": EXECUTION_BY_AUTOMATION,
    "READINESS_BY_EXECUTION": READINESS_BY_EXECUTION,
    "DECISION_BY_READINESS": DECISION_BY_READINESS,
}

# Every knob an operator may retune, with the default that reproduces today's
# behavior. ``kind`` is "bands" (replace a threshold ladder) or "scalar" (a
# number). Declaring them here is what lets ``snapshot_ops_sensitivity`` walk
# the tunables without a hand-written list that can fall out of date.
SNAPSHOT_OPS_TUNABLES: list[dict[str, object]] = [
    {"name": "volatility_bands", "kind": "bands", "default": VOLATILITY_BANDS,
     "affects": ["volatility", "risk", "recommendation", "action_plan"]},
    {"name": "volatility_weights", "kind": "mapping", "default": VOLATILITY_WEIGHTS,
     "affects": ["risk", "recommendation", "action_plan"]},
    {"name": "risk_level_bands", "kind": "bands", "default": RISK_LEVEL_BANDS,
     "affects": ["risk", "recommendation", "action_plan"]},
    {"name": "health_status_bands", "kind": "bands", "default": HEALTH_STATUS_BANDS,
     "affects": ["health", "operations"]},
    {"name": "health_risk_bands", "kind": "bands", "default": HEALTH_RISK_BANDS,
     "affects": ["health_risk", "operations"]},
    {"name": "health_penalty_per_stale", "kind": "scalar", "default": HEALTH_PENALTY_PER_STALE,
     "affects": ["health", "health_risk", "operations"]},
    {"name": "stale_after_days", "kind": "scalar", "default": 7,
     "affects": ["staleness", "health", "operations"]},
    {"name": "window_days", "kind": "scalar", "default": 30,
     "affects": ["comparison", "volatility", "staleness"]},
]


def resolve_band(
    value: float,
    bands: list[dict[str, object]],
    default: str,
    *,
    key: str = "min",
    label: str = "level",
) -> str:
    """First band whose ``min`` ``value`` reaches wins; else ``default``.

    Shared by every band table above so a band is evaluated the same way
    everywhere, and so an operator can rely on "insert a row, it takes effect"
    without each call site re-deriving the comparison.
    """
    for band in bands:
        if value >= float(band[key]):
            return str(band[label])
    return default


# --- The ladder, as one pure fold ---------------------------------------------
#
# Everything below ``health_risk`` is a pure relabelling, so it is computed once
# per (risk level, policy) and shared. Two consequences worth stating:
#
# 1. The twelve-deep chain no longer re-derives a verdict at every hop, so a
#    reported ``decision`` provably belongs to the same ``risk_level`` the
#    caller was shown.
# 2. Simulation and sensitivity analysis get the entire chain for free, with no
#    database round-trip, because the chain does not depend on the database.

_LADDER_CACHE: dict[tuple[str, str], dict[str, str]] = {}


def run_operations_ladder(risk_level: str, policy_id: str = "default") -> dict[str, str]:
    """Fold ``SNAPSHOT_OPS_STAGES`` from a health risk level to a decision.

    An unmapped input resolves to the empty string rather than raising: the
    ladder is advisory, and an advisory tool that hard-fails on an unrecognised
    level is a tool nobody runs. ``validate_snapshot_ops_policy`` is what
    actually reports the gap, and the caller can see the empty value.
    """
    cache_key = (risk_level, policy_id)
    cached = _LADDER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    resolved: dict[str, str] = {"risk_level": risk_level}
    for stage in SNAPSHOT_OPS_STAGES:
        source = resolved.get(stage["source"], "")
        resolved[stage["stage"]] = OPERATIONS_STAGE_TABLES[stage["table"]].get(source, "")
    _LADDER_CACHE[cache_key] = resolved
    return resolved


def classify_volatilities(
    volatilities: list[int], bands: list[dict[str, object]] | None = None
) -> dict[str, int]:
    """Bucket raw ``|current - previous|`` magnitudes into class counts.

    One classifier, shared by the async report and the pure evaluator, so a
    re-tuned band cannot mean two different things in the two code paths. The
    evaluator deliberately takes *magnitudes* rather than pre-bucketed counts:
    given counts it would have nothing left to classify, which previously made
    a ``volatility_bands`` override a silently ignored argument.
    """
    table = VOLATILITY_BANDS if bands is None else bands
    counts: dict[str, int] = {name: 0 for name in VOLATILITY_CLASSES}
    for value in volatilities:
        name = resolve_band(
            abs(int(value)), table, VOLATILITY_DEFAULT, key="min", label="classification"
        )
        counts[name] = counts.get(name, 0) + 1
    return counts


def resolve_health_score(total_stale: int, penalty_per_stale: int | None = None) -> int:
    """Freshness score: 100 minus a fixed penalty per stale snapshot."""
    penalty = HEALTH_PENALTY_PER_STALE if penalty_per_stale is None else int(penalty_per_stale)
    return max(HEALTH_SCORE_FLOOR, 100 - (int(total_stale) * penalty))


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


async def _build_retention_snapshot_comparison_report(
    db: AsyncSession, window_days: int, now: datetime | None = None
) -> RetentionSnapshotComparisonReport:
    now = _now(now)
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


async def _build_retention_snapshot_momentum_report(
    db: AsyncSession, window_days: int, now: datetime | None = None
) -> RetentionSnapshotMomentumReport:
    report = await _build_retention_snapshot_comparison_report(db, window_days, now)
    items = []
    for comparison in report.comparisons:
        if comparison.delta > 0:
            direction = MOMENTUM_DIRECTIONS["positive"]
        elif comparison.delta < 0:
            direction = MOMENTUM_DIRECTIONS["negative"]
        else:
            direction = MOMENTUM_DIRECTIONS["zero"]
        items.append(
            RetentionSnapshotMomentumItem(
                snapshot_type=comparison.snapshot_type,
                direction=direction,
                momentum=abs(comparison.delta),
            )
        )

    return RetentionSnapshotMomentumReport(
        generated_at=_now(now),
        window_days=window_days,
        items=items,
    )


async def _build_retention_snapshot_volatility_report(
    db: AsyncSession, window_days: int, now: datetime | None = None
) -> RetentionSnapshotVolatilityReport:
    report = await _build_retention_snapshot_comparison_report(db, window_days, now)
    items = []
    for comparison in report.comparisons:
        volatility = abs(comparison.delta)
        items.append(
            RetentionSnapshotVolatilityItem(
                snapshot_type=comparison.snapshot_type,
                volatility=volatility,
                classification=resolve_band(
                    volatility,
                    VOLATILITY_BANDS,
                    VOLATILITY_DEFAULT,
                    key="min",
                    label="classification",
                ),
            )
        )
    return RetentionSnapshotVolatilityReport(
        generated_at=_now(now),
        window_days=window_days,
        items=items,
    )


async def _build_retention_snapshot_volatility_summary(
    db: AsyncSession, window_days: int, now: datetime | None = None
) -> RetentionSnapshotVolatilitySummary:
    report = await _build_retention_snapshot_volatility_report(db, window_days, now)
    counts = {name: 0 for name in VOLATILITY_CLASSES}
    for item in report.items:
        counts[item.classification] = counts.get(item.classification, 0) + 1

    return RetentionSnapshotVolatilitySummary(
        generated_at=_now(now),
        window_days=window_days,
        stable=counts["stable"],
        moderate=counts["moderate"],
        high=counts["high"],
    )


async def _build_retention_snapshot_risk_profile(
    db: AsyncSession, window_days: int, now: datetime | None = None
) -> RetentionSnapshotRiskProfile:
    summary = await _build_retention_snapshot_volatility_summary(db, window_days, now)
    # Weights are looked up per class, so a class added to VOLATILITY_CLASSES
    # without a weight contributes 0 rather than raising mid-request.
    score = sum(
        VOLATILITY_WEIGHTS.get(name, 0) * int(getattr(summary, name, 0) or 0)
        for name in VOLATILITY_CLASSES
    )
    risk_level = resolve_band(
        score, RISK_LEVEL_BANDS, RISK_LEVEL_DEFAULT, key="min", label="risk_level"
    )

    return RetentionSnapshotRiskProfile(
        generated_at=_now(now),
        window_days=window_days,
        risk_level=risk_level,
        score=score,
    )


async def _build_retention_snapshot_recommendation(
    db: AsyncSession, window_days: int, now: datetime | None = None
) -> RetentionSnapshotRecommendation:
    profile = await _build_retention_snapshot_risk_profile(db, window_days, now)
    return RetentionSnapshotRecommendation(
        generated_at=_now(now),
        window_days=window_days,
        risk_level=profile.risk_level,
        recommendation=RECOMMENDATION_BY_RISK[profile.risk_level],
    )


async def _build_retention_snapshot_action_plan(
    db: AsyncSession, window_days: int, now: datetime | None = None
) -> RetentionSnapshotActionPlan:
    recommendation = await _build_retention_snapshot_recommendation(db, window_days, now)
    return RetentionSnapshotActionPlan(
        generated_at=_now(now),
        window_days=window_days,
        risk_level=recommendation.risk_level,
        action=ACTION_BY_RISK[recommendation.risk_level],
    )


async def _build_retention_snapshot_audit_report(
    db: AsyncSession, window_days: int, now: datetime | None = None
) -> RetentionSnapshotAuditReport:
    now = _now(now)
    window_start = now - timedelta(days=window_days)
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
        generated_at=now,
        window_days=window_days,
        total_snapshots=sum(item.total_snapshots for item in items),
        items=items,
    )


async def _build_retention_snapshot_audit_export(
    db: AsyncSession, window_days: int, now: datetime | None = None
) -> RetentionSnapshotAuditExport:
    report = await _build_retention_snapshot_audit_report(db, window_days, now)
    snapshot_types = [item.snapshot_type for item in report.items]
    summary = f"{report.total_snapshots} snapshots across {len(snapshot_types)} types"
    return RetentionSnapshotAuditExport(
        generated_at=_now(now),
        window_days=window_days,
        summary=summary,
        snapshot_types=snapshot_types,
        total_snapshots=report.total_snapshots,
    )


async def _build_retention_snapshot_type_breakdown(
    db: AsyncSession, window_days: int, snapshot_type: str, now: datetime | None = None
) -> RetentionSnapshotTypeBreakdownReport:
    window_start = _now(now) - timedelta(days=window_days)
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
        generated_at=_now(now),
        window_days=window_days,
        snapshot_type=snapshot_type,
        total_snapshots=total_snapshots,
        unique_users=unique_users,
        items=items,
    )


async def _build_retention_snapshot_staleness_report(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotStalenessReport:
    now = _now(now)
    window_start = now - timedelta(days=window_days)
    stale_cutoff = now - timedelta(days=stale_after_days)
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
        generated_at=now,
        window_days=window_days,
        stale_after_days=stale_after_days,
        total_stale_snapshots=sum(item.stale_snapshots for item in items),
        items=items,
    )


async def _build_retention_snapshot_staleness_trend(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotStalenessTrendReport:
    report = await _build_retention_snapshot_staleness_report(
        db, window_days, stale_after_days, now
    )
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
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        items=items,
    )


async def _build_retention_snapshot_health_score(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotHealthScore:
    report = await _build_retention_snapshot_staleness_report(
        db, window_days, stale_after_days, now
    )
    score = resolve_health_score(report.total_stale_snapshots)
    status = resolve_band(
        score, HEALTH_STATUS_BANDS, HEALTH_STATUS_DEFAULT, key="min", label="status"
    )
    return RetentionSnapshotHealthScore(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        score=score,
        status=status,
    )


async def _build_retention_snapshot_health_summary(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotHealthSummary:
    score_report = await _build_retention_snapshot_health_score(
        db, window_days, stale_after_days, now
    )
    summary = f"Snapshot freshness is {score_report.status} with a score of {score_report.score}."
    return RetentionSnapshotHealthSummary(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        score=score_report.score,
        status=score_report.status,
        summary=summary,
    )


async def _build_retention_snapshot_health_risk(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotHealthRisk:
    score_report = await _build_retention_snapshot_health_score(
        db, window_days, stale_after_days, now
    )
    risk_level = resolve_band(
        score_report.score, HEALTH_RISK_BANDS, HEALTH_RISK_DEFAULT, key="min", label="risk_level"
    )
    return RetentionSnapshotHealthRisk(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        score=score_report.score,
        status=score_report.status,
        risk_level=risk_level,
    )


async def _build_retention_snapshot_health_recommendation(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotHealthRecommendation:
    risk_report = await _build_retention_snapshot_health_risk(
        db, window_days, stale_after_days, now
    )
    return RetentionSnapshotHealthRecommendation(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        score=risk_report.score,
        status=risk_report.status,
        risk_level=risk_report.risk_level,
        recommendation=HEALTH_RECOMMENDATION_BY_RISK[risk_report.risk_level],
    )


async def _build_retention_snapshot_operations_overview(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotOperationsOverview:
    recommendation_report = await _build_retention_snapshot_health_recommendation(
        db, window_days, stale_after_days, now
    )
    return RetentionSnapshotOperationsOverview(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        score=recommendation_report.score,
        status=recommendation_report.status,
        risk_level=recommendation_report.risk_level,
        recommendation=recommendation_report.recommendation,
        overview=OPERATIONS_OVERVIEW_BY_RISK[recommendation_report.risk_level],
    )


async def _build_retention_snapshot_operations_status(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotOperationsStatus:
    overview_report = await _build_retention_snapshot_operations_overview(
        db, window_days, stale_after_days, now
    )
    return RetentionSnapshotOperationsStatus(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        status=run_operations_ladder(overview_report.risk_level)["status"],
        risk_level=overview_report.risk_level,
        overview=overview_report.overview,
    )


async def _build_retention_snapshot_operations_compliance(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotOperationsCompliance:
    status_report = await _build_retention_snapshot_operations_status(
        db, window_days, stale_after_days, now
    )
    ladder = run_operations_ladder(status_report.risk_level)
    return RetentionSnapshotOperationsCompliance(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        compliance=ladder["compliance"],
        risk_level=status_report.risk_level,
        overview=status_report.overview,
    )


async def _build_retention_snapshot_operations_posture(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotOperationsPosture:
    compliance_report = await _build_retention_snapshot_operations_compliance(
        db, window_days, stale_after_days, now
    )
    ladder = run_operations_ladder(compliance_report.risk_level)
    return RetentionSnapshotOperationsPosture(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        posture=ladder["posture"],
        risk_level=compliance_report.risk_level,
        overview=compliance_report.overview,
    )


async def _build_retention_snapshot_operations_automation(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotOperationsAutomation:
    posture_report = await _build_retention_snapshot_operations_posture(
        db, window_days, stale_after_days, now
    )
    ladder = run_operations_ladder(posture_report.risk_level)
    return RetentionSnapshotOperationsAutomation(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        automation=ladder["automation"],
        risk_level=posture_report.risk_level,
        overview=posture_report.overview,
    )


async def _build_retention_snapshot_operations_execution_state(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotOperationsExecutionState:
    automation_report = await _build_retention_snapshot_operations_automation(
        db, window_days, stale_after_days, now
    )
    ladder = run_operations_ladder(automation_report.risk_level)
    return RetentionSnapshotOperationsExecutionState(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        execution_state=ladder["execution_state"],
        risk_level=automation_report.risk_level,
        overview=automation_report.overview,
    )


async def _build_retention_snapshot_operations_launch_readiness(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotOperationsLaunchReadiness:
    execution_report = await _build_retention_snapshot_operations_execution_state(
        db, window_days, stale_after_days, now
    )
    ladder = run_operations_ladder(execution_report.risk_level)
    return RetentionSnapshotOperationsLaunchReadiness(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        readiness=ladder["readiness"],
        risk_level=execution_report.risk_level,
        overview=execution_report.overview,
    )


async def _build_retention_snapshot_operations_go_no_go(
    db: AsyncSession, window_days: int, stale_after_days: int, now: datetime | None = None
) -> RetentionSnapshotOperationsGoNoGo:
    launch_report = await _build_retention_snapshot_operations_launch_readiness(
        db, window_days, stale_after_days, now
    )
    ladder = run_operations_ladder(launch_report.risk_level)
    return RetentionSnapshotOperationsGoNoGo(
        generated_at=_now(now),
        window_days=window_days,
        stale_after_days=stale_after_days,
        decision=ladder["decision"],
        overview=launch_report.overview,
    )



# --- Policy validation, simulation, sensitivity, catalog ------------------------
#
# The seventeen endpoints above each answer one question about a fixed policy.
# An operator's actual questions are different and none of them are on the API:
# "if I only start treating 5+ as high volatility, what does the go/no-go say?",
# "how many stale snapshots until we drop from go to hold?", "is this config
# even closed?". Answering them through the endpoints means seventeen round
# trips and no comparison, so they are answered here instead — as pure functions
# over the two primitive measurements, which means no database access at all.


def validate_snapshot_ops_policy() -> dict[str, object]:
    """Referential integrity of the operations policy tables.

    Reports rather than raises. A mis-configured band should degrade an
    advisory report, not take the endpoint down; the findings below are what an
    operator needs to see to fix it.
    """
    findings: list[dict[str, str]] = []

    # The ladder must be closed: every stage's input is produced by an earlier
    # stage (or is the seed), and every stage's name is unique.
    produced = {"risk_level"}
    seen_stages: set[str] = set()
    for stage in SNAPSHOT_OPS_STAGES:
        if stage["stage"] in seen_stages:
            findings.append(
                {
                    "severity": "error",
                    "code": "duplicate_stage",
                    "detail": f"stage {stage['stage']!r} is declared more than once",
                }
            )
        if stage["source"] not in produced:
            findings.append(
                {
                    "severity": "error",
                    "code": "unfed_stage",
                    "detail": (
                        f"stage {stage['stage']!r} reads {stage['source']!r}, "
                        "which no earlier stage produces"
                    ),
                }
            )
        produced.add(stage["stage"])
        seen_stages.add(stage["stage"])

    # No ladder stage may be dropped: the terminal decision is the point.
    for required in ("decision",):
        if required not in seen_stages:
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_terminal_stage",
                    "detail": f"the ladder no longer produces {required!r}",
                }
            )

    # Every table must map every value the previous stage can emit, or the fold
    # silently produces an empty verdict at exactly the worst moment (an outage).
    table_inputs: list[tuple[str, set[str]]] = [
        ("OPERATIONS_STATUS_BY_RISK", set(HEALTH_RISK_BANDS and (b["risk_level"] for b in HEALTH_RISK_BANDS)) | {HEALTH_RISK_DEFAULT}),
        ("COMPLIANCE_BY_STATUS", set(OPERATIONS_STATUS_BY_RISK.values())),
        ("POSTURE_BY_COMPLIANCE", set(COMPLIANCE_BY_STATUS.values())),
        ("AUTOMATION_BY_POSTURE", set(POSTURE_BY_COMPLIANCE.values())),
        ("EXECUTION_BY_AUTOMATION", set(AUTOMATION_BY_POSTURE.values())),
        ("READINESS_BY_EXECUTION", set(EXECUTION_BY_AUTOMATION.values())),
        ("DECISION_BY_READINESS", set(READINESS_BY_EXECUTION.values())),
    ]
    for table_name, required_inputs in table_inputs:
        table = OPERATIONS_STAGE_TABLES[table_name]
        for value in sorted(required_inputs):
            if value not in table:
                findings.append(
                    {
                        "severity": "error",
                        "code": "unmapped_input",
                        "detail": f"{table_name} has no entry for {value!r}; the fold would return ''",
                    }
                )

    # Every classification a band can emit must be countable by the summary,
    # or a new band would be classified but never tallied.
    emitted_classes = {str(band["classification"]) for band in VOLATILITY_BANDS} | {VOLATILITY_DEFAULT}
    for name in sorted(emitted_classes):
        if name not in VOLATILITY_CLASSES:
            findings.append(
                {
                    "severity": "warning",
                    "code": "uncounted_class",
                    "detail": f"volatility class {name!r} is emitted but not in VOLATILITY_CLASSES",
                }
            )
    for name in VOLATILITY_CLASSES:
        if name not in emitted_classes:
            findings.append(
                {
                    "severity": "warning",
                    "code": "unreachable_class",
                    "detail": f"VOLATILITY_CLASSES lists {name!r}, which no band can emit",
                }
            )

    # Sentences are looked up by verdict, so a missing one is a KeyError at
    # request time rather than a cosmetic gap.
    for level in sorted(
        {str(b["risk_level"]) for b in RISK_LEVEL_BANDS} | {RISK_LEVEL_DEFAULT}
    ):
        for label, table in (
            ("RECOMMENDATION_BY_RISK", RECOMMENDATION_BY_RISK),
            ("ACTION_BY_RISK", ACTION_BY_RISK),
        ):
            if level not in table:
                findings.append(
                    {
                        "severity": "error",
                        "code": "missing_message",
                        "detail": f"{label} has no entry for risk level {level!r}",
                    }
                )
    for level in sorted(
        {str(b["risk_level"]) for b in HEALTH_RISK_BANDS} | {HEALTH_RISK_DEFAULT}
    ):
        for label, table in (
            ("HEALTH_RECOMMENDATION_BY_RISK", HEALTH_RECOMMENDATION_BY_RISK),
            ("OPERATIONS_OVERVIEW_BY_RISK", OPERATIONS_OVERVIEW_BY_RISK),
            ("OPERATIONS_STATUS_BY_RISK", OPERATIONS_STATUS_BY_RISK),
        ):
            if level not in table:
                findings.append(
                    {
                        "severity": "error",
                        "code": "missing_message",
                        "detail": f"{label} has no entry for health risk level {level!r}",
                    }
                )

    # Bands must be strictly descending, or the first-match fold silently drops
    # a band that can never be reached.
    for name, bands in (
        ("VOLATILITY_BANDS", VOLATILITY_BANDS),
        ("RISK_LEVEL_BANDS", RISK_LEVEL_BANDS),
        ("HEALTH_STATUS_BANDS", HEALTH_STATUS_BANDS),
        ("HEALTH_RISK_BANDS", HEALTH_RISK_BANDS),
    ):
        minimums = [float(band["min"]) for band in bands]
        if minimums != sorted(minimums, reverse=True):
            findings.append(
                {
                    "severity": "error",
                    "code": "unordered_bands",
                    "detail": f"{name} must be ordered highest threshold first; got {minimums}",
                }
            )

    errors = sum(1 for f in findings if f["severity"] == "error")
    return {
        "valid": errors == 0,
        "error_count": errors,
        "warning_count": len(findings) - errors,
        "findings": findings,
        "stages": [dict(stage) for stage in SNAPSHOT_OPS_STAGES],
    }


def evaluate_snapshot_ops(
    volatilities: list[int],
    total_stale: int,
    *,
    volatility_bands: list[dict[str, object]] | None = None,
    volatility_weights: dict[str, int] | None = None,
    risk_level_bands: list[dict[str, object]] | None = None,
    health_status_bands: list[dict[str, object]] | None = None,
    health_risk_bands: list[dict[str, object]] | None = None,
    health_penalty_per_stale: int | None = None,
) -> dict[str, object]:
    """The full derived ladder from the two primitive measurements. Pure.

    ``volatilities`` is the list of raw ``|current - previous|`` magnitudes and
    ``total_stale`` is the number of stale snapshots in the window. Every band is
    an explicit parameter defaulting to the live table, so a scenario is
    evaluated against a *copy* of the policy and the module globals are never
    touched — the failure mode ``app/cell_matrix`` had to be repaired for.

    Taking raw magnitudes rather than pre-bucketed counts is what makes
    ``volatility_bands`` and ``volatility_weights`` real tunables here: a
    classification is derived, not assumed.

    This is the single source of truth the async builders above also route
    through, so a simulated verdict and a served verdict cannot disagree.
    """
    v_bands = VOLATILITY_BANDS if volatility_bands is None else volatility_bands
    v_weights = VOLATILITY_WEIGHTS if volatility_weights is None else volatility_weights
    r_bands = RISK_LEVEL_BANDS if risk_level_bands is None else risk_level_bands
    hs_bands = HEALTH_STATUS_BANDS if health_status_bands is None else health_status_bands
    hr_bands = HEALTH_RISK_BANDS if health_risk_bands is None else health_risk_bands
    penalty = (
        HEALTH_PENALTY_PER_STALE
        if health_penalty_per_stale is None
        else int(health_penalty_per_stale)
    )

    counts = classify_volatilities(list(volatilities), v_bands)
    risk_score = sum(
        int(v_weights.get(name, 0) or 0) * int(counts.get(name, 0) or 0)
        for name in VOLATILITY_CLASSES
    )
    risk_level = resolve_band(
        risk_score, r_bands, RISK_LEVEL_DEFAULT, key="min", label="risk_level"
    )
    health_score = resolve_health_score(total_stale, penalty)
    health_status = resolve_band(
        health_score, hs_bands, HEALTH_STATUS_DEFAULT, key="min", label="status"
    )
    health_risk = resolve_band(
        health_score, hr_bands, HEALTH_RISK_DEFAULT, key="min", label="risk_level"
    )

    return {
        "volatility": {
            "stable": int(counts.get("stable", 0) or 0),
            "moderate": int(counts.get("moderate", 0) or 0),
            "high": int(counts.get("high", 0) or 0),
        },
        "risk": {
            "score": risk_score,
            "risk_level": risk_level,
            "recommendation": RECOMMENDATION_BY_RISK.get(risk_level, ""),
            "action": ACTION_BY_RISK.get(risk_level, ""),
        },
        "health": {
            "score": health_score,
            "status": health_status,
            "risk_level": health_risk,
            "summary": f"Snapshot freshness is {health_status} with a score of {health_score}.",
            "recommendation": HEALTH_RECOMMENDATION_BY_RISK.get(health_risk, ""),
        },
        "operations": {
            "overview": OPERATIONS_OVERVIEW_BY_RISK.get(health_risk, ""),
            **run_operations_ladder(health_risk),
        },
    }


async def simulate_snapshot_ops(
    db: AsyncSession,
    window_days: int = 30,
    stale_after_days: int = 7,
    *,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    """Baseline vs scenario for the whole ladder, in one pass.

    Reads the two primitives once, then evaluates the live policy and every
    requested scenario against them. This is the endpoint that used not to
    exist: "what does the go/no-go say under a stricter policy?" required
    calling all seventeen reports by hand and diffing the prose.
    """
    overrides = dict(overrides or {})
    unknown = sorted(set(overrides) - {str(t["name"]) for t in SNAPSHOT_OPS_TUNABLES})
    if unknown:
        raise ValueError(
            f"unknown snapshot-ops tunables: {', '.join(unknown)}; "
            f"known: {', '.join(str(t['name']) for t in SNAPSHOT_OPS_TUNABLES)}"
        )

    now = _now()
    volatility_report = await _build_retention_snapshot_volatility_report(db, window_days, now)
    staleness_report = await _build_retention_snapshot_staleness_report(
        db, window_days, stale_after_days, now
    )
    volatilities = [item.volatility for item in volatility_report.items]

    baseline = evaluate_snapshot_ops(volatilities, staleness_report.total_stale_snapshots)
    scenarios: list[dict[str, object]] = []
    for name, value in sorted(overrides.items()):
        candidate = evaluate_snapshot_ops(
            volatilities, staleness_report.total_stale_snapshots, **{name: value}
        )
        scenarios.append(
            {
                "tunable": name,
                "override": value,
                "result": candidate,
                "changed": candidate != baseline,
                "changed_paths": _diff_ladder(baseline, candidate),
            }
        )

    return {
        "generated_at": now,
        "window_days": window_days,
        "stale_after_days": stale_after_days,
        "measurements": {
            "volatilities": volatilities,
            "volatility_counts": baseline["volatility"],
            "total_stale_snapshots": staleness_report.total_stale_snapshots,
        },
        "baseline": baseline,
        "scenarios": scenarios,
        "policy_valid": validate_snapshot_ops_policy()["valid"],
    }


def _diff_ladder(baseline: dict[str, object], candidate: dict[str, object]) -> list[str]:
    """Dotted paths whose leaf value differs between two ladders."""
    changed: list[str] = []

    def walk(prefix: str, left: object, right: object) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                walk(f"{prefix}.{key}" if prefix else key, left.get(key), right.get(key))
        elif left != right:
            changed.append(prefix)

    walk("", baseline, candidate)
    return changed


def snapshot_ops_sensitivity(
    volatilities: list[int],
    total_stale: int,
    *,
    sweep_max: int = 100,
) -> dict[str, object]:
    """Break-even points: how much drift flips each verdict.

    Answers the question an operator actually has — "how close are we to
    dropping from go to hold?" — by searching the integer space rather than by
    extrapolation, so the reported boundary is one the code actually takes.
    Reported as a span per verdict because a verdict is not monotonic in total
    stale snapshots once the bands are re-tuned.
    """
    baseline = evaluate_snapshot_ops(list(volatilities), total_stale)

    # Sweep the staleness count, since that is the input an operator controls.
    decisions: dict[str, list[int]] = {}
    statuses: dict[str, list[int]] = {}
    for candidate_stale in range(0, max(int(sweep_max), 0) + 1):
        ladder = evaluate_snapshot_ops(list(volatilities), candidate_stale)
        decisions.setdefault(str(ladder["operations"]["decision"]), []).append(candidate_stale)
        statuses.setdefault(str(ladder["health"]["status"]), []).append(candidate_stale)

    def span(values: list[int]) -> dict[str, int]:
        return {"min": min(values), "max": max(values), "count": len(values)}

    return {
        "baseline": baseline,
        "volatilities": list(volatilities),
        "total_stale_snapshots": total_stale,
        "decision_by_stale_count": {name: span(v) for name, v in sorted(decisions.items())},
        "health_status_by_stale_count": {name: span(v) for name, v in sorted(statuses.items())},
        "swept_stale_counts": {"min": 0, "max": max(int(sweep_max), 0)},
        "note": (
            "staleness is swept from 0 to sweep_max; a decision absent from "
            "decision_by_stale_count is unreachable under the live policy"
        ),
    }


def build_snapshot_ops_catalog() -> dict[str, object]:
    """Introspectable description of the configurable operations surface."""
    validation = validate_snapshot_ops_policy()
    return {
        "volatility_bands": [dict(band) for band in VOLATILITY_BANDS],
        "volatility_classes": list(VOLATILITY_CLASSES),
        "volatility_default": VOLATILITY_DEFAULT,
        "volatility_weights": dict(VOLATILITY_WEIGHTS),
        "risk_level_bands": [dict(band) for band in RISK_LEVEL_BANDS],
        "risk_level_default": RISK_LEVEL_DEFAULT,
        "health_status_bands": [dict(band) for band in HEALTH_STATUS_BANDS],
        "health_status_default": HEALTH_STATUS_DEFAULT,
        "health_risk_bands": [dict(band) for band in HEALTH_RISK_BANDS],
        "health_risk_default": HEALTH_RISK_DEFAULT,
        "health_penalty_per_stale": HEALTH_PENALTY_PER_STALE,
        "health_score_floor": HEALTH_SCORE_FLOOR,
        "stale_after_days_default": 7,
        "window_days_default": 30,
        "momentum_directions": dict(MOMENTUM_DIRECTIONS),
        "ladder": {
            "stages": [dict(stage) for stage in SNAPSHOT_OPS_STAGES],
            "terminal_stage": SNAPSHOT_OPS_STAGES[-1]["stage"] if SNAPSHOT_OPS_STAGES else None,
            "tables": {name: dict(table) for name, table in OPERATIONS_STAGE_TABLES.items()},
            "cached_verdicts": sorted({key[0] for key in _LADDER_CACHE}),
        },
        "tunables": [dict(tunable) for tunable in SNAPSHOT_OPS_TUNABLES],
        "validation": validation,
        "capabilities": {
            "validate_snapshot_ops_policy": "referential integrity of every policy table",
            "evaluate_snapshot_ops": "pure full-ladder evaluation from the two primitives",
            "simulate_snapshot_ops": "baseline vs per-tunable scenarios, one DB read",
            "snapshot_ops_sensitivity": "break-even staleness counts per verdict",
            "build_snapshot_ops_catalog": "this document",
        },
    }
