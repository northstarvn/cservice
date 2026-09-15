from datetime import datetime, timezone
from collections import Counter
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas import schemas
from app.schemas import chat as chat_schemas


TOPIC_CATALOG: list[dict[str, str | float | list[str]]] = [
    {
        "topic": "booking status and confirmations",
        "source": "system",
        "rationale": "Most common operational topic for customers tracking scheduled service outcomes.",
        "confidence": 0.94,
        "keywords": ["booking", "confirm", "status", "appointment"],
    },
    {
        "topic": "booking rescheduling and changes",
        "source": "system",
        "rationale": "High-frequency follow-up topic when plans change or an assignment needs to move.",
        "confidence": 0.91,
        "keywords": ["reschedule", "change", "move", "update"],
    },
    {
        "topic": "cancellations and refunds",
        "source": "system",
        "rationale": "Useful for support and retention triage when customers disengage or request reversal.",
        "confidence": 0.89,
        "keywords": ["cancel", "refund", "reverse", "void"],
    },
    {
        "topic": "service quality and follow-up",
        "source": "system",
        "rationale": "Captures feedback loops that influence retention, recovery, and escalation handling.",
        "confidence": 0.88,
        "keywords": ["quality", "follow up", "feedback", "issue"],
    },
    {
        "topic": "billing and payment questions",
        "source": "system",
        "rationale": "Common service-adjacent topic that often requires a different support path.",
        "confidence": 0.87,
        "keywords": ["bill", "payment", "invoice", "charge"],
    },
    {
        "topic": "support escalation and handoff",
        "source": "system",
        "rationale": "Keeps urgent or unresolved issues visible as a first-class topic.",
        "confidence": 0.86,
        "keywords": ["escalate", "handoff", "urgent", "manager"],
    },
    {
        "topic": "account access and profile help",
        "source": "system",
        "rationale": "Helps classify requests related to login, identity, and user profile maintenance.",
        "confidence": 0.84,
        "keywords": ["account", "login", "password", "profile"],
    },
    {
        "topic": "availability and scheduling constraints",
        "source": "system",
        "rationale": "Tracks capacity-related friction and timing conflicts before they become cancellations.",
        "confidence": 0.83,
        "keywords": ["available", "schedule", "time", "slot"],
    },
    {
        "topic": "customer sentiment and recovery",
        "source": "system",
        "rationale": "Useful for measuring dissatisfaction, intent to continue, and recovery opportunities.",
        "confidence": 0.82,
        "keywords": ["sentiment", "angry", "frustrated", "recover"],
    },
    {
        "topic": "FAQ and self-service guidance",
        "source": "system",
        "rationale": "Helps route routine questions toward fast self-service or automated answers.",
        "confidence": 0.8,
        "keywords": ["how to", "faq", "help", "guide"],
    },
    {
        "topic": "language and localization support",
        "source": "system",
        "rationale": "Important for multilingual workflows and localized customer service experiences.",
        "confidence": 0.78,
        "keywords": ["language", "translation", "locale", "multilingual"],
    },
    {
        "topic": "routing and service assignment",
        "source": "system",
        "rationale": "Connects topic selection to downstream workflow and booking assignment decisions.",
        "confidence": 0.77,
        "keywords": ["route", "assign", "room", "match"],
    },
    {
        "topic": "appointment reminders and notifications",
        "source": "system",
        "rationale": "Captures reminder timing, missed alerts, and notification reliability concerns.",
        "confidence": 0.83,
        "keywords": ["reminder", "notify", "alert", "nudge"],
    },
    {
        "topic": "service eligibility and requirements",
        "source": "system",
        "rationale": "Useful when customers need to confirm whether a request qualifies for a service.",
        "confidence": 0.81,
        "keywords": ["eligible", "requirement", "qualify", "criteria"],
    },
    {
        "topic": "address and location details",
        "source": "system",
        "rationale": "Tracks location-sensitive requests that depend on accurate address or site data.",
        "confidence": 0.8,
        "keywords": ["address", "location", "site", "direction"],
    },
    {
        "topic": "arrival timing and eta updates",
        "source": "system",
        "rationale": "Common for status updates where customers want a clear arrival estimate.",
        "confidence": 0.82,
        "keywords": ["eta", "arrival", "when", "time"],
    },
    {
        "topic": "service exceptions and edge cases",
        "source": "system",
        "rationale": "Keeps unusual scenarios visible when standard booking and support flows do not fit.",
        "confidence": 0.76,
        "keywords": ["exception", "edge case", "special", "custom"],
    },
    {
        "topic": "handoff readiness and escalation context",
        "source": "system",
        "rationale": "Tracks whether a conversation has enough context to move cleanly to a human agent.",
        "confidence": 0.79,
        "keywords": ["handoff", "context", "escalation", "agent"],
    },
    {
        "topic": "billing disputes and charge review",
        "source": "system",
        "rationale": "Separates contested charges from routine billing questions for better support routing.",
        "confidence": 0.86,
        "keywords": ["dispute", "charge", "billing", "review"],
    },
    {
        "topic": "service follow-up and resolution tracking",
        "source": "system",
        "rationale": "Helps monitor whether issues were closed, pending, or waiting on a callback.",
        "confidence": 0.84,
        "keywords": ["follow-up", "resolution", "callback", "closed"],
    },
    {
        "topic": "customer onboarding and first-time guidance",
        "source": "system",
        "rationale": "Supports first-use education and helps surface the earliest friction points.",
        "confidence": 0.79,
        "keywords": ["onboarding", "first time", "getting started", "setup"],
    },
    {
        "topic": "service preferences and customization",
        "source": "system",
        "rationale": "Useful when users want tailored options, preferences, or recurring instructions.",
        "confidence": 0.78,
        "keywords": ["preference", "custom", "tailor", "recurring"],
    },
    {
        "topic": "accessibility and assistance needs",
        "source": "system",
        "rationale": "Highlights requests involving accessibility accommodations or special assistance.",
        "confidence": 0.75,
        "keywords": ["accessibility", "assistance", "accommodation", "support"],
    },
    {
        "topic": "escalation prevention and de-escalation",
        "source": "system",
        "rationale": "Captures opportunities to defuse conflict before a complaint becomes a case.",
        "confidence": 0.77,
        "keywords": ["de-escalate", "calm", "resolve", "complaint"],
    },
    {
        "topic": "availability exceptions and waitlist management",
        "source": "system",
        "rationale": "Tracks overbooked schedules, waitlists, and exceptions to normal availability.",
        "confidence": 0.81,
        "keywords": ["waitlist", "overbook", "slot", "availability"],
    },
    {
        "topic": "follow-up preference and communication channel",
        "source": "system",
        "rationale": "Important for choosing the right contact path and confirming response expectations.",
        "confidence": 0.74,
        "keywords": ["call", "text", "email", "contact"],
    },
    {
        "topic": "policy explanation and entitlement review",
        "source": "system",
        "rationale": "Useful when a customer wants the reason behind a rule, limit, or entitlement.",
        "confidence": 0.73,
        "keywords": ["policy", "entitlement", "rule", "explain"],
    },
    {
        "topic": "service status and progress updates",
        "source": "system",
        "rationale": "Captures generic progress-check requests that need a status-aware response.",
        "confidence": 0.85,
        "keywords": ["progress", "status", "update", "where"],
    },
    {
        "topic": "issue reproduction and troubleshooting",
        "source": "system",
        "rationale": "Helps classify diagnostic conversations that need step-by-step investigation.",
        "confidence": 0.84,
        "keywords": ["reproduce", "troubleshoot", "steps", "diagnose"],
    },
    {
        "topic": "complaints and service recovery",
        "source": "system",
        "rationale": "Tracks dissatisfaction that should feed retention and recovery workflows.",
        "confidence": 0.88,
        "keywords": ["complaint", "recover", "unsatisfied", "unhappy"],
    },
    {
        "topic": "workflow automation and task routing",
        "source": "system",
        "rationale": "Connects topic selection to downstream automation, queueing, and routing logic.",
        "confidence": 0.76,
        "keywords": ["automation", "workflow", "queue", "routing"],
    },
    {
        "topic": "room assignment and resource matching",
        "source": "system",
        "rationale": "Keeps booking-side routing decisions visible when a service needs a specific room or resource.",
        "confidence": 0.79,
        "keywords": ["room", "assignment", "resource", "match"],
    },
    {
        "topic": "capacity planning and slot allocation",
        "source": "system",
        "rationale": "Useful for scheduling and dispatch flows that must balance demand against limited availability.",
        "confidence": 0.78,
        "keywords": ["capacity", "slot", "allocation", "demand"],
    },
    {
        "topic": "booking audit trails and traceability",
        "source": "system",
        "rationale": "Captures accountability needs when a user or operator needs a clear record of what changed.",
        "confidence": 0.77,
        "keywords": ["audit", "trace", "history", "event"],
    },
    {
        "topic": "customer follow-through and next-step planning",
        "source": "system",
        "rationale": "Highlights unfinished work that should translate into a concrete next action or callback.",
        "confidence": 0.76,
        "keywords": ["next step", "follow through", "callback", "plan"],
    },
    {
        "topic": "handoff preparation and context packaging",
        "source": "system",
        "rationale": "Makes escalation data easier to pass to another agent or workflow without losing context.",
        "confidence": 0.75,
        "keywords": ["handoff", "context", "package", "transfer"],
    },
    {
        "topic": "service appointment preparation",
        "source": "system",
        "rationale": "Covers pre-visit questions about readiness, setup, and what the customer should expect.",
        "confidence": 0.8,
        "keywords": ["prepare", "prep", "ready", "expect"],
    },
    {
        "topic": "same-day rescheduling and urgent changes",
        "source": "system",
        "rationale": "Separates urgent changes from routine schedule updates so time-sensitive cases are easier to detect.",
        "confidence": 0.84,
        "keywords": ["same day", "urgent", "today", "asap"],
    },
    {
        "topic": "no-show prevention and follow-up",
        "source": "system",
        "rationale": "Captures missed-appointment prevention, reminders, and recovery messaging.",
        "confidence": 0.82,
        "keywords": ["no show", "missed", "remind", "follow up"],
    },
    {
        "topic": "service area coverage and eligibility checks",
        "source": "system",
        "rationale": "Useful when customers need to know whether a location or request is within supported coverage.",
        "confidence": 0.79,
        "keywords": ["coverage", "area", "eligible", "service area"],
    },
    {
        "topic": "queue status and response timing",
        "source": "system",
        "rationale": "Tracks wait-time expectations and queue visibility for service or support requests.",
        "confidence": 0.81,
        "keywords": ["queue", "wait", "response", "timing"],
    },
    {
        "topic": "handoff timing and ownership transfer",
        "source": "system",
        "rationale": "Captures when responsibility moves between teams, agents, or workflows.",
        "confidence": 0.77,
        "keywords": ["ownership", "transfer", "handoff", "team"],
    },
    {
        "topic": "account verification and identity checks",
        "source": "system",
        "rationale": "Separates identity validation from general account help and security-sensitive support.",
        "confidence": 0.85,
        "keywords": ["verify", "identity", "security", "confirm"],
    },
    {
        "topic": "subscription and membership status",
        "source": "system",
        "rationale": "Useful for services with recurring plans, tiers, or active membership states.",
        "confidence": 0.78,
        "keywords": ["subscription", "membership", "plan", "tier"],
    },
    {
        "topic": "refund timing and payout tracking",
        "source": "system",
        "rationale": "Captures payout follow-up for customers waiting on financial reversal or settlement.",
        "confidence": 0.83,
        "keywords": ["refund", "payout", "timing", "processing"],
    },
    {
        "topic": "issue severity and priority triage",
        "source": "system",
        "rationale": "Helps prioritize urgent cases before routing into the right support channel.",
        "confidence": 0.8,
        "keywords": ["severity", "priority", "urgent", "triage"],
    },
    {
        "topic": "customer feedback and survey response",
        "source": "system",
        "rationale": "Useful when conversations include post-service feedback or satisfaction measurement.",
        "confidence": 0.76,
        "keywords": ["survey", "feedback", "rate", "review"],
    },
    {
        "topic": "service limits and quota usage",
        "source": "system",
        "rationale": "Tracks rate limits, entitlement ceilings, and consumption-based restrictions.",
        "confidence": 0.77,
        "keywords": ["limit", "quota", "usage", "allowance"],
    },
    {
        "topic": "workflow exceptions and manual override",
        "source": "system",
        "rationale": "Highlights cases where a standard process needs human review or an exception path.",
        "confidence": 0.78,
        "keywords": ["override", "manual", "exception", "review"],
    },
    {
        "topic": "customer education and guided walkthroughs",
        "source": "system",
        "rationale": "Covers explainers, walkthroughs, and teaching flows that reduce support burden.",
        "confidence": 0.8,
        "keywords": ["walkthrough", "guide", "teach", "explain"],
    },
    {
        "topic": "operational readiness and staffing coverage",
        "source": "system",
        "rationale": "Useful for service teams that need to reason about shifts, coverage, and readiness.",
        "confidence": 0.74,
        "keywords": ["staffing", "coverage", "readiness", "shift"],
    },
    {
        "topic": "priority customer handling and vip routing",
        "source": "system",
        "rationale": "Separates elevated customer handling from standard routing and queueing.",
        "confidence": 0.79,
        "keywords": ["vip", "priority", "premium", "route"],
    },
]


