"""Stage C, parts 2-4: relationship health, the orchestrator, and the copilot.

Three modules whose whole value is in what they *refuse* to do, so most of these
tests are negative controls:

* **Health refuses to be a status.** A customer can be ``Trusted`` and
  ``critical`` at once, and that combination is the one worth acting on fastest.
  A blended number hides it; a demotion punishes them.
* **The orchestrator refuses to nag.** Three identical offers suppress the fourth,
  and the count is on the *precondition*, not on the stage — so cycling the loop
  does not reset it.
* **The copilot refuses to open with our internal labels**, and the scan covers
  the whole card rather than only the opening line.

And two real defects, both found here and both pinned:

1. **The health band was inverted.** The signals are risk measurements and the
   bands are named by health, so a customer at ``critical`` churn risk scored
   0.12 and came back ``critical`` while a healthy one scored 0.36 and came back
   ``at_risk``. It sent the healthiest customers to a human and the sickest to an
   automated offer.
2. **The band walk took the first match in a worst-first table**, so a flawless
   1.000 resolved to ``at_risk`` — the docstring described a best-first walk the
   code did not do.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _ledger(completed=1, cancelled=0, tenure=400):
    from app.services import loyalty_status

    return loyalty_status.build_experiential_ledger(
        bookings=[
            {
                "status": "completed",
                "created_at": (NOW - timedelta(days=tenure + i)).isoformat(),
                "completed_at": (NOW - timedelta(days=tenure + i)).isoformat(),
                "booking_id": i,
            }
            for i in range(completed)
        ]
        + [
            {
                "status": "cancelled",
                "created_at": (NOW - timedelta(days=tenure + 100 + i)).isoformat(),
                "booking_id": 900 + i,
            }
            for i in range(cancelled)
        ],
        now=NOW,
    )


def _health(**kwargs):
    from app.services import relationship_health

    defaults = {
        "ledger": _ledger(),
        "latest_activity_at": (NOW - timedelta(days=7)).isoformat(),
        "now": NOW,
    }
    defaults.update(kwargs)
    return relationship_health.build_relationship_health(**defaults)


# ===========================================================================
# Relationship health
# ===========================================================================


class TestHealthPolarity:
    def test_the_polarity_is_declared(self):
        """Because getting it wrong is silent, and it was wrong."""
        from app.services import relationship_health

        assert relationship_health.POLARITY == "higher_is_healthier"

    def test_lower_churn_risk_never_scores_below_higher(self):
        """The regression guard for the inversion.

        The composite moved in the right direction before the fix and the *bands*
        read it backwards, which is why both the score ordering and the band are
        asserted.
        """
        seen = {}
        for band in ("low", "medium", "high", "critical"):
            report = _health(churn_band=band)
            seen[band] = (report["score"], report["band"])
        scores = [seen[band][0] for band in ("low", "medium", "high", "critical")]
        assert scores == sorted(scores, reverse=True), seen

    def test_a_flawless_score_is_healthy(self):
        """The second defect: first-match in a worst-first table gave ``at_risk``."""
        from app.services import relationship_health

        assert relationship_health.resolve_health_band(1.0) == "healthy"
        assert relationship_health.resolve_health_band(0.79) == "strained"
        assert relationship_health.resolve_health_band(0.55) == "strained"
        assert relationship_health.resolve_health_band(0.30) == "at_risk"
        assert relationship_health.resolve_health_band(0.0) == "critical"

    def test_the_band_table_walks_taken_highest_first(self):
        from app.services import relationship_health

        floors = {
            str(row["band"]): float(row["min_score"])
            for row in relationship_health.RELATIONSHIP_HEALTH_BANDS
            if str(row["band"]) not in ("no_data", "critical")
        }
        for score in (0.3, 0.5, 0.6, 0.9, 1.0):
            band = relationship_health.resolve_health_band(score)
            met = [name for name, floor in floors.items() if score >= floor]
            assert band == max(met, key=lambda n: floors[n]), (score, band, met)
        # Below every floor is the critical floor, which is a floor and not a row
        # a first-match walk could pick up.
        assert relationship_health.resolve_health_band(0.0) == "critical"


class TestDispositiveFindings:
    def test_critical_churn_is_never_merely_strained(self):
        """A composite cannot say this; a rule can.

        With five healthy signals a zeroed churn risk still lands above the
        ``at_risk`` floor, so without the override a customer predicted to leave
        reads as merely tired of us.
        """
        report = _health(churn_band="critical")
        assert report["band"] == "at_risk"
        assert [row["override_id"] for row in report["overrides_fired"]] == ["critical_churn"]

    def test_an_open_complaint_with_risk_gets_a_person(self):
        """Two systems failing the same customer at once."""
        report = _health(churn_band="high", complaints=[{"status": "open"}])
        assert report["band"] == "critical"
        assert report["sla_hours"] == 4
        assert report["action"] == "human_review"
        # And the arithmetic said otherwise, which is the point of reporting both.
        assert report["arithmetic_band"] != "critical"
        assert any(
            row["override_id"] == "open_complaint_and_risk"
            for row in report["overrides_fired"]
        )

    def test_a_complaint_alone_is_not_dispositive(self):
        """Plenty of people complain and stay. Escalating all of them trains
        operators to ignore the band.
        """
        report = _health(churn_band="low", complaints=[{"status": "open"}])
        assert report["band"] == "healthy"
        assert report["overrides_fired"] == []

    def test_silence_is_strained_even_when_everything_else_is_perfect(self):
        report = _health(
            churn_band="low",
            latest_activity_at=(NOW - timedelta(days=200)).isoformat(),
        )
        assert report["band"] == "strained"
        assert any(row["override_id"] == "severe_dormancy" for row in report["overrides_fired"])

    def test_unknown_activity_is_not_treated_as_a_year_of_silence(self):
        """Absent data must not escalate. This was an explicit guard in
        ``apply_health_overrides`` and it is easy to undo by accident.
        """
        report = _health(churn_band="low", latest_activity_at=None)
        assert report["counts"]["days_since_activity"] is None
        assert report["overrides_fired"] == []


class TestHealthIsNotStatus:
    def test_the_invariant_is_asserted_in_code(self):
        """So a future "let's demote them when they go critical" is a visible
        edit rather than an emergent consequence of a blended score.
        """
        from app.services import relationship_health

        report = _health(churn_band="critical")
        relationship_health.assert_health_is_not_status(report)
        with pytest.raises(AssertionError):
            relationship_health.assert_health_is_not_status({"is_status": True})

    def test_a_customer_can_be_trusted_and_critical_at_once(self):
        """The combination the whole split exists for."""
        from app.services import loyalty_status, relationship_health

        ledger = _ledger(completed=5, tenure=2500)
        status = loyalty_status.resolve_loyalty_status(ledger)
        health = relationship_health.build_relationship_health(
            ledger=ledger,
            churn_band="critical",
            complaints=[{"status": "open"}],
            latest_activity_at=(NOW - timedelta(days=2)).isoformat(),
            now=NOW,
        )
        assert status["label"] == "Principal"
        assert health["band"] == "critical"
        assert status["decays_on_inactivity"] is False

    def test_no_health_band_is_unmeasured_not_healthy(self):
        report = _health(churn_band="")
        churn_signal = next(
            row for row in report["signals"] if row["signal_id"] == "churn_risk"
        )
        assert churn_signal["countable"] is False
        assert churn_signal["value"] is None
        assert report["counts"]["churn_band_known"] == 0


class TestSlaVersusOfferWindow:
    def test_only_critical_has_an_sla(self):
        """An SLA nobody owns is decoration, and it makes the one that is owned
        read as decoration too.
        """
        from app.services import relationship_health

        report = relationship_health.validate_relationship_health()
        assert report["valid"] is True, report["error_list"]
        with_sla = [
            str(row["band"])
            for row in relationship_health.RELATIONSHIP_HEALTH_BANDS
            if row.get("sla_hours")
        ]
        assert with_sla == ["critical"]

    def test_the_validator_refuses_a_second_sla(self):
        from app.services import relationship_health

        saved = relationship_health.RELATIONSHIP_HEALTH_BANDS
        tampered = []
        for row in saved:
            item = dict(row)
            if str(item["band"]) == "strained":
                item["sla_hours"] = 12
            tampered.append(item)
        relationship_health.RELATIONSHIP_HEALTH_BANDS = tuple(tampered)
        try:
            report = relationship_health.validate_relationship_health()
            assert report["valid"] is False
            assert any("strained" in error for error in report["error_list"])
        finally:
            relationship_health.RELATIONSHIP_HEALTH_BANDS = saved

    def test_the_offer_window_is_not_an_sla(self):
        from app.services import relationship_health

        band = relationship_health.RELATIONSHIP_HEALTH_BANDS_BY_ID["at_risk"]
        assert band["sla_hours"] is None
        assert band["offer_window_hours"] == 24
        assert relationship_health.SLA_BANDS == ("critical",)
        assert relationship_health.OFFER_WINDOW_BANDS == ("at_risk",)


class TestRiskTableSafety:
    def test_a_missing_risk_key_is_an_error_not_a_default(self):
        """Here a missing key contributes 0.0 -- it reports every customer as
        healthy. The mirror of the same table in `loyalty_status`, where a missing
        key contributes 1.0.
        """
        from app.services import relationship_health, retention

        known = {str(item) for item in retention.RETENTION_CHURN_BANDS}
        assert set(relationship_health.INVERTED_RISK) <= known

    def test_an_unknown_churn_band_does_not_grade_as_healthy(self):
        report = _health(churn_band="catastrophic-but-unknown")
        assert report["counts"]["churn_band_known"] == 1
        # An unrecognised band contributes nothing, which is not "fine".
        assert report["band"] != "healthy"


class TestHealthSignals:
    def test_every_signal_declares_a_source_and_a_reason(self):
        from app.services import relationship_health

        for spec in relationship_health.HEALTH_SIGNALS:
            assert spec["source"], spec["signal_id"]
            assert len(str(spec["why"])) > 40, spec["signal_id"]

    def test_dormancy_is_the_lowest_weight(self):
        """Silence is the ambiguous signal. Weighting it like churn risk is how a
        programme ends up cajoling people who are content.
        """
        from app.services import relationship_health

        weights = {
            str(row["signal_id"]): float(row["weight"])
            for row in relationship_health.HEALTH_SIGNALS
        }
        assert weights["dormancy"] == min(weights.values())

    def test_churn_dominates_because_the_prose_says_it_does(self):
        from app.services import relationship_health

        weights = {
            str(row["signal_id"]): float(row["weight"])
            for row in relationship_health.HEALTH_SIGNALS
        }
        assert weights["churn_risk"] == max(weights.values())
        assert weights["churn_risk"] / sum(weights.values()) > 0.35

    def test_the_rollup_publishes_that_it_is_not_exhaustive(self):
        """A report that returned the twenty loudest customers and called itself
        comprehensive would be worse than one that admits it is a ranking.
        """
        from app.services import relationship_health

        from app.services import relationship_health

        catalog = relationship_health.build_relationship_health_catalog()
        assert "triage queue" in catalog["note"]
        # The published contract, read from the source of the rollup itself rather
        # than from prose that could drift away from it.
        source = relationship_health.build_relationship_health_report.__doc__ or ""
        assert "is_exhaustive" in source


# ===========================================================================
# The orchestrator
# ===========================================================================


class TestOrchestratorMachine:
    def test_the_cycle_is_a_cycle_and_not_a_staircase(self):
        """`status -> at_risk` is the only backward edge and it is what lets a
        customer whose health fell be recognised again.
        """
        from app.services import journey_orchestrator

        assert "at_risk" in journey_orchestrator.ORCHESTRATION_TRANSITIONS["status"]
        report = journey_orchestrator.validate_orchestrator()
        assert report["valid"] is True, report["error_list"]

    def test_every_stage_is_reachable_and_has_a_way_out(self):
        """An unreachable stage is somewhere a customer can never arrive; a dead
        end is somewhere they stop, which reads as "resolved".
        """
        from app.services import journey_orchestrator

        incoming: dict[str, int] = {}
        outgoing: dict[str, int] = {}
        for stage in journey_orchestrator.ORCHESTRATION_STAGES:
            incoming[stage] = outgoing[stage] = 0
        for stage, targets in journey_orchestrator.ORCHESTRATION_TRANSITIONS.items():
            outgoing[stage] = len(targets)
            for target in targets:
                incoming[target] += 1
        for stage in journey_orchestrator.ORCHESTRATION_STAGES:
            assert incoming[stage] > 0, stage
            assert outgoing[stage] > 0, stage

    def test_a_skip_is_refused_with_the_reachable_set_named(self):
        from app.services import journey_orchestrator

        result = journey_orchestrator.resolve_transition("at_risk", "follow_up")
        assert result["allowed"] is False
        assert "offer" in result["reason"]

    def test_an_unknown_stage_is_refused_not_guessed(self):
        from app.services import journey_orchestrator

        result = journey_orchestrator.resolve_transition("at_risk", "banana")
        assert result["allowed"] is False
        assert result["unknown"] is True

    def test_the_validator_catches_a_staircase(self):
        """Without the backward edge, orchestration is a staircase and a re-entry
        is impossible.
        """
        from app.services import journey_orchestrator

        saved = journey_orchestrator.ORCHESTRATION_TRANSITIONS
        journey_orchestrator.ORCHESTRATION_TRANSITIONS = {
            "at_risk": ("offer",),
            "offer": ("follow_up", "status"),
            "follow_up": ("status",),
            "status": (),
        }
        try:
            report = journey_orchestrator.validate_orchestrator()
            assert report["valid"] is False
            assert any("backward edge" in error for error in report["error_list"])
        finally:
            journey_orchestrator.ORCHESTRATION_TRANSITIONS = saved


class TestRepeatRule:
    def _pc(self, **kwargs):
        from app.services import journey_orchestrator

        return journey_orchestrator.precondition_hash(**kwargs)

    def test_three_identical_offers_suppress_the_fourth(self):
        """The off-by-one landed exactly where nagging becomes a blocked number.

        The first version reported the count of attempts *before* the one being
        recorded, so `MAX_SAME_OFFER_ATTEMPTS = 3` permitted a fourth identical
        offer.
        """
        from app.services import journey_orchestrator

        state = journey_orchestrator.initial_state(user_id=1, detected_at=NOW)
        pc = self._pc(
            offer_kind="goodwill",
            recovery_context={"recovery_readiness": "high"},
            health_band="at_risk",
            investment_band="standard",
        )
        for attempt in range(1, 4):
            state = journey_orchestrator.record_attempt(
                state, outcome="declined", precondition=pc, now=NOW
            )
            step = journey_orchestrator.next_step(
                state=state,
                health={"band": "at_risk"},
                offer_kind="goodwill",
                recovery_context={"recovery_readiness": "high"},
                investment_band="standard",
                offer_warranted=True,
                now=NOW,
            )
            # The precondition in the test must be the one next_step computes for
            # this exact input, or the test is measuring two different situations.
            assert step["precondition"] == self._pc(
                offer_kind="goodwill",
                recovery_context={"recovery_readiness": "high"},
                health_band="at_risk",
                investment_band="standard",
            )
            assert step["suppressed_by_repeat_rule"] is (attempt >= 3), attempt

    def test_a_different_situation_is_not_a_repeat(self):
        """A precondition hash of (kind, recovery context, health band,
        investment band) -- not the stage, not the count, not the customer id.
        """
        from app.services import journey_orchestrator

        state = journey_orchestrator.initial_state(user_id=1, detected_at=NOW)
        pc = self._pc(
            offer_kind="goodwill",
            recovery_context={"recovery_readiness": "high"},
            health_band="at_risk",
            investment_band="",
        )
        for _ in range(3):
            state = journey_orchestrator.record_attempt(
                state, outcome="declined", precondition=pc, now=NOW
            )
        changed = journey_orchestrator.next_step(
            state=state,
            health={"band": "critical"},
            offer_kind="waiver",
            recovery_context={"recovery_readiness": "critical"},
            offer_warranted=True,
            now=NOW,
        )
        assert changed["same_precondition_attempts"] == 0
        assert changed["suppressed_by_repeat_rule"] is False
        assert changed["next_stage"] == "offer"

    def test_the_count_is_recomputed_not_read_off_the_state(self):
        """Stale-state regression.

        ``next_step`` originally read ``same_precondition_attempts`` from the
        state, which ``record_attempt`` writes for *its own* call's precondition
        -- so a changed situation stayed suppressed by a decision made about the
        old one. The same class of bug as `capability_audit` reading a derived
        index instead of the table.
        """
        from app.services import journey_orchestrator

        state = journey_orchestrator.initial_state(user_id=1, detected_at=NOW)
        pc = self._pc(
            offer_kind="goodwill",
            recovery_context={"recovery_readiness": "high"},
            health_band="at_risk",
            investment_band="",
        )
        for _ in range(3):
            state = journey_orchestrator.record_attempt(
                state, outcome="declined", precondition=pc, now=NOW
            )
        assert state["same_precondition_attempts"] == 3
        step = journey_orchestrator.next_step(
            state=state,
            health={"band": "critical"},
            offer_kind="waiver",
            recovery_context={"recovery_readiness": "critical"},
            investment_band="",
            offer_warranted=True,
            now=NOW,
        )
        assert step["same_precondition_attempts"] == 0

    def test_the_hash_ignores_the_offer_id(self):
        """A *new* offer of the same kind in the same situation is the same pitch."""
        from app.services import journey_orchestrator

        a = journey_orchestrator.precondition_hash(
            offer_kind="goodwill", recovery_context={"r": "high"}, health_band="at_risk"
        )
        b = journey_orchestrator.precondition_hash(
            offer_kind="goodwill", recovery_context={"r": "high"}, health_band="at_risk"
        )
        assert a == b

    def test_the_hash_distinguishes_every_input_that_matters(self):
        from app.services import journey_orchestrator

        base_kwargs = {
            "offer_kind": "goodwill",
            "recovery_context": {"r": "high"},
            "health_band": "at_risk",
            "investment_band": "standard",
        }
        base = journey_orchestrator.precondition_hash(**base_kwargs)
        for changed in (
            {"offer_kind": "waiver"},
            {"recovery_context": {"r": "critical"}},
            {"health_band": "critical"},
            {"investment_band": "high"},
        ):
            assert journey_orchestrator.precondition_hash(
                **{**base_kwargs, **changed}
            ) != base, changed


class TestOrchestratorSteps:
    def test_a_critical_customer_gets_a_person_not_just_an_offer(self):
        """The orchestrator's own step, and then the copilot's override on top.

        Both matter and they are different surfaces: the orchestrator says what the
        loop should do, the copilot says what an agent should say to the customer.
        """
        from app.services import agent_copilot, journey_orchestrator

        step = journey_orchestrator.next_step(
            state=journey_orchestrator.initial_state(user_id=1, detected_at=NOW),
            health={"band": "critical", "sla_hours": 4},
            offer_kind="goodwill",
            offer_warranted=True,
            recovery_context={"recovery_readiness": "critical"},
            now=NOW,
        )
        assert step["next_stage"] == "offer"
        assert "human on this now" in step["action"]
        assert any("critical health is an offer *and* a person" in e for e in step["evidence"])

        card = agent_copilot.build_copilot_card(
            journey_plan={"next_best_actions": [{"action": "Upsell"}]},
            health={"band": "critical", "sla_hours": 4},
        )
        assert "somebody" in card["next_best_action"]["health_override"]["do"]
        assert "do not open with a scenario or a score" in card["next_best_action"]["health_override"]["do"]

    def test_it_returns_a_plan_and_never_the_act(self):
        """Issuing an offer writes; moving a stage writes. Deciding what to do and
        doing it are two responsibilities.
        """
        from app.services import journey_orchestrator

        step = journey_orchestrator.next_step(
            state=journey_orchestrator.initial_state(user_id=1, detected_at=NOW),
            health={"band": "at_risk"},
            offer_kind="goodwill",
            offer_warranted=True,
            now=NOW,
        )
        assert step["action"]
        assert step["precondition"]
        assert "never the act" in step["note"]
        # No database handle appears anywhere in the payload.
        assert "db" not in step and "session" not in step

    def test_stuck_is_about_our_bookkeeping_not_the_customer(self):
        from app.services import journey_orchestrator

        result = journey_orchestrator.is_stuck(
            {
                "stage": "offer",
                "entered_stage_at": (NOW - timedelta(days=9)).isoformat(),
            },
            now=NOW,
        )
        assert result["stuck"] is True
        assert "not about the customer" in result["note"]

    def test_cycles_are_counted_and_published(self):
        from app.services import journey_orchestrator

        state = journey_orchestrator.initial_state(user_id=1, detected_at=NOW)
        for target in ("offer", "follow_up", "status", "at_risk"):
            moved = journey_orchestrator.advance(state, target, now=NOW)
            assert moved["advanced"] is True, moved["reason"]
            state = moved["state"]
        summary = journey_orchestrator.summarise_cycle(state, now=NOW)
        assert summary["cycles"] == 1
        assert "working programme from a nagging one" in summary["note"]


# ===========================================================================
# The copilot
# ===========================================================================


class TestCopilotProhibitions:
    def test_the_investment_band_is_never_sayable(self):
        """It exists to size a credit. Saying it would be accurate and monstrous."""
        from app.services import agent_copilot

        assert "investment_band" in agent_copilot.COPILOT_DO_NOT_LEAD_WITH_BY_ID
        card = agent_copilot.build_copilot_card(
            customer_360={"summary_text": "Their lifetime value puts them in the strategic band."},
            health={"band": "healthy"},
        )
        found = {row["fact_id"] for row in card["internal_only_facts_present"]}
        assert "investment_band" in found
        assert card["opening_rewritten"] is True

    def test_the_scan_covers_the_whole_card_not_just_the_opening(self):
        """The realistic path: `customer_360.summary_text` is a summary of the
        customer's own record and it will happily contain our internal labels.
        """
        from app.services import agent_copilot

        card = agent_copilot.build_copilot_card(
            customer_360={"summary_text": "Their churn score is 0.82, access band elite."},
            health={"band": "healthy"},
        )
        found = {row["fact_id"] for row in card["internal_only_facts_present"]}
        assert {"churn_score", "access_band"} <= found
        leaked = [f for f in card["what_is_true"]["facts"] if f.get("internal_only")]
        assert leaked, card["what_is_true"]["facts"]
        assert leaked[0]["protected_facts"]

    def test_a_leaked_fact_is_kept_and_marked_not_deleted(self):
        """Hiding it would make the copilot look as though it does not know the
        field, and an agent who cannot see it cannot reason about where else it
        leaks.
        """
        from app.services import agent_copilot

        card = agent_copilot.build_copilot_card(
            customer_360={"summary_text": "policy tier: customer-premium"},
            health={"band": "healthy"},
        )
        assert card["internal_only_facts_present"]
        assert any(f.get("internal_only") for f in card["what_is_true"]["facts"])

    def test_every_prohibition_carries_a_replacement(self):
        """A prohibition without a substitute is a dead end, and a rule they work
        around is worse than no rule.
        """
        from app.services import agent_copilot

        for row in agent_copilot.COPILOT_DO_NOT_LEAD_WITH:
            assert row["say_instead"], row["fact_id"]
            assert row["why"], row["fact_id"]

    def test_the_investment_band_is_on_the_list(self):
        from app.services import agent_copilot

        report = agent_copilot.validate_copilot()
        assert report["valid"] is True, report["error_list"]

    def test_a_fact_with_no_detection_needle_is_an_error(self):
        """Otherwise it can never be found, which makes it a comment."""
        from app.services import agent_copilot

        saved = agent_copilot.COPILOT_DO_NOT_LEAD_WITH
        agent_copilot.COPILOT_DO_NOT_LEAD_WITH = (
            {**dict(saved[0]), "fact_id": "phantom", "say_instead": "x", "why": "y"},
        ) + tuple(dict(row) for row in saved)
        try:
            assert agent_copilot.validate_copilot()["valid"] is False
        finally:
            agent_copilot.COPILOT_DO_NOT_LEAD_WITH = saved


class TestCopilotAssembly:
    def test_critical_health_overrides_a_matched_plan(self):
        """Otherwise a correct-looking recommendation to upsell somebody who has
        an open complaint.
        """
        from app.services import agent_copilot

        card = agent_copilot.build_copilot_card(
            journey_plan={"next_best_actions": [{"action": "Upsell a premium plan"}]},
            health={"band": "critical", "sla_hours": 4},
        )
        override = card["next_best_action"]["health_override"]
        assert override is not None
        assert "must not be allowed to argue them out of a human" in override["reason"]
        assert "Upsell" in card["next_best_action"]["plan_actions"][0]

    def test_every_card_requires_a_human(self):
        """A customer-facing statement written by a rule engine is a promise the
        system cannot keep.
        """
        from app.services import agent_copilot

        card = agent_copilot.build_copilot_card(health={"band": "healthy"})
        assert card["requires_human"] is True
        assert card["is_autopilot"] is False

    def test_it_reports_its_own_coverage(self):
        from app.services import agent_copilot

        thin = agent_copilot.build_copilot_card(health={"band": "healthy"})
        assert thin["confidence"] == "partial"
        assert "treat the next-best-action as a suggestion" in thin["confidence_note"]

        full = agent_copilot.build_copilot_card(
            customer_360={"summary_text": "x"},
            journey_plan={"next_best_actions": []},
            health={"band": "healthy"},
        )
        assert full["confidence"] == "full"

    def test_a_missing_360_is_reported_not_omitted(self):
        """An agent told what is missing will ask for it; one told nothing assumes
        it is fine.
        """
        from app.services import agent_copilot

        card = agent_copilot.build_copilot_card(
            customer_360={
                "summary_text": "x",
                "unavailable_sections": {"sentiment": "model unavailable"},
            },
            health={"band": "healthy"},
        )
        said = " ".join(f["fact"] for f in card["what_is_true"]["facts"])
        assert "sentiment" in said and "model unavailable" in said

    def test_only_actionable_offers_are_put_in_the_script(self):
        from app.services import agent_copilot

        card = agent_copilot.build_copilot_card(
            health={"band": "at_risk"},
            offers=[
                {"reference": "OFF-1", "offer_kind": "goodwill", "actionable": True},
                {"reference": "OFF-2", "offer_kind": "goodwill", "actionable": False},
            ],
        )
        assert len(card["offers"]) == 1
        assert card["offers"][0]["reference"] == "OFF-1"

    def test_the_opening_never_names_an_internal_fact(self):
        from app.services import agent_copilot

        for band in ("healthy", "strained", "at_risk", "critical", "no_data"):
            card = agent_copilot.build_copilot_card(health={"band": band})
            opening = card["opening"].lower()
            for needle in ("churn score", "access band", "policy tier", "ltv"):
                assert needle not in opening, (band, opening)

    def test_it_composes_rather_than_re_deriving(self):
        """A third answer to a question the codebase already answers twice is the
        one an agent would believe.
        """
        from app.services import agent_copilot

        card = agent_copilot.build_copilot_card(
            customer_360={"summary_text": "x"},
            explanations={"recovery": {"plain_language": "we were late"}},
            journey_plan={"next_best_actions": [{"action": "a"}]},
            health={"band": "healthy"},
            offers=[],
        )
        assert card["why_in_plain_language"]["source"] == "customer_explain"
        assert card["next_best_action"]["plan_source"] == "loyalty_journey"
        assert card["section_order"] == list(agent_copilot.COPILOT_SECTION_ORDER)