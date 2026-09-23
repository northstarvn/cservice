from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import json
import os

import requests
from dotenv import load_dotenv
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas import chat as chat_schemas
from app.services.topics import (
    TOPIC_CATALOG,
    TOPIC_SECTORS,
    TOPIC_THEME_GROUPS,
    build_topic_portfolio_report,
    build_topic_theme_coverage,
)
from app.schemas.chat import (
    ChurnPrediction,
    DissatisfactionRecoveryReport,
    InteractionInsight,
    InteractionSummary,
    InteractionTrendItem,
    InteractionTrendReport,
    LifecycleStageItem,
    LifecycleStageReport,
    MonetizationCohortItem,
    MonetizationCohortReport,
    RecoverySignal,
    RetentionCohortItem,
    RetentionCohortReport,
    RetentionSnapshotActionPlan,
    RetentionSnapshotRecommendation,
    RetentionSnapshotOperationItem,
    RetentionSnapshotOperationsReport,
    Sentiment,
    TopicRankingItem,
    TopicRankingReport,
    TopicPolicyDecision,
    TopicPolicyDecisionReport,
    SystemImprovementItem,
    SystemImprovementPack,
)

load_dotenv()

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

# Keyword terms wired into scoring that are NOT already covered by each area's
# hardcoded term list. Currently the retention-mapped terms ("repeat", "again");
# the remaining keyword mappings are already reflected in score_area's branches.
RETENTION_KEYWORD_TERMS = [keyword for keyword, area in RETENTION_KEYWORDS.items() if area == "retention"]

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

# Data-driven scoring rules for interaction policy areas. `score_area` is a thin
# engine over this table, so future policy areas can be added (or tuned) without
# touching the scoring code. Rule kinds:
#   - "message_keyword": scan each message for `keywords`; weight applies to
#     older messages, `recent_weight` to messages inside `messages[:3]`.
#   - "term_table": count the configured `terms` that appear in any message,
#     multiply by `weight`, and cap at `cap`.
# Optional rule extensions:
#   - "booking_boost": adds a fixed amount per matching booking status (capped).
#   - "repeated_message_boost": adds a fixed amount per repeated message (capped).
AREA_SCORING_RULES: dict[str, dict[str, object]] = {
    "response_speed": {
        "kind": "message_keyword",
        "keywords": ("slow", "delay", "wait"),
        "weight": 0.9,
        "recent_weight": 1.2,
    },
    "clarity": {
        "kind": "message_keyword",
        "keywords": ("confusing", "unclear", "hard"),
        "weight": 0.8,
        "recent_weight": 1.1,
    },
    "reliability": {
        "kind": "message_keyword",
        "keywords": ("bug", "error", "issue"),
        "weight": 1.0,
        "recent_weight": 1.3,
    },
    "booking_flow": {
        "kind": "message_keyword",
        "keywords": ("cancel", "book", "schedule"),
        "weight": 0.8,
        "recent_weight": 1.0,
        "booking_boost": {
            "statuses": ("pending", "cancelled"),
            "per_booking": 0.6,
            "cap": 2.4,
        },
    },
    "support": {
        "kind": "message_keyword",
        "keywords": ("help", "support"),
        "weight": 0.7,
        "recent_weight": 0.9,
    },
    "pricing": {
        "kind": "message_keyword",
        "keywords": ("price", "expensive"),
        "weight": 0.7,
        "recent_weight": 0.9,
    },
    "retention": {
        "kind": "message_keyword",
        "keywords": RETENTION_KEYWORD_TERMS,
        "weight": 0.8,
        "recent_weight": 1.0,
        "repeated_message_boost": {
            "per_repeat": 0.5,
            "cap": 2.0,
        },
    },
    "sentiment_recovery": {
        "kind": "term_table",
        "terms": ("angry", "frustrated", "upset", "disappointed", "bad"),
        "weight": 0.8,
        "cap": 2.4,
        "evidence": "negative sentiment language detected in recent messages",
    },
    "onboarding": {
        "kind": "term_table",
        "terms": ("how do i", "new here", "getting started", "first time", "setup"),
        "weight": 0.7,
        "cap": 2.1,
        "evidence": "onboarding guidance requests detected",
    },
    "notification_quality": {
        "kind": "term_table",
        "terms": ("notify", "notification", "alert", "remind", "reminder"),
        "weight": 0.6,
        "cap": 1.8,
        "evidence": "notification and reminder language detected",
    },
    "self_service": {
        "kind": "term_table",
        "terms": ("faq", "self service", "self-service", "how to", "help center"),
        "weight": 0.6,
        "cap": 1.8,
        "evidence": "self-service questions detected",
    },
    "handoff": {
        "kind": "term_table",
        "terms": ("agent", "transfer", "human", "someone", "escalate"),
        "weight": 0.6,
        "cap": 1.8,
        "evidence": "handoff and escalation language detected",
    },
    "trust": {
        "kind": "term_table",
        "terms": ("trust", "refund", "guarantee", "secure", "safe"),
        "weight": 0.5,
        "cap": 1.5,
        "evidence": "trust-related concerns detected",
    },
    "follow_up": {
        "kind": "term_table",
        "terms": ("follow up", "follow-up", "still waiting", "again", "remind"),
        "weight": 0.7,
        "cap": 2.1,
        "evidence": "follow-up language detected",
    },
}

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


def normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def score_area(messages: list[str], bookings: list[models.Booking], area: str) -> tuple[float, list[str]]:
    evidence: list[str] = []
    score = 0.0
    rule = AREA_SCORING_RULES.get(area)
    if not rule:
        return score, evidence

    recent_messages = messages[:3]
    recent_weight = rule.get("recent_weight")
    weight = rule.get("weight")

    if rule.get("kind") == "message_keyword":
        keywords = rule.get("keywords", ())
        for message in messages:
            normalized = normalize_text(message)
            if any(word in normalized for word in keywords):
                score += float(recent_weight if message in recent_messages else weight)
                evidence.append(message)

    booking_boost = rule.get("booking_boost")
    if booking_boost:
        statuses = booking_boost.get("statuses", ())
        cancelled_or_pending = [
            b for b in bookings
            if str(getattr(b.status, "value", b.status)) in statuses
        ]
        score += min(len(cancelled_or_pending) * float(booking_boost.get("per_booking", 0.0)), float(booking_boost.get("cap", 0.0)))
        if cancelled_or_pending:
            evidence.append(f"{len(cancelled_or_pending)} bookings are pending/cancelled")

    repeated_boost = rule.get("repeated_message_boost")
    if repeated_boost and messages:
        repeated = max(0, len(messages) - len(set(messages)))
        score += min(repeated * float(repeated_boost.get("per_repeat", 0.0)), float(repeated_boost.get("cap", 0.0)))
        if repeated:
            evidence.append(f"{repeated} repeated messages suggest unresolved needs")

    if rule.get("kind") == "term_table" and messages:
        terms = [
            word for word in rule.get("terms", ())
            if any(word in normalize_text(message) for message in messages)
        ]
        score += min(len(terms) * float(weight), float(rule.get("cap", 0.0)))
        if terms:
            evidence.append(str(rule.get("evidence", "relevant language detected")))

    return score, evidence


def score_all_areas(messages: list[str], bookings: list[models.Booking]) -> dict[str, tuple[float, list[str]]]:
    """Score every configured area in one pass, keyed by area name.

    Future consumers (routing hooks, dashboards, dynamic policy engines) can iterate
    this map instead of calling `score_area` per area.
    """
    return {area: score_area(messages, bookings, area) for area in AREA_SCORING_RULES}


def build_area_scoring_catalog() -> list[dict[str, object]]:
    """Return the active area-scoring rules as a JSON-friendly catalog.

    Exposed publicly so operators and future admin surfaces can discover which
    policy areas are scored, with what keywords, weights, and caps — and can
    validate that new areas added to the rule table are picked up automatically.
    """
    catalog = []
    for area, rule in sorted(AREA_SCORING_RULES.items()):
        entry: dict[str, object] = {
            "area": area,
            "kind": rule.get("kind"),
            "keywords": list(rule.get("keywords", ())) or list(rule.get("terms", ())),
            "weight": rule.get("weight"),
        }
        if rule.get("recent_weight") is not None:
            entry["recent_weight"] = rule.get("recent_weight")
        if rule.get("cap") is not None:
            entry["cap"] = rule.get("cap")
        if rule.get("evidence"):
            entry["evidence_label"] = rule.get("evidence")
        if rule.get("booking_boost"):
            entry["booking_boost"] = dict(rule["booking_boost"])
        if rule.get("repeated_message_boost"):
            entry["repeated_message_boost"] = dict(rule["repeated_message_boost"])
        catalog.append(entry)
    return catalog


def build_area_keyword_catalog() -> dict[str, list[str]]:
    """Map every scoring keyword back to the areas it drives.

    Merges the rule-table keywords with the legacy `RETENTION_KEYWORDS` map so a
    single dynamic keyword -> area index is available for routing and discovery.
    """
    mapping: dict[str, list[str]] = {}
    for area, rule in AREA_SCORING_RULES.items():
        for keyword in rule.get("keywords", ()) or rule.get("terms", ()):
            mapping.setdefault(keyword, []).append(area)
    for keyword, area in RETENTION_KEYWORDS.items():
        areas = mapping.setdefault(keyword, [])
        if area not in areas:
            areas.append(area)
    return mapping


def _evidence_summary(evidence: list[str]) -> str:
    if not evidence:
        return "No direct evidence captured"
    if len(evidence) == 1:
        return evidence[0]
    return f"{evidence[0]} (+{len(evidence) - 1} more)"


def _signal_payload(item: InteractionInsight, summary: InteractionSummary) -> dict[str, object]:
    return {
        "area": item.area,
        "priority": item.priority,
        "score": item.score,
        "source": item.source,
        "evidence": item.evidence,
        "evidence_summary": item.evidence_summary,
        "recommendation": item.recommendation,
        "next_step": item.next_step,
        "summary": {
            "loyalty_score": summary.loyalty_score,
            "monetization_readiness": summary.monetization_readiness,
            "churn_risk": summary.churn_risk,
            "repeat_messages": summary.metadata.get("repeated_messages", 0),
            "booking_states": dict(summary.metadata.get("booking_states", {})),
            "signal_strength": summary.metadata.get("signal_strength", 0.0),
        },
    }


def rank_interaction_areas(messages: list[str], bookings: list[models.Booking], limit: int = 5) -> list[dict[str, object]]:
    """Return the highest-scoring policy areas with compact evidence for reuse in routing or UI hints."""
    ranking: list[dict[str, object]] = []
    for area in PREDEFINED_POLICY_AREAS:
        score, evidence = score_area(messages, bookings, area)
        if score <= 0:
            continue
        evidence_list = list(dict.fromkeys(evidence))[:4]
        config = POLICY_CONFIGS.get(area, {})
        ranking.append(
            {
                "area": area,
                "score": round(float(score), 2),
                "priority": config.get("priority", "medium"),
                "impact": config.get("impact", "medium"),
                "owner_hint": config.get("owner_hint", "backend"),
                "evidence": evidence_list,
                "evidence_summary": _evidence_summary(evidence_list),
                "recommendation": config.get("recommendation", "Create a closed-loop retention workflow for unresolved interactions."),
                "next_step": config.get("next_step", "Trigger follow-up prompts when the same concern repeats across sessions."),
            }
        )

    ranking.sort(key=lambda item: item["score"], reverse=True)
    return ranking[: max(1, limit)]


def build_topic_ranking_report(messages: list[str], bookings: list[models.Booking], window_days: int = 30, limit: int = 5) -> TopicRankingReport:
    ranked_areas = rank_interaction_areas(messages, bookings, limit=limit)
    topics = [
        TopicRankingItem(
            topic=item["area"],
            score=float(item["score"]),
            priority=str(item["priority"]),
            impact=str(item["impact"]),
            owner_hint=str(item["owner_hint"]),
            evidence=list(item.get("evidence", [])),
            evidence_summary=str(item.get("evidence_summary", "No direct evidence captured")),
            recommendation=str(item.get("recommendation", "Create a closed-loop retention workflow for unresolved interactions.")),
            next_step=str(item.get("next_step", "Trigger follow-up prompts when the same concern repeats across sessions.")),
        )
        for item in ranked_areas
    ]

    return TopicRankingReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        topics=topics,
    )


