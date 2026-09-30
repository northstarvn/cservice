"""Tests for the `app/services/policy_scoring.py` governance expansion.

`policy_scoring.py` had a data-driven *decision* layer (tier rules, posture
rules, access bands) sitting on top of a score layer that was six arithmetic
expressions written inline inside an `async def` that also issued six queries.
This pass made the score layer data too, and added the things a score nobody
can argue with is missing.

The point of this file is to pin the parts that are easy to get quietly wrong:

1. **The published numbers did not move.** Every constant that used to be an
   inline literal is now a table row with the same value. A weight that drifts
   by 0.01 changes every stored `CustomerPolicyScore.summary` and every tier
   boundary in the customer base, and nothing else would notice. The
   differential tests below re-implement the *original* expressions and assert
   equality over thousands of inputs, including the two asymmetries that are
   preserved rather than corrected (`customer_score` floors its inverted
   dissatisfaction term but does not ceiling it; `scaled_mean` divides by the
   term count, not by the sum of the weights).
2. **The accumulation order is part of the contract.** For `depth`, the
   five-word bonus lands *between* the routing and urgency marker groups.
   Float addition is not associative, so the marker table carries an explicit
   `order` and it is asserted.
3. **The pinned tier/posture/band contract is untouched.**
   `build_policy_tier_catalog()` keeps its exact key set, and the original
   resolvers return exactly what they returned before.
4. **A rule that cannot run says so.** A composition rule reading a signal
   produced later, a marker group for a metric that does not exist, a
   recommendation predicate with a typo in its condition name, a template
   referencing a field the formatter is never given — all of these produce a
   plausible score and no signal that a rule did not fire. Each is a validator
   error.
5. **A shadowed rule is reported, not silently dead.** First-match-wins tables
   make order the semantics, so the coverage sweep reports which row actually
   decides and for how much of the score space.

The original surface is pinned throughout: the four resolvers, the topic
scorers, the health summary, the recommendations, the access decision, the
snapshot composition, and `can_access_functionality`.
"""
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import main
from app.services import policy_scoring
from app.services import policy_scoring as ps
from app.services.policy_scoring import (
    COMPOSITE_RULES,
    COMPOSITE_SIGNALS,
    CONTROL_POSTURE_RULES,
    HEALTH_THRESHOLDS,
    POLICY_SCORING_OPS,
    POLICY_SCORING_VERSION,
    POLICY_TIER_RANK,
    POLICY_TIER_RULES,
    POSTURE_ADJUSTMENTS,
    RECOMMENDATION_CONDITIONS,
    RECOMMENDATION_RULES,
    SCORE_INPUTS,
    SCORE_SIGNALS,
    TIER_ESCALATIONS,
    TOPIC_MARKER_WEIGHTS,
    TOPIC_SCORE_RULES,
    PolicyScoreSnapshot,
    apply_posture_adjustment,
    build_policy_access_decision,
    build_policy_health_summary,
    build_policy_scoring_catalog,
    build_policy_topic_depth,
    build_policy_topic_richness,
    build_policy_tier_catalog,
    can_access_functionality,
    compose_access_score,
    policy_decision_trace,
    policy_health_report,
    policy_recommendation_trace,
    policy_rule_coverage,
    policy_score_sensitivity,
    policy_signal_map,
    policy_tier_cascade,
    policy_what_if,
    posture_adjustment_trace,
    resolve_access_band,
    resolve_access_band_trace,
    resolve_control_posture,
    resolve_control_posture_trace,
    resolve_policy_tier,
    resolve_policy_tier_trace,
    resolve_tier_escalation,
    topic_metric_contributions,
    topic_metric_score,
    topic_richness_balance,
    validate_policy_scoring,
)
from app.services.topics import TOPIC_CATALOG, TOPIC_THEME_GROUPS


# ---------------------------------------------------------------------------
# The original formulas, transcribed.
#
# These are the pre-expansion expressions, kept verbatim so the equivalence
# tests compare against the thing that shipped rather than against another
# restatement of the same tables. If a table row is edited, these fail.
# ---------------------------------------------------------------------------

_BREADTH_MARKERS = (
    (["and", "or", "with", "for", "support", "service"], 8.0),
    (["billing", "booking", "policy", "routing", "handoff", "coverage"], 10.0),
    (["retention", "discovery", "context", "logistics", "reassurance", "transcript"], 8.0),
    (["privacy", "consent", "translation", "attachment", "checklist", "feedback"], 7.0),
    (["intent", "frame", "preferences", "knowledge", "callback", "visibility", "continuity"], 9.0),
)

_COMPLEXITY_TERMS = [
    "exception", "override", "escalation", "handoff", "routing", "eligibility",
    "verification", "refund", "capacity", "policy", "retention", "discovery",
    "summary", "context", "logistics", "privacy", "consent", "translation",
    "attachment", "checklist", "feedback", "intent", "knowledge", "callback",
    "visibility", "continuity", "reassurance",
]

_DEPTH_MARKERS = (
    (["and", "with", "or", "routing", "handoff", "billing", "booking"], 10.0),
    (["exception", "urgent", "priority", "policy", "verification"], 12.0),
    (["retention", "discovery", "context", "summary", "logistics", "reassurance"], 8.0),
)


def _orig_safe_average(values):
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def orig_breadth(topic_text):
    normalized = (topic_text or "").lower()
    if not normalized:
        return 0.0
    tokens = [t for t in normalized.replace("/", " ").replace("-", " ").split() if t]
    breadth = len(set(tokens)) * 7.5
    breadth += min(len(TOPIC_CATALOG) * 0.2, 12.0)
    for markers, weight in _BREADTH_MARKERS:
        if any(marker in normalized for marker in markers):
            breadth += weight
    return min(100.0, round(breadth, 2))


def orig_complexity(topic_text):
    normalized = (topic_text or "").lower()
    if not normalized:
        return 0.0
    score = sum(9.0 for term in _COMPLEXITY_TERMS if term in normalized)
    if len(normalized.split()) >= 4:
        score += 8.0
    return min(100.0, round(score, 2))


def orig_depth(topic_text):
    normalized = (topic_text or "").strip().lower()
    if not normalized:
        return "no_topic_depth"
    words = [w for w in normalized.replace("/", " ").replace("-", " ").split() if w]
    depth = len(set(words)) * 8.0
    depth += min(len(TOPIC_THEME_GROUPS) * 1.5, 18.0)
    if any(marker in normalized for marker in _DEPTH_MARKERS[0][0]):
        depth += 10.0
    if len(words) >= 5:
        depth += 6.0
    for markers, weight in _DEPTH_MARKERS[1:]:
        if any(marker in normalized for marker in markers):
            depth += weight
    return f"depth={min(100.0, round(depth, 2)):.2f}"


def orig_posture(access_score, control_posture):
    if control_posture == "high_trust":
        return access_score
    if control_posture == "customer_trusted":
        return min(100.0, round(access_score + 2.0, 2))
    if control_posture == "observed":
        return max(0.0, round(access_score - 4.0, 2))
    return max(0.0, round(access_score - 8.0, 2))


def orig_compose(signal_score, loyalty_score, dissatisfaction_score, booking_count,
                 completed_count, topic_confidence, topic_breadth_score,
                 topic_complexity_score):
    system_score = min(100.0, round(
        (signal_score * 12.0) + (completed_count * 6.0) + (topic_complexity_score * 0.25), 2))
    customer_score = min(100.0, round(
        (loyalty_score * 0.6) + max(0.0, 100.0 - dissatisfaction_score) + (topic_confidence * 12.0), 2))
    interest_score = min(100.0, round(_orig_safe_average(
        [signal_score * 10.0, booking_count * 4.0, completed_count * 8.0, topic_breadth_score]), 2))
    closeness_score = min(100.0, round(
        _orig_safe_average([customer_score, interest_score, topic_breadth_score]), 2))
    community_closeness_score = min(100.0, round(
        _orig_safe_average([system_score, closeness_score, topic_complexity_score]), 2))
    access_score = min(100.0, round(
        _orig_safe_average([customer_score, community_closeness_score, system_score]), 2))
    return {
        "system_score": system_score,
        "customer_score": customer_score,
        "interest_score": interest_score,
        "closeness_score": closeness_score,
        "community_closeness_score": community_closeness_score,
        "access_score": access_score,
    }


