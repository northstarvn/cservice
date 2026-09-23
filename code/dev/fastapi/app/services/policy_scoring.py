from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas import chat as chat_schemas
from app.schemas import schemas as app_schemas
from app.services.topics import (
    TOPIC_CATALOG,
    TOPIC_SECTORS,
    TOPIC_THEME_GROUPS,
    build_topic_coverage_report,
    build_topic_intelligence_report,
    build_topic_portfolio_report,
    build_topic_suggestion_report,
    build_topic_theme_coverage,
)


ACCESS_TIER_THRESHOLD = 70.0
INTERNAL_ACCESS_TIER_THRESHOLD = 85.0

# Data-driven policy tier / control posture / access band rules. The resolvers
# below (`resolve_policy_tier`, `resolve_control_posture`, `resolve_access_band`)
# are thin engines over these tables, so tiers, thresholds, and bands can be
# adjusted or extended without touching decision code.
POLICY_TIER_RANK: dict[str, int] = {
    "restricted": 0,
    "standard": 1,
    "customer-premium": 2,
    "system-premium": 3,
}

POLICY_TIER_RULES: list[dict[str, object]] = [
    {
        "tier": "system-premium",
        "access_score_min": INTERNAL_ACCESS_TIER_THRESHOLD,
        "system_score_min": INTERNAL_ACCESS_TIER_THRESHOLD,
    },
    {
        "tier": "customer-premium",
        "access_score_min": ACCESS_TIER_THRESHOLD,
        "system_score_min": 0.0,
    },
    {
        "tier": "standard",
        "access_score_min": 40.0,
        "system_score_min": 0.0,
    },
]

CONTROL_POSTURE_TIER_MAP: dict[str, str] = {
    "system-premium": "high_trust",
    "customer-premium": "customer_trusted",
}

CONTROL_POSTURE_RULES: list[dict[str, object]] = [
    {
        "posture": "observed",
        "access_score_min": 55.0,
        "system_score_min": 55.0,
    },
]

ACCESS_BAND_RULES: list[dict[str, object]] = [
    {"band": "elite", "access_score_min": 90.0},
    {"band": "strong", "access_score_min": 75.0},
    {"band": "moderate", "access_score_min": 55.0},
]


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
    topic_context: str = ""
    topic_richness: str = ""


def _safe_average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def _topic_breadth_score(topic_text: str) -> float:
    normalized = (topic_text or "").lower()
    if not normalized:
        return 0.0
    tokens = [token for token in normalized.replace("/", " ").replace("-", " ").split() if token]
    unique_tokens = set(tokens)
    breadth = len(unique_tokens) * 7.5
    breadth += min(len(TOPIC_CATALOG) * 0.2, 12.0)
    if any(marker in normalized for marker in ["and", "or", "with", "for", "support", "service"]):
        breadth += 8.0
    if any(marker in normalized for marker in ["billing", "booking", "policy", "routing", "handoff", "coverage"]):
        breadth += 10.0
    if any(marker in normalized for marker in ["retention", "discovery", "context", "logistics", "reassurance", "transcript"]):
        breadth += 8.0
    if any(marker in normalized for marker in ["privacy", "consent", "translation", "attachment", "checklist", "feedback"]):
        breadth += 7.0
    if any(marker in normalized for marker in ["intent", "frame", "preferences", "knowledge", "callback", "visibility", "continuity"]):
        breadth += 9.0
    return min(100.0, round(breadth, 2))


def _topic_complexity_score(topic_text: str) -> float:
    normalized = (topic_text or "").lower()
    if not normalized:
        return 0.0
    complexity_terms = [
        "exception",
        "override",
        "escalation",
        "handoff",
        "routing",
        "eligibility",
        "verification",
        "refund",
        "capacity",
        "policy",
        "retention",
        "discovery",
        "summary",
        "context",
        "logistics",
        "privacy",
        "consent",
        "translation",
        "attachment",
        "checklist",
        "feedback",
        "intent",
        "knowledge",
        "callback",
        "visibility",
        "continuity",
        "reassurance",
    ]
    score = sum(9.0 for term in complexity_terms if term in normalized)
    if len(normalized.split()) >= 4:
        score += 8.0
    return min(100.0, round(score, 2))


