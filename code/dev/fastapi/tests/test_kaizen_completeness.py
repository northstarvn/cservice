"""Completeness grading, the level-aware gate, and the one-command sweep.

The question these answer is the one an immature backend asks first and cannot:
*is this part built, and if not, is that allowed yet?*

Written adversarially. Every honest-reporting feature in this repository has
eventually been shown to report the wrong thing, and this subsystem is the most
exposed to it -- it grades other code from evidence, so a wrong assumption in
the grader produces confident nonsense about parts that are fine. Each rule
below has a negative control that has to fail for the right reason.

The three failures worth naming, because they happened while writing this:

1. **Naive route introspection reported 29 of 30 surfaces absent.** This
   FastAPI release keeps included routers as lazy wrappers, so ``app.routes``
   lists 39 entries where 274 effective paths are served. Correctness of the
   *whole* completeness report depended on noticing that.
2. **The stub detector compared different probes to each other.** It grouped
   every outcome a capability produced and asked whether personas disagreed,
   which compared ``complaint_is_routed`` against
   ``complaint_verdict_is_explained`` and declared a healthy engine a stub.
3. **Then it inverted again** -- identical *expectations* were read as
   disagreement, so three healthy engines came back as stubs. Personas only mean
   anything against the same question asked of each of them.
"""
from __future__ import annotations

import pytest


# ===========================================================================
# The state vocabulary
# ===========================================================================


class TestStates:
    def test_there_are_six_and_the_order_is_the_point(self):
        from app import capability_audit

        assert capability_audit.CAPABILITY_STATES == (
            "absent",
            "declared_only",
            "stub",
            "partial",
            "untested",
            "complete",
        )
        # A boolean cannot tell a roadmap from a defect, and the two want
        # different responses from the same person on the same day.
        assert capability_audit.DEFECT_STATES == {"stub", "partial"}
        assert capability_audit.INCOMPLETE_STATES == {"absent", "declared_only"}
        # `untested` is neither: the part is fine, nobody looked.
        assert capability_audit.UNKNOWN_STATES == {"untested"}

    def test_untested_is_not_counted_as_evidence_of_health(self):
        """The whole reason it is its own state rather than a flag on complete.

        "We found nothing wrong" and "we looked at nothing" are different
        sentences, and merging them is how an unmeasured part ships on the
        strength of silence.
        """
        from app import capability_audit

        assert not capability_audit.is_at_least("untested", "complete")
        assert capability_audit.is_at_least("untested", "partial")
        assert not capability_audit.is_at_least("absent", "declared_only")

    def test_the_ladder_catalog_publishes_the_vocabulary(self):
        from app import real_life_flows

        catalog = real_life_flows.build_flows_catalog()
        assert catalog["capability_states"] == [
            "absent",
            "declared_only",
            "stub",
            "partial",
            "untested",
            "complete",
        ]


# ===========================================================================
# Absence must be evidence
# ===========================================================================


class TestAbsenceIsEvidence:
    """The rule the whole module rests on.

    A probe that raised is not evidence that the part is missing. It is equally
    consistent with a typo, a renamed argument and a regression. So
    ``resolve_engine`` distinguishes "the module is there and the name is not"
    (absence) from "I could not tell" (unknown), and ``unknown`` never degrades
    to ``absent``.
    """

    def test_a_missing_attribute_is_absent(self):
        from app import capability_audit

        verdict = capability_audit.resolve_engine(
            {"capability_id": "x", "root": "app.release_ladder", "target": "no_such_name"}
        )
        assert verdict["state"] == "absent"
        assert "no_such_name" in verdict["detail"]

    def test_an_unimportable_root_is_unknown_not_absent(self):
        from app import capability_audit

        verdict = capability_audit.resolve_engine(
            {"capability_id": "x", "root": "app.not_a_module", "target": "anything"}
        )
        # Not `absent`. If this read as absent, a typo in a root path would shrink
        # every report it was pointed at, which is the failure mode this module
        # exists to avoid.
        assert verdict["state"] == "unknown"
        assert verdict["state"] != "absent"

    def test_a_row_naming_no_target_is_unknown(self):
        from app import capability_audit

        assert capability_audit.resolve_engine({"capability_id": "x"})["state"] == "unknown"

    def test_a_non_callable_target_is_a_stub(self):
        from app import capability_audit

        verdict = capability_audit.resolve_engine(
            {
                "capability_id": "x",
                "root": "app.release_ladder",
                # A constant, not a function: present, and does no work.
                "target": "ROLLBACK_WINDOW",
            }
        )
        assert verdict["state"] == "stub"

    def test_every_declared_target_resolves(self):
        """Guards against an invented name.

        Five of the twelve engine targets were guesses when this was first
        written, and the audit caught every one of them -- which is the argument
        for resolving by import rather than trusting a hand-written list.
        """
        from app import capability_audit

        for name in capability_audit.ENGINE_IDS:
            verdict = capability_audit.resolve_engine(capability_audit.ENGINE_BY_ID[name])
            assert verdict["state"] == "present", (name, verdict["detail"])


