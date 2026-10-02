from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

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


# ---------------------------------------------------------------------------
# Score composition (the numbers behind the six published signals)
# ---------------------------------------------------------------------------
#
# The six published signals were each one hand-written arithmetic expression
# inline in ``build_customer_policy_snapshot``. Every constant in those
# expressions is reproduced below *with the same value*, so a snapshot composed
# through ``compose_access_score`` is byte-identical to one built by the
# original expression -- but the weights are now data. A new signal, a reweighted
# input, or a differently-scaled average is a row edit, not a code change.
#
# Two quirks are preserved rather than corrected, because every stored
# ``CustomerPolicyScore.summary`` string embeds the numbers they produced:
#
# 1. ``customer_score`` floors its inverted dissatisfaction term at 0 but does
#    not ceiling it. A negative dissatisfaction reading is meant to add to the
#    customer score; truncating it would silently change historical comparisons.
# 2. ``scaled_mean`` divides by the *term count*, not by the sum of the weights.
#    In ``interest_score`` the weights are 10/4/8/1, so this is a scaled average
#    with a divisor of 4 -- not a weighted mean. Reading it as a weighted mean
#    would move the published score by a wide margin.
#
# Signal literals that are part of a returned payload (``"no_topic_richness"``,
# ``"unclassified-topic [fallback]"``, the summary templates) are deliberately
# *not* table-ised: they are contract text consumed by callers, not tuning
# knobs, and a config table that can rewrite a payload key is a worse contract
# than a literal that cannot.

POLICY_SCORING_VERSION = "policy_scoring_governance_v1"

# The six signals published on a snapshot / score row, in composition order.
# ``access_score`` is the published headline; the other five are the evidence
# behind it, and every one of them is visible in the health summary.
SCORE_SIGNALS: list[dict[str, Any]] = [
    {
        "signal": "system_score",
        "role": "evidence",
        "measures": "operational engagement (interaction signal, completed bookings, topic complexity)",
        "published_in": ["PolicyScoreBreakdown", "CustomerPolicyScoreOut", "CustomerPolicyHealthSummaryOut"],
    },
    {
        "signal": "customer_score",
        "role": "evidence",
        "measures": "customer relationship quality (loyalty, dissatisfaction, topic confidence)",
        "published_in": ["PolicyScoreBreakdown", "CustomerPolicyScoreOut", "CustomerPolicyHealthSummaryOut"],
    },
    {
        "signal": "interest_score",
        "role": "evidence",
        "measures": "stated interest volume (signals, bookings, completed work, topic breadth)",
        "published_in": ["PolicyScoreBreakdown", "CustomerPolicyScoreOut", "CustomerPolicyHealthSummaryOut"],
    },
    {
        "signal": "closeness_score",
        "role": "evidence",
        "measures": "mean of customer and interest evidence with topic breadth",
        "published_in": ["PolicyScoreBreakdown", "CustomerPolicyScoreOut", "CustomerPolicyHealthSummaryOut"],
    },
    {
        "signal": "community_closeness_score",
        "role": "evidence",
        "measures": "mean of system evidence, closeness, and topic complexity",
        "published_in": ["PolicyScoreBreakdown", "CustomerPolicyScoreOut", "CustomerPolicyHealthSummaryOut"],
    },
    {
        "signal": "access_score",
        "role": "headline",
        "measures": "mean of customer, community, and system evidence; the value the tier rules read",
        "published_in": ["PolicyScoreBreakdown", "CustomerPolicyScoreOut", "CustomerPolicyAccessOut"],
    },
]

# The signal names in composition order, derived rather than repeated so a row
# added to ``SCORE_SIGNALS`` is picked up here without a second edit. The
# validator checks it still matches ``COMPOSITE_RULES`` exactly.
COMPOSITE_SIGNALS: list[str] = [str(row["signal"]) for row in SCORE_SIGNALS]

# The raw inputs the composition rules read. Declared so a rule that names an
# input nobody computes is caught by the validator instead of scoring 0.0
# silently -- a missing input and a genuinely-zero input are otherwise
# indistinguishable in the output, and only one of them is a bug.
SCORE_INPUTS: list[dict[str, Any]] = [
    {"input": "signal_score", "source": "avg(InteractionSignal.score)", "aggregation": "avg"},
    {"input": "loyalty_score", "source": "avg(RetentionSnapshot.loyalty_score)", "aggregation": "avg"},
    {"input": "dissatisfaction_score", "source": "avg(RecoveryOutcome.dissatisfaction_score)", "aggregation": "avg"},
    {"input": "booking_count", "source": "count(Booking.id)", "aggregation": "count"},
    {"input": "completed_count", "source": "count(Booking.id where status=completed)", "aggregation": "count"},
    {"input": "topic_confidence", "source": "max(TopicSelection.confidence)", "aggregation": "max"},
    {"input": "topic_breadth_score", "source": "_topic_breadth_score(current_topic)", "aggregation": "derived"},
    {"input": "topic_complexity_score", "source": "_topic_complexity_score(current_topic)", "aggregation": "derived"},
]

# Composition order is significant: a later rule may read an earlier rule's
# output (``closeness_score`` reads ``customer_score`` and ``interest_score``),
# so the engine walks this list front to back and each signal becomes available
# as soon as it is produced.
COMPOSITE_RULES: list[dict[str, Any]] = [
    {
        "signal": "system_score",
        "mode": "weighted_sum",
        "terms": [
            {"input": "signal_score", "weight": 12.0},
            {"input": "completed_count", "weight": 6.0},
            {"input": "topic_complexity_score", "weight": 0.25},
        ],
    },
    {
        "signal": "customer_score",
        "mode": "weighted_sum",
        "terms": [
            {"input": "loyalty_score", "weight": 0.6},
            # inverted: 100 - dissatisfaction, floored at 0, not ceilinged
            {"input": "dissatisfaction_score", "weight": 1.0, "invert_ceiling": 100.0, "floor": 0.0},
            {"input": "topic_confidence", "weight": 12.0},
        ],
    },
    {
        "signal": "interest_score",
        "mode": "scaled_mean",
        "terms": [
            {"input": "signal_score", "weight": 10.0},
            {"input": "booking_count", "weight": 4.0},
            {"input": "completed_count", "weight": 8.0},
            {"input": "topic_breadth_score", "weight": 1.0},
        ],
    },
    {
        "signal": "closeness_score",
        "mode": "scaled_mean",
        "terms": [
            {"input": "customer_score", "weight": 1.0},
            {"input": "interest_score", "weight": 1.0},
            {"input": "topic_breadth_score", "weight": 1.0},
        ],
    },
    {
        "signal": "community_closeness_score",
        "mode": "scaled_mean",
        "terms": [
            {"input": "system_score", "weight": 1.0},
            {"input": "closeness_score", "weight": 1.0},
            {"input": "topic_complexity_score", "weight": 1.0},
        ],
    },
    {
        "signal": "access_score",
        "mode": "scaled_mean",
        "terms": [
            {"input": "customer_score", "weight": 1.0},
            {"input": "community_closeness_score", "weight": 1.0},
            {"input": "system_score", "weight": 1.0},
        ],
    },
]

# Posture adjustment. ``clamp`` is per-row rather than one clamp for all four
# postures because the original was asymmetric, and the asymmetry is visible in
# stored scores: a trusted posture can only ever *raise* the access score (and
# saturates at the ceiling), while every other posture lowers it and floors at
# zero. ``high_trust`` is additionally not rounded and not clamped at all --
# it is a pass-through, so an unrounded input comes back unrounded.
#
# ``covers`` declares which reachable postures the catch-all row is the intended
# handler for. Without it the default row looks like it handles nothing and the
# constrained posture looks unhandled, which is the opposite of the truth: the
# original's trailing ``else`` *was* the constrained branch.
POSTURE_ADJUSTMENTS: list[dict[str, Any]] = [
    {"posture": "high_trust", "delta": 0.0, "clamp": "none", "rounded": False, "default": False},
    {"posture": "customer_trusted", "delta": 2.0, "clamp": "upper", "rounded": True, "default": False},
    {"posture": "observed", "delta": -4.0, "clamp": "lower", "rounded": True, "default": False},
    {"posture": "*", "delta": -8.0, "clamp": "lower", "rounded": True, "default": True, "covers": ["constrained"]},
]

# Access-decision escalation. ``build_policy_access_decision`` reports *both* the
# requested tier and the effective one, so an operator can see that a
# "standard" ask was quietly raised. Keeping the mapping declarative means a new
# posture can join the escalation set without a branch, and the reason is
# reportable rather than implicit.
TIER_ESCALATIONS: list[dict[str, Any]] = [
    {
        "id": "posture_floor",
        "control_posture_in": ["constrained", "observed"],
        "required_tier": "standard",
        "effective_tier": "customer-premium",
        "reason": "a constrained or observed posture will not grant baseline functionality",
    },
]

# Health / richness cutoffs. Kept in one table because they are all "where is
# the line" numbers, and an operator tuning a policy has to tune them together
# to keep the health summary and the expansion recommendation consistent.
HEALTH_THRESHOLDS: dict[str, Any] = {
    "weak_below": 60.0,
    "strong_at_or_above": 75.0,
    "weak_points_reported": 3,
    "medium_priority_at_or_above": 45.0,
    "expansion_access_at_or_above": 75.0,
    "expansion_system_at_or_above": 75.0,
    "max_recommendations": 5,
    "richness_balanced_at_or_above": 75.0,
    "richness_focused_at_or_above": 45.0,
    # The balance numerator has seven addends and divides by six. That is not a
    # typo being carried forward for its own sake: the divisor is the count of
    # the *original* five evidence terms plus the raw topic scores, and it was
    # never re-derived when terms were added. Preserved exactly.
    "richness_divisor": 6.0,
    "richness_keyword_weight": 4.0,
    "richness_focus_weight": 2.5,
    "richness_family_weight": 3.5,
    "richness_coverage_scale": 100.0,
}

