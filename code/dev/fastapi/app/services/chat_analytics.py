from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import json
import os

import requests
from dotenv import load_dotenv

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas.chat import (
    ChurnPrediction,
    InteractionInsight,
    InteractionSummary,
    InteractionTrendItem,
    InteractionTrendReport,
    LifecycleStageItem,
    LifecycleStageReport,
    RetentionCohortItem,
    RetentionCohortReport,
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


def normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def score_area(messages: list[str], bookings: list[models.Booking], area: str) -> tuple[float, list[str]]:
    evidence = []
    score = 0.0
    for message in messages:
        normalized = normalize_text(message)
        if area == "response_speed" and any(word in normalized for word in ["slow", "delay", "wait"]):
            score += 1.0
            evidence.append(message)
        elif area == "clarity" and any(word in normalized for word in ["confusing", "unclear", "hard"]):
            score += 1.0
            evidence.append(message)
        elif area == "reliability" and any(word in normalized for word in ["bug", "error", "issue"]):
            score += 1.0
            evidence.append(message)
        elif area == "booking_flow" and any(word in normalized for word in ["cancel", "book", "schedule"]):
            score += 0.8
            evidence.append(message)
        elif area == "support" and any(word in normalized for word in ["help", "support"]):
            score += 0.8
            evidence.append(message)
        elif area == "pricing" and any(word in normalized for word in ["price", "expensive"]):
            score += 0.7
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

    return score, evidence


def build_interaction_insights(messages: list[str], bookings: list[models.Booking], sentiment: Optional[Sentiment]) -> list[InteractionInsight]:
    areas = ["response_speed", "clarity", "reliability", "booking_flow", "support", "pricing", "retention"]
    ranking = []
    for area in areas:
        score, evidence = score_area(messages, bookings, area)
        ranking.append((area, score, evidence))

    ranking.sort(key=lambda item: item[1], reverse=True)
    insights = []
    for area, score, evidence in ranking[:4]:
        if score <= 0:
            continue
        if area == "response_speed":
            recommendation = "Reduce wait times and make response status visible to users."
            next_step = "Add response-time tracking and auto-acknowledgements for long-running requests."
            priority = "high"
        elif area == "clarity":
            recommendation = "Rewrite confusing flows and simplify user-facing prompts."
            next_step = "Review the most repeated support phrases and shorten the required form copy."
            priority = "high"
        elif area == "reliability":
            recommendation = "Stabilize error-prone journeys before expanding features."
            next_step = "Log failure points by endpoint and surface recoverable errors to users."
            priority = "high"
        elif area == "booking_flow":
            recommendation = "Make booking changes easier and reduce abandonment in pending states."
            next_step = "Track booking drop-offs, cancellations, and time-to-confirmation."
            priority = "medium"
        elif area == "support":
            recommendation = "Offer more proactive support and clearer escalation paths."
            next_step = "Add guided help for frequent questions and route high-friction cases sooner."
            priority = "medium"
        elif area == "pricing":
            recommendation = "Clarify pricing and value messaging to reduce hesitation."
            next_step = "Test pricing explanations and highlight service outcomes more clearly."
            priority = "medium"
        else:
            recommendation = "Create a closed-loop retention workflow for unresolved interactions."
            next_step = "Trigger follow-up prompts when the same concern repeats across sessions."
            priority = "high"

        evidence_list = list(dict.fromkeys(evidence))[:4]
        insights.append(
            InteractionInsight(
                area=area,
                priority=priority,
                score=round(float(score), 2),
                evidence=evidence_list,
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

    top_issues = []
    for item in insights[:3]:
        top_issues.append(item.area.replace("_", " "))
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
        top_issues=top_issues,
        strengths=strengths or ["interaction history is still too small to infer strong patterns"],
        insights=insights,
        metadata={
            "window_days": 30,
            "repeated_messages": len(repeated_issues),
            "booking_states": Counter(str(getattr(b.status, "value", b.status)) for b in bookings),
        },
        generated_at=datetime.now(timezone.utc),
    )




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
    for status_count in summary.metadata.get("booking_states", {}).items():
        if status_count[0] in {"pending", "cancelled"}:
            pending_bookings += int(status_count[1])
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

    user_query = select(models.User.id)
    user_result = await db.execute(user_query)
    user_ids = [row[0] for row in user_result.all()]

    cohorts: dict[str, dict[str, float | int | str]] = {}
    for user_id in user_ids:
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
        summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
        signal_score = round(sum(signal.score for signal in signals) / max(len(signals), 1), 2)
        cohort_name, primary_risk, recommended_action = classify_loyalty_cohort(summary.loyalty_score, signal_score)

        if cohort_name not in cohorts:
            cohorts[cohort_name] = {
                "user_count": 0,
                "total_loyalty": 0.0,
                "total_signal": 0.0,
                "primary_risk": primary_risk,
                "recommended_action": recommended_action,
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
            )
        )

    return RetentionCohortReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        cohorts=cohort_items,
    )


async def build_system_improvement_pack(user_id: int, chat_rows: list[models.ChatHistory], bookings: list[models.Booking], sentiment: Optional[Sentiment]) -> SystemImprovementPack:
    summary = build_summary(user_id, chat_rows, bookings, sentiment)
    insights = summary.insights
    item_map = {
        "response_speed": {"impact": "high", "owner_hint": "backend and ops"},
        "clarity": {"impact": "high", "owner_hint": "product and content"},
        "reliability": {"impact": "high", "owner_hint": "backend engineering"},
        "booking_flow": {"impact": "medium", "owner_hint": "product and frontend"},
        "support": {"impact": "medium", "owner_hint": "customer support"},
        "pricing": {"impact": "medium", "owner_hint": "product strategy"},
        "retention": {"impact": "high", "owner_hint": "growth and CRM"},
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


async def build_lifecycle_stage_report(db: AsyncSession, window_days: int) -> LifecycleStageReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    user_rows = await db.execute(select(models.User.id))
    user_ids = [row[0] for row in user_rows.all()]

    stages: list[LifecycleStageItem] = []
    for user_id in user_ids:
        chat_rows, bookings = await load_user_interaction_window(db, user_id, window_days)
        signal_query = (
            select(models.InteractionSignal)
            .where(models.InteractionSignal.user_id == user_id)
            .where(models.InteractionSignal.created_at >= cutoff)
        )
        signals = (await db.execute(signal_query)).scalars().all()
        latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
        summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
        churn_prediction = await build_churn_prediction(db, user_id, window_days)
        stage, confidence, drivers, retention_focus = classify_lifecycle_stage(summary, churn_prediction, len(chat_rows), len(bookings))
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


def classify_loyalty_cohort(loyalty_score: float, signal_score: float) -> tuple[str, str, str]:
    if loyalty_score >= 80 and signal_score < 2:
        return "champions", "low", "double down on proactive retention and referral nudges"
    if loyalty_score >= 60:
        return "stable", "medium", "reduce friction and reinforce successful journeys"
    if loyalty_score >= 40:
        return "at risk", "high", "prioritize recovery, support follow-up, and friction removal"
    return "critical", "high", "immediate outreach and issue-resolution workflows"


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