def _topic_theme_matches(topic_text: str) -> list[str]:
    normalized = (topic_text or "").lower()
    selection = type("PolicyTopicSelection", (), {"topic": normalized})()
    return [item["theme"] for item in build_topic_theme_coverage(selection) if item.get("matched_count")]


def _topic_sector_matches(topic_text: str) -> list[str]:
    normalized = (topic_text or "").lower()
    if not normalized:
        return []
    matches: list[str] = []
    for group in TOPIC_SECTORS:
        if any(topic in normalized for topic in group["topics"]):
            matches.append(str(group["sector"]))
    return list(dict.fromkeys(matches))


def _topic_signals(topic_text: str) -> dict[str, object]:
    normalized = (topic_text or "").strip()
    coverage = build_topic_coverage_report(type("PolicyTopicSelection", (), {"topic": normalized})())
    intelligence = build_topic_intelligence_report(type("PolicyTopicSelection", (), {"topic": normalized})())
    portfolio = build_topic_portfolio_report(type("PolicyTopicSelection", (), {"topic": normalized})())
    suggestions = build_topic_suggestion_report(normalized, limit=3)
    return {
        "coverage_ratio": coverage.coverage_ratio,
        "matched_topics": list(dict.fromkeys(coverage.matched_topics)),
        "matched_keywords": list(dict.fromkeys(intelligence.matched_keywords)),
        "matched_themes": list(dict.fromkeys(portfolio["matched_themes"])),
        "theme_overlap_score": sum(item.get("overlap_score", 0) for item in build_topic_theme_coverage(type("PolicyTopicSelection", (), {"topic": normalized})()) if item.get("matched_count")),
        "matched_theme_topics": list(dict.fromkeys(topic for topic in coverage.matched_topics if topic in topic_text.lower())),
        "matched_sectors": [sector for sector in _topic_sector_matches(normalized)],
        "suggested_topics": suggestions.suggested_topics,
        "topic_focus": list(dict.fromkeys(list(coverage.top_recommendations) + [item.topic for item in intelligence.suggested_topics[:3]] + list(dict.fromkeys(portfolio["matched_themes"])) + list(dict.fromkeys(coverage.matched_topics[:4]))))[:8],
        "topic_richness_score": round(min(100.0, (coverage.coverage_ratio * 40.0) + (len(intelligence.matched_keywords) * 5.5) + (len(portfolio["matched_themes"]) * 7.0) + (len(portfolio["topic_focus"]) * 2.0)), 2),
        "topic_family_count": len(set(_topic_theme_matches(normalized) + _topic_sector_matches(normalized))),
    }


def resolve_policy_tier(access_score: float, system_score: float) -> str:
    """First matching tier rule wins (rules are ordered highest -> lowest)."""
    for rule in POLICY_TIER_RULES:
        if access_score >= float(rule["access_score_min"]) and system_score >= float(rule["system_score_min"]):
            return str(rule["tier"])
    return "restricted"


def resolve_control_posture(policy_tier: str, access_score: float, system_score: float) -> str:
    if policy_tier in CONTROL_POSTURE_TIER_MAP:
        return CONTROL_POSTURE_TIER_MAP[policy_tier]
    for rule in CONTROL_POSTURE_RULES:
        if access_score >= float(rule["access_score_min"]) and system_score >= float(rule["system_score_min"]):
            return str(rule["posture"])
    return "constrained"


def resolve_access_band(access_score: float) -> str:
    for rule in ACCESS_BAND_RULES:
        if access_score >= float(rule["access_score_min"]):
            return str(rule["band"])
    return "limited"


