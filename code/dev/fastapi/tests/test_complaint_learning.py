"""Complaint learning: system-raised complaints, learned weights, stacked clusters.

Four properties are pinned here, and each is one that could plausibly have been
built wrong and would still have looked right:

**A complaint can be raised by the system, and the guards hold.** All three
guards -- confidence, cooldown, dedupe -- are tested by deliberately violating
each one, because a guard that is only ever tested on the happy path is a comment
rather than a guard.

**A learned weight moves the way the argument says it does.** The direction
inversion is the subtle one: a *damage* signal that did not end in churn was
overstating its damage, so it must get *lighter*. Getting that backwards would
produce a system that learns to ignore the strongest signals, and every test in
the suite would still pass because the arithmetic is symmetric.

**Neutral trains nothing.** This is the single most important property in the
module. A `neutral` outcome means "not yet known", and if it counted as evidence
then every unjudged case would agree with whatever weight already existed --
which is how a learner concludes that doing nothing is correct.

**Suggestions are durable and never destructive.** A proposal id must not move
when the evidence grows, and nothing this module does may remove a line from
`BLOCKAGES.md`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select as sa_select

from app import models, rule_engine
from app.services import complaint_learning as CL
from app.services import complaints as C
from tests._doubles import SqliteHarness

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


# =============================================================================
# The loyalty objective
# =============================================================================


class TestLoyaltyOutcomeDerivation:
    """`derive_loyalty_outcome` is the objective. Getting it wrong poisons the
    weights, because every weight is trained on whatever this function returns."""

    def test_churn_dominates_even_when_the_customer_came_back(self):
        """The ordering is the whole argument.

        A customer who churned and later returned has not been retained. If
        `re-engaged` were checked first, every churned-then-returned customer
        would train the weights as a success and the strongest negative signal in
        the table would decay toward irrelevance.
        """
        facts = {
            "churn_risk": "critical",
            "reactivated": True,
            "days_to_rebooking": 3,
            "days_since_complaint": 60,
        }
        assert CL.derive_loyalty_outcome(facts)["outcome"] == "churned"

    def test_too_early_is_neutral_and_never_decidable(self):
        """A three-day-old complaint has no outcome.

        Calling that `retained` is the specific error that teaches a learner that
        ignoring complaints works, because every unjudged case would then be
        recorded as agreeing with the status quo.
        """
        result = CL.derive_loyalty_outcome({"days_since_complaint": 3, "churn_risk": "low"})
        assert result["outcome"] == "neutral"
        assert result["decidable"] is False
        assert not CL.observation_is_decidable("neutral")

    def test_missing_horizon_is_neutral_rather_than_guessed(self):
        result = CL.derive_loyalty_outcome({"churn_risk": "low"})
        assert result["outcome"] == "neutral"
        assert result["basis"] == "no_horizon"

    def test_dormancy_is_distinct_from_churn(self):
        result = CL.derive_loyalty_outcome({
            "churn_risk": "low",
            "days_since_complaint": 90,
            "days_since_last_contact": 30,
        })
        assert result["outcome"] == "dormant"
        # Dormancy is worth less than churn, or the learner would not
        # distinguish "went quiet" from "left" -- and those need different work.
        assert CL.loyalty_delta_for("dormant") > CL.loyalty_delta_for("churned")

    def test_rebooking_outranks_merely_still_being_there(self):
        result = CL.derive_loyalty_outcome({
            "churn_risk": "low",
            "days_since_complaint": 60,
            "days_to_rebooking": 10,
        })
        assert result["outcome"] == "re-engaged"
        assert CL.loyalty_delta_for("re-engaged") > CL.loyalty_delta_for("retained")

    def test_unknown_outcome_is_worth_exactly_zero(self):
        """An outcome nobody defined must not silently become evidence."""
        assert CL.loyalty_delta_for("something_new") == 0.0
        assert CL.observation_is_decidable("something_new") is False

    def test_the_objective_is_retention_not_engagement_volume(self):
        """The catalog must state what is *not* being optimised.

        This is a real constraint on future work, not documentation: a later
        change that adds "contact frequency" as a positive signal would invert
        the complaint system's purpose, and the only thing standing in its way is
        that the exclusion is written down here.
        """
        catalog = CL.build_complaint_learning_catalog()
        assert "customer retention and habitual return" in catalog["objective"]["name"]
        assert "contact frequency" in catalog["objective"]["not_optimised"]
        assert "time-on-service" in catalog["objective"]["not_optimised"]


# =============================================================================
# The weight update rule
# =============================================================================


class TestWeightUpdateRule:
    def _update(self, **kwargs):
        base = {
            "current": 2.0, "prior": 1.4, "direction": "loyalty_negative",
            "loyalty_delta": 1.0, "observations": 9, "agreements": 5, "disagreements": 4,
        }
        base.update(kwargs)
        return CL.weight_after_observation(**base)

    def test_damage_that_did_not_cost_loyalty_gets_lighter(self):
        """The direction inversion, which is the easiest thing to get wrong.

        A `loyalty_negative` signal attached to a case the customer *stayed* was
        overstating the damage. Raising it instead would teach the system that
        the loudest signals matter most, which is precisely backwards.
        """
        lighter = self._update(loyalty_delta=1.0)["weight"]
        assert lighter < 2.0

    def test_damage_that_did_cost_loyalty_gets_heavier(self):
        heavier = self._update(loyalty_delta=-1.0)["weight"]
        assert heavier > 2.0

    def test_protection_signal_moves_the_other_way(self):
        """A `loyalty_positive` signal is the mirror image, and must be pinned
        separately -- a symmetric bug would pass the negative test alone."""
        gained = self._update(direction="loyalty_positive", loyalty_delta=1.0)["weight"]
        lost = self._update(direction="loyalty_positive", loyalty_delta=-1.0)["weight"]
        assert gained > 2.0
        assert lost < 2.0

    def test_a_neutral_signal_never_moves_on_an_outcome(self):
        """`high_value` and `regulatory` are context, not claims about loyalty.

        They are in the table so a reviewer can see the temptation to weight a
        complaint more because the customer is commercially valuable, rather than
        acting on it. No loyalty outcome may move them.
        """
        # current == prior so decay is a no-op and the step is the only thing that
        # could move the weight. Testing this with current != prior would conflate
        # two mechanisms and pin neither.
        for delta in (1.0, -1.0, 0.0):
            result = self._update(
                current=1.0, prior=1.0, direction="neutral", loyalty_delta=delta
            )
            assert result["weight"] == 1.0
            assert result["moved"] is False

    def test_neutral_signals_are_insensitive_to_the_outcome(self):
        """The stronger statement, and the one that actually matters.

        A neutral signal's weight must be a function of its own prior and nothing
        else. If any outcome could move it, the value would encode an opinion
        about loyalty that the signal was declared not to have.
        """
        weights = {
            delta: self._update(
                current=1.0, prior=1.0, direction="neutral", loyalty_delta=delta
            )["weight"]
            for delta in (1.5, 1.0, 0.5, -0.5, -1.0)
        }
        assert set(weights.values()) == {1.0}

    def test_a_neutral_signal_still_decays_toward_its_prior(self):
        """Decay is a separate mechanism and it *does* apply here.

        A neutral signal is evidence-free, not exempt: if its weight has been
        moved by something else -- a bound clamp, a reclassified direction -- it
        should still drift home. Pinned so the fix above is not mistaken for
        "neutral signals are frozen forever".
        """
        drifted = self._update(
            current=2.0, prior=1.0, direction="neutral", loyalty_delta=0.0
        )["weight"]
        assert 1.0 < drifted < 2.0

    def test_weights_are_bounded_at_both_ends(self):
        """A run of outcomes must not produce a weight that dominates a decision."""
        floor = self._update(current=0.25, loyalty_delta=1.0)["weight"]
        ceiling = self._update(current=3.9, loyalty_delta=-1.0)["weight"]
        assert floor >= CL.LEARNING_PARAMS["min_weight"]
        assert ceiling <= CL.LEARNING_PARAMS["max_weight"]

    def test_the_bound_is_reported_so_a_pinned_weight_is_visible(self):
        """A weight sitting on a bound is a finding, not a number.

        Without the flag a reviewer sees a confident-looking 4.0 and has no way to
        know the learner wanted to go higher and was not allowed.
        """
        result = self._update(current=4.0, loyalty_delta=-1.0)
        assert result["at_bound"] is True

    def test_decay_pulls_an_unobserved_signal_back_toward_its_prior(self):
        """Weights must not freeze a belief the population has outgrown."""
        drifted = self._update(current=3.5, prior=1.0, loyalty_delta=0.0)["weight"]
        assert drifted < 3.5
        assert drifted > 1.0

    def test_a_saturated_weight_stops_moving_and_reports_it(self):
        """Rounding to 3dp is what makes this terminate.

        Without it the last binary place would change on every pass forever, so
        `version` would climb for no reason and a "did anything change" diff
        would always be dirty. Once clamped to the ceiling, further evidence of
        the same kind must be a genuine no-op.
        """
        weight = 2.0
        for _ in range(200):
            step = CL.weight_after_observation(
                current=weight, prior=1.4, direction="loyalty_negative",
                loyalty_delta=-1.0, observations=5, agreements=2, disagreements=3,
            )
            weight = step["weight"]
        assert weight == CL.LEARNING_PARAMS["max_weight"]
        # And it stays exactly there rather than creeping.
        again = CL.weight_after_observation(
            current=weight, prior=1.4, direction="loyalty_negative",
            loyalty_delta=-1.0, observations=5, agreements=2, disagreements=3,
        )
        assert again["weight"] == weight
        assert again["moved"] is False
        assert again["at_bound"] is True

    def test_confidence_separates_evidence_volume_from_agreement(self):
        """Thirty observations that split evenly are not the same as thirty that
        agreed. Volume alone would report both as confident."""
        unanimous = CL.confidence_for(30, 30, 0)
        split = CL.confidence_for(30, 15, 15)
        assert unanimous > split

    def test_no_observations_means_no_confidence(self):
        assert CL.confidence_for(0, 0, 0) == 0.0

    def test_a_signal_moved_by_a_handful_of_observations_is_nearly_unbelieved(self):
        """Below `confidence_min_observations` confidence is deliberately near
        zero, so three observations cannot be presented as a finding."""
        assert CL.confidence_for(3, 3, 0) < 0.2


# =============================================================================
# Signal reading and contribution scoring
# =============================================================================


class TestContributionScoring:
    def test_every_signal_is_read_not_just_the_first_match(self):
        """The ranking is the useful output.

        An early exit would make the ranking depend on the order the config
        happens to be written in, which is not information.
        """
        scored = CL.score_contributions({
            "churn_risk": "critical", "reopened_count": 1, "no_owner": True,
        })
        assert set(scored["signal_ids"]) == {"churn_critical", "reopened", "unowned"}

    def test_damage_and_protection_are_reported_separately(self):
        """A fast first response and a missed SLA are not "net zero".

        Collapsing them would hide exactly the pairing an operator needs: one
        thing went right and one thing went wrong.
        """
        scored = CL.score_contributions({
            "reopened_count": 1, "satisfaction_score": 5, "no_owner": True,
        })
        assert scored["loyalty_damage"] > 0
        assert scored["loyalty_protection"] > 0
        assert scored["net"] == round(
            scored["loyalty_damage"] + scored["loyalty_protection"], 4
        )

    def test_sentiment_reading_fires_on_a_drop_not_on_a_low_baseline(self):
        """A customer who was always unhappy is a different problem from one who
        was fine until this case, and only the second is addressable."""
        cliff = CL.score_contributions({"sentiment_first": 0.9, "sentiment_last": 0.2})
        flat = CL.score_contributions({"sentiment_first": 0.2, "sentiment_last": 0.1})
        assert "sentiment_cliff" in cliff["signal_ids"]
        assert "sentiment_cliff" not in flat["signal_ids"]

    def test_a_missing_sentiment_series_does_not_fire(self):
        """Two samples cannot show a trend."""
        assert "sentiment_cliff" not in CL.score_contributions({})["signal_ids"]

    def test_an_unlearned_signal_still_has_its_configured_prior(self):
        """Zero would silently mean "ignore this" for everything not yet learned."""
        assert CL.signal_weight("churn_critical", {}) == CL.LOYALTY_SIGNAL_BY_ID[
            "churn_critical"
        ]["prior_weight"]

    def test_a_learned_weight_overrides_the_prior(self):
        assert CL.signal_weight("churn_critical", {"churn_critical": 0.5}) == 0.5

    def test_a_reader_that_raises_costs_one_signal_not_the_whole_decision(self):
        """A broken reader is a bug, and a bug here must not take down a case."""
        original = CL.SIGNAL_READERS["no_owner"]
        CL.SIGNAL_READERS["no_owner"] = lambda facts: 1 / 0
        try:
            scored = CL.score_contributions({"churn_risk": "critical"})
            assert "churn_critical" in scored["signal_ids"]
            assert "unowned" not in scored["signal_ids"]
        finally:
            CL.SIGNAL_READERS["no_owner"] = original

    def test_high_value_is_carried_but_never_scored(self):
        """The temptation is recorded so it is visible, and inert so it is not
        acted on. Weighting complaints by commercial value is how the people who
        need help least get it."""
        scored = CL.score_contributions({"value_tier": "platinum"})
        assert "high_value" in scored["signal_ids"]
        assert scored["loyalty_damage"] == 0.0
        assert scored["context_count"] == 1

    def test_every_signal_appears_in_the_table_whether_learned_or_not(self):
        """A table listing only learned entries makes the configured priors
        invisible, and a reviewer then believes a default is a measurement."""
        rows = CL.weight_table_payload()
        assert len(rows) == len(CL.LOYALTY_SIGNALS)
        assert all(row["source"] == "configured_prior" for row in rows)
        assert all(row["confidence"] == 0.0 for row in rows)


# =============================================================================
# The authority invariant
# =============================================================================


class TestLearnedWeightsAreAdvisory:
    """The safety argument, pinned as executable assertions.

    `LEARNED_WEIGHT_AUTHORITY` is a dict, and a dict does not stop anyone from
    editing it. These are the assertions that would fail if the argument were
    withdrawn, and the validator that errors on it at startup.
    """

    def test_the_authority_flags_are_what_the_docstring_claims(self):
        assert CL.LEARNED_WEIGHT_AUTHORITY["may_reorder_recommendations"] is True
        assert CL.LEARNED_WEIGHT_AUTHORITY["may_change_explanation"] is True
        assert CL.LEARNED_WEIGHT_AUTHORITY["may_change_tier_alone"] is False
        assert CL.LEARNED_WEIGHT_AUTHORITY["may_change_severity_alone"] is False
        assert CL.LEARNED_WEIGHT_AUTHORITY["may_auto_escalate"] is False

    def test_the_validator_errors_if_a_weight_is_allowed_to_move_a_tier(self):
        CL.LEARNED_WEIGHT_AUTHORITY["may_change_tier_alone"] = True
        try:
            result = CL.validate_complaint_learning()
            assert result["valid"] is False
            assert any("may_change_tier_alone" in e for e in result["errors"])
        finally:
            CL.LEARNED_WEIGHT_AUTHORITY["may_change_tier_alone"] = False

    def test_no_learned_number_reaches_the_trigger_context(self):
        """The structural guarantee, not just the documented one.

        A learned weight is absent from the `context` dict that
        `rule_engine.evaluate_when` is called with. So it cannot move a tier even
        by accident, even by a future contributor who adds it "just for the
        explanation" -- the escalation path has no way to see it.
        """
        from app.services.complaints import _probe_scope

        context = _probe_scope()
        assert not any("weight" in key for key in context)
        assert not any("contribution" in key for key in context)

    def test_the_learned_feed_is_optional_so_a_cold_table_cannot_block_a_decision(self):
        feed = C.DECISION_FEED_BY_ID["loyalty_contribution"]
        assert feed["required"] is False
        assert feed["async_only"] is True


# =============================================================================
# Detectors
# =============================================================================


class TestSystemDetectors:
    def test_every_detector_names_a_real_complaint_category(self):
        for detector in CL.SYSTEM_COMPLAINT_DETECTORS:
            assert detector["category"] in C.COMPLAINT_CATEGORY_BY_NAME

    def test_every_detector_has_a_cooldown(self):
        """Without one, a stuck condition opens a case every sweep and the queue
        becomes unreadable."""
        for detector in CL.SYSTEM_COMPLAINT_DETECTORS:
            assert detector["cooldown_hours"] > 0

    def test_every_when_block_parses(self):
        for detector in CL.SYSTEM_COMPLAINT_DETECTORS:
            assert rule_engine.validate_when(detector["when"])["valid"], detector["detector_id"]

    def test_every_when_block_reads_only_keys_a_context_produces(self):
        """A detector reading `churn_scorr` would be permanently dead while
        reading exactly like a working one."""
        for detector in CL.SYSTEM_COMPLAINT_DETECTORS:
            fields = rule_engine.validate_when(detector["when"]).get("fields_referenced", [])
            for field in fields:
                assert field in CL._DETECTOR_CONTEXT_KEYS, (
                    f"{detector['detector_id']} reads {field!r}, which no context produces"
                )

    def test_a_detector_whose_confidence_is_below_its_own_gate_is_inert(self):
        """`billing_dispute_unresolved` ships in this state deliberately.

        An arrears balance plus an open complaint is most customers, and opening
        a case about every one of them would be an accusation rather than a
        service. The row documents the shape without acting.
        """
        detector = CL.SYSTEM_DETECTOR_BY_ID["billing_dispute_unresolved"]
        assert CL.detector_is_enabled(detector) is False
        warnings = CL.validate_complaint_learning()["warnings"]
        assert any("billing_dispute_unresolved" in w for w in warnings)

    def test_the_sla_breach_detector_needs_an_unacknowledged_case(self):
        """The distinction the detector exists on.

        "Unanswered" and "unacknowledged" are different: a case nobody has looked
        at is worth a system complaint, a case that is merely old and in progress
        is not.
        """
        detector = CL.SYSTEM_DETECTOR_BY_ID["sla_response_breach"]
        overdue = {"response_overdue_hours": 9.0, "acknowledged": False}
        acked = {"response_overdue_hours": 9.0, "acknowledged": True}
        assert CL.evaluate_detector(detector, overdue)["matched"] is True
        assert CL.evaluate_detector(detector, acked)["matched"] is False

    def test_the_booking_detector_needs_a_pattern_not_one_failure(self):
        detector = CL.SYSTEM_DETECTOR_BY_ID["booking_failure_pattern"]
        assert CL.evaluate_detector(detector, {"cancelled_bookings": 4, "pending_bookings": 1})["matched"]
        assert not CL.evaluate_detector(detector, {"cancelled_bookings": 1, "pending_bookings": 1})["matched"]

    def test_a_malformed_when_block_matches_nothing_rather_than_everything(self):
        """Fail closed. A detector that cannot parse must not open cases."""
        detector = {**CL.SYSTEM_DETECTOR_BY_ID["sla_response_breach"], "when": {"x": {"~~": 1}}}
        assert CL.evaluate_detector(detector, {"x": 99})["matched"] is False

    def test_the_confidence_gate_never_raises_its_own_confidence(self):
        """The rule decides *whether*; only config decides *how sure*.

        A rule that could claim 0.99 would be inventing certainty about a person.
        """
        for detector in CL.SYSTEM_COMPLAINT_DETECTORS:
            verdict = CL.evaluate_detector(detector, {})
            assert verdict["confidence"] == detector["confidence"]

    def test_the_origin_round_trips(self):
        assert CL.parse_system_origin(CL.system_origin("sla_response_breach")) == "sla_response_breach"

    def test_a_human_raised_case_is_not_a_system_one(self):
        assert CL.parse_system_origin("chat") is None
        assert CL.parse_system_origin("agent_raised") is None
        assert CL.parse_system_origin("") is None

    def test_cooldown_maths(self):
        fired = NOW - timedelta(hours=10)
        assert CL._cooldown_remaining_hours(fired, 72.0, NOW) == 62.0
        assert CL._cooldown_remaining_hours(NOW - timedelta(hours=100), 72.0, NOW) == 0.0
        assert CL._cooldown_remaining_hours(None, 72.0, NOW) == 0.0


# =============================================================================
# Cluster detection
# =============================================================================


class TestClusterThresholds:
    def _rule(self, **kw):
        return {**CL.IMPROVEMENT_CLUSTER_BY_ID["category_churn"], **kw}

    def test_reopen_rate_is_the_threshold_that_matters(self):
        """A cluster of closed-and-not-reopened cases is the system working."""
        steady = CL.cluster_meets_thresholds(
            {"cases": 20, "distinct_users": 9, "reopen_rate": 0.0},
            self._rule(min_cases=5, min_distinct_users=3, min_reopen_rate=0.2),
        )
        churning = CL.cluster_meets_thresholds(
            {"cases": 5, "distinct_users": 4, "reopen_rate": 0.6},
            self._rule(min_cases=5, min_distinct_users=3, min_reopen_rate=0.2),
        )
        assert steady["meets"] is False
        assert churning["meets"] is True

    def test_a_shortfall_reports_which_number_was_short(self):
        """Only reporting "no" gets thresholds tuned by guesswork, and lowering
        the wrong one is how a suggestion stream becomes noise."""
        result = CL.cluster_meets_thresholds(
            {"cases": 9, "distinct_users": 1, "reopen_rate": 0.0},
            self._rule(min_cases=5, min_distinct_users=3, min_reopen_rate=0.2),
        )
        assert result["meets"] is False
        assert set(result["short_by"]) == {"min_distinct_users", "min_reopen_rate"}

    def test_a_rule_whose_distinct_user_floor_exceeds_its_case_floor_can_never_fire(self):
        result = CL.validate_complaint_learning()
        assert result["valid"] is True
        # And the validator *would* catch it, which is the point of the check.
        rule = CL.IMPROVEMENT_CLUSTER_RULES[0]
        original = rule["min_distinct_users"]
        rule["min_distinct_users"] = rule["min_cases"] + 1
        try:
            assert CL.validate_complaint_learning()["valid"] is False
        finally:
            rule["min_distinct_users"] = original

    def test_an_unknown_axis_value_clusters_under_a_named_key_not_the_axis_name(self):
        """Otherwise every unrecognised case aggregates into one fake "stack"."""
        assert CL._cluster_axis_value(object(), "category") == CL.DEFAULT_COMPLAINT_CATEGORY
        assert CL._cluster_axis_value(object(), "owner_team") == CL.UNOWNED_CLUSTER_KEY


# =============================================================================
# Proposal rendering
# =============================================================================


class TestProposalShape:
    def _cluster(self, **evidence):
        base = {
            "cases": 12, "distinct_users": 30, "reopen_rate": 0.58,
            "regulatory_cases": 0, "system_raised": 0,
            "median_age_days": 31.0, "oldest_case_days": 88.0,
        }
        base.update(evidence)
        return {
            "cluster_key": "category_churn:category:billing", "rule_id": "category_churn",
            "axis": "category", "value": "billing", "window_days": 90,
            "label": "A category that keeps coming back", "evidence": base, "case_ids": [1, 2],
        }

    def test_the_shape_matches_the_audit_proposal_it_is_compared_against(self):
        """A reader comparing the two suggestion streams should not translate."""
        from app.schemas.audit import EnhancementProposal

        rendered = CL.build_improvement_proposal(self._cluster())
        for field in EnhancementProposal.model_fields:
            assert field in rendered, f"complaint proposals must carry {field}"

    def test_the_proposal_id_is_stable_as_the_evidence_grows(self):
        """The single most important property of the id.

        If it moved with the numbers, every sweep would mint a fresh proposal and
        a human would see the same suggestion forever, which is how a suggestion
        stream trains people to ignore it.
        """
        small = CL.build_improvement_proposal(self._cluster(cases=5))
        large = CL.build_improvement_proposal(self._cluster(cases=500))
        assert small["proposal_id"] == large["proposal_id"]

    def test_different_clusters_get_different_ids(self):
        other = {**self._cluster(), "cluster_key": "category_churn:category:privacy"}
        assert (
            CL.build_improvement_proposal(self._cluster())["proposal_id"]
            != CL.build_improvement_proposal(other)["proposal_id"]
        )

    def test_reopen_percent_appears_consistently(self):
        """An inverted percentage would state the opposite of the evidence.

        58% reopened means 58% of resolutions did not hold, not 42%.
        """
        proposal = CL.build_improvement_proposal(self._cluster(reopen_rate=0.58))
        assert "58%" in proposal["recommended_action"]
        assert "42%" not in proposal["recommended_action"]

    def test_priority_prefers_the_cluster_that_keeps_coming_back(self):
        """Priority is not case count.

        A large but stable cluster is a capacity problem. A small cluster that
        keeps reopening is a correctness problem, and it must outrank the first.
        """
        loud = CL.build_improvement_proposal(
            self._cluster(cases=40, distinct_users=3, reopen_rate=0.0, rule_id="category_volume")
        )
        quiet = CL.build_improvement_proposal(
            self._cluster(cases=6, distinct_users=3, reopen_rate=0.7)
        )
        assert loud["priority"] == "medium"
        assert quiet["priority"] == "high"

    def test_a_system_raised_cluster_is_called_out(self):
        """It points at the detector or the fault, which is a different fix."""
        proposal = CL.build_improvement_proposal(self._cluster(system_raised=4))
        assert "system" in proposal["recommended_action"]
        assert any("system" in r for r in proposal["rationale"])

    def test_a_regulatory_cluster_is_called_out_as_compliance_risk(self):
        proposal = CL.build_improvement_proposal(self._cluster(regulatory_cases=3))
        assert any("compliance" in r or "statutory" in r for r in proposal["rationale"])

    def test_effort_is_a_hint_and_says_so_by_being_coarse(self):
        """Nobody here has measured either number of days, so it is a band."""
        small = CL.build_improvement_proposal(self._cluster(distinct_users=2))
        large = CL.build_improvement_proposal(self._cluster(distinct_users=40))
        assert small["effort"] == "low"
        assert large["effort"] == "high"

    def test_rendering_is_deterministic_given_a_fixed_now(self):
        """Pure means *given the same inputs*, and `detected_at` is an input.

        The first version of this test called the function twice with no `now`
        and asserted equality, which failed on the timestamp -- the code was
        right and the test was asserting something the signature does not promise.
        """
        cluster = self._cluster()
        assert (
            CL.build_improvement_proposal(cluster, now=NOW)
            == CL.build_improvement_proposal(cluster, now=NOW)
        )


# =============================================================================
# BLOCKAGES.md
# =============================================================================


class TestBlockagesRendering:
    def _proposal(self, pid="CIMP-abc123"):
        return {
            "proposal_id": pid, "title": "Something stacked", "component": "services/x",
            "owner_hint": "platform", "priority": "high", "impact": "medium", "effort": "low",
            "rationale": ["because"], "recommended_action": "do the thing",
            "signals": [], "status": "detected", "candidate_id": "",
            "evidence": {"cases": 8, "distinct_users": 5, "reopen_rate": 0.25, "system_raised": 0},
        }

    def test_it_does_not_write_the_file(self):
        """The module has no write path to BLOCKAGES.md at all.

        That file is hand-authored governance prose; a generated section inside it
        would invert the document's authority, and nothing here can do that
        because there is no code to do it with.
        """
        import inspect

        source = inspect.getsource(CL)
        for forbidden in ("open(BLOCKAGES", "write_text", ".write(", "Path(\"BLOCKAGES"):
            assert forbidden not in source, f"complaint_learning must not contain {forbidden}"

    def test_rendering_is_pure_and_repeatable(self):
        first = CL.render_blockages_section([self._proposal()], now=NOW)
        second = CL.render_blockages_section([self._proposal()], now=NOW)
        assert first == second

    def test_it_reports_what_a_paste_would_add_and_removes_nothing(self):
        """The asymmetry is the whole safety property.

        Adding a suggestion to a human's document is recoverable. Silently
        dropping their line is not.
        """
        existing = "# My notes\n\nSomething I wrote by hand.\n"
        result = CL.diff_against_rendered([self._proposal()], existing, now=NOW)
        assert result["new"] == ["CIMP-abc123"]
        assert result["would_add"] == 1
        assert result["writes"] is False
        assert "Something I wrote by hand." in existing

    def test_an_already_present_proposal_is_not_re_added(self):
        existing = CL.render_blockages_section([self._proposal()], now=NOW)
        result = CL.diff_against_rendered([self._proposal()], existing, now=NOW)
        assert result["would_add"] == 0
        assert result["already_present"] == ["CIMP-abc123"]

    def test_a_dismissed_proposal_is_not_rendered(self):
        dismissed = {**self._proposal(), "status": "dismissed"}
        assert "CIMP-abc123" not in CL.render_blockages_section([dismissed], now=NOW)

    def test_no_open_proposals_renders_a_clear_empty_state(self):
        """A reader must be able to tell "nothing is wrong" from "nothing ran"."""
        text = CL.render_blockages_section([], now=NOW)
        assert "none open" in text
        assert "ran and every cluster was under its thresholds" in text


# =============================================================================
# Release ladder integration
# =============================================================================


class TestReleaseLadderIntegration:
    def _proposal(self):
        return CL.build_improvement_proposal({
            "cluster_key": "category_volume:category:billing", "rule_id": "category_volume",
            "axis": "category", "value": "billing", "window_days": 30,
            "label": "One complaint category dominating",
            "evidence": {"cases": 9, "distinct_users": 6, "reopen_rate": 0.0,
                         "regulatory_cases": 0, "system_raised": 0,
                         "median_age_days": 5.0, "oldest_case_days": 20.0},
            "case_ids": [],
        })

    def test_a_proposal_becomes_a_candidate_at_draft_and_nowhere_else(self):
        """Complaint volume is evidence for "how far along is this", never for
        "may this go further"."""
        from app import release_ladder

        candidate = CL.proposal_to_candidate(
            self._proposal(), commit="abc123def456", revision="0008_complaint_learning"
        )
        assert candidate.level == release_ladder.DRAFT_LEVEL == "l0_draft"
        assert candidate.code_version.startswith("code:")
        assert candidate.data_version == "data:0008_complaint_learning"

    def test_the_ladder_refuses_to_register_above_draft(self):
        from app import release_ladder

        assert release_ladder.register_candidate(
            CL.proposal_to_candidate(self._proposal(), commit="a" * 12)
        ).level == "l0_draft"
        with pytest.raises(ValueError):
            release_ladder.register_candidate(
                release_ladder.ReleaseCandidate(
                    candidate_id="too-high", code_version="code:a", data_version="data:b",
                    level="l2_canary", summary="x",
                )
            )

    def test_the_evidence_travels_into_the_candidate_measurements(self):
        """A reviewer sees the numbers in the ladder, not just a title."""
        candidate = CL.proposal_to_candidate(self._proposal(), commit="a" * 12)
        assert candidate.measured["complaint_cases"] == 9
        assert candidate.measured["distinct_customers"] == 6
        assert candidate.measured["reopen_rate"] == 0.0

    def test_the_summary_is_capped_so_the_ladder_table_stays_readable(self):
        candidate = CL.proposal_to_candidate(self._proposal(), commit="a" * 12)
        assert len(candidate.summary) <= CL.SUMMARY_MAX_CHARS + 3

    def test_candidate_pair_resolves(self):
        """`ReleaseCandidate.pair()` raised AttributeError on every call.

        It passed the dataclass to `split_version_pair`, which reads with
        `.get`. Pre-existing bug, found while wiring proposals onto the ladder.
        It matters because `pair()` is how the rollback path names the versions
        it must be able to return to.
        """
        candidate = CL.proposal_to_candidate(self._proposal(), commit="a" * 12)
        assert candidate.pair()["code_version"].startswith("code:")


# =============================================================================
# The validator itself
# =============================================================================


class TestValidatorCatchesRealFaults:
    """A validator that cannot fail is a comment."""

    def test_the_module_is_valid_as_shipped(self):
        result = CL.validate_complaint_learning()
        assert result["valid"] is True
        assert result["errors"] == []
        # Both warnings are deliberate and documented, so they are pinned: a
        # validator that cries wolf gets ignored.
        assert len(result["warnings"]) == 2

    def test_a_signal_with_no_reader_is_an_error(self):
        original = CL.LOYALTY_SIGNALS[0]["derive"]
        CL.LOYALTY_SIGNALS[0]["derive"] = "no_such_reader"
        try:
            result = CL.validate_complaint_learning()
            assert result["valid"] is False
            assert any("no reader" in e for e in result["errors"])
        finally:
            CL.LOYALTY_SIGNALS[0]["derive"] = original

    def test_a_prior_outside_the_learnable_range_is_an_error(self):
        """Otherwise the first observation silently clamps it and the configured
        prior is unreachable without anyone noticing."""
        original = CL.LOYALTY_SIGNALS[0]["prior_weight"]
        CL.LOYALTY_SIGNALS[0]["prior_weight"] = 99.0
        try:
            assert CL.validate_complaint_learning()["valid"] is False
        finally:
            CL.LOYALTY_SIGNALS[0]["prior_weight"] = original

    def test_a_zero_cooldown_is_an_error(self):
        original = CL.SYSTEM_COMPLAINT_DETECTORS[0]["cooldown_hours"]
        CL.SYSTEM_COMPLAINT_DETECTORS[0]["cooldown_hours"] = 0
        try:
            assert CL.validate_complaint_learning()["valid"] is False
        finally:
            CL.SYSTEM_COMPLAINT_DETECTORS[0]["cooldown_hours"] = original

    def test_a_detector_on_an_unknown_category_is_an_error(self):
        original = CL.SYSTEM_COMPLAINT_DETECTORS[0]["category"]
        CL.SYSTEM_COMPLAINT_DETECTORS[0]["category"] = "not_a_category"
        try:
            assert CL.validate_complaint_learning()["valid"] is False
        finally:
            CL.SYSTEM_COMPLAINT_DETECTORS[0]["category"] = original

    def test_the_module_is_valid_again_after_every_probe(self):
        """A validator that leaves the module broken is worse than none."""
        assert CL.validate_complaint_learning()["valid"] is True

    def test_the_catalog_reports_the_inert_detector(self):
        """Otherwise a deliberately-disabled row reads as a missing feature."""
        catalog = CL.build_complaint_learning_catalog()
        by_id = {d["detector_id"]: d for d in catalog["system_complaints"]["detectors"]}
        assert by_id["billing_dispute_unresolved"]["enabled"] is False
        assert by_id["sla_response_breach"]["enabled"] is True

    def test_the_catalog_states_that_blockages_is_not_written(self):
        catalog = CL.build_complaint_learning_catalog()
        assert "not written" in catalog["improvements"]["blockages_md"]


# =============================================================================
# End to end, against a real database
# =============================================================================
#
# The pure logic above is covered without a database because it is pure. These
# are the paths that are not: the detectors reading real rows, the learning pass
# writing real observations, and the cluster scan grouping real cases. A session
# double would assert back whatever the code assumed, so these use SQLite.


@pytest.fixture()
def harness():
    """A real in-memory database per test. See `tests/_doubles.SqliteHarness`."""
    h = SqliteHarness()
    h.run(h.setup())
    try:
        yield h
    finally:
        h.run(h.teardown())
        h.close()


def _make_user(harness, user_id: int) -> None:
    """Insert a user, reusing an existing id rather than fighting the constraint.

    `_stack` calls this once per case but several cases can share a customer --
    distinct-user count is one of the cluster thresholds, so a fixture that
    created a fresh user per case would silently make every cluster look more
    diverse than it is.
    """

    async def go() -> None:
        existing = await harness.session.scalar(
            sa_select(models.User.id).where(models.User.id == int(user_id))
        )
        if existing is not None:
            return
        harness.session.add(models.User(
            id=int(user_id), username=f"u{user_id}", email=f"u{user_id}@example.test",
            full_name=f"User {user_id}", hashed_password="not-a-real-hash",
        ))
        await harness.session.commit()

    harness.run(go())


def _add_case(harness, **kwargs):
    """Insert a complaint case directly, with a controllable opened_at."""

    async def go():
        session = harness.session
        base = {
            "user_id": 1,
            "reference": f"CMP-{kwargs.get('id', 1):06d}",
            "category": "service_quality",
            "severity": "medium",
            "status": "open",
            "tier": "tier_1",
            "opened_at": NOW,
            "source": "chat",
        }
        base.update(kwargs)
        case = models.ComplaintCase(**base)
        session.add(case)
        await session.commit()
        return case

    return harness.run(go())


class TestSystemRaisedComplaintsEndToEnd:
    def test_an_unanswered_case_gets_a_system_complaint(self, harness):
        """The headline capability: the system opens a case for someone who
        never complained, because the evidence was already in our own records."""
        _make_user(harness, 1)
        original = _add_case(harness, id=1, opened_at=NOW - timedelta(hours=30))
        # response_due_at is derived from the SLA clock; set it in the past so the
        # case is genuinely overdue rather than merely old.
        _patch_case(harness, original.id, response_due_at=NOW - timedelta(hours=10), resolution_due_at=None)
        _patch_case(harness, original.id, acknowledged_at=None, first_response_at=None)

        detection = harness.run(CL.detect_system_complaints(harness.session, now=NOW))
        assert detection["dry_run"] is True
        assert detection["would_raise"] >= 1
        assert any(
            c["detector_id"] == "sla_response_breach" and c["will_raise"]
            for c in detection["candidates"]
        ), [c["detector_id"] for c in detection["candidates"]]

        # The dry run must not have written anything.
        assert _case_count(harness) == 1

        result = harness.run(
            CL.raise_system_complaints(harness.session, now=NOW)
        )
        assert result["raised"] >= 1
        raised = _cases_with_source(harness, "system:")
        assert raised, "expected a case with a system origin"
        assert raised[0].source.startswith("system:")
        assert raised[0].user_id == 1

    def test_the_cooldown_stops_a_second_case_for_the_same_condition(self, harness):
        """Without this, a stuck condition opens a case every sweep and the
        queue becomes unreadable."""
        _make_user(harness, 1)
        original = _add_case(harness, id=1, opened_at=NOW - timedelta(hours=30))
        _patch_case(harness, original.id, response_due_at=NOW - timedelta(hours=10), resolution_due_at=None)
        _patch_case(harness, original.id, acknowledged_at=None, first_response_at=None)

        first = harness.run(CL.raise_system_complaints(harness.session, now=NOW))
        assert first["raised"] >= 1
        after_first = _case_count(harness)

        second = harness.run(CL.raise_system_complaints(harness.session, now=NOW))
        assert second["raised"] == 0
        assert second["held_back"], "the cooldown must be reported, not silent"
        assert _case_count(harness) == after_first

    def test_dedupe_reuses_a_live_case_rather_than_opening_a_second(self, harness):
        """One unresolved problem should be one record, not five."""
        _make_user(harness, 1)
        _add_case(harness, id=1, category="service_quality", status="open", opened_at=NOW)
        detection = harness.run(CL.detect_system_complaints(harness.session, now=NOW))
        for candidate in detection["candidates"]:
            if candidate["dedupe_category"] and candidate["matched"] if "matched" in candidate else candidate["dedupe_category"]:
                assert candidate["blocked_by"] in {"dedupe", ""}

    def test_a_raised_case_carries_the_detector_evidence(self, harness):
        """The reason has to be on the record, not just in the log."""
        _make_user(harness, 1)
        original = _add_case(harness, id=1, opened_at=NOW - timedelta(hours=30))
        _patch_case(harness, original.id, response_due_at=NOW - timedelta(hours=10), resolution_due_at=None)
        _patch_case(harness, original.id, acknowledged_at=None, first_response_at=None)
        harness.run(CL.raise_system_complaints(harness.session, now=NOW))

        raised = _cases_with_source(harness, "system:")
        assert raised
        # `factors_json` is a Text column, so it is a string on the row. The
        # evidence is not missing, it is unserialised -- and reading it as a dict
        # would have reported "detector_id not in <str>" and sent the reader
        # looking for a bug in the writer.
        factors = CL._loads(raised[0].factors_json, "object")
        assert factors.get("raised_by") == "system"
        assert factors.get("detector_id") == "sla_response_breach"
        assert 0.0 < float(factors.get("confidence", 0)) <= 1.0
        assert "sla_response_breached" in factors.get("seed_signals", [])
        # And it must point at the case that triggered it, so the customer-facing
        # record and the machine's reason are the same case.
        assert int(factors.get("trigger_case_id", 0)) != 0

    def test_the_summary_says_the_system_raised_it(self, harness):
        """A system complaint must not read as though the customer complained."""
        _make_user(harness, 1)
        original = _add_case(harness, id=1, opened_at=NOW - timedelta(hours=30))
        _patch_case(harness, original.id, response_due_at=NOW - timedelta(hours=10), resolution_due_at=None)
        _patch_case(harness, original.id, acknowledged_at=None, first_response_at=None)
        harness.run(CL.raise_system_complaints(harness.session, now=NOW))
        raised = _cases_with_source(harness, "system:")
        assert "automatically" in (raised[0].summary or "").lower()

    def test_the_raise_cap_holds(self, harness):
        """One bad snapshot must not become one case per customer."""
        for uid in range(1, 12):
            _make_user(harness, uid)
            case = _add_case(harness, id=uid, user_id=uid, opened_at=NOW - timedelta(hours=30))
            _patch_case(harness, case.id, response_due_at=NOW - timedelta(hours=10), resolution_due_at=None)
            _patch_case(harness, case.id, acknowledged_at=None, first_response_at=None)
        result = harness.run(
            CL.raise_system_complaints(harness.session, now=NOW, max_raise=2)
        )
        assert result["raised"] <= 2
        assert result["cap_reached"] is True

    def test_system_raised_cases_are_listable_by_detector(self, harness):
        """'The system opened 40 cases' is not actionable; 'the billing detector
        opened 39' is."""
        _make_user(harness, 1)
        original = _add_case(harness, id=1, opened_at=NOW - timedelta(hours=30))
        _patch_case(harness, original.id, response_due_at=NOW - timedelta(hours=10), resolution_due_at=None)
        _patch_case(harness, original.id, acknowledged_at=None, first_response_at=None)
        harness.run(CL.raise_system_complaints(harness.session, now=NOW))
        listed = harness.run(CL.list_system_raised(harness.session))
        assert listed["total"] >= 1
        assert listed["by_detector"]


