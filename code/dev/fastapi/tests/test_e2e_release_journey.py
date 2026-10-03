"""Tier 3: the journey of a change, from a commit to live traffic and back.

Tiers 1 and 2 asked whether the *product* works. This tier asks whether the
**process around the product** works -- specifically the release ladder, whose
whole purpose is to be the last thing standing between a change and a customer
who is already using the system.

That makes it the one flow where the interesting assertions are mostly
**refusals**. Every other flow in this suite asserts that something happened;
this one asserts that things did not, and that they did not *for a reason the
response names*. A ladder that silently promotes is worse than no ladder,
because it is believed.

Three findings came out of writing it, and each one is a defect in the ladder
rather than in the test:

* ``regressions_none`` -- a blocking entry gate for ``l3_canary``, the first
  level at which a customer can be affected -- read a measurement named
  ``regressions`` that **nothing in the repository produced**. The only way past
  it was to type ``0`` into a request body. ``compare_runs`` had been producing
  the evidence all along as ``outcome_flips``; ``divergence_measurements`` now
  counts the regression half of them, and only that half, because a flip that
  *repairs* a broken probe is not a regression and counting it would mean a fix
  cannot ship until it is reverted.

* ``rollback_target_available`` and ``backend_completeness_honest`` guard
  ``l3_canary`` and ``l4_live`` and had no computable source either, so the last
  two rungs of a five-rung ladder were unlocked by typing three numbers. They
  now have sources -- ``rollback`` and ``completeness`` -- which means a caller
  who types ``{"rollback_targets": 99, "capability_blocking": 0}`` and names the
  sources gets the ledger's real count and the capability audit's real finding,
  which in this checkout is four. That is the whole of the anti-forgery
  mechanism and it is asserted here directly.

* A second rollback of an already-rolled-back event returned **201** and
  appended a no-op to the audit trail, immediately after
  ``rollback_targets``'s own docstring said that "rolling back to where you
  already are is not a rollback, it is a no-op that would append a misleading
  event to the audit trail". The exclusion was positional (skip the last event)
  where the invariant is about *version pairs*, and the two part company the
  moment a rollback appends.

The flow therefore does not walk a candidate to ``l4_live``. It walks it to the
ceiling the evidence honestly allows, proves the ceiling is real, and then
drives the ledger directly to test rollback -- which is a setup step, not a gate
being bypassed, and is called out as such where it happens.
"""

from __future__ import annotations

import pytest

from _e2e_world import mounted, world

# The cast's ids, named here rather than imported: `_e2e_world` exports the
# personas, and a test that reads `person(1).user_id` instead of saying `ANA` is
# a test making the reader look up what it means.
ANA = 1
ROOT = 5

#: The environment variables `shadow_env` reads to decide whether a shadow is
#: genuinely isolated. Six of them, and all six have to be right: `shadow_isolated`
#: is a conjunction, so satisfying five reports the sixth as a blocking failure,
#: which is the honest answer and not a passing one.
ISOLATED_SHADOW_ENV = {
    "DATABASE_URL": "postgresql://live:live@live-db.internal:5432/cservice",
    "CSERVICE_SHADOW_DATABASE_URL": "postgresql://shadow:shadow@shadow-db.internal:5432/cservice_shadow",
    "CSERVICE_ENV": "shadow",
    "CSERVICE_SHADOW_WRITE_TARGETS": "shadow_database",
    "CSERVICE_LIVE_READ_ONLY_REPLICATION": "1",
    "CSERVICE_SHADOW_EGRESS_ALLOWLIST": "127.0.0.1:5432",
}