class TestDeferral:
    """A probe against a part that is positively absent is deferred, not failed."""

    def test_a_raised_probe_against_a_missing_part_is_deferred(self):
        from app import capability_audit, real_life_flows

        # Force the "absent" answer by pointing a capability at a name that does
        # not exist -- the positive evidence of absence the rule depends on.
        saved = capability_audit.ENGINES
        # `ENGINES` is the table the assessor reads (not `ENGINE_BY_ID`, which is
        # a derived index) precisely so this edit is visible to production code.
        capability_audit.ENGINES = tuple(
            {**dict(row), "target": "no_such_function"}
            if str(row["capability_id"]) == "rule_engine"
            else dict(row)
            for row in saved
        )
        real_life_flows.reset_completeness_cache()
        try:
            deferrable = capability_audit.deferrable_capabilities()
            assert "rule_engine" in deferrable
            reason = capability_audit.may_defer("rule_pack_selects", deferrable)
            assert reason and "no_such_function" in reason
            # And a probe with no capability is never deferrable, whatever else
            # is missing.
            assert capability_audit.may_defer("authz_routes_classified", deferrable) is None
        finally:
            capability_audit.ENGINES = saved
            real_life_flows.reset_completeness_cache()

    def test_an_unrelated_exception_is_never_treated_as_absence(self):
        """The negative control that matters most.

        A typo, a renamed keyword argument and a regression all raise
        ``AttributeError`` or ``TypeError`` from a probe. If any of those were
        read as "not built yet", the simulator would report a healthy backend as
        empty and an incident as a to-do item.
        """
        from app import capability_audit

        empty: dict = {}
        for probe_id in ("access_band_resolves", "shadow_is_one_way", "anything"):
            assert capability_audit.may_defer(probe_id, empty) is None

    def test_deferral_does_not_count_as_a_pass(self):
        """A deferred subflow is evidence of nothing, in either direction."""
        from app import real_life_flows

        deferred = real_life_flows._outcome(
            "sub", real_life_flows.PERSONA_BY_ID["loyal_with_points"], False, "deferred",
            deferred=True, deferral_reason="not built",
        )
        run = real_life_flows.FlowRun(
            "f", "loyal_with_points", outcomes=[deferred]
        )
        # `passed` is True because nothing failed...
        assert run.passed is True
        # ...and the subflow is reported as exercised zero times, which is the
        # number that stops that from reading as coverage.
        assert run.exercised == 0
        assert len(run.deferred) == 1
        assert run.as_dict()["exercised_subflows"] == 0

    def test_measurements_report_deferral_alongside_failure(self):
        """`flows_failed: 0` is also what an empty backend reports.

        If only the failure count is published, a gate reading it is satisfied by
        a backend that has built nothing -- so the deferred count is a
        first-class field, not a footnote.
        """
        from app import real_life_flows

        deferred = real_life_flows._outcome(
            "sub", real_life_flows.PERSONA_BY_ID["loyal_with_points"], False, "deferred",
            deferred=True, deferral_reason="not built",
        )
        run = real_life_flows.FlowRun("f", "loyal_with_points", outcomes=[deferred])
        measurements = real_life_flows.flow_gate_measurements([run])
        assert measurements["flows_failed"] == 0
        assert measurements["subflows_deferred"] == 1
        assert measurements["subflows_exercised"] == 0
        assert measurements["deferral_reasons"] == ["not built"]


# ===========================================================================
# Route resolution: the lazy-router trap
# ===========================================================================


class TestRouteResolution:
    def test_app_routes_alone_under_reports_the_served_surface(self):
        """The bug, pinned so it cannot be reintroduced by a well-meaning edit.

        Included routers are lazy wrappers in this FastAPI release. Reading
        ``app.routes`` directly declared 29 of the 30 surfaces the flows name as
        absent -- 26 of them false. The completeness report is only as honest as
        this traversal.
        """
        from app import capability_audit
        from app.main import app

        naive = {
            capability_audit._normalise_path(getattr(route, "path", "") or "")
            for route in app.routes
        }
        served = capability_audit.effective_route_paths(app.routes)
        assert len(served) > len(naive) * 2, (len(served), len(naive))

    def test_a_collection_root_counts_as_served(self):
        """`/bookings` is the shape of the surface, not one HTTP path.

        A stricter rule reported the collection root missing on a backend where
        every one of its twenty routes exists, which is a spelling, not a gap.
        """
        from app import capability_audit
        from app.main import app

        served = capability_audit.effective_route_paths(app.routes)
        state, detail = capability_audit._surface_state("/bookings", served, False)
        assert state == "complete", detail

    def test_most_flow_surfaces_resolve(self):
        """29 of 30 at the time of writing; one real mismatch is found below."""
        from app import capability_audit, real_life_flows
        from app.main import app

        served = capability_audit.effective_route_paths(app.routes)
        resolved = sum(
            1
            for flow in real_life_flows.FLOW_CATALOG
            for surface in flow["surfaces"]
            if capability_audit._surface_state(surface, served, False)[0] == "complete"
        )
        total = sum(len(flow["surfaces"]) for flow in real_life_flows.FLOW_CATALOG)
        assert resolved >= total - 1, (resolved, total)

    def test_a_real_mismatch_is_found_and_named(self):
        """The independent oracle for the surface check.

        ``regulatory_complaint_deadline`` names ``/complaints/admin/sla-report``;
        the served path is ``/complaints/admin/sla``. Either the flow's inventory
        is stale or the route is unbuilt, and the report has to say which state
        rather than quietly passing it -- a rename is not a working surface.
        """
        from app import capability_audit
        from app.main import app

        served = capability_audit.effective_route_paths(app.routes)
        state, detail = capability_audit._surface_state(
            "/complaints/admin/sla-report", served, True
        )
        assert state == "declared_only"
        assert "no route serves it" in detail
        # And the rule table is what makes it `declared_only` rather than absent.
        assert "/complaints/admin/sla" in served


# ===========================================================================
# The stub detector, and its two false-positive bugs
# ===========================================================================