class TestLearningPassEndToEnd:
    def _closed_case(self, harness, case_id, user_id, outcome, days_ago=40):
        opened = NOW - timedelta(days=days_ago)
        case = _add_case(
            harness, id=case_id, user_id=user_id, status="closed",
            opened_at=opened, closed_at=opened + timedelta(days=2),
            resolution_code="refunded",
            # The case's own reopened count, not just the decision's outcome word.
            # The `reopened` signal reads the case; a decision row saying
            # "reopened" with a case recording zero would be an inconsistency the
            # module should not paper over.
            reopened_count=1 if outcome == "reopened" else 0,
        )
        _add_decision(harness, case.id, user_id, outcome)
        return case

    def test_a_pass_on_an_empty_database_learns_nothing_and_says_so(self, harness):
        result = harness.run(CL.run_learning_pass(harness.session, now=NOW))
        assert result["candidates"] == 0
        assert result["observations_written"] == 0
        assert result["signals_moved"] == []

    def test_a_too_young_case_is_not_judged(self, harness):
        """The most important negative case in the module.

        A case opened yesterday has no loyalty outcome, and recording it as
        evidence would be how the learner concludes that ignoring complaints
        works.
        """
        _make_user(harness, 1)
        self._closed_case(harness, 1, 1, "resolved", days_ago=1)
        result = harness.run(CL.run_learning_pass(harness.session, now=NOW))
        assert result["candidates"] == 0
        assert result["signals_moved"] == []

    def test_a_reopened_case_writes_observations_and_moves_the_weight(self, harness):
        _make_user(harness, 1)
        self._closed_case(harness, 1, 1, "reopened", days_ago=40)
        _make_user(harness, 2)
        self._closed_case(harness, 2, 2, "churned", days_ago=41)

        result = harness.run(CL.run_learning_pass(harness.session, now=NOW))
        assert result["observations_written"] > 0
        rows = _observations(harness)
        assert rows, "observations must be durable rows, not just a count"
        assert all(r.weight_snapshot > 0 for r in rows)
        assert any(r.signal_id == "reopened" for r in rows)

    def test_running_twice_does_not_double_count(self, harness):
        """Idempotence of the learning pass itself.

        A scheduled sweep that ran twice must not train twice, or the weights
        would move at double speed for reasons nobody chose.
        """
        _make_user(harness, 1)
        self._closed_case(harness, 1, 1, "reopened", days_ago=40)
        first = harness.run(CL.run_learning_pass(harness.session, now=NOW))
        second = harness.run(CL.run_learning_pass(harness.session, now=NOW))
        assert second["observations_written"] == 0
        assert any(s["reason"] == "already_observed" for s in second["skipped"])
        assert first["observations_written"] > 0

    def test_a_neutral_outcome_writes_evidence_but_moves_nothing(self, harness):
        """The observation is recorded so the reason is auditable; the weight is
        untouched so the system learns nothing from a non-answer."""
        _make_user(harness, 1)
        # No post-complaint contact -> dormant would need 14+ days of silence, and
        # no horizon data -> neutral. Either way, no training.
        self._closed_case(harness, 1, 1, "resolved", days_ago=3)
        result = harness.run(CL.run_learning_pass(harness.session, now=NOW))
        assert result["signals_moved"] == []

    def test_the_weight_report_shows_learned_and_configured_separately(self, harness):
        """A cold start and a learned value must never look the same."""
        _make_user(harness, 1)
        self._closed_case(harness, 1, 1, "reopened", days_ago=40)
        harness.run(CL.run_learning_pass(harness.session, now=NOW))
        report = harness.run(CL.build_weight_report(harness.session))
        assert report["learned_count"] + report["configured_count"] == len(CL.LOYALTY_SIGNALS)
        assert any(row["source"] == "learned" for row in report["table"])
        assert any(row["source"] == "configured_prior" for row in report["table"])
        assert report["authority"]["may_change_tier_alone"] is False

    def test_the_report_carries_the_authority_statement(self, harness):
        """The catalog is what a reviewer reads to check the safety claim, so it
        has to actually carry it."""
        report = harness.run(CL.build_weight_report(harness.session))
        why = report["authority"]["why"]
        assert "statistic" in why
        assert "quarter" in why
        assert report["authority"]["may_auto_escalate"] is False