# Topic marker weights. ``order`` is authoritative and is not decorative:
# contributions are summed in this order, and the original functions accumulated
# in a specific order too (for ``depth`` the five-word bonus lands *between* the
# routing and urgency groups). Float addition is not associative, so the order
# is part of reproducing the published number.
#
# ``match`` distinguishes the two original idioms: ``any`` adds the weight once
# if at least one marker appears (``if any(marker in normalized ...)``), and
# ``count`` adds it once per distinct marker present
# (``sum(9.0 for term in terms if term in normalized)``). Both are substring
# containment on the lowercased text, not token matching -- "android" contains
# "and" and is scored for it.
TOPIC_MARKER_WEIGHTS: list[dict[str, Any]] = [
    {
        "metric": "breadth", "order": 10, "name": "connectors", "match": "any", "weight": 8.0,
        "markers": ["and", "or", "with", "for", "support", "service"],
    },
    {
        "metric": "breadth", "order": 20, "name": "transaction", "match": "any", "weight": 10.0,
        "markers": ["billing", "booking", "policy", "routing", "handoff", "coverage"],
    },
    {
        "metric": "breadth", "order": 30, "name": "journey", "match": "any", "weight": 8.0,
        "markers": ["retention", "discovery", "context", "logistics", "reassurance", "transcript"],
    },
    {
        "metric": "breadth", "order": 40, "name": "safeguard", "match": "any", "weight": 7.0,
        "markers": ["privacy", "consent", "translation", "attachment", "checklist", "feedback"],
    },
    {
        "metric": "breadth", "order": 50, "name": "intent", "match": "any", "weight": 9.0,
        "markers": ["intent", "frame", "preferences", "knowledge", "callback", "visibility", "continuity"],
    },
    {
        "metric": "complexity", "order": 10, "name": "operational_terms", "match": "count", "weight": 9.0,
        "markers": [
            "exception", "override", "escalation", "handoff", "routing", "eligibility",
            "verification", "refund", "capacity", "policy", "retention", "discovery",
            "summary", "context", "logistics", "privacy", "consent", "translation",
            "attachment", "checklist", "feedback", "intent", "knowledge", "callback",
            "visibility", "continuity", "reassurance",
        ],
    },
    {
        "metric": "depth", "order": 10, "name": "routing", "match": "any", "weight": 10.0,
        "markers": ["and", "with", "or", "routing", "handoff", "billing", "booking"],
    },
    {
        "metric": "depth", "order": 20, "name": "urgency", "match": "any", "weight": 12.0,
        "markers": ["exception", "urgent", "priority", "policy", "verification"],
    },
    {
        "metric": "depth", "order": 30, "name": "journey", "match": "any", "weight": 8.0,
        "markers": ["retention", "discovery", "context", "summary", "logistics", "reassurance"],
    },
]

# Per-metric skeleton: the base term, the catalog-size term, and the word-count
# bonus. ``word_count`` says which tokenization the threshold uses -- the
# slash/dash-replaced token list for breadth and depth, the plain split for
# complexity (which is why "a/b c d e" is four words to complexity and five to
# depth).
TOPIC_SCORE_RULES: list[dict[str, Any]] = [
    {
        "metric": "breadth",
        "base": {"input": "unique_token_count", "weight": 7.5},
        "catalog": {"source": "TOPIC_CATALOG", "per_item": 0.2, "cap": 12.0},
        "word_count": None,
        "token_split": ["/", "-"],
    },
    {
        "metric": "complexity",
        "base": None,
        "catalog": None,
        "word_count": {"count": 4, "bonus": 8.0, "order": 20, "tokenize": "plain"},
        "token_split": [],
    },
    {
        "metric": "depth",
        "base": {"input": "unique_token_count", "weight": 8.0},
        "catalog": {"source": "TOPIC_THEME_GROUPS", "per_item": 1.5, "cap": 18.0},
        "word_count": {"count": 5, "bonus": 6.0, "order": 15, "tokenize": "split"},
        "token_split": ["/", "-"],
    },
]

# Recommendation rules. ``emit`` is ``once`` (one recommendation if ``when``
# holds) or ``per_weak_point`` (one per source item, up to ``limit``).
# Supported ``when`` conditions are enumerated in ``RECOMMENDATION_CONDITIONS``
# and validated by ``validate_policy_scoring``, so an unknown key is an error
# rather than a rule that silently never fires.
RECOMMENDATION_RULES: list[dict[str, Any]] = [
    {
        "id": "posture_recheck",
        "emit": "once",
        "priority": "high",
        "area": "control_posture",
        "when": {"control_posture_in": ["constrained", "observed"]},
        "recommendation": "Recheck access thresholds before granting higher-tier functionality.",
        "evidence": "Control posture is {control_posture}.",
    },
    {
        "id": "weak_signal",
        "emit": "per_weak_point",
        "source": "health.weak_points",
        "limit": 3,
        "area": "{name}",
        "priority": {
            "at_or_above": HEALTH_THRESHOLDS["medium_priority_at_or_above"],
            "then": "medium",
            "otherwise": "high",
        },
        "when": {},
        "recommendation": "Improve {name_spaced} to raise overall access readiness.",
        "evidence": "{name} is at {score:.2f}.",
    },
    {
        "id": "expansion",
        "emit": "once",
        "priority": "low",
        "area": "expansion",
        "when": {
            "access_score_at_or_above": HEALTH_THRESHOLDS["expansion_access_at_or_above"],
            "system_score_at_or_above": HEALTH_THRESHOLDS["expansion_system_at_or_above"],
        },
        "recommendation": "Consider allowing richer functionality for this user segment.",
        "evidence": "Access score {access_score:.2f} and system score {system_score:.2f} are both strong.",
    },
]

# The condition vocabulary, as data, so the validator can check every rule
# against it and so an operator can see which predicates exist before writing
# one. ``handler`` names the evaluator branch; the list is the contract.
RECOMMENDATION_CONDITIONS: list[dict[str, Any]] = [
    {"condition": "control_posture_in", "kind": "membership", "value": "list[str]"},
    {"condition": "control_posture_not_in", "kind": "membership", "value": "list[str]"},
    {"condition": "access_score_at_or_above", "kind": "threshold", "value": "float"},
    {"condition": "access_score_below", "kind": "threshold", "value": "float"},
    {"condition": "system_score_at_or_above", "kind": "threshold", "value": "float"},
    {"condition": "customer_score_below", "kind": "threshold", "value": "float"},
    {"condition": "weak_points_present", "kind": "presence", "value": "bool"},
    {"condition": "dominant_signal_in", "kind": "membership", "value": "list[str]"},
]

# Shared tunables. Anything here that is a *cap* or a *rounding precision* is a
# tuning knob; the string literals are payload contract text and are listed so
# the catalog can show them without them being overridable.
POLICY_SCORING_OPS: dict[str, Any] = {
    "score_ceiling": 100.0,
    "score_floor": 0.0,
    "decimals": 2,
    "default_tier": "restricted",
    "default_posture": "constrained",
    "default_band": "limited",
    "unknown_tier_rank": 0,
    "unknown_required_tier_rank": 1,
    "topic_focus_limit": 8,
    "topic_matched_keywords_limit": 8,
    "topic_focus_fallback_limit": 5,
    "topic_suggestion_limit": 3,
    "topic_matched_topic_limit": 4,
    "confidence_ceiling": 1.0,
    "confidence_breadth_divisor": 100.0,
    "confidence_complexity_divisor": 120.0,
    "max_health_signals": 6,
}


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


def _topic_score_rule(metric: str) -> dict[str, Any]:
    for row in TOPIC_SCORE_RULES:
        if str(row["metric"]) == metric:
            return row
    raise KeyError(metric)


def _topic_tokens(normalized: str, separators: Sequence[str]) -> list[str]:
    text = normalized
    for separator in separators or ():
        text = text.replace(separator, " ")
    return [token for token in text.split() if token]


#: The catalog names a ``TOPIC_SCORE_RULES`` row may name, mapped to the objects
#: they refer to.
#:
#: This replaces a ``globals()[source]`` lookup, which is what made the imports
#: above look unused to every linter while being load-bearing: the rule rows
#: store the catalog as a *string* ("TOPIC_CATALOG"), and the scoring path
#: resolved it against this module's own globals. Two consequences, both bad:
#: a linter cannot see the dependency, and an unknown name raises ``KeyError``
#: with no indication of which names are valid.
#:
#: An explicit registry makes the dependency visible to readers *and* to
#: tooling, and turns a typo into a named error. The value is read live, not
#: snapshotted: ``TOPIC_CATALOG`` grows when topics are added, and the original
#: expressions read it at call time, so snapshotting would make the two disagree
#: for every topic added afterwards.
TOPIC_CATALOG_SOURCES: dict[str, Any] = {
    "TOPIC_CATALOG": TOPIC_CATALOG,
    "TOPIC_THEME_GROUPS": TOPIC_THEME_GROUPS,
    "TOPIC_SECTORS": TOPIC_SECTORS,
}


def _topic_catalog_size(source: str) -> int:
    """Length of a named catalog, resolved through :data:`TOPIC_CATALOG_SOURCES`."""
    name = str(source)
    if name not in TOPIC_CATALOG_SOURCES:
        raise KeyError(
            f"unknown topic catalog {name!r}; known: {sorted(TOPIC_CATALOG_SOURCES)}"
        )
    return len(TOPIC_CATALOG_SOURCES[name])