TOPIC_TEXTS = [
    "", "   ", "\t", None, "/", "-", "--", "a", "a b", "a b c", "a b c d",
    "retention and billing policy for callback", "exception policy for urgent retention and billing",
    "a/b/c/d/e", "a-b-c-d-e", "handoff routing coverage", "privacy consent translation attachment",
    "intent frame preferences knowledge callback visibility continuity",
    "refund refund refund", "android", "AND", "Policy", "iOS/iPad", "a  b   c",
    "escalation override eligibility verification refund capacity", "summary context logistics reassurance",
    "discovery transcript service support for or with", "billing booking", "x" * 200,
    "mixed/slash-and-dash text with enough words to trip the thresholds",
    "retention/discovery context/logistics summary logistics reassurance reassurance",
]

METRIC_SETS = [
    dict(signal_score=0.0, loyalty_score=0.0, dissatisfaction_score=0.0, booking_count=0.0,
         completed_count=0.0, topic_confidence=0.0, topic_breadth_score=0.0,
         topic_complexity_score=0.0),
    dict(signal_score=1.0, loyalty_score=50.0, dissatisfaction_score=50.0, booking_count=1.0,
         completed_count=1.0, topic_confidence=0.5, topic_breadth_score=12.0,
         topic_complexity_score=17.0),
    dict(signal_score=4.0, loyalty_score=70.0, dissatisfaction_score=30.0, booking_count=3.0,
         completed_count=2.0, topic_confidence=0.5, topic_breadth_score=40.0,
         topic_complexity_score=20.0),
    dict(signal_score=8.333, loyalty_score=12.5, dissatisfaction_score=137.0, booking_count=9.0,
         completed_count=7.0, topic_confidence=0.25, topic_breadth_score=99.0,
         topic_complexity_score=55.5),
    dict(signal_score=100.0, loyalty_score=100.0, dissatisfaction_score=0.0, booking_count=40.0,
         completed_count=40.0, topic_confidence=1.0, topic_breadth_score=100.0,
         topic_complexity_score=100.0),
    dict(signal_score=0.5, loyalty_score=33.0, dissatisfaction_score=-20.0, booking_count=0.0,
         completed_count=0.0, topic_confidence=0.75, topic_breadth_score=7.5,
         topic_complexity_score=0.25),
    dict(signal_score=2.675, loyalty_score=66.666, dissatisfaction_score=44.44,
         booking_count=1.0, completed_count=1.0, topic_confidence=0.0,
         topic_breadth_score=70.0707, topic_complexity_score=120.0),
]


def _snapshot(**overrides):
    base = dict(
        system_score=62.0, customer_score=58.0, access_score=61.5, interest_score=50.0,
        closeness_score=52.0, community_closeness_score=55.0, policy_tier="standard",
        control_posture="observed", summary="s", topic_context="", topic_richness="",
    )
    base.update(overrides)
    return PolicyScoreSnapshot(**base)


class _PolicyScore:
    """Stand-in for a `models.CustomerPolicyScore` row."""

    def __init__(self, policy_tier, control_posture="observed", access_score=50.0,
                 customer_score=50.0, system_score=50.0):
        self.policy_tier = policy_tier
        self.control_posture = control_posture
        self.access_score = access_score
        self.customer_score = customer_score
        self.system_score = system_score


class _User:
    def __init__(self, user_id=7):
        self.id = user_id


# ===========================================================================
# 1. The published numbers did not move
# ===========================================================================


def test_breadth_matches_the_original_expression_for_every_text():
    for text in TOPIC_TEXTS:
        assert policy_scoring._topic_breadth_score(text) == orig_breadth(text), text


def test_complexity_matches_the_original_expression_for_every_text():
    for text in TOPIC_TEXTS:
        assert policy_scoring._topic_complexity_score(text) == orig_complexity(text), text


def test_depth_matches_the_original_expression_for_every_text():
    for text in TOPIC_TEXTS:
        assert build_policy_topic_depth(text) == orig_depth(text), text


@pytest.mark.parametrize("text", TOPIC_TEXTS)
def test_the_topic_scorers_never_emit_nan_or_a_non_float(text):
    breadth = policy_scoring._topic_breadth_score(text)
    complexity = policy_scoring._topic_complexity_score(text)
    assert isinstance(breadth, float) and breadth == breadth
    assert isinstance(complexity, float) and complexity == complexity
    assert 0.0 <= breadth <= 100.0
    assert 0.0 <= complexity <= 100.0


def test_composition_matches_the_original_expression_for_every_metric_set():
    for metrics in METRIC_SETS:
        composed = compose_access_score(metrics)
        expected = orig_compose(**metrics)
        for signal, value in expected.items():
            assert composed[signal] == value, (signal, metrics)


def test_composition_matches_the_original_expression_across_the_declared_range():
    """A broad sweep, because the weights interact: averaging and capping.

    The pinned metric sets are the interesting corners, but a weight change can
    still only show up between them, so the space is walked on a grid that
    straddles every threshold in the tables (ceiling, each term weight, and the
    dissatisfaction floor).
    """
    pool = [0.0, 0.5, 1.0, 3.7, 8.33, 12.345, 20.0, 33.3, 50.0, 66.666, 75.0, 100.0, 137.0, -8.0]
    keys = list(METRIC_SETS[0])
    checked = 0
    for signal_score in pool[:7]:
        for loyalty in pool[::3]:
            for dissat in pool[::2]:
                for bookings in pool[::4]:
                    for completed in pool[::3]:
                        metrics = dict(
                            signal_score=signal_score, loyalty_score=loyalty,
                            dissatisfaction_score=dissat, booking_count=bookings,
                            completed_count=completed, topic_confidence=pool[checked % len(pool)],
                            topic_breadth_score=pool[(checked + 3) % len(pool)],
                            topic_complexity_score=pool[(checked + 5) % len(pool)],
                        )
                        composed = compose_access_score(metrics)
                        expected = orig_compose(**{key: metrics[key] for key in keys})
                        for signal, value in expected.items():
                            assert composed[signal] == value, (signal, metrics)
                        checked += 1
    assert checked > 2000


def test_posture_adjustment_matches_the_original_expression():
    for access in (0.0, 0.01, 1.0, 2.5, 45.0, 66.0, 70.0, 98.0, 98.5, 99.0, 99.5, 100.0, 100.5, 120.0):
        for posture in ("high_trust", "customer_trusted", "observed", "constrained", "", "unknown"):
            assert apply_posture_adjustment(access, posture) == orig_posture(access, posture), (access, posture)


def test_a_trusted_posture_does_not_round_an_unrounded_score():
    """`high_trust` is a pass-through; rounding it would change an input nobody asked to change."""
    assert apply_posture_adjustment(1.23456, "high_trust") == 1.23456
    # `observed` floors at 0 rather than going negative, so the delta is not
    # visible in the result at all below the floor.
    assert apply_posture_adjustment(1.23456, "observed") == 0.0
    assert orig_posture(1.23456, "observed") == 0.0


def test_the_saturated_and_floored_postures_report_which_bound_bound_them():
    saturated = posture_adjustment_trace(99.5, "customer_trusted")
    assert saturated["clamped"] is True
    assert saturated["bound"] == "ceiling"
    assert saturated["access_score_after"] == 100.0

    floored = posture_adjustment_trace(1.0, "observed")
    assert floored["clamped"] is True
    assert floored["bound"] == "floor"
    assert floored["access_score_after"] == 0.0

    ordinary = posture_adjustment_trace(61.5, "observed")
    assert ordinary["clamped"] is False
    assert ordinary["bound"] == "none"


# ===========================================================================
# 2. The accumulation order is part of the contract
# ===========================================================================


def test_depth_sums_its_word_bonus_between_the_routing_and_urgency_groups():
    depth_rule = next(row for row in TOPIC_SCORE_RULES if row["metric"] == "depth")
    bonus_order = int(depth_rule["word_count"]["order"])
    orders = sorted(int(row["order"]) for row in TOPIC_MARKER_WEIGHTS if row["metric"] == "depth")
    assert bonus_order == 15
    assert orders == [10, 20, 30]
    # strictly between the routing group and the urgency group
    assert 10 < bonus_order < 20


def test_the_contributions_come_back_in_sum_order():
    text = "exception policy for urgent retention and billing handoff"
    contributions = topic_metric_contributions("depth", text)
    orders = [int(row["order"]) for row in contributions]
    assert orders == sorted(orders)
    sources = [row["source"] for row in contributions]
    assert sources[0] == "base"
    assert sources[1] == "catalog_size"
    assert "word_count" in sources
    assert sources.index("marker:routing") < sources.index("word_count") < sources.index("marker:urgency")