def build_policy_tier_catalog() -> dict[str, object]:
    """Return the active tier, posture, and band rules as a JSON-friendly catalog.

    Future admin surfaces can inspect which thresholds are live without importing
    internals, and can validate that new tiers added to the rule tables are picked
    up automatically by the resolvers.
    """
    return {
        "tier_rank": dict(POLICY_TIER_RANK),
        "tier_rules": [dict(rule) for rule in POLICY_TIER_RULES],
        "control_posture_tier_map": dict(CONTROL_POSTURE_TIER_MAP),
        "control_posture_rules": [dict(rule) for rule in CONTROL_POSTURE_RULES],
        "access_band_rules": [dict(rule) for rule in ACCESS_BAND_RULES],
        "access_tier_threshold": ACCESS_TIER_THRESHOLD,
        "internal_access_tier_threshold": INTERNAL_ACCESS_TIER_THRESHOLD,
    }


def _policy_tier(access_score: float, system_score: float) -> str:
    return resolve_policy_tier(access_score, system_score)


def _control_posture(policy_tier: str, access_score: float, system_score: float) -> str:
    return resolve_control_posture(policy_tier, access_score, system_score)


def _posture_adjusted_access_score(access_score: float, control_posture: str) -> float:
    if control_posture == "high_trust":
        return access_score
    if control_posture == "customer_trusted":
        return min(100.0, round(access_score + 2.0, 2))
    if control_posture == "observed":
        return max(0.0, round(access_score - 4.0, 2))
    return max(0.0, round(access_score - 8.0, 2))


def summarize_policy_score(snapshot: PolicyScoreSnapshot) -> str:
    parts = [
        f"system={snapshot.system_score:.2f}, customer={snapshot.customer_score:.2f}, "
        f"access={snapshot.access_score:.2f}, interest={snapshot.interest_score:.2f}, "
        f"closeness={snapshot.closeness_score:.2f}, community={snapshot.community_closeness_score:.2f}, "
        f"posture={snapshot.control_posture}"
    ]
    if snapshot.topic_context:
        parts.append(f"topic_context={snapshot.topic_context}")
    if snapshot.topic_richness:
        parts.append(f"topic_richness={snapshot.topic_richness}")
    return ", ".join(parts)


def build_policy_health_summary(snapshot: PolicyScoreSnapshot) -> app_schemas.CustomerPolicyHealthSummaryOut:
    signals = {
        "system_score": snapshot.system_score,
        "customer_score": snapshot.customer_score,
        "access_score": snapshot.access_score,
        "interest_score": snapshot.interest_score,
        "closeness_score": snapshot.closeness_score,
        "community_closeness_score": snapshot.community_closeness_score,
    }
    weak_points = sorted(
        ((name, score) for name, score in signals.items() if score < 60.0),
        key=lambda item: item[1],
    )
    strong_points = sorted(
        ((name, score) for name, score in signals.items() if score >= 75.0),
        key=lambda item: item[1],
        reverse=True,
    )
    dominant_signal = max(signals, key=signals.get)
    return app_schemas.CustomerPolicyHealthSummaryOut(
        generated_at=datetime.now(timezone.utc),
        policy_tier=snapshot.policy_tier,
        control_posture=snapshot.control_posture,
        dominant_signal=dominant_signal,
        weak_points=[{"name": name, "score": score} for name, score in weak_points],
        strong_points=[{"name": name, "score": score} for name, score in strong_points],
        balance_index=round(snapshot.access_score - min(signals.values()), 2),
    )