class _Outcome:
    """A stand-in for a probe outcome, so the detector is testable alone."""

    def __init__(self, subflow_id, expected, observed, error=""):
        self.subflow_id = subflow_id
        self.persona_id = "p"
        self.expected = dict(expected)
        self.observed = dict(observed)
        self.error = error
        self.held = True
        self.deferred = False


class TestStubDetector:
    def test_two_expectations_and_one_answer_is_a_stub(self):
        from app import capability_audit

        outcomes = [
            _Outcome("q", {"band": "elite"}, {"band": "elite"}),
            _Outcome("q", {"band": "limited"}, {"band": "elite"}),
        ]
        assert capability_audit.stub_signal(outcomes)["state"] == "stub"

    def test_two_expectations_and_two_answers_is_complete(self):
        from app import capability_audit

        outcomes = [
            _Outcome("q", {"band": "elite"}, {"band": "elite"}),
            _Outcome("q", {"band": "limited"}, {"band": "limited"}),
        ]
        assert capability_audit.stub_signal(outcomes)["state"] == "complete"

    def test_different_probes_are_never_compared_to_each_other(self):
        """Bug 2, pinned.

        Comparing ``complaint_is_routed`` against ``complaint_verdict_is_explained``
        compares two different questions with different observation shapes, and
        reported a healthy complaint engine as a stub.
        """
        from app import capability_audit

        outcomes = [
            _Outcome("route_it", {"x": 1}, {"tier": "tier_1"}),
            _Outcome("explain_it", {"x": 1}, {"decision": "accept", "guards": []}),
        ]
        assert capability_audit.stub_signal(outcomes)["state"] == "complete"

    def test_identical_expectations_are_blind_not_stub(self):
        """Bug 3, pinned, and the more useful of the two findings.

        Every persona expected the same thing, so one answer is correct and a
        constant engine would also pass. Reading that as a stub reported three
        healthy engines as broken; reading it as complete would have claimed
        evidence that does not exist. It is a hole in the *probe*.
        """
        from app import capability_audit

        outcomes = [
            _Outcome("q", {"non_empty": True}, {"stage": "engaged"}),
            _Outcome("q", {"non_empty": True}, {"stage": "engaged"}),
        ]
        assert capability_audit.stub_signal(outcomes)["state"] == "complete"
        assert capability_audit._blind_probes(outcomes) == {"q"}

    def test_a_raised_outcome_is_excluded(self):
        """An empty observation is not a constant answer."""
        from app import capability_audit

        outcomes = [
            _Outcome("q", {"a": 1}, {}, error="AttributeError: nope"),
            _Outcome("q", {"a": 2}, {}, error="AttributeError: nope"),
        ]
        assert capability_audit.stub_signal(outcomes)["state"] == "complete"
        assert capability_audit._blind_probes(outcomes) == set()


# ===========================================================================
# Assessment against the real tree
# ===========================================================================