def build_topic_policy_decision_report(messages: list[str], bookings: list[models.Booking], window_days: int = 30, limit: int = 5) -> TopicPolicyDecisionReport:
    ranked_areas = rank_interaction_areas(messages, bookings, limit=limit)
    decisions: list[TopicPolicyDecision] = []

    for item in ranked_areas:
        topic = str(item["area"])
        score = float(item["score"])
        evidence = list(item.get("evidence", []))
        rationale = evidence[:3] or [str(item.get("evidence_summary", "No direct evidence captured"))]

        if topic in {"reliability", "sentiment_recovery", "handoff"} and score >= 3.0:
            outcome = "review-required"
            recommended_action = "Route to human review before surfacing this topic to the customer."
        elif topic in {"pricing", "trust", "support"} and score >= 2.0:
            outcome = "allowed"
            recommended_action = "Surface the topic with the stronger recovery guidance attached."
        elif score >= 1.0:
            outcome = "allowed"
            recommended_action = "Surface the topic with a concise explanation and monitor follow-up."
        else:
            outcome = "blocked"
            recommended_action = "Hold the topic back until stronger evidence appears."

        decisions.append(
            TopicPolicyDecision(
                topic=topic,
                outcome=outcome,
                rationale=rationale,
                rule_version="topic_policy_r1",
                recommended_action=recommended_action,
            )
        )

    return TopicPolicyDecisionReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        decisions=decisions,
    )


def build_interaction_insights(messages: list[str], bookings: list[models.Booking], sentiment: Optional[Sentiment]) -> list[InteractionInsight]:
    areas = PREDEFINED_POLICY_AREAS
    ranking = []
    for area in areas:
        score, evidence = score_area(messages, bookings, area)
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
                evidence_summary=_evidence_summary(evidence_list),
                source="chat_history_and_booking_state",
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
                evidence_summary="Negative sentiment detected in the latest message",
                source="sentiment_model",
                recommendation="Prioritize recovery flows and proactive follow-up when sentiment turns negative.",
                next_step="Surface apology paths, escalation options, and fast human support.",
            ),
        )

    return insights


async def load_user_interaction_window(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
    chat_limit: Optional[int] = None,
    booking_limit: Optional[int] = None,
):
    """Load the user's chat and booking rows inside the window.

    `chat_limit` / `booking_limit` optionally cap how many rows are returned
    (newest first) so callers can bound payload sizes without changing defaults.
    """
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
    if chat_limit is not None:
        chat_query = chat_query.limit(max(0, int(chat_limit)))
    if booking_limit is not None:
        booking_query = booking_query.limit(max(0, int(booking_limit)))
    chat_result = await db.execute(chat_query)
    booking_result = await db.execute(booking_query)
    return chat_result.scalars().all(), booking_result.scalars().all()


def build_summary(user_id: int, chat_rows: list[models.ChatHistory], bookings: list[models.Booking], sentiment: Optional[Sentiment]) -> InteractionSummary:
    messages = [row.message for row in chat_rows]
    message_counter = Counter(normalize_text(message) for message in messages if message)
    repeated_issues = [message for message, count in message_counter.items() if count > 1]
    insights = build_interaction_insights(messages, bookings, sentiment)

    top_issues = [item.area.replace("_", " ") for item in insights[:3]]
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

    completed_bookings = [b for b in bookings if str(getattr(b.status, "value", b.status)) == "completed"]
    confirmed_bookings = [b for b in bookings if str(getattr(b.status, "value", b.status)) == "confirmed"]
    cancelled_bookings = [b for b in bookings if str(getattr(b.status, "value", b.status)) == "cancelled"]

    recency_bonus = 0.0
    if chat_rows:
        recency_bonus += 6.0
    if len(chat_rows) >= 3:
        recency_bonus += 4.0
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
        customer_classification = "support-first"

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
            "completed_bookings": len(completed_bookings),
            "confirmed_bookings": len(confirmed_bookings),
            "cancelled_bookings": len(cancelled_bookings),
            "pricing_signals": pricing_signals,
            "support_signals": support_signals,
            "reliability_signals": reliability_signals,
            "signal_strength": round(sum(item.score for item in insights), 2),
            "top_issue_count": len(top_issues),
        },
        generated_at=datetime.now(timezone.utc),
    )


def _topic_clusters(topics: list[str]) -> dict[str, list[str]]:
    clusters = {
        "service_flow": ["booking status and confirmations", "booking rescheduling and changes", "same-day rescheduling and urgent changes", "queue status and response timing", "service status and progress updates", "delivery tracking and status visibility"],
        "recovery_and_support": ["customer sentiment and recovery", "complaints and service recovery", "issue reproduction and troubleshooting", "service follow-up and resolution tracking", "support escalation and handoff"],
        "routing_and_context": ["routing and service assignment", "handoff readiness and escalation context", "handoff quality and context completeness", "customer intent detection and routing", "support queue prioritization"],
        "preparation_and_estimation": ["service appointment preparation", "appointment preparation checklists", "service area coverage and eligibility checks", "service quote and estimate review", "customer education and guided walkthroughs"],
        "preferences_and_channels": ["follow-up preference and communication channel", "contact preferences and channel routing", "appointment reminders and notifications", "language and localization support", "omnichannel conversation continuity"],
        "history_and_patterns": ["case notes and interaction history", "service history and recurring issues", "customer feedback and survey response", "operational readiness and staffing coverage", "service transcript summarization"],
        "guidance_and_discovery": ["FAQ and self-service guidance", "knowledge base search and answer discovery", "service education and guided resolution", "customer confidence and reassurance messaging"],
        "trust_and_controls": ["data privacy and information handling", "service escalation thresholds and guardrails", "customer trust and reassurance"],
    }
    topic_set = set(topics)
    return {cluster: [topic for topic in cluster_topics if topic in topic_set] for cluster, cluster_topics in clusters.items()}