TOPIC_THEME_GROUPS: list[dict[str, str | list[str]]] = [
    {
        "theme": "booking_flow",
        "topics": ["booking status and confirmations", "booking rescheduling and changes", "cancellations and refunds", "availability and scheduling constraints", "appointment reminders and notifications", "service status and progress updates"],
    },
    {
        "theme": "support_workflow",
        "topics": ["support escalation and handoff", "handoff readiness and escalation context", "escalation prevention and de-escalation", "follow-up preference and communication channel", "customer sentiment and recovery", "complaints and service recovery"],
    },
    {
        "theme": "service_operations",
        "topics": ["routing and service assignment", "room assignment and resource matching", "capacity planning and slot allocation", "booking audit trails and traceability", "workflow automation and task routing"],
    },
    {
        "theme": "customer_enablement",
        "topics": ["FAQ and self-service guidance", "customer onboarding and first-time guidance", "service preferences and customization", "accessibility and assistance needs", "language and localization support", "customer follow-through and next-step planning"],
    },
    {
        "theme": "account_security",
        "topics": ["account access and profile help", "account verification and identity checks", "policy explanation and entitlement review", "service limits and quota usage"],
    },
    {
        "theme": "financial_workflow",
        "topics": ["billing and payment questions", "billing disputes and charge review", "refund timing and payout tracking", "cancellations and refunds", "subscription and membership status"],
    },
    {
        "theme": "operational_readiness",
        "topics": ["service appointment preparation", "operational readiness and staffing coverage", "queue status and response timing", "same-day rescheduling and urgent changes", "no-show prevention and follow-up"],
    },
    {
        "theme": "routing_and_capacity",
        "topics": ["routing and service assignment", "room assignment and resource matching", "capacity planning and slot allocation", "availability exceptions and waitlist management", "workflow automation and task routing"],
    },
    {
        "theme": "trust_and_recovery",
        "topics": ["customer sentiment and recovery", "complaints and service recovery", "issue severity and priority triage", "escalation prevention and de-escalation", "priority customer handling and vip routing"],
    },
    {
        "theme": "communication_lifecycle",
        "topics": ["appointment reminders and notifications", "follow-up preference and communication channel", "handoff timing and ownership transfer", "handoff readiness and escalation context", "service follow-up and resolution tracking"],
    },
]