class TestAssessment:
    def test_no_healthy_engine_is_reported_as_a_stub(self):
        """The regression guard for bug 3, on the real tree.

        Before the fix this returned three stubs -- ``complaints``,
        ``preferences`` and ``recovery_playbooks`` -- all of them healthy.
        """
        from app import real_life_flows

        runs = real_life_flows.run_all_flows()
        report = real_life_flows.capability_report(runs)
        stubs = [row["capability_id"] for row in report["capabilities"] if row["state"] == "stub"]
        assert stubs == [], stubs

    def test_probe_coverage_is_reported_in_both_directions(self):
        """A registry that overstates its own coverage is a hole in the tests.

        **This assertion used to assert the opposite, and it was wrong.** It
        pinned ``posture_adjustment_is_effective`` and ``rule_pack_selects`` as
        orphans, under a docstring reading "registered, classified and never
        invoked by any flow -- written, wired, and dead." Both *were* invoked:
        ``points_and_arrears_payment`` and ``support_agent_triage`` name them. The
        audit derives "exercised" from the ``subflow_id`` a probe emits and
        "registered" from its ``PROBES`` key, and both probes emitted a
        *different* string from their key -- ``posture_adjustment_matches_its_row``
        and ``rule_packs_select``. So two wired probes read as dead ones, and the
        test was reporting the audit's bug as a fact about the product.

        That is why this now asserts the direction that matters: **no registered
        probe may be an orphan**, with a planted orphan as the negative control.
        A registry check that only runs on a healthy tree proves nothing about the
        registry.
        """
        from app import real_life_flows

        runs = real_life_flows.run_all_flows()
        report = real_life_flows.capability_report(runs)
        assert report["orphan_probes"] == [], (
            "a registered probe reported as never exercised: "
            f"{report['orphan_probes']}"
        )
        assert "topics" in report["untested"]

    def test_a_probe_that_really_is_orphaned_is_still_reported(self):
        """The negative control for the test above.

        Remove a probe from every flow that names it and the audit has to notice.
        Without this, "no orphans" is indistinguishable from an audit that cannot
        detect one.

        Note the two indexes: ``run_flow`` resolves a flow through
        ``FLOW_BY_ID``, not by scanning ``FLOW_CATALOG``, so a test that edits
        only the tuple edits nothing that runs. Both are patched here, and the
        reason is worth knowing before writing the next one.
        """
        from app import real_life_flows

        saved_catalog = real_life_flows.FLOW_CATALOG
        saved_index = real_life_flows.FLOW_BY_ID
        drop = "consent_gate_excludes_service"
        flows = [
            {
                **flow,
                "probe_ids": tuple(p for p in flow["probe_ids"] if p != drop),
            }
            for flow in saved_catalog
        ]
        real_life_flows.FLOW_CATALOG = tuple(flows)
        real_life_flows.FLOW_BY_ID = {str(f["flow_id"]): f for f in flows}
        try:
            report = real_life_flows.capability_report(real_life_flows.run_all_flows())
            assert drop in report["orphan_probes"], report["orphan_probes"]
        finally:
            real_life_flows.FLOW_CATALOG = saved_catalog
            real_life_flows.FLOW_BY_ID = saved_index

    def test_every_probe_emits_the_subflow_id_it_is_registered_under(self):
        """The invariant the orphan list was quietly measuring.

        A probe whose emitted ``subflow_id`` differs from its registry key is
        invisible to two systems at once: ``capability_audit`` cannot match it to
        a capability, and ``_classify`` has no branch for it, so it falls through
        to ``invariant_violated`` instead of the category that names the defect.
        Checked directly, per persona, because "every persona agrees" is not the
        claim.
        """
        from app import real_life_flows

        for probe_id, probe in real_life_flows.PROBES.items():
            for persona in real_life_flows.PERSONAS:
                outcome = probe(persona)
                assert outcome.subflow_id == probe_id, (
                    f"{probe_id} emits {outcome.subflow_id!r} for "
                    f"{persona.persona_id!r}"
                )

    def test_every_probe_that_claims_persona_variation_can_actually_detect_it(self):
        """A probe that advertises sensitivity and cannot deliver it is a false negative.

        Four probes carried docstrings saying "personas differ ..." while every
        persona produced one identical expectation, because each branched on a
        local dict keyed on ``persona_id`` -- and two of the keys in each dict
        named a persona that does not exist, so the branch could never execute.
        The audits reported them blind; nobody read that as a defect.
        """
        from app import real_life_flows

        report = real_life_flows.check_probe_sharpness()
        assert report["claims_variation_but_constant"] == [], report[
            "claims_variation_but_constant"
        ]
        assert report["valid"] is True

    def test_a_probe_whose_expectation_cannot_fail_is_reported_blind(self):
        """The negative control for the sharpness check above.

        Replace one probe with a constant, and the check has to notice. Otherwise
        ``check_probe_sharpness()["valid"] is True`` is a claim about a function
        that returns True.
        """
        from app import real_life_flows

        saved = real_life_flows.PROBES["contact_hour_is_local"]

        def constant(persona):
            """Personas differ *only* in region, at one fixed UTC moment."""
            return saved(persona).__class__(
                subflow_id="contact_hour_is_local",
                persona_id=persona.persona_id,
                held=True,
                summary="constant",
                observed={},
                expected={"local_hour": 9},
            )

        # The docstring is copied deliberately. `check_probe_sharpness` decides
        # whether a probe *claims* persona variation by reading it, so a stand-in
        # without the claim would pass the check and the negative control would
        # be measuring the wrong thing.
        constant.__doc__ = saved.__doc__
        real_life_flows.PROBES["contact_hour_is_local"] = constant
        try:
            report = real_life_flows.check_probe_sharpness()
            assert "contact_hour_is_local" in report["constant_expectation"]
            assert report["valid"] is False, report["claims_variation_but_constant"]
        finally:
            real_life_flows.PROBES["contact_hour_is_local"] = saved

    def test_every_unfinished_row_carries_its_evidence(self):
        """A completeness verdict with no evidence is an oracle with no audit trail."""
        from app import real_life_flows

        report = real_life_flows.capability_report()
        for row in report["capabilities"]:
            if row["state"] != "complete":
                assert row["evidence"], row
                assert all(isinstance(item, str) and item for item in row["evidence"])

    def test_an_unresolvable_capability_is_graded_worse_not_better(self):
        """A broken audit must not be able to shrink the report."""
        from app import capability_audit, real_life_flows

        saved = capability_audit.ENGINES
        capability_audit.ENGINES = tuple(
            {**dict(row), "root": "app.not_a_module"}
            if str(row["capability_id"]) == "topics"
            else dict(row)
            for row in saved
        )
        try:
            report = capability_audit.assess_capabilities(
                routes=_routes(),
                flows=real_life_flows.FLOW_CATALOG,
                probes=real_life_flows.PROBES,
                outcomes=[],
            )
            row = next(r for r in report["capabilities"] if r["capability_id"] == "topics")
            assert row["state"] == "partial"
            assert row["unassessable"] is True
            assert "topics" in report["unassessable"]
        finally:
            capability_audit.ENGINES = saved

    def test_a_flow_naming_an_unregistered_probe_is_a_blocker(self):
        """It claims an invariant nothing checks, so it passes vacuously."""
        from app import real_life_flows

        runs = real_life_flows.run_all_flows()
        report = real_life_flows.capability_report(runs)
        rows = real_life_flows.completeness_blockages(report)
        categories = {row.category for row in rows}
        assert "probe_not_registered" not in categories  # none today

        # Now force one, by removing a probe the flows name.
        from app import capability_audit

        saved = capability_audit.PROBE_CAPABILITY
        capability_audit.PROBE_CAPABILITY = dict(saved)
        capability_audit.PROBE_CAPABILITY["not_a_real_probe"] = "topics"
        flows = [
            {**dict(flow), "probe_ids": tuple(flow["probe_ids"]) + ("not_a_real_probe",)}
            if flow["flow_id"] == "support_agent_triage"
            else dict(flow)
            for flow in real_life_flows.FLOW_CATALOG
        ]
        try:
            from app.main import app

            report = capability_audit.assess_capabilities(
                routes=app.routes,
                flows=flows,
                probes=real_life_flows.PROBES,
                outcomes=[],
            )
            found = [
                row
                for row in real_life_flows.completeness_blockages(report)
                if row.category == "probe_not_registered"
            ]
            assert len(found) == 1
            assert found[0].severity == "blocker"
        finally:
            capability_audit.PROBE_CAPABILITY = saved