def build_policy_access_recommendations(snapshot: PolicyScoreSnapshot) -> list[app_schemas.CustomerPolicyRecommendationOut]:
    health = build_policy_health_summary(snapshot)
    recommendations: list[app_schemas.CustomerPolicyRecommendationOut] = []

    if snapshot.control_posture in {"constrained", "observed"}:
        recommendations.append(
            app_schemas.CustomerPolicyRecommendationOut(
                priority="high",
                area="control_posture",
                recommendation="Recheck access thresholds before granting higher-tier functionality.",
                evidence=f"Control posture is {snapshot.control_posture}.",
            )
        )

    weak_points = health.weak_points
    for weak_point in weak_points[:3]:
        recommendations.append(
            app_schemas.CustomerPolicyRecommendationOut(
                priority="medium" if weak_point["score"] >= 45 else "high",
                area=weak_point["name"],
                recommendation=f"Improve {weak_point['name'].replace('_', ' ')} to raise overall access readiness.",
                evidence=f"{weak_point['name']} is at {weak_point['score']:.2f}.",
            )
        )

    if snapshot.access_score >= 75 and snapshot.system_score >= 75:
        recommendations.append(
            app_schemas.CustomerPolicyRecommendationOut(
                priority="low",
                area="expansion",
                recommendation="Consider allowing richer functionality for this user segment.",
                evidence=f"Access score {snapshot.access_score:.2f} and system score {snapshot.system_score:.2f} are both strong.",
            )
        )

    return recommendations[:5]


def build_policy_decision_report(
    snapshot: PolicyScoreSnapshot,
    user: models.User,
    functionality: str,
    required_tier: str = "standard",
) -> app_schemas.CustomerPolicyDecisionSummaryOut:
    access_decision = build_policy_access_decision(
        user,
        type("PolicyScoreRef", (), {
            "policy_tier": snapshot.policy_tier,
            "control_posture": snapshot.control_posture,
            "access_score": snapshot.access_score,
            "customer_score": snapshot.customer_score,
            "system_score": snapshot.system_score,
        })(),
        functionality=functionality,
        required_tier=required_tier,
    )
    health = build_policy_health_summary(snapshot)
    recommendations = build_policy_access_recommendations(snapshot)
    topic_signal = snapshot.topic_richness or snapshot.topic_context or "no_topic_signal"
    topic_analysis = build_policy_topic_analysis_report(snapshot, user)
    return app_schemas.CustomerPolicyDecisionSummaryOut(
        generated_at=datetime.now(timezone.utc),
        user_id=user.id,
        functionality=functionality,
        required_tier=required_tier,
        decision=access_decision,
        health=health,
        recommendations=recommendations,
        summary=f"{snapshot.summary}, topic_signal={topic_signal}, topic_depth={topic_analysis.topic_depth}, themes={len(topic_analysis.matched_themes)}, recommendations={len(recommendations)}",
        topic_context=f"{topic_analysis.topic_context}; signal={topic_signal}" if topic_analysis.topic_context else topic_signal,
    )


def build_policy_topic_context(user: models.User, topic_text: str) -> str:
    normalized = (topic_text or "").strip()
    if not normalized:
        return f"user-{user.id}: no active topic"
    breadth = _topic_breadth_score(normalized)
    complexity = _topic_complexity_score(normalized)
    matched_themes = _topic_theme_matches(normalized)
    matched_sectors = _topic_sector_matches(normalized)
    topic_signals = _topic_signals(normalized)
    return (
        f"{normalized} [breadth={breadth:.2f}, complexity={complexity:.2f}, "
        f"themes={len(matched_themes)}, sectors={len(matched_sectors)}, coverage={topic_signals['coverage_ratio']:.2f}, focus={len(topic_signals['topic_focus'])}, "
        f"family_count={topic_signals['topic_family_count']}, richness={topic_signals['topic_richness_score']:.2f}]"
    )


def build_policy_topic_richness(topic_text: str) -> str:
    normalized = (topic_text or "").strip()
    if not normalized:
        return "no_topic_richness"
    breadth = _topic_breadth_score(normalized)
    complexity = _topic_complexity_score(normalized)
    signals = _topic_signals(normalized)
    family_count = signals["topic_family_count"]
    balance = round((breadth + complexity + (signals["coverage_ratio"] * 100.0) + (len(signals["matched_keywords"]) * 4.0) + (len(signals["topic_focus"]) * 2.5) + (family_count * 3.5) + signals["topic_richness_score"]) / 6.0, 2)
    if balance >= 75:
        return f"rich [balance={balance:.2f}, signals={len(signals['matched_topics'])}, families={family_count}]"
    if balance >= 45:
        return f"balanced [balance={balance:.2f}, signals={len(signals['matched_topics'])}, families={family_count}]"
    return f"focused [balance={balance:.2f}, signals={len(signals['matched_topics'])}, families={family_count}]"