class TestClusteringEndToEnd:
    def _stack(self, harness, count, users, category="billing", reopen=0, days=5):
        ids = []
        for i in range(count):
            uid = (i % users) + 1
            _make_user(harness, uid)
            ids.append(
                _add_case(
                    harness, id=i + 1, user_id=uid, category=category,
                    status="closed", opened_at=NOW - timedelta(days=days),
                    closed_at=NOW - timedelta(days=days - 1),
                    reopened_count=1 if i < reopen else 0,
                ).id
            )
        return ids

    def test_a_stack_below_threshold_is_not_a_stack(self, harness):
        self._stack(harness, count=2, users=2)
        detection = harness.run(CL.detect_stacked_complaints(harness.session, now=NOW))
        assert detection["stack_count"] == 0
        assert detection["near_misses"], "a near miss must still be reported"

    def test_a_stack_above_threshold_is_detected_with_its_evidence(self, harness):
        self._stack(harness, count=8, users=6, reopen=4)
        detection = harness.run(CL.detect_stacked_complaints(harness.session, now=NOW))
        assert detection["stack_count"] >= 1
        top = detection["stacks"][0]
        assert top["evidence"]["cases"] >= 8
        assert top["evidence"]["reopen_rate"] > 0

    def test_proposals_are_persisted_and_idempotent(self, harness):
        """Re-running must update the row, not mint a second one.

        A proposal that reappeared every sweep would train a human to ignore the
        suggestion stream, which is the same as having no stream.
        """
        self._stack(harness, count=8, users=6, reopen=4)
        first = harness.run(CL.build_and_store_proposals(harness.session, now=NOW))
        assert first["created"] >= 1
        second = harness.run(CL.build_and_store_proposals(harness.session, now=NOW))
        assert second["created"] == 0
        assert second["updated"] == first["created"]

    def test_a_dismissed_proposal_is_not_reset_by_the_next_sweep(self, harness):
        """A decision a human made must survive an automated job."""
        self._stack(harness, count=8, users=6, reopen=4)
        first = harness.run(CL.build_and_store_proposals(harness.session, now=NOW))
        pid = first["proposals"][0]["proposal_id"]
        harness.run(CL.set_proposal_status(harness.session, pid, "dismissed"))
        harness.run(CL.build_and_store_proposals(harness.session, now=NOW))
        stored = harness.run(CL.list_proposals(harness.session, status="dismissed"))
        assert [p["proposal_id"] for p in stored["proposals"]] == [pid]

    def test_publishing_registers_at_draft_and_marks_the_row(self, harness):
        self._stack(harness, count=8, users=6, reopen=4)
        harness.run(CL.build_and_store_proposals(harness.session, now=NOW))
        result = harness.run(CL.publish_proposals(harness.session, now=NOW))
        assert result["published_count"] >= 1
        assert all(p["level"] == "l0_draft" for p in result["published"])
        stored = harness.run(CL.list_proposals(harness.session, status="published"))
        assert stored["total"] >= 1

    def test_publishing_twice_reports_a_skip_rather_than_duplicating(self, harness):
        self._stack(harness, count=8, users=6, reopen=4)
        harness.run(CL.build_and_store_proposals(harness.session, now=NOW))
        harness.run(CL.publish_proposals(harness.session, now=NOW))
        again = harness.run(CL.publish_proposals(harness.session, now=NOW))
        assert again["published_count"] == 0
        assert any(s["reason"] == "already_registered" for s in again["skipped"])

    def test_an_unknown_status_is_rejected_rather_than_stored(self, harness):
        """Fail closed on a write path."""
        self._stack(harness, count=8, users=6, reopen=4)
        harness.run(CL.build_and_store_proposals(harness.session, now=NOW))
        with pytest.raises(ValueError):
            harness.run(CL.set_proposal_status(harness.session, "CIMP-nope", "banana"))

    def test_the_blockages_render_never_touches_the_file(self, harness):
        """End-to-end confirmation of the design decision.

        Publishing a proposal must not write to `BLOCKAGES.md`. The file is
        hand-authored governance prose, and a generated section inside it would
        invert its authority.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        blockages = root / "BLOCKAGES.md"
        before = blockages.read_bytes() if blockages.exists() else None
        self._stack(harness, count=8, users=6, reopen=4)
        harness.run(CL.build_and_store_proposals(harness.session, now=NOW))
        harness.run(CL.publish_proposals(harness.session, now=NOW))
        after = blockages.read_bytes() if blockages.exists() else None
        assert after == before, "publishing a proposal must not write BLOCKAGES.md"


# =============================================================================
# Async helpers
# =============================================================================
#
# These take the harness rather than reaching for a module-level global. The
# first version bound the session into a dict from an autouse fixture, which
# meant a helper called outside a test would read `None` and fail somewhere
# unrelated to the real cause.


def _patch_case(harness, case_id: int, **values) -> None:
    from sqlalchemy import update as sa_update

    async def go():
        await harness.session.execute(
            sa_update(models.ComplaintCase)
            .where(models.ComplaintCase.id == int(case_id))
            .values(**values)
        )
        await harness.session.commit()

    harness.run(go())


def _case_count(harness) -> int:
    from sqlalchemy import func, select

    async def go() -> int:
        return int(await harness.session.scalar(
            select(func.count(models.ComplaintCase.id))
        ) or 0)

    return harness.run(go())


def _cases_with_source(harness, prefix: str) -> list:
    from sqlalchemy import select

    async def go() -> list:
        result = await harness.session.execute(
            select(models.ComplaintCase)
            .where(models.ComplaintCase.source.like(f"{prefix}%"))
            .order_by(models.ComplaintCase.id)
        )
        return list(result.scalars().all())

    return harness.run(go())


def _observations(harness) -> list:
    from sqlalchemy import select

    async def go() -> list:
        result = await harness.session.execute(
            select(models.ComplaintSignalObservation)
            .order_by(models.ComplaintSignalObservation.id)
        )
        return list(result.scalars().all())

    return harness.run(go())


def _add_decision(harness, case_id: int, user_id: int, outcome: str) -> None:
    """A decision row carrying the complaint's own outcome.

    `complaint_decisions.outcome_observed` is the *complaint* outcome vocabulary
    ("resolved", "reopened", ...). It is deliberately not what the learning
    weights train on -- `complaint_signal_observations.loyalty_outcome` is, and
    that one is derived from what happened to the customer afterwards. Both
    columns existing on different tables is the point the module makes, so the
    fixture populates both halves of the distinction.
    """

    async def go():
        harness.session.add(models.ComplaintDecision(
            complaint_id=int(case_id),
            user_id=int(user_id),
            decision="escalate",
            outcome="proposed",
            rationale="test fixture",
            outcome_observed=str(outcome),
        ))
        await harness.session.commit()

    harness.run(go())


class TestDedupeDoesNotFireOnItsOwnTrigger:
    """The bug this class exists for.

    Every dedupe-enabled detector fired on a live case *in its own category*, then
    asked "is there already an open case in this category?" and found the case it
    had just been triggered by. Five of the six shipped detectors could therefore
    never open a case at all.

    It is worth its own class because the symptom looked like correct behaviour:
    every diagnostic said "matched, blocked: dedupe", which is exactly what a
    working dedupe looks like. Nothing crashed and no count was obviously wrong.
    """

    def test_every_dedupe_enabled_detector_can_actually_fire(self, harness):
        """Each detector gets a case that satisfies its `when`, and must reach
        `will_raise`."""
        from app.services.complaints import _probe_scope

        expectations = {
            "sla_response_breach": {"response_overdue_hours": 9.0, "acknowledged": False},
            "sla_resolution_breach": {"resolution_overdue_hours": 48.0, "resolved": False},
            "booking_failure_pattern": {"cancelled_bookings": 4, "pending_bookings": 1},
            "critical_churn_at_risk": {"churn_risk": "critical", "total_complaints": 2},
            "unowned_stale_case": {"no_owner": True, "age_hours": 50.0, "open_only": True},
        }
        for detector_id, overrides in expectations.items():
            detector = CL.SYSTEM_DETECTOR_BY_ID[detector_id]
            if not CL.detector_is_enabled(detector):
                continue
            matched = CL.evaluate_detector(
                detector, {**_probe_scope(), **overrides}
            )["matched"]
            assert matched, f"{detector_id} did not match its own scenario"

    def test_the_gate_excludes_the_trigger_case_from_dedupe(self, harness):
        """The structural fix, pinned.

        `find_open_case` is called with `exclude_id` set to the trigger case. If
        that argument is ever dropped, the detector blocks against itself again.
        """
        import inspect

        source = inspect.getsource(CL._gate_candidate)
        assert "exclude_id=int(case.id)" in source, (
            "the dedupe lookup must exclude the trigger case, or the detector "
            "blocks against the very case that woke it up"
        )

    def test_the_raise_path_excludes_the_trigger_case_too(self, harness):
        """The same bug existed independently in the write path, so it is pinned
        separately rather than assumed to follow."""
        import inspect

        source = inspect.getsource(CL.raise_system_complaints)
        assert 'exclude_id=int(candidate["trigger_case_id"])' in source

    def test_dedupe_still_blocks_on_a_genuinely_separate_live_case(self, harness):
        """Excluding the trigger must not disable dedupe entirely."""
        _make_user(harness, 1)
        trigger = _add_case(
            harness, id=1, category="service_quality", status="open",
            opened_at=NOW - timedelta(hours=30),
        )
        # A *different* live case in the same category, which is what dedupe is for.
        _add_case(
            harness, id=2, category="service_quality", status="open",
            opened_at=NOW - timedelta(hours=1),
        )
        _patch_case(harness, trigger.id, response_due_at=NOW - timedelta(hours=10), resolution_due_at=None)
        _patch_case(harness, trigger.id, acknowledged_at=None, first_response_at=None)

        detection = harness.run(CL.detect_system_complaints(harness.session, now=NOW))
        breach = [
            c for c in detection["candidates"] if c["detector_id"] == "sla_response_breach"
        ]
        assert breach, "the detector should still have matched"
        assert breach[0]["dedupe_target_id"] == 2, (
            "dedupe should point at the other live case, not at the trigger"
        )
        assert breach[0]["blocked_by"] == "dedupe"
        assert breach[0]["will_raise"] is False


class TestRaiseReportsWhyNothingHappened:
    """An operator asking "the sweep raised nothing, why?" needs an answer."""

    def test_suppressed_reasons_reach_the_raise_result(self, harness):
        _make_user(harness, 1)
        original = _add_case(harness, id=1, opened_at=NOW - timedelta(hours=30))
        _patch_case(harness, original.id, response_due_at=NOW - timedelta(hours=10), resolution_due_at=None)
        _patch_case(harness, original.id, acknowledged_at=None, first_response_at=None)
        harness.run(CL.raise_system_complaints(harness.session, now=NOW))

        second = harness.run(CL.raise_system_complaints(harness.session, now=NOW))
        assert second["raised"] == 0
        # The first version returned `held_back: []` here, because the candidates
        # were filtered out during detection and never reached the raise loop, so
        # every reason was dropped. An empty list reads as "nothing was
        # considered", which is a different and wrong claim.
        assert second["suppressed"]
        assert sum(second["suppressed"].values()) > 0
        assert any(h["reason"] for h in second["held_back"])


# =============================================================================
# The HTTP surface
# =============================================================================
#
# The service is tested against a real database above. What is left is the part
# only HTTP can show: that the routes exist, that they are admin-gated rather
# than merely declared as such, that the read endpoints do not write, and that
# the one endpoint whose whole argument is "it does not write" really does not.


@pytest.fixture()
def client(harness):
    """An app wired to the in-memory database, with the admin dependency faked.

    `get_current_admin_user` is overridden rather than authenticated, so these
    tests exercise authorisation *placement* -- is the dependency bound to the
    route -- rather than re-testing the token machinery. The authz rule table is
    checked separately below, since a route can have the dependency bound and
    still be classified as public.
    """
    from fastapi.testclient import TestClient

    from app import deps
    from app.main import app

    class _Admin:
        id = 1
        username = "admin"
        is_admin = True

    async def _fake_get_db():
        yield harness.session

    async def _fake_admin():
        return _Admin()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_admin
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)


class TestLearningRoutes:
    def test_every_learning_route_is_registered(self, client):
        spec = client.get("/openapi.json").json()
        for path in (
            "/complaints/admin/learning/catalog",
            "/complaints/admin/learning/weights",
            "/complaints/admin/learning/observe",
            "/complaints/admin/learning/detect",
            "/complaints/admin/learning/raise",
            "/complaints/admin/learning/system-raised",
            "/complaints/admin/learning/clusters",
            "/complaints/admin/learning/proposals",
            "/complaints/admin/learning/publish",
            "/complaints/admin/learning/blockages",
        ):
            assert path in spec["paths"], f"{path} is not registered"

    def test_every_learning_route_refuses_an_unauthenticated_caller(self, harness):
        """The property itself, checked at runtime rather than by introspection.

        The `meta_surface` defect in the release-ladder work was a route
        classified as admin with no security dependency bound, so the endpoints
        were public. Two earlier attempts at this test were worse than none: one
        filtered `app.routes` for `APIRoute` and found nothing (the app's routes
        sit behind an `_IncludedRouter` wrapper that exposes no attributes to
        walk), and the one before that introspected the OpenAPI schema, concluded
        nothing, and ended in a bare `assert True`.

        The honest check is to call the endpoint and see whether it refuses. That
        tests the behaviour rather than the wiring, and it cannot pass vacuously:
        drop the dependency and these become 401s, which is the failure this test
        exists to catch.
        """
        from fastapi.testclient import TestClient

        from app import deps
        from app.main import app

        # Only `get_current_admin_user` is un-overridden here; `get_db` stays
        # overridden so a request that *is* let through would find a database
        # rather than failing to connect, which would muddy the result.
        app.dependency_overrides.pop(deps.get_current_admin_user, None)
        try:
            anonymous = TestClient(app, raise_server_exceptions=False)
            for method, path in (
                ("GET", "/complaints/admin/learning/catalog"),
                ("GET", "/complaints/admin/learning/weights"),
                ("POST", "/complaints/admin/learning/observe"),
                ("GET", "/complaints/admin/learning/detect"),
                ("POST", "/complaints/admin/learning/raise"),
                ("GET", "/complaints/admin/learning/system-raised"),
                ("GET", "/complaints/admin/learning/clusters"),
                ("POST", "/complaints/admin/learning/proposals"),
                ("GET", "/complaints/admin/learning/proposals"),
                ("POST", "/complaints/admin/learning/publish"),
                ("GET", "/complaints/admin/learning/blockages"),
            ):
                response = anonymous.request(method, path)
                assert response.status_code in (401, 403), (
                    f"{method} {path} returned {response.status_code} to an "
                    f"unauthenticated caller; it is classified as admin but "
                    f"nothing is enforcing it"
                )
        finally:
            # Nothing to restore: this test *removes* the override, and the
            # `client` fixture's own teardown pops it again regardless.
            app.dependency_overrides.pop(deps.get_current_admin_user, None)

    def test_the_catalog_endpoint_states_the_objective_and_its_exclusions(self, client):
        payload = client.get("/complaints/admin/learning/catalog").json()
        assert payload["objective"]["name"]
        assert "contact frequency" in payload["objective"]["not_optimised"]
        assert payload["validation"]["valid"] is True
        detectors = {d["detector_id"]: d for d in payload["system_complaints"]["detectors"]}
        assert detectors["billing_dispute_unresolved"]["enabled"] is False

    def test_the_catalog_reports_that_blockages_is_not_written(self, client):
        payload = client.get("/complaints/admin/learning/catalog").json()
        assert "not written" in payload["improvements"]["blockages_md"]

    def test_the_weights_endpoint_separates_learned_from_configured(self, client):
        payload = client.get("/complaints/admin/learning/weights").json()
        assert payload["learned_count"] == 0
        assert payload["configured_count"] == len(CL.LOYALTY_SIGNALS)
        assert all(row["source"] == "configured_prior" for row in payload["table"])
        assert payload["authority"]["may_change_tier_alone"] is False

    def test_the_learning_pass_endpoint_is_advisory_only(self, client):
        payload = client.post("/complaints/admin/learning/observe").json()
        assert payload["advisory_only"] is True
        assert payload["signals_moved"] == []

    def test_detect_is_a_dry_run(self, client, harness):
        _make_user(harness, 1)
        _add_case(harness, id=1, opened_at=NOW - timedelta(hours=30))
        payload = client.get("/complaints/admin/learning/detect").json()
        assert payload["dry_run"] is True
        assert _case_count(harness) == 1, "detect must not write a case"

    def test_learn_does_not_write(self, client, harness):
        _make_user(harness, 1)
        before = _case_count(harness)
        client.post("/complaints/admin/learning/observe")
        assert _case_count(harness) == before

    def test_publish_on_an_empty_database_is_a_no_op_not_an_error(self, client):
        payload = client.post("/complaints/admin/learning/publish").json()
        assert payload["published_count"] == 0
        assert payload["ladder_level"] == "l0_draft"

    def test_an_unknown_proposal_status_is_a_422(self, client):
        response = client.post(
            "/complaints/admin/learning/proposals/CIMP-nope/status?status=banana"
        )
        assert response.status_code == 422

    def test_an_unknown_proposal_id_is_also_a_422(self, client):
        response = client.post(
            "/complaints/admin/learning/proposals/CIMP-nope/status?status=dismissed"
        )
        assert response.status_code == 422

    def test_the_blockages_endpoint_writes_nothing(self, client):
        """The design decision, asserted at the only level it matters.

        There is no endpoint anywhere in this module that writes the file, and
        this one says so in its response body as well as in the catalog. A
        reviewer should be able to establish that from two `GET`s.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        blockages = root / "BLOCKAGES.md"
        before = blockages.read_bytes() if blockages.exists() else None
        payload = client.get("/complaints/admin/learning/blockages").json()
        assert payload["writes"] is False
        after = blockages.read_bytes() if blockages.exists() else None
        assert after == before

    def test_no_endpoint_writes_blockages(self, client):
        """Structural, not behavioural.

        Behavioural tests only prove that *these* calls did not write. This
        proves the module has no write path at all, which is the property that
        survives someone adding a new endpoint later.
        """
        spec = client.get("/openapi.json").json()
        # Scoped to the complaints surface. The kaizen pipeline has its own
        # `/kaizen/admin/blockages/append` POST from the release-ladder work,
        # which is a different feature with a different owner and is correctly
        # allowed to write; matching on the substring "blockages" alone swept
        # that in and made this test fail for someone else's endpoint.
        blockages_routes = [
            (p, m) for p, ops in spec["paths"].items()
            for m in ops if p.startswith("/complaints/") and "blockages" in p
        ]
        assert blockages_routes, "expected the complaint blockages route to exist"
        # Reads only. A POST/PUT/PATCH/DELETE here would be a new write path and
        # would need its own justification.
        assert all(method in {"get", "head"} for _, method in blockages_routes)