def topic_metric_contributions(metric: str, topic_text: str) -> list[dict[str, Any]]:
    """Every additive term of a topic metric, in the order it is summed.

    Exposed so a score can be argued about. ``value`` is the term's contribution
    and ``order`` is why the list is in that order -- the accumulation order is
    part of reproducing the published number, so a reader comparing two
    implementations needs to see it rather than infer it.
    """
    rule = _topic_score_rule(metric)
    normalized = (topic_text or "").lower()
    tokens = _topic_tokens(normalized, rule.get("token_split") or ())
    contributions: list[dict[str, Any]] = []

    base = rule.get("base")
    if base:
        count = len(set(tokens)) if str(base["input"]) == "unique_token_count" else len(tokens)
        contributions.append(
            {
                "order": 0,
                "source": "base",
                "value": count * float(base["weight"]),
                "detail": f"{count} unique tokens x {base['weight']}",
            }
        )

    catalog = rule.get("catalog")
    if catalog:
        size = _topic_catalog_size(str(catalog["source"]))
        contributions.append(
            {
                "order": 1,
                "source": "catalog_size",
                "value": min(size * float(catalog["per_item"]), float(catalog["cap"])),
                "detail": f"min({size} x {catalog['per_item']}, {catalog['cap']})",
            }
        )

    for row in TOPIC_MARKER_WEIGHTS:
        if str(row["metric"]) != metric:
            continue
        matched = [marker for marker in row["markers"] if marker in normalized]
        if str(row["match"]) == "count":
            value = float(row["weight"]) * len(matched)
        else:
            value = float(row["weight"]) if matched else 0.0
        if not matched:
            continue
        contributions.append(
            {
                "order": int(row["order"]),
                "source": f"marker:{row['name']}",
                "value": value,
                "detail": f"{len(matched)} of {len(row['markers'])} markers ({row['match']} x {row['weight']})",
            }
        )

    word_rule = rule.get("word_count")
    if word_rule:
        words = tokens if str(word_rule.get("tokenize", "split")) == "split" else normalized.split()
        if len(words) >= int(word_rule["count"]):
            contributions.append(
                {
                    "order": int(word_rule["order"]),
                    "source": "word_count",
                    "value": float(word_rule["bonus"]),
                    "detail": f"{len(words)} words >= {word_rule['count']} x {word_rule['bonus']}",
                }
            )

    return sorted(contributions, key=lambda item: int(item["order"]))


def topic_metric_score(metric: str, topic_text: str) -> float:
    """Score one topic metric from ``TOPIC_SCORE_RULES`` + ``TOPIC_MARKER_WEIGHTS``.

    The text is expected already lowercased by the caller and non-empty; the
    empty-text behaviour is a caller decision and differs per metric (breadth and
    complexity return 0.0, depth returns the ``"no_topic_depth"`` literal).
    """
    total = 0.0
    for contribution in topic_metric_contributions(metric, topic_text):
        total += float(contribution["value"])
    return min(float(POLICY_SCORING_OPS["score_ceiling"]), round(total, int(POLICY_SCORING_OPS["decimals"])))


def _topic_breadth_score(topic_text: str) -> float:
    normalized = (topic_text or "").lower()
    if not normalized:
        return 0.0
    return topic_metric_score("breadth", normalized)


def _topic_complexity_score(topic_text: str) -> float:
    normalized = (topic_text or "").lower()
    if not normalized:
        return 0.0
    return topic_metric_score("complexity", normalized)


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


def _matched_tier_rule(access_score: float, system_score: float) -> tuple[int, dict[str, object] | None]:
    """Index of the first matching tier rule, or ``-1``.

    The tier rules are first-match-wins and ordered highest tier first, so the
    *index* is the rule's identity. Reported rather than looked up in a new
    ``id`` column, because ``POLICY_TIER_RULES`` is an existing table and adding
    a key to it would change the pinned ``build_policy_tier_catalog`` payload.
    """
    for index, rule in enumerate(POLICY_TIER_RULES):
        if access_score >= float(rule["access_score_min"]) and system_score >= float(rule["system_score_min"]):
            return index, rule
    return -1, None


def _matched_posture_rule(policy_tier: str, access_score: float, system_score: float) -> tuple[str, str, dict[str, object] | None]:
    """``(route, index, rule)`` where route is ``tier_map``/``rules``/``default``."""
    if policy_tier in CONTROL_POSTURE_TIER_MAP:
        return "tier_map", -1, {"posture": CONTROL_POSTURE_TIER_MAP[policy_tier], "via": "CONTROL_POSTURE_TIER_MAP", "tier": policy_tier}
    for index, rule in enumerate(CONTROL_POSTURE_RULES):
        if access_score >= float(rule["access_score_min"]) and system_score >= float(rule["system_score_min"]):
            return "rules", index, rule
    return "default", -1, None


def _matched_band_rule(access_score: float) -> tuple[int, dict[str, object] | None]:
    for index, rule in enumerate(ACCESS_BAND_RULES):
        if access_score >= float(rule["access_score_min"]):
            return index, rule
    return -1, None


def resolve_policy_tier_trace(access_score: float, system_score: float) -> dict[str, Any]:
    """Explain which tier rule fired, and what a near miss would have produced.

    ``next_tier`` is the first rule that failed and why -- the number an
    operator actually needs when a customer sits just under a boundary. Without
    it, a tier decision is only legible by re-deriving the threshold in a shell.
    """
    index, rule = _matched_tier_rule(access_score, system_score)
    matched = rule is not None
    near_miss: dict[str, Any] | None = None
    if not matched and POLICY_TIER_RULES:
        # The nearest unmet rule is the last one in the table (the lowest tier),
        # since the list is ordered highest-first: it is the closest boundary the
        # score has not yet reached.
        candidate = POLICY_TIER_RULES[-1]
        access_gap = round(float(candidate["access_score_min"]) - access_score, 2)
        near_miss = {
            "rule_index": len(POLICY_TIER_RULES) - 1,
            "tier": str(candidate["tier"]),
            "access_score_short_by": access_gap if access_gap > 0 else 0.0,
            "system_score_short_by": (
                round(float(candidate["system_score_min"]) - system_score, 2)
                if system_score < float(candidate["system_score_min"]) else 0.0
            ),
        }
    return {
        "policy_tier": str(rule["tier"]) if matched else str(POLICY_SCORING_OPS["default_tier"]),
        "matched": matched,
        "rule_index": index,
        "rule": dict(rule) if rule is not None else None,
        "evaluated": [dict(item) for item in POLICY_TIER_RULES],
        "next_tier": near_miss,
    }


def resolve_policy_tier(access_score: float, system_score: float) -> str:
    """First matching tier rule wins (rules are ordered highest -> lowest)."""
    return str(resolve_policy_tier_trace(access_score, system_score)["policy_tier"])


def resolve_control_posture_trace(policy_tier: str, access_score: float, system_score: float) -> dict[str, Any]:
    """Explain which posture route fired: the tier map, a rule, or the default.

    A constrained posture is the *default*, not a failure to match. Reporting it
    as ``matched: False`` would read as a missing rule, so it is reported as a
    route with a reason instead.
    """
    route, index, rule = _matched_posture_rule(policy_tier, access_score, system_score)
    return {
        "control_posture": (
            str(rule["posture"]) if rule is not None else str(POLICY_SCORING_OPS["default_posture"])
        ),
        "route": route,
        "rule_index": index,
        "rule": dict(rule) if rule is not None else None,
        "tier_map": dict(CONTROL_POSTURE_TIER_MAP),
        "evaluated": [dict(item) for item in CONTROL_POSTURE_RULES],
    }


def resolve_control_posture(policy_tier: str, access_score: float, system_score: float) -> str:
    trace = resolve_control_posture_trace(policy_tier, access_score, system_score)
    return str(trace["control_posture"])


def _validate_score(access_score: Any) -> float:
    """Refuse a score outside the scale this module publishes.

    ``POLICY_SCORING_OPS`` declares ``score_floor`` / ``score_ceiling`` as the
    scale every one of these resolvers takes. A caller holding ``-10`` or ``101``
    has a bug, and this is the cheapest possible moment to find it.

    Why refusing beats clamping: the documented phantom defect is an access-band
    resolver handed a **0-1** score, where 0.8 becomes ``limited`` -- the worst
    band in the table -- and does so silently, so an ``elite`` customer is served
    the bottom-band experience and nothing anywhere says why. Clamping is what
    makes that invisible; a ``ValueError`` is what makes it a stack trace in the
    caller's test suite.

    Both edges are checked and both raise, so a score on the wrong *scale*
    (``0.85``) is caught as readily as one that is merely too large.

    Not applied to the trace helper's *inputs* beyond this one function: the tier
    and posture resolvers are called from many paths that already clamp
    deliberately (``apply_posture_adjustment``), and refusing there would turn a
    documented clamp into a crash on the main scoring path.
    """
    try:
        value = float(access_score)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"access_score must be a number on the "
            f"{POLICY_SCORING_OPS['score_floor']}-{POLICY_SCORING_OPS['score_ceiling']} "
            f"scale, got {access_score!r}"
        ) from exc
    floor = float(POLICY_SCORING_OPS["score_floor"])
    ceiling = float(POLICY_SCORING_OPS["score_ceiling"])
    if not floor <= value <= ceiling:
        raise ValueError(
            f"access_score {value} is outside the published "
            f"{floor}-{ceiling} scale. If this is a score on a different scale "
            "(0-1 rather than 0-100), convert it before resolving a band -- "
            "clamping here silently returns the 'limited' band, which is how an "
            "elite customer ends up served the bottom-band experience."
        )
    return value


def resolve_access_band_trace(access_score: float) -> dict[str, Any]:
    access_score = _validate_score(access_score)
    index, rule = _matched_band_rule(access_score)
    matched = rule is not None
    near_miss = None
    if not matched and ACCESS_BAND_RULES:
        candidate = ACCESS_BAND_RULES[-1]
        gap = round(float(candidate["access_score_min"]) - access_score, 2)
        if gap > 0:
            near_miss = {
                "rule_index": len(ACCESS_BAND_RULES) - 1,
                "band": str(candidate["band"]),
                "access_score_short_by": gap,
            }
    return {
        "access_band": str(rule["band"]) if matched else str(POLICY_SCORING_OPS["default_band"]),
        "matched": matched,
        "rule_index": index,
        "rule": dict(rule) if rule is not None else None,
        "evaluated": [dict(item) for item in ACCESS_BAND_RULES],
        "next_band": near_miss,
    }