def build_policy_topic_depth(topic_text: str) -> str:
    normalized = (topic_text or "").strip().lower()
    if not normalized:
        return "no_topic_depth"
    words = [word for word in normalized.replace("/", " ").replace("-", " ").split() if word]
    depth = len(set(words)) * 8.0
    depth += min(len(TOPIC_THEME_GROUPS) * 1.5, 18.0)
    if any(marker in normalized for marker in ["and", "with", "or", "routing", "handoff", "billing", "booking"]):
        depth += 10.0
    if len(words) >= 5:
        depth += 6.0
    if any(marker in normalized for marker in ["exception", "urgent", "priority", "policy", "verification"]):
        depth += 12.0
    if any(marker in normalized for marker in ["retention", "discovery", "context", "summary", "logistics", "reassurance"]):
        depth += 8.0
    return f"depth={min(100.0, round(depth, 2)):.2f}"


def build_policy_topic_fallback(topic_text: str) -> str:
    normalized = (topic_text or "").strip()
    if normalized:
        topic_lower = normalized.lower()
        sector_match = next(
            (group["sector"] for group in TOPIC_SECTORS if any(topic in topic_lower for topic in group["topics"])),
            None,
        )
        if sector_match:
            return f"{normalized} [sector={sector_match}]"
        theme_match = next(
            (group["theme"] for group in TOPIC_THEME_GROUPS if any(topic in topic_lower for topic in group["topics"])),
            None,
        )
        if theme_match:
            return f"{normalized} [theme={theme_match}]"
        return normalized
    return "unclassified-topic [fallback]"


def build_policy_topic_analysis_report(snapshot: PolicyScoreSnapshot, user: models.User) -> chat_schemas.PolicyTopicAnalysisReport:
    raw_topic = snapshot.topic_context or snapshot.topic_richness or ""
    current_topic = raw_topic.strip() or build_policy_topic_fallback(raw_topic)
    topic_context = snapshot.topic_context or build_policy_topic_context(user, current_topic)
    topic_richness = snapshot.topic_richness or build_policy_topic_richness(current_topic)
    topic_depth = build_policy_topic_depth(current_topic)
    breadth = _topic_breadth_score(current_topic)
    complexity = _topic_complexity_score(current_topic)
    topic_fallback = current_topic
    enriched_fallback = build_policy_topic_fallback(current_topic)
    theme_coverage = build_topic_theme_coverage(type("PolicyTopicSelection", (), {"topic": current_topic})())
    theme_coverage_items = [item for item in theme_coverage if item["matched_count"]]
    matched_themes = [item["theme"] for item in theme_coverage_items]
    portfolio_report = build_topic_portfolio_report(type("PolicyTopicSelection", (), {"topic": current_topic})())
    intelligence_report = build_topic_intelligence_report(type("PolicyTopicSelection", (), {"topic": current_topic})())
    coverage_report = build_topic_coverage_report(type("PolicyTopicSelection", (), {"topic": current_topic})())
    signals = _topic_signals(current_topic)
    matched_topics = list(dict.fromkeys(signals["matched_topics"]))
    topic_focus = signals["topic_focus"] or matched_topics[:5] or list(dict.fromkeys(intelligence_report.matched_keywords[:5]))
    theme_overlap_score = int(signals.get("theme_overlap_score", 0))
    items = [
        chat_schemas.PolicyTopicInsightItem(
            topic=current_topic,
            breadth=breadth,
            complexity=complexity,
            richness=topic_richness,
            depth=topic_depth,
            fallback=topic_fallback,
            theme_coverage=theme_coverage_items,
            sector_coverage=list(dict.fromkeys(portfolio_report["matched_themes"])),
            matched_keywords=(signals["matched_keywords"] or matched_topics or list(dict.fromkeys(intelligence_report.matched_keywords)))[:8],
            confidence=round(min(1.0, breadth / 100.0 + complexity / 120.0), 2),
        )
    ]
    summary = (
        f"{snapshot.summary or summarize_policy_score(snapshot)}, "
        f"topic={current_topic}, topic_context={topic_context}, topic_richness={topic_richness}, topic_depth={topic_depth}, "
        f"fallback={enriched_fallback}, topic_focus={len(topic_focus)}, topic_signals={len(matched_topics)}, sectors={len(_topic_sector_matches(current_topic))}, themes={len(matched_themes)}, overlap={theme_overlap_score}"
    )
    return chat_schemas.PolicyTopicAnalysisReport(
        generated_at=datetime.now(timezone.utc),
        user_id=user.id,
        current_topic=current_topic,
        topic_context=topic_context,
        topic_richness=topic_richness,
        topic_depth=topic_depth,
        topic_coverage=theme_coverage_items,
        portfolio_coverage=portfolio_report["coverage_ratio"],
        matched_themes=matched_themes,
        topic_focus=topic_focus,
        topic_signal_count=len(matched_topics),
        items=items,
        summary=f"Policy topic analysis for '{current_topic}' spans {len(matched_topics)} matched topics, {len(matched_themes)} themes, {len(theme_coverage_items)} theme matches, {len(portfolio_report['matched_themes'])} portfolio themes, {len(_topic_sector_matches(current_topic))} sectors, and theme_overlap_score={theme_overlap_score}; topic_depth={topic_depth}. The topic layer now emphasizes privacy, continuity, routing, trust, discovery, retention, checklist, callback, and knowledge evidence.",
    )