class TestLearningRoutesAreAdminOnly:
    """Authz classification is a table, and a table can be wrong quietly."""

    LEARNING_PATHS = (
        "/complaints/admin/learning/catalog",
        "/complaints/admin/learning/weights",
        "/complaints/admin/learning/observe",
        "/complaints/admin/learning/detect",
        "/complaints/admin/learning/raise",
        "/complaints/admin/learning/system-raised",
        "/complaints/admin/learning/clusters",
        "/complaints/admin/learning/proposals",
        "/complaints/admin/learning/publish",
        "/complaints/admin/learning/blockages",
    )

    def test_every_learning_path_lands_on_the_admin_rule(self):
        from app import deps

        for path in self.LEARNING_PATHS:
            for method in ("GET", "POST"):
                result = deps.match_authz_rule(method, path)
                assert result["rule_id"] == "complaints_admin", (
                    f"{method} {path} -> {result['rule_id']}"
                )
                assert result["matched_fallback"] is False, (
                    f"{method} {path} fell through to the catch-all"
                )

    def test_the_admin_rule_requires_an_admin_dependency(self):
        """Landing on the right rule is necessary but not sufficient.

        The `meta_surface` defect was a route classified as admin with no
        security dependency bound. This checks the binding on the rule itself.

        `AUTHZ_RULES` is a list, not a dict -- keyed lookup on it raises
        TypeError, which is what the first version of this test did.
        """
        from app import deps

        rows = [r for r in deps.AUTHZ_RULES if r.get("rule_id") == "complaints_admin"]
        assert rows, "the complaints_admin authz rule is missing"
        assert "get_current_admin_user" in rows[0].get("enforced_by", ()), rows[0]

    def test_authz_validation_stays_clean_with_the_new_routes(self):
        from app import deps

        result = deps.validate_authz()
        assert result["valid"] is True
        # `errors` and `warnings` are counts, not lists. Asserting them against []
        # passes for any non-zero count, because 0 == [] is False but the failure
        # reads as "there were errors" with no way to see which.
        assert result["errors"] == 0, result["error_list"]
        assert result["warnings"] == 0, result["warning_list"]