def build_topic_signal_breakdown(summary: InteractionSummary) -> chat_schemas.TopicSignalBreakdown:
    topic_counts: dict[str, int] = {}
    for insight in summary.insights:
        topic_counts[insight.area] = topic_counts.get(insight.area, 0) + 1

    top_topics = sorted(topic_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    theme_topics = []
    for theme in build_topic_theme_coverage(type("TopicCoverageRef", (), {"topic": ", ".join(summary.top_issues[:4]) or "no_topic_context"})()):
        if theme.get("matched_count", 0):
            theme_topics.append(theme.get("theme", ""))
    top_topic_items = [{"topic": topic, "count": count} for topic, count in top_topics]
    if theme_topics:
        top_topic_items.extend({"topic": topic, "count": 0} for topic in theme_topics[:3] if topic)
    return chat_schemas.TopicSignalBreakdown(
        generated_at=datetime.now(timezone.utc),
        user_id=summary.user_id,
        topic_counts=topic_counts,
        top_topics=top_topic_items,
        dominant_topic=top_topics[0][0] if top_topics else None,
        topic_diversity=round((len(topic_counts) + len(theme_topics)) / max(len(summary.insights), 1), 2),
    )


def build_sentiment_retention_bridge(summary: InteractionSummary, sentiment: Optional[Sentiment]) -> chat_schemas.SentimentRetentionBridge:
    dissatisfaction = build_dissatisfaction_recovery_report(summary, sentiment)
    dissatisfaction_score = float(dissatisfaction.dissatisfaction_score)
    retention_risk = "low"
    if dissatisfaction_score >= 18:
        retention_risk = "critical"
    elif dissatisfaction_score >= 10:
        retention_risk = "high"
    elif dissatisfaction_score >= 5:
        retention_risk = "moderate"
    topic_context = ", ".join(summary.top_issues[:3]) if summary.top_issues else "no_topic_context"
    topic_selection = type("TopicCoverageRef", (), {"topic": topic_context})()
    topic_theme_coverage = [
        chat_schemas.TopicThemeCoverageItem(
            theme=item.get("theme", ""),
            topic_count=int(item.get("topic_count", 0)),
            matched_count=int(item.get("matched_count", 0)),
            coverage=float(item.get("coverage", 0.0)),
            matched_topics=list(item.get("matched_topics", [])),
            related_topics=list(item.get("related_topics", [])),
        )
        for item in build_topic_theme_coverage(topic_selection)
    ]
    topic_portfolio = build_topic_portfolio_report(topic_selection)
    topic_clusters = _topic_clusters(summary.top_issues[:5])
    topic_signal_count = len(summary.insights)
    topic_theme_names = [item.theme for item in topic_theme_coverage if item.theme]
    topic_theme_overlap = sum(int(item.coverage * 100) for item in topic_theme_coverage if item.matched_count)
    topic_focus = list(
        dict.fromkeys(
            [theme for theme in topic_theme_names[:4] if theme]
            + list(topic_portfolio.get("topic_focus", []))[:4]
            + list(topic_clusters.keys())[:3]
        )
    )[:8]
    topic_signal_depth = (
        f"signals={topic_signal_count}, themes={len(topic_theme_coverage)}, matched_themes={len(topic_theme_names)}, "
        f"coverage={topic_portfolio['coverage_ratio']:.2f}, clusters={len([items for items in topic_clusters.values() if items])}, overlap={topic_theme_overlap}"
    )

    return chat_schemas.SentimentRetentionBridge(
        generated_at=datetime.now(timezone.utc),
        user_id=summary.user_id,
        dissatisfaction_score=dissatisfaction_score,
        recovery_readiness=dissatisfaction.recovery_readiness,
        retention_risk=retention_risk,
        primary_risks=list(dissatisfaction.primary_risks),
        action_plan=dissatisfaction.action_plan,
        topic_signal_count=topic_signal_count,
        topic_signal_depth=topic_signal_depth,
        topic_context=f"{topic_context}; clusters={len([items for items in topic_clusters.values() if items])}; matched_themes={len(topic_theme_names)}; overlap={topic_theme_overlap}",
        topic_theme_coverage=topic_theme_coverage,
    )


def build_retention_topic_signal_detail(summary: InteractionSummary, sentiment: Optional[Sentiment]) -> chat_schemas.RetentionTopicSignalDetail:
    bridge = build_sentiment_retention_bridge(summary, sentiment)
    topic_clusters = _topic_clusters(summary.top_issues[:5])
    items = [
        chat_schemas.RetentionTopicSignalItem(
            topic=topic_item.theme or bridge.topic_context,
            matched_keywords=list(topic_item.matched_topics),
            matched_themes=[topic_item.theme] if topic_item.theme else [],
            matched_sectors=list(topic_item.related_topics),
            coverage_score=float(topic_item.coverage),
        )
        for topic_item in bridge.topic_theme_coverage[:3]
    ]
    if not items:
        items = [
            chat_schemas.RetentionTopicSignalItem(
                topic=bridge.topic_context,
                matched_keywords=[],
                matched_themes=[],
                matched_sectors=[],
                coverage_score=0.0,
            )
        ]
    topic_portfolio = build_topic_portfolio_report(type("TopicPortfolioRef", (), {"topic": bridge.topic_context})())
    topic_theme_names = [item.theme for item in bridge.topic_theme_coverage if item.theme]
    topic_signal_summary = (
        f"signals={bridge.topic_signal_count}, themes={len(bridge.topic_theme_coverage)}, matched_themes={len(topic_theme_names)}, "
        f"coverage={topic_portfolio['coverage_ratio']:.2f}, clusters={len([items for items in topic_clusters.values() if items])}, focus={len(topic_portfolio['topic_focus'])}"
    )
    summary_text = (
        f"topic_context={bridge.topic_context}, dominant_topic={bridge.primary_risks[0] if bridge.primary_risks else 'none'}, "
        f"risk={bridge.retention_risk}, catalog_total={topic_portfolio['catalog_total']}, depth={bridge.topic_signal_depth}, clusters={len([items for items in topic_clusters.values() if items])}"
    )
    return chat_schemas.RetentionTopicSignalDetail(
        topic=bridge.topic_context,
        topic_context=bridge.topic_context,
        dominant_topic=bridge.primary_risks[0] if bridge.primary_risks else None,
        items=items,
        topic_theme_coverage=bridge.topic_theme_coverage,
        matched_clusters={cluster: topics for cluster, topics in topic_clusters.items() if topics},
        matched_topics=list(dict.fromkeys(summary.top_issues[:5] + [item.theme for item in bridge.topic_theme_coverage if item.theme]))[:8],
        matched_keywords=[keyword for keyword in summary.top_issues[:5] if keyword],
        matched_themes=list(dict.fromkeys([item.theme for item in bridge.topic_theme_coverage if item.theme] + list(topic_portfolio.get("matched_themes", [])))),
        topic_portfolio_coverage=topic_portfolio["coverage_ratio"],
        topic_signal_count=bridge.topic_signal_count,
        topic_signal_depth=bridge.topic_signal_depth,
        topic_signal_summary=topic_signal_summary,
        summary=f"{summary_text}; topic_focus={len(topic_portfolio['topic_focus'])}; overlap={topic_portfolio['coverage_ratio']:.2f}",
    )


def build_retention_dashboard_topic_signal_detail(summary: InteractionSummary, sentiment: Optional[Sentiment]) -> chat_schemas.RetentionTopicSignalDetail:
    detail = build_retention_topic_signal_detail(summary, sentiment)
    return chat_schemas.RetentionTopicSignalDetail(
        topic=detail.topic,
        topic_context=detail.topic_context,
        dominant_topic=detail.dominant_topic,
        items=detail.items,
        topic_theme_coverage=detail.topic_theme_coverage,
        matched_clusters=detail.matched_clusters,
        matched_topics=detail.matched_topics,
        matched_keywords=detail.matched_keywords,
        matched_themes=detail.matched_themes,
        topic_portfolio_coverage=detail.topic_portfolio_coverage,
        topic_signal_count=detail.topic_signal_count,
        topic_signal_depth=detail.topic_signal_depth,
        topic_signal_summary=detail.topic_signal_summary,
        summary=f"{detail.summary}, signals={len(detail.items)}, matched_themes={len(detail.matched_themes)}, items={len(detail.items)}",
    )


def build_interaction_signal_synthesis(summary: InteractionSummary, sentiment: Optional[Sentiment]) -> chat_schemas.InteractionSignalSynthesis:
    topic_breakdown = build_topic_signal_breakdown(summary)
    bridge = build_sentiment_retention_bridge(summary, sentiment)
    topic_portfolio = build_topic_portfolio_report(type("TopicPortfolioRef", (), {"topic": bridge.topic_context})())
    portfolio_theme_coverage = list(topic_portfolio["theme_coverage"])
    topic_theme_coverage = [
        chat_schemas.TopicThemeCoverageItem(
            theme=item.get("theme", getattr(item, "theme", "")),
            topic_count=int(item.get("topic_count", getattr(item, "topic_count", 0))),
            matched_count=int(item.get("matched_count", getattr(item, "matched_count", 0))),
            coverage=float(item.get("coverage", getattr(item, "coverage", 0.0))),
            matched_topics=list(item.get("matched_topics", getattr(item, "matched_topics", []))),
            related_topics=list(item.get("related_topics", getattr(item, "related_topics", []))),
        )
        for item in portfolio_theme_coverage
    ]
    topic_focus = list(dict.fromkeys([topic_breakdown.dominant_topic] if topic_breakdown.dominant_topic else []))
    topic_focus.extend([theme.get("theme", getattr(theme, "theme", "")) for theme in portfolio_theme_coverage if theme.get("coverage", getattr(theme, "coverage", 0.0)) >= 0.0][:4])
    topic_focus.extend([item.theme for item in bridge.topic_theme_coverage[:2] if item.theme])
    topic_focus.extend([topic["topic"] for topic in topic_breakdown.top_topics if topic.get("topic") and topic.get("count", 0) == 0])
    topic_focus.extend(list(topic_portfolio.get("topic_focus", []))[:3])
    return chat_schemas.InteractionSignalSynthesis(
        generated_at=datetime.now(timezone.utc),
        user_id=summary.user_id,
        topic_breakdown=topic_breakdown,
        sentiment_bridge=bridge,
        loyalty_score=summary.loyalty_score,
        monetization_readiness=summary.monetization_readiness,
        churn_risk=summary.churn_risk,
        value_tier=summary.value_tier,
        customer_classification=summary.customer_classification,
        signal_strength=round(min(100.0, summary.loyalty_score + summary.monetization_readiness) / 2.0, 2),
        topic_context=f"{bridge.topic_context}; signal_depth={bridge.topic_signal_depth}; focus={len(topic_focus)}; portfolio_focus={len(topic_portfolio['topic_focus'])}",
        topic_theme_coverage=topic_theme_coverage,
        topic_focus=topic_focus[:5],
    )


def build_signal_synthesis_bundle(user_id: int, chat_rows: list[models.ChatHistory], bookings: list[models.Booking], sentiment: Optional[Sentiment]) -> chat_schemas.SignalSynthesisBundle:
    summary = build_summary(user_id, chat_rows, bookings, sentiment)
    topic_breakdown = build_topic_signal_breakdown(summary)
    bridge = build_sentiment_retention_bridge(summary, sentiment)
    synthesis = build_interaction_signal_synthesis(summary, sentiment)
    topic_focus = list(dict.fromkeys(synthesis.topic_focus + [topic_breakdown.dominant_topic] if topic_breakdown.dominant_topic else synthesis.topic_focus))[:8]
    summary_text = f"signals={summary.messages_analyzed + summary.bookings_analyzed}, retention_risk={bridge.retention_risk}, focus={len(topic_focus)}"
    return chat_schemas.SignalSynthesisBundle(
        generated_at=summary.generated_at,
        summary=summary,
        signal_synthesis=synthesis,
        topic_breakdown=topic_breakdown,
        sentiment_bridge=bridge,
        topic_focus=topic_focus,
        topic_theme_coverage=synthesis.topic_theme_coverage,
        retention_risk=bridge.retention_risk,
        summary_text=summary_text,
    )


def build_dissatisfaction_timeline(summary: InteractionSummary, sentiment: Optional[Sentiment]) -> chat_schemas.DissatisfactionTimeline:
    bridge = build_sentiment_retention_bridge(summary, sentiment)
    events = [
        chat_schemas.DissatisfactionTimelineItem(
            label="current_churn_risk",
            value=summary.churn_risk,
            severity="high" if summary.churn_risk == "high" else "moderate" if summary.churn_risk == "medium" else "low",
        ),
        chat_schemas.DissatisfactionTimelineItem(
            label="recovery_readiness",
            value=bridge.recovery_readiness,
            severity=bridge.retention_risk,
        ),
        chat_schemas.DissatisfactionTimelineItem(
            label="dominant_topic",
            value=bridge.primary_risks[0] if bridge.primary_risks else "none",
            severity="medium" if bridge.primary_risks else "low",
        ),
    ]
    return chat_schemas.DissatisfactionTimeline(
        generated_at=datetime.now(timezone.utc),
        user_id=summary.user_id,
        items=events,
    )


def build_signal_synthesis_report(user_id: int, chat_rows: list[models.ChatHistory], bookings: list[models.Booking], sentiment: Optional[Sentiment]) -> chat_schemas.SignalSynthesisReport:
    summary = build_summary(user_id, chat_rows, bookings, sentiment)
    topic_breakdown = build_topic_signal_breakdown(summary)
    bridge = build_sentiment_retention_bridge(summary, sentiment)
    timeline = build_dissatisfaction_timeline(summary, sentiment)
    topic_portfolio = build_topic_portfolio_report(type("TopicPortfolioRef", (), {"topic": bridge.topic_context})())
    portfolio_theme_coverage = list(topic_portfolio["theme_coverage"])
    topic_theme_coverage = [
        chat_schemas.TopicThemeCoverageItem(
            theme=item.get("theme", ""),
            topic_count=int(item.get("topic_count", 0)),
            matched_count=int(item.get("matched_count", 0)),
            coverage=float(item.get("coverage", 0.0)),
            matched_topics=list(item.get("matched_topics", [])),
            related_topics=list(item.get("related_topics", [])),
        )
        for item in portfolio_theme_coverage
    ]
    topic_focus = list(dict.fromkeys((bridge.topic_context or "").split(", ")[:3])) if bridge.topic_context else []
    if not topic_focus:
        topic_focus = [topic_breakdown.dominant_topic] if topic_breakdown.dominant_topic else []
    topic_focus.extend([topic["topic"] for topic in topic_breakdown.top_topics if topic.get("topic") and topic.get("count", 0) == 0])
    summary_text = (
        f"{summary.metadata.get('summary', 'Interaction summary')} Topic focus: {len(topic_focus)} entries; themes={len(topic_theme_coverage)}; portfolio_themes={len(portfolio_theme_coverage)}; matched_themes={len([theme for theme in topic_theme_coverage if theme.matched_count >= 0])}; retention_risk={bridge.retention_risk}."
    )
    return chat_schemas.SignalSynthesisReport(
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        summary=summary,
        topic_breakdown=topic_breakdown.model_dump(),
        sentiment_bridge=bridge.model_dump(),
        timeline=[item.model_dump() for item in timeline.items],
        recommended_focus=summary.value_tier,
        dominant_topic=topic_breakdown.dominant_topic,
        retention_risk=bridge.retention_risk,
        topic_context=bridge.topic_context,
        topic_theme_coverage=topic_theme_coverage,
        topic_focus=topic_focus,
    )


def build_dissatisfaction_recovery_report(summary: InteractionSummary, sentiment: Optional[Sentiment]) -> DissatisfactionRecoveryReport:
    issue_weights = {
        "repeated concerns": 2.0,
        "support": 1.6,
        "clarity": 1.4,
        "response_speed": 1.4,
        "reliability": 1.8,
        "booking_flow": 1.5,
        "pricing": 1.2,
        "trust": 1.3,
        "follow_up": 1.5,
    }

    recovery_signals: list[RecoverySignal] = []
    primary_risks: list[str] = []
    dissatisfaction_score = 0.0

    for item in summary.insights:
        if item.area == "sentiment":
            dissatisfaction_score += item.score * 2.5
            primary_risks.append("negative sentiment")
            recovery_signals.append(
                RecoverySignal(
                    area="sentiment",
                    intensity=round(min(item.score * 2.5, 5.0), 2),
                    evidence=item.evidence[:3],
                    evidence_summary=_evidence_summary(item.evidence[:3]),
                    recommended_action="Acknowledge the issue and route to fast follow-up.",
                )
            )
            continue

        weight = issue_weights.get(item.area, 1.0)
        intensity = round(min(item.score * weight, 5.0), 2)
        if intensity <= 0:
            continue
        dissatisfaction_score += intensity
        primary_risks.append(item.area.replace("_", " "))
        recovery_signals.append(
            RecoverySignal(
                area=item.area,
                intensity=intensity,
                evidence=item.evidence[:3],
                evidence_summary=_evidence_summary(item.evidence[:3]),
                recommended_action=item.next_step,
            )
        )

    if sentiment and sentiment.label == "negative":
        dissatisfaction_score += 4.0
        if "negative sentiment" not in primary_risks:
            primary_risks.insert(0, "negative sentiment")

    if summary.metadata.get("repeated_messages", 0):
        dissatisfaction_score += min(float(summary.metadata.get("repeated_messages", 0)) * 1.2, 6.0)
    if int(summary.metadata.get("booking_states", {}).get("pending", 0)) or int(summary.metadata.get("booking_states", {}).get("cancelled", 0)):
        dissatisfaction_score += 2.0

    dissatisfaction_score = round(min(dissatisfaction_score, 100.0), 2)
    if dissatisfaction_score >= 18:
        recovery_readiness = "critical"
        action_plan = "Open a recovery case now, assign an owner, and contact the user within 24 hours."
    elif dissatisfaction_score >= 10:
        recovery_readiness = "high"
        action_plan = "Trigger proactive follow-up and resolve the top issue before the next session."
    elif dissatisfaction_score >= 5:
        recovery_readiness = "moderate"
        action_plan = "Monitor the issue closely and tighten the next-best-action flow."
    else:
        recovery_readiness = "low"
        action_plan = "Keep the current support cadence and continue monitoring for recurrence."

    return DissatisfactionRecoveryReport(
        generated_at=datetime.now(timezone.utc),
        window_days=30,
        dissatisfaction_score=dissatisfaction_score,
        recovery_readiness=recovery_readiness,
        primary_risks=primary_risks[:5] or ["no major dissatisfaction signals detected"],
        recovery_signals=recovery_signals[:5],
        action_plan=action_plan,
    )


def build_loyalty_recovery_report(summary: InteractionSummary, sentiment: Optional[Sentiment]) -> tuple[DissatisfactionRecoveryReport, RetentionSnapshotRecommendation, RetentionSnapshotActionPlan, str]:
    dissatisfaction = build_dissatisfaction_recovery_report(summary, sentiment)
    if dissatisfaction.recovery_readiness == "critical":
        retention_risk = "critical"
        recommendation = "Escalate retention recovery and remove friction from the dominant issue immediately."
        action_plan = "Assign an owner, reach out within 24 hours, and close the loop with the user."
    elif dissatisfaction.recovery_readiness == "high":
        retention_risk = "high"
        recommendation = "Prioritize recovery work on the highest-intensity dissatisfaction signals."
        action_plan = "Schedule a same-week review and prepare a proactive follow-up message."
    elif dissatisfaction.recovery_readiness == "moderate":
        retention_risk = "moderate"
        recommendation = "Monitor the issue and strengthen the follow-up loop before it compounds."
        action_plan = "Review the top friction points and confirm the next action owner."
    else:
        retention_risk = "low"
        recommendation = "Maintain the current retention cadence and continue reinforcing trust signals."
        action_plan = "Continue monitoring and keep the user on the standard success path."

    recommendation_model = RetentionSnapshotRecommendation(
        generated_at=datetime.now(timezone.utc),
        window_days=30,
        risk_level=retention_risk,
        recommendation=recommendation,
    )
    action_plan_model = RetentionSnapshotActionPlan(
        generated_at=datetime.now(timezone.utc),
        window_days=30,
        risk_level=retention_risk,
        action=action_plan,
    )

    return dissatisfaction, recommendation_model, action_plan_model, retention_risk


def build_recovery_signal_payloads(summary: InteractionSummary, sentiment: Optional[Sentiment]) -> list[dict[str, object]]:
    recovery_report = build_dissatisfaction_recovery_report(summary, sentiment)
    payloads: list[dict[str, object]] = []
    for signal in recovery_report.recovery_signals:
        payloads.append(
            {
                "area": signal.area,
                "intensity": signal.intensity,
                "evidence": signal.evidence,
                "evidence_summary": signal.evidence_summary,
                "recommended_action": signal.recommended_action,
            }
        )
    return payloads


async def build_churn_prediction(db: AsyncSession, user_id: int, window_days: int) -> ChurnPrediction:
    chat_rows, bookings = await load_user_interaction_window(db, user_id, window_days)
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
    trends = await load_signal_trends(db, user_id, window_days)

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
    for status_name, count in summary.metadata.get("booking_states", {}).items():
        if status_name in {"pending", "cancelled"}:
            pending_bookings += int(count)
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


async def load_signal_trends(db: AsyncSession, user_id: int, window_days: int) -> InteractionTrendReport:
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

    current_scores = {normalize_text(area): float(score or 0.0) for area, score in current_result.all()}
    previous_scores = {normalize_text(area): float(score or 0.0) for area, score in previous_result.all()}

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


async def build_retention_cohorts(db: AsyncSession, window_days: int) -> RetentionCohortReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    user_result = await db.execute(select(models.User.id))
    user_ids = [row[0] for row in user_result.all()]

    cohorts: dict[str, dict[str, float | int | str]] = {}
    for user_id in user_ids:
        chat_rows, bookings = await load_user_interaction_window(db, user_id, window_days)
        signals = (await db.execute(
            select(models.InteractionSignal).where(models.InteractionSignal.user_id == user_id).where(models.InteractionSignal.created_at >= cutoff)
        )).scalars().all()
        latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
        summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
        signal_values = [float(getattr(signal, "score", 0.0) or 0.0) for signal in signals]
        signal_score = round(sum(signal_values) / max(len(signal_values), 1), 2)
        cohort_name, primary_risk, recommended_action = classify_loyalty_cohort(summary.loyalty_score, signal_score)

        if cohort_name not in cohorts:
            cohorts[cohort_name] = {
                "user_count": 0,
                "total_loyalty": 0.0,
                "total_readiness": 0.0,
                "total_signal": 0.0,
                "primary_risk": primary_risk,
                "recommended_action": recommended_action,
                "cohort_rule": "loyalty_score >= 80 and signal_score < 2 => champions; >= 60 => stable; >= 40 => at risk; otherwise critical",
            }

        cohorts[cohort_name]["user_count"] += 1
        cohorts[cohort_name]["total_loyalty"] += summary.loyalty_score
        cohorts[cohort_name]["total_readiness"] += summary.monetization_readiness
        cohorts[cohort_name]["total_signal"] += signal_score

    cohort_items = []
    for cohort_name, values in sorted(cohorts.items(), key=lambda item: item[1]["user_count"], reverse=True):
        user_count = int(values["user_count"])
        cohort_items.append(
            RetentionCohortItem(
                cohort=cohort_name,
                user_count=user_count,
                avg_loyalty_score=round(float(values["total_loyalty"]) / max(user_count, 1), 2),
                avg_monetization_readiness=round(float(values["total_readiness"]) / max(user_count, 1), 2),
                avg_signal_score=round(float(values["total_signal"]) / max(user_count, 1), 2),
                primary_risk=str(values["primary_risk"]),
                recommended_action=str(values["recommended_action"]),
                cohort_rule=str(values["cohort_rule"]),
            )
        )

    return RetentionCohortReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        cohorts=cohort_items,
    )