# ===========================================================================
# Level awareness: the requirement behind the whole feature
# ===========================================================================


class TestLevelAwareness:
    @staticmethod
    def _report(states: list[str]):
        """A synthetic report, so the policy is tested without the real tree."""
        from app import capability_audit

        rows = [
            {
                "capability_id": f"part_{index}",
                "kind": "engine",
                "title": f"part {index}",
                "state": state,
                "evidence": ["synthetic"],
                "probes": [],
                "flows": [],
                "detail": "",
            }
            for index, state in enumerate(states)
        ]
        counts = {state: 0 for state in capability_audit.CAPABILITY_STATES}
        for row in rows:
            counts[row["state"]] += 1
        return {
            "capabilities": rows,
            "counts": counts,
            "total": len(rows),
            "complete_share": round(counts["complete"] / len(rows), 4) if rows else 0.0,
            "untested": [],
            "unassessable": [],
        }

    def test_an_immature_backend_passes_the_early_rungs(self):
        """The requirement, stated as a test.

        "Not built yet" is the *starting state* of this project. A gate that
        treated it as a failure would block the first commit of every feature, so
        at l0_draft and l1_verified an empty backend must pass.
        """
        from app import capability_audit

        report = self._report(["absent", "absent", "declared_only", "complete"])
        for level in ("l0_draft", "l1_verified", "l2_shadow"):
            measured = capability_audit.capability_gate_measurements(report, for_level=level)
            assert measured["capability_blocking"] == 0, (level, measured)
            assert measured["capability_incomplete"] == 3  # two absent + one declared

    def test_a_gap_stops_being_a_to_do_item_at_canary(self):
        from app import capability_audit

        report = self._report(["absent", "declared_only"])
        canary = capability_audit.capability_gate_measurements(report, for_level="l3_canary")
        assert canary["capability_blocking"] == 2
        assert canary["gaps_block_at_this_level"] is True

    def test_a_defect_blocks_at_every_level(self):
        """A part that exists and lies is worse than one honestly absent."""
        from app import capability_audit

        report = self._report(["stub", "partial"])
        for level in ("l0_draft", "l1_verified", "l2_shadow", "l3_canary", "l4_live"):
            measured = capability_audit.capability_gate_measurements(report, for_level=level)
            assert measured["capability_blocking"] == 2, level

    def test_untested_blocks_only_at_live(self):
        """Canarying an unmeasured part is how you measure it. Shipping one is not."""
        from app import capability_audit

        report = self._report(["untested"])
        for level in ("l0_draft", "l2_shadow", "l3_canary"):
            measured = capability_audit.capability_gate_measurements(report, for_level=level)
            assert measured["capability_blocking"] == 0, level
        live = capability_audit.capability_gate_measurements(report, for_level="l4_live")
        assert live["capability_blocking"] == 1
        assert live["untested_blocks_at_this_level"] is True

    def test_the_policy_is_published_not_implied(self):
        from app import capability_audit

        measured = capability_audit.capability_gate_measurements(
            {"capabilities": [], "counts": {}, "total": 0, "complete_share": 0.0,
             "untested": [], "unassessable": []},
            for_level="l3_canary",
        )
        assert "l3_canary" in measured["policy_note"]
        assert "every level" in measured["policy_note"]

    def test_the_live_tree_grades_differently_at_each_rung(self):
        """Real numbers, so the policy is demonstrated and not just asserted."""
        from app import real_life_flows

        report = real_life_flows.capability_report(real_life_flows.run_all_flows())
        counts = [
            real_life_flows.completeness_measurements(report, for_level=level)[
                "capability_blocking"
            ]
            for level in ("l0_draft", "l2_shadow", "l3_canary", "l4_live")
        ]
        # Monotonically non-decreasing as the rung rises. Never the reverse: that
        # would mean promoting made the bar looser.
        assert counts == sorted(counts), counts
        assert counts[0] == 0, counts


# ===========================================================================
# The ladder gate
# ===========================================================================


class TestCompletenessGate:
    def test_it_blocks_canary_and_live_but_not_the_early_rungs(self):
        from app import release_ladder

        assert "backend_completeness_honest" in release_ladder.required_gates_for("l3_canary")
        assert "backend_completeness_honest" in release_ladder.required_gates_for("l4_live")
        for level in ("l0_draft", "l1_verified", "l2_shadow"):
            assert "backend_completeness_honest" not in release_ladder.required_gates_for(level)

    def test_it_fails_closed_when_nobody_measured_it(self):
        from app import release_ladder

        candidate = release_ladder.ReleaseCandidate(
            "c", "code:a", "data:r", level="l2_shadow", maintainer="ana",
            measured={"tests_failed": 0, "flows_run": 1, "flows_failed": 0,
                      "shadow_isolated": True, "pipelines_one_way": True,
                      "shadow_to_live_writes": 0, "shadow_divergence_ratio": 0.0,
                      "regressions": 0, "rollback_targets": 5},
        )
        report = release_ladder.evaluate_gates(candidate, for_level="l3_canary")
        assert "backend_completeness_honest" in report["blocking_failures"]

    def test_the_reason_names_the_parts(self):
        from app import release_ladder

        candidate = release_ladder.ReleaseCandidate(
            "c", "code:a", "data:r", level="l2_shadow", maintainer="ana",
            measured={
                "tests_failed": 0, "flows_run": 1, "flows_failed": 0,
                "shadow_isolated": True, "pipelines_one_way": True,
                "shadow_to_live_writes": 0, "shadow_divergence_ratio": 0.0,
                "regressions": 0, "rollback_targets": 5,
                "capability_blocking": 2,
                "capability_blocking_detail": ["topics=untested", "complaints=absent"],
                "for_level": "l4_live",
            },
        )
        report = release_ladder.evaluate_gates(candidate, for_level="l4_live")
        gate = next(
            row for row in report["gates"] if row["gate_id"] == "backend_completeness_honest"
        )
        assert "topics=untested" in gate["reason"]
        assert "l4_live" in gate["reason"]

    def test_the_validator_knows_the_gate(self):
        from app import release_ladder

        report = release_ladder.validate_release_ladder()
        assert report["valid"] is True, report["errors"]
        assert "backend_completeness_honest" in release_ladder.PROMOTION_GATE_BY_ID