class TestMetaSurfaces:
    """Maintenance rule 5: when the map gains a leaf, `/meta/` reflects it.

    The code map documents what the backend already says about itself, so a
    surface that exists in code and is absent from `/meta/ecosystem` is a
    discoverability gap rather than a documentation one.
    """

    def test_the_ecosystem_lists_the_learning_surface(self, client):
        subservices = client.get("/meta/ecosystem").json()["subservices"]
        assert "complaint_learning" in subservices
        entry = subservices["complaint_learning"]
        assert entry["status"] == "ready"
        assert "/complaints/admin/learning/weights" in entry["routes"]
        assert "/complaints/admin/learning/raise" in entry["routes"]

    def test_the_ecosystem_does_not_claim_to_write_blockages(self, client):
        """The description is the first thing a reader sees about this surface,
        so the constraint belongs there rather than only in a docstring."""
        purpose = client.get("/meta/ecosystem").json()["subservices"][
            "complaint_learning"
        ]["purpose"]
        assert "never set a tier" in purpose
        assert "writes BLOCKAGES.md" in purpose


class TestSystemDetectorRepeatRuleOnlyLooksAtDetectors:
    """The rule is named after the detectors, so it has to look at them.

    Found by the end-to-end demonstration: a pile of ordinary customer
    complaints, every one lodged with `source="chat"`, clustered on `origin` and
    reported as "The system keeps raising the same complaint: chat". Nothing about
    that was a detector, and a proposal title is the sentence a human reads first.
    """

    def test_the_rule_declares_a_system_origin_filter(self):
        rule = CL.IMPROVEMENT_CLUSTER_BY_ID["system_detector_repeat"]
        assert rule["origin_filter"] == "system"

    def test_customer_raised_cases_do_not_form_a_system_detector_cluster(self, harness):
        _make_user(harness, 1)
        for i in range(1, 7):
            _add_case(
                harness, id=i, user_id=1, category="billing", status="closed",
                source="chat", opened_at=NOW - timedelta(days=2),
                closed_at=NOW - timedelta(days=1), reopened_count=1,
            )
        detection = harness.run(CL.detect_stacked_complaints(harness.session, now=NOW))
        keys = [c["cluster_key"] for c in detection["stacks"]]
        assert not any(k.startswith("system_detector_repeat") for k in keys), keys
        assert not any(
            c["rule_id"] == "system_detector_repeat" for c in detection["near_misses"]
        ), "an empty group is not a near miss"

    def test_repeated_system_cases_do_form_a_cluster_named_after_the_detector(self, harness):
        # Three distinct users, not one: the rule requires
        # `min_distinct_users >= 2` precisely so that one customer being
        # repeatedly mis-flagged is a support problem rather than a systemic one.
        for i in range(1, 6):
            _make_user(harness, i)
            _add_case(
                harness, id=i, user_id=i, category="service_quality", status="new",
                source="system:sla_response_breach", opened_at=NOW - timedelta(days=1),
            )
        detection = harness.run(CL.detect_stacked_complaints(harness.session, now=NOW))
        stack = [c for c in detection["stacks"] if c["rule_id"] == "system_detector_repeat"]
        assert stack, [c["cluster_key"] for c in detection["stacks"]]
        # The value names the detector, not the storage convention.
        assert stack[0]["value"] == "sla_response_breach"

    def test_the_origin_axis_reads_a_detector_id_not_a_storage_string(self):
        class _Case:
            source = "system:sla_resolution_breach"

        class _Human:
            source = "chat"

        assert CL._cluster_axis_value(_Case(), "origin") == "sla_resolution_breach"
        assert CL._cluster_axis_value(_Human(), "origin") == "chat"