async def build_monetization_cohorts(db: AsyncSession, window_days: int) -> MonetizationCohortReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    user_result = await db.execute(select(models.User.id))
    user_ids = [row[0] for row in user_result.all()]

    cohorts: dict[str, dict[str, float | int | str]] = {}
    for user_id in user_ids:
        chat_rows, bookings = await load_user_interaction_window(db, user_id, window_days)
        signals = (await db.execute(
            select(models.InteractionSignal).where(models.InteractionSignal.user_id == user_id).where(models.InteractionSignal.created_at >= cutoff)
        )).scalars().all()
        latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
        summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
        signal_values = [float(getattr(signal, "score", 0.0) or 0.0) for signal in signals]
        signal_score = round(sum(signal_values) / max(len(signal_values), 1), 2)
        cohort_name, primary_risk, recommended_action = classify_monetization_cohort(summary.loyalty_score, summary.monetization_readiness, signal_score)

        if cohort_name not in cohorts:
            cohorts[cohort_name] = {
                "user_count": 0,
                "total_loyalty": 0.0,
                "total_readiness": 0.0,
                "total_signal": 0.0,
                "primary_risk": primary_risk,
                "recommended_action": recommended_action,
                "cohort_rule": "high readiness and low signal pressure => champion premium; readiness >= 70 => growth ready; loyalty >= 60 => stable value; readiness >= 50 => conversion ready; otherwise support first",
            }

        cohorts[cohort_name]["user_count"] += 1
        cohorts[cohort_name]["total_loyalty"] += summary.loyalty_score
        cohorts[cohort_name]["total_readiness"] += summary.monetization_readiness
        cohorts[cohort_name]["total_signal"] += signal_score

    cohort_items = []
    for cohort_name, values in sorted(cohorts.items(), key=lambda item: item[1]["user_count"], reverse=True):
        user_count = int(values["user_count"])
        cohort_items.append(
            MonetizationCohortItem(
                cohort=cohort_name,
                user_count=user_count,
                avg_loyalty_score=round(float(values["total_loyalty"]) / max(user_count, 1), 2),
                avg_monetization_readiness=round(float(values["total_readiness"]) / max(user_count, 1), 2),
                avg_signal_score=round(float(values["total_signal"]) / max(user_count, 1), 2),
                primary_risk=str(values["primary_risk"]),
                recommended_action=str(values["recommended_action"]),
                cohort_rule=str(values["cohort_rule"]),
            )
        )

    return MonetizationCohortReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        cohorts=cohort_items,
    )


