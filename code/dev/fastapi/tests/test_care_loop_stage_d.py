"""Stage D: the closed loop.

The five items, and the tests are mostly negative controls because that is where
this subsystem can go wrong:

* **Preference fail-closed** -- the finding is a count, not a theory:
  ``customer_offers`` was the only service in the codebase that consulted the
  preference centre. These tests pin that every *declared* proactive path now
  calls the gate, and that the gate fails closed rather than assuming permission.
* **Offer outcomes → promotion** -- a naive accept rate rewards making fewer
  offers. Three real bugs were found by the module's own validator and are pinned:
  the Wilson formula was not the closed form, ``fulfilled`` was excluded from
  ``decided`` so a perfectly-delivered tier ranked as *no evidence*, and the
  generosity bucket compared each rule against itself.
* **Journey outcome loop** -- completion means "reached ``status`` at least once",
  read from history, because a cycle on its fourth lap is not zero completions.
* **Care weights** -- emphasis, not verdict. The three refusals come from
  ``complaint_learning`` and the validator reads that table rather than restating
  it, so relaxing one upstream fails here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _offer(source, status, *, kind="goodwill", scale=1.0, age=10, expires="2026-12-01"):
    return {
        "source_offer_id": source,
        "offer_kind": kind,
        "status": status,
        "generosity_scale": scale,
        "issued_at": (NOW - timedelta(days=age)).isoformat(),
        "expires_at": f"{expires}T00:00:00+00:00",
        "points": 250.0,
    }


# ===========================================================================
# Preference fail-closed
# ===========================================================================


class TestEveryProactivePathIsGated:
    def test_the_finding_was_a_count_and_not_a_theory(self):
        """`customer_offers` was the only service that asked.

        Recorded here because the whole module exists because of a grep, and the
        grep is the kind of fact that stops being true silently.
        """
        from app import deps
        from app.main import app

        paths = {path for path, _ in deps.iter_authz_routes(app.routes)}
        for path in (
            "/chat/me/offers",
            "/chat/admin/recovery/offers",
            "/kaizen/admin/care-gate/consult",
        ):
            assert path in paths, path

    def test_every_declared_path_calls_the_gate(self):
        """Read from source, like the authz drift report.

        A property that can be checked mechanically is worth enforcing
        mechanically; one nobody checks is a comment.
        """
        from app.services import care_gate

        report = care_gate.validate_care_gate()
        assert report["valid"] is True, report["error_list"]
        assert report["coverage"]["ungated"] == []
        assert report["gated"] == report["paths"]

    def test_an_ungated_path_is_an_error(self):
        """The negative control: remove the call and the validator must notice."""
        from app.services import care_gate

        saved = care_gate.PROACTIVE_PATHS
        care_gate.PROACTIVE_PATHS = (
            {**dict(saved[0]), "function": "an_unconsulted_function"},
        ) + tuple(dict(row) for row in saved)
        try:
            report = care_gate.validate_care_gate()
            assert report["valid"] is False
            assert any(
                "does not resolve" in error or "never calls" in error
                for error in report["error_list"]
            )
        finally:
            care_gate.PROACTIVE_PATHS = saved

    def test_every_path_says_what_it_pushes(self):
        from app.services import care_gate

        for row in care_gate.PROACTIVE_PATHS:
            assert str(row["pushes"]).strip(), row["path_id"]


class TestTheGateFailsClosed:
    def test_unreadable_preferences_mean_do_not_push(self):
        """Not "assume it is fine".

        Not knowing how somebody wants to be contacted is a reason not to
        interrupt them. An unreadable profile and an empty one are different
        facts, and only the first fails closed.
        """
        from app.services import care_gate

        gate = care_gate.consult("recovery_outreach", None, None)
        assert gate["mode"] == "preferences_missing"
        assert gate["push"] is False
        # And the thing itself still exists: we still owe them the fix.
        assert gate["issue"] is True

    def test_an_empty_profile_is_not_the_same_as_an_unreadable_one(self):
        """Silence is not a preference.

        Treating an unset preference as "do not contact me" would mean nobody is
        ever contacted until they opt in, which is a different product with a
        different consent model.
        """
        from app.services import care_gate

        unreadable = care_gate.consult("recovery_outreach", None, None)
        empty = care_gate.consult("recovery_outreach", {}, {})
        assert unreadable["push"] is False
        assert empty["mode"] == "preferences_read"
        assert empty["push"] is True

    def test_an_unregistered_path_cannot_push(self):
        """A new proactive surface that forgot to register is the case this
        module exists for.
        """
        from app.services import care_gate

        gate = care_gate.consult("brand_new_surface", {}, {}, purpose="marketing")
        assert gate["declared"] is False
        assert gate["push"] is False
        assert "not a registered proactive path" in gate["reasons"][0]

    def test_consent_never_withholds_a_recovery_or_service_thing(self):
        from app.services import care_gate

        for path_id in ("recovery_outreach", "recovery_callback", "offer_notification"):
            gate = care_gate.consult(
                path_id, {}, {name: False for name in ("marketing", "analytics")},
                purpose="recovery",
            )
            assert gate["issue"] is True, path_id
            assert gate["service_critical"] is True, path_id

    def test_a_consent_gated_purpose_is_not_issued_at_all(self):
        """The other half of the split, kept from Stage B.

        Creating a marketing offer whose consent is absent produces something that
        can never be sent.
        """
        from app.services import care_gate

        gate = care_gate.consult(
            "recovery_outreach", {}, {"marketing": False}, purpose="marketing"
        )
        assert gate["issue"] is False
        assert gate["push"] is False

    def test_reactive_only_defers_the_push_and_keeps_the_thing(self):
        from app.services import care_gate

        gate = care_gate.consult(
            "recovery_outreach",
            {"communication_frequency": "only_reactive"},
            {},
            purpose="recovery",
        )
        assert gate["issue"] is True
        assert gate["push"] is False
        assert any("reactive contact only" in reason for reason in gate["reasons"])

    def test_the_service_critical_purposes_match_the_preference_centre(self):
        """So the gate can report whether the consent exemption applied."""
        from app.services import care_gate, preferences

        for purpose in care_gate.SERVICE_CRITICAL_PURPOSES:
            assert purpose in preferences.CONSENT_PURPOSE_BY_NAME
            assert purpose not in preferences.CONSENT_GATED_PURPOSES


class TestRecoveryOutreachIsGatedNow:
    def test_the_strategy_reports_whether_it_may_push(self):
        """The defect: this picked a channel, a tone and a framing for somebody
        who had already reported a problem, and nothing could stop it except a rule
        pack about complaint frequency.
        """
        from app.services import recovery_playbooks

        context = {"recovery_readiness": "high"}
        ungated = recovery_playbooks.resolve_recovery_outreach_strategy(context)
        assert ungated["push_permitted"] is False
        assert ungated["gate"]["mode"] == "preferences_missing"

        readable = recovery_playbooks.resolve_recovery_outreach_strategy(
            context, preferences_map={}, consents={}
        )
        assert readable["push_permitted"] is True

        quiet = recovery_playbooks.resolve_recovery_outreach_strategy(
            context, preferences_map={"communication_frequency": "only_reactive"}, consents={}
        )
        assert quiet["push_permitted"] is False

    def test_a_callback_is_a_proactive_contact_with_a_deadline(self):
        """The version hardest to decline once it has been dialled."""
        from app.services import recovery_playbooks

        result = recovery_playbooks.resolve_recovery_callback_plan(
            {"recovery_readiness": "high"}
        )
        assert result["push_permitted"] is False
        # Still resolved: an operator scheduling work needs to see the plan.
        assert result["plan_id"]

    def test_the_counsel_and_the_notes_survive_gating(self):
        """The gate changes `push_permitted` and nothing an operator reads.

        An operator looking at this panel needs to see what *would* be sent.
        """
        from app.services import recovery_playbooks

        plain = recovery_playbooks.resolve_recovery_outreach_strategy(
            {"recovery_readiness": "high"}, preferences_map={}, consents={}
        )
        ungated = recovery_playbooks.resolve_recovery_outreach_strategy(
            {"recovery_readiness": "high"}
        )
        assert plain["channel"] == ungated["channel"]
        assert plain["tone"] == ungated["tone"]
        assert plain["resolved_layer"] == ungated["resolved_layer"]


# ===========================================================================
# Offer outcomes → promotion
# ===========================================================================


class TestWilsonBound:
    def test_it_never_exceeds_the_observed_rate(self):
        """The bound must not claim more confidence than the data supports.

        The first version used a centre-minus-margin decomposition that looked
        equivalent and returned 0.0116 for a 0/10 record -- a bound *above* the
        observed rate of zero.
        """
        from app.services import offer_outcomes

        for successes, trials in ((0, 10), (0, 3), (1, 1), (5, 10), (50, 100), (2, 40)):
            bound = offer_outcomes.wilson_lower_bound(successes, trials)
            assert bound <= successes / trials + 1e-9, (successes, trials, bound)

    def test_it_separates_evidence_volume_at_the_same_proportion(self):
        """The whole reason for using a bound rather than a rate.

        `1/1 = 100%` and `1000/1000 = 100%` are the same rate and mean completely
        different things. The first version returned 1.0 for both.
        """
        from app.services import offer_outcomes

        assert offer_outcomes.wilson_lower_bound(1, 1) < offer_outcomes.wilson_lower_bound(1000, 1000)
        assert offer_outcomes.wilson_lower_bound(1, 2) < offer_outcomes.wilson_lower_bound(500, 1000)

    def test_the_validator_checks_its_own_formula(self):
        from app.services import offer_outcomes

        assert offer_outcomes.validate_offer_outcomes()["valid"] is True


class TestRanking:
    def _rows(self):
        rows = []
        # Offered a lot, accepted, barely fulfilled: a *fulfilment* problem.
        for index in range(40):
            rows.append(_offer("save_critical", "fulfilled" if index < 6 else "accepted", scale=1.5))
        # Rarely offered, always delivered: high rate, low volume.
        for _ in range(6):
            rows.append(_offer("save_standard", "fulfilled"))
        # Offered a lot, refused.
        for _ in range(30):
            rows.append(_offer("save_high", "declined"))
        # Two outcomes: no evidence.
        rows.append(_offer("save_low", "fulfilled"))
        rows.append(_offer("save_low", "fulfilled"))
        return rows

    def test_a_fulfilled_offer_counts_as_a_decision(self):
        """Bug: excluding it reported a perfectly-delivered tier as no evidence.

        An offer that reached fulfilled was accepted first, so `decided: 0` for six
        fulfilled offers is simply wrong.
        """
        from app.services import offer_outcomes

        ranked = {row["source_offer_id"]: row for row in offer_outcomes.rank_offer_outcomes(self._rows())}
        assert ranked["save_standard"]["decided"] == 6
        assert ranked["save_standard"]["unranked"] is False

    def test_a_refused_tier_is_recommended_for_retirement(self):
        from app.services import offer_outcomes

        ranked = {row["source_offer_id"]: row for row in offer_outcomes.rank_offer_outcomes(self._rows())}
        assert ranked["save_high"]["recommendation"] == "retire"
        assert ranked["save_high"]["decided"] >= 25

    def test_high_acceptance_with_poor_fulfilment_is_held_not_promoted(self):
        """The two scores are not combined, and this is why.

        Averaging them would call this "mediocre" and point the operator at the
        offer volume instead of at us. We are accepting and then not delivering,
        and offering *more* would make it worse.
        """
        from app.services import offer_outcomes

        ranked = {row["source_offer_id"]: row for row in offer_outcomes.rank_offer_outcomes(self._rows())}
        critical = ranked["save_critical"]
        assert critical["acceptance_bound"] > 0.8
        assert critical["fulfilment_bound"] < 0.2
        assert critical["recommendation"] == "hold"

    def test_thin_evidence_is_provisional_even_when_the_rate_is_perfect(self):
        from app.services import offer_outcomes

        ranked = {row["source_offer_id"]: row for row in offer_outcomes.rank_offer_outcomes(self._rows())}
        assert ranked["save_standard"]["provisional"] is True
        # Acting on six observations is reacting to noise.
        assert ranked["save_standard"]["recommendation"] == "hold"
        assert ranked["save_low"]["unranked"] is True

    def test_expiry_is_not_a_decline(self):
        """An offer nobody opened is evidence about the channel and the timing, not
        about whether the customer wanted it.
        """
        from app.services import offer_outcomes

        rows = [_offer("t", "expired", expires="2026-09-01") for _ in range(10)]
        rows.append(_offer("t", "fulfilled"))
        summary = offer_outcomes.summarise_outcomes(rows, now=NOW)
        counts = summary["by_source"]["t"]
        assert counts["expired"] == 10
        # Ten expiries contribute nothing to the decision count.
        assert counts["accepted"] + counts["declined"] + counts["fulfilled"] == 1

    def test_an_expired_clock_is_read_even_before_it_is_swept(self):
        """The same lesson as the offer inbox: read the clock, not the stored
        status, or a stale row keeps counting as open.
        """
        from app.services import offer_outcomes

        rows = [
            {"source_offer_id": "t", "status": "offered", "offer_kind": "goodwill",
             "issued_at": "2026-08-01T00:00:00+00:00", "expires_at": "2026-09-01T00:00:00+00:00"}
        ]
        summary = offer_outcomes.summarise_outcomes(rows, now=NOW)
        assert summary["by_source"]["t"]["expired"] == 1
        assert summary["by_source"]["t"]["open"] == 0

    def test_nothing_is_applied(self):
        """An engine that silently retunes itself from its own outputs makes the
        offer table unauditable.
        """
        from app.services import offer_outcomes

        changes = offer_outcomes.recommend_scale_adjustments(
            offer_outcomes.rank_offer_outcomes(self._rows())
        )
        assert changes
        for change in changes:
            assert change["applied"] is False
            assert change["reason"]


class TestGenerosityRanking:
    def test_it_actually_attributes_outcomes_to_a_bucket(self):
        """Bug: it compared each rule's bucket against *itself*.

        Every rule then reported identical numbers and the ranking was
        decorative -- a report that looks like an analysis and measures nothing.
        """
        from app.services import offer_outcomes

        rows = [_offer("a", "fulfilled", scale=1.5) for _ in range(10)]
        rows += [_offer("b", "fulfilled", scale=1.0) for _ in range(10)]
        rows += [_offer("c", "accepted", scale=1.5) for _ in range(10)]
        ranked = {row["rule_id"]: row for row in offer_outcomes.rank_generosity_rules(rows)}
        high = ranked["generosity_strategic"]
        standard = ranked["generosity_default"]
        assert high["scale_bucket"] == "high"
        assert standard["scale_bucket"] == "standard"
        # Different buckets, therefore different numbers.
        assert high["delivered"] != standard["delivered"]
        assert high["fulfilment_bound"] != standard["fulfilment_bound"]

    def test_the_attribution_is_declared_weak(self):
        """Offers record the scale, not the rule that chose it."""
        from app.services import offer_outcomes

        ranked = offer_outcomes.rank_generosity_rules([_offer("a", "fulfilled", scale=1.5)])
        assert all("scale bucket" in row["outcome_attribution"] for row in ranked)


# ===========================================================================
# Journey outcome loop
# ===========================================================================


class TestJourneyOutcomes:
    def test_completion_is_read_from_history(self):
        """A cycle sitting in at_risk on its fourth lap is not zero completions."""
        from app.services import offer_outcomes

        states = [
            {
                "user_id": index,
                "stage": "at_risk",
                "cycles": 3,
                "entered_stage_at": (NOW - timedelta(hours=2)).isoformat(),
                "history": [
                    {"stage": "offer", "at": "2026-09-01T00:00:00+00:00"},
                    {"stage": "follow_up", "at": "2026-09-01T01:00:00+00:00"},
                    {"stage": "status", "at": "2026-09-01T02:00:00+00:00"},
                    {"stage": "at_risk", "at": "2026-09-02T00:00:00+00:00"},
                ],
            }
            for index in range(3)
        ]
        report = offer_outcomes.journey_outcome_report(states)
        assert report["completed_now"] == 0
        assert report["completed_ever"] == 3
        assert report["completion_rate"] == 1.0
        assert report["max_cycles"] == 3

    def test_stuck_stages_get_different_actions(self):
        """`follow_up` being sticky means we accepted and did not deliver.

        `offer` being sticky means we are asking and not hearing. Same symptom,
        opposite response, so a single generic "stuck" would be useless.
        """
        from app.services import offer_outcomes

        states = [
            {
                "user_id": 1,
                "stage": "follow_up",
                "cycles": 0,
                "entered_stage_at": (NOW - timedelta(days=9)).isoformat(),
                "history": [],
            },
            {
                "user_id": 2,
                "stage": "offer",
                "cycles": 0,
                "entered_stage_at": (NOW - timedelta(days=9)).isoformat(),
                "history": [],
            },
        ]
        report = offer_outcomes.journey_outcome_report(states)
        assert report["stuck_count"] == 2
        actions = {row["user_id"]: row["action"] for row in report["stuck"]}
        assert "we are behind" in actions[1]
        assert "not the customer ignoring us" in actions[2]

    def test_a_healthy_stage_is_not_stuck(self):
        from app.services import offer_outcomes

        states = [
            {
                "user_id": 1,
                "stage": "offer",
                "cycles": 0,
                "entered_stage_at": (NOW - timedelta(hours=1)).isoformat(),
                "history": [],
            }
        ]
        assert offer_outcomes.journey_outcome_report(states)["stuck_count"] == 0


# ===========================================================================
# Care weights
# ===========================================================================


class TestCareWeights:
    def test_a_learned_weight_raises_attempt_and_never_lowers_it(self):
        """A weight system that can learn to care about somebody less is a weight
        system that eventually will.
        """
        from app.services import care_weights

        loud = care_weights.resolve_care_weights({"complainant_left": 4.0})
        quiet = care_weights.resolve_care_weights({"complainant_left": 0.05})
        assert loud["recovery_emphasis"] > 1.0
        assert quiet["recovery_emphasis"] == 1.0
        assert quiet["dimensions"]["recovery_emphasis"]["at_floor"] is True

    def test_nothing_may_push_a_checkback_past_the_critical_sla(self):
        from app.services import care_weights, relationship_health

        sla = relationship_health.RELATIONSHIP_HEALTH_BANDS_BY_ID["critical"]["sla_hours"]
        loud = care_weights.resolve_care_weights(
            {name: 4.0 for name in ("unresolved_stale", "unowned", "sla_response_breached", "sla_resolution_breached")}
        )
        assert loud["follow_up_hours"] >= float(sla)

    def test_a_sentiment_cliff_leans_towards_acknowledging_first(self):
        """The first thing a customer who feels dismissed hears is another
        explanation.
        """
        from app.services import care_weights

        result = care_weights.resolve_care_weights({"sentiment_cliff": 3.0})
        assert result["acknowledged_first"] is True
        assert result["dimensions"]["communication_frame"]["contributors"]

    def test_an_absent_signal_leaves_the_prior_untouched(self):
        """Not an error and not zero: an absent signal is not evidence of a
        pattern.
        """
        from app.services import care_weights

        result = care_weights.resolve_care_weights({})
        assert result["recovery_emphasis"] == 1.0
        assert result["moved"] == []
        assert result["unapplied"] == []

    def test_a_rule_naming_a_signal_that_does_not_exist_is_reported_not_dropped(self):
        """A weight pointing at a renamed signal applies to nothing, and silence
        hides that.
        """
        from app.services import care_weights

        saved = care_weights.CARE_WEIGHT_RULES
        care_weights.CARE_WEIGHT_RULES = (
            {**dict(saved[0]), "rule_id": "ghost", "signal_id": "no_such_signal",
             "dimension": "recovery_emphasis"},
        ) + tuple(dict(row) for row in saved)
        try:
            result = care_weights.resolve_care_weights({"no_such_signal": 3.0})
            assert any(row["rule_id"] == "ghost" for row in result["unapplied"])
        finally:
            care_weights.CARE_WEIGHT_RULES = saved

    def test_the_three_refusals_come_from_the_upstream_table(self):
        """Read from ``LEARNED_WEIGHT_AUTHORITY``, not restated -- so relaxing one
        upstream fails here until someone decides what it means.
        """
        from app.services import care_gate, care_weights, complaint_learning

        report = care_weights.validate_care_weights()
        assert report["valid"] is True, report["error_list"]
        for power in care_weights.REFUSED_LEARNED_POWERS:
            assert complaint_learning.LEARNED_WEIGHT_AUTHORITY[power] is False

    def test_the_validator_fails_if_a_refusal_is_relaxed_upstream(self):
        from app.services import care_weights, complaint_learning

        saved = dict(complaint_learning.LEARNED_WEIGHT_AUTHORITY)
        complaint_learning.LEARNED_WEIGHT_AUTHORITY["may_auto_escalate"] = True
        try:
            report = care_weights.validate_care_weights()
            assert report["valid"] is False
            assert any("may_auto_escalate" in error for error in report["error_list"])
        finally:
            complaint_learning.LEARNED_WEIGHT_AUTHORITY.clear()
            complaint_learning.LEARNED_WEIGHT_AUTHORITY.update(saved)

    def test_caring_emphasis_is_a_proactive_contact_and_asks_the_gate(self):
        """Changing the framing of an unprompted message is a form of contact."""
        from app.services import care_weights

        gated = care_weights.resolve_care_weights({"complainant_left": 3.0})
        assert gated["push_permitted"] is False
        assert gated["gate"]["mode"] == "preferences_missing"

        readable = care_weights.resolve_care_weights(
            {"complainant_left": 3.0}, preferences_map={}, consents={}
        )
        assert readable["push_permitted"] is True

    def test_the_signal_vocabulary_is_read_not_restated(self):
        """A local copy drifts on the first rename."""
        from app.services import care_weights

        report = care_weights.validate_care_weights()
        assert report["valid"] is True, report["error_list"]
        assert care_weights._signal_metadata()
        for rule in care_weights.CARE_WEIGHT_RULES:
            assert rule["signal_id"] in care_weights._signal_metadata()


# ===========================================================================
# Routes
# ===========================================================================


class TestStageDRoutes:
    def test_the_gate_report_lists_every_path_and_is_all_gated(self, admin_client):
        response = admin_client.get("/kaizen/admin/care-gate")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ungated"] == []
        assert body["gated"] == body["total"] >= 5
        assert body["validation"]["valid"] is True

    def test_the_consult_endpoint_distinguishes_unreadable_from_empty(self, admin_client):
        unreadable = admin_client.post(
            "/kaizen/admin/care-gate/consult", json={"path_id": "recovery_outreach"}
        ).json()
        empty = admin_client.post(
            "/kaizen/admin/care-gate/consult",
            json={"path_id": "recovery_outreach", "preferences": {}},
        ).json()
        assert unreadable["push"] is False
        assert unreadable["mode"] == "preferences_missing"
        assert empty["push"] is True

    def test_the_outcomes_endpoint_ranks_and_applies_nothing(self, admin_client):
        response = admin_client.post(
            "/kaizen/admin/offer-outcomes",
            json={"offers": [_offer("t", "declined") for _ in range(30)]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["rankings"][0]["recommendation"] == "retire"
        assert all(change["applied"] is False for change in body["recommended_changes"])
        assert "journey" in body

    def test_care_is_a_first_class_measurement_source(self, admin_client):
        """So a gate about the care loop cannot be satisfied by typing a number."""
        sources = admin_client.get("/kaizen/admin/catalog").json()["measurement_sources"]
        assert "care" in sources
        assert len(sources["care"]) > 30

    def test_from_app_care_records_measured_values(self, admin_client):
        from app import release_ladder

        admin_client.post("/kaizen/admin/candidates", json={"candidate_id": "sd1", "commit": "aaa"})
        response = admin_client.post(
            "/kaizen/admin/candidates/sd1/measure", json={"from_app": ["care"]}
        )
        assert response.status_code == 200, response.text
        candidate = release_ladder.find_candidate("sd1")
        assert "care_offers_seen" in candidate.measured
        assert "care_journey_completion_rate" in candidate.measured
        # And app-computed wins, so a hand-typed number cannot override it.
        admin_client.post(
            "/kaizen/admin/candidates/sd1/measure",
            json={"measured": {"care_offers_seen": 9999}, "from_app": ["care"]},
        )
        assert release_ladder.find_candidate("sd1").measured["care_offers_seen"] != 9999

@pytest.fixture()
def admin_client():
    from fastapi.testclient import TestClient

    from app import deps
    from app.main import app

    class _Admin:
        id = 1
        username = "tester"
        is_admin = True

    async def _admin():
        return _Admin()

    app.dependency_overrides[deps.get_current_admin_user] = _admin
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(deps.get_current_admin_user, None)
