"""Retention: snapshot history read models, health banding, and forecasting.

Two layers, and the split is deliberate.

The **read layer** is the original one: load a user's ``RetentionSnapshot`` rows
for a window and render them as reports (snapshot list, delta, trends, health,
coverage, operational, dashboard, recommendations). Every function in it is
additive over the snapshot table and is called directly by ``routers/chat.py``.

The **policy layer** is new. The first version of this module had a single
config table and therefore had its tuning constants inline: an 85-entry
``topic_hints`` mapping and a 14-cluster ``clusters`` mapping written as dict
literals inside the functions that used them, the churn-risk rank map
``{"low": 0, "medium": 1, "high": 2, "critical": 3}`` written out three times,
and the decision cutoffs ``len(snapshots) < 5``, ``total >= 3``,
``average_churn >= 2.0``, ``average_loyalty < 50``, ``coverage_ratio >= 0.5``
scattered across ``build_retention_recommendations`` and
``build_retention_operational_report``. That is the anti-pattern this repo
exists to remove: a threshold nobody can find, retune, simulate, or test, and
whose change is a code edit. All of it is now a table plus one resolver, and
every default reproduces the previous value exactly.

Policy tables added here (adding a rule is config-only, never code):

- ``RETENTION_TOPIC_HINTS`` — free-text summary -> retention-relevant topics.
  Folded out of ``_retention_topic_context_from_summary`` verbatim, 80 rows.
- ``RETENTION_TOPIC_CLUSTERS`` — topic -> cluster grouping, 14 clusters / 63
  members. Folded out of ``_retention_topic_clusters`` verbatim.
- ``RETENTION_CHURN_RANK`` — the ordinal churn-risk scale, declared once, with
  ``churn_risk_rank`` as the only reader. Replaces three inline copies.
- ``RETENTION_HEALTH_RULES`` — ``when``-DSL over derived snapshot metrics ->
  health band, priority, and recommended actions. Replaces the inline cutoffs
  in the recommendation and operational reports.
- ``RETENTION_ANOMALY_RULES`` — ``when``-DSL over the snapshot *series* ->
  anomaly findings, on the same pattern as ``ACTIVITY_TREE_ANOMALY_RULES``.
- ``RETENTION_FORECAST_RULES`` — projection horizon, method, and confidence
  widening per horizon, for ``build_retention_forecast``.
- ``RETENTION_SNAPSHOT_OPS`` — prune/keep defaults, the health report's
  type-coverage denominator, and the churn-rank fallback.

``when`` rules are evaluated by the shared engine in ``app.rule_engine``
(``evaluate_when``), the same core the loyalty, communication, arrears, and
points engines already use, so the combinators, numeric ops, and reserved
``_date`` key behave identically here.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from statistics import fmean, pstdev
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.rule_engine import evaluate_when
from app.schemas import chat as chat_schemas
from app.schemas.chat import (
    RetentionCoverageItem,
    RetentionCoverageReport,
    RetentionMaintenancePreview,
    RetentionMaintenancePreviewItem,
    RetentionOperationalItem,
    RetentionOperationalReport,
    RetentionSnapshotAdminItem,
    RetentionSnapshotAdminReport,
    RetentionSnapshotDelta,
    RetentionSnapshotItem,
    RetentionSnapshotReport,
    RetentionSnapshotTrendItem,
    RetentionSnapshotTrendReport,
    RetentionDashboard,
    UserRetentionSnapshotHealth,
)
from app.services.chat_analytics import (
    analyze_sentiment,
    build_churn_prediction,
    build_retention_dashboard_topic_signal_detail,
    build_retention_snapshot_operations_report,
    build_summary,
    load_user_interaction_window,
)
from app.services.topics import (
    TOPIC_CATALOG,
    TOPIC_SECTORS,
    build_topic_intelligence_report,
    build_topic_portfolio_report,
    build_topic_suggestion_report,
    build_topic_theme_coverage,
)


# ---------------------------------------------------------------------------
# Policy tables
# ---------------------------------------------------------------------------
#
# ``RETENTION_TOPIC_HINTS`` and ``RETENTION_TOPIC_CLUSTERS`` are byte-equivalent
# extractions of the two dict literals that used to live inside
# ``_retention_topic_context_from_summary`` and ``_retention_topic_clusters``.
# Row order is preserved because both consumers are order-sensitive: the first
# returns "the first N topics that matched" and the second returns a cluster map
# whose serialized key order is part of the pinned payload.

# Free-text keyword -> retention-relevant topic. Matching is substring, lowercased.
RETENTION_TOPIC_HINTS: list[dict[str, object]] = [
    {
        "topic": "booking status and confirmations",
        "keywords": ["booking", "confirm", "status", "appointment"],
    },
    {
        "topic": "booking rescheduling and changes",
        "keywords": ["reschedule", "change", "move", "update"],
    },
    {
        "topic": "cancellations and refunds",
        "keywords": ["cancel", "refund", "reverse", "void"],
    },
    {
        "topic": "service quality and follow-up",
        "keywords": ["quality", "follow up", "feedback", "issue"],
    },
    {
        "topic": "support escalation and handoff",
        "keywords": ["escalate", "handoff", "urgent", "manager"],
    },
    {
        "topic": "customer sentiment and recovery",
        "keywords": ["sentiment", "angry", "frustrated", "recover"],
    },
    {
        "topic": "FAQ and self-service guidance",
        "keywords": ["how to", "faq", "help", "guide"],
    },
    {
        "topic": "routing and service assignment",
        "keywords": ["route", "assign", "room", "match"],
    },
    {
        "topic": "service status and progress updates",
        "keywords": ["progress", "status", "update", "where"],
    },
    {
        "topic": "issue reproduction and troubleshooting",
        "keywords": ["reproduce", "troubleshoot", "steps", "diagnose"],
    },
    {
        "topic": "billing and payment questions",
        "keywords": ["bill", "payment", "invoice", "charge"],
    },
    {
        "topic": "account access and profile help",
        "keywords": ["account", "login", "password", "profile"],
    },
    {
        "topic": "queue status and response timing",
        "keywords": ["queue", "wait", "response", "timing"],
    },
    {
        "topic": "service appointment preparation",
        "keywords": ["prepare", "prep", "ready", "expect"],
    },
    {
        "topic": "same-day rescheduling and urgent changes",
        "keywords": ["same day", "urgent", "today", "asap"],
    },
    {
        "topic": "no-show prevention and follow-up",
        "keywords": ["no show", "missed", "remind", "follow up"],
    },
    {
        "topic": "handoff readiness and escalation context",
        "keywords": ["handoff", "context", "escalation", "agent"],
    },
    {
        "topic": "service eligibility and requirements",
        "keywords": ["eligible", "requirement", "qualify", "criteria"],
    },
    {
        "topic": "address and location details",
        "keywords": ["address", "location", "site", "direction"],
    },
    {
        "topic": "arrival timing and eta updates",
        "keywords": ["eta", "arrival", "when", "time"],
    },
    {
        "topic": "service exceptions and edge cases",
        "keywords": ["exception", "special", "edge", "custom"],
    },
    {
        "topic": "workflow automation and task routing",
        "keywords": ["automation", "workflow", "queue", "routing"],
    },
    {
        "topic": "room assignment and resource matching",
        "keywords": ["room", "assignment", "resource", "match"],
    },
    {
        "topic": "capacity planning and slot allocation",
        "keywords": ["capacity", "slot", "allocation", "demand"],
    },
    {
        "topic": "customer onboarding and first-time guidance",
        "keywords": ["onboarding", "first time", "getting started", "setup"],
    },
    {
        "topic": "service preferences and customization",
        "keywords": ["preference", "custom", "tailor", "recurring"],
    },
    {
        "topic": "accessibility and assistance needs",
        "keywords": ["accessibility", "assistance", "accommodation", "support"],
    },
    {
        "topic": "policy explanation and entitlement review",
        "keywords": ["policy", "entitlement", "rule", "explain"],
    },
    {
        "topic": "billing disputes and charge review",
        "keywords": ["dispute", "charge", "billing", "review"],
    },
    {
        "topic": "service follow-up and resolution tracking",
        "keywords": ["follow-up", "resolution", "callback", "closed"],
    },
    {
        "topic": "customer feedback and survey response",
        "keywords": ["survey", "feedback", "rate", "review"],
    },
    {
        "topic": "operational readiness and staffing coverage",
        "keywords": ["staffing", "coverage", "readiness", "shift"],
    },
    {
        "topic": "delivery tracking and status visibility",
        "keywords": ["delivery", "tracking", "shipment", "status"],
    },
    {
        "topic": "appointment preparation checklists",
        "keywords": ["checklist", "bring", "prepare", "before"],
    },
    {
        "topic": "contact preferences and channel routing",
        "keywords": ["contact", "channel", "email", "text"],
    },
    {
        "topic": "case notes and interaction history",
        "keywords": ["notes", "history", "previous", "case"],
    },
    {
        "topic": "service quote and estimate review",
        "keywords": ["quote", "estimate", "cost", "review"],
    },
    {
        "topic": "handoff quality and context completeness",
        "keywords": ["handoff", "context", "summary", "complete"],
    },
    {
        "topic": "support queue prioritization",
        "keywords": ["queue", "priority", "triage", "order"],
    },
    {
        "topic": "customer intent detection and routing",
        "keywords": ["intent", "route", "purpose", "request"],
    },
    {
        "topic": "service history and recurring issues",
        "keywords": ["history", "repeat", "recurring", "issue"],
    },
    {
        "topic": "customer consent and communication permissions",
        "keywords": ["consent", "permission", "opt in", "contact"],
    },
    {
        "topic": "data privacy and information handling",
        "keywords": ["privacy", "data", "information", "personal"],
    },
    {
        "topic": "service feedback loops and product insights",
        "keywords": ["feedback", "insight", "product", "improve"],
    },
    {
        "topic": "omnichannel conversation continuity",
        "keywords": ["channel", "continuity", "chat", "email"],
    },
    {
        "topic": "service language fallback and translation",
        "keywords": ["translation", "fallback", "language", "locale"],
    },
    {
        "topic": "document upload and attachment review",
        "keywords": ["document", "upload", "attachment", "file"],
    },
    {
        "topic": "service eligibility exceptions and approvals",
        "keywords": ["exception", "approval", "override", "eligible"],
    },
    {
        "topic": "self-service search and knowledge discovery",
        "keywords": ["search", "knowledge", "help", "discover"],
    },
    {
        "topic": "service acknowledgement and response receipt",
        "keywords": ["acknowledge", "receipt", "response", "confirm"],
    },
    {
        "topic": "routing confidence and intent ambiguity",
        "keywords": ["ambiguity", "confidence", "intent", "route"],
    },
    {
        "topic": "service personalization and repeat preferences",
        "keywords": ["personalize", "repeat", "preference", "tailor"],
    },
    {
        "topic": "payment method updates and billing profile",
        "keywords": ["payment", "billing", "profile", "method"],
    },
    {
        "topic": "case prioritization and service urgency",
        "keywords": ["urgent", "priority", "case", "triage"],
    },
    {
        "topic": "customer trust and reassurance",
        "keywords": ["trust", "reassure", "confidence", "comfort"],
    },
    {
        "topic": "automation exceptions and human override",
        "keywords": ["automation", "override", "manual", "human"],
    },
    {
        "topic": "next-best-action guidance",
        "keywords": ["next", "action", "guidance", "step"],
    },
    {
        "topic": "service risk and exception monitoring",
        "keywords": ["risk", "monitor", "exception", "alert"],
    },
    {
        "topic": "support education and guided resolution",
        "keywords": ["education", "guided", "resolution", "support"],
    },
    {
        "topic": "knowledge base search and answer discovery",
        "keywords": ["knowledge", "search", "answer", "discover"],
    },
    {
        "topic": "service transcripts and conversation summaries",
        "keywords": ["transcript", "summary", "conversation", "notes"],
    },
    {
        "topic": "customer retention and save offers",
        "keywords": ["retain", "save", "offer", "keep"],
    },
    {
        "topic": "case ownership and handoff tracking",
        "keywords": ["ownership", "handoff", "transfer", "case"],
    },
    {
        "topic": "customer preferences and saved context",
        "keywords": ["preferences", "saved", "context", "repeat"],
    },
    {
        "topic": "service escalation and approval review",
        "keywords": ["approval", "escalation", "review", "exception"],
    },
    {
        "topic": "payment troubleshooting and chargeback support",
        "keywords": ["payment", "chargeback", "troubleshoot", "billing"],
    },
    {
        "topic": "appointment logistics and travel coordination",
        "keywords": ["travel", "logistics", "directions", "arrival"],
    },
    {
        "topic": "customer confidence and reassurance messaging",
        "keywords": ["confidence", "reassurance", "trust", "comfort"],
    },
    {
        "topic": "service insights and product feedback",
        "keywords": ["insights", "product", "feedback", "improve"],
    },
    {
        "topic": "workflow status and queue monitoring",
        "keywords": ["workflow", "status", "queue", "monitor"],
    },
    {
        "topic": "service escalation thresholds and guardrails",
        "keywords": ["threshold", "guardrail", "escalation", "review"],
    },
    {
        "topic": "customer intent and request framing",
        "keywords": ["intent", "request", "frame", "purpose"],
    },
    {
        "topic": "service transcript summarization",
        "keywords": ["transcript", "summary", "conversation", "recap"],
    },
    {
        "topic": "follow-up ownership and callback planning",
        "keywords": ["callback", "ownership", "follow-up", "plan"],
    },
    {
        "topic": "customer preparation checklist and preflight guidance",
        "keywords": ["checklist", "preflight", "prepare", "before"],
    },
    {
        "topic": "service callback timing and response expectations",
        "keywords": ["callback", "response", "expectation", "timing"],
    },
    {
        "topic": "service acknowledgement and receipt confirmation",
        "keywords": ["acknowledge", "receipt", "confirm", "seen"],
    },
    {
        "topic": "service area coverage and eligibility checks",
        "keywords": ["coverage", "area", "eligible", "service area"],
    },
    {
        "topic": "service education and guided resolution",
        "keywords": ["education", "guided", "resolution", "support"],
    },
    {
        "topic": "customer intent detection and request framing",
        "keywords": ["intent", "purpose", "request", "frame"],
    },
]

# Cluster id -> member topics. A cluster is reported even when empty, so
# coverage gaps are visible rather than absent.
RETENTION_TOPIC_CLUSTERS: list[dict[str, object]] = [
    {
        "cluster_id": "operational_flow",
        "topics": [
            "booking status and confirmations", "booking rescheduling and changes", "same-day rescheduling and urgent changes", "queue status and response timing", "service status and progress updates",
        ],
    },
    {
        "cluster_id": "service_recovery",
        "topics": [
            "customer sentiment and recovery", "complaints and service recovery", "issue reproduction and troubleshooting", "service follow-up and resolution tracking", "support escalation and handoff",
        ],
    },
    {
        "cluster_id": "routing_context",
        "topics": [
            "routing and service assignment", "handoff readiness and escalation context", "handoff quality and context completeness", "customer intent detection and routing", "support queue prioritization",
        ],
    },
    {
        "cluster_id": "customer_preparation",
        "topics": [
            "service appointment preparation", "appointment preparation checklists", "service area coverage and eligibility checks", "service quote and estimate review", "customer education and guided walkthroughs",
        ],
    },
    {
        "cluster_id": "channel_preferences",
        "topics": [
            "follow-up preference and communication channel", "contact preferences and channel routing", "appointment reminders and notifications", "language and localization support",
        ],
    },
    {
        "cluster_id": "history_and_trends",
        "topics": [
            "case notes and interaction history", "service history and recurring issues", "customer feedback and survey response", "operational readiness and staffing coverage",
        ],
    },
    {
        "cluster_id": "privacy_and_continuity",
        "topics": [
            "customer consent and communication permissions", "data privacy and information handling", "omnichannel conversation continuity", "service language fallback and translation",
        ],
    },
    {
        "cluster_id": "guidance_and_routing",
        "topics": [
            "routing confidence and intent ambiguity", "next-best-action guidance", "case prioritization and service urgency", "self-service search and knowledge discovery",
        ],
    },
    {
        "cluster_id": "trust_and_approvals",
        "topics": [
            "customer trust and reassurance", "service risk and exception monitoring", "automation exceptions and human override", "service eligibility exceptions and approvals",
        ],
    },
    {
        "cluster_id": "discovery_and_context",
        "topics": [
            "knowledge base search and answer discovery", "service transcripts and conversation summaries", "customer preferences and saved context", "customer confidence and reassurance messaging",
        ],
    },
    {
        "cluster_id": "retention_and_save",
        "topics": [
            "customer retention and save offers", "case ownership and handoff tracking", "service escalation and approval review", "payment troubleshooting and chargeback support",
        ],
    },
    {
        "cluster_id": "logistics_and_visibility",
        "topics": [
            "appointment logistics and travel coordination", "service insights and product feedback", "workflow status and queue monitoring", "arrival timing and eta updates", "service callback timing and response expectations", "delivery tracking and status visibility",
        ],
    },
    {
        "cluster_id": "knowledge_and_assurance",
        "topics": [
            "service transcript summarization", "service education and guided resolution", "customer confidence and reassurance messaging", "customer trust and reassurance", "knowledge base search and answer discovery",
        ],
    },
    {
        "cluster_id": "identity_and_controls",
        "topics": [
            "account verification and identity checks", "service escalation thresholds and guardrails", "data privacy and information handling", "customer intent detection and request framing",
        ],
    },
]

# Derived lookup indexes. Built from the tables above so the table stays the
# single place a topic or cluster is declared.
RETENTION_TOPIC_HINTS_BY_TOPIC: dict[str, list[str]] = {
    str(row["topic"]): [str(keyword) for keyword in row["keywords"]]  # type: ignore[union-attr]
    for row in RETENTION_TOPIC_HINTS
}
RETENTION_CLUSTER_IDS: tuple[str, ...] = tuple(
    str(row["cluster_id"]) for row in RETENTION_TOPIC_CLUSTERS
)

# The ordinal churn-risk scale. Declared once because three readers had their own
# copy, which is how a ranking silently disagrees with itself.
RETENTION_CHURN_RANK: dict[str, float] = {
    "low": 0.0,
    "medium": 1.0,
    "high": 2.0,
    "critical": 3.0,
}
# Ordinals are also exposed by name (banding needs the ordered labels, not just
# the number) and validated against the rank map so a new level cannot be added
# to one and not the other.
RETENTION_CHURN_BANDS: tuple[str, ...] = ("low", "medium", "high", "critical")

# Snapshot-window maintenance and report-shape defaults. ``keep`` is the one the
# prune helpers and the maintenance preview already defaulted to (20).
RETENTION_SNAPSHOT_OPS: dict[str, object] = {
    "default_window_days": 30,
    "prune_keep": 20,
    "type_coverage_denominator": 4,
    "churn_rank_fallback": 0.0,
    "unranked_churn_label": "low",
    "max_anomalies_reported": 25,
}

# Scalar cutoffs used by the recommendation and operational reports. These are the
# numbers that used to be written inline in those two functions; they are declared
# here so a reviewer can see the whole decision surface in one place. The banding
# cutoffs in ``RETENTION_HEALTH_RULES`` intentionally mirror this table's values
# in ``when``-DSL form — the rules carry predicates the scalar table cannot express
# (trend direction, multi-condition AND/OR), so the two are kept adjacent and
# cross-referenced rather than merged.
RETENTION_SAMPLE_THRESHOLDS: dict[str, float] = {
    "sample_size_min": 5,       # <- ``len(snapshots) < 5``
    "trend_min": 3,             # <- ``total >= 3``
    "churn_alert": 2.0,         # <- ``average_churn >= 2.0``
    "loyalty_alert": 50.0,      # <- ``average_loyalty < 50``
    "topic_coverage_target": 0.5,  # <- ``coverage_ratio >= 0.5``
}

# Health banding. ``when`` is the shared rule_engine DSL over the derived
# snapshot metrics assembled by ``build_retention_health_context``. Ordered
# highest priority first; first match wins; a context matching nothing falls
# through to the ``default`` band so there is always a verdict.
#
# Syntax note, because it is the easiest thing to get wrong here: in the shared
# ``when`` DSL a *list* on the right of a key means membership
# (``{"tier": ["a", "b"]}``) and a *dict* means named operators
# (``{"avg": {"gte": 2.5}}``). Writing ``{"avg": [">=", 2.5]}`` does not compare
# anything — it asks whether the value equals the string ">=", which is silently
# false. Every rule below therefore uses the dict form, and
# ``tests/test_retention_policy_expansion.py`` asserts the whole table validates.
#
# Banding deliberately leads on *movement* (deltas and regression slopes) rather
# than on averages. An average lags: a customer that has fallen from 80 to 22
# across four snapshots still averages 51, so a rule keyed on
# ``loyalty_avg < 50`` would call that customer "watch" while they are actively
# leaving. Slope and delta are what say "this is getting worse, now". The
# cutoffs that the pre-existing reports use are in
# ``RETENTION_SAMPLE_THRESHOLDS`` above.
RETENTION_HEALTH_RULES: list[dict[str, object]] = [
    {
        # Must stay at priority 0. Every derived metric is 0.0/empty for an empty
        # series, and 0.0 reads as a *real* measurement: without this rule a
        # customer with no snapshots at all satisfies ``loyalty_avg < 50`` and is
        # banded "at_risk", which reports a measurement that was never taken. The
        # guard belongs in the table rather than in a branch in the resolver
        # precisely because it is a policy claim, not an implementation detail.
        "rule_id": "health_no_data",
        "name": "No snapshot signal",
        "priority": 0,
        "band": "no_data",
        "when": {"all": [{"snapshot_count": {"lte": 0}}]},
        "actions": [
            "Capture a baseline retention snapshot before drawing any conclusion.",
        ],
    },
    {
        "rule_id": "health_critical_collapse",
        "name": "Deteriorating rapidly",
        "priority": 10,
        "band": "critical",
        "when": {
            "all": [
                {"snapshot_count": {"gte": 2}},
                {"churn_delta": {"gte": 1.0}},
                {"loyalty_delta": {"lte": -20.0}},
            ]
        },
        "actions": [
            "Escalate to a retention owner and trigger proactive recovery playbooks.",
            "Review the dominant snapshot type for a recurring failure mode.",
        ],
    },
    {
        "rule_id": "health_critical_weak_loyalty",
        "name": "Low loyalty under churn pressure",
        "priority": 20,
        "band": "critical",
        "when": {"all": [{"loyalty_avg": {"lt": 40.0}}, {"churn_avg": {"gte": 1.5}}]},
        "actions": [
            "Open a save-offer recovery loop before the next billing cycle.",
            "Confirm the customer has a named retention contact.",
        ],
    },
    {
        "rule_id": "health_critical_trend",
        "name": "Steadily worsening",
        "priority": 30,
        "band": "critical",
        "when": {"all": [{"churn_trend": {"gte": 0.4}}, {"loyalty_trend": {"lte": -1.5}}]},
        "actions": [
            "Treat as time-critical: the series is still descending.",
            "Schedule a proactive recovery run and confirm the next contact date.",
        ],
    },
    {
        "rule_id": "health_at_risk_exposure",
        "name": "Elevated churn or weak loyalty",
        "priority": 40,
        "band": "at_risk",
        "when": {"any": [{"churn_avg": {"gte": 2.0}}, {"loyalty_avg": {"lt": 50.0}}]},
        "actions": [
            "Queue a proactive recovery playbook run for this customer.",
            "Review topic clusters for the dominant dissatisfaction theme.",
        ],
    },
    {
        "rule_id": "health_at_risk_trend",
        "name": "Trending the wrong way",
        "priority": 50,
        "band": "at_risk",
        "when": {"any": [{"churn_trend": {"gte": 0.2}}, {"loyalty_trend": {"lte": -0.8}}]},
        "actions": [
            "Monitor closely; the current level is acceptable but the slope is not.",
        ],
    },
    {
        "rule_id": "health_watch_insufficient_data",
        "name": "Insufficient signal",
        "priority": 60,
        "band": "watch",
        "when": {"all": [{"snapshot_count": {"lt": 5}}]},
        "actions": [
            "Collect more snapshots before drawing trend conclusions.",
        ],
    },
    {
        "rule_id": "health_watch_low_trend_confidence",
        "name": "Trend not yet reliable",
        "priority": 70,
        "band": "watch",
        "when": {"all": [{"snapshot_count": {"lt": 3}}]},
        "actions": [
            "Treat trend reporting as directional only at this sample size.",
        ],
    },
    {
        "rule_id": "health_stable",
        "name": "Stable",
        "priority": 900,
        "band": "stable",
        "when": {"all": [{"churn_avg": {"lt": 1.0}}, {"loyalty_avg": {"gte": 65.0}}]},
        "actions": [
            "Maintain the current cadence; no intervention required.",
        ],
    },
]
RETENTION_HEALTH_DEFAULT_BAND: str = "watch"
# ``no_data`` is first because it is an absence of evidence, not a severity:
# it sorts ahead of ``critical`` in a report only to make sure it is never
# confused with a measurement.
RETENTION_HEALTH_BAND_ORDER: tuple[str, ...] = ("no_data", "critical", "at_risk", "watch", "stable")

# Series-shape anomalies. Same pattern as the activity-tree anomaly tables:
# absolute rules over the series, evaluated by the shared engine, reported
# severity-first.
RETENTION_ANOMALY_RULES: list[dict[str, object]] = [
    {
        "rule_id": "anomaly_loyalty_collapse",
        "name": "Loyalty collapse",
        "severity": "critical",
        "when": {"all": [{"snapshot_count": {"gte": 2}}, {"loyalty_delta": {"lte": -25.0}}]},
        "detail": "Loyalty score fell sharply across the window.",
    },
    {
        "rule_id": "anomaly_churn_escalation",
        "name": "Churn escalation",
        "severity": "critical",
        "when": {"all": [{"snapshot_count": {"gte": 2}}, {"churn_delta": {"gte": 1.0}}]},
        "detail": "Churn risk escalated by at least one full band across the window.",
    },
    {
        "rule_id": "anomaly_loyalty_erosion",
        "name": "Loyalty erosion",
        "severity": "warning",
        "when": {"all": [{"snapshot_count": {"gte": 3}}, {"loyalty_delta": {"lte": -10.0}}]},
        "detail": "Loyalty score is trending down across repeated snapshots.",
    },
    {
        "rule_id": "anomaly_stagnation",
        "name": "Stagnation",
        "severity": "info",
        "when": {"all": [{"snapshot_count": {"gte": 3}}, {"abs_loyalty_delta": {"lt": 1.0}}]},
        "detail": "Loyalty score has not moved; the signal is flat.",
    },
    {
        "rule_id": "anomaly_sparse_capture",
        "name": "Sparse capture",
        "severity": "info",
        "when": {"all": [{"snapshot_count": {"lt": 3}}]},
        "detail": "Too few snapshots to distinguish a trend from noise.",
    },
]
RETENTION_ANOMALY_SEVERITIES: tuple[str, ...] = ("critical", "warning", "info")

# Forward projection of the snapshot series. ``method`` is a registry key
# resolved by ``_FORECAST_METHODS``; adding a projection style is one entry
# there, not a branch in the forecaster. Confidence widens with the horizon,
# which is the point: a 30-day projection from three snapshots is not as
# trustworthy as a 7-day projection from thirty, and the payload says so.
RETENTION_FORECAST_RULES: dict[str, object] = {
    "method": "linear_regression",
    "horizons_days": (7, 30, 90),
    "default_horizon_days": 30,
    "min_points": 3,
    "confidence_base": 0.5,
    "confidence_per_horizon_day": 0.012,
    "confidence_floor": 0.05,
    "loyalty_bounds": (0.0, 100.0),
    "churn_bounds": (0.0, 3.0),
}


# ---------------------------------------------------------------------------
# Churn-risk scale (single reader)
# ---------------------------------------------------------------------------


def churn_risk_rank(label: object, fallback: object | None = None) -> float:
    """Ordinal position of a churn-risk label on ``RETENTION_CHURN_RANK``.

    Unknown labels fall back to the configured default (0.0), which is what the
    three inline ``.get(label, 0.0)`` copies did. Passing ``fallback`` overrides
    that for callers that want a different default, e.g. an "unranked is
    suspicious, not benign" caller.
    """
    if fallback is not None:
        return float(fallback)
    return RETENTION_CHURN_RANK.get(
        str(label), float(RETENTION_SNAPSHOT_OPS["churn_rank_fallback"])
    )


def churn_risk_label(rank: float) -> str:
    """Inverse of :func:`churn_risk_rank` — nearest band at or below ``rank``."""
    ranked = sorted(RETENTION_CHURN_RANK.items(), key=lambda item: item[1])
    label = str(RETENTION_SNAPSHOT_OPS["unranked_churn_label"])
    for name, value in ranked:
        if float(value) <= float(rank):
            label = name
    return label


def churn_risk_bands() -> tuple[str, ...]:
    return RETENTION_CHURN_BANDS


# ---------------------------------------------------------------------------
# Topic signal (table-driven)
# ---------------------------------------------------------------------------


def _retention_topic_context_from_summary(summary_text: str) -> list[str]:
    """Free-text summary -> retention-relevant topics, in table order.

    Reads ``RETENTION_TOPIC_HINTS``. The matched-topic list is the *ordered
    subset* of the table whose keywords appear (case-insensitively) in the text,
    so the first match in the table is the "dominant" topic for downstream
    reports. Row order is therefore part of the contract.
    """
    normalized = (summary_text or "").lower()
    matched = [
        str(row["topic"])
        for row in RETENTION_TOPIC_HINTS
        if any(
            str(keyword).lower() in normalized
            for keyword in row["keywords"]  # type: ignore[union-attr]
        )
    ]
    return list(dict.fromkeys(matched))


def _retention_topic_clusters(topics: list[str]) -> dict[str, list[str]]:
    """Bucket topics into the declared clusters.

    Every declared cluster appears in the result, including empty ones, so a
    caller can tell "this cluster matched nothing" apart from "this cluster does
    not exist". That distinction is the whole reason ``matched_clusters`` filters
    rather than the table being pruned to the matches.
    """
    topic_set = set(topics)
    return {
        str(row["cluster_id"]): [
            topic for topic in row["topics"] if topic in topic_set  # type: ignore[union-attr]
        ]
        for row in RETENTION_TOPIC_CLUSTERS
    }


def build_retention_cluster_coverage(
    matched_topics: list[str], matched_clusters: dict[str, list[str]]
) -> dict[str, object]:
    """Which clusters are represented, and which declared clusters are silent.

    A retention review that only lists the clusters that matched cannot answer
    "what are we not hearing about?", which is usually the more useful question.
    """
    represented = [cluster for cluster, members in matched_clusters.items() if members]
    silent = [cluster for cluster in RETENTION_CLUSTER_IDS if cluster not in represented]
    return {
        "clusters_declared": len(RETENTION_CLUSTER_IDS),
        "clusters_represented": len(represented),
        "clusters_silent": len(silent),
        "represented_clusters": represented,
        "silent_clusters": silent,
        "coverage_ratio": round(len(represented) / max(len(RETENTION_CLUSTER_IDS), 1), 2),
    }


def build_retention_topic_signal_report(summary_text: str, user_id: int, window_days: int) -> dict[str, object]:
    matched_topics = _retention_topic_context_from_summary(summary_text)
    topic_selection = type("RetentionTopicSelection", (), {"topic": summary_text})()
    topic_portfolio = build_topic_portfolio_report(topic_selection)
    topic_intelligence = build_topic_intelligence_report(topic_selection)
    topic_suggestions = build_topic_suggestion_report(summary_text, limit=5)
    topic_theme_coverage = build_topic_theme_coverage(topic_selection)
    matched_keywords = list(topic_intelligence.matched_keywords)
    matched_themes = [item.get("theme", getattr(item, "theme", "")) for item in topic_theme_coverage if item.get("matched_count", getattr(item, "matched_count", 0))]
    theme_overlap_scores = [item.get("overlap_score", getattr(item, "overlap_score", 0)) for item in topic_theme_coverage if item.get("matched_count", getattr(item, "matched_count", 0))]
    catalog_topics = {item["topic"] for item in TOPIC_CATALOG}
    topic_clusters = _retention_topic_clusters(matched_topics)
    matched_topic_details = []
    items = []
    for topic in matched_topics[:8]:
        topic_item_selection = type("RetentionTopicSelection", (), {"topic": topic})()
        topic_item_portfolio = build_topic_portfolio_report(topic_item_selection)
        topic_theme_matches = [
            theme.get("theme", getattr(theme, "theme", ""))
            for theme in topic_item_portfolio["theme_coverage"]
            if theme.get("coverage", getattr(theme, "coverage", 0.0)) >= 0.0
        ]
        topic_sectors = [
            sector["sector"]
            for sector in TOPIC_SECTORS
            if topic in sector["topics"]
        ]
        matched_topic_details.append({"topic": topic, "themes": topic_theme_matches, "coverage_ratio": topic_item_portfolio["coverage_ratio"]})
        items.append(
            {
                "topic": topic,
                "matched_keywords": [keyword for keyword in matched_keywords if keyword in topic.lower() or keyword in (summary_text or "").lower()],
                "matched_themes": topic_theme_matches,
                "matched_sectors": topic_sectors or (["retention_operations"] if topic_item_portfolio["matched_topics"] else []),
                "cluster_matches": [cluster for cluster, cluster_topics in topic_clusters.items() if topic in cluster_topics],
                "coverage_score": round(
                    min(
                        1.0,
                        topic_item_portfolio["coverage_ratio"]
                        + (len(matched_keywords) * 0.05)
                        + (len(matched_themes) * 0.03),
                    ),
                    2,
                ),
            }
        )

    expanded_focus = list(
        dict.fromkeys(
            matched_topics[:8]
            + [topic for cluster_topics in topic_clusters.values() for topic in cluster_topics[:2]]
            + [item.topic for item in topic_suggestions.suggested_topics[:3]]
        )
    )[:8]
    topic_focus = list(dict.fromkeys(expanded_focus + matched_topics[:3] + matched_keywords[:3]))[:10]
    active_clusters = [topics for topics in topic_clusters.values() if topics]
    richness_score = round(min(100.0, len(matched_topics) * 6.5 + len(matched_themes) * 8.0 + len(matched_keywords) * 2.5 + len(active_clusters) * 5.0), 2)

    return {
        "generated_at": datetime.now(timezone.utc),
        "user_id": user_id,
        "window_days": window_days,
        "topic_context": summary_text,
        "dominant_topic": matched_topics[0] if matched_topics else None,
        "matched_topics": matched_topics,
        "matched_keywords": matched_keywords,
        "matched_themes": matched_themes,
        "matched_topic_details": matched_topic_details,
        "matched_clusters": {cluster: topics for cluster, topics in topic_clusters.items() if topics},
        "topic_catalog_size": len(catalog_topics),
        "topic_portfolio_coverage": topic_portfolio["coverage_ratio"],
        "topic_suggestions": [item.topic for item in topic_suggestions.suggested_topics],
        "topic_theme_overlap_score": sum(theme_overlap_scores),
        "topic_focus": topic_focus,
        "topic_signal_depth": f"topics={len(matched_topics)}, themes={len(matched_themes)}, keywords={len(matched_keywords)}, clusters={len(active_clusters)}",
        "topic_signal_richness": richness_score,
        "items": items,
        "summary": f"Topic context '{summary_text}' matched {len(matched_topics)} retention-relevant topics across {len(matched_themes)} themes with {len(matched_keywords)} keywords and {len(active_clusters)} clusters. Dominant topic: {matched_topics[0] if matched_topics else 'none'}. Focus topics: {len(topic_focus)}. Signal richness: {richness_score:.2f}. Theme overlap score: {sum(theme_overlap_scores)}. The signal layer now spans privacy, continuity, routing, trust, discovery, retention, and operational-readiness topics.",
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


async def prune_and_report_retention_snapshots(
    db: AsyncSession,
    user_id: int,
    window_days: int,
    keep: int = 20,
) -> dict[str, int | datetime]:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
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
    churn_scores = [churn_risk_rank(snapshot.churn_risk) for snapshot in snapshots]
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
        coverage=round(len(counts_by_type) / max(1, len(snapshots)), 2),
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
    current = snapshots[0] if snapshots else None
    previous = snapshots[1] if len(snapshots) > 1 else None

    if not current:
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
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    churn_rank = RETENTION_CHURN_RANK

    grouped: dict[str, list[models.RetentionSnapshot]] = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.snapshot_type, []).append(snapshot)

    trend_items = []
    for snapshot_type, items in sorted(grouped.items()):
        trend_items.append(
            RetentionSnapshotTrendItem(
                snapshot_type=snapshot_type,
                count=len(items),
                avg_loyalty_score=round(sum(item.loyalty_score for item in items) / max(len(items), 1), 2),
                avg_churn_risk_score=round(sum(churn_rank.get(item.churn_risk, 0.0) for item in items) / max(len(items), 1), 2),
                latest_created_at=max((item.created_at for item in items), default=None),
            )
        )

    return RetentionSnapshotTrendReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        trends=trend_items,
    )


async def build_retention_snapshot_admin_report(db: AsyncSession, window_days: int) -> RetentionSnapshotAdminReport:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    total_snapshots = int(
        (await db.execute(
            select(func.count(models.RetentionSnapshot.id)).where(models.RetentionSnapshot.created_at >= cutoff)
        )).scalar() or 0
    )

    grouped_query = (
        select(
            models.RetentionSnapshot.snapshot_type,
            func.count(models.RetentionSnapshot.id),
            func.avg(models.RetentionSnapshot.loyalty_score),
            func.max(models.RetentionSnapshot.created_at),
        )
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .group_by(models.RetentionSnapshot.snapshot_type)
        .order_by(desc(func.count(models.RetentionSnapshot.id)))
    )
    grouped_result = await db.execute(grouped_query)

    items = [
        RetentionSnapshotAdminItem(
            snapshot_type=str(snapshot_type),
            count=int(count_value or 0),
            avg_loyalty_score=round(float(avg_loyalty or 0.0), 2),
            latest_created_at=latest_created_at,
        )
        for snapshot_type, count_value, avg_loyalty, latest_created_at in grouped_result.all()
    ]

    return RetentionSnapshotAdminReport(
        generated_at=now,
        window_days=window_days,
        total_snapshots=total_snapshots,
        items=items,
    )


async def build_user_retention_snapshot_health(db: AsyncSession, user_id: int, window_days: int) -> UserRetentionSnapshotHealth:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = (
        select(models.RetentionSnapshot)
        .where(models.RetentionSnapshot.user_id == user_id)
        .where(models.RetentionSnapshot.created_at >= cutoff)
        .order_by(desc(models.RetentionSnapshot.created_at))
    )
    result = await db.execute(query)
    snapshots = result.scalars().all()

    snapshot_count = len(snapshots)
    avg_loyalty_score = round(sum(snapshot.loyalty_score for snapshot in snapshots) / max(snapshot_count, 1), 2)

    latest = snapshots[0] if snapshots else None

    return UserRetentionSnapshotHealth(
        user_id=user_id,
        window_days=window_days,
        snapshot_count=snapshot_count,
        latest_snapshot_type=latest.snapshot_type if latest else None,
        latest_lifecycle_stage=latest.lifecycle_stage if latest else None,
        avg_loyalty_score=avg_loyalty_score,
        generated_at=datetime.now(timezone.utc),
    )


async def build_retention_dashboard(db: AsyncSession, user_id: int, window_days: int) -> RetentionDashboard:
    snapshot_report = await build_retention_snapshot_report(db, user_id, window_days)
    snapshot_delta = await build_retention_snapshot_delta(db, user_id, window_days)
    snapshot_trends = await build_retention_snapshot_trends(db, user_id, window_days)
    chat_rows, bookings = await load_user_interaction_window(db, user_id, window_days)
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
    churn_prediction = await build_churn_prediction(db, user_id, window_days)
    snapshot_operations_report = await build_retention_snapshot_operations_report(db, window_days, stale_after_days=window_days)
    summary_text = summary.metadata.get("summary", "") if getattr(summary, "metadata", None) else ""
    if not summary_text and summary.top_issues:
        summary_text = ", ".join(summary.top_issues)
    topic_signal_report = build_retention_topic_signal_report(summary_text, user_id, window_days)
    topic_signal_detail = build_retention_dashboard_topic_signal_detail(summary, latest_sentiment)

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
        topic_signal_detail=topic_signal_detail,
        topic_theme_coverage=topic_signal_detail.topic_theme_coverage,
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

    if len(snapshots) < int(RETENTION_SAMPLE_THRESHOLDS["sample_size_min"]):
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
    average_churn = sum(churn_risk_rank(snapshot.churn_risk) for snapshot in snapshots) / max(len(snapshots), 1)
    if average_churn >= float(RETENTION_SAMPLE_THRESHOLDS["churn_alert"]):
        recommendations.append(
            {
                "priority": "high",
                "area": "churn-risk",
                "recommendation": "Trigger proactive recovery steps for the highest-risk users and route by topic.",
                "evidence": f"Average churn risk score is {round(average_churn, 2)} with topic catalog coverage at {topic_portfolio['coverage_ratio']:.2f} and {len(topic_portfolio['matched_topics'])} matched topics.",
            }
        )
    if average_loyalty < float(RETENTION_SAMPLE_THRESHOLDS["loyalty_alert"]):
        recommendations.append(
            {
                "priority": "high",
                "area": "loyalty",
                "recommendation": "Add a retention recovery loop to improve loyalty signals and match topic themes.",
                "evidence": f"Average loyalty score is {round(average_loyalty, 2)}; theme coverage spans {topic_theme_count} groups and {len(topic_portfolio['matched_topics'])} matched topics.",
            }
        )

    # Band-derived recommendation, appended last so the entries above keep their
    # order and this one is strictly additive.
    #
    # This block referenced a local named `health` that the function never
    # assigned, so it raised `NameError` on every call that got past the
    # `if not snapshots` early return. It survived because the function has no
    # callers -- `build_retention_recommendations` is unreachable from any
    # router -- so the crash never fired. Ruff's F821 is what named it.
    #
    # Repaired through `build_retention_health_report` rather than by assembling
    # the context here: that helper already does `snapshot_series_points` ->
    # `build_retention_health_context` -> `resolve_retention_health` and is what
    # the async dashboard helpers use, so calling it is what keeps this
    # recommendation's band identical to the one shown everywhere else. Building
    # the context locally would have been a second, free to drift.
    health = build_retention_health_report(snapshots)["health"]
    if health["actions"]:
        recommendations.append(
            {
                "priority": "high" if health["band"] == "critical" else "medium",
                "area": f"health-band:{health['band']}",
                "recommendation": str(health["rule_name"]),
                "evidence": f"Rule {health['rule_id']} fired on {sorted(health['matched_fields'])}; suggested actions: {'; '.join(health['actions'])}.",
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
        + f" Topic coverage: {topic_portfolio['coverage_ratio']:.2f}; matched topics: {len(topic_intelligence.matched_keywords)}; themes: {len(topic_portfolio['theme_coverage'])}. Discovery and retention families are now surfaced more explicitly.",
    )


async def build_retention_operational_report(db: AsyncSession, user_id: int, window_days: int) -> RetentionOperationalReport:
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    counts_by_type = await count_retention_snapshots_by_type(db, user_id, window_days)
    total = len(snapshots)
    dominant_type = max(counts_by_type, key=counts_by_type.get) if counts_by_type else None
    topic_portfolio = build_topic_portfolio_report(None)
    sample_size_min = int(RETENTION_SAMPLE_THRESHOLDS["sample_size_min"])
    trend_min = int(RETENTION_SAMPLE_THRESHOLDS["trend_min"])
    coverage_target = float(RETENTION_SAMPLE_THRESHOLDS["topic_coverage_target"])
    items = [
        RetentionOperationalItem(
            name="snapshot_volume",
            status="healthy" if total >= sample_size_min else "watch",
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
            status="healthy" if total >= trend_min else "watch",
            detail=("Trend reporting has enough signal to support action." if total >= trend_min else "Collect more data before relying on trend reporting.") + f" Theme coverage count: {len(topic_portfolio['theme_coverage'])}.",
            owner_hint="product analytics",
        ),
        RetentionOperationalItem(
            name="topic_coverage",
            status="healthy" if topic_portfolio["coverage_ratio"] >= coverage_target else "watch",
            detail=f"Topic coverage ratio is {topic_portfolio['coverage_ratio']:.2f} with {len(topic_portfolio['matched_topics'])} matched topics and {len(topic_portfolio['theme_coverage'])} theme buckets.",
            owner_hint="retention analytics",
        ),
    ]
    return RetentionOperationalReport(
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        window_days=window_days,
        items=items,
        summary=("Retention operations are stable enough for action." if total >= trend_min else "Retention operations need more data before strong conclusions.") + f" Topic portfolio coverage is {topic_portfolio['coverage_ratio']:.2f} across {len(topic_portfolio['theme_coverage'])} themes.",
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
    )# ---------------------------------------------------------------------------
# Series normalization
# ---------------------------------------------------------------------------
#
# The forecast, health, and anomaly surfaces all read the *same* normalized
# point series rather than each re-deriving loyalty/churn/timestamp off the ORM
# rows. That is deliberate: a metric computed three ways from three places is
# three answers to one question, and the disagreement is invisible until someone
# compares two endpoints by hand.


def snapshot_series_points(snapshots: list[object]) -> list[dict[str, object]]:
    """Normalize ``RetentionSnapshot`` rows (or plain dicts) to an ordered series.

    Ordered oldest -> newest, which is the direction a trend is read in. Accepts
    both ORM objects and dicts so the analytics below are testable with no
    database and so a caller can feed a hand-built series.
    """
    points: list[dict[str, object]] = []
    for index, snapshot in enumerate(snapshots or []):
        if isinstance(snapshot, dict):
            get = snapshot.get
        else:
            def get(key, default=None, _obj=snapshot):
                return getattr(_obj, key, default)

        created_at = get("created_at")
        points.append(
            {
                "index": index,
                "snapshot_id": get("id"),
                "snapshot_type": str(get("snapshot_type") or ""),
                "lifecycle_stage": str(get("lifecycle_stage") or ""),
                "loyalty_score": float(get("loyalty_score", 0.0) or 0.0),
                "churn_risk": str(get("churn_risk") or ""),
                "churn_rank": churn_risk_rank(get("churn_risk")),
                "created_at": created_at,
            }
        )
    # The loaders already order newest-first; normalize to oldest-first so a
    # caller that passes either direction gets the same series.
    points.sort(key=lambda point: (point["created_at"] is None, point["created_at"]))
    for position, point in enumerate(points):
        point["index"] = position
    return points


def build_retention_series_metrics(points: list[dict[str, object]]) -> dict[str, object]:
    """Scalar metrics over a series: averages, deltas, and dispersion.

    ``*_delta`` values are last minus first, so a positive ``churn_delta`` means
    the customer got worse. ``abs_loyalty_delta`` exists because the stagnation
    anomaly needs "did not move" and a signed field cannot express that in a
    ``when`` rule.
    """
    count = len(points)
    loyalty_values = [float(point["loyalty_score"]) for point in points]
    churn_values = [float(point["churn_rank"]) for point in points]

    loyalty_avg = round(fmean(loyalty_values), 2) if loyalty_values else 0.0
    churn_avg = round(fmean(churn_values), 2) if churn_values else 0.0
    loyalty_delta = round(loyalty_values[-1] - loyalty_values[0], 2) if count >= 2 else 0.0
    churn_delta = round(churn_values[-1] - churn_values[0], 2) if count >= 2 else 0.0

    return {
        "snapshot_count": count,
        "loyalty_avg": loyalty_avg,
        "loyalty_min": round(min(loyalty_values), 2) if loyalty_values else 0.0,
        "loyalty_max": round(max(loyalty_values), 2) if loyalty_values else 0.0,
        "loyalty_latest": round(loyalty_values[-1], 2) if loyalty_values else 0.0,
        "loyalty_stdev": round(pstdev(loyalty_values), 2) if count >= 2 else 0.0,
        "loyalty_delta": loyalty_delta,
        "abs_loyalty_delta": round(abs(loyalty_delta), 2),
        "churn_avg": churn_avg,
        "churn_latest": round(churn_values[-1], 2) if churn_values else 0.0,
        "churn_max": round(max(churn_values), 2) if churn_values else 0.0,
        "churn_delta": churn_delta,
        "churn_stdev": round(pstdev(churn_values), 2) if count >= 2 else 0.0,
        "churn_latest_label": str(points[-1]["churn_risk"]) if points else "",
        "snapshot_types": sorted({str(point["snapshot_type"]) for point in points if point["snapshot_type"]}),
        "lifecycle_stages": sorted({str(point["lifecycle_stage"]) for point in points if point["lifecycle_stage"]}),
    }


def build_retention_health_context(points: list[dict[str, object]]) -> dict[str, object]:
    """Metrics plus trend slope, which is what the health rules band on.

    ``churn_trend`` is the per-snapshot change in churn rank (positive =
    deteriorating) derived by linear regression over the series index, so a
    customer oscillating between bands does not read as a steady decline.
    """
    metrics = build_retention_series_metrics(points)
    loyalty_slope, churn_slope = _linear_slopes(points)
    metrics["loyalty_trend"] = round(loyalty_slope, 4)
    metrics["churn_trend"] = round(churn_slope, 4)
    return metrics


# ---------------------------------------------------------------------------
# Health banding
# ---------------------------------------------------------------------------


def resolve_retention_health(
    context: dict[str, object],
    *,
    effective_date: Any = None,
) -> dict[str, object]:
    """Band a health context via ``RETENTION_HEALTH_RULES``.

    Rules are evaluated in declared priority order and the first match wins, so
    the table is a precedence list rather than a set of independent predicates.
    A context that matches nothing gets the configured default band, which means
    the function is total: there is always a verdict, never an exception and
    never ``None``.
    """
    for rule in sorted(RETENTION_HEALTH_RULES, key=lambda item: int(item["priority"])):
        if rule.get("enabled", True) is False:
            continue
        matched, fields = evaluate_when(
            rule.get("when", {}), context, effective_date=effective_date
        )
        if matched:
            return {
                "band": str(rule["band"]),
                "rule_id": str(rule["rule_id"]),
                "rule_name": str(rule["name"]),
                "priority": int(rule["priority"]),
                "actions": [str(action) for action in rule.get("actions", [])],  # type: ignore[union-attr]
                "matched_fields": fields,
                "band_rank": RETENTION_HEALTH_BAND_ORDER.index(str(rule["band"]))
                if str(rule["band"]) in RETENTION_HEALTH_BAND_ORDER
                else len(RETENTION_HEALTH_BAND_ORDER),
                "default_applied": False,
            }
    return {
        "band": RETENTION_HEALTH_DEFAULT_BAND,
        "rule_id": "default",
        "rule_name": "Default (no rule matched)",
        "priority": 10_000,
        "actions": [],
        "matched_fields": {},
        "band_rank": RETENTION_HEALTH_BAND_ORDER.index(RETENTION_HEALTH_DEFAULT_BAND)
        if RETENTION_HEALTH_DEFAULT_BAND in RETENTION_HEALTH_BAND_ORDER
        else len(RETENTION_HEALTH_BAND_ORDER),
        "default_applied": True,
    }


def build_retention_health_report(
    snapshots: list[object],
    *,
    effective_date: Any = None,
) -> dict[str, object]:
    """Band + anomalies for one customer's snapshot series.

    Pure: takes the rows, returns the verdict. The async dashboard/report helpers
    load the rows and call this, so the decision is unit-testable without a
    database and identical wherever it is surfaced.
    """
    points = snapshot_series_points(snapshots)
    context = build_retention_health_context(points)
    health = resolve_retention_health(context, effective_date=effective_date)
    anomalies = detect_retention_anomalies(context, effective_date=effective_date)
    return {
        "generated_at": datetime.now(timezone.utc),
        "metrics": context,
        "health": health,
        "anomalies": anomalies,
        "series_length": len(points),
    }


# ---------------------------------------------------------------------------
# Series anomaly detection
# ---------------------------------------------------------------------------


def detect_retention_anomalies(
    context: dict[str, object],
    *,
    effective_date: Any = None,
) -> list[dict[str, object]]:
    """Findings from ``RETENTION_ANOMALY_RULES`` over the snapshot series.

    Returned ordered by severity (``critical`` first) then by rule declaration
    order, and truncated to the configured reporting cap so a pathological
    series cannot produce an unbounded response. The truncation is reported as a
    count on the list's sibling field by the caller that embeds it.
    """
    cap = int(RETENTION_SNAPSHOT_OPS["max_anomalies_reported"])
    findings: list[dict[str, object]] = []
    for rule in RETENTION_ANOMALY_RULES:
        if rule.get("enabled", True) is False:
            continue
        matched, fields = evaluate_when(
            rule.get("when", {}), context, effective_date=effective_date
        )
        if not matched:
            continue
        findings.append(
            {
                "rule_id": str(rule["rule_id"]),
                "name": str(rule["name"]),
                "severity": str(rule["severity"]),
                "detail": str(rule["detail"]),
                "matched_fields": fields,
            }
        )
    findings.sort(key=lambda finding: RETENTION_ANOMALY_SEVERITIES.index(str(finding["severity"]))
                  if str(finding["severity"]) in RETENTION_ANOMALY_SEVERITIES
                  else len(RETENTION_ANOMALY_SEVERITIES))
    return findings[:cap]


# ---------------------------------------------------------------------------
# Forward projection
# ---------------------------------------------------------------------------


def _linear_slopes(points: list[dict[str, object]]) -> tuple[float, float]:
    """Least-squares slope of loyalty and churn rank against series index.

    Returns ``(0.0, 0.0)`` for a series too short to fit, and for a perfectly
    flat one. Exposed to the forecast rather than inlined so the health context
    and the projection cannot disagree about what "the trend" means.
    """
    count = len(points)
    if count < 2:
        return 0.0, 0.0
    xs = [float(point["index"]) for point in points]  # type: ignore[arg-type]
    mean_x = fmean(xs)
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance <= 0.0:
        return 0.0, 0.0

    def slope_for(values: list[float]) -> float:
        mean_y = fmean(values)
        numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
        return numerator / variance

    return slope_for([float(point["loyalty_score"]) for point in points]), slope_for(
        [float(point["churn_rank"]) for point in points]
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# Projection styles, keyed by ``RETENTION_FORECAST_RULES["method"]``. Each takes
# the points, the per-snapshot slope, the number of snapshots ahead to project,
# and the metric's bounds, and returns the projected value. Adding a style is one
# entry here.
_FORECAST_METHODS = {
    "linear_regression": lambda slope: slope,
    "last_value": lambda slope: 0.0,
    "damped": lambda slope: slope * 0.5,
}


def forecast_confidence(horizon_days: int, point_count: int) -> float:
    """Confidence for a projection, lower for longer horizons and thinner series.

    Both effects are real: a 90-day projection is not as knowable as a 7-day one,
    and three snapshots are not as informative as thirty. The base term and the
    per-day and per-point terms are all config, so the shape of the decay is
    tunable without touching the arithmetic.
    """
    base = float(RETENTION_FORECAST_RULES["confidence_base"])
    per_day = float(RETENTION_FORECAST_RULES["confidence_per_horizon_day"])
    points = max(int(point_count), 1)
    evidence = 1.0 - (1.0 / points) if points > 1 else 0.0
    decay = max(0.0, 1.0 - (per_day * max(int(horizon_days), 0)))
    return round(_clamp(base * (0.35 + (0.65 * evidence)) * decay,
                        float(RETENTION_FORECAST_RULES["confidence_floor"]),
                        1.0), 4)


def build_retention_forecast(
    snapshots: list[object],
    *,
    horizon_days: int | None = None,
    method: str | None = None,
) -> dict[str, object]:
    """Project loyalty and churn forward over a snapshot series.

    Refuses to project below ``min_points`` and says so in ``sufficient_data``
    rather than returning a confident-looking number derived from two readings:
    the honest answer to "where is this customer in 30 days" from two snapshots
    is "not knowable", and an API that answers anyway is worse than one that
    declines. Confidence is reported alongside every projection so a consumer can
    decide how much weight to give it.
    """
    points = snapshot_series_points(snapshots)
    metrics = build_retention_health_context(points)
    resolved_method = str(method or RETENTION_FORECAST_RULES["method"])
    steps_per_horizon = max(1.0, float(RETENTION_FORECAST_RULES["default_horizon_days"]) / 7.0)
    steps = int(round(max(float(horizon_days or RETENTION_FORECAST_RULES["default_horizon_days"]), 0.0) / steps_per_horizon))
    min_points = int(RETENTION_FORECAST_RULES["min_points"])
    method_fn = _FORECAST_METHODS.get(resolved_method)
    count = len(points)

    if method_fn is None or count < min_points:
        return {
            "generated_at": datetime.now(timezone.utc),
            "sufficient_data": False,
            "reason": (
                f"unknown forecast method {resolved_method!r}"
                if method_fn is None
                else f"needs at least {min_points} snapshots, got {count}"
            ),
            "method": resolved_method,
            "horizon_days": int(horizon_days or RETENTION_FORECAST_RULES["default_horizon_days"]),
            "projected_steps": steps,
            "series_length": count,
            "min_points": min_points,
            "confidence": 0.0,
            "metrics": metrics,
            "available_methods": sorted(_FORECAST_METHODS),
        }

    loyalty_damping = method_fn(float(metrics["loyalty_trend"]))
    churn_damping = method_fn(float(metrics["churn_trend"]))
    loyalty_low, loyalty_high = (float(bound) for bound in RETENTION_FORECAST_RULES["loyalty_bounds"])  # type: ignore[misc]
    churn_low, churn_high = (float(bound) for bound in RETENTION_FORECAST_RULES["churn_bounds"])  # type: ignore[misc]

    loyalty_projected = _clamp(
        float(metrics["loyalty_latest"]) + (loyalty_damping * steps), loyalty_low, loyalty_high
    )
    churn_projected = _clamp(
        float(metrics["churn_latest"]) + (churn_damping * steps), churn_low, churn_high
    )
    resolved_horizon = int(horizon_days or RETENTION_FORECAST_RULES["default_horizon_days"])

    return {
        "generated_at": datetime.now(timezone.utc),
        "sufficient_data": True,
        "reason": "",
        "method": resolved_method,
        "horizon_days": resolved_horizon,
        "projected_steps": steps,
        "series_length": count,
        "min_points": min_points,
        "confidence": forecast_confidence(resolved_horizon, count),
        "metrics": metrics,
        "loyalty": {
            "latest": float(metrics["loyalty_latest"]),
            "projected": round(loyalty_projected, 2),
            "change": round(loyalty_projected - float(metrics["loyalty_latest"]), 2),
            "bounds": [loyalty_low, loyalty_high],
        },
        "churn": {
            "latest": round(float(metrics["churn_latest"]), 2),
            "latest_label": str(metrics["churn_latest_label"]),
            "projected": round(churn_projected, 2),
            "projected_label": churn_risk_label(churn_projected),
            "change": round(churn_projected - float(metrics["churn_latest"]), 2),
            "bounds": [churn_low, churn_high],
        },
        "available_methods": sorted(_FORECAST_METHODS),
    }


def build_retention_horizon_sweep(
    snapshots: list[object],
    *,
    method: str | None = None,
) -> dict[str, object]:
    """The same projection evaluated at every configured horizon.

    One series in, the full confidence-decay curve out. This is what makes the
    confidence numbers legible: a consumer sees that the 90-day number is nearly
    worthless next to the 7-day one instead of having to trust a single figure.
    """
    points = snapshot_series_points(snapshots)
    horizons = [int(horizon) for horizon in RETENTION_FORECAST_RULES["horizons_days"]]  # type: ignore[union-attr]
    return {
        "generated_at": datetime.now(timezone.utc),
        "series_length": len(points),
        "method": str(method or RETENTION_FORECAST_RULES["method"]),
        "horizons": [
            {
                "horizon_days": horizon,
                **{
                    key: value
                    for key, value in build_retention_forecast(
                        points, horizon_days=horizon, method=method
                    ).items()
                    if key in {"sufficient_data", "confidence", "loyalty", "churn", "reason"}
                },
            }
            for horizon in horizons
        ],
    }
# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def build_retention_catalog() -> dict[str, Any]:
    """Introspection payload for ``/meta/scoring-catalog``.

    Reports the policy tables, the derived indexes they produce, and the band
    vocabularies, so an operator can see which rule *could* have fired and which
    bands exist without reading the module. A band or severity that is
    unreachable from the table is surfaced as a count of zero rather than
    omitted, because a configured band that never fires is exactly the kind of
    gap this catalog exists to make visible.
    """
    band_counts: dict[str, int] = {band: 0 for band in RETENTION_HEALTH_BAND_ORDER}
    for rule in RETENTION_HEALTH_RULES:
        band = str(rule["band"])
        band_counts[band] = band_counts.get(band, 0) + 1

    severity_counts: dict[str, int] = {severity: 0 for severity in RETENTION_ANOMALY_SEVERITIES}
    for rule in RETENTION_ANOMALY_RULES:
        severity = str(rule["severity"])
        severity_counts[severity] = severity_counts.get(severity, 0) + 1

    cluster_sizes = {
        str(row["cluster_id"]): len(row["topics"])  # type: ignore[arg-type]
        for row in RETENTION_TOPIC_CLUSTERS
    }

    return {
        "catalog_version": "retention_v2",
        "layers": {
            "read": [
                "snapshot_report",
                "snapshot_delta",
                "snapshot_trends",
                "snapshot_summary",
                "snapshot_admin_report",
                "user_snapshot_health",
                "retention_dashboard",
                "retention_recommendations",
                "retention_coverage_report",
                "retention_operational_report",
                "retention_maintenance_preview",
            ],
            "policy": [
                "topic_hints",
                "topic_clusters",
                "cluster_coverage",
                "churn_rank",
                "health_banding",
                "anomaly_detection",
                "forecast",
                "horizon_sweep",
            ],
        },
        "when_dsl": "shared rule_engine.evaluate_when (combinators, numeric/list/date ops)",
        "config_tables": {
            "topic_hints": {
                "name": "RETENTION_TOPIC_HINTS",
                "rows": len(RETENTION_TOPIC_HINTS),
                "keywords": sum(len(row["keywords"]) for row in RETENTION_TOPIC_HINTS),  # type: ignore[arg-type]
                "match": "case-insensitive substring against the lowercased summary",
                "order_sensitive": True,
            },
            "topic_clusters": {
                "name": "RETENTION_TOPIC_CLUSTERS",
                "rows": len(RETENTION_TOPIC_CLUSTERS),
                "members": sum(len(row["topics"]) for row in RETENTION_TOPIC_CLUSTERS),  # type: ignore[arg-type]
                "cluster_sizes": cluster_sizes,
                "empty_clusters_reported": True,
            },
            "churn_rank": {
                "name": "RETENTION_CHURN_RANK",
                "scale": RETENTION_CHURN_RANK,
                "bands": list(RETENTION_CHURN_BANDS),
                "unranked_fallback": RETENTION_SNAPSHOT_OPS["churn_rank_fallback"],
                "single_reader": "churn_risk_rank / churn_risk_label",
            },
            "snapshot_ops": {"name": "RETENTION_SNAPSHOT_OPS", "values": dict(RETENTION_SNAPSHOT_OPS)},
            "health_rules": {
                "name": "RETENTION_HEALTH_RULES",
                "rows": len(RETENTION_HEALTH_RULES),
                "band_order": list(RETENTION_HEALTH_BAND_ORDER),
                "rules_by_band": band_counts,
                "default_band": RETENTION_HEALTH_DEFAULT_BAND,
                "precedence": "ascending priority, first match wins",
            },
            "anomaly_rules": {
                "name": "RETENTION_ANOMALY_RULES",
                "rows": len(RETENTION_ANOMALY_RULES),
                "severity_order": list(RETENTION_ANOMALY_SEVERITIES),
                "rules_by_severity": severity_counts,
                "report_cap": RETENTION_SNAPSHOT_OPS["max_anomalies_reported"],
            },
            "forecast_rules": {
                "name": "RETENTION_FORECAST_RULES",
                "values": {
                    key: (list(value) if isinstance(value, tuple) else value)
                    for key, value in RETENTION_FORECAST_RULES.items()
                },
                "available_methods": sorted(_FORECAST_METHODS),
            },
        },
    }


# ---------------------------------------------------------------------------
# Async entry points
# ---------------------------------------------------------------------------
#
# The analytics above are pure and take rows. These load the rows and delegate,
# so a router never has to know which stage does the work and the decision logic
# stays testable with no database.


async def build_retention_health_report_for_user(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
    *,
    effective_date: Any = None,
) -> dict[str, Any]:
    """Health band + anomalies for one customer over a window."""
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    report = build_retention_health_report(snapshots, effective_date=effective_date)
    report["user_id"] = int(user_id)
    report["window_days"] = int(window_days)
    return report


async def build_retention_forecast_for_user(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
    *,
    horizon_days: int | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    """Forward projection of one customer's snapshot series."""
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    forecast = build_retention_forecast(snapshots, horizon_days=horizon_days, method=method)
    forecast["user_id"] = int(user_id)
    forecast["window_days"] = int(window_days)
    return forecast


async def build_retention_horizon_sweep_for_user(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
    *,
    method: str | None = None,
) -> dict[str, Any]:
    """The projection evaluated at every configured horizon for one customer."""
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    sweep = build_retention_horizon_sweep(snapshots, method=method)
    sweep["user_id"] = int(user_id)
    sweep["window_days"] = int(window_days)
    return sweep


async def build_retention_cluster_coverage_for_user(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
) -> dict[str, Any]:
    """Which topic clusters the customer's snapshots speak to, and which do not.

    Unlike the topic-signal report this is *absence*-oriented: the point is the
    silent clusters, so it reads the newest snapshot's summary text rather than
    re-deriving a signal report.
    """
    snapshots = await _load_retention_snapshots(db, user_id, window_days)
    summary_text = str(getattr(snapshots[0], "summary_json", "") or "") if snapshots else ""
    matched = _retention_topic_context_from_summary(summary_text)
    clusters = _retention_topic_clusters(matched)
    return {
        "generated_at": datetime.now(timezone.utc),
        "user_id": int(user_id),
        "window_days": int(window_days),
        "matched_topics": matched,
        "cluster_members": {cluster: members for cluster, members in clusters.items() if members},
        **build_retention_cluster_coverage(matched, clusters),
    }