def build_policy_access_band(access_score: float) -> str:
    return resolve_access_band(access_score)


def build_policy_score_breakdown(snapshot: PolicyScoreSnapshot) -> chat_schemas.PolicyScoreBreakdown:
    return chat_schemas.PolicyScoreBreakdown(
        system_score=snapshot.system_score,
        customer_score=snapshot.customer_score,
        access_score=snapshot.access_score,
        interest_score=snapshot.interest_score,
        closeness_score=snapshot.closeness_score,
        community_closeness_score=snapshot.community_closeness_score,
        policy_tier=snapshot.policy_tier,
        control_posture=snapshot.control_posture,
        access_band=build_policy_access_band(snapshot.access_score),
        summary=summarize_policy_score(snapshot),
    )


def build_typed_policy_score_report(snapshot: PolicyScoreSnapshot) -> chat_schemas.PolicyScoreReport:
    breakdown = build_policy_score_breakdown(snapshot)
    topic_context = snapshot.topic_context or ""
    topic_summary = topic_context or breakdown.summary
    topic_context_size = len([part for part in topic_context.split(",") if part.strip()]) if topic_context else 0
    return chat_schemas.PolicyScoreReport(
        generated_at=datetime.now(timezone.utc),
        snapshot=breakdown,
        summary=f"{breakdown.summary}; topic_context={topic_summary}; topic_context_size={topic_context_size}; topic_families={len(topic_context.split('[')[0].split()) if topic_context else 0}",
    )


def build_policy_score_out(snapshot: PolicyScoreSnapshot, user: models.User) -> app_schemas.CustomerPolicyScoreOut:
    now = datetime.now(timezone.utc)
    return app_schemas.CustomerPolicyScoreOut(
        id=0,
        user_id=user.id,
        system_score=snapshot.system_score,
        customer_score=snapshot.customer_score,
        access_score=snapshot.access_score,
        interest_score=snapshot.interest_score,
        closeness_score=snapshot.closeness_score,
        community_closeness_score=snapshot.community_closeness_score,
        policy_tier=snapshot.policy_tier,
        control_posture=snapshot.control_posture,
        source="system_and_customer_metrics",
        summary=snapshot.summary,
        created_at=now,
        updated_at=now,
    )