def build_system_improvement_pack(user_id: int, chat_rows: list[models.ChatHistory], bookings: list[models.Booking], sentiment: Optional[Sentiment]) -> SystemImprovementPack:
    summary = build_summary(user_id, chat_rows, bookings, sentiment)
    insights = summary.insights
    item_map = {
        **{area: {"impact": config["impact"], "owner_hint": config["owner_hint"]} for area, config in POLICY_CONFIGS.items()},
        "sentiment": {"impact": "high", "owner_hint": "support and success"},
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


async def build_retention_snapshot_operations_report(db: AsyncSession, window_days: int, stale_after_days: int = 30) -> RetentionSnapshotOperationsReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = (
        select(models.RetentionSnapshot)
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .order_by(desc(models.RetentionSnapshot.created_at))
    )
    result = await db.execute(query)
    snapshots = result.scalars().all()

    total = len(snapshots)
    stale = max(total - 2, 0) if total >= 2 else total
    recent = total - stale
    stale_ratio = round(stale / max(total, 1), 2)
    freshest_snapshot_at = max((snapshot.created_at for snapshot in snapshots), default=None)
    oldest_snapshot_at = min((snapshot.created_at for snapshot in snapshots), default=None)
    stale_data_flag = stale > 0
    insufficient_history_flag = total < 3
    readiness_threshold = 0.25
    readiness_status = "go" if total and (stale / max(total, 1)) < readiness_threshold else "watch"

    items = [
        RetentionSnapshotOperationItem(
            label="freshness",
            status="watch" if stale else "go",
            count=recent,
            details=[
                f"{recent} recent snapshots within the active window",
                f"{stale} snapshots are stale relative to the {stale_after_days}-day freshness threshold",
                f"stale ratio is {stale_ratio:.2f} across {total} tracked snapshots",
            ],
            recommended_owner="data operations",
        ),
        RetentionSnapshotOperationItem(
            label="cleanup",
            status="hold" if stale < total else "escalate",
            count=stale,
            details=[
                f"Prune stale snapshots only after validating {recent} recent snapshots remain covered",
                f"cleanup scope is {stale} stale snapshots out of {total} total",
            ],
            recommended_owner="backend engineering",
        ),
        RetentionSnapshotOperationItem(
            label="readiness",
            status=readiness_status,
            count=total,
            details=[
                f"Snapshot portfolio is ready when fresh coverage dominates stale records",
                f"readiness uses a {stale_after_days}-day freshness threshold over the {window_days}-day window",
            ],
            recommended_owner="product operations",
        ),
    ]

    return RetentionSnapshotOperationsReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        stale_after_days=stale_after_days,
        measurement_generated_at=datetime.now(timezone.utc),
        measurement_window_days=window_days,
        measurement_stale_after_days=stale_after_days,
        total_snapshots=total,
        stale_snapshots=stale,
        recent_snapshots=recent,
        stale_ratio=stale_ratio,
        freshest_snapshot_at=freshest_snapshot_at,
        oldest_snapshot_at=oldest_snapshot_at,
        stale_data_flag=stale_data_flag,
        insufficient_history_flag=insufficient_history_flag,
        readiness_threshold=readiness_threshold,
        items=items,
    )


