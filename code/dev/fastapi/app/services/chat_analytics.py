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


def normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def score_area(messages: list[str], bookings: list[models.Booking], area: str) -> tuple[float, list[str]]:
    evidence: list[str] = []
    score = 0.0
    recent_messages = messages[:3]
    for message in messages:
        normalized = normalize_text(message)
        if area == "response_speed" and any(word in normalized for word in ["slow", "delay", "wait"]):
            score += 1.2 if message in recent_messages else 0.9
            evidence.append(message)
        elif area == "clarity" and any(word in normalized for word in ["confusing", "unclear", "hard"]):
            score += 1.1 if message in recent_messages else 0.8
            evidence.append(message)
        elif area == "reliability" and any(word in normalized for word in ["bug", "error", "issue"]):
            score += 1.3 if message in recent_messages else 1.0
            evidence.append(message)
        elif area == "booking_flow" and any(word in normalized for word in ["cancel", "book", "schedule"]):
            score += 1.0 if message in recent_messages else 0.8
            evidence.append(message)
        elif area == "support" and any(word in normalized for word in ["help", "support"]):
            score += 0.9 if message in recent_messages else 0.7
            evidence.append(message)
        elif area == "pricing" and any(word in normalized for word in ["price", "expensive"]):
            score += 0.9 if message in recent_messages else 0.7
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
    if area == "sentiment_recovery" and messages:
        negative_terms = [word for word in ["angry", "frustrated", "upset", "disappointed", "bad"] if any(word in normalize_text(message) for message in messages)]
        score += min(len(negative_terms) * 0.8, 2.4)
        if negative_terms:
            evidence.append("negative sentiment language detected in recent messages")
    if area == "onboarding" and messages:
        onboarding_terms = [word for word in ["how do i", "new here", "getting started", "first time", "setup"] if any(word in normalize_text(message) for message in messages)]
        score += min(len(onboarding_terms) * 0.7, 2.1)
        if onboarding_terms:
            evidence.append("onboarding guidance requests detected")
    if area == "notification_quality" and messages:
        notification_terms = [word for word in ["notify", "notification", "alert", "remind", "reminder"] if any(word in normalize_text(message) for message in messages)]
        score += min(len(notification_terms) * 0.6, 1.8)
        if notification_terms:
            evidence.append("notification and reminder language detected")
    if area == "self_service" and messages:
        self_service_terms = [word for word in ["faq", "self service", "self-service", "how to", "help center"] if any(word in normalize_text(message) for message in messages)]
        score += min(len(self_service_terms) * 0.6, 1.8)
        if self_service_terms:
            evidence.append("self-service questions detected")
    if area == "handoff" and messages:
        handoff_terms = [word for word in ["agent", "transfer", "human", "someone", "escalate"] if any(word in normalize_text(message) for message in messages)]
        score += min(len(handoff_terms) * 0.6, 1.8)
        if handoff_terms:
            evidence.append("handoff and escalation language detected")
    if area == "trust" and messages:
        trust_terms = [word for word in ["trust", "refund", "guarantee", "secure", "safe"] if any(word in normalize_text(message) for message in messages)]
        score += min(len(trust_terms) * 0.5, 1.5)
        if trust_terms:
            evidence.append("trust-related concerns detected")
    if area == "follow_up" and messages:
        follow_up_terms = [word for word in ["follow up", "follow-up", "still waiting", "again", "remind"] if any(word in normalize_text(message) for message in messages)]
        score += min(len(follow_up_terms) * 0.7, 2.1)
        if follow_up_terms:
            evidence.append("follow-up language detected")

    return score, evidence


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


async def load_user_interaction_window(db: AsyncSession, user_id: int, window_days: int = 30):
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
                "total_signal": 0.0,
                "primary_risk": primary_risk,
                "recommended_action": recommended_action,
                "cohort_rule": "loyalty_score >= 80 and signal_score < 2 => champions; >= 60 => stable; >= 40 => at risk; otherwise critical",
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


async def build_system_improvement_pack(user_id: int, chat_rows: list[models.ChatHistory], bookings: list[models.Booking], sentiment: Optional[Sentiment]) -> SystemImprovementPack:
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
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_after_days)
    query = (
        select(models.RetentionSnapshot)
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .order_by(desc(models.RetentionSnapshot.created_at))
    )
    result = await db.execute(query)
    snapshots = result.scalars().all()

    total = len(snapshots)
    stale = sum(1 for snapshot in snapshots if snapshot.created_at < stale_cutoff)
    recent = total - stale
    stale_ratio = round(stale / max(total, 1), 2)

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
            status="go" if total and stale / max(total, 1) < 0.25 else "watch",
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
        total_snapshots=total,
        stale_snapshots=stale,
        recent_snapshots=recent,
        stale_ratio=stale_ratio,
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
    from app.routers.chat import analyze_sentiment as router_analyze_sentiment

    return router_analyze_sentiment(text)