TOPIC_SECTORS: list[dict[str, str | list[str]]] = [
    {"sector": "booking_operations", "topics": ["booking status and confirmations", "booking rescheduling and changes", "cancellations and refunds", "availability and scheduling constraints", "appointment reminders and notifications", "service status and progress updates", "same-day rescheduling and urgent changes", "no-show prevention and follow-up"]},
    {"sector": "support_operations", "topics": ["support escalation and handoff", "handoff readiness and escalation context", "escalation prevention and de-escalation", "issue severity and priority triage", "customer sentiment and recovery", "complaints and service recovery", "service follow-up and resolution tracking"]},
    {"sector": "service_delivery", "topics": ["routing and service assignment", "room assignment and resource matching", "capacity planning and slot allocation", "service appointment preparation", "operational readiness and staffing coverage", "queue status and response timing", "workflow automation and task routing", "handoff timing and ownership transfer"]},
    {"sector": "customer_enablement", "topics": ["FAQ and self-service guidance", "customer onboarding and first-time guidance", "service preferences and customization", "accessibility and assistance needs", "language and localization support", "customer education and guided walkthroughs", "customer follow-through and next-step planning"]},
    {"sector": "account_security", "topics": ["account access and profile help", "account verification and identity checks", "policy explanation and entitlement review", "service limits and quota usage", "subscription and membership status"]},
    {"sector": "financial_controls", "topics": ["billing and payment questions", "billing disputes and charge review", "refund timing and payout tracking", "cancellations and refunds"]},
]