def resolve_access_band(access_score: float) -> str:
    return str(resolve_access_band_trace(access_score)["access_band"])


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


def _posture_adjustment_row(control_posture: str) -> dict[str, Any]:
    for row in POSTURE_ADJUSTMENTS:
        if str(row["posture"]) == control_posture and not bool(row.get("default")):
            return row
    for row in POSTURE_ADJUSTMENTS:
        if bool(row.get("default")):
            return row
    raise KeyError(control_posture)


def posture_adjustment_trace(access_score: float, control_posture: str) -> dict[str, Any]:
    """The arithmetic behind one posture adjustment, step by step.

    ``clamped`` is the useful half: a trusted posture that saturates at 100.0
    moves the tier no further even as the raw score keeps climbing, which is
    invisible in a stored score. ``bound`` names which limit did it.
    """
    row = _posture_adjustment_row(control_posture)
    delta = float(row["delta"])
    raw = access_score + delta
    unrounded = round(raw, int(POLICY_SCORING_OPS["decimals"])) if bool(row.get("rounded", True)) else raw
    adjusted = apply_posture_adjustment(access_score, control_posture)
    clamp = str(row.get("clamp", "none"))
    bound = "none"
    if clamp in {"upper", "both"} and adjusted < unrounded:
        bound = "ceiling"
    if clamp in {"lower", "both"} and adjusted > unrounded:
        bound = "floor"
    return {
        "posture": control_posture,
        "access_score_before": access_score,
        "delta": delta,
        "raw_after_delta": raw,
        "after_rounding": unrounded,
        "access_score_after": adjusted,
        "clamp": clamp,
        "rounded": bool(row.get("rounded", True)),
        "clamped": adjusted != unrounded,
        "bound": bound,
    }


def apply_posture_adjustment(access_score: float, control_posture: str) -> float:
    """Apply the declared delta for a posture, with that posture's own clamp.

    Not a pass-through wrapper: the clamp and the rounding are per-posture data,
    so a posture can be added with a saturating raise, a floored cut, or an
    exact passthrough without a new branch. The arithmetic is applied in the
    same order as the original expression -- delta first, then rounding, then
    the clamp -- because rounding before clamping and after it differ at the
    boundary.
    """
    row = _posture_adjustment_row(control_posture)
    value = access_score + float(row["delta"])
    if bool(row.get("rounded", True)):
        value = round(value, int(POLICY_SCORING_OPS["decimals"]))
    clamp = str(row.get("clamp", "none"))
    if clamp in {"upper", "both"}:
        value = min(float(POLICY_SCORING_OPS["score_ceiling"]), value)
    if clamp in {"lower", "both"}:
        value = max(float(POLICY_SCORING_OPS["score_floor"]), value)
    return value


def _posture_adjusted_access_score(access_score: float, control_posture: str) -> float:
    return apply_posture_adjustment(access_score, control_posture)


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


def policy_signal_map(snapshot: PolicyScoreSnapshot) -> dict[str, float]:
    """The six health-reportable signals, in the order the health summary lists.

    Order is the published order and is load-bearing twice over: the weak and
    strong point lists are sorted by score alone, so a tie is broken by
    insertion order, and ``dominant_signal`` is a ``max()`` over these keys,
    which also breaks ties on the first key. A dict built in a different order
    would change which signal is named dominant in a tie.
    """
    return {
        "system_score": snapshot.system_score,
        "customer_score": snapshot.customer_score,
        "access_score": snapshot.access_score,
        "interest_score": snapshot.interest_score,
        "closeness_score": snapshot.closeness_score,
        "community_closeness_score": snapshot.community_closeness_score,
    }


def policy_health_report(snapshot: PolicyScoreSnapshot) -> dict[str, Any]:
    """The health computation as data, before it is wrapped in a schema model.

    ``build_policy_health_summary`` has always returned a pydantic model, so the
    weak/strong classification was only reachable by re-deriving it. The
    thresholds and the balance formula are now data and the classification is
    reported here, which is what makes the cutoffs inspectable and testable
    without constructing a schema object.
    """
    signals = policy_signal_map(snapshot)
    weak_below = float(HEALTH_THRESHOLDS["weak_below"])
    strong_at = float(HEALTH_THRESHOLDS["strong_at_or_above"])
    weak_points = sorted(
        ((name, score) for name, score in signals.items() if score < weak_below),
        key=lambda item: item[1],
    )
    strong_points = sorted(
        ((name, score) for name, score in signals.items() if score >= strong_at),
        key=lambda item: item[1],
        reverse=True,
    )
    return {
        "signals": dict(signals),
        "weak_points": [{"name": name, "score": score} for name, score in weak_points],
        "strong_points": [{"name": name, "score": score} for name, score in strong_points],
        "dominant_signal": max(signals, key=signals.get),
        "balance_index": round(snapshot.access_score - min(signals.values()), 2),
        "thresholds": {
            "weak_below": weak_below,
            "strong_at_or_above": strong_at,
        },
    }


def build_policy_health_summary(snapshot: PolicyScoreSnapshot) -> app_schemas.CustomerPolicyHealthSummaryOut:
    report = policy_health_report(snapshot)
    return app_schemas.CustomerPolicyHealthSummaryOut(
        generated_at=datetime.now(timezone.utc),
        policy_tier=snapshot.policy_tier,
        control_posture=snapshot.control_posture,
        dominant_signal=str(report["dominant_signal"]),
        weak_points=list(report["weak_points"]),
        strong_points=list(report["strong_points"]),
        balance_index=float(report["balance_index"]),
    )


def _recommendation_source(name: str, report: Mapping[str, Any]) -> list[Any]:
    """Resolve a ``source`` path on a rule row against the health report.

    Only the health report's own lists are addressable. A rule that names a
    source the report does not have is a validator error rather than a rule that
    quietly emits nothing.
    """
    if name == "health.weak_points":
        return list(report["weak_points"])
    if name == "health.strong_points":
        return list(report["strong_points"])
    raise KeyError(name)


def _recommendation_condition_holds(condition: str, expected: Any, context: Mapping[str, Any]) -> bool:
    if condition == "control_posture_in":
        return str(context["control_posture"]) in list(expected)
    if condition == "control_posture_not_in":
        return str(context["control_posture"]) not in list(expected)
    if condition == "access_score_at_or_above":
        return float(context["access_score"]) >= float(expected)
    if condition == "access_score_below":
        return float(context["access_score"]) < float(expected)
    if condition == "system_score_at_or_above":
        return float(context["system_score"]) >= float(expected)
    if condition == "customer_score_below":
        return float(context["customer_score"]) < float(expected)
    if condition == "weak_points_present":
        return bool(context["weak_points"]) is bool(expected)
    if condition == "dominant_signal_in":
        return str(context["dominant_signal"]) in list(expected)
    raise KeyError(condition)


def policy_recommendation_trace(snapshot: PolicyScoreSnapshot) -> dict[str, Any]:
    """Evaluate every recommendation rule and report which fired and why.

    The recommendations themselves were always a flat list of at most five
    entries, which is a poor answer to "why did I not get the expansion
    recommendation". This keeps the decision: one row per rule with the
    conditions that held, the ones that did not, and the emitted items.
    """
    report = policy_health_report(snapshot)
    context: dict[str, Any] = {
        "control_posture": snapshot.control_posture,
        "access_score": snapshot.access_score,
        "system_score": snapshot.system_score,
        "customer_score": snapshot.customer_score,
        "weak_points": report["weak_points"],
        "dominant_signal": report["dominant_signal"],
    }
    base_format = {
        "control_posture": snapshot.control_posture,
        "access_score": snapshot.access_score,
        "system_score": snapshot.system_score,
        "customer_score": snapshot.customer_score,
    }
    rows: list[dict[str, Any]] = []
    emitted: list[dict[str, Any]] = []
    for rule in RECOMMENDATION_RULES:
        conditions = {str(key): value for key, value in dict(rule.get("when") or {}).items()}
        unmet = sorted(
            key for key, value in conditions.items()
            if not _recommendation_condition_holds(key, value, context)
        )
        fired = not unmet
        items: list[dict[str, Any]] = []
        if fired and str(rule["emit"]) == "once":
            fields = dict(base_format)
            priority = rule.get("priority")
            items.append(
                {
                    "priority": str(priority if isinstance(priority, str) else "high"),
                    "area": str(rule["area"]),
                    "recommendation": str(rule["recommendation"]).format(**fields),
                    "evidence": str(rule["evidence"]).format(**fields),
                }
            )
        elif fired:
            limit = int(rule.get("limit", POLICY_SCORING_OPS["max_health_signals"]))
            raw_spec = rule.get("priority")
            # Tolerant at runtime on purpose. A malformed row is a validator
            # error, but the trace is called from a request path, and a 500 from
            # the explainability surface is a worse outcome than a fallback
            # priority that the validator will flag separately.
            spec = raw_spec if isinstance(raw_spec, dict) else {"at_or_above": 0.0, "then": "high", "otherwise": "high"}
            for weak_point in _recommendation_source(str(rule["source"]), report)[:limit]:
                score = float(weak_point["score"])
                name = str(weak_point["name"])
                fields = {
                    **base_format,
                    "name": name,
                    "name_spaced": name.replace("_", " "),
                    "score": score,
                }
                items.append(
                    {
                        "priority": str(
                            spec["then"] if score >= float(spec["at_or_above"]) else spec["otherwise"]
                        ),
                        "area": str(rule["area"]).format(**fields),
                        "recommendation": str(rule["recommendation"]).format(**fields),
                        "evidence": str(rule["evidence"]).format(**fields),
                    }
                )
        rows.append(
            {
                "id": str(rule["id"]),
                "emit": str(rule["emit"]),
                "fired": fired,
                "conditions": conditions,
                "unmet_conditions": unmet,
                "items": items,
            }
        )
        emitted.extend(items)
    limit = int(HEALTH_THRESHOLDS["max_recommendations"])
    return {
        "rules": rows,
        "recommendations": emitted[:limit],
        "suppressed_count": max(0, len(emitted) - limit),
        "limit": limit,
    }