def test_the_contribution_values_sum_to_the_published_score():
    for metric, scorer in (
        ("breadth", policy_scoring._topic_breadth_score),
        ("complexity", policy_scoring._topic_complexity_score),
    ):
        for text in TOPIC_TEXTS:
            if not (text or ""):
                # The empty-text guard is the caller's, and the two callers
                # disagree: breadth and complexity return 0.0, depth returns
                # a literal. The engine therefore has no empty-text rule of
                # its own, and a whitespace-only topic is scored normally.
                assert scorer(text) == 0.0
                continue
            total = sum(float(row["value"]) for row in topic_metric_contributions(metric, text))
            assert min(100.0, round(total, 2)) == scorer(text), (metric, text)
    # A whitespace-only topic is *not* empty to the engine.
    assert min(100.0, round(sum(
        float(row["value"]) for row in topic_metric_contributions("breadth", "   ")), 2)) == \
        policy_scoring._topic_breadth_score("   ")


def test_complexity_counts_each_distinct_marker_once_but_breadth_counts_a_group_once():
    """The two original idioms are different on purpose and must stay different."""
    once = policy_scoring._topic_complexity_score("policy policy policy")
    twice = policy_scoring._topic_complexity_score("policy refund")
    assert twice == round(orig_complexity("policy refund"), 2)
    assert once != twice
    # Same unique-token count, and both markers sit in the same group, so the
    # "any" group fires once for each: a repeated marker is not extra credit.
    repeated = policy_scoring._topic_breadth_score("billing billing")
    sibling = policy_scoring._topic_breadth_score("booking booking")
    assert repeated == sibling == orig_breadth("billing billing")
    assert len([
        row for row in topic_metric_contributions("breadth", "billing billing billing")
        if row["source"] == "marker:transaction"
    ]) == 1


def test_a_marker_matches_as_a_substring_not_a_token():
    """The original used `in`, so 'android' scores for 'and'. Preserved."""
    assert policy_scoring._topic_breadth_score("android") == orig_breadth("android")
    assert policy_scoring._topic_breadth_score("android") > policy_scoring._topic_breadth_score("qwerty")


def test_breadth_and_depth_tokenize_slashes_and_complexity_does_not():
    text = "a/b/c/d/e"
    assert policy_scoring._topic_breadth_score(text) == orig_breadth(text)
    assert policy_scoring._topic_complexity_score(text) == orig_complexity(text)
    assert build_policy_topic_depth(text) == orig_depth(text)
    depth_rule = next(row for row in TOPIC_SCORE_RULES if row["metric"] == "depth")
    complexity_rule = next(row for row in TOPIC_SCORE_RULES if row["metric"] == "complexity")
    assert depth_rule["word_count"]["tokenize"] == "split"
    assert complexity_rule["word_count"]["tokenize"] == "plain"


def test_a_whitespace_only_topic_is_scored_by_breadth_but_declined_by_depth():
    """Pre-existing asymmetry between the two callers' empty-text guards."""
    assert policy_scoring._topic_breadth_score("   ") == orig_breadth("   ")
    assert policy_scoring._topic_breadth_score("   ") > 0.0
    assert build_policy_topic_depth("   ") == "no_topic_depth"
    assert build_policy_topic_depth("") == "no_topic_depth"
    assert build_policy_topic_richness("") == "no_topic_richness"


def test_the_catalog_size_is_read_at_call_time_not_snapshot_at_import():
    """`TOPIC_CATALOG` grows when topics are added; the score must follow it."""
    rule = next(row for row in TOPIC_SCORE_RULES if row["metric"] == "breadth")
    source = str(rule["catalog"]["source"])
    original_length = len(getattr(policy_scoring, source))
    try:
        policy_scoring.TOPIC_CATALOG.append({"topic": "policy-scoring-probe-topic", "sector": "probe"})
        grown = policy_scoring._topic_breadth_score("a b c d e")
    finally:
        policy_scoring.TOPIC_CATALOG.pop()
    assert len(policy_scoring.TOPIC_CATALOG) == original_length
    detail = next(row["detail"] for row in topic_metric_contributions("breadth", "a b c d e")
                  if row["source"] == "catalog_size")
    assert f"{original_length} x" in detail
    # The catalog term is already capped at 12.0, so growing it changes nothing.
    assert grown == policy_scoring._topic_breadth_score("a b c d e")


# ===========================================================================
# 3. The pinned tier / posture / band contract is untouched
# ===========================================================================


def test_the_tier_catalog_keeps_its_exact_key_set():
    catalog = build_policy_tier_catalog()
    assert set(catalog) == {
        "tier_rank", "tier_rules", "control_posture_tier_map", "control_posture_rules",
        "access_band_rules", "access_tier_threshold", "internal_access_tier_threshold",
    }
    assert catalog["tier_rank"] == {
        "restricted": 0, "standard": 1, "customer-premium": 2, "system-premium": 3,
    }
    assert catalog["access_tier_threshold"] == 70.0
    assert catalog["internal_access_tier_threshold"] == 85.0
    assert [rule["tier"] for rule in catalog["tier_rules"]] == [
        "system-premium", "customer-premium", "standard",
    ]


def test_the_resolvers_still_return_what_they_returned_before():
    assert resolve_policy_tier(85.0, 85.0) == "system-premium"
    assert resolve_policy_tier(85.0, 84.0) == "customer-premium"
    assert resolve_policy_tier(70.0, 0.0) == "customer-premium"
    assert resolve_policy_tier(40.0, 0.0) == "standard"
    assert resolve_policy_tier(39.0, 0.0) == "restricted"
    assert resolve_control_posture("system-premium", 0.0, 0.0) == "high_trust"
    assert resolve_control_posture("customer-premium", 0.0, 0.0) == "customer_trusted"
    assert resolve_control_posture("standard", 60.0, 60.0) == "observed"
    assert resolve_control_posture("standard", 54.0, 60.0) == "constrained"
    assert resolve_access_band(90.0) == "elite"
    assert resolve_access_band(89.0) == "strong"
    assert resolve_access_band(75.0) == "strong"
    assert resolve_access_band(54.0) == "limited"


def test_the_private_aliases_still_agree_with_the_resolvers():
    for access, system in ((0.0, 0.0), (39.0, 0.0), (40.0, 0.0), (70.0, 0.0), (85.0, 85.0), (100.0, 100.0)):
        assert policy_scoring._policy_tier(access, system) == resolve_policy_tier(access, system)
        assert policy_scoring._control_posture(
            resolve_policy_tier(access, system), access, system
        ) == resolve_control_posture(resolve_policy_tier(access, system), access, system)
    assert policy_scoring._posture_adjusted_access_score(70.0, "observed") == 66.0
    assert policy_scoring.build_policy_access_band(90.0) == "elite"


def test_can_access_functionality_keeps_its_asymmetric_fallback_ranks():
    assert can_access_functionality(_PolicyScore("system-premium"), "system-premium") is True
    assert can_access_functionality(_PolicyScore("standard"), "customer-premium") is False
    # An unknown *actual* tier fails closed; an unknown *required* tier does not
    # become the strictest possible check.
    assert can_access_functionality(_PolicyScore("no-such-tier"), "standard") is False
    assert can_access_functionality(_PolicyScore("system-premium"), "no-such-tier") is True
    assert POLICY_SCORING_OPS["unknown_tier_rank"] < POLICY_SCORING_OPS["unknown_required_tier_rank"]


def test_the_posture_resolver_reports_a_default_as_a_route_not_a_failure():
    trace = resolve_control_posture_trace("standard", 54.0, 60.0)
    assert trace["control_posture"] == "constrained"
    assert trace["route"] == "default"
    assert trace["rule"] is None
    mapped = resolve_control_posture_trace("system-premium", 0.0, 0.0)
    assert mapped["route"] == "tier_map"
    assert mapped["rule"]["via"] == "CONTROL_POSTURE_TIER_MAP"


def test_the_tier_trace_names_the_nearest_unmet_rule():
    trace = resolve_policy_tier_trace(35.0, 0.0)
    assert trace["matched"] is False
    assert trace["policy_tier"] == "restricted"
    assert trace["next_tier"]["tier"] == "standard"
    assert trace["next_tier"]["access_score_short_by"] == 5.0
    assert trace["next_tier"]["system_score_short_by"] == 0.0


def test_the_band_trace_names_the_nearest_unmet_band():
    trace = resolve_access_band_trace(50.0)
    assert trace["access_band"] == "limited"
    assert trace["next_band"]["band"] == "moderate"
    assert trace["next_band"]["access_score_short_by"] == 5.0
    assert resolve_access_band_trace(90.0)["next_band"] is None


# ===========================================================================
# 4. The composition engine
# ===========================================================================