def _topic_keywords(topic: str) -> list[str]:
    normalized = (topic or "").lower()
    return [token for token in normalized.replace("/", " ").replace("-", " ").replace("&", " ").split() if token]


def _topic_topics_by_theme() -> dict[str, list[str]]:
    theme_topics: dict[str, list[str]] = {}
    for group in TOPIC_THEME_GROUPS:
        theme_topics[group["theme"]] = list(group["topics"])
    return theme_topics


def _topic_theme_for(topic: str) -> str | None:
    for group in TOPIC_THEME_GROUPS:
        if topic in group["topics"]:
            return str(group["theme"])
    return None


def _topic_sector_for(topic: str) -> str | None:
    for group in TOPIC_SECTORS:
        if topic in group["topics"]:
            return str(group["sector"])
    return None


def _related_topics_for(topic: str) -> list[str]:
    related: list[str] = []
    topic_theme = _topic_theme_for(topic)
    topic_sector = _topic_sector_for(topic)
    for item in TOPIC_CATALOG:
        candidate = item["topic"]
        if candidate == topic:
            continue
        if topic_theme and candidate in next((group["topics"] for group in TOPIC_THEME_GROUPS if group["theme"] == topic_theme), []):
            related.append(candidate)
            continue
        if topic_sector and candidate in next((group["topics"] for group in TOPIC_SECTORS if group["sector"] == topic_sector), []):
            related.append(candidate)
    return related[:6]


def build_topic_taxonomy_report() -> schemas.TopicTaxonomyReport:
    topic_map = []
    for item in TOPIC_CATALOG:
        topic_map.append(
            schemas.TopicTopicMapItem(
                topic=str(item["topic"]),
                keywords=list(item.get("keywords", [])),
                related_topics=_related_topics_for(str(item["topic"])),
                theme=_topic_theme_for(str(item["topic"])),
                sector=_topic_sector_for(str(item["topic"])),
            )
        )

    theme_map = []
    for group in TOPIC_THEME_GROUPS:
        sector = next((sector_group["sector"] for sector_group in TOPIC_SECTORS if any(topic in sector_group["topics"] for topic in group["topics"])), None)
        theme_map.append({"theme": group["theme"], "sector": sector, "topics": list(group["topics"]), "topic_count": len(group["topics"])})

    return schemas.TopicTaxonomyReport(
        generated_at=datetime.now(timezone.utc),
        total_topics=len(TOPIC_CATALOG),
        total_themes=len(TOPIC_THEME_GROUPS),
        sectors=sorted({str(group["sector"]) for group in TOPIC_SECTORS}),
        topic_map=topic_map,
        theme_map=theme_map,
        summary=f"Topic taxonomy spans {len(TOPIC_CATALOG)} topics across {len(TOPIC_THEME_GROUPS)} themes and {len(TOPIC_SECTORS)} sectors with {len(topic_map[:5])} representative focus topics.",
        topic_focus=[item["topic"] for item in TOPIC_CATALOG[:5]],
    )


def build_topic_theme_coverage(selection: Optional[models.TopicSelection]) -> list[dict[str, str | int | float]]:
    selected_topic = (selection.topic if selection else "") or ""
    selected_text = selected_topic.lower()
    catalog_topics = {item["topic"] for item in TOPIC_CATALOG}
    coverage_items: list[dict[str, str | int | float]] = []
    for group in TOPIC_THEME_GROUPS:
        covered_topics = [topic for topic in group["topics"] if topic in catalog_topics]
        matched_topics = [topic for topic in covered_topics if topic.lower() in selected_text or any(keyword in selected_text for keyword in _topic_keywords(topic))]
        coverage_items.append(
            {
                "theme": group["theme"],
                "topic_count": len(covered_topics),
                "matched_count": len(matched_topics),
                "coverage": round(len(covered_topics) / max(1, len(group["topics"])), 2),
            }
        )
    return coverage_items