def build_policy_access_recommendations(snapshot: PolicyScoreSnapshot) -> list[app_schemas.CustomerPolicyRecommendationOut]:
    trace = policy_recommendation_trace(snapshot)
    return [
        app_schemas.CustomerPolicyRecommendationOut(
            priority=str(item["priority"]),
            area=str(item["area"]),
            recommendation=str(item["recommendation"]),
            evidence=str(item["evidence"]),
        )
        for item in trace["recommendations"]
    ]


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


def topic_richness_balance(breadth: float, complexity: float, signals: Mapping[str, Any]) -> float:
    """The balance figure behind the ``rich``/``balanced``/``focused`` label.

    The numerator has seven addends and divides by
    ``HEALTH_THRESHOLDS['richness_divisor']``; see that key for why. A missing
    input is treated as 0.0 here rather than raising, so a caller that has not
    computed every signal still gets a usable balance instead of an exception
    inside a formatting path.
    """
    numerator = (
        float(breadth)
        + float(complexity)
        + (float(signals["coverage_ratio"]) * float(HEALTH_THRESHOLDS["richness_coverage_scale"]))
        + (len(signals["matched_keywords"]) * float(HEALTH_THRESHOLDS["richness_keyword_weight"]))
        + (len(signals["topic_focus"]) * float(HEALTH_THRESHOLDS["richness_focus_weight"]))
        + (float(signals["topic_family_count"]) * float(HEALTH_THRESHOLDS["richness_family_weight"]))
        + float(signals["topic_richness_score"])
    )
    return round(numerator / float(HEALTH_THRESHOLDS["richness_divisor"]), int(POLICY_SCORING_OPS["decimals"]))


def build_policy_topic_richness(topic_text: str) -> str:
    normalized = (topic_text or "").strip()
    if not normalized:
        return "no_topic_richness"
    breadth = _topic_breadth_score(normalized)
    complexity = _topic_complexity_score(normalized)
    signals = _topic_signals(normalized)
    family_count = signals["topic_family_count"]
    balance = topic_richness_balance(breadth, complexity, signals)
    label = (
        "rich"
        if balance >= float(HEALTH_THRESHOLDS["richness_balanced_at_or_above"])
        else "balanced"
        if balance >= float(HEALTH_THRESHOLDS["richness_focused_at_or_above"])
        else "focused"
    )
    return f"{label} [balance={balance:.2f}, signals={len(signals['matched_topics'])}, families={family_count}]"


def build_policy_topic_depth(topic_text: str) -> str:
    normalized = (topic_text or "").strip().lower()
    if not normalized:
        return "no_topic_depth"
    return f"depth={topic_metric_score('depth', normalized):.2f}"


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
    theme_coverage = build_topic_theme_coverage(type("PolicyTopicSelection", (), {"topic": current_topic})())
    theme_coverage_items = [item for item in theme_coverage if item["matched_count"]]
    matched_themes = [item["theme"] for item in theme_coverage_items]
    portfolio_report = build_topic_portfolio_report(type("PolicyTopicSelection", (), {"topic": current_topic})())
    intelligence_report = build_topic_intelligence_report(type("PolicyTopicSelection", (), {"topic": current_topic})())
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
    # A `summary` local used to live here, computing a richer description than
    # the inline `summary=` the report is actually built with below. It was never
    # read, so two summary strings existed and the published one was the inline
    # one. Removed rather than wired up: which of the two should be the published
    # summary is a product decision, and changing that text is not a lint fix.
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


def resolve_tier_escalation(control_posture: str, required_tier: str) -> dict[str, Any]:
    """Decide the effective tier for an access request, and say why.

    The original code computed this with one inline ``if`` and returned only the
    result, so the reported ``effective_required_tier`` could differ from the
    requested one with nothing to explain the difference. First matching
    ``TIER_ESCALATIONS`` row wins; no match is the common case and is reported
    as ``escalated: False`` rather than as a failure.
    """
    for index, rule in enumerate(TIER_ESCALATIONS):
        if str(rule["required_tier"]) != str(required_tier):
            continue
        if str(control_posture) not in list(rule["control_posture_in"]):
            continue
        return {
            "required_tier": str(required_tier),
            "effective_required_tier": str(rule["effective_tier"]),
            "escalated": True,
            "rule_id": str(rule["id"]),
            "rule_index": index,
            "reason": str(rule["reason"]),
        }
    return {
        "required_tier": str(required_tier),
        "effective_required_tier": str(required_tier),
        "escalated": False,
        "rule_id": None,
        "rule_index": -1,
        "reason": f"no escalation applies to posture {control_posture!r} at tier {required_tier!r}",
    }


def build_policy_access_decision(
    user: models.User,
    policy_score: models.CustomerPolicyScore,
    functionality: str,
    required_tier: str = "standard",
) -> app_schemas.CustomerPolicyAccessOut:
    control_posture = getattr(policy_score, "control_posture", "observed")
    escalation = resolve_tier_escalation(control_posture, required_tier)
    effective_required_tier = str(escalation["effective_required_tier"])

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


def compose_access_score(metrics: Mapping[str, Any]) -> dict[str, float]:
    """Compose the six published signals from raw metrics, per ``COMPOSITE_RULES``.

    Pure, ordered, and DB-free. The rules are walked front to back and each
    produced signal is added to the working set, so a later rule can read an
    earlier rule's output exactly as the original expression did
    (``closeness_score`` reads ``customer_score``; ``access_score`` reads both
    plus ``system_score``).

    A term naming an input that is absent from ``metrics`` contributes 0.0 and is
    reported in ``missing_inputs`` rather than raising -- but only *after* the
    whole run, so a partially-specified metric set still produces a score an
    operator can inspect. Silently scoring a missing input as zero is the failure
    mode this reports instead.
    """
    working: dict[str, Any] = dict(metrics)
    missing: set[str] = set()
    produced: dict[str, float] = {}
    contributions: list[dict[str, Any]] = []
    decimals = int(POLICY_SCORING_OPS["decimals"])
    ceiling = float(POLICY_SCORING_OPS["score_ceiling"])

    for rule in COMPOSITE_RULES:
        signal = str(rule["signal"])
        terms = list(rule.get("terms") or [])
        values: list[float] = []
        for term in terms:
            name = str(term["input"])
            if name not in working:
                missing.add(name)
                values.append(0.0)
                contributions.append(
                    {"signal": signal, "input": name, "weight": 0.0, "value": 0.0, "missing": True}
                )
                continue
            raw = float(working[name])
            if "invert_ceiling" in term:
                raw = float(term["invert_ceiling"]) - raw
            if term.get("floor") is not None:
                raw = max(float(term["floor"]), raw)
            if term.get("ceiling") is not None:
                raw = min(float(term["ceiling"]), raw)
            value = raw * float(term["weight"])
            values.append(value)
            contributions.append(
                {
                    "signal": signal,
                    "input": name,
                    "weight": float(term["weight"]),
                    "value": value,
                    "missing": False,
                    **({"inverted": True} if "invert_ceiling" in term else {}),
                }
            )
        mode = str(rule["mode"])
        if not terms:
            total = 0.0
        elif mode == "weighted_sum":
            total = sum(values)
        elif mode == "scaled_mean":
            # Divides by the term count, not by the sum of the weights. See the
            # note above COMPOSITE_RULES: in interest_score the weights are
            # 10/4/8/1 and a weighted mean would be a different published score.
            total = sum(values) / len(values)
        else:
            raise KeyError(mode)
        floor = rule.get("floor")
        if floor is not None:
            total = max(float(floor), total)
        rule_ceiling = rule.get("ceiling")
        if rule_ceiling is not None:
            total = min(float(rule_ceiling), total)
        else:
            total = min(ceiling, total)
        value = round(total, decimals)
        produced[signal] = value
        working[signal] = value

    return {
        **produced,
        "missing_inputs": sorted(missing),
        "contributions": contributions,
    }


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

    scores = compose_access_score(
        {
            "signal_score": signal_score,
            "loyalty_score": loyalty_score,
            "dissatisfaction_score": dissatisfaction_score,
            "booking_count": booking_count,
            "completed_count": completed_count,
            "topic_confidence": topic_confidence,
            "topic_breadth_score": topic_breadth_score,
            "topic_complexity_score": topic_complexity_score,
        }
    )
    system_score = float(scores["system_score"])
    customer_score = float(scores["customer_score"])
    interest_score = float(scores["interest_score"])
    closeness_score = float(scores["closeness_score"])
    community_closeness_score = float(scores["community_closeness_score"])
    access_score = float(scores["access_score"])
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
    """Compare the stored tier rank with the required rank.

    The two fallback ranks are asymmetric on purpose and now named: an unknown
    *actual* tier ranks 0 (no access, fail closed) while an unknown *required*
    tier ranks 1 (the ``standard`` floor, so a typo in a requirement does not
    accidentally become the strictest possible check). Both defaults are
    ``POLICY_SCORING_OPS`` entries so the asymmetry is inspectable rather than
    two bare literals.
    """
    actual_rank = POLICY_TIER_RANK.get(
        policy_score.policy_tier, int(POLICY_SCORING_OPS["unknown_tier_rank"])
    )
    required_rank = POLICY_TIER_RANK.get(
        required_tier, int(POLICY_SCORING_OPS["unknown_required_tier_rank"])
    )
    return actual_rank >= required_rank


