from datetime import datetime, timezone
from collections import Counter
import re
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
    {
        "topic": "customer intent detection and request framing",
        "source": "system",
        "rationale": "Helps classify the request goal early so the right flow can respond faster.",
        "confidence": 0.83,
        "keywords": ["intent", "request", "purpose", "frame"],
    },
    {
        "topic": "customer preferences and saved context",
        "source": "system",
        "rationale": "Carries repeat preferences and prior context across sessions.",
        "confidence": 0.82,
        "keywords": ["preferences", "saved", "context", "repeat"],
    },
    {
        "topic": "appointment preparation checklists",
        "source": "system",
        "rationale": "Captures pre-visit readiness steps and required preparations.",
        "confidence": 0.8,
        "keywords": ["checklist", "prepare", "bring", "before"],
    },
    {
        "topic": "contact preferences and channel routing",
        "source": "system",
        "rationale": "Routes follow-up through the customer's preferred communication path.",
        "confidence": 0.79,
        "keywords": ["contact", "channel", "email", "text"],
    },
    {
        "topic": "service transcript summarization",
        "source": "system",
        "rationale": "Turns long interactions into concise summaries that support review and handoff.",
        "confidence": 0.81,
        "keywords": ["transcript", "summary", "conversation", "recap"],
    },
    {
        "topic": "service risk and exception monitoring",
        "source": "system",
        "rationale": "Highlights cases that need closer monitoring before they become escalations.",
        "confidence": 0.78,
        "keywords": ["risk", "monitor", "exception", "alert"],
    },
    {
        "topic": "knowledge base search and answer discovery",
        "source": "system",
        "rationale": "Supports guided answer retrieval for routine questions and self-service discovery.",
        "confidence": 0.79,
        "keywords": ["knowledge", "search", "answer", "discover"],
    },
    {
        "topic": "service callback timing and response expectations",
        "source": "system",
        "rationale": "Makes callback commitments and response windows easier to track.",
        "confidence": 0.8,
        "keywords": ["callback", "response", "expectation", "timing"],
    },
    {
        "topic": "service area coverage and eligibility checks",
        "source": "system",
        "rationale": "Flags whether a request is supported in the customer's area or segment.",
        "confidence": 0.79,
        "keywords": ["coverage", "area", "eligible", "service area"],
    },
    {
        "topic": "delivery tracking and status visibility",
        "source": "system",
        "rationale": "Brings shipping and delivery-style tracking into the shared topic model.",
        "confidence": 0.77,
        "keywords": ["delivery", "tracking", "shipment", "status"],
    },
    {
        "topic": "case notes and interaction history",
        "source": "system",
        "rationale": "Makes past notes and prior interactions easier to surface in follow-up flows.",
        "confidence": 0.8,
        "keywords": ["notes", "history", "previous", "case"],
    },
    {
        "topic": "workflow status and queue monitoring",
        "source": "system",
        "rationale": "Captures operational queue checks and workflow visibility needs.",
        "confidence": 0.78,
        "keywords": ["workflow", "status", "queue", "monitor"],
    },
    {
        "topic": "customer confidence and reassurance messaging",
        "source": "system",
        "rationale": "Surfaces reassurance prompts when customers need more certainty.",
        "confidence": 0.76,
        "keywords": ["confidence", "reassurance", "trust", "comfort"],
    },
    {
        "topic": "omnichannel conversation continuity",
        "source": "system",
        "rationale": "Tracks continuity across chat, email, and other support channels.",
        "confidence": 0.77,
        "keywords": ["channel", "continuity", "chat", "email"],
    },
    {
        "topic": "data privacy and information handling",
        "source": "system",
        "rationale": "Keeps privacy-sensitive handling visible in topic routing and analysis.",
        "confidence": 0.78,
        "keywords": ["privacy", "data", "information", "personal"],
    },
    {
        "topic": "service education and guided resolution",
        "source": "system",
        "rationale": "Covers guided support that teaches users how to resolve repeat issues.",
        "confidence": 0.79,
        "keywords": ["education", "guided", "resolution", "support"],
    },
    {
        "topic": "service escalation thresholds and guardrails",
        "source": "system",
        "rationale": "Makes it easier to spot where escalation rules should kick in.",
        "confidence": 0.77,
        "keywords": ["threshold", "guardrail", "escalation", "review"],
    },
    {
        "topic": "customer trust and reassurance",
        "source": "system",
        "rationale": "Keeps trust-building language visible as a first-class topic.",
        "confidence": 0.75,
        "keywords": ["trust", "reassure", "confidence", "comfort"],
    },
    {
        "topic": "service continuity and follow-through",
        "source": "system",
        "rationale": "Connects recurring support, booking, and delivery-style work into a continuity-aware topic.",
        "confidence": 0.8,
        "keywords": ["continuity", "follow-through", "handoff", "resume"],
    },
    {
        "topic": "case resolution ownership",
        "source": "system",
        "rationale": "Makes ownership and final resolution responsibility explicit for service operations.",
        "confidence": 0.79,
        "keywords": ["ownership", "resolution", "case", "resolve"],
    },
    {
        "topic": "service insight and reporting",
        "source": "system",
        "rationale": "Adds analytics and reporting language for operations teams that monitor support performance.",
        "confidence": 0.77,
        "keywords": ["insight", "reporting", "analytics", "dashboard"],
    },
    {
        "topic": "request prioritization and triage",
        "source": "system",
        "rationale": "Captures how incoming requests are sorted, prioritized, and routed to the right path.",
        "confidence": 0.81,
        "keywords": ["priority", "triage", "rank", "queue"],
    },
    {
        "topic": "customer communication history",
        "source": "system",
        "rationale": "Provides a durable topic for conversations that depend on prior interactions and references.",
        "confidence": 0.76,
        "keywords": ["history", "previous", "conversation", "record"],
    },
    {
        "topic": "service compliance and policy alignment",
        "source": "system",
        "rationale": "Keeps policy-sensitive support interactions visible when service rules matter.",
        "confidence": 0.78,
        "keywords": ["compliance", "policy", "alignment", "rule"],
    },
    {
        "topic": "conversation summarization and handoff notes",
        "source": "system",
        "rationale": "Helps route long conversations into concise, transferable notes for the next handler.",
        "confidence": 0.79,
        "keywords": ["summary", "handoff", "notes", "recap"],
    },
    {
        "topic": "service coordination and orchestration",
        "source": "system",
        "rationale": "Captures multi-step workflows that need coordination across teams or systems.",
        "confidence": 0.8,
        "keywords": ["coordination", "orchestration", "workflow", "sequence"],
    },
    {
        "topic": "customer reminders and engagement",
        "source": "system",
        "rationale": "Extends reminder handling into proactive engagement and attendance recovery.",
        "confidence": 0.77,
        "keywords": ["reminder", "engagement", "follow-up", "nudge"],
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
    {
        "theme": "customer_context",
        "topics": ["customer intent detection and request framing", "customer preferences and saved context", "customer confidence and reassurance", "customer trust and reassurance", "case notes and interaction history"],
    },
    {
        "theme": "knowledge_and_guidance",
        "topics": ["FAQ and self-service guidance", "knowledge base search and answer discovery", "service education and guided resolution", "customer education and guided walkthroughs", "service transcript summarization"],
    },
    {
        "theme": "communication_control",
        "topics": ["contact preferences and channel routing", "omnichannel conversation continuity", "service callback timing and response expectations", "service escalation thresholds and guardrails", "data privacy and information handling"],
    },
    {
        "theme": "operational_intelligence",
        "topics": ["service insight and reporting", "service continuity and follow-through", "customer communication history", "conversation summarization and handoff notes", "service coordination and orchestration"],
    },
    {
        "theme": "triage_and_priority",
        "topics": ["issue severity and priority triage", "request prioritization and triage", "service compliance and policy alignment", "customer reminders and engagement", "case resolution ownership"],
    },
]