def build_topic_richness_report(selection: Optional[models.TopicSelection]) -> dict:
    intelligence = build_topic_intelligence_report(selection)
    themes = build_topic_theme_coverage(selection)
    keyword_count = len(intelligence.matched_keywords)
    matched_themes = [theme for theme in themes if theme["matched_count"]]
    richness_score = min(100.0, round((keyword_count * 6.5) + (len(matched_themes) * 12.0) + (intelligence.coverage_ratio * 40.0), 2))
    return {
        "generated_at": datetime.now(timezone.utc),
        "topic": selection.topic if selection else None,
        "keyword_count": keyword_count,
        "matched_keywords": intelligence.matched_keywords,
        "theme_matches": matched_themes,
        "coverage_ratio": intelligence.coverage_ratio,
        "richness_score": richness_score,
        "summary": f"{intelligence.summary} Richness score {richness_score:.2f} uses {keyword_count} keywords and {len(matched_themes)} matched themes.",
    }


def build_topic_selection_report(selection: models.TopicSelection) -> schemas.TopicSelectionReport:
    return schemas.TopicSelectionReport(
        generated_at=datetime.now(timezone.utc),
        user_id=selection.user_id,
        topic=selection.topic,
        source=selection.source,
        rationale=selection.rationale,
        confidence=selection.confidence,
        is_current=bool(getattr(selection, "is_current", True)),
        summary=f"Selection for '{selection.topic}' from {selection.source} with confidence {selection.confidence:.2f}.",
    )


def build_typed_topic_selection_report(selection: models.TopicSelection) -> schemas.TopicSelectionReport:
    return build_topic_selection_report(selection)


def build_topic_selection_history_report(
    user_id: int,
    selections: list[models.TopicSelection],
) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc),
        "user_id": user_id,
        "items": [build_topic_selection_out(selection) for selection in selections],
        "summary": f"Selection history tracks {len(selections)} topic selections for user {user_id}.",
    }


def build_typed_topic_selection_history_report(
    user_id: int,
    selections: list[models.TopicSelection],
) -> schemas.TopicSelectionHistoryReport:
    return schemas.TopicSelectionHistoryReport(
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        items=[build_topic_selection_out(selection) for selection in selections],
    )


def build_topic_catalog_report() -> schemas.TopicCatalogReport:
    category_counter = Counter(item["topic"].split(" and ")[0] for item in TOPIC_CATALOG)
    theme_report = build_topic_theme_report()
    return schemas.TopicCatalogReport(
        generated_at=datetime.now(timezone.utc),
        total_topics=len(TOPIC_CATALOG),
        items=[build_typed_topic_catalog_item(item) for item in TOPIC_CATALOG],
        top_prefixes=[
            {"prefix": prefix, "count": count}
            for prefix, count in category_counter.most_common(5)
        ],
        summary=(
            f"Topic catalog spans {len(TOPIC_CATALOG)} entries across {theme_report['total_themes']} themes."
            f" Top prefixes emphasize the most common topic families with {len(category_counter.most_common(5))} dominant groups."
        ),
    )


def build_topic_theme_report() -> dict:
    theme_counts = []
    catalog_topics = {item["topic"] for item in TOPIC_CATALOG}
    for group in TOPIC_THEME_GROUPS:
        group_topics = [topic for topic in group["topics"] if topic in catalog_topics]
        theme_counts.append(
            {
                "theme": group["theme"],
                "topic_count": len(group_topics),
                "topics": group_topics,
                "coverage": round(len(group_topics) / max(1, len(group["topics"])), 2),
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc),
        "total_themes": len(TOPIC_THEME_GROUPS),
        "themes": theme_counts,
        "summary": f"Theme coverage tracks {len(TOPIC_THEME_GROUPS)} themes across {len(TOPIC_CATALOG)} catalog topics with {sum(1 for item in theme_counts if item['coverage'] >= 1.0)} fully covered themes.",
    }


def build_topic_coverage_report(selection: Optional[models.TopicSelection]) -> chat_schemas.TopicCoverageReport:
    intelligence = build_topic_intelligence_report(selection)
    top_topics = intelligence.suggested_topics[:3]
    selected_topic = (selection.topic if selection else "") or ""
    selected_text = selected_topic.lower()
    matched_catalog_topics = [
        item for item in TOPIC_CATALOG if any(keyword in selected_text for keyword in item.get("keywords", []))
    ]
    coverage_by_theme = []
    catalog_topics = {item["topic"] for item in TOPIC_CATALOG}
    for group in TOPIC_THEME_GROUPS:
        covered = [topic for topic in group["topics"] if topic in catalog_topics]
        coverage_by_theme.append(
            {
                "theme": group["theme"],
                "coverage": round(len(covered) / max(1, len(group["topics"])), 2),
                "topic_count": len(covered),
            }
        )
    return chat_schemas.TopicCoverageReport(
        generated_at=datetime.now(timezone.utc),
        topic=selection.topic if selection else None,
        matched_topics=[item["topic"] for item in matched_catalog_topics],
        matched_topic_count=len(matched_catalog_topics),
        keyword_matches=intelligence.matched_keywords,
        keyword_match_count=len(intelligence.matched_keywords),
        coverage_ratio=intelligence.coverage_ratio,
        catalog_size=len(TOPIC_CATALOG),
        top_recommendations=[item.topic for item in top_topics],
        theme_coverage=[chat_schemas.TopicCoverageThemeItem(**item) for item in coverage_by_theme],
        topic_focus=list(dict.fromkeys([selection.topic if selection else ""] + [item.topic for item in top_topics]))[:5],
        matched_theme_topics=[topic for group in TOPIC_THEME_GROUPS for topic in group["topics"] if topic in selected_text],
        summary=(
            f"Coverage report matches {len(matched_catalog_topics)} catalog topics"
            f" across {len(coverage_by_theme)} themes for the current selection with {len(top_topics)} top recommendations."
        ),
    )