@pytest.fixture()
def ladder(monkeypatch):
    """The release ladder's candidates and ledger, restored afterwards.

    Both are module-level singletons behind ``get_default_candidates`` and
    ``get_default_ledger``, and every route in this tier mutates them. A flow
    test that leaves a candidate at ``l2_shadow`` or a rollback event behind would
    make the *next* test in the session see a ladder that has history, which is
    the same class of leak as a shared database row and much harder to read when
    it lands.

    Restoring in a fixture rather than in each test means a test that fails
    half-way still leaves the tree clean.
    """
    from app import release_ladder

    saved_candidates = release_ladder.get_default_candidates()
    saved_ledger = release_ladder.get_default_ledger()

    class Ladder:
        """Thin accessor, so the tests read as journeys and not as plumbing."""

        module = release_ladder

        def register(self, candidate_id: str, **kwargs) -> dict:
            """Clear the ledger, then add one candidate and return its safety row.

            Registered directly rather than over HTTP because ``POST
            /kaizen/admin/candidates`` is exercised on its own, and a fixture that
            went through HTTP would make every test in this file depend on
            registration working before it could test anything else.
            """
            self.module.set_default_ledger(release_ladder.DeploymentLedger())
            return self.module.register_candidate(
                release_ladder.ReleaseCandidate(
                    candidate_id=candidate_id,
                    code_version=kwargs.pop("code_version", f"code:{candidate_id}"),
                    data_version=kwargs.pop("data_version", f"data:{candidate_id}"),
                    maintainer=kwargs.pop("maintainer", "root"),
                    summary=kwargs.pop("summary", "tier 3 journey"),
                    measured={},
                    **kwargs,
                )
            )

        def seed_deployments(self, *versions: str):
            """Write `code:`/`data:` deploy events for each version pair, in order.

            This is the one place the flow steps around the ladder, and it does so
            deliberately. Reaching ``l4_live`` -- the only level that writes a
            deployment event -- requires gates this checkout honestly fails, so
            seeding the ledger is a *setup* step rather than a way past them. The
            gates are tested separately, in
            :class:`TestTheCeilingIsReal`, and there they are shown to be holding.
            """
            ledger = self.module.get_default_ledger()
            return [
                ledger.record(
                    "deploy",
                    code_version=f"code:{version}",
                    data_version=f"data:{version}",
                    actor="root",
                    reason=f"deploy {version}",
                )
                for version in versions
            ]

        @property
        def levels(self) -> list[str]:
            return [row["level_id"] for row in self.module.MATURITY_LEVELS]

    yield Ladder()
    release_ladder.set_default_candidates(saved_candidates)
    release_ladder.set_default_ledger(saved_ledger)
    monkeypatch.undo()


