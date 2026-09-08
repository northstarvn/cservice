from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


ACCESS_TIER_THRESHOLD = 70.0
INTERNAL_ACCESS_TIER_THRESHOLD = 85.0


@dataclass(frozen=True)
class PolicyScoreSnapshot:
    system_score: float
    customer_score: float
    access_score: float
    interest_score: float
    closeness_score: float
    community_closeness_score: float
    policy_tier: str
    control_posture: str
    summary: str


def _safe_average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def _policy_tier(access_score: float, system_score: float) -> str:
    if access_score >= INTERNAL_ACCESS_TIER_THRESHOLD and system_score >= INTERNAL_ACCESS_TIER_THRESHOLD:
        return "system-premium"
    if access_score >= ACCESS_TIER_THRESHOLD:
        return "customer-premium"
    if access_score >= 40:
        return "standard"
    return "restricted"


def _control_posture(policy_tier: str, access_score: float, system_score: float) -> str:
    if policy_tier == "system-premium":
        return "high_trust"
    if policy_tier == "customer-premium":
        return "customer_trusted"
    if access_score >= 55 and system_score >= 55:
        return "observed"
    return "constrained"


def _posture_adjusted_access_score(access_score: float, control_posture: str) -> float:
    if control_posture == "high_trust":
        return access_score
    if control_posture == "customer_trusted":
        return min(100.0, round(access_score + 2.0, 2))
    if control_posture == "observed":
        return max(0.0, round(access_score - 4.0, 2))
    return max(0.0, round(access_score - 8.0, 2))


def summarize_policy_score(snapshot: PolicyScoreSnapshot) -> str:
    return (
        f"system={snapshot.system_score:.2f}, customer={snapshot.customer_score:.2f}, "
        f"access={snapshot.access_score:.2f}, interest={snapshot.interest_score:.2f}, "
        f"closeness={snapshot.closeness_score:.2f}, community={snapshot.community_closeness_score:.2f}, "
        f"posture={snapshot.control_posture}"
    )


async def build_customer_policy_snapshot(db: AsyncSession, user: models.User) -> PolicyScoreSnapshot:
    signal_result = await db.execute(
        select(func.coalesce(func.avg(models.InteractionSignal.score), 0.0))
        .where(models.InteractionSignal.user_id == user.id)
    )
    signal_score = float(signal_result.scalar() or 0.0)

    retention_result = await db.execute(
        select(func.coalesce(func.avg(models.RetentionSnapshot.loyalty_score), 0.0))
        .where(models.RetentionSnapshot.user_id == user.id)
    )
    loyalty_score = float(retention_result.scalar() or 0.0)

    recovery_result = await db.execute(
        select(func.coalesce(func.avg(models.RecoveryOutcome.dissatisfaction_score), 0.0))
        .where(models.RecoveryOutcome.user_id == user.id)
    )
    dissatisfaction_score = float(recovery_result.scalar() or 0.0)

    booking_count_result = await db.execute(
        select(func.count(models.Booking.id)).where(models.Booking.user_id == user.id)
    )
    booking_count = float(booking_count_result.scalar() or 0.0)

    completed_count_result = await db.execute(
        select(func.count(models.Booking.id)).where(models.Booking.user_id == user.id).where(models.Booking.status == models.BookingStatus.completed)
    )
    completed_count = float(completed_count_result.scalar() or 0.0)

    system_score = min(100.0, round((signal_score * 12.0) + (completed_count * 6.0), 2))
    customer_score = min(100.0, round((loyalty_score * 0.6) + max(0.0, 100.0 - dissatisfaction_score), 2))
    interest_score = min(100.0, round(_safe_average([signal_score * 10.0, booking_count * 4.0, completed_count * 8.0]), 2))
    closeness_score = min(100.0, round(_safe_average([customer_score, interest_score]), 2))
    community_closeness_score = min(100.0, round(_safe_average([system_score, closeness_score]), 2))
    access_score = min(100.0, round(_safe_average([customer_score, community_closeness_score, system_score]), 2))
    policy_tier = _policy_tier(access_score, system_score)
    control_posture = _control_posture(policy_tier, access_score, system_score)
    access_score = _posture_adjusted_access_score(access_score, control_posture)
    policy_tier = _policy_tier(access_score, system_score)
    control_posture = _control_posture(policy_tier, access_score, system_score)

    snapshot = PolicyScoreSnapshot(
        system_score=system_score,
        customer_score=customer_score,
        access_score=access_score,
        interest_score=interest_score,
        closeness_score=closeness_score,
        community_closeness_score=community_closeness_score,
        policy_tier=policy_tier,
        control_posture=control_posture,
        summary="",
    )
    return replace(snapshot, summary=summarize_policy_score(snapshot))


async def upsert_customer_policy_score(db: AsyncSession, user: models.User) -> models.CustomerPolicyScore:
    snapshot = await build_customer_policy_snapshot(db, user)
    result = await db.execute(
        select(models.CustomerPolicyScore).where(models.CustomerPolicyScore.user_id == user.id)
    )
    policy_score = result.scalar_one_or_none()
    if policy_score is None:
        policy_score = models.CustomerPolicyScore(user_id=user.id)

    policy_score.system_score = snapshot.system_score
    policy_score.customer_score = snapshot.customer_score
    policy_score.access_score = snapshot.access_score
    policy_score.interest_score = snapshot.interest_score
    policy_score.closeness_score = snapshot.closeness_score
    policy_score.community_closeness_score = snapshot.community_closeness_score
    policy_score.policy_tier = snapshot.policy_tier
    policy_score.source = "system_and_customer_metrics"
    policy_score.summary = snapshot.summary
    policy_score.updated_at = datetime.now(timezone.utc)
    db.add(policy_score)
    await db.commit()
    await db.refresh(policy_score)
    return policy_score


def can_access_functionality(policy_score: models.CustomerPolicyScore, required_tier: str = "standard") -> bool:
    tiers = {"restricted": 0, "standard": 1, "customer-premium": 2, "system-premium": 3}
    return tiers.get(policy_score.policy_tier, 0) >= tiers.get(required_tier, 1)