# ===========================================================================
# Self-validation of the inventory
# ===========================================================================


class TestInventoryValidation:
    def test_the_live_inventory_is_valid(self):
        from app import capability_audit, real_life_flows
        from app.main import app

        report = capability_audit.validate_capabilities(
            flows=real_life_flows.FLOW_CATALOG, probes=real_life_flows.PROBES, routes=app.routes
        )
        assert report["valid"] is True, report["errors"]

    def test_a_probe_classified_but_not_registered_is_an_error(self):
        """The audit's own table must not name a probe that is not there.

        A completeness inventory pointing at a missing probe reports confident
        nonsense about which parts are untested -- the same failure as a
        hardcoded expectation, arrived at through the declaration instead.
        """
        from app import capability_audit, real_life_flows
        from app.main import app

        saved = capability_audit.PROBE_CAPABILITY
        capability_audit.PROBE_CAPABILITY = {**saved, "ghost_probe": "topics"}
        try:
            report = capability_audit.validate_capabilities(
                flows=real_life_flows.FLOW_CATALOG,
                probes=real_life_flows.PROBES,
                routes=app.routes,
            )
            assert report["valid"] is False
            assert any("ghost_probe" in error for error in report["errors"])
        finally:
            capability_audit.PROBE_CAPABILITY = saved

    def test_an_engine_naming_a_flow_that_does_not_exist_is_an_error(self):
        from app import capability_audit, real_life_flows
        from app.main import app

        saved = capability_audit.ENGINES
        capability_audit.ENGINES = tuple(
            {**dict(row), "flows": ("no_such_flow",)}
            if str(row["capability_id"]) == "topics"
            else dict(row)
            for row in saved
        )
        try:
            report = capability_audit.validate_capabilities(
                flows=real_life_flows.FLOW_CATALOG,
                probes=real_life_flows.PROBES,
                routes=app.routes,
            )
            assert report["valid"] is False
            assert any("no_such_flow" in error for error in report["errors"])
        finally:
            capability_audit.ENGINES = saved


# ===========================================================================
# The sweep: one command, one exit code
# ===========================================================================


def _routes():
    from app.main import app

    return app.routes


class TestSweep:
    def test_one_call_answers_every_question(self):
        from app import kaizen_runner

        payload = kaizen_runner.run_sweep(include_shadow=False)
        for section in ("flows", "completeness", "blockages", "authorization", "ladder"):
            assert payload[section].get("ok") is True, (section, payload[section])
        assert payload["flows"]["measurements"]["flows_run"] > 0
        assert payload["completeness"]["total"] > 0

    def test_it_does_not_write_anything_by_default(self):
        """The timer runs this, so an automatic writer would fill the log."""
        from pathlib import Path

        from app import kaizen_runner

        path = Path(__file__).resolve().parent.parent / "BLOCKAGES.md"
        before = path.stat().st_mtime_ns, path.stat().st_size
        payload = kaizen_runner.run_sweep(include_shadow=False, for_level="l0_draft")
        assert "append" not in payload
        assert (path.stat().st_mtime_ns, path.stat().st_size) == before

    def test_a_blocker_a_warning_and_a_broken_sweep_have_three_codes(self):
        """Because "nothing wrong" and "I could not tell" must not return the same
        number. A CI job reading 0 for both is a CI job that has stopped running.
        """
        from app import kaizen_runner

        clean = {"blockages": {"ok": True, "blockers": 0}, "shadow": {}, "ladder": {}}
        assert kaizen_runner.exit_code(clean) == 0
        assert kaizen_runner.exit_code({**clean, "flows": {"ok": False, "error": "boom"}}) == 2
        assert (
            kaizen_runner.exit_code(
                {"blockages": {"ok": True, "blockers": 1}, "shadow": {}, "ladder": {}}
            )
            == 1
        )

    def test_an_unmeasured_gate_is_not_a_failed_gate(self):
        """Otherwise the sweep says "no" on every developer checkout and is
        learned to be ignored, which is the same as no sweep at all.
        """
        from app import kaizen_runner

        unmeasured = {
            "blockages": {"ok": True, "blockers": 0},
            "shadow": {},
            "ladder": {
                "ok": True,
                "blocking_failures": ["rollback_target_available"],
                "unmeasured_gates": ["rollback_target_available"],
                "measured_failures": [],
            },
        }
        assert kaizen_runner.exit_code(unmeasured) == 0
        measured = {
            **unmeasured,
            "ladder": {
                "ok": True,
                "blocking_failures": ["backend_completeness_honest"],
                "unmeasured_gates": [],
                "measured_failures": ["backend_completeness_honest"],
            },
        }
        assert kaizen_runner.exit_code(measured) == 1

    def test_the_summary_is_readable_and_says_what_was_not_measured(self):
        from app import kaizen_runner

        text = kaizen_runner.summarize(kaizen_runner.run_sweep(include_shadow=False))
        assert "flows" in text
        assert "completeness" in text
        assert "blocking at" in text
        assert "not measured here" in text or "no measured failure" in text

    def test_a_broken_section_is_reported_rather_than_raised(self):
        """A sweep that dies halfway leaves a partial picture with no indication
        that it is partial, which is worse than one that finishes and reports a
        section as failed.
        """
        from app import kaizen_runner, real_life_flows

        saved = real_life_flows.run_all_flows
        real_life_flows.run_all_flows = lambda **_: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            payload = kaizen_runner.run_sweep(include_shadow=False)
            assert payload["exit_code"] == 2
            assert payload["flows"]["ok"] is False
            assert "boom" in payload["flows"]["error"]
            # The other sections still ran.
            assert payload["completeness"]["ok"] is True
        finally:
            real_life_flows.run_all_flows = saved