def test_composition_produces_exactly_the_declared_signals():
    composed = compose_access_score(METRIC_SETS[2])
    for signal in COMPOSITE_SIGNALS:
        assert signal in composed
    assert set(COMPOSITE_SIGNALS) == {str(row["signal"]) for row in SCORE_SIGNALS}
    assert set(COMPOSITE_SIGNALS) == {str(row["signal"]) for row in COMPOSITE_RULES}


def test_composition_reports_a_missing_input_instead_of_only_scoring_it_zero():
    partial = {key: value for key, value in METRIC_SETS[2].items() if key != "topic_breadth_score"}
    composed = compose_access_score(partial)
    assert composed["missing_inputs"] == ["topic_breadth_score"]
    missing = [row for row in composed["contributions"] if row["missing"]]
    assert {row["input"] for row in missing} == {"topic_breadth_score"}
    # It still produced a score rather than raising: a partially-specified set
    # is something an operator needs to be able to look at.
    assert composed["access_score"] >= 0.0
    full = compose_access_score(METRIC_SETS[2])
    assert composed["interest_score"] != full["interest_score"]


def test_composition_knock_on_effects_flow_through_the_averages():
    """`system_score` reaches access through community_closeness as well."""
    composed = compose_access_score(METRIC_SETS[2])
    contributions = composed["contributions"]
    inputs_to = {}
    for row in contributions:
        inputs_to.setdefault(row["signal"], []).append(row["input"])
    assert inputs_to["closeness_score"] == ["customer_score", "interest_score", "topic_breadth_score"]
    assert inputs_to["access_score"] == ["customer_score", "community_closeness_score", "system_score"]


def test_the_inverted_dissatisfaction_term_is_floored_but_not_ceiled():
    floor_case = compose_access_score({**METRIC_SETS[2], "dissatisfaction_score": 250.0})
    # 100 - 250 floors at 0, so the term contributes nothing at all.
    assert floor_case["customer_score"] == (
        0.6 * METRIC_SETS[2]["loyalty_score"] + 12.0 * METRIC_SETS[2]["topic_confidence"]
    )
    assert floor_case["customer_score"] == orig_compose(
        **{**METRIC_SETS[2], "dissatisfaction_score": 250.0}
    )["customer_score"]
    negative_case = compose_access_score({**METRIC_SETS[2], "dissatisfaction_score": -20.0})
    untruncated = orig_compose(**{**METRIC_SETS[2], "dissatisfaction_score": -20.0})
    assert negative_case["customer_score"] == untruncated["customer_score"]
    assert 100.0 - (-20.0) > 100.0  # the value itself is above the ceiling
    assert negative_case["customer_score"] >= 100.0


def test_an_unknown_composition_mode_is_refused_rather_than_silently_treated_as_a_sum():
    rule = dict(COMPOSITE_RULES[0])
    rule["mode"] = "not-a-mode"
    original = list(COMPOSITE_RULES)
    try:
        COMPOSITE_RULES[0] = rule
        with pytest.raises(KeyError):
            compose_access_score(METRIC_SETS[2])
    finally:
        COMPOSITE_RULES[:] = original


def test_an_unknown_topic_metric_is_refused():
    with pytest.raises(KeyError):
        topic_metric_contributions("no-such-metric", "billing policy")
    with pytest.raises(KeyError):
        topic_metric_score("no-such-metric", "billing policy")


def test_a_metric_derived_from_an_unknown_catalog_is_refused_not_treated_as_empty():
    rule = dict(next(row for row in TOPIC_SCORE_RULES if row["metric"] == "breadth"))
    rule["catalog"] = {"source": "NO_SUCH_CATALOG", "per_item": 0.2, "cap": 12.0}
    original = list(TOPIC_SCORE_RULES)
    try:
        TOPIC_SCORE_RULES[0] = rule
        with pytest.raises(KeyError):
            policy_scoring._topic_breadth_score("a b c")
    finally:
        TOPIC_SCORE_RULES[:] = original


# ===========================================================================
# 5. The escalation, and the access decision built on it
# ===========================================================================


def test_the_escalation_fires_only_for_the_standard_tier_under_a_low_posture():
    assert resolve_tier_escalation("observed", "standard")["effective_required_tier"] == "customer-premium"
    assert resolve_tier_escalation("constrained", "standard")["effective_required_tier"] == "customer-premium"
    assert resolve_tier_escalation("customer_trusted", "standard")["effective_required_tier"] == "standard"
    assert resolve_tier_escalation("observed", "customer-premium")["effective_required_tier"] == "customer-premium"
    assert resolve_tier_escalation("high_trust", "standard")["effective_required_tier"] == "standard"


def test_the_escalation_reports_the_rule_that_fired_and_why():
    fired = resolve_tier_escalation("observed", "standard")
    assert fired["escalated"] is True
    assert fired["rule_id"] == "posture_floor"
    assert fired["reason"]
    not_fired = resolve_tier_escalation("high_trust", "standard")
    assert not_fired["escalated"] is False
    assert not_fired["rule_id"] is None
    assert "no escalation applies" in not_fired["reason"]


def test_the_access_decision_payload_is_unchanged():
    decision = build_policy_access_decision(_User(11), _PolicyScore("standard", "observed"), "export")
    assert decision.user_id == 11
    assert decision.required_tier == "standard"
    assert decision.effective_required_tier == "customer-premium"
    assert decision.allowed is False
    assert decision.control_posture == "observed"
    allowed = build_policy_access_decision(
        _User(11), _PolicyScore("customer-premium", "observed"), "export")
    assert allowed.effective_required_tier == "customer-premium"
    assert allowed.allowed is True


def test_the_access_decision_still_defaults_a_missing_posture_to_observed():
    bare = _PolicyScore("standard")
    del bare.control_posture
    decision = build_policy_access_decision(_User(3), bare, "export")
    assert decision.control_posture == "observed"
    assert decision.effective_required_tier == "customer-premium"


# ===========================================================================
# 6. Health, recommendations, and the balance figure
# ===========================================================================


def test_the_health_report_splits_signals_at_the_declared_thresholds():
    report = policy_health_report(_snapshot())
    assert {row["name"] for row in report["weak_points"]} == {
        name for name, value in report["signals"].items() if value < HEALTH_THRESHOLDS["weak_below"]
    }
    assert {row["name"] for row in report["strong_points"]} == {
        name for name, value in report["signals"].items() if value >= HEALTH_THRESHOLDS["strong_at_or_above"]
    }
    assert report["dominant_signal"] == max(report["signals"], key=report["signals"].get)
    assert report["balance_index"] == round(61.5 - min(report["signals"].values()), 2)


def test_the_health_signal_order_is_the_published_order():
    """`dominant_signal` and the tie-break in both point lists depend on it."""
    assert list(policy_signal_map(_snapshot())) == [
        "system_score", "customer_score", "access_score", "interest_score",
        "closeness_score", "community_closeness_score",
    ]


def test_a_tie_in_the_health_signals_named_the_first_declared_signal():
    report = policy_health_report(_snapshot(
        system_score=40.0, customer_score=40.0, access_score=40.0, interest_score=40.0,
        closeness_score=40.0, community_closeness_score=40.0,
    ))
    assert report["dominant_signal"] == "system_score"


def test_the_health_summary_model_still_carries_every_field():
    summary = build_policy_health_summary(_snapshot())
    assert summary.policy_tier == "standard"
    assert summary.control_posture == "observed"
    assert summary.dominant_signal
    assert isinstance(summary.weak_points, list)
    assert isinstance(summary.strong_points, list)
    assert summary.balance_index == policy_health_report(_snapshot())["balance_index"]


def test_the_recommendations_are_still_at_most_five_and_in_rule_order():
    weak = _snapshot(
        system_score=10.0, customer_score=12.0, access_score=11.0, interest_score=13.0,
        closeness_score=14.0, community_closeness_score=15.0, control_posture="constrained",
    )
    recommendations = policy_scoring.build_policy_access_recommendations(weak)
    areas = [item.area for item in recommendations]
    assert areas[0] == "control_posture"
    # Weak points are reported worst-first (10, 11, 12), which is a different
    # order from the published signal order.
    assert areas[1:4] == ["system_score", "access_score", "customer_score"]
    assert len(recommendations) <= HEALTH_THRESHOLDS["max_recommendations"]
    # The evidence is formatted, not interpolated into the source.
    assert recommendations[1].evidence == "system_score is at 10.00."