def build_topic_suggestion_report(
    query: str,
    limit: int = 5,
    theme: str | None = None,
    sector: str | None = None,
) -> schemas.TopicSuggestionReport:
    normalized_query = (query or "").strip()
    query_text = normalized_query.lower()
    suggestions = []

    for item in TOPIC_CATALOG:
        topic_text = item["topic"].lower()
        keyword_hits = [keyword for keyword in item.get("keywords", []) if keyword in query_text]
        theme_hits = []
        sector_hits = []
        if theme:
            theme_hits = [group["theme"] for group in TOPIC_THEME_GROUPS if group["theme"] == theme and item["topic"] in group["topics"]]
        if sector:
            sector_hits = [group["sector"] for group in TOPIC_SECTORS if group["sector"] == sector and item["topic"] in group["topics"]]
        if query_text and (query_text in topic_text or keyword_hits or theme_hits or sector_hits):
            suggestions.append(item)

    if not suggestions:
        suggestions = list(TOPIC_CATALOG[:limit])

    trimmed = suggestions[:limit]
    topic_focus = list(dict.fromkeys([item["topic"] for item in trimmed]))
    return schemas.TopicSuggestionReport(
        generated_at=datetime.now(timezone.utc),
        query=normalized_query,
        total_results=len(trimmed),
        suggested_topics=[schemas.TopicSearchResult(topic=item["topic"], score=float(item["confidence"]) * 100.0, matched_keywords=list(item.get("keywords", []))) for item in trimmed],
        catalog_size=len(TOPIC_CATALOG),
        topic_focus=topic_focus,
        summary=f"Found {len(trimmed)} topic suggestions for '{normalized_query or 'all topics'}'.",
    )


def build_topic_intelligence_report(selection: Optional[models.TopicSelection]) -> schemas.TopicIntelligenceReport:
    if not selection:
        return schemas.TopicIntelligenceReport(
            generated_at=datetime.now(timezone.utc),
            topic=None,
            matched_keywords=[],
            suggested_topics=[schemas.TopicCatalogItem(**item) for item in TOPIC_CATALOG[:5]],
            coverage_ratio=0.0,
            match_count=0,
            summary="No current topic selection available.",
        )

    topic_text = (selection.topic or "").lower()
    matched_keywords: list[str] = []
    suggestions: list[dict[str, str | float | list[str]]] = []

    for item in TOPIC_CATALOG:
        keywords = item.get("keywords", [])
        keyword_hits = [keyword for keyword in keywords if keyword in topic_text]
        if keyword_hits:
            matched_keywords.extend(keyword_hits)
            suggestions.append(item)

    if not suggestions:
        suggestions = TOPIC_CATALOG[:5]

    coverage_ratio = round(len(suggestions) / max(1, len(TOPIC_CATALOG)), 2)

    return schemas.TopicIntelligenceReport(
        generated_at=datetime.now(timezone.utc),
        topic=selection.topic,
        matched_keywords=sorted(set(matched_keywords)),
        suggested_topics=[schemas.TopicCatalogItem(**item) for item in suggestions],
        coverage_ratio=coverage_ratio,
        match_count=len(matched_keywords),
        summary=f"Topic '{selection.topic}' is tracked with {len(matched_keywords)} keyword matches.",
    )


def build_topic_recommendation_report(selection: Optional[models.TopicSelection]) -> schemas.TopicRecommendationReport:
    intelligence = build_topic_intelligence_report(selection)
    suggested_topics = intelligence.suggested_topics
    primary_topic = suggested_topics[0].topic if suggested_topics else None
    portfolio = build_topic_portfolio_report(selection)
    topic_focus = list(
        dict.fromkeys(
            [topic for topic in [primary_topic] if topic]
            + list(portfolio["matched_themes"])
            + [item.topic for item in suggested_topics[:3]]
        )
    )[:5]
    theme_coverage = []
    for item in portfolio["theme_coverage"]:
        theme_coverage.append(
            {
                "theme": item.get("theme", getattr(item, "theme", "")),
                "coverage": item.get("coverage", getattr(item, "coverage", 0.0)),
                "topic_count": item.get("topic_count", getattr(item, "topic_count", 0)),
            }
        )
    return schemas.TopicRecommendationReport(
        generated_at=intelligence.generated_at,
        topic=intelligence.topic,
        primary_topic=primary_topic,
        coverage_ratio=intelligence.coverage_ratio,
        match_count=intelligence.match_count,
        matched_themes=portfolio["matched_themes"],
        theme_coverage=theme_coverage,
        topic_focus=topic_focus,
        recommendations=[
            schemas.TopicRecommendationItem(
                topic=item.topic,
                source=item.source,
                confidence=item.confidence,
                rationale=item.rationale,
            )
            for item in suggested_topics[:3]
        ],
        summary=intelligence.summary + f" Theme matches: {len(portfolio['matched_themes'])}. Coverage entries: {len(theme_coverage)}.",
    )