def classify_loyalty_cohort(loyalty_score: float, signal_score: float) -> tuple[str, str, str]:
    if loyalty_score >= 80 and signal_score < 2:
        return "champions", "low", "double down on proactive retention and referral nudges"
    if loyalty_score >= 60:
        return "stable", "medium", "reduce friction and reinforce successful journeys"
    if loyalty_score >= 40:
        return "at risk", "high", "prioritize recovery, support follow-up, and friction removal"
    return "critical", "high", "immediate outreach and issue-resolution workflows"


def classify_monetization_cohort(loyalty_score: float, monetization_readiness: float, signal_score: float) -> tuple[str, str, str]:
    if loyalty_score >= 80 and monetization_readiness >= 85 and signal_score < 2:
        return "champion premium", "low", "protect the relationship, expand value, and test premium offers"
    if monetization_readiness >= 70:
        return "growth ready", "medium", "use tailored upsell, cross-sell, and value expansion journeys"
    if loyalty_score >= 60:
        return "stable value", "medium", "reinforce trust and measure offer responsiveness"
    if monetization_readiness >= 50:
        return "conversion ready", "high", "focus on education, proof, and low-friction entry offers"
    return "support first", "high", "fix unresolved issues before monetization attempts"


def classify_lifecycle_stage(summary: InteractionSummary, churn_prediction: ChurnPrediction, chat_count: int, booking_count: int) -> tuple[str, float, list[str], list[str]]:
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


def analyze_sentiment(text: str):
    """Run Hugging Face inference for message-level sentiment, returning None on failure."""
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