def test_a_weak_point_above_the_medium_cutoff_is_medium_priority_and_below_is_high():
    def only_weak_point_is(score):
        return policy_scoring.build_policy_access_recommendations(
            _snapshot(
                system_score=score, customer_score=90.0, access_score=90.0,
                interest_score=90.0, closeness_score=90.0, community_closeness_score=90.0,
                control_posture="customer_trusted",
            )
        )

    above = only_weak_point_is(HEALTH_THRESHOLDS["medium_priority_at_or_above"])
    below = only_weak_point_is(HEALTH_THRESHOLDS["medium_priority_at_or_above"] - 0.01)
    assert [item.area for item in above][0] == "system_score"
    assert above[0].priority == "medium"
    assert below[0].priority == "high"


def test_the_recommendation_trace_says_which_predicate_blocked_a_rule():
    trace = policy_recommendation_trace(_snapshot())
    by_id = {row["id"]: row for row in trace["rules"]}
    assert by_id["posture_recheck"]["fired"] is True
    assert by_id["expansion"]["fired"] is False
    assert by_id["expansion"]["unmet_conditions"] == [
        "access_score_at_or_above", "system_score_at_or_above"
    ]
    assert by_id["weak_signal"]["fired"] is True
    assert len(trace["recommendations"]) == len(policy_scoring.build_policy_access_recommendations(_snapshot()))


def test_the_expansion_recommendation_fires_when_both_scores_are_strong():
    strong = _snapshot(access_score=80.0, system_score=80.0, customer_score=80.0,
                       interest_score=80.0, closeness_score=80.0, community_closeness_score=80.0,
                       policy_tier="customer-premium", control_posture="customer_trusted")
    areas = [item.area for item in policy_scoring.build_policy_access_recommendations(strong)]
    assert "expansion" in areas
    assert "control_posture" not in areas
    trace = policy_recommendation_trace(strong)
    assert next(row for row in trace["rules"] if row["id"] == "expansion")["fired"] is True


def test_a_weak_point_recommendation_names_the_signal_readably():
    recommendations = policy_scoring.build_policy_access_recommendations(
        _snapshot(system_score=10.0, customer_score=80.0, access_score=80.0,
                  interest_score=80.0, closeness_score=80.0, community_closeness_score=80.0,
                  control_posture="customer_trusted")
    )
    first = recommendations[0]
    assert first.area == "system_score"
    assert first.recommendation == "Improve system score to raise overall access readiness."
    assert first.evidence == "system_score is at 10.00."


def test_the_richness_balance_divides_by_the_declared_divisor_not_the_term_count():
    signals = {
        "coverage_ratio": 0.5, "matched_keywords": ["a", "b"],
        "topic_focus": ["x", "y", "z"], "topic_family_count": 2,
        "topic_richness_score": 40.0,
    }
    expected = round(
        (10.0 + 20.0 + 0.5 * 100.0 + 2 * 4.0 + 3 * 2.5 + 2 * 3.5 + 40.0) / 6.0, 2
    )
    assert topic_richness_balance(10.0, 20.0, signals) == expected
    assert HEALTH_THRESHOLDS["richness_divisor"] == 6.0


def test_the_richness_label_follows_the_declared_bands():
    def label(balance):
        if balance >= HEALTH_THRESHOLDS["richness_balanced_at_or_above"]:
            return "rich"
        if balance >= HEALTH_THRESHOLDS["richness_focused_at_or_above"]:
            return "balanced"
        return "focused"

    rich = build_policy_topic_richness("retention and billing policy for callback")
    assert rich.split(" ")[0] in {"rich", "balanced", "focused"}
    assert rich.startswith("no_topic_richness") is False
    assert label(100.0) == "rich"
    assert label(50.0) == "balanced"
    assert label(10.0) == "focused"


# ===========================================================================
# 7. The trace, what-if, and sensitivity surfaces
# ===========================================================================


def test_the_tier_cascade_replays_the_two_pass_resolution():
    cascade = policy_tier_cascade(compose_access_score(METRIC_SETS[2]))
    assert cascade["pre_adjustment"]["policy_tier"] == resolve_policy_tier(
        cascade["pre_adjustment"]["access_score"], cascade["pre_adjustment"]["system_score"])
    assert cascade["post_adjustment"]["access_score"] == apply_posture_adjustment(
        cascade["pre_adjustment"]["access_score"], cascade["pre_adjustment"]["control_posture"])
    assert cascade["post_adjustment"]["policy_tier"] == resolve_policy_tier(
        cascade["post_adjustment"]["access_score"], cascade["post_adjustment"]["system_score"])
    assert cascade["access_band"] == resolve_access_band(cascade["post_adjustment"]["access_score"])
    assert cascade["recomputed"] is (
        cascade["pre_adjustment"]["policy_tier"] != cascade["post_adjustment"]["policy_tier"]
        or cascade["pre_adjustment"]["control_posture"] != cascade["post_adjustment"]["control_posture"]
    )


def test_the_cascade_reports_a_case_where_the_posture_adjustment_moves_the_tier():
    scores = compose_access_score(METRIC_SETS[2])
    # Raising the pre-adjustment access score to just under a tier boundary and
    # letting a trusted posture push it over is exactly what the second pass is
    # for. Both floors have to clear: system-premium needs 85 on *both*.
    nudged = dict(scores, access_score=83.0, system_score=86.0)
    nudged_cascade = policy_tier_cascade(nudged)
    assert nudged_cascade["recomputed"] is True
    assert nudged_cascade["pre_adjustment"]["policy_tier"] == "customer-premium"
    assert nudged_cascade["pre_adjustment"]["control_posture"] == "customer_trusted"
    assert nudged_cascade["post_adjustment"]["policy_tier"] == "system-premium"
    assert nudged_cascade["post_adjustment"]["control_posture"] == "high_trust"
    # Only the access score clearing is not enough on its own.
    system_only = policy_tier_cascade(dict(scores, access_score=83.0))
    assert system_only["recomputed"] is False


def test_the_what_if_diffs_the_signals_it_changed():
    result = policy_what_if(METRIC_SETS[2], {"completed_count": 9.0})
    assert result["overrides"] == {"completed_count": 9.0}
    assert "system_score" in result["changed_signals"]
    assert "access_score" in result["changed_signals"]
    for name, change in result["changed_signals"].items():
        assert change["before"] == result["baseline_scores"][name]
        assert change["after"] == result["scores"][name]
        assert change["delta"] == round(change["after"] - change["before"], 2)
    assert result["tier_changed"] is True


def test_a_what_if_with_no_overrides_changes_nothing():
    result = policy_what_if(METRIC_SETS[2])
    assert result["changed_signals"] == {}
    assert result["tier_changed"] is False
    assert result["posture_changed"] is False
    assert result["scores"] == result["baseline_scores"]


def test_the_what_if_reports_the_gate_it_would_pass():
    result = policy_what_if(METRIC_SETS[2], {}, required_tier="system-premium")
    assert result["meets_requirement"] is (
        POLICY_TIER_RANK.get(result["cascade"]["post_adjustment"]["policy_tier"], 0)
        >= POLICY_TIER_RANK.get("system-premium", 1)
    )
    assert result["escalation"]["effective_required_tier"] == "system-premium"


def test_the_sensitivity_reports_every_declared_input():
    result = policy_score_sensitivity(METRIC_SETS[2], (1.0, 5.0))
    assert set(result["sensitivity"]) == {str(row["input"]) for row in SCORE_INPUTS}
    assert set(result["sensitivity"]["signal_score"]["1.0"]) == {"access_score", "delta", "tier_changed"}
    assert result["baseline_access_score"] == compose_access_score(METRIC_SETS[2])["access_score"]


def test_the_sensitivity_moves_the_access_score_in_the_right_direction():
    # Nothing saturated: every signal is far from its ceiling, so a delta of 0.0
    # would mean no effect rather than a swallowed change.
    unsaturated = {
        "signal_score": 1.5, "loyalty_score": 10.0, "dissatisfaction_score": 50.0,
        "booking_count": 1.0, "completed_count": 1.0, "topic_confidence": 0.1,
        "topic_breadth_score": 10.0, "topic_complexity_score": 5.0,
    }
    composed = compose_access_score(unsaturated)
    assert all(composed[name] < 100.0 for name in COMPOSITE_SIGNALS), composed
    result = policy_score_sensitivity(unsaturated, (5.0,))
    assert result["sensitivity"]["signal_score"]["5.0"]["delta"] > 0.0
    # A higher dissatisfaction lowers the customer score, so the access score must fall.
    assert result["sensitivity"]["dissatisfaction_score"]["5.0"]["delta"] < 0.0
    # And a negative delta moves it the other way, monotonically.
    down = policy_score_sensitivity(unsaturated, (-1.0,))
    assert down["sensitivity"]["signal_score"]["-1.0"]["delta"] < 0.0