class TestAutomaticTrigger:
    def test_it_is_off_by_default(self):
        from app import kaizen_runner

        assert kaizen_runner.KAIZEN_AUTORUN is False
        assert kaizen_runner.KAIZEN_APPEND is False
        # A timer that writes to the log is either a no-op (the duplicate guard
        # refuses it) or a flood. Both are useless, so it is opt-in.
        assert kaizen_runner.KAIZEN_APPEND is not True

    def test_the_status_publishes_the_configuration_not_only_the_result(self):
        """Because the useful question is not "did it pass" but "is it running".

        A worker that silently never started looks exactly like a healthy one.
        """
        from app import kaizen_runner

        status = kaizen_runner.autostatus()
        assert status["autorun_enabled"] is False
        assert status["interval_seconds"] > 0
        assert "last" in status
        assert "note" in status
        assert "BLOCKAGES.md" in status["note"]

    def test_it_runs_immediately_then_stops_on_the_event(self):
        """Not after the first interval: a worker that first sleeps leaves no
        evidence it ever ran, and the failure looks like health.
        """
        import asyncio

        from app import kaizen_runner

        calls: list[dict] = []

        async def scenario():
            stop = asyncio.Event()
            task = asyncio.create_task(
                kaizen_runner.sweep_forever(
                    stop=stop, interval=3600, include_shadow=False,
                    on_result=lambda payload: calls.append(payload),
                )
            )
            # Long enough for the immediate first sweep, short enough to be fast.
            for _ in range(200):
                await asyncio.sleep(0.05)
                if calls:
                    break
            stop.set()
            await asyncio.wait_for(task, timeout=30)

        asyncio.run(scenario())
        assert calls, "the sweep never ran"
        assert calls[0].get("flows", {}).get("ok") is True
        # And it never wrote to the log.
        assert "append" not in calls[0] or calls[0]["append"].get("written") is False

    def test_a_sweep_result_is_remembered_and_bounded(self):
        from app import kaizen_runner

        assert kaizen_runner.last_sweep() is None or isinstance(
            kaizen_runner.last_sweep(), dict
        )
        payload = kaizen_runner.run_sweep(include_shadow=False)
        remembered = kaizen_runner._remember(payload)
        assert remembered["exit_code"] == payload["exit_code"]
        history = kaizen_runner.sweep_history()
        assert len(history) <= 50
        assert history[-1]["exit_code"] == payload["exit_code"]


# ===========================================================================
# The endpoints
# ===========================================================================


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


class TestCompletenessEndpoint:
    def test_it_refuses_an_anonymous_caller(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            assert client.get("/kaizen/admin/completeness").status_code in (401, 403)
            assert client.get("/kaizen/admin/sweep").status_code in (401, 403)
            assert client.post("/kaizen/admin/sweep", json={}).status_code in (401, 403)

    def test_it_reports_the_grading_and_its_own_limits(self, admin_client):
        response = admin_client.get("/kaizen/admin/completeness")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] > 0
        assert body["states"] == [
            "absent", "declared_only", "stub", "partial", "untested", "complete"
        ]
        assert "blind_probes" in body
        assert "orphan_probes" in body
        assert "iter_authz_routes" in body["route_table_note"]
        # And the deferred count travels with the failure count.
        assert "subflows_deferred" in body["flows_measurements"]

    def test_a_level_filters_nothing_and_adds_a_grading(self, admin_client):
        unfiltered = admin_client.get("/kaizen/admin/completeness").json()
        assert "measurements" not in unfiltered
        graded = admin_client.get("/kaizen/admin/completeness?level=l4_live").json()
        assert graded["measurements"]["for_level"] == "l4_live"
        # Every row still ships; the level grades rather than hides.
        assert len(graded["capabilities"]) == len(unfiltered["capabilities"])

    def test_an_unknown_level_is_refused_rather_than_graded_as_harmless(self):
        """A typo must not read as "nothing is at stake"."""
        from app import release_ladder

        assert release_ladder.required_gates_for("l5_nonsense") == ()
        # And the ladder refuses it before asking for gates at all.
        from app import release_ladder as R

        candidate = R.ReleaseCandidate("c", "code:a", "data:r", level="l5_nonsense")
        decision = R.advance_candidate(candidate)
        assert decision["advanced"] is False
        assert "unknown level" in decision["reason"]