def build_topic_portfolio_report(selection: Optional[models.TopicSelection]) -> dict:
    catalog = build_topic_catalog_report()
    theme_report = build_topic_theme_report()
    taxonomy_report = build_topic_taxonomy_report()
    coverage_report = build_topic_coverage_report(selection)
    richness_report = build_topic_richness_report(selection)
    topic_focus = list(dict.fromkeys(list(coverage_report.matched_topics) + list(coverage_report.top_recommendations)))[:5]
    return {
        "generated_at": datetime.now(timezone.utc),
        "topic": selection.topic if selection else None,
        "catalog_total": catalog.total_topics,
        "theme_total": theme_report["total_themes"],
        "taxonomy_total": taxonomy_report.total_topics,
        "coverage_ratio": coverage_report.coverage_ratio,
        "top_recommendations": list(coverage_report.top_recommendations),
        "matched_topics": list(coverage_report.matched_topics),
        "theme_coverage": [
            {
                "theme": item.theme,
                "topic_count": item.topic_count,
                "coverage": item.coverage,
            }
            for item in coverage_report.theme_coverage
        ],
        "richness_score": richness_report["richness_score"],
        "matched_themes": [theme["theme"] for theme in richness_report["theme_matches"]],
        "topic_focus": topic_focus,
        "summary": coverage_report.summary + f" Focus topics: {len(topic_focus)}.",
    }


def _topic_recommendation_dict(report: schemas.TopicRecommendationReport) -> dict[str, object]:
    return {
        "generated_at": report.generated_at,
        "topic": report.topic,
        "primary_topic": report.primary_topic,
        "coverage_ratio": report.coverage_ratio,
        "match_count": report.match_count,
        "matched_themes": list(report.matched_themes),
        "theme_coverage": list(report.theme_coverage),
        "recommendations": list(report.recommendations),
        "topic_focus": list(report.topic_focus),
        "summary": report.summary,
    }


def build_typed_topic_catalog_report() -> schemas.TopicCatalogReport:
    return build_topic_catalog_report()


def build_typed_topic_intelligence_report(
    selection: Optional[models.TopicSelection],
) -> schemas.TopicIntelligenceReport:
    report = build_topic_intelligence_report(selection)
    return schemas.TopicIntelligenceReport(
        generated_at=report.generated_at,
        topic=report.topic,
        matched_keywords=list(report.matched_keywords),
        suggested_topics=list(report.suggested_topics),
        coverage_ratio=float(report.coverage_ratio),
        match_count=int(report.match_count),
        summary=report.summary,
    )


def build_typed_topic_catalog_report_from_items(
    generated_at: datetime,
    items: list[dict[str, str | float | list[str]]],
) -> schemas.TopicCatalogReport:
    return schemas.TopicCatalogReport(
        generated_at=generated_at,
        total_topics=len(items),
        items=[build_typed_topic_catalog_item(item) for item in items],
    )


def build_typed_topic_catalog_item(topic: dict[str, str | float | list[str]]) -> schemas.TopicCatalogItem:
    return schemas.TopicCatalogItem(**topic)


def build_typed_topic_theme_report() -> dict:
    return build_topic_theme_report()


def build_typed_topic_recommendation_report(selection: Optional[models.TopicSelection]) -> dict:
    return _topic_recommendation_dict(build_topic_recommendation_report(selection))


def build_typed_topic_suggestion_report(
    query: str,
    limit: int = 5,
    theme: str | None = None,
    sector: str | None = None,
) -> schemas.TopicSuggestionReport:
    return build_topic_suggestion_report(query, limit=limit, theme=theme, sector=sector)