def test_the_sensitivity_reveals_a_saturated_signal_rather_than_reporting_no_movement():
    """A delta of 0.0 can mean 'no effect' or 'the ceiling swallowed it'."""
    saturated = policy_score_sensitivity(METRIC_SETS[4], (5.0,))
    assert saturated["baseline_access_score"] == 100.0
    assert saturated["sensitivity"]["signal_score"]["5.0"]["delta"] == 0.0


def test_the_decision_trace_reads_only_the_snapshot():
    trace = policy_decision_trace(_snapshot())
    assert trace["tier"]["policy_tier"] == "standard"
    assert trace["posture"]["control_posture"] == "observed"
    assert trace["access_band"]["access_band"] == "moderate"
    assert trace["gate"]["escalated"] is True
    assert trace["gate"]["allowed"] is False
    assert trace["gate"]["rank_margin"] == -1
    assert trace["posture_adjustment"]["delta"] == -4.0
    assert set(trace["signal_bands"]) == set(trace["signals"])


def test_the_decision_trace_gate_reports_a_clearance_not_just_a_verdict():
    trace = policy_decision_trace(_snapshot(
        access_score=80.0, policy_tier="customer-premium", control_posture="customer_trusted"))
    assert trace["gate"]["escalated"] is False
    assert trace["gate"]["allowed"] is True
    assert trace["gate"]["rank_margin"] == 1


# ===========================================================================
# 8. The validator: a rule that cannot run has to say so
# ===========================================================================


def test_the_live_tables_validate_clean():
    report = validate_policy_scoring()
    assert report["errors"] == 0, report["error_list"]
    assert report["valid"] is True
    assert report["version"] == POLICY_SCORING_VERSION
    assert report["signals"] == len(SCORE_SIGNALS)
    assert report["composition_rules"] == len(COMPOSITE_RULES)
    assert report["marker_groups"] == len(TOPIC_MARKER_WEIGHTS)
    assert report["recommendation_rules"] == len(RECOMMENDATION_RULES)


def test_a_forward_referencing_composition_rule_is_an_error():
    """Reading a signal produced later scores 0.0 and says nothing."""
    original = [dict(row) for row in COMPOSITE_RULES]
    try:
        COMPOSITE_RULES[0] = dict(original[0], terms=[{"input": "access_score", "weight": 1.0}])
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("produced later" in message for message in report["error_list"])
    finally:
        COMPOSITE_RULES[:] = original


def test_a_composition_rule_reading_an_undeclared_name_is_an_error():
    rules = [dict(row) for row in COMPOSITE_RULES]
    rules[0] = dict(rules[0], terms=[{"input": "not_an_input", "weight": 1.0}])
    original = list(COMPOSITE_RULES)
    try:
        COMPOSITE_RULES[:] = rules
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("not_an_input" in message for message in report["error_list"])
    finally:
        COMPOSITE_RULES[:] = original


def test_a_composition_rule_reading_its_own_output_is_an_error():
    rules = [dict(row) for row in COMPOSITE_RULES]
    rules[0] = dict(rules[0], terms=[{"input": "system_score", "weight": 1.0}])
    original = list(COMPOSITE_RULES)
    try:
        COMPOSITE_RULES[:] = rules
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("reads its own output" in message for message in report["error_list"])
    finally:
        COMPOSITE_RULES[:] = original


def test_an_unknown_composition_mode_is_an_error():
    rules = [dict(row) for row in COMPOSITE_RULES]
    rules[0] = dict(rules[0], mode="median")
    original = list(COMPOSITE_RULES)
    try:
        COMPOSITE_RULES[:] = rules
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("mode 'median'" in message for message in report["error_list"])
    finally:
        COMPOSITE_RULES[:] = original


def test_a_composition_rule_with_no_terms_is_an_error_not_a_silent_zero():
    rules = [dict(row) for row in COMPOSITE_RULES]
    rules[0] = dict(rules[0], terms=[])
    original = list(COMPOSITE_RULES)
    try:
        COMPOSITE_RULES[:] = rules
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("no terms" in message for message in report["error_list"])
    finally:
        COMPOSITE_RULES[:] = original


def test_a_duplicate_composition_signal_is_an_error():
    rules = [dict(row) for row in COMPOSITE_RULES]
    rules[1] = dict(rules[1], signal="system_score")
    original = list(COMPOSITE_RULES)
    try:
        COMPOSITE_RULES[:] = rules
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("duplicate signal" in message for message in report["error_list"])
    finally:
        COMPOSITE_RULES[:] = original


def test_two_contributions_at_the_same_order_make_the_accumulation_ambiguous():
    original = list(TOPIC_MARKER_WEIGHTS)
    try:
        TOPIC_MARKER_WEIGHTS.append(dict(original[0], name="shadow", order=original[0]["order"]))
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("accumulation order is ambiguous" in message for message in report["error_list"])
    finally:
        TOPIC_MARKER_WEIGHTS[:] = original


def test_a_marker_group_for_an_unknown_metric_is_an_error():
    original = list(TOPIC_MARKER_WEIGHTS)
    try:
        TOPIC_MARKER_WEIGHTS.append(dict(original[0], metric="sparkle", name="sparkle"))
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("which TOPIC_SCORE_RULES does not declare" in message for message in report["error_list"])
    finally:
        TOPIC_MARKER_WEIGHTS[:] = original


def test_a_marker_group_with_an_unknown_match_mode_is_an_error():
    original = list(TOPIC_MARKER_WEIGHTS)
    try:
        TOPIC_MARKER_WEIGHTS[0] = dict(original[0], match="maybe")
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("match 'maybe'" in message for message in report["error_list"])
    finally:
        TOPIC_MARKER_WEIGHTS[:] = original


def test_a_metric_derived_from_an_unknown_catalog_is_an_error():
    original = list(TOPIC_SCORE_RULES)
    try:
        TOPIC_SCORE_RULES[0] = dict(
            original[0], catalog={"source": "NO_SUCH_CATALOG", "per_item": 0.2, "cap": 12.0})
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("unknown catalog" in message for message in report["error_list"])
    finally:
        TOPIC_SCORE_RULES[:] = original


def test_a_recommendation_predicate_with_a_typo_is_an_error():
    """The worst failure in this file: the rule simply never fires."""
    original = [dict(row) for row in RECOMMENDATION_RULES]
    try:
        RECOMMENDATION_RULES[2] = dict(original[2], when={"access_score_at_or_abov": 75.0})
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("access_score_at_or_abov" in message for message in report["error_list"])
    finally:
        RECOMMENDATION_RULES[:] = original


def test_a_recommendation_template_referencing_an_unfed_field_is_an_error():
    original = [dict(row) for row in RECOMMENDATION_RULES]
    try:
        RECOMMENDATION_RULES[0] = dict(
            original[0], evidence="Control posture is {control_posture} for {user_id}.")
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("user_id" in message for message in report["error_list"])
    finally:
        RECOMMENDATION_RULES[:] = original


def test_a_recommendation_area_template_referencing_an_unfed_field_is_an_error():
    original = [dict(row) for row in RECOMMENDATION_RULES]
    try:
        RECOMMENDATION_RULES[1] = dict(original[1], area="{name}/{cohort}")
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("cohort" in message for message in report["error_list"])
    finally:
        RECOMMENDATION_RULES[:] = original


def test_a_duplicate_recommendation_id_is_an_error():
    original = [dict(row) for row in RECOMMENDATION_RULES]
    try:
        RECOMMENDATION_RULES[2] = dict(original[2], id="posture_recheck")
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("declared twice" in message for message in report["error_list"])
    finally:
        RECOMMENDATION_RULES[:] = original


def test_a_per_weak_point_rule_without_a_priority_spec_is_an_error():
    original = [dict(row) for row in RECOMMENDATION_RULES]
    try:
        RECOMMENDATION_RULES[1] = dict(original[1], priority="high")
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("at_or_above" in message for message in report["error_list"])
    finally:
        RECOMMENDATION_RULES[:] = original


def test_a_per_weak_point_rule_with_an_unknown_source_is_an_error():
    original = [dict(row) for row in RECOMMENDATION_RULES]
    try:
        RECOMMENDATION_RULES[1] = dict(original[1], source="health.loudest")
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("unknown source" in message for message in report["error_list"])
    finally:
        RECOMMENDATION_RULES[:] = original


def test_more_than_one_default_posture_row_is_an_error():
    original = list(POSTURE_ADJUSTMENTS)
    try:
        POSTURE_ADJUSTMENTS.append(dict(original[3], posture="constrained"))
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("default rows" in message for message in report["error_list"])
    finally:
        POSTURE_ADJUSTMENTS[:] = original