TOPIC_SECTORS: list[dict[str, str | list[str]]] = [
    {"sector": "booking_operations", "topics": ["booking status and confirmations", "booking rescheduling and changes", "cancellations and refunds", "availability and scheduling constraints", "appointment reminders and notifications", "service status and progress updates", "same-day rescheduling and urgent changes", "no-show prevention and follow-up"]},
    {"sector": "support_operations", "topics": ["support escalation and handoff", "handoff readiness and escalation context", "escalation prevention and de-escalation", "issue severity and priority triage", "customer sentiment and recovery", "complaints and service recovery", "service follow-up and resolution tracking"]},
    {"sector": "service_delivery", "topics": ["routing and service assignment", "room assignment and resource matching", "capacity planning and slot allocation", "service appointment preparation", "operational readiness and staffing coverage", "queue status and response timing", "workflow automation and task routing", "handoff timing and ownership transfer"]},
    {"sector": "customer_enablement", "topics": ["FAQ and self-service guidance", "customer onboarding and first-time guidance", "service preferences and customization", "accessibility and assistance needs", "language and localization support", "customer education and guided walkthroughs", "customer follow-through and next-step planning"]},
    {"sector": "account_security", "topics": ["account access and profile help", "account verification and identity checks", "policy explanation and entitlement review", "service limits and quota usage", "subscription and membership status"]},
    {"sector": "financial_controls", "topics": ["billing and payment questions", "billing disputes and charge review", "refund timing and payout tracking", "cancellations and refunds"]},
    {"sector": "customer_context", "topics": ["customer intent detection and request framing", "customer preferences and saved context", "customer confidence and reassurance", "customer trust and reassurance", "case notes and interaction history"]},
    {"sector": "knowledge_and_guidance", "topics": ["FAQ and self-service guidance", "knowledge base search and answer discovery", "service education and guided resolution", "service transcript summarization", "appointment preparation checklists"]},
    {"sector": "communication_control", "topics": ["contact preferences and channel routing", "omnichannel conversation continuity", "service callback timing and response expectations", "service escalation thresholds and guardrails", "data privacy and information handling"]},
    {"sector": "operations_intelligence", "topics": ["service insight and reporting", "service continuity and follow-through", "customer communication history", "conversation summarization and handoff notes", "service coordination and orchestration"]},
    {"sector": "priority_management", "topics": ["issue severity and priority triage", "request prioritization and triage", "service compliance and policy alignment", "customer reminders and engagement", "case resolution ownership"]},
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


def _topic_overlap_score(topic: str, candidate: str) -> int:
    topic_keywords = set(_topic_keywords(topic))
    candidate_keywords = set(_topic_keywords(candidate))
    shared_keywords = len(topic_keywords & candidate_keywords)
    if topic in candidate or candidate in topic:
        shared_keywords += 2
    if _topic_theme_for(topic) and _topic_theme_for(topic) == _topic_theme_for(candidate):
        shared_keywords += 2
    if _topic_sector_for(topic) and _topic_sector_for(topic) == _topic_sector_for(candidate):
        shared_keywords += 1
    return shared_keywords


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
        summary=f"Topic taxonomy spans {len(TOPIC_CATALOG)} topics across {len(TOPIC_THEME_GROUPS)} themes and {len(TOPIC_SECTORS)} sectors with {len(topic_map[:8])} representative focus topics and {len(theme_map)} theme groups.",
        topic_focus=[item["topic"] for item in TOPIC_CATALOG[:8]],
    )