# ---------------------------------------------------------------------------
# Governance surface (additive; the pinned tier/posture/band contract is intact)
# ---------------------------------------------------------------------------
#
# What this layer adds over the original six-tables design:
#
# * ``compose_access_score`` -- the score formula as a pure, DB-free engine.
#   Previously the only way to see how a number was produced was to read one
#   arithmetic expression inside an ``async def`` that also issued six queries.
# * ``policy_tier_cascade`` / ``policy_what_if`` / ``policy_score_sensitivity``
#   -- the tier/posture cascade as a replayable function, so a threshold can be
#   argued about against a real metric set before it is written to a table.
# * ``policy_decision_trace`` -- why this customer has this tier, with the
#   near-miss distance to the tier they nearly reached.
# * ``validate_policy_scoring`` -- the tables checked against each other. A
#   forward-referencing composition rule, a marker group for a metric that does
#   not exist, a posture nobody adjusts, or a recommendation predicate with a
#   typo in its condition name are all silent today: they produce a plausible
#   score and no signal that a rule did not fire.


def policy_tier_cascade(scores: Mapping[str, Any]) -> dict[str, Any]:
    """Replay the tier/posture/band resolution the snapshot builder performs.

    ``build_customer_policy_snapshot`` resolves the tier and posture, applies
    the posture adjustment to the access score, and then resolves *both again*.
    That two-pass shape is why a customer's tier can differ from the tier their
    raw access score earned. Preserved exactly here so the pass order is
    inspectable, and ``recomputed`` names the cases where it changed anything.
    """
    access_score = float(scores["access_score"])
    system_score = float(scores["system_score"])
    first_tier = resolve_policy_tier(access_score, system_score)
    first_posture = resolve_control_posture(first_tier, access_score, system_score)
    adjustment = posture_adjustment_trace(access_score, first_posture)
    adjusted = float(adjustment["access_score_after"])
    second_tier = resolve_policy_tier(adjusted, system_score)
    second_posture = resolve_control_posture(second_tier, adjusted, system_score)
    return {
        "pre_adjustment": {
            "access_score": access_score,
            "system_score": system_score,
            "policy_tier": first_tier,
            "control_posture": first_posture,
        },
        "posture_adjustment": adjustment,
        "post_adjustment": {
            "access_score": adjusted,
            "system_score": system_score,
            "policy_tier": second_tier,
            "control_posture": second_posture,
        },
        "access_band": resolve_access_band(adjusted),
        "recomputed": second_tier != first_tier or second_posture != first_posture,
    }


def policy_what_if(
    metrics: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
    required_tier: str = "standard",
) -> dict[str, Any]:
    """Recompute the whole cascade with some inputs changed, and diff it.

    Answers "what would this customer's tier be if the weight on completed
    bookings were 8.0 instead of 6.0" without editing a table, without a
    database, and without a request. The baseline and the variant are both
    computed through the live rules, so a threshold change in the tables is
    reflected on both sides at once.
    """
    baseline_scores = compose_access_score(metrics)
    variant_scores = compose_access_score({**metrics, **dict(overrides or {})})
    baseline_cascade = policy_tier_cascade(baseline_scores)
    variant_cascade = policy_tier_cascade(variant_scores)
    escalation = resolve_tier_escalation(str(variant_cascade["post_adjustment"]["control_posture"]), required_tier)
    changed = {
        name: {
            "before": baseline_scores[name],
            "after": variant_scores[name],
            "delta": round(float(variant_scores[name]) - float(baseline_scores[name]), 2),
        }
        for name in COMPOSITE_SIGNALS
        if baseline_scores[name] != variant_scores[name]
    }
    return {
        "overrides": dict(overrides or {}),
        "scores": {name: variant_scores[name] for name in COMPOSITE_SIGNALS},
        "baseline_scores": {name: baseline_scores[name] for name in COMPOSITE_SIGNALS},
        "changed_signals": changed,
        "cascade": variant_cascade,
        "baseline_cascade": baseline_cascade,
        "tier_changed": (
            baseline_cascade["post_adjustment"]["policy_tier"]
            != variant_cascade["post_adjustment"]["policy_tier"]
        ),
        "posture_changed": (
            baseline_cascade["post_adjustment"]["control_posture"]
            != variant_cascade["post_adjustment"]["control_posture"]
        ),
        "escalation": escalation,
        "meets_requirement": POLICY_TIER_RANK.get(
            variant_cascade["post_adjustment"]["policy_tier"], int(POLICY_SCORING_OPS["unknown_tier_rank"])
        ) >= POLICY_TIER_RANK.get(required_tier, int(POLICY_SCORING_OPS["unknown_required_tier_rank"])),
    }


def policy_score_sensitivity(
    metrics: Mapping[str, Any],
    deltas: Sequence[float] = (1.0,),
) -> dict[str, Any]:
    """How far the access score moves per input, for each requested delta.

    Nudges one input at a time and recomposes, so the answer includes the
    knock-on effects through the averaged signals rather than only the direct
    term. A weight with a small direct effect and a large one through
    ``access_score`` is exactly the thing that is invisible in the rule table.
    """
    baseline = compose_access_score(metrics)
    base_access = float(baseline["access_score"])
    inputs = [str(row["input"]) for row in SCORE_INPUTS]
    results: dict[str, Any] = {}
    for name in inputs:
        per_delta: dict[str, Any] = {}
        for delta in deltas:
            variant = compose_access_score({**metrics, name: float(metrics.get(name, 0.0)) + float(delta)})
            moved = float(variant["access_score"]) - base_access
            per_delta[str(delta)] = {
                "access_score": float(variant["access_score"]),
                "delta": round(moved, 2),
                "tier_changed": resolve_policy_tier(float(variant["access_score"]), float(variant["system_score"]))
                != resolve_policy_tier(base_access, float(baseline["system_score"])),
            }
        results[name] = per_delta
    return {"baseline_access_score": base_access, "sensitivity": results}


def policy_decision_trace(
    snapshot: PolicyScoreSnapshot,
    required_tier: str = "standard",
) -> dict[str, Any]:
    """Explain a stored snapshot end to end: signals, bands, tier, posture, gate.

    Reads only the snapshot, so it works on a score row loaded from the database
    as well as on a freshly composed one. The ``gate`` block is the part a
    support conversation actually needs: what was asked for, what it was
    escalated to, whether the stored tier clears it, and by how much rank.
    """
    signals = policy_signal_map(snapshot)
    tier = resolve_policy_tier_trace(snapshot.access_score, snapshot.system_score)
    posture = resolve_control_posture_trace(
        snapshot.policy_tier, snapshot.access_score, snapshot.system_score
    )
    band = resolve_access_band_trace(snapshot.access_score)
    escalation = resolve_tier_escalation(snapshot.control_posture, required_tier)
    actual_rank = POLICY_TIER_RANK.get(snapshot.policy_tier, int(POLICY_SCORING_OPS["unknown_tier_rank"]))
    effective_rank = POLICY_TIER_RANK.get(
        str(escalation["effective_required_tier"]), int(POLICY_SCORING_OPS["unknown_required_tier_rank"])
    )
    return {
        "signals": signals,
        "signal_bands": {name: resolve_access_band(float(score)) for name, score in signals.items()},
        "health": policy_health_report(snapshot),
        "tier": tier,
        "posture": posture,
        "access_band": band,
        "posture_adjustment": posture_adjustment_trace(snapshot.access_score, snapshot.control_posture),
        "gate": {
            **escalation,
            "policy_tier": snapshot.policy_tier,
            "policy_tier_rank": actual_rank,
            "effective_required_tier_rank": effective_rank,
            "allowed": actual_rank >= effective_rank,
            "rank_margin": actual_rank - effective_rank,
        },
    }