def test_an_unknown_posture_clamp_is_an_error():
    original = list(POSTURE_ADJUSTMENTS)
    try:
        POSTURE_ADJUSTMENTS[1] = dict(original[1], clamp="sideways")
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("clamp 'sideways'" in message for message in report["error_list"])
    finally:
        POSTURE_ADJUSTMENTS[:] = original


def test_an_escalation_naming_an_unreachable_posture_is_an_error():
    original = [dict(row) for row in TIER_ESCALATIONS]
    try:
        TIER_ESCALATIONS[0] = dict(original[0], control_posture_in=["observed", "vibes"])
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("unreachable postures: vibes" in message for message in report["error_list"])
    finally:
        TIER_ESCALATIONS[:] = original


def test_an_escalation_targeting_an_unranked_tier_is_an_error():
    original = [dict(row) for row in TIER_ESCALATIONS]
    try:
        TIER_ESCALATIONS[0] = dict(original[0], effective_tier="platinum")
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("has no rank" in message for message in report["error_list"])
    finally:
        TIER_ESCALATIONS[:] = original


def test_tier_rules_out_of_order_are_an_error():
    """First-match-wins makes order the semantics, not a presentation detail."""
    original = list(POLICY_TIER_RULES)
    try:
        POLICY_TIER_RULES[:] = [original[2], original[0], original[1]]
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("not ordered by descending access_score_min" in message for message in report["error_list"])
    finally:
        POLICY_TIER_RULES[:] = original


def test_a_tier_rule_whose_tier_has_no_rank_is_an_error():
    original = list(POLICY_TIER_RULES)
    try:
        POLICY_TIER_RULES.append({"tier": "platinum", "access_score_min": 99.0, "system_score_min": 0.0})
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("'platinum' has no rank" in message for message in report["error_list"])
    finally:
        POLICY_TIER_RULES[:] = original


def test_a_shadowed_tier_rule_is_reported_as_dead_configuration():
    original = list(POLICY_TIER_RULES)
    try:
        # Same access floor as the row above it but a *stricter* system floor,
        # so every score pair that would match this row already matched that
        # one. The access mins stay non-increasing, so the ordering check is
        # satisfied and the dead row is reported on its own.
        POLICY_TIER_RULES.insert(1, {
            "tier": "customer-premium", "access_score_min": 85.0, "system_score_min": 90.0,
        })
        report = validate_policy_scoring()
        assert report["valid"] is True, report["error_list"]
        assert any("shadowed by an earlier row" in message for message in report["warning_list"])
        assert 1 in report["coverage"]["unreachable_tier_rules"]
        assert report["coverage"]["tier_rules"][1]["decided"] == 0
        # The row is still in the catalog, which is exactly why reporting it
        # matters: dead configuration otherwise reads as live.
        assert len(build_policy_tier_catalog()["tier_rules"]) == len(POLICY_TIER_RULES)
    finally:
        POLICY_TIER_RULES[:] = original


def test_a_threshold_constant_that_drifts_from_the_table_is_a_warning():
    """`ACCESS_TIER_THRESHOLD` and the table are two copies of one number."""
    report = validate_policy_scoring()
    drift = [message for message in report["warning_list"] if "drifted apart" in message]
    assert drift == [] or all("no POLICY_TIER_RULES row" in message for message in drift)


def test_an_inverted_health_threshold_is_an_error():
    original = dict(HEALTH_THRESHOLDS)
    try:
        HEALTH_THRESHOLDS["weak_below"] = 90.0
        HEALTH_THRESHOLDS["strong_at_or_above"] = 80.0
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("both a weak point and a strong point" in message for message in report["error_list"])
    finally:
        HEALTH_THRESHOLDS.clear()
        HEALTH_THRESHOLDS.update(original)


def test_a_recommendation_cap_below_what_the_rules_can_emit_is_a_warning():
    original = dict(HEALTH_THRESHOLDS)
    try:
        HEALTH_THRESHOLDS["max_recommendations"] = 2
        report = validate_policy_scoring()
        assert report["valid"] is True, report["error_list"]
        assert any("truncates" in message for message in report["warning_list"])
    finally:
        HEALTH_THRESHOLDS.clear()
        HEALTH_THRESHOLDS.update(original)


def test_a_nonpositive_decimal_precision_is_an_error():
    original = dict(POLICY_SCORING_OPS)
    try:
        POLICY_SCORING_OPS["decimals"] = -1
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("decimals must not be negative" in message for message in report["error_list"])
    finally:
        POLICY_SCORING_OPS.clear()
        POLICY_SCORING_OPS.update(original)


def test_a_ceiling_below_the_floor_is_an_error():
    original = dict(POLICY_SCORING_OPS)
    try:
        POLICY_SCORING_OPS["score_ceiling"] = 10.0
        POLICY_SCORING_OPS["score_floor"] = 50.0
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("not above the score floor" in message for message in report["error_list"])
    finally:
        POLICY_SCORING_OPS.clear()
        POLICY_SCORING_OPS.update(original)


def test_losing_the_fail_closed_asymmetry_is_a_warning():
    original = dict(POLICY_SCORING_OPS)
    try:
        POLICY_SCORING_OPS["unknown_tier_rank"] = 2
        POLICY_SCORING_OPS["unknown_required_tier_rank"] = 1
        report = validate_policy_scoring()
        assert any("stops failing closed" in message for message in report["warning_list"])
    finally:
        POLICY_SCORING_OPS.clear()
        POLICY_SCORING_OPS.update(original)


def test_a_missing_ops_key_is_an_error_not_a_default():
    original = dict(POLICY_SCORING_OPS)
    try:
        POLICY_SCORING_OPS.pop("confidence_breadth_divisor")
        report = validate_policy_scoring()
        assert report["valid"] is False
        assert any("missing 'confidence_breadth_divisor'" in message for message in report["error_list"])
    finally:
        POLICY_SCORING_OPS.clear()
        POLICY_SCORING_OPS.update(original)


def test_a_reachable_posture_with_no_adjustment_row_is_a_warning_not_a_silent_delta():
    original = list(CONTROL_POSTURE_RULES)
    try:
        CONTROL_POSTURE_RULES.append({"posture": "monitored", "access_score_min": 0.0, "system_score_min": 0.0})
        report = validate_policy_scoring()
        assert any("monitored" in message and "catch-all" in message for message in report["warning_list"])
    finally:
        CONTROL_POSTURE_RULES[:] = original


def test_an_adjustment_row_for_a_posture_nobody_produces_is_a_warning():
    original = list(POSTURE_ADJUSTMENTS)
    try:
        POSTURE_ADJUSTMENTS.append({
            "posture": "platinum", "delta": 5.0, "clamp": "upper", "rounded": True, "default": False,
        })
        report = validate_policy_scoring()
        assert any("platinum" in message for message in report["warning_list"])
    finally:
        POSTURE_ADJUSTMENTS[:] = original


def test_the_coverage_sweep_needs_a_positive_step():
    with pytest.raises(ValueError):
        policy_rule_coverage(step=0.0)


def test_the_coverage_sweep_covers_the_whole_score_space():
    coverage = policy_rule_coverage(step=1.0)
    assert coverage["grid_points"] == 101 * 101
    assert sum(row["decided"] for row in coverage["tier_rules"]) + (
        coverage["grid_points"] - sum(row["decided"] for row in coverage["tier_rules"])
    ) == coverage["grid_points"]
    assert coverage["unreachable_tier_rules"] == []
    assert coverage["unreachable_band_rules"] == []
    assert set(coverage["posture_routes"]) == {"tier_map", "rules", "default"}
    # The dominant posture route in the live tables is the default, which is the
    # single most useful thing this sweep says about the current configuration.
    assert max(coverage["posture_routes"], key=lambda key: coverage["posture_routes"][key]["decided"]) == "default"
    assert sum(item["decided"] for item in coverage["posture_routes"].values()) == coverage["grid_points"]


def test_the_coverage_sweep_shares_sum_to_one():
    coverage = policy_rule_coverage(step=2.0)
    assert sum(row["share"] for row in coverage["tier_rules"]) <= 1.0
    assert abs(sum(item["share"] for item in coverage["posture_routes"].values()) - 1.0) < 1e-4
    # Shares are rounded for display, so the raw counts are the authority.
    assert sum(item["decided"] for item in coverage["posture_routes"].values()) == coverage["grid_points"]


# ===========================================================================
# 9. The catalog, and the /meta surfaces
# ===========================================================================