def build_policy_access_decision(
    user: models.User,
    policy_score: models.CustomerPolicyScore,
    functionality: str,
    required_tier: str = "standard",
) -> app_schemas.CustomerPolicyAccessOut:
    control_posture = getattr(policy_score, "control_posture", "observed")
    effective_required_tier = required_tier
    if control_posture in {"constrained", "observed"} and required_tier == "standard":
        effective_required_tier = "customer-premium"

    allowed = can_access_functionality(policy_score, required_tier=effective_required_tier)
    return app_schemas.CustomerPolicyAccessOut(
        user_id=user.id,
        functionality=functionality,
        required_tier=required_tier,
        effective_required_tier=effective_required_tier,
        allowed=allowed,
        policy_tier=policy_score.policy_tier,
        control_posture=control_posture,
        access_score=policy_score.access_score,
        customer_score=policy_score.customer_score,
        system_score=policy_score.system_score,
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

    topic_result = await db.execute(
        select(func.coalesce(func.max(models.TopicSelection.confidence), 0.0)).where(models.TopicSelection.user_id == user.id)
    )
    topic_confidence = float(topic_result.scalar() or 0.0)

    topic_selection_result = await db.execute(
        select(models.TopicSelection.topic).where(models.TopicSelection.user_id == user.id).where(models.TopicSelection.is_current.is_(True))
    )
    current_topic = str(topic_selection_result.scalar() or "")

    topic_breadth_score = _topic_breadth_score(current_topic)
    topic_complexity_score = _topic_complexity_score(current_topic)
    topic_context = build_policy_topic_context(user, current_topic)
    topic_richness = build_policy_topic_richness(current_topic)
    topic_fallback = build_policy_topic_fallback(current_topic)

    system_score = min(100.0, round((signal_score * 12.0) + (completed_count * 6.0) + (topic_complexity_score * 0.25), 2))
    customer_score = min(100.0, round((loyalty_score * 0.6) + max(0.0, 100.0 - dissatisfaction_score) + (topic_confidence * 12.0), 2))
    interest_score = min(100.0, round(_safe_average([signal_score * 10.0, booking_count * 4.0, completed_count * 8.0, topic_breadth_score]), 2))
    closeness_score = min(100.0, round(_safe_average([customer_score, interest_score, topic_breadth_score]), 2))
    community_closeness_score = min(100.0, round(_safe_average([system_score, closeness_score, topic_complexity_score]), 2))
    access_score = min(100.0, round(_safe_average([customer_score, community_closeness_score, system_score]), 2))
    policy_tier = _policy_tier(access_score, system_score)
    control_posture = _control_posture(policy_tier, access_score, system_score)
    access_score = _posture_adjusted_access_score(access_score, control_posture)
    policy_tier = _policy_tier(access_score, system_score)
    control_posture = _control_posture(policy_tier, access_score, system_score)

    policy_snapshot = PolicyScoreSnapshot(
        system_score=system_score,
        customer_score=customer_score,
        access_score=access_score,
        interest_score=interest_score,
        closeness_score=closeness_score,
        community_closeness_score=community_closeness_score,
        policy_tier=policy_tier,
        control_posture=control_posture,
        topic_context=topic_context,
        topic_richness=topic_richness,
        summary="",
    )
    summary_snapshot = replace(
        policy_snapshot,
        summary=(
            f"{summarize_policy_score(policy_snapshot)}"
            f", topic_breadth={topic_breadth_score:.2f}, topic_complexity={topic_complexity_score:.2f}, topic_confidence={topic_confidence:.2f}, topic={topic_fallback}, topic_richness={topic_richness}, topic_depth={build_policy_topic_depth(current_topic)}, topic_context={topic_context}"
        ),
    )
    return summary_snapshot


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
    return POLICY_TIER_RANK.get(policy_score.policy_tier, 0) >= POLICY_TIER_RANK.get(required_tier, 1)