class TestTheCeilingIsReal:
    """A candidate climbs as far as the evidence allows, and no further.

    "As far as the evidence allows" is the claim, so both halves are asserted:
    the promotions that happened, and the ones that did not.
    """

    def test_a_change_is_registered_at_draft_with_its_next_level_named(self, world, ladder):
        root = mounted(world, user_id=ROOT)

        created = root.json(
            "post",
            "/kaizen/admin/candidates",
            json={"candidate_id": "e2e-draft", "commit": "a1b2c3d", "revision": "rev-42"},
        )

        assert created["level"] == "l0_draft", created
        assert created["serves_traffic"] is False, created
        assert created["safe_to_advance"] is False, created
        assert created["next_level"] == "l1_verified", created
        assert created["code_version"] == "code:a1b2c3d", created
        assert created["data_version"] == "data:rev-42", created
        # The two things standing between a commit and a promotion are named before
        # anyone asks to promote it.
        assert set(created["unmeasured_gates"]) == {
            "unit_suite_green",
            "flow_simulation_clean",
        }, created
        assert created["measured"] == {}, (
            "a new candidate reported measurements; they come from `measure`, and "
            f"reporting them at creation hides which ones were asserted: {created}"
        )

    def test_a_request_to_skip_the_ladder_is_rejected_rather_than_ignored(self, world, ladder):
        """`extra="forbid"` on `CandidateIn` is load-bearing, and worth pinning.

        A candidate that could be created at ``l4_live`` would make every gate
        decorative. Silently dropping a field the caller believed in is the same
        class of defect as publishing a number that is not the enforced one: the
        two disagree and only one of them is visible.
        """
        root = mounted(world, user_id=ROOT)

        refused = root.post(
            "/kaizen/admin/candidates",
            json={"candidate_id": "e2e-skip", "commit": "a1b2c3d", "level": "l4_live"},
        )
        assert refused.status_code == 422, refused.text
        assert "level" in refused.text, refused.text
        assert "e2e-skip" not in [
            row["candidate_id"] for row in root.json("get", "/kaizen/admin/candidates")["candidates"]
        ], "a refused request still registered the candidate"

    def test_a_customer_can_do_none_of_this(self, world, ladder):
        ana = mounted(world, user_id=ANA)

        assert ana.post(
            "/kaizen/admin/candidates", json={"candidate_id": "e2e-x", "commit": "a1b2c3d"}
        ).status_code == 403
        ladder.register("e2e-authority")
        assert ana.post("/kaizen/admin/candidates/e2e-authority/advance", json={}).status_code == 403
        assert ana.post(
            "/kaizen/admin/candidates/e2e-authority/measure", json={"measured": {"regressions": 0}}
        ).status_code == 403
        assert ana.post("/kaizen/admin/deployments/dep-0001/rollback", json={}).status_code == 403
        assert ana.get("/kaizen/admin/deployments").status_code == 403

    def test_a_candidate_promotes_to_verified_on_real_evidence(self, world, ladder):
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-l1")

        # Nothing measured: the advance is a question, and it is answered.
        asked = root.json("post", "/kaizen/admin/candidates/e2e-l1/advance", json={})
        assert asked["advanced"] is False, asked
        assert asked["to_level"] is None, asked
        assert "unit_suite_green" in asked["reason"], asked
        assert asked["candidate"]["level"] == "l0_draft", (
            "a refused advance still moved the candidate: " + repr(asked)
        )

        measured = root.json(
            "post",
            "/kaizen/admin/candidates/e2e-l1/measure",
            json={"measured": {"tests_failed": 0}, "from_app": ["flows"]},
        )
        assert measured["measured"]["flows_run"] > 0, measured
        assert measured["measured"]["flows_failed"] == 0, measured

        promoted = root.json("post", "/kaizen/admin/candidates/e2e-l1/advance", json={})
        assert promoted["advanced"] is True, promoted
        assert (promoted["from_level"], promoted["to_level"]) == (
            "l0_draft",
            "l1_verified",
        ), promoted
        # Nobody is served by a verified candidate, and the ladder says so in the
        # promotion response rather than only in the levels table.
        assert promoted["candidate"]["serves_traffic"] is False, promoted

    def test_it_stops_at_verified_here_because_the_shadow_is_not_isolated(self, world, ladder):
        """The honest ceiling of a checkout with no shadow, asserted as a fact.

        This is the negative half of the tier. A test that only walks a candidate
        up passes just as happily against a ladder that promotes on anything, and
        the difference between those two ladders is the difference between a
        control and a decoration.
        """
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-ceiling")
        root.json(
            "post",
            "/kaizen/admin/candidates/e2e-ceiling/measure",
            json={
                "measured": {"tests_failed": 0, "pipelines_one_way": True, "shadow_to_live_writes": 0},
                "from_app": ["flows", "shadow"],
            },
        )
        root.json("post", "/kaizen/admin/candidates/e2e-ceiling/advance", json={})

        # The shadow is genuinely not configured here, so the source reports the
        # real answer rather than a default that would let the gate pass.
        shadow_state = root.json(
            "post",
            "/kaizen/admin/candidates/e2e-ceiling/measure",
            json={"from_app": ["shadow"]},
        )["measured"]
        assert shadow_state["shadow_isolated"] is False, shadow_state
        assert shadow_state["shadow_blocking_failures"], (
            "isolation failed without naming what failed: " + repr(shadow_state)
        )

        blocked = root.json("post", "/kaizen/admin/candidates/e2e-ceiling/advance", json={})
        assert blocked["advanced"] is False, blocked
        assert blocked["gate_report"]["blocking_failures"] == ["shadow_isolation_clean"], blocked
        gate = blocked["gate_report"]["gates"][0]
        assert gate["result"] == "fail", (
            f"an unconfigured shadow should read as a failed gate, not as an "
            f"unmeasured one: an unmeasured gate invites a number to be typed in, "
            f"and a failed one does not. gate={gate}"
        )
        assert blocked["candidate"]["level"] == "l1_verified", blocked

    def test_with_a_real_shadow_it_reaches_the_second_rung(self, world, ladder, monkeypatch):
        for name, value in ISOLATED_SHADOW_ENV.items():
            monkeypatch.setenv(name, value)
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-l2")

        root.json(
            "post",
            "/kaizen/admin/candidates/e2e-l2/measure",
            json={
                "measured": {"tests_failed": 0, "pipelines_one_way": True, "shadow_to_live_writes": 0},
                "from_app": ["flows", "shadow"],
            },
        )
        first = root.json("post", "/kaizen/admin/candidates/e2e-l2/advance", json={})
        assert (first["advanced"], first["to_level"]) == (True, "l1_verified"), first
        second = root.json("post", "/kaizen/admin/candidates/e2e-l2/advance", json={})
        assert (second["advanced"], second["to_level"]) == (True, "l2_shadow"), second
        assert second["candidate"]["serves_traffic"] is False, (
            "l2_shadow serves nobody, and this is the last level that can say so"
        )
        # Two rungs climbed, two separate advances. `advance` moves exactly one
        # level per call -- asserted rather than assumed, because a ladder that
        # skipped a rung would still report `advanced: true` here.
        assert (first["from_level"], second["from_level"]) == ("l0_draft", "l1_verified"), (
            first, second
        )

    def test_the_third_rung_is_gated_by_a_real_finding(self, world, ladder, monkeypatch):
        """`l3_canary` is the first level a customer can be affected by.

        So this asserts all three of its blockers, by name. It is also the test
        that would fail if someone deleted a gate from ``MATURITY_LEVELS``, which
        is the way a ladder quietly loses a rung.
        """
        for name, value in ISOLATED_SHADOW_ENV.items():
            monkeypatch.setenv(name, value)
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-l3")
        ladder.seed_deployments("a", "b", "c")

        root.json(
            "post",
            "/kaizen/admin/candidates/e2e-l3/measure",
            json={
                "measured": {
                    "tests_failed": 0,
                    "pipelines_one_way": True,
                    "shadow_to_live_writes": 0,
                },
                "from_app": ["flows", "shadow"],
            },
        )
        root.json("post", "/kaizen/admin/candidates/e2e-l3/advance", json={})
        root.json("post", "/kaizen/admin/candidates/e2e-l3/advance", json={})
        assert ladder.module.find_candidate("e2e-l3").level == "l2_shadow", (
            "the precondition for this test is a candidate at l2_shadow"
        )

        # Ask for the divergence, rollback and completeness evidence, and *also*
        # type the numbers those three gates read. The sources win.
        measured = root.json(
            "post",
            "/kaizen/admin/candidates/e2e-l3/measure",
            json={
                "measured": {
                    "regressions": 0,
                    "rollback_targets": 99,
                    "capability_blocking": 0,
                },
                "from_app": ["divergence", "rollback", "completeness", "authz"],
            },
        )["measured"]

        assert measured["regressions"] == 0, measured
        assert measured["regressions_improvements"] == 0, measured
        assert measured["rollback_targets"] == 2, (
            f"the ledger holds 2 reachable targets and the typed 99 survived: {measured}"
        )
        assert measured["capability_blocking"] > 0, (
            "capability_blocking is a real count in this checkout and 0 was typed; "
            f"the app's number must win: {measured}"
        )
        assert measured["capability_blocking_detail"], (
            "a non-zero count with no detail is a number nobody can act on: "
            f"{measured}"
        )

        blocked = root.json("post", "/kaizen/admin/candidates/e2e-l3/advance", json={})
        assert blocked["advanced"] is False, blocked
        # `backend_completeness_honest` is the one still failing, and it is failing
        # on a real capability finding rather than on a missing number.
        assert set(blocked["gate_report"]["blocking_failures"]) == {
            "backend_completeness_honest"
        }, blocked
        assert blocked["gate_report"]["unmeasured_gates"] == [], blocked
        assert blocked["candidate"]["level"] == "l2_shadow", blocked
        # Nobody was served by the attempt. This is the assertion that makes the
        # other five worth having: `l3_canary` is the first level that can affect
        # a customer, and it is exactly here that a forged `capability_blocking: 0`
        # would have put the candidate at a level serving real traffic.
        assert blocked["candidate"]["serves_traffic"] is False, blocked

    def test_a_third_rung_with_no_deployment_history_is_blocked_on_that_too(
        self, world, ladder, monkeypatch
    ):
        """The same gate, the other direction.

        The completeness gate above blocks on a real finding. This one blocks on
        there being nothing to roll back to -- the other reason `l3_canary` is the
        first level that can be reached at all. Asserting only the first would
        leave a ladder that promotes on the strength of a capability count with no
        way back.
        """
        for name, value in ISOLATED_SHADOW_ENV.items():
            monkeypatch.setenv(name, value)
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-no-history")
        # Deliberately no `seed_deployments`: this is the empty ledger.

        root.json(
            "post",
            "/kaizen/admin/candidates/e2e-no-history/measure",
            json={
                "measured": {"tests_failed": 0, "pipelines_one_way": True, "shadow_to_live_writes": 0},
                "from_app": ["flows", "shadow"],
            },
        )
        root.json("post", "/kaizen/admin/candidates/e2e-no-history/advance", json={})
        root.json("post", "/kaizen/admin/candidates/e2e-no-history/advance", json={})

        blocked = root.json("post", "/kaizen/admin/candidates/e2e-no-history/advance", json={})
        assert blocked["advanced"] is False, blocked
        assert "rollback_target_available" in blocked["gate_report"]["blocking_failures"], (
            f"a candidate reached l3_canary with nothing to roll back to: {blocked}"
        )
        assert blocked["candidate"]["serves_traffic"] is False, blocked