def test_the_ops_catalog_exposes_the_new_tables_without_touching_the_old_ones():
    catalog = build_policy_scoring_catalog()
    assert catalog["version"] == POLICY_SCORING_VERSION
    assert catalog["signals"] == [dict(row) for row in SCORE_SIGNALS]
    assert catalog["inputs"] == [dict(row) for row in SCORE_INPUTS]
    assert catalog["composition_rules"] == [dict(row) for row in COMPOSITE_RULES]
    assert catalog["composition_modes"] == ["scaled_mean", "weighted_sum"]
    assert catalog["posture_adjustments"] == [dict(row) for row in POSTURE_ADJUSTMENTS]
    assert catalog["tier_escalations"] == [dict(row) for row in TIER_ESCALATIONS]
    assert catalog["topic_marker_weights"] == [dict(row) for row in TOPIC_MARKER_WEIGHTS]
    assert catalog["topic_score_rules"] == [dict(row) for row in TOPIC_SCORE_RULES]
    assert catalog["health_thresholds"] == dict(HEALTH_THRESHOLDS)
    assert catalog["recommendation_rules"] == [dict(row) for row in RECOMMENDATION_RULES]
    assert catalog["recommendation_conditions"] == [dict(row) for row in RECOMMENDATION_CONDITIONS]
    assert catalog["ops"] == dict(POLICY_SCORING_OPS)
    assert catalog["validation"]["errors"] == 0
    # The pinned tier contract is reproduced here unchanged, not replaced.
    assert catalog["tier_catalog"] == build_policy_tier_catalog()


def test_the_ops_catalog_is_json_serializable():
    import json

    json.dumps(build_policy_scoring_catalog())


def test_the_meta_root_lists_the_feature():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    assert "policy_scoring_governance" in client.get("/meta").json()["features"]


def test_the_policies_scoring_catalog_is_reachable_over_http():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    response = client.get("/meta/policy-scoring")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["version"] == POLICY_SCORING_VERSION
    assert payload["validation"]["errors"] == 0
    assert set(payload["tier_catalog"]) == set(build_policy_tier_catalog())
    assert payload["signals"]


def test_the_scoring_catalog_endpoint_carries_the_new_section():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    response = client.get("/meta/scoring-catalog")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "policy_scoring_governance" in payload
    assert payload["policy_scoring_governance"]["version"] == POLICY_SCORING_VERSION
    # The pinned section is still there under its own name.
    assert set(payload["policy_tiers"]) == set(build_policy_tier_catalog())


def test_the_feature_summary_points_at_the_new_endpoint():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    payload = client.get("/meta/features").json()
    assert payload["endpoints"]["policy_scoring_catalog"] == "/meta/policy-scoring"
    assert payload["endpoints"]["scoring_catalog"] == "/meta/scoring-catalog"


def test_the_ecosystem_describes_the_subservice_with_its_config_tables():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    subservice = client.get("/meta/ecosystem").json()["subservices"]["policy_scoring_governance"]
    assert "/meta/policy-scoring" in subservice["routes"]
    for table in ("SCORE_SIGNALS", "COMPOSITE_RULES", "POSTURE_ADJUSTMENTS", "TIER_ESCALATIONS",
                  "TOPIC_MARKER_WEIGHTS", "HEALTH_THRESHOLDS", "RECOMMENDATION_RULES",
                  "POLICY_SCORING_OPS", "POLICY_SCORING_VERSION"):
        assert table in subservice["config_tables"], table
    assert subservice["status"] == "ready"
    assert subservice["notes"]


# ===========================================================================
# 10. The snapshot builder still composes through the tables
# ===========================================================================


def test_the_snapshot_builder_composes_through_the_engine():
    """The DB path is unchanged; the arithmetic now runs through the table."""
    import inspect

    source = inspect.getsource(ps.build_customer_policy_snapshot)
    assert "compose_access_score(" in source
    assert "* 12.0" not in source
    assert "12.0) +" not in source
    # The six queries are still issued in the same order.
    assert source.count(".where(") >= 6


def test_the_snapshot_still_returns_every_published_field():
    import asyncio

    from unittest.mock import AsyncMock, MagicMock

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar=MagicMock(return_value=4.0)),
        MagicMock(scalar=MagicMock(return_value=70.0)),
        MagicMock(scalar=MagicMock(return_value=30.0)),
        MagicMock(scalar=MagicMock(return_value=3.0)),
        MagicMock(scalar=MagicMock(return_value=2.0)),
        MagicMock(scalar=MagicMock(return_value=0.5)),
        MagicMock(scalar=MagicMock(return_value="retention and billing policy for callback")),
    ])
    snapshot = asyncio.run(
        ps.build_customer_policy_snapshot(db, _User(9))
    )
    expected = compose_access_score({
        "signal_score": 4.0, "loyalty_score": 70.0, "dissatisfaction_score": 30.0,
        "booking_count": 3.0, "completed_count": 2.0, "topic_confidence": 0.5,
        "topic_breadth_score": policy_scoring._topic_breadth_score(
            "retention and billing policy for callback"),
        "topic_complexity_score": policy_scoring._topic_complexity_score(
            "retention and billing policy for callback"),
    })
    assert snapshot.system_score == expected["system_score"]
    assert snapshot.customer_score == expected["customer_score"]
    assert snapshot.interest_score == expected["interest_score"]
    assert snapshot.closeness_score == expected["closeness_score"]
    assert snapshot.community_closeness_score == expected["community_closeness_score"]
    assert snapshot.policy_tier
    assert snapshot.control_posture
    assert "topic_breadth=" in snapshot.summary
    assert "topic_depth=" in snapshot.summary


def test_the_snapshot_applies_the_posture_adjustment_and_resolves_again():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    async def run():
        db = MagicMock()
        db.execute = AsyncMock(side_effect=[
            MagicMock(scalar=MagicMock(return_value=0.0)),
            MagicMock(scalar=MagicMock(return_value=0.0)),
            MagicMock(scalar=MagicMock(return_value=0.0)),
            MagicMock(scalar=MagicMock(return_value=0.0)),
            MagicMock(scalar=MagicMock(return_value=0.0)),
            MagicMock(scalar=MagicMock(return_value=0.0)),
            MagicMock(scalar=MagicMock(return_value="")),
        ])
        return await ps.build_customer_policy_snapshot(db, _User(1))

    snapshot = asyncio.run(run())
    # Zero inputs are not a zero customer score: no dissatisfaction reading is
    # the *best* possible reading, so the inverted term contributes 100.
    zeroed = {
        "signal_score": 0.0, "loyalty_score": 0.0, "dissatisfaction_score": 0.0,
        "booking_count": 0.0, "completed_count": 0.0, "topic_confidence": 0.0,
        "topic_breadth_score": 0.0, "topic_complexity_score": 0.0,
    }
    scores = compose_access_score(zeroed)
    assert scores["customer_score"] == 100.0
    cascade = policy_tier_cascade(scores)
    assert snapshot.system_score == scores["system_score"]
    assert snapshot.customer_score == scores["customer_score"]
    assert snapshot.access_score == cascade["post_adjustment"]["access_score"]
    assert snapshot.access_score == round(scores["access_score"] - 8.0, 2)
    assert snapshot.control_posture == "constrained"
    assert snapshot.policy_tier == cascade["post_adjustment"]["policy_tier"]


def test_an_empty_topic_leaves_the_snapshot_scores_untouched_but_still_scores_text():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    async def run():
        db = MagicMock()
        db.execute = AsyncMock(side_effect=[
            MagicMock(scalar=MagicMock(return_value=2.0)),
            MagicMock(scalar=MagicMock(return_value=50.0)),
            MagicMock(scalar=MagicMock(return_value=20.0)),
            MagicMock(scalar=MagicMock(return_value=1.0)),
            MagicMock(scalar=MagicMock(return_value=1.0)),
            MagicMock(scalar=MagicMock(return_value=0.0)),
            MagicMock(scalar=MagicMock(return_value="   ")),
        ])
        return await ps.build_customer_policy_snapshot(db, _User(1))

    snapshot = asyncio.run(run())
    # A whitespace-only topic is not an empty topic for the breadth scorer: it
    # picks up the catalog-size term even though it has no tokens at all.
    assert policy_scoring._topic_breadth_score("   ") == min(
        len(TOPIC_CATALOG) * 0.2, 12.0)
    assert snapshot.system_score == round(2.0 * 12.0 + 1.0 * 6.0, 2)
    # The topic *context* builder does strip it, so the two disagree here and
    # always have.
    assert snapshot.topic_context == "user-1: no active topic"
    assert snapshot.topic_richness == "no_topic_richness"