def build_topic_search_report(
    query: str,
    limit: int = 5,
    page: int = 1,
    per_page: int = 5,
    theme: str | None = None,
    sector: str | None = None,
) -> schemas.TopicSearchReport:
    suggestion_report = build_topic_suggestion_report(query, limit=max(limit, per_page), theme=theme, sector=sector)
    total_results = len(suggestion_report.suggested_topics)
    total_pages = max(1, (total_results + max(1, per_page) - 1) // max(1, per_page))
    start = max(0, (page - 1) * max(1, per_page))
    end = start + max(1, per_page)
    items = suggestion_report.suggested_topics[start:end]
    window_start = start + 1 if total_results else 0
    window_end = min(end, total_results)
    return schemas.TopicSearchReport(
        generated_at=suggestion_report.generated_at,
        query=suggestion_report.query,
        total_results=total_results,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        items=items,
        catalog_size=suggestion_report.catalog_size,
        topic_focus=list(suggestion_report.topic_focus),
        summary=f"{suggestion_report.summary} Showing {window_start}-{window_end} of {total_results} results across {suggestion_report.catalog_size} catalog topics and {len(suggestion_report.suggested_topics)} ranked suggestions.",
    )


def build_topic_intelligence_overview(
    user_id: int,
    selection: Optional[models.TopicSelection],
    selections: list[models.TopicSelection],
) -> schemas.TopicIntelligenceOverview:
    workspace = build_topic_workspace_report(user_id, selection, selections)
    portfolio = workspace.portfolio
    coverage = workspace.coverage
    recommendations = _topic_recommendation_dict(build_topic_recommendation_report(selection))
    suggested_topics = build_topic_search_report(
        selection.topic if selection else "",
        limit=5,
        page=1,
        per_page=5,
    ).items
    topic_focus = list(dict.fromkeys(workspace.topic_focus + [item.topic for item in suggested_topics]))[:8]
    return schemas.TopicIntelligenceOverview(
        generated_at=workspace.generated_at,
        user_id=user_id,
        topic=workspace.topic,
        workspace=workspace,
        portfolio=portfolio,
        coverage=coverage,
        recommendations=recommendations,
        suggested_topics=suggested_topics,
        catalog_size=workspace.catalog.total_topics,
        matched_topic_count=len(coverage.get("matched_topics", [])),
        suggestion_count=len(suggested_topics),
        summary=f"Topic overview for user {user_id} with {len(coverage.get('matched_topics', []))} matched topics and {len(suggested_topics)} suggestions.",
        topic_focus=topic_focus,
    )


def build_topic_workspace_report(
    user_id: int,
    selection: Optional[models.TopicSelection],
    selections: list[models.TopicSelection],
) -> schemas.TopicWorkspaceReport:
    catalog = build_topic_catalog_report()
    taxonomy = build_topic_taxonomy_report()
    themes = build_typed_topic_theme_report()
    intelligence = build_typed_topic_intelligence_report(selection)
    coverage = build_topic_coverage_report(selection)
    portfolio = build_topic_portfolio_report(selection)
    recommendations = build_typed_topic_recommendation_report(selection)
    history = build_typed_topic_selection_history_report(user_id, selections)
    portfolio_themes = [
        item.get("theme", "")
        for item in portfolio.get("theme_coverage", [])
        if item.get("theme")
    ]
    topic_focus = list(
        dict.fromkeys(
            portfolio.get("topic_focus", [])
            + portfolio_themes[:3]
            + recommendations.get("matched_themes", [])[:3]
        )
    )[:5]
    coverage_payload = coverage.model_dump() if hasattr(coverage, "model_dump") else coverage
    return schemas.TopicWorkspaceReport(
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        topic=selection.topic if selection else None,
        catalog=catalog,
        taxonomy=taxonomy,
        themes=themes,
        intelligence=intelligence,
        coverage=coverage_payload,
        portfolio=portfolio,
        recommendations=recommendations,
        selection_history=history,
        richness_score=float(portfolio.get("richness_score", 0.0)),
        topic_focus=topic_focus,
        summary=f"Workspace for user {user_id} with {catalog.total_topics} catalog topics and richness {float(portfolio.get('richness_score', 0.0)):.2f}.",
    )


def _normalize_topic_value(topic: str) -> str:
    normalized = (topic or "").strip()
    if not normalized:
        raise ValueError("Topic cannot be empty")
    if len(normalized) > 120:
        raise ValueError("Topic must be 120 characters or fewer")
    return normalized


def build_topic_selection_out(selection: models.TopicSelection) -> schemas.TopicSelectionOut:
    return schemas.TopicSelectionOut(
        id=selection.id,
        user_id=selection.user_id,
        topic=selection.topic,
        source=selection.source,
        rationale=selection.rationale,
        confidence=selection.confidence,
        is_current=bool(getattr(selection, "is_current", True)),
        created_at=selection.created_at,
        updated_at=selection.updated_at,
    )


async def create_topic_selection(
    db: AsyncSession,
    current_user: models.User,
    topic: str,
    source: str = "chat",
    rationale: str = "",
    confidence: float = 0.0,
) -> models.TopicSelection:
    normalized_topic = _normalize_topic_value(topic)
    normalized_source = (source or "chat").strip() or "chat"
    normalized_rationale = (rationale or "").strip()
    selection = models.TopicSelection(
        user_id=current_user.id,
        topic=normalized_topic,
        source=normalized_source,
        rationale=normalized_rationale,
        confidence=confidence,
        is_current=True,
    )
    db.add(selection)
    await db.commit()
    await db.refresh(selection)
    return selection


async def replace_topic_selection(
    db: AsyncSession,
    current_user: models.User,
    topic: str,
    source: str = "chat",
    rationale: str = "",
    confidence: float = 0.0,
) -> models.TopicSelection:
    prior_selection = await get_latest_topic_selection(db, current_user.id)
    if prior_selection:
        prior_selection.is_current = False
        db.add(prior_selection)

    selection = await create_topic_selection(
        db,
        current_user,
        topic=topic,
        source=source,
        rationale=rationale,
        confidence=confidence,
    )
    await db.commit()
    await db.refresh(selection)
    return selection


async def get_latest_topic_selection(
    db: AsyncSession,
    user_id: int,
) -> Optional[models.TopicSelection]:
    result = await db.execute(
        select(models.TopicSelection)
        .where(models.TopicSelection.user_id == user_id)
        .where(models.TopicSelection.is_current.is_(True))
        .order_by(models.TopicSelection.created_at.desc(), models.TopicSelection.id.desc())
    )
    selection = result.scalars().first()
    if selection:
        return selection

    fallback = await db.execute(
        select(models.TopicSelection)
        .where(models.TopicSelection.user_id == user_id)
        .order_by(models.TopicSelection.created_at.desc(), models.TopicSelection.id.desc())
    )
    return fallback.scalars().first()


async def get_topic_selection_history(
    db: AsyncSession,
    user_id: int,
) -> list[models.TopicSelection]:
    result = await db.execute(
        select(models.TopicSelection)
        .where(models.TopicSelection.user_id == user_id)
        .order_by(models.TopicSelection.created_at.desc(), models.TopicSelection.id.desc())
    )
    return result.scalars().all()


async def archive_topic_selection(
    db: AsyncSession,
    current_user: models.User,
) -> Optional[models.TopicSelection]:
    selection = await get_latest_topic_selection(db, current_user.id)
    if not selection:
        return None

    selection.is_current = False
    db.add(selection)
    await db.commit()
    await db.refresh(selection)
    return selection


def list_topic_catalog() -> list[dict[str, str | float | list[str]]]:
    return list(TOPIC_CATALOG)