class TestTheMeasurementSources:
    """What each source computes, and the fact that they are not free assertions.

    These are the load-bearing assertions of the tier. The ladder's claim is that
    a measurement about the running system is *read*, not *believed*; before this
    tier only one of its seven sources (``authz``) had that property.
    """

    def test_the_three_new_sources_are_registered_where_the_old_ones_are(self, world, ladder):
        root = mounted(world, user_id=ROOT)

        sources = root.json("get", "/kaizen/admin/catalog")["measurement_sources"]
        assert {"completeness", "divergence", "rollback"} <= set(sources), sorted(sources)

    def test_every_gate_about_this_application_has_a_source_that_computes_it(
        self, world, ladder
    ):
        """The ladder's blocking gates split in two, and only one kind may be typed.

        A blocking gate is satisfied by one of two things: something this process
        computes, or something an operator observed elsewhere and asserts. A test
        runner's failure count is the second kind -- the app genuinely cannot know
        it, and refusing to accept it would make the gate unsatisfiable rather
        than honest. The rollback ledger's depth is the first kind: the app *is*
        the ledger, and before this tier it was in the second class anyway.

        So the test asserts the classification in both directions. It is derived
        from the gate table rather than restated, so a new gate added without a
        source fails here instead of quietly becoming a hand-typed fifth rung.
        """
        from app import release_ladder

        #: The blocking gates whose subject lives outside this process. Each is
        #: asserted to be exactly this list, so a gate cannot be moved into the
        #: operator-asserted class to make this test pass.
        OBSERVED_ELSEWHERE = {
            "unit_suite_green",  # a test runner's report
            "data_pipelines_one_way",  # a pipeline run's own audit
            "canary_error_rate_below_threshold",  # production monitoring
        }

        ladder.register("e2e-source-audit")

        # Run every source once and collect the keys it produced. `flows` and
        # `divergence` are the expensive ones; this pays for them deliberately,
        # which is cheaper than faking the answer it is checking.
        root = mounted(world, user_id=ROOT)
        computed: set[str] = set()
        for name in ("authz", "care", "flows", "shadow", "completeness", "divergence", "rollback"):
            body = root.json(
                "post",
                "/kaizen/admin/candidates/e2e-source-audit/measure",
                json={"from_app": [name]},
            )["measured"]
            assert isinstance(body, dict) and body, f"{name} produced nothing"
            computed |= set(body)

        # Every blocking gate guarding a level at or above the first rung, plus the
        # advisory ones, so a demoted gate cannot escape by being reclassified.
        rungs = {"l1_verified", "l2_shadow", "l3_canary", "l4_live"}
        blocking = {
            gate_id
            for level in release_ladder.MATURITY_LEVELS
            if level["level_id"] in rungs
            for gate_id in level["entry_gates"]
        }
        assert blocking, "the ladder stopped publishing entry gates"

        # Direction one: anything not asserted from outside must be computed here.
        for gate_id in sorted(blocking - OBSERVED_ELSEWHERE):
            checks = set(release_ladder.PROMOTION_GATE_BY_ID[gate_id].get("checks") or ())
            missing = {name for name in checks if name not in computed}
            assert not missing, (
                f"gate {gate_id!r} reads {sorted(missing)}, which no measurement "
                "source computes and which is not on OBSERVED_ELSEWHERE -- so the "
                "only way to satisfy it is to type the number into a request body. "
                f"Sources produced: {sorted(computed)}"
            )

        # Direction two: the operator-asserted list is exactly the gates about
        # things outside this process, so it cannot be used as a parking space.
        stale = OBSERVED_ELSEWHERE - blocking
        assert not stale, (
            f"{sorted(stale)} no longer gate anything; leaving them in the "
            "operator-asserted set would let the next gate be excused the same way"
        )

    def test_asking_for_the_same_run_set_twice_does_not_pay_for_it_twice(self, world, ladder, monkeypatch):
        """`completeness` and `divergence` share `flows`' run set.

        `run_all_flows` is the expensive call in this application. Before the
        sources were taught to share it, naming two of them cost two full
        simulations, which reads as "the measurement endpoint is slow" rather than
        as the duplication it was.
        """
        from app import real_life_flows

        calls = {"n": 0}
        real = real_life_flows.run_all_flows

        def counting(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(real_life_flows, "run_all_flows", counting)
        ladder.register("e2e-share")
        root = mounted(world, user_id=ROOT)

        root.json(
            "post",
            "/kaizen/admin/candidates/e2e-share/measure",
            json={"from_app": ["flows", "completeness", "divergence"]},
        )

        assert calls["n"] == 1, (
            f"three sources that all need the flow run set invoked it "
            f"{calls['n']} times"
        )

    def test_a_source_named_alongside_a_typed_number_wins(self, world, ladder):
        """The mechanism itself, isolated from the three gates it protects.

        Deliberately narrow: this is the property, and the three gates are
        applications of it. If the merge order in `measure_candidate` is ever
        flipped, this fails and the gate tests fail with a less obvious message.
        """
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-precedence")
        ladder.seed_deployments("a", "b")

        measured = root.json(
            "post",
            "/kaizen/admin/candidates/e2e-precedence/measure",
            json={"measured": {"rollback_targets": 12345}, "from_app": ["rollback"]},
        )["measured"]

        assert measured["rollback_targets"] == 1, (
            "a hand-typed rollback depth survived a request that named the source "
            f"which computes it: {measured}"
        )
        # The source also publishes the policy the gate compares against, so the
        # number on the candidate can be checked against the bar it has to clear.
        assert measured["min_rollback_targets"] == 1, measured
        assert measured["rollback_window"] == 5, measured
        assert measured["rollback_window_satisfied"] is True, measured

    def test_the_regressions_source_counts_only_the_damaging_half_of_a_flip(
        self, world, ladder
    ):
        """A flip that repairs a probe is not a regression.

        Counting both halves would mean a change that fixes two broken subflows is
        blocked until it is reverted, which is the gate inverting its own purpose.
        """
        from app import real_life_flows

        comparison = {
            "divergence_ratio": 0.5,
            "tolerance": 0.25,
            "outcome_flips": [
                {"flow_id": "f1", "persona_id": "ana", "subflow_id": "s1",
                 "baseline_held": True, "candidate_held": False},
                {"flow_id": "f2", "persona_id": "bruno", "subflow_id": "s2",
                 "baseline_held": False, "candidate_held": True},
                {"flow_id": "f3", "persona_id": "chiara", "subflow_id": "s3",
                 "baseline_held": True, "candidate_held": False},
            ],
        }

        measured = real_life_flows.divergence_measurements(comparison)

        assert measured["regressions"] == 2, measured
        assert measured["regressions_improvements"] == 1, measured
        assert measured["outcome_flips"] == 3, measured
        assert measured["regressions_detail"] == ["f1/ana/s1", "f3/chiara/s3"], (
            f"a regression with no identity cannot be looked up: {measured}"
        )
        # The ratio is passed through untouched: it counts both halves on purpose,
        # because divergence is divergence whichever way it points.
        assert measured["shadow_divergence_ratio"] == 0.5, measured
        assert measured["divergence_tolerance"] == 0.25, measured


class TestRollback:
    """Three deploys, one rollback, and the three things that must not happen.

    Deploy -> rollback is the flow with money in it, except the money is other
    people's time during an incident. So the property worth testing is not that
    a rollback works -- it is that it cannot be made to *look* like it did.
    """

    def test_a_rollback_restores_code_and_data_together_and_appends(self, world, ladder):
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-deploy")
        deployed = ladder.seed_deployments("a", "b", "c")
        assert len(deployed) == 3, deployed

        ledger_before = root.json("get", "/kaizen/admin/deployments")
        assert ledger_before["ledger"]["current"]["code_version"] == "code:c", ledger_before
        assert ledger_before["ledger"]["rollback_targets"] == ["dep-0002", "dep-0001"], (
            f"the offered targets must exclude what is already serving: {ledger_before}"
        )
        assert ledger_before["ledger"]["window_satisfied"] is True, ledger_before
        # Two of five: enough for the gate, nowhere near the whole window. Reported
        # so an operator can see how little margin there is.
        assert ledger_before["ledger"]["window_reach"] == 2, ledger_before
        assert ledger_before["ledger"]["full_window_reachable"] is False, ledger_before

        restored = root.json(
            "post",
            "/kaizen/admin/deployments/dep-0001/rollback",
            json={"reason": "error rate above threshold", "actor": "oncall"},
        )

        event = restored["event"]
        assert event["kind"] == "rollback", event
        assert event["restores"] == "dep-0001", event
        # The pair moves together. Code at an older revision against data at a
        # newer one is the state this endpoint exists to prevent.
        assert (event["code_version"], event["data_version"]) == ("code:a", "data:a"), event
        assert event["actor"] == "oncall", event
        assert restored["ledger"]["current"]["code_version"] == "code:a", restored
        # Appends, never rewrites: the deploy it undid is still in the sequence.
        assert restored["ledger"]["events"] == 4, restored["ledger"]

        # The rollback response carries the new event and the ledger's own summary,
        # not the event list -- so the order is checked by reading it back.
        after = root.json("get", "/kaizen/admin/deployments")
        assert [row["event_id"] for row in after["events"]][-1] == event["event_id"], (
            "the rollback was not appended last, so the audit trail is out of order"
        )
        sequences = [row["sequence"] for row in after["events"]]
        assert sequences == sorted(set(sequences)), (
            f"the ledger sequence must be strictly increasing and gapless: {sequences}"
        )
        # And the three deploys are all still there, so nothing was rewritten.
        assert [row["kind"] for row in after["events"]] == [
            "deploy",
            "deploy",
            "deploy",
            "rollback",
        ], after["events"]

    def test_a_rollback_to_what_is_already_serving_is_refused(self, world, ladder):
        """The defect this tier found, asserted as fixed.

        ``rollback_targets`` excluded only the *last* event. A rollback appends,
        so after rolling back to ``dep-0001`` that event sits behind a newer one
        carrying the same pair -- and the endpoint answered 201, appending a second
        ``rollback`` event that changed nothing, immediately after the function's
        own docstring said it must not.
        """
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-double")
        ladder.seed_deployments("a", "b", "c")

        root.json("post", "/kaizen/admin/deployments/dep-0001/rollback", json={})
        assert root.json("get", "/kaizen/admin/deployments")["ledger"]["current"][
            "code_version"
        ] == "code:a"

        refused = root.post("/kaizen/admin/deployments/dep-0001/rollback", json={})
        assert refused.status_code == 409, (
            f"rolling back to the version already serving should be refused, not "
            f"appended: {refused.status_code} {refused.text}"
        )
        assert "already the version being served" in refused.text, refused.text
        # The message must not blame the window. dep-0001 is inside the window and
        # ineligible for a different reason, and "outside the window" would send an
        # operator looking for a pruning bug that does not exist.
        assert "rollback window" not in refused.text, refused.text

        after = root.json("get", "/kaizen/admin/deployments")
        assert after["ledger"]["events"] == 4, (
            f"the refused rollback still appended an event: {after['ledger']['events']}"
        )
        # And it is no longer offered.
        assert "dep-0001" not in after["ledger"]["rollback_targets"], after["ledger"]

    def test_an_undo_of_an_undo_is_still_a_real_rollback(self, world, ladder):
        """The complement: excluding *what is served* must not over-exclude.

        Rolling forward again is a legitimate action -- the bad deploy is fixed by
        shipping a patch -- and it restores a pair that is not currently served, so
        it has to remain possible.
        """
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-forward")
        ladder.seed_deployments("a", "b", "c")
        root.json("post", "/kaizen/admin/deployments/dep-0001/rollback", json={})

        forward = root.json(
            "post", "/kaizen/admin/deployments/dep-0003/rollback", json={"reason": "patched"}
        )
        assert forward["event"]["kind"] == "rollback", forward
        assert forward["event"]["restores"] == "dep-0003", forward
        assert forward["ledger"]["current"]["code_version"] == "code:c", forward["ledger"]
        assert forward["ledger"]["events"] == 5, forward["ledger"]

    def test_a_rollback_to_an_unknown_event_is_a_404_and_not_a_400(self, world, ladder):
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-unknown")
        ladder.seed_deployments("a", "b")

        missing = root.post("/kaizen/admin/deployments/dep-9999/rollback", json={})
        assert missing.status_code == 404, missing.text
        # `dep-0002` is the only live event besides the current one... which means
        # with two deploys there is exactly one target and it is `dep-0001`.
        assert root.json("get", "/kaizen/admin/deployments")["ledger"]["rollback_targets"] == [
            "dep-0001"
        ]
        outside = root.post("/kaizen/admin/deployments/dep-0002/rollback", json={})
        assert outside.status_code == 409, (
            "rolling back to the currently-serving event is a conflict, not a "
            f"not-found: {outside.status_code} {outside.text}"
        )

    def test_an_empty_ledger_offers_nothing_and_says_why(self, world, ladder):
        root = mounted(world, user_id=ROOT)
        ladder.register("e2e-empty")

        empty = root.json("get", "/kaizen/admin/deployments")
        assert empty["events"] == [], empty
        assert empty["ledger"]["rollback_targets"] == [], empty["ledger"]
        assert empty["ledger"]["window_satisfied"] is False, empty["ledger"]
        assert empty["ledger"]["current"] is None, empty["ledger"]
        assert empty["min_rollback_targets"] >= 1, empty

        refused = root.post("/kaizen/admin/deployments/dep-0001/rollback", json={})
        assert refused.status_code == 404, refused.text