class TestSweepEndpoint:
    def test_it_runs_a_sweep_and_writes_nothing(self, admin_client):
        response = admin_client.post("/kaizen/admin/sweep", json={"include_shadow": False})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["sweep"]["flows"]["ok"] is True
        assert "completeness" in body["summary"]
        assert body["sweep"].get("append") is None

    def test_it_refuses_to_append(self, admin_client):
        """An endpoint a dashboard can poll must not be able to write the log.

        422 rather than 400, which is this router's convention for a body that
        is semantically wrong rather than malformed -- the same code the other
        validation refusals use.
        """
        response = admin_client.post("/kaizen/admin/sweep", json={"append": True})
        assert response.status_code == 422
        assert "run_kaizen.py" in response.json()["detail"]

    def test_an_unknown_level_is_a_422(self, admin_client):
        response = admin_client.post("/kaizen/admin/sweep", json={"for_level": "l3"})
        assert response.status_code == 422

    def test_the_status_endpoint_works_without_a_sweep(self, admin_client):
        response = admin_client.get("/kaizen/admin/sweep")
        assert response.status_code == 200
        body = response.json()
        assert body["autorun_enabled"] is False
        assert "interval_seconds" in body

# ===========================================================================
# The flow filter
# ===========================================================================


class TestRunAllFlowsHonoursItsFlowIds:
    """``run_all_flows(flow_ids=...)`` used to be accepted and ignored.

    Two things made it survive. The loop read ``FLOW_CATALOG`` unconditionally,
    so a filtered call returned every run in the catalogue; and no caller and no
    test passed the argument, so there was nothing to fail. The parameter's own
    docstring claims this test by name, which is why it exists -- a docstring
    citing a test is a promise, and a promise with no test is the same defect one
    layer down.

    The failure mode being guarded against is specific and worth stating: a
    caller who asks for one flow, gets twenty-one runs, and reads the aggregate
    as if it were their subset's. Every aggregate this function produces --
    ``flows_run``, ``flows_failed``, ``subflows_deferred`` -- is then a
    whole-set number wearing a subset's label.
    """

    def test_a_filtered_call_returns_only_the_flows_asked_for(self):
        from app import real_life_flows

        wanted = "admin_governance_review"
        assert wanted in real_life_flows.FLOW_IDS, sorted(real_life_flows.FLOW_IDS)

        runs = real_life_flows.run_all_flows(flow_ids=[wanted])

        assert runs, "the filter returned nothing at all"
        assert {run.flow_id for run in runs} == {wanted}, (
            "the filter did not filter: "
            f"{sorted({run.flow_id for run in runs})}"
        )

    def test_no_filter_still_returns_the_whole_catalogue(self):
        """The other direction, so the first test cannot pass by breaking the default.

        Without this, a filter that returned exactly one flow always would satisfy
        the test above while ``run_all_flows()`` -- which every gate measurement
        depends on -- returned a single row.
        """
        from app import real_life_flows

        every = real_life_flows.run_all_flows()
        one = real_life_flows.run_all_flows(flow_ids=[real_life_flows.FLOW_IDS[0]])

        assert len({run.flow_id for run in every}) == len(real_life_flows.FLOW_IDS)
        assert len(every) > len(one), (
            f"filtering one flow left {len(one)} of {len(every)} runs"
        )

    def test_two_flows_return_the_union_and_nothing_more(self):
        from app import real_life_flows

        pair = list(real_life_flows.FLOW_IDS[:2])
        runs = real_life_flows.run_all_flows(flow_ids=pair)

        assert {run.flow_id for run in runs} == set(pair), sorted(
            {run.flow_id for run in runs}
        )
        # A repeated id collapses rather than running the flow twice, because two
        # copies of the same run would double every count the caller derives.
        again = real_life_flows.run_all_flows(flow_ids=pair + [pair[0]])
        assert len(again) == len(runs), (len(again), len(runs))

    def test_an_unknown_flow_id_is_refused_by_name(self):
        """An empty result would read as "everything passed".

        The caller asked for something that does not exist, and the answer they
        would otherwise get is an empty list -- which every aggregate built from
        it turns into ``flows_run: 0`` and, for several gates, into a pass.
        """
        from app import real_life_flows

        with pytest.raises(ValueError) as excinfo:
            real_life_flows.run_all_flows(flow_ids=["no_such_flow"])

        message = str(excinfo.value)
        assert "no_such_flow" in message, message
        assert "known:" in message, (
            "the refusal does not say what does exist, so it is not actionable: "
            f"{message}"
        )

    def test_one_unknown_id_among_known_ones_still_refuses(self):
        """Refusing only the unknown half would run a subset and call it the request."""
        from app import real_life_flows

        with pytest.raises(ValueError) as excinfo:
            real_life_flows.run_all_flows(
                flow_ids=[real_life_flows.FLOW_IDS[0], "typoed_flow"]
            )

        assert "typoed_flow" in str(excinfo.value), excinfo.value

    def test_an_empty_filter_runs_nothing_rather_than_everything(self):
        """``flow_ids=[]`` is a question about zero flows, not about all of them.

        Worth pinning because the obvious implementation -- ``if flow_ids:`` --
        treats the empty list as "no filter given" and returns everything, which
        is the exact inversion.
        """
        from app import real_life_flows

        assert real_life_flows.run_all_flows(flow_ids=[]) == []
        assert real_life_flows.run_all_flows(flow_ids=None), (
            "`None` must still mean 'no filter'"
        )