def build_topic_theme_coverage(selection: Optional[models.TopicSelection]) -> list[dict[str, str | int | float]]:
    selected_topic = (selection.topic if selection else "") or ""
    selected_text = selected_topic.lower()
    catalog_topics = {item["topic"] for item in TOPIC_CATALOG}
    coverage_items: list[dict[str, str | int | float]] = []
    for group in TOPIC_THEME_GROUPS:
        covered_topics = [topic for topic in group["topics"] if topic in catalog_topics]
        matched_topics = [topic for topic in covered_topics if topic.lower() in selected_text or any(keyword in selected_text for keyword in _topic_keywords(topic))]
        overlap_score = sum(_topic_overlap_score(selected_topic, topic) for topic in matched_topics)
        coverage_items.append(
            {
                "theme": group["theme"],
                "topic_count": len(covered_topics),
                "matched_count": len(matched_topics),
                "coverage": round(len(covered_topics) / max(1, len(group["topics"])), 2),
                "overlap_score": overlap_score,
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
        topic_focus=list(dict.fromkeys([selection.topic if selection else ""] + [item.topic for item in top_topics] + [item["topic"] for item in matched_catalog_topics[:3]]))[:6],
        matched_theme_topics=[topic for group in TOPIC_THEME_GROUPS for topic in group["topics"] if topic in selected_text],
        summary=(
            f"Coverage report matches {len(matched_catalog_topics)} catalog topics"
            f" across {len(coverage_by_theme)} themes for the current selection with {len(top_topics)} top recommendations and {len(matched_catalog_topics)} keyword-linked matches."
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
        overlap_hits = _topic_overlap_score(normalized_query, item["topic"])
        if query_text and (query_text in topic_text or keyword_hits or theme_hits or sector_hits or overlap_hits >= 2):
            enriched_item = dict(item)
            enriched_item["confidence"] = round(min(0.99, float(item["confidence"]) + overlap_hits * 0.01), 2)
            enriched_item["rationale"] = f"{item['rationale']} Matched on {len(keyword_hits)} keywords and {overlap_hits} overlap signals."
            suggestions.append(enriched_item)

    if not suggestions:
        suggestions = sorted(
            TOPIC_CATALOG,
            key=lambda item: (-len(_topic_keywords(str(item["topic"])))),
        )[:limit]

    trimmed = sorted(
        suggestions,
        key=lambda item: (-_topic_overlap_score(normalized_query, str(item["topic"])), -float(item["confidence"]), str(item["topic"])),
    )[:limit]
    topic_focus = list(dict.fromkeys([item["topic"] for item in trimmed]))
    theme_coverage = build_topic_theme_coverage(type("TopicSuggestionSelection", (), {"topic": normalized_query})())
    return schemas.TopicSuggestionReport(
        generated_at=datetime.now(timezone.utc),
        query=normalized_query,
        total_results=len(trimmed),
        suggested_topics=[schemas.TopicSearchResult(topic=item["topic"], score=float(item["confidence"]) * 100.0, matched_keywords=list(item.get("keywords", []))) for item in trimmed],
        catalog_size=len(TOPIC_CATALOG),
        topic_focus=topic_focus,
        summary=f"Found {len(trimmed)} topic suggestions for '{normalized_query or 'all topics'}' across {len([item for item in theme_coverage if item['matched_count']])} matched themes.",
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
        suggestions = sorted(TOPIC_CATALOG, key=lambda item: (-len(item.get("keywords", [])), str(item["topic"])))[:5]

    coverage_ratio = round(len(suggestions) / max(1, len(TOPIC_CATALOG)), 2)
    matched_themes = [group["theme"] for group in TOPIC_THEME_GROUPS if selection.topic and any(topic in selection.topic.lower() for topic in group["topics"])]

    return schemas.TopicIntelligenceReport(
        generated_at=datetime.now(timezone.utc),
        topic=selection.topic,
        matched_keywords=sorted(set(matched_keywords)),
        suggested_topics=[schemas.TopicCatalogItem(**item) for item in suggestions],
        coverage_ratio=coverage_ratio,
        match_count=len(matched_keywords),
        topic_focus=list(dict.fromkeys([item["topic"] for item in suggestions[:3]] + matched_themes[:3])),
        catalog_size=len(TOPIC_CATALOG),
        summary=f"Topic '{selection.topic}' is tracked with {len(matched_keywords)} keyword matches across {len(matched_themes)} theme matches.",
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
        summary=intelligence.summary + f" Theme matches: {len(portfolio['matched_themes'])}. Coverage entries: {len(theme_coverage)}. Related recommendations: {len(suggested_topics[:3])}.",
    )


def build_topic_portfolio_report(selection: Optional[models.TopicSelection]) -> dict:
    catalog = build_topic_catalog_report()
    theme_report = build_topic_theme_report()
    taxonomy_report = build_topic_taxonomy_report()
    coverage_report = build_topic_coverage_report(selection)
    richness_report = build_topic_richness_report(selection)
    topic_focus = list(dict.fromkeys(list(coverage_report.matched_topics) + list(coverage_report.top_recommendations)))[:5]
    theme_overlap = sum(item.get("overlap_score", 0) for item in build_topic_theme_coverage(selection))
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
        "theme_overlap_score": theme_overlap,
        "topic_focus": topic_focus,
        "summary": coverage_report.summary + f" Focus topics: {len(topic_focus)}. Theme coverage entries: {len(coverage_report.theme_coverage)}. Theme overlap score: {theme_overlap}.",
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
        summary=f"{suggestion_report.summary} Showing {window_start}-{window_end} of {total_results} results across {suggestion_report.catalog_size} catalog topics, {len(suggestion_report.suggested_topics)} ranked suggestions, and {len(suggestion_report.topic_focus)} focus topics.",
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
    topic_focus = list(dict.fromkeys(workspace.topic_focus + [item.topic for item in suggested_topics]))[:10]
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
        summary=f"Topic overview for user {user_id} with {len(coverage.get('matched_topics', []))} matched topics, {len(suggested_topics)} suggestions, {len(portfolio.get('matched_themes', []))} matched themes, {len(portfolio.get('topic_focus', []))} portfolio focus entries, and {len(workspace.selection_history.items)} history items.",
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
    )[:8]
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
        topic_focus=topic_focus[:8],
        summary=f"Workspace for user {user_id} with {catalog.total_topics} catalog topics, {taxonomy.total_topics} taxonomy topics, {len(portfolio.get('matched_themes', []))} matched themes, richness {float(portfolio.get('richness_score', 0.0)):.2f}, and {len(portfolio.get('topic_focus', []))} portfolio focus entries.",
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


# --- selection governance ---------------------------------------------------
#
# Everything a topic selection needs to be *defensible* lives in config rather
# than in the write path. ``create_topic_selection``/``replace_topic_selection``
# keep their exact behaviour (they still only normalise length and non-empty);
# the tables below are evaluated by ``POST /topics/validate`` and the reports, so
# a caller can ask "would this be accepted, and why" before writing anything.

#: Who may propose a topic, and how far that proposal is trusted. ``source`` is
#: free text on the column, so an unknown source is not trusted by default.
TOPIC_SOURCE_POLICIES: list[dict[str, object]] = [
    {
        "source": "chat",
        "trust": "untrusted",
        "default_confidence": 0.4,
        "requires_rationale": False,
        "note": "free text lifted from the conversation; guarded, never authoritative",
    },
    {
        "source": "policy",
        "trust": "trusted",
        "default_confidence": 0.85,
        "requires_rationale": True,
        "note": "derived from a configured policy; rationale is the policy reference",
    },
    {
        "source": "system",
        "trust": "trusted",
        "default_confidence": 0.9,
        "requires_rationale": True,
        "note": "produced by a deterministic rule (catalog match, classification)",
    },
    {
        "source": "admin",
        "trust": "trusted",
        "default_confidence": 0.95,
        "requires_rationale": True,
        "note": "explicit operator decision; the only source allowed to override a guard",
    },
    {
        "source": "import",
        "trust": "trusted",
        "default_confidence": 0.8,
        "requires_rationale": False,
        "note": "migrated from another system; trusted on provenance, not on content",
    },
]
DEFAULT_TOPIC_SOURCE = "chat"
TOPIC_SOURCE_BY_NAME: dict[str, dict[str, object]] = {
    str(row["source"]): dict(row) for row in TOPIC_SOURCE_POLICIES
}
#: Weakest -> strongest, so a trust comparison is a rank comparison.
TOPIC_TRUST_ORDER: tuple[str, ...] = ("untrusted", "suspected", "trusted")
TOPIC_TRUST_RANK: dict[str, int] = {
    name: rank for rank, name in enumerate(TOPIC_TRUST_ORDER)
}
#: Sources allowed to override a failing guard. Deliberately narrow: a guard that
#: anyone can waive is not a guard.
TOPIC_OVERRIDE_SOURCES: tuple[str, ...] = ("admin",)

#: Content the topic column must not carry. A topic is displayed back to users
#: and read by every downstream decision, so identifiers pasted into free text
#: are a leak, not a topic.
TOPIC_SENSITIVE_PATTERNS: list[dict[str, object]] = [
    {
        "pattern_id": "email",
        "regex": r"[\w.+-]+@[\w-]+\.[\w.]+",
        "severity": "blocking",
        "rationale": "an email address is a contact record, not a topic",
    },
    {
        "pattern_id": "phone",
        "regex": r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)",
        "severity": "blocking",
        "rationale": "a phone number is a contact record, not a topic",
    },
    {
        "pattern_id": "long_digit_run",
        "regex": r"(?<!\w)\d{9,}(?!\w)",
        "severity": "blocking",
        "rationale": "a long digit run is an account, card or case identifier",
    },
    {
        "pattern_id": "bearer_token",
        "regex": r"(?i)\b(?:bearer|token|api[_-]?key|secret)\b\s*[:=]\s*\S+",
        "severity": "blocking",
        "rationale": "a credential in a topic would end up in reports and prompts",
    },
    {
        "pattern_id": "url",
        "regex": r"https?://\S+",
        "severity": "advisory",
        "rationale": "a bare URL is usually an accident of paste, not the subject",
    },
]
TOPIC_SENSITIVE_PATTERN_BY_ID: dict[str, dict[str, object]] = {
    str(row["pattern_id"]): dict(row) for row in TOPIC_SENSITIVE_PATTERNS
}

#: Operators a guard may use (the same idiom as the decision-intelligence gate
#: tables, so a threshold reads identically across subservices).
TOPIC_GUARD_OPS: dict[str, str] = {
    "gte": "metric >= threshold",
    "gt": "metric > threshold",
    "lte": "metric <= threshold",
    "lt": "metric < threshold",
    "eq": "metric == threshold",
    "neq": "metric != threshold",
    "in": "metric is one of threshold (a list)",
    "not_in": "metric is not one of threshold (a list)",
    "is_true": "metric is truthy; threshold ignored",
    "is_false": "metric is falsy; threshold ignored",
    "is_none": "metric is missing or None",
    "present": "the metric key exists at all",
}
TOPIC_GUARD_SEVERITIES: tuple[str, ...] = ("advisory", "review", "reject")
TOPIC_GUARD_SEVERITY_RANK: dict[str, int] = {
    name: rank for rank, name in enumerate(TOPIC_GUARD_SEVERITIES)
}

#: Guards applied to a *proposed* selection. ``sources: ["*"]`` matches every
#: source; a list narrows the guard. The length bounds mirror
#: ``_normalize_topic_value`` so the pre-flight verdict and the write agree.
TOPIC_SELECTION_GUARDS: list[dict[str, object]] = [
    {
        "guard_id": "topic_not_empty",
        "metric": "topic_length",
        "op": "gte",
        "threshold": 1,
        "severity": "reject",
        "sources": ("*",),
        "rationale": "an empty topic is not a selection",
    },
    {
        "guard_id": "topic_length_cap",
        "metric": "topic_length",
        "op": "lte",
        "threshold": 120,
        "severity": "reject",
        "sources": ("*",),
        "rationale": "the column is String(120); a longer value cannot be stored",
    },
    {
        "guard_id": "no_sensitive_content",
        "metric": "sensitive_hit_count",
        "op": "lte",
        "threshold": 0,
        "severity": "reject",
        "sources": ("*",),
        "rationale": "a contact record or credential pasted into the topic leaks downstream",
    },
    {
        "guard_id": "source_known",
        "metric": "source_known",
        "op": "is_true",
        "severity": "review",
        "threshold": True,
        "sources": ("*",),
        "rationale": "source is free text; an unlisted source has no trust level",
    },
    {
        "guard_id": "source_trusted",
        "metric": "source_trust",
        "op": "in",
        "threshold": ("trusted",),
        "severity": "review",
        "sources": ("*",),
        "rationale": "an untrusted source may propose, but a human should confirm",
    },
    {
        "guard_id": "min_confidence",
        "metric": "confidence",
        "op": "gte",
        "threshold": 0.3,
        "severity": "review",
        "sources": ("chat", "import"),
        "rationale": "a low-confidence proposal is a guess, and guesses need confirmation",
    },
    {
        "guard_id": "rationale_present",
        "metric": "rationale_length",
        "op": "gte",
        "threshold": 1,
        "severity": "review",
        "sources": ("system", "policy", "admin"),
        "rationale": "a machine-sourced topic without a rationale cannot be audited",
    },
    {
        "guard_id": "catalog_recognised",
        "metric": "catalog_match_count",
        "op": "gte",
        "threshold": 1,
        "severity": "advisory",
        "sources": ("*",),
        "rationale": "off-catalog topics are allowed, but coverage reporting will miss them",
    },
    {
        "guard_id": "unambiguous_sector",
        "metric": "sector_count",
        "op": "lte",
        "threshold": 1,
        "severity": "advisory",
        "sources": ("*",),
        "rationale": "a topic in two sectors cannot be routed by sector alone",
    },
    {
        "guard_id": "changed_from_current",
        "metric": "unchanged_from_current",
        "op": "is_false",
        "severity": "advisory",
        "sources": ("*",),
        "rationale": "re-saving an identical topic adds a history row without changing anything",
    },
]
TOPIC_GUARD_BY_ID: dict[str, dict[str, object]] = {
    str(row["guard_id"]): dict(row) for row in TOPIC_SELECTION_GUARDS
}

#: How a proposal is matched back to the catalog, cheapest test first. The first
#: mode that matches wins, so a config change can move a topic between bands
#: without touching the ranking code.
TOPIC_MATCH_MODES: list[dict[str, object]] = [
    {"mode": "exact", "description": "the proposal is a catalog topic, ignoring case and padding"},
    {"mode": "keyword", "description": "a catalog keyword appears in the proposal"},
    {"mode": "phrase", "description": "a catalog topic appears inside the proposal, or vice versa"},
    {"mode": "tokens", "description": "token overlap with a catalog topic, scored by share of tokens"},
    {"mode": "none", "description": "off-catalog; allowed, but reported as uncovered"},
]
DEFAULT_TOPIC_MATCH_MODE = "tokens"
TOPIC_TOKEN_MATCH_FLOOR = 0.5

#: Weighting for the config-driven ranker. These are the signals the built-in
#: suggester uses implicitly, made explicit and tunable; contributions are
#: reported per item so a ranking is explainable.
TOPIC_RANKING_WEIGHTS: dict[str, float] = {
    "keyword": 3.0,
    "theme": 2.0,
    "sector": 2.0,
    "overlap": 1.0,
    "confidence": 1.0,
}
TOPIC_RANKING_TIEBREAK: tuple[str, ...] = ("overlap", "confidence", "topic")

#: How a selection history is banded. ``switches`` counts transitions between
#: consecutive *distinct* topics; a customer who re-picks the same topic is not
#: drifting.
TOPIC_DRIFT_BANDS: list[dict[str, object]] = [
    {
        "band": "stable",
        "max_switches": 1,
        "max_distinct_topics": 2,
        "description": "one settled topic, at most one change",
    },
    {
        "band": "drifting",
        "max_switches": 3,
        "max_distinct_topics": 4,
        "description": "the topic is still settling",
    },
    {
        "band": "volatile",
        "max_switches": None,
        "max_distinct_topics": None,
        "description": "no upper bound; treat downstream decisions as unstable",
    },
]
DEFAULT_TOPIC_DRIFT_BAND = "stable"
TOPIC_DRIFT_BAND_BY_NAME: dict[str, dict[str, object]] = {
    str(row["band"]): dict(row) for row in TOPIC_DRIFT_BANDS
}

#: Retention/refresh expectations for the selection history. Advisory only: the
#: table is not DDL, and nothing here deletes a row.
TOPIC_LIFECYCLE_POLICIES: list[dict[str, object]] = [
    {
        "policy_id": "default",
        "history_retention": 50,
        "history_window_days": 365,
        "min_confidence_to_persist": 0.0,
        "require_rationale_on_replace": False,
        "cooldown_minutes_between_changes": 0,
        "description": "keep the last 50 selections for a year; no write-side throttling",
    },
    {
        "policy_id": "high_volume",
        "history_retention": 20,
        "history_window_days": 90,
        "min_confidence_to_persist": 0.2,
        "require_rationale_on_replace": True,
        "cooldown_minutes_between_changes": 5,
        "description": "chatty tenants: throttle replacements and insist on a rationale",
    },
    {
        "policy_id": "regulated",
        "history_retention": 200,
        "history_window_days": 1095,
        "min_confidence_to_persist": 0.0,
        "require_rationale_on_replace": True,
        "cooldown_minutes_between_changes": 0,
        "description": "three years of decision history, every replacement justified",
    },
]
DEFAULT_TOPIC_LIFECYCLE_POLICY = "default"
TOPIC_LIFECYCLE_POLICY_BY_ID: dict[str, dict[str, object]] = {
    str(row["policy_id"]): dict(row) for row in TOPIC_LIFECYCLE_POLICIES
}


def _number(value: object) -> Optional[float]:
    """Coerce to float, or None. Booleans are never numbers."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _op_holds(value: object, op: str, threshold: object) -> bool:
    """Apply one guard operator. Fails closed on an unknown operator name."""
    if op not in TOPIC_GUARD_OPS:
        return False
    if op == "is_true":
        return bool(value)
    if op == "is_false":
        return not value
    if op == "is_none":
        return value is None
    if op == "present":
        return value is not None
    if op in {"in", "not_in"}:
        options = threshold if isinstance(threshold, (list, tuple, set)) else [threshold]
        inside = value in options
        return inside if op == "in" else not inside
    if op == "eq":
        return value == threshold
    if op == "neq":
        return value != threshold
    number = _number(value)
    limit = _number(threshold)
    if number is None or limit is None:
        return False
    if op == "gte":
        return number >= limit
    if op == "gt":
        return number > limit
    if op == "lte":
        return number <= limit
    if op == "lt":
        return number < limit
    return False


def topic_source_policy(source: str | None) -> dict[str, object]:
    """The source row, or an unlisted-source row that trusts nothing."""
    name = (source or "").strip() or DEFAULT_TOPIC_SOURCE
    if name in TOPIC_SOURCE_BY_NAME:
        return dict(TOPIC_SOURCE_BY_NAME[name])
    return {
        "source": name,
        "trust": "untrusted",
        "default_confidence": 0.0,
        "requires_rationale": False,
        "note": "unlisted source; the column is free text, so it defaults to untrusted",
    }


def topic_sensitivity_hits(topic: str) -> list[dict[str, object]]:
    """Every configured sensitive pattern that matches, with the matched text."""
    text = topic or ""
    hits: list[dict[str, object]] = []
    for row in TOPIC_SENSITIVE_PATTERNS:
        match = re.search(str(row["regex"]), text)
        if not match:
            continue
        hits.append(
            {
                "pattern_id": row["pattern_id"],
                "severity": row["severity"],
                "matched": match.group(0),
                "rationale": row["rationale"],
            }
        )
    return hits


def classify_topic_match(
    topic: str,
    *,
    modes: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Match a proposal to the catalog and say *how* it matched.

    Reports the first mode in :data:`TOPIC_MATCH_MODES` that matches, so the
    band a topic lands in is a config decision rather than a code path.
    """
    text = (topic or "").strip()
    lowered = text.lower()
    catalog = {str(item["topic"]): dict(item) for item in TOPIC_CATALOG}
    result: dict[str, object] = {
        "topic": text,
        "mode": "none",
        "matched_topics": [],
        "best_overlap": 0.0,
        "theme": None,
        "sector": None,
    }
    for row in modes or TOPIC_MATCH_MODES:
        mode = str(row["mode"])
        if mode == "exact":
            matches = [name for name in catalog if name.lower() == lowered]
        elif mode == "keyword":
            matches = [
                str(item["topic"])
                for item in TOPIC_CATALOG
                if any(str(k).lower() in lowered for k in item.get("keywords", []))
            ]
        elif mode == "phrase":
            matches = [
                name
                for name in catalog
                if lowered and (lowered in name.lower() or name.lower() in lowered)
            ]
        elif mode == "tokens":
            proposal_tokens = set(_topic_keywords(text))
            scored: list[tuple[float, str]] = []
            for name in catalog:
                tokens = set(_topic_keywords(name))
                if not tokens or not proposal_tokens:
                    continue
                share = len(proposal_tokens & tokens) / len(proposal_tokens)
                if share >= TOPIC_TOKEN_MATCH_FLOOR:
                    scored.append((share, name))
            scored.sort(key=lambda pair: (-pair[0], pair[1]))
            matches = [name for share, name in scored]
            result["best_overlap"] = round(scored[0][0], 4) if scored else 0.0
        else:
            matches = []
        if matches:
            result["mode"] = mode
            result["matched_topics"] = matches
            primary = matches[0]
            result["theme"] = _topic_theme_for(primary)
            result["sector"] = _topic_sector_for(primary)
            result["best_overlap"] = max(
                float(result.get("best_overlap") or 0.0),
                round(_topic_overlap_score(text, primary) / max(1, len(_topic_keywords(primary))), 4),
            )
            return result
    return result


def topic_proposal_metrics(
    topic: str,
    *,
    source: str | None = None,
    rationale: str = "",
    confidence: float = 0.0,
    current_topic: str | None = None,
) -> dict[str, object]:
    """Every metric a guard may read about one proposed selection."""
    text = (topic or "").strip()
    match = classify_topic_match(text)
    sensitivity = topic_sensitivity_hits(text)
    policy = topic_source_policy(source)
    sectors = sorted(
        {
            str(group["sector"])
            for group in TOPIC_SECTORS
            if text and text in [str(item) for item in group["topics"]]
        }
    )
    return {
        "topic": text,
        "topic_length": len(text),
        "source": policy["source"],
        "source_known": str(policy["source"]) in TOPIC_SOURCE_BY_NAME,
        "source_trust": policy["trust"],
        "rationale_length": len((rationale or "").strip()),
        "confidence": _number(confidence) or 0.0,
        "catalog_match_count": len(match["matched_topics"]),
        "match_mode": match["mode"],
        "sector_count": len(sectors),
        "sectors": sectors,
        "sensitive_hit_count": len(sensitivity),
        "sensitive_hits": sensitivity,
        "unchanged_from_current": bool(
            current_topic is not None and current_topic.strip() == text
        ),
        "override_allowed": str(policy["source"]) in TOPIC_OVERRIDE_SOURCES,
    }


def evaluate_selection_guards(
    metrics: dict[str, object],
    *,
    guards: list[dict[str, object]] | None = None,
    source: str | None = None,
) -> list[dict[str, object]]:
    """Evaluate the applicable guards, strongest severity first.

    A guard applies when its ``sources`` list contains the proposal's source or
    ``"*"``. Every result keeps the reason, the observed value and the
    rationale, because a guard that cannot explain itself gets disabled.
    """
    source_name = str(metrics.get("source") or source or DEFAULT_TOPIC_SOURCE)
    results: list[dict[str, object]] = []
    for row in guards or TOPIC_SELECTION_GUARDS:
        sources = tuple(row.get("sources") or ("*",))
        if "*" not in sources and source_name not in sources:
            continue
        metric = str(row.get("metric", ""))
        op = str(row.get("op", ""))
        threshold = row.get("threshold")
        observed = metric in metrics and metrics[metric] is not None
        actual = metrics.get(metric)
        if not observed:
            holds = False
            reason = "metric_missing"
        else:
            holds = _op_holds(actual, op, threshold)
            reason = "ok" if holds else "threshold_not_met"
        severity = str(row.get("severity", "advisory"))
        if severity not in TOPIC_GUARD_SEVERITY_RANK:
            severity = "advisory"
        results.append(
            {
                "guard_id": row.get("guard_id"),
                "metric": metric,
                "op": op,
                "op_meaning": TOPIC_GUARD_OPS.get(op, "unknown operator"),
                "threshold": threshold,
                "actual": actual,
                "observed": observed,
                "severity": severity,
                "holds": holds,
                "reason": reason,
                "rationale": row.get("rationale", ""),
            }
        )
    results.sort(
        key=lambda result: (
            -TOPIC_GUARD_SEVERITY_RANK.get(str(result["severity"]), 0),
            str(result["guard_id"]),
        )
    )
    return results


def selection_verdict(
    metrics: dict[str, object],
    *,
    guards: list[dict[str, object]] | None = None,
    source: str | None = None,
) -> dict[str, object]:
    """``accept`` / ``review`` / ``reject``, plus the guards that decided it.

    ``reject``  — a ``reject``-severity guard failed: do not write this.
    ``review``  — nothing fatal, but something needs a human: write it knowingly.
    ``accept``  — every applicable guard holds.

    The verdict is advisory by construction: the write path is unchanged, so an
    admin can still persist a rejected topic and record why.
    """
    results = evaluate_selection_guards(metrics, guards=guards, source=source)
    failed = [result for result in results if not result["holds"]]
    rejected = [str(r["guard_id"]) for r in failed if r["severity"] == "reject"]
    review = [str(r["guard_id"]) for r in failed if r["severity"] == "review"]
    advisory = [str(r["guard_id"]) for r in failed if r["severity"] == "advisory"]
    if rejected:
        decision = "reject"
    elif review or advisory:
        decision = "review"
    else:
        decision = "accept"
    return {
        "decision": decision,
        "rejected_by": rejected,
        "needs_review_by": review,
        "advisory_by": advisory,
        "guards": results,
        "metrics": metrics,
        "override_allowed": bool(metrics.get("override_allowed")),
        "override_note": (
            "an override-capable source may still persist; the verdict is "
            "advisory and the write path is unchanged"
        ),
    }


def build_topic_selection_validation(
    topic: str,
    *,
    source: str | None = None,
    rationale: str = "",
    confidence: float = 0.0,
    current_topic: str | None = None,
    guards: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Pre-flight a proposed selection without touching the database."""
    metrics = topic_proposal_metrics(
        topic,
        source=source,
        rationale=rationale,
        confidence=confidence,
        current_topic=current_topic,
    )
    verdict = selection_verdict(metrics, guards=guards, source=source)
    return {
        "generated_at": datetime.now(timezone.utc),
        "topic": metrics["topic"],
        "source": metrics["source"],
        "source_policy": topic_source_policy(source),
        "match": classify_topic_match(str(metrics["topic"])),
        "sensitive_hits": metrics["sensitive_hits"],
        "verdict": verdict,
        "summary": (
            f"Proposed topic '{metrics['topic']}' from {metrics['source']} is "
            f"{verdict['decision']} by {len(verdict['guards']) - len([g for g in verdict['guards'] if g['holds']])} "
            f"of {len(verdict['guards'])} guards (mode {metrics['match_mode']}, "
            f"{metrics['catalog_match_count']} catalog matches)."
        ),
    }


def build_topic_governance_catalog() -> dict[str, object]:
    """The introspectable contract for topic governance (meta tooling)."""
    return {
        "sources": {
            "table": [dict(row) for row in TOPIC_SOURCE_POLICIES],
            "default": DEFAULT_TOPIC_SOURCE,
            "trust_order": list(TOPIC_TRUST_ORDER),
            "override_sources": list(TOPIC_OVERRIDE_SOURCES),
            "unknown_source": "unlisted sources resolve to untrusted with 0.0 default confidence",
            "helper": "topic_source_policy",
        },
        "guards": {
            "table": [dict(row) for row in TOPIC_SELECTION_GUARDS],
            "operators": dict(TOPIC_GUARD_OPS),
            "severities": list(TOPIC_GUARD_SEVERITIES),
            "severity_meaning": {
                "reject": "do not write the proposal",
                "review": "a human should confirm before or after writing",
                "advisory": "reported only, never decisive",
            },
            "verdict": "accept | review | reject",
            "verdict_rule": "any reject failure -> reject; any review/advisory failure -> review; else accept",
            "applies_to": "a guard applies when its sources list holds the proposal's source or '*'",
            "advisory": (
                "the verdict does not gate the write path; create/replace keep their "
                "existing behaviour, so an operator can override knowingly"
            ),
            "helpers": [
                "topic_proposal_metrics",
                "evaluate_selection_guards",
                "selection_verdict",
                "build_topic_selection_validation",
            ],
        },
        "sensitive_patterns": {
            "table": [dict(row) for row in TOPIC_SENSITIVE_PATTERNS],
            "checked": "against the topic text, before persistence",
            "helper": "topic_sensitivity_hits",
            "note": "the topic is rendered back to users and read by every downstream decision",
        },
        "match_modes": {
            "table": [dict(row) for row in TOPIC_MATCH_MODES],
            "order": "cheapest test first; the first mode that matches wins",
            "token_floor": TOPIC_TOKEN_MATCH_FLOOR,
            "default": DEFAULT_TOPIC_MATCH_MODE,
            "helper": "classify_topic_match",
        },
        "ranking": {
            "weights": dict(TOPIC_RANKING_WEIGHTS),
            "tiebreak": list(TOPIC_RANKING_TIEBREAK),
            "note": (
                "the same signals the built-in suggester uses, each with an explicit "
                "weight; the suggester hardcodes them. Ordering is therefore not "
                "guaranteed to match, and every per-signal contribution is reported"
            ),
            "strict_filters": "theme/sector drop out-of-scope topics instead of down-weighting them",
            "helper": "rank_topics",
        },
        "drift_bands": {
            "table": [dict(row) for row in TOPIC_DRIFT_BANDS],
            "default": DEFAULT_TOPIC_DRIFT_BAND,
            "note": "switches count transitions between consecutive distinct topics",
            "helper": "build_topic_drift_report",
        },
        "lifecycle": {
            "table": [dict(row) for row in TOPIC_LIFECYCLE_POLICIES],
            "default": DEFAULT_TOPIC_LIFECYCLE_POLICY,
            "advisory": "retention and cooldown are reported, never enforced: no DDL, no deletes",
            "helper": "build_topic_lifecycle_report",
        },
        "taxonomy_integrity": {
            "checks": [
                "catalog topics in no theme",
                "catalog topics in no sector",
                "topics in more than one sector",
                "themes with no sector",
                "sectors with no themes",
                "duplicate catalog topics",
            ],
            "helper": "build_topic_taxonomy_integrity_report",
        },
    }


def rank_topics(
    query: str,
    *,
    limit: int = 5,
    theme: str | None = None,
    sector: str | None = None,
    weights: dict[str, float] | None = None,
    catalog: list[dict[str, object]] | None = None,
    strict_filters: bool = False,
) -> dict[str, object]:
    """Config-weighted catalog ranking.

    The signals are the ones the built-in suggester already uses (keyword hits,
    token overlap, theme/sector affinity, catalog confidence), but each one is
    weighted from :data:`TOPIC_RANKING_WEIGHTS` and the per-signal contributions
    are returned, so a ranking can be explained ("why is this first") instead of
    just trusted. The ordering is therefore *not* guaranteed to match the
    suggester's. ``strict_filters`` drops out-of-scope topics instead of merely
    down-weighting them.
    """
    table = dict(TOPIC_RANKING_WEIGHTS if weights is None else weights)
    text = (query or "").strip()
    lowered = text.lower()
    scored: list[dict[str, object]] = []
    for item in catalog or TOPIC_CATALOG:
        topic = str(item["topic"])
        theme_name = _topic_theme_for(topic)
        sector_name = _topic_sector_for(topic)
        if strict_filters and theme and theme_name != theme:
            continue
        if strict_filters and sector and sector_name != sector:
            continue
        keywords = [str(keyword).lower() for keyword in item.get("keywords", [])]
        keyword_hits = [keyword for keyword in keywords if keyword and keyword in lowered]
        theme_hit = 1 if theme and theme_name == theme else 0
        sector_hit = 1 if sector and sector_name == sector else 0
        overlap = _topic_overlap_score(text, topic) if text else 0
        confidence = _number(item.get("confidence")) or 0.0
        contributions = {
            "keyword": table.get("keyword", 0.0) * len(keyword_hits),
            "theme": table.get("theme", 0.0) * theme_hit,
            "sector": table.get("sector", 0.0) * sector_hit,
            "overlap": table.get("overlap", 0.0) * overlap,
            "confidence": table.get("confidence", 0.0) * confidence,
        }
        score = sum(contributions.values())
        if score <= 0:
            continue
        scored.append(
            {
                "topic": topic,
                "score": round(score, 4),
                "contributions": {key: round(value, 4) for key, value in contributions.items()},
                "matched_keywords": keyword_hits,
                "overlap": overlap,
                "confidence": confidence,
                "theme": _topic_theme_for(topic),
                "sector": _topic_sector_for(topic),
            }
        )
    for key in reversed(TOPIC_RANKING_TIEBREAK):
        if key == "confidence":
            scored.sort(key=lambda row: -float(row["confidence"]))
        elif key == "overlap":
            scored.sort(key=lambda row: -int(row["overlap"]))
    scored.sort(key=lambda row: (-float(row["score"]), str(row["topic"])))
    return {
        "generated_at": datetime.now(timezone.utc),
        "query": text,
        "theme": theme,
        "sector": sector,
        "weights": table,
        "total_matches": len(scored),
        "items": scored[: max(1, int(limit))],
        "match": classify_topic_match(text),
        "summary": (
            f"Ranked {len(scored)} catalog topics for '{text or 'all topics'}' "
            f"using weights {table}."
        ),
    }


def _distinct_topic_sequence(
    selections: list[models.TopicSelection],
) -> list[dict[str, object]]:
    """Chronological selections with consecutive repeats collapsed."""
    ordered = list(reversed(list(selections or [])))
    sequence: list[dict[str, object]] = []
    for selection in ordered:
        topic = (selection.topic or "").strip()
        if sequence and str(sequence[-1]["topic"]) == topic:
            continue
        sequence.append(
            {
                "topic": topic,
                "source": selection.source,
                "confidence": float(selection.confidence or 0.0),
                "created_at": selection.created_at,
                "theme": _topic_theme_for(topic),
                "sector": _topic_sector_for(topic),
            }
        )
    return sequence


def topic_drift_band(
    switches: int,
    distinct_topics: int,
    *,
    bands: list[dict[str, object]] | None = None,
) -> str:
    """The configured band for an observed switch/distinct count."""
    for row in bands or TOPIC_DRIFT_BANDS:
        max_switches = row.get("max_switches")
        max_distinct = row.get("max_distinct_topics")
        if max_switches is not None and switches > int(max_switches):
            continue
        if max_distinct is not None and distinct_topics > int(max_distinct):
            continue
        return str(row["band"])
    return str((bands or TOPIC_DRIFT_BANDS)[-1]["band"])


def build_topic_drift_report(
    selections: Optional[list[models.TopicSelection]] = None,
    *,
    bands: list[dict[str, object]] | None = None,
    window: int | None = None,
) -> dict[str, object]:
    """Is a customer's topic settled, or is it moving under every decision?

    ``switches`` counts transitions between consecutive *distinct* topics, so
    re-saving the same topic is not drift; ``volatility`` is switches per
    transition window and is the number to threshold on.
    """
    sequence = _distinct_topic_sequence(selections or [])
    if window:
        sequence = sequence[-int(window) :]
    switches = max(0, len(sequence) - 1)
    distinct = sorted({str(item["topic"]) for item in sequence})
    themes = [item["theme"] for item in sequence if item["theme"]]
    dominant_theme = max(set(themes), key=themes.count) if themes else None
    band = topic_drift_band(switches, len(distinct), bands=bands)
    mean_confidence = (
        round(sum(float(item["confidence"]) for item in sequence) / len(sequence), 4)
        if sequence
        else 0.0
    )
    return {
        "generated_at": datetime.now(timezone.utc),
        "selections": len(selections or []),
        "distinct_transitions": len(sequence),
        "switches": switches,
        "distinct_topics": len(distinct),
        "topics": distinct,
        "sequence": sequence,
        "band": band,
        "volatility": round(switches / max(1, len(sequence)), 4),
        "dominant_theme": dominant_theme,
        "mean_confidence": mean_confidence,
        "settled": band == DEFAULT_TOPIC_DRIFT_BAND,
        "bands": [dict(row) for row in (bands or TOPIC_DRIFT_BANDS)],
        "summary": (
            f"{len(distinct)} distinct topics across {switches} switches -> band "
            f"'{band}' (volatility {round(switches / max(1, len(sequence)), 4)}), "
            f"dominant theme {dominant_theme or 'none'}."
        ),
    }


def build_topic_lifecycle_report(
    selections: Optional[list[models.TopicSelection]] = None,
    *,
    policy_id: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, object]:
    """Compare a selection history against the configured retention policy.

    Advisory by construction: the policy says what *should* be kept, and this
    report says what is; nothing is deleted.
    """
    policy = dict(
        TOPIC_LIFECYCLE_POLICY_BY_ID.get(
            policy_id or DEFAULT_TOPIC_LIFECYCLE_POLICY, {}
        )
    )
    if not policy:
        raise ValueError(
            f"unknown lifecycle policy {policy_id!r}; "
            f"expected one of {sorted(TOPIC_LIFECYCLE_POLICY_BY_ID)}"
        )
    rows = list(selections or [])
    retention = policy.get("history_retention")
    window_days = policy.get("history_window_days")
    horizon = (as_of or datetime.now(timezone.utc)).timestamp() - float(window_days or 0) * 86400
    within_window = 0
    for selection in rows:
        created = selection.created_at
        if created is None:
            continue
        created_ts = created.timestamp() if hasattr(created, "timestamp") else None
        if created_ts is not None and created_ts >= horizon:
            within_window += 1
    min_confidence = _number(policy.get("min_confidence_to_persist")) or 0.0
    below_floor = [
        {"id": selection.id, "topic": selection.topic, "confidence": float(selection.confidence or 0.0)}
        for selection in rows
        if float(selection.confidence or 0.0) < min_confidence
    ]
    return {
        "generated_at": datetime.now(timezone.utc),
        "policy": policy["policy_id"],
        "description": policy.get("description"),
        "selections": len(rows),
        "history_retention": retention,
        "retention_exceeded": bool(retention is not None and len(rows) > int(retention)),
        "over_retention": max(0, len(rows) - int(retention)) if retention is not None else 0,
        "history_window_days": window_days,
        "selections_within_window": within_window,
        "min_confidence_to_persist": min_confidence,
        "below_confidence_floor": below_floor,
        "require_rationale_on_replace": bool(policy.get("require_rationale_on_replace")),
        "rationale_less": [
            {"id": selection.id, "topic": selection.topic}
            for selection in rows
            if not (selection.rationale or "").strip()
        ],
        "cooldown_minutes_between_changes": policy.get("cooldown_minutes_between_changes"),
        "advisory": "reported only: no row is deleted and no write is refused",
    }


def build_topic_taxonomy_integrity_report() -> dict[str, object]:
    """Consistency checks over the three taxonomy tables.

    The tables are maintained by hand and genuinely overlap (``cancellations and
    refunds`` lives in two sectors on purpose, some topics in none), so this
    report makes the consequences explicit rather than leaving them implicit in
    a lookup.
    """
    catalog_topics = [str(item["topic"]) for item in TOPIC_CATALOG]
    theme_membership: dict[str, list[str]] = {}
    for group in TOPIC_THEME_GROUPS:
        for topic in group["topics"]:
            theme_membership.setdefault(str(topic), []).append(str(group["theme"]))
    sector_membership: dict[str, list[str]] = {}
    for group in TOPIC_SECTORS:
        for topic in group["topics"]:
            sector_membership.setdefault(str(topic), []).append(str(group["sector"]))
    unthemed = sorted(topic for topic in catalog_topics if topic not in theme_membership)
    unsectored = sorted(topic for topic in catalog_topics if topic not in sector_membership)
    multi_sector = sorted(
        topic for topic in catalog_topics if len(sector_membership.get(topic, [])) > 1
    )
    sector_themes: dict[str, set[str]] = {str(g["sector"]): set() for g in TOPIC_SECTORS}
    for group in TOPIC_THEME_GROUPS:
        for topic in group["topics"]:
            for sector in sector_membership.get(str(topic), []):
                sector_themes.setdefault(sector, set()).add(str(group["theme"]))
    orphan_themes = sorted(
        str(group["theme"])
        for group in TOPIC_THEME_GROUPS
        if not any(
            sector_membership.get(str(topic)) for topic in group["topics"]
        )
    )
    empty_sectors = sorted(
        str(group["sector"])
        for group in TOPIC_SECTORS
        if not group["topics"]
    )
    duplicates = sorted(
        {topic for topic in catalog_topics if catalog_topics.count(topic) > 1}
    )
    findings: list[dict[str, object]] = []
    if unthemed:
        findings.append(
            {
                "check": "catalog_topics_in_no_theme",
                "count": len(unthemed),
                "items": unthemed,
                "impact": "theme coverage reports these as uncovered",
            }
        )
    if unsectored:
        findings.append(
            {
                "check": "catalog_topics_in_no_sector",
                "count": len(unsectored),
                "items": unsectored,
                "impact": "sector routing cannot place these",
            }
        )
    if multi_sector:
        findings.append(
            {
                "check": "topics_in_multiple_sectors",
                "count": len(multi_sector),
                "items": multi_sector,
                "impact": "sector-filtered search returns the topic under more than one sector",
            }
        )
    if orphan_themes:
        findings.append(
            {
                "check": "themes_with_no_sector",
                "count": len(orphan_themes),
                "items": orphan_themes,
                "impact": "a theme with no sector cannot be reached by sector filters",
            }
        )
    if empty_sectors:
        findings.append(
            {
                "check": "sectors_with_no_topics",
                "count": len(empty_sectors),
                "items": empty_sectors,
                "impact": "an empty sector can never match a search",
            }
        )
    if duplicates:
        findings.append(
            {
                "check": "duplicate_catalog_topics",
                "count": len(duplicates),
                "items": duplicates,
                "impact": "a duplicate inflates catalog size and splits coverage credit",
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc),
        "catalog_size": len(catalog_topics),
        "theme_count": len(TOPIC_THEME_GROUPS),
        "sector_count": len(TOPIC_SECTORS),
        "unthemed_topics": unthemed,
        "unsectored_topics": unsectored,
        "multi_sector_topics": multi_sector,
        "orphan_themes": orphan_themes,
        "empty_sectors": empty_sectors,
        "duplicate_topics": duplicates,
        "sector_theme_map": {sector: sorted(themes) for sector, themes in sector_themes.items()},
        "findings": findings,
        "healthy": not findings,
        "summary": (
            f"Taxonomy integrity over {len(catalog_topics)} topics: {len(findings)} "
            f"findings ({len(unthemed)} unthemed, {len(unsectored)} unsectored, "
            f"{len(multi_sector)} in multiple sectors)."
        ),
    }


def build_topic_governance_report() -> dict[str, object]:
    """The full governance surface: config, integrity, and the live verdicts."""
    integrity = build_topic_taxonomy_integrity_report()
    catalog = build_topic_governance_catalog()
    return {
        "generated_at": datetime.now(timezone.utc),
        "catalog": catalog,
        "integrity": integrity,
        "guards": [dict(row) for row in TOPIC_SELECTION_GUARDS],
        "sources": [dict(row) for row in TOPIC_SOURCE_POLICIES],
        "sensitive_patterns": [dict(row) for row in TOPIC_SENSITIVE_PATTERNS],
        "match_modes": [dict(row) for row in TOPIC_MATCH_MODES],
        "drift_bands": [dict(row) for row in TOPIC_DRIFT_BANDS],
        "lifecycle_policies": [dict(row) for row in TOPIC_LIFECYCLE_POLICIES],
        "ranking_weights": dict(TOPIC_RANKING_WEIGHTS),
        "healthy": integrity["healthy"],
        "summary": (
            f"Topic governance over {integrity['catalog_size']} topics: "
            f"{len(TOPIC_SELECTION_GUARDS)} guards, {len(TOPIC_SOURCE_POLICIES)} "
            f"sources, {len(TOPIC_SENSITIVE_PATTERNS)} sensitive patterns, "
            f"{len(integrity['findings'])} integrity findings."
        ),
    }