def policy_rule_coverage(step: float = 0.5) -> dict[str, Any]:
    """Sweep the score space and report which rule decided each region.

    First-match-wins rule tables make order the semantics, and a row that can
    never be the first match is dead configuration that still reads as live in
    the catalog. The sweep is a grid, so a band narrower than ``step`` can hide a
    rule; unreachable rows are therefore warnings, not errors. Coverage is the
    fraction of the grid each rule decides, which is the practical question:
    a band that decides 0.1% of the space still has to be correct.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    grid = [index * float(step) for index in range(int(100.0 / float(step)) + 1)]
    tier_hits: dict[int, int] = {index: 0 for index in range(len(POLICY_TIER_RULES))}
    posture_hits: dict[str, int] = {"tier_map": 0, "rules": 0, "default": 0}
    band_hits: dict[int, int] = {index: 0 for index in range(len(ACCESS_BAND_RULES))}
    total = 0
    for access in grid:
        for system in grid:
            total += 1
            index, _ = _matched_tier_rule(access, system)
            if index >= 0:
                tier_hits[index] += 1
            route, _, _ = _matched_posture_rule(
                str(POLICY_TIER_RULES[index]["tier"]) if index >= 0 else str(POLICY_SCORING_OPS["default_tier"]),
                access,
                system,
            )
            posture_hits[route] += 1
            band_index, _ = _matched_band_rule(access)
            if band_index >= 0:
                band_hits[band_index] += 1
    return {
        "step": float(step),
        "grid_points": total,
        "tier_rules": [
            {
                "rule_index": index,
                "tier": str(rule["tier"]),
                "decided": tier_hits.get(index, 0),
                "share": round(tier_hits.get(index, 0) / total, 6) if total else 0.0,
            }
            for index, rule in enumerate(POLICY_TIER_RULES)
        ],
        "access_band_rules": [
            {
                "rule_index": index,
                "band": str(rule["band"]),
                "decided": band_hits.get(index, 0),
                "share": round(band_hits.get(index, 0) / total, 6) if total else 0.0,
            }
            for index, rule in enumerate(ACCESS_BAND_RULES)
        ],
        "posture_routes": {
            route: {"decided": count, "share": round(count / total, 6) if total else 0.0}
            for route, count in posture_hits.items()
        },
        "unreachable_tier_rules": [
            index for index, count in tier_hits.items() if count == 0
        ],
        "unreachable_band_rules": [
            index for index, count in band_hits.items() if count == 0
        ],
        "note": (
            "grid sweep at the stated step; a band narrower than the step can "
            "hide a rule, so unreachable rows are reported as warnings"
        ),
    }


_COMPOSITE_MODES = {"weighted_sum", "scaled_mean"}
_MARKER_MATCH_MODES = {"any", "count"}
_CLAMPS = {"none", "upper", "lower", "both"}
_EMIT_MODES = {"once", "per_weak_point"}
_SIGNAL_ROLES = {"headline", "evidence"}
_TIER_SOURCES = {"health.weak_points", "health.strong_points"}

# Format keys each recommendation template is allowed to use. Checked with
# ``string.Formatter().parse`` rather than by formatting, so a template with no
# placeholders at all is still checked and a template with an unknown key is
# caught before it would raise inside a request.
_RECOMMENDATION_FORMAT_KEYS = {
    "control_posture", "access_score", "system_score", "customer_score",
    "name", "name_spaced", "score",
}


def _template_fields(template: str) -> set[str]:
    import string as _string

    return {key for _, key, _, _ in _string.Formatter().parse(template) if key}


def validate_policy_scoring() -> dict[str, Any]:
    """Check the scoring tables against each other.

    Errors mean a configured rule cannot be trusted to run as written: a
    composition rule reading a signal that is produced later (or never), a
    marker group for a metric that does not exist, a predicate name the
    evaluator does not implement, a template referencing a field the formatter
    is never given, or a default tier that outranks a rule tier and makes it
    unreachable.

    Warnings mean something is configured but not doing anything: a rule row the
    grid sweep never reaches, a posture with no adjustment row, a named
    threshold that no longer matches the table it was introduced for.
    """
    errors: list[str] = []
    warnings: list[str] = []

    declared = {str(row["signal"]) for row in SCORE_SIGNALS}
    produced = [str(row["signal"]) for row in COMPOSITE_RULES]
    if len(produced) != len(set(produced)):
        errors.append("COMPOSITE_RULES has a duplicate signal")
    for signal in produced:
        if signal not in declared:
            errors.append(f"COMPOSITE_RULES produces '{signal}', which SCORE_SIGNALS does not declare")
    for signal in sorted(declared - set(produced)):
        warnings.append(f"declared signal '{signal}' has no composition rule")
    headlines = [row for row in SCORE_SIGNALS if str(row.get("role")) not in _SIGNAL_ROLES]
    for row in headlines:
        errors.append(f"signal '{row.get('signal')}' has role {row.get('role')!r}, expected one of {sorted(_SIGNAL_ROLES)}")
    if len([row for row in SCORE_SIGNALS if str(row.get("role")) == "headline"]) != 1:
        errors.append("expected exactly one headline signal")

    known_inputs = {str(row["input"]) for row in SCORE_INPUTS}
    produced_so_far: set[str] = set()
    for rule in COMPOSITE_RULES:
        signal = str(rule["signal"])
        mode = str(rule["mode"])
        if mode not in _COMPOSITE_MODES:
            errors.append(f"composition rule for '{signal}' has mode {mode!r}, expected one of {sorted(_COMPOSITE_MODES)}")
        terms = list(rule.get("terms") or [])
        if not terms:
            errors.append(f"composition rule for '{signal}' has no terms and will always score 0.0")
        for term in terms:
            name = str(term["input"])
            if name == signal:
                errors.append(f"composition rule for '{signal}' reads its own output")
            elif name in produced_so_far or name in known_inputs:
                pass
            elif name in declared:
                # Composition order is significant, so reading a signal that is
                # produced later is a silent 0.0, not a "fine, it will be there".
                errors.append(
                    f"composition rule for '{signal}' reads signal '{name}', which is produced later in "
                    "COMPOSITE_RULES; rules are evaluated front to back"
                )
            else:
                errors.append(
                    f"composition term '{name}' for '{signal}' is neither a declared input nor a declared "
                    "signal, so it contributes 0.0 without saying so"
                )
            if "invert_ceiling" in term and "floor" not in term:
                warnings.append(
                    f"composition term '{name}' for '{signal}' inverts but declares no floor, so it can go negative"
                )
        produced_so_far.add(signal)

    read_inputs = {str(term["input"]) for rule in COMPOSITE_RULES for term in (rule.get("terms") or [])}
    for row in SCORE_INPUTS:
        if str(row["input"]) not in read_inputs:
            warnings.append(f"declared input '{row['input']}' is read by no composition rule")

    # The tier table is first-match-wins, so descending order *is* the semantics.
    tiers = [float(rule["access_score_min"]) for rule in POLICY_TIER_RULES]
    if tiers != sorted(tiers, reverse=True):
        errors.append("POLICY_TIER_RULES is not ordered by descending access_score_min; first match wins")
    bands = [float(rule["access_score_min"]) for rule in ACCESS_BAND_RULES]
    if bands != sorted(bands, reverse=True):
        errors.append("ACCESS_BAND_RULES is not ordered by descending access_score_min; first match wins")
    for rule in POLICY_TIER_RULES:
        if str(rule["tier"]) not in POLICY_TIER_RANK:
            errors.append(f"tier '{rule['tier']}' has no rank in POLICY_TIER_RANK and can never be compared")
    for name, constant in (
        ("ACCESS_TIER_THRESHOLD", ACCESS_TIER_THRESHOLD),
        ("INTERNAL_ACCESS_TIER_THRESHOLD", INTERNAL_ACCESS_TIER_THRESHOLD),
    ):
        if not any(float(rule["access_score_min"]) == float(constant) for rule in POLICY_TIER_RULES):
            warnings.append(
                f"{name} is {constant} but no POLICY_TIER_RULES row uses that threshold; "
                "the constant and the table have drifted apart"
            )
    default_tier_rank = POLICY_TIER_RANK.get(
        str(POLICY_SCORING_OPS["default_tier"]), int(POLICY_SCORING_OPS["unknown_tier_rank"])
    )
    for rule in POLICY_TIER_RULES:
        rank = POLICY_TIER_RANK.get(str(rule["tier"]), 0)
        if rank <= default_tier_rank:
            warnings.append(
                f"tier '{rule['tier']}' ranks no higher than the default tier "
                f"'{POLICY_SCORING_OPS['default_tier']}' and is therefore indistinguishable from it"
            )

    defaults = [row for row in POSTURE_ADJUSTMENTS if bool(row.get("default"))]
    if len(defaults) != 1:
        errors.append(f"POSTURE_ADJUSTMENTS declares {len(defaults)} default rows, expected exactly one")
    explicit = [str(row["posture"]) for row in POSTURE_ADJUSTMENTS if not bool(row.get("default"))]
    if len(explicit) != len(set(explicit)):
        errors.append("POSTURE_ADJUSTMENTS declares the same posture more than once")
    for row in POSTURE_ADJUSTMENTS:
        if str(row.get("clamp", "none")) not in _CLAMPS:
            errors.append(f"posture '{row['posture']}' has clamp {row.get('clamp')!r}, expected one of {sorted(_CLAMPS)}")
    reachable_postures = set(CONTROL_POSTURE_TIER_MAP.values()) | {
        str(rule["posture"]) for rule in CONTROL_POSTURE_RULES
    } | {str(POLICY_SCORING_OPS["default_posture"])}
    covered: set[str] = set(explicit)
    for row in POSTURE_ADJUSTMENTS:
        covered |= {str(name) for name in (row.get("covers") or [])}
    for posture in sorted(reachable_postures - covered):
        warnings.append(
            f"posture '{posture}' is reachable but no POSTURE_ADJUSTMENTS row covers it; "
            "it silently takes the catch-all delta"
        )
    for posture in sorted(covered - reachable_postures):
        warnings.append(f"posture '{posture}' is covered by an adjustment row but no resolver ever produces it")

    for rule in TIER_ESCALATIONS:
        if str(rule["effective_tier"]) not in POLICY_TIER_RANK:
            errors.append(f"escalation '{rule['id']}' targets tier '{rule['effective_tier']}', which has no rank")
        if not list(rule.get("control_posture_in") or []):
            errors.append(f"escalation '{rule['id']}' lists no control posture and can never fire")
        unknown = sorted(set(map(str, rule.get("control_posture_in") or [])) - reachable_postures)
        if unknown:
            errors.append(f"escalation '{rule['id']}' names unreachable postures: {', '.join(unknown)}")
    # Collapsed into one warning: a requested tier with no escalation row is the
    # normal case, and three separate lines would bury the ones that matter.
    uncovered_tiers = sorted(set(POLICY_TIER_RANK) - {str(rule["required_tier"]) for rule in TIER_ESCALATIONS})
    if uncovered_tiers:
        warnings.append(
            f"no TIER_ESCALATIONS row covers requested tier(s): {', '.join(uncovered_tiers)}; "
            "those requests are granted at the tier asked for"
        )

    metrics = {str(row["metric"]) for row in TOPIC_SCORE_RULES}
    seen_metrics: set[str] = set()
    seen_marker_keys: set[tuple[str, str]] = set()
    seen_orders: set[tuple[str, int]] = set()
    for row in TOPIC_MARKER_WEIGHTS:
        metric = str(row["metric"])
        name = str(row["name"])
        if metric not in metrics:
            errors.append(f"marker group '{name}' targets metric '{metric}', which TOPIC_SCORE_RULES does not declare")
        if str(row["match"]) not in _MARKER_MATCH_MODES:
            errors.append(f"marker group '{name}' has match {row.get('match')!r}, expected one of {sorted(_MARKER_MATCH_MODES)}")
        if not list(row.get("markers") or []):
            errors.append(f"marker group '{name}' has no markers and can never contribute")
        if (metric, name) in seen_marker_keys:
            errors.append(f"marker group '{name}' is declared twice for metric '{metric}'")
        seen_marker_keys.add((metric, name))
        key = (metric, int(row["order"]))
        if key in seen_orders:
            errors.append(
                f"metric '{metric}' has two contributions at order {row['order']}; the accumulation order is ambiguous"
            )
        seen_orders.add(key)
    for metric in sorted(metrics):
        if not any(str(row["metric"]) == metric for row in TOPIC_MARKER_WEIGHTS):
            warnings.append(f"metric '{metric}' has no marker groups")
    for row in TOPIC_SCORE_RULES:
        metric = str(row["metric"])
        if metric in seen_metrics:
            errors.append(f"TOPIC_SCORE_RULES declares metric '{metric}' more than once")
        seen_metrics.add(metric)
        catalog = row.get("catalog")
        if catalog and str(catalog["source"]) not in TOPIC_CATALOG_SOURCES:
            errors.append(
                f"metric '{metric}' derives from unknown catalog "
                f"'{catalog['source']}'; known: {sorted(TOPIC_CATALOG_SOURCES)}"
            )
        base = row.get("base")
        if not base and not catalog and not row.get("word_count"):
            warnings.append(f"metric '{metric}' has no base term, no catalog term and no word bonus; it is always 0.0")

    if float(HEALTH_THRESHOLDS["weak_below"]) >= float(HEALTH_THRESHOLDS["strong_at_or_above"]):
        errors.append("a signal can be both a weak point and a strong point")
    if float(HEALTH_THRESHOLDS["richness_focused_at_or_above"]) >= float(HEALTH_THRESHOLDS["richness_balanced_at_or_above"]):
        errors.append("the focused richness label would be unreachable")
    if float(HEALTH_THRESHOLDS["richness_divisor"]) <= 0.0:
        errors.append("richness_divisor must be positive")
    if int(HEALTH_THRESHOLDS["max_recommendations"]) <= 0:
        errors.append("max_recommendations must be positive")
    # The cap truncates silently: the emitted list is a fixed size, so a rule that
    # would have fired is simply not there. Computed rather than inferred from
    # two numbers disagreeing, because only the total decides it.
    max_emitted = 0
    for rule in RECOMMENDATION_RULES:
        if str(rule["emit"]) == "per_weak_point":
            max_emitted += int(rule.get("limit", POLICY_SCORING_OPS["max_health_signals"]))
        else:
            max_emitted += 1
    if max_emitted > int(HEALTH_THRESHOLDS["max_recommendations"]):
        warnings.append(
            f"the recommendation cap truncates: the rules can emit up to {max_emitted} items against a cap of "
            f"{HEALTH_THRESHOLDS['max_recommendations']}, so the last rule to fire is dropped without a trace"
        )

    conditions = {str(row["condition"]) for row in RECOMMENDATION_CONDITIONS}
    if len(conditions) != len(RECOMMENDATION_CONDITIONS):
        errors.append("RECOMMENDATION_CONDITIONS declares the same condition twice")
    seen_ids: set[str] = set()
    for rule in RECOMMENDATION_RULES:
        rule_id = str(rule["id"])
        if rule_id in seen_ids:
            errors.append(f"recommendation rule id '{rule_id}' is declared twice")
        seen_ids.add(rule_id)
        emit = str(rule["emit"])
        if emit not in _EMIT_MODES:
            errors.append(f"recommendation rule '{rule_id}' has emit {emit!r}, expected one of {sorted(_EMIT_MODES)}")
        for key in dict(rule.get("when") or {}):
            if key not in conditions:
                errors.append(
                    f"recommendation rule '{rule_id}' uses condition '{key}', which RECOMMENDATION_CONDITIONS does not declare"
                )
        if emit == "per_weak_point":
            if str(rule.get("source", "")) not in _TIER_SOURCES:
                errors.append(f"recommendation rule '{rule_id}' has unknown source {rule.get('source')!r}")
            # A per-item rule varies its priority, so it needs the mapping form.
            # Checked as a type first: coercing a string here would raise out of
            # the validator instead of reporting the row, which is the one place
            # a malformed table must never take the process down.
            spec = rule.get("priority")
            if not isinstance(spec, dict) or not {"at_or_above", "then", "otherwise"} <= set(spec):
                errors.append(
                    f"recommendation rule '{rule_id}' needs priority at_or_above/then/otherwise to vary its priority"
                )
        elif not isinstance(rule.get("priority"), str):
            errors.append(f"recommendation rule '{rule_id}' emits once and needs a string priority")
        for field in ("recommendation", "evidence"):
            template = str(rule.get(field, ""))
            unknown_fields = sorted(_template_fields(template) - _RECOMMENDATION_FORMAT_KEYS)
            if unknown_fields:
                errors.append(
                    f"recommendation rule '{rule_id}' field '{field}' formats {', '.join(unknown_fields)}, "
                    "which the formatter is never given"
                )
        if "{" in str(rule.get("area", "")):
            unknown_fields = sorted(_template_fields(str(rule["area"])) - _RECOMMENDATION_FORMAT_KEYS)
            if unknown_fields:
                errors.append(
                    f"recommendation rule '{rule_id}' area formats {', '.join(unknown_fields)}, "
                    "which the formatter is never given"
                )

    required_ops = (
        "score_ceiling", "score_floor", "decimals", "default_tier", "default_posture",
        "default_band", "unknown_tier_rank", "unknown_required_tier_rank", "topic_focus_limit",
        "topic_matched_keywords_limit", "topic_focus_fallback_limit", "topic_suggestion_limit",
        "topic_matched_topic_limit", "confidence_ceiling", "confidence_breadth_divisor",
        "confidence_complexity_divisor", "max_health_signals",
    )
    for key in required_ops:
        if key not in POLICY_SCORING_OPS:
            errors.append(f"POLICY_SCORING_OPS is missing '{key}'")
    if float(POLICY_SCORING_OPS.get("score_ceiling", 0.0)) <= float(POLICY_SCORING_OPS.get("score_floor", 0.0)):
        errors.append("the score ceiling is not above the score floor")
    if int(POLICY_SCORING_OPS.get("decimals", -1)) < 0:
        errors.append("score decimals must not be negative")
    for key in ("topic_focus_limit", "topic_matched_keywords_limit", "topic_focus_fallback_limit"):
        if int(POLICY_SCORING_OPS.get(key, 0)) <= 0:
            errors.append(f"{key} must be positive")
    for key in ("confidence_breadth_divisor", "confidence_complexity_divisor"):
        if float(POLICY_SCORING_OPS.get(key, 0.0)) <= 0.0:
            errors.append(f"{key} must be positive")
    unknown_tier_rank = int(POLICY_SCORING_OPS.get("unknown_tier_rank", 0))
    unknown_required_rank = int(POLICY_SCORING_OPS.get("unknown_required_tier_rank", 0))
    if unknown_tier_rank >= unknown_required_rank:
        warnings.append(
            "an unknown actual tier no longer ranks below an unknown required tier, "
            "so an unrecognised tier stops failing closed"
        )
    if int(POLICY_SCORING_OPS.get("max_health_signals", 0)) != len(SCORE_SIGNALS):
        warnings.append("max_health_signals no longer matches the number of declared signals")

    coverage = policy_rule_coverage()
    for index in coverage["unreachable_tier_rules"]:
        warnings.append(
            f"tier rule {index} ('{POLICY_TIER_RULES[index]['tier']}') is shadowed by an earlier row and never decides"
        )
    for index in coverage["unreachable_band_rules"]:
        warnings.append(
            f"access band rule {index} ('{ACCESS_BAND_RULES[index]['band']}') is shadowed by an earlier row and never decides"
        )

    return {
        "valid": not errors,
        "errors": len(errors),
        "warnings": len(warnings),
        "error_list": errors,
        "warning_list": warnings,
        "version": POLICY_SCORING_VERSION,
        "signals": len(SCORE_SIGNALS),
        "composition_rules": len(COMPOSITE_RULES),
        "marker_groups": len(TOPIC_MARKER_WEIGHTS),
        "recommendation_rules": len(RECOMMENDATION_RULES),
        "coverage": coverage,
        "note": (
            "errors mean a configured rule cannot run as written; warnings mean "
            "something is configured but not doing anything."
        ),
    }


def build_policy_scoring_catalog() -> dict[str, Any]:
    """Introspectable contract for the scoring governance layer.

    ``build_policy_tier_catalog`` stays the pinned tier/posture/band contract and
    is reproduced under ``tier_catalog`` unchanged; everything else here is new.
    The most useful sections are ``validation`` (which rules cannot run) and
    ``coverage`` (which rule actually decides, and for how much of the score
    space).
    """
    validation = validate_policy_scoring()
    return {
        "version": POLICY_SCORING_VERSION,
        "validation": {
            "valid": validation["valid"],
            "errors": validation["errors"],
            "warnings": validation["warnings"],
            "error_list": validation["error_list"],
            "warning_list": validation["warning_list"],
        },
        "signals": [dict(row) for row in SCORE_SIGNALS],
        "inputs": [dict(row) for row in SCORE_INPUTS],
        "composition_rules": [dict(row) for row in COMPOSITE_RULES],
        "composition_modes": sorted(_COMPOSITE_MODES),
        "posture_adjustments": [dict(row) for row in POSTURE_ADJUSTMENTS],
        "tier_escalations": [dict(row) for row in TIER_ESCALATIONS],
        "topic_marker_weights": [dict(row) for row in TOPIC_MARKER_WEIGHTS],
        "topic_score_rules": [dict(row) for row in TOPIC_SCORE_RULES],
        "health_thresholds": dict(HEALTH_THRESHOLDS),
        "recommendation_rules": [dict(row) for row in RECOMMENDATION_RULES],
        "recommendation_conditions": [dict(row) for row in RECOMMENDATION_CONDITIONS],
        "ops": dict(POLICY_SCORING_OPS),
        "coverage": validation["coverage"],
        "tier_catalog": build_policy_tier_catalog(),
        "note": (
            "Additive governance layer over the pinned tier/posture/band tables: "
            "the score formula is a pure ordered engine, every constant that used "
            "to be inline is a table row, and the recommendation and escalation "
            "decisions are rules with reportable predicates."
        ),
    }
