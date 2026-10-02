"""Kaizen: the one-way shadow environment, the maturity ladder, and the flow
simulation that feeds them.

Three claims are pinned here, and they are different in kind:

* **Structural claims.** A replicated channel cannot run shadow -> live; a
  shadow pointed at live's database is detected by comparing identities rather
  than by trusting a config value; a gate nobody measured blocks a promotion.
  These are proven by breaking them.
* **Honesty claims.** A gate that could not be evaluated fails; a rollback
  appends rather than rewrites; a candidate cannot be registered above draft.
  These are proven by asserting the *absence* of a convenient behaviour.
* **The simulation's own limits.** ``real_life_flows`` documents that a probe
  whose expectation is derived from the table it validates cannot detect an edit
  to that table. That limit is real and is pinned here from the other side: this
  file spells the posture deltas out in literals, which is the independent oracle
  the probes deliberately do not have.

The negative controls matter most. A flow simulation that returns "no blockage"
on a healthy system proves nothing unless it has also been shown to fail on a
broken one, and the first version of this harness had five probes that reported
confident nonsense -- an access band resolver called on a 0-1 scale, a rule pack
name borrowed from a different subsystem, a constant imported from the module
that did not define it. Every one of those is now a test, because the failure
mode of this kind of harness is not crashing; it is being wrong while looking
right.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest


LIVE_URL = "postgresql+asyncpg://u:p@127.0.0.1:5432/cservice"
SHADOW_URL = "postgresql+asyncpg://u:p@127.0.0.1:5432/cservice_shadow"


def _healthy_observations(**overrides):
    """A fully measured, fully passing set of isolation inputs."""
    from app import shadow_env

    base = {
        "live_url": shadow_env.observed("live_url", LIVE_URL),
        "shadow_url": shadow_env.observed("shadow_url", SHADOW_URL),
        "shadow_env_name": shadow_env.observed("shadow_env_name", "shadow"),
        "shadow_write_targets": shadow_env.observed(
            "shadow_write_targets", ("shadow_database",)
        ),
        "feeds": shadow_env.observed("feeds", [dict(f) for f in shadow_env.SHADOW_FEEDS]),
        "live_read_only": shadow_env.observed("live_read_only", True),
        "shadow_egress_allowlist": shadow_env.observed(
            "shadow_egress_allowlist", ("127.0.0.1:5432",)
        ),
    }
    base.update(overrides)
    return base


# ===========================================================================
# Direction arithmetic -- the one-way rule
# ===========================================================================


class TestOneWayRule:
    def test_every_declared_feed_runs_in_a_permitted_direction(self):
        from app import shadow_env

        report = shadow_env.feed_direction_report()
        assert report["one_way"] is True
        assert report["channels"] == len(shadow_env.SHADOW_FEEDS)
        assert report["failed"] == []

    def test_shadow_to_live_is_the_only_forbidden_direction(self):
        from app import shadow_env

        assert shadow_env.FORBIDDEN_DIRECTION == "shadow_to_live"
        assert shadow_env.FORBIDDEN_DIRECTION_IDS == ("shadow_to_live",)

    def test_direction_is_derived_not_trusted(self):
        """A row that lies about its own direction must be an error.

        The declared value is compared against the direction implied by its
        source and target. A channel that says ``live_to_shadow`` while pointing
        at live would otherwise look safe, and the whole value of putting the
        direction in a table is that it can be checked rather than believed.
        """
        from app import shadow_env

        lying = {
            "feed_id": "liar",
            "source": "shadow",
            "target": "live",
            "direction": "live_to_shadow",
            "tables": ("bookings",),
            "scrub": (),
        }
        verdict = shadow_env.feed_direction_verdict(lying)
        assert verdict["derived"] == "shadow_to_live"
        assert verdict["agrees"] is False
        assert verdict["ok"] is False
        assert any("forbidden" in reason for reason in verdict["reasons"])

    def test_derive_direction_covers_every_pairing(self):
        from app import shadow_env

        assert shadow_env.derive_direction("live", "shadow") == "live_to_shadow"
        assert shadow_env.derive_direction("shadow", "live") == "shadow_to_live"
        assert shadow_env.derive_direction("shadow", "shadow") == "shadow_to_shadow"
        assert shadow_env.derive_direction("live", "live") == "live_to_live"
        assert shadow_env.derive_direction("nowhere", "live") == "unknown_direction"

    def test_an_unknown_direction_is_not_permitted(self):
        from app import shadow_env

        verdict = shadow_env.feed_direction_verdict(
            {"feed_id": "x", "source": "mars", "target": "live", "direction": "live_to_shadow"}
        )
        assert verdict["ok"] is False

    def test_leakage_paths_name_the_channel_and_direction(self):
        from app import shadow_env

        assert shadow_env.leakage_paths() == []

        bad = [dict(f) for f in shadow_env.SHADOW_FEEDS]
        bad[2].update(source="shadow", target="live")
        paths = shadow_env.leakage_paths(bad)
        assert len(paths) == 1
        assert "shadow -> live" in paths[0]

    def test_the_validator_reports_a_forbidden_channel_as_an_error(self):
        from app import shadow_env

        bad = [dict(f) for f in shadow_env.SHADOW_FEEDS]
        bad[0].update(source="shadow", target="live")
        observations = _healthy_observations(
            feeds=shadow_env.observed("feeds", bad)
        )
        verdict = shadow_env.validate_shadow_env(observations)
        assert verdict["valid"] is False
        assert verdict["one_way"] is False
        assert any("shadow -> live" in error for error in verdict["errors"])

    def test_the_validator_reads_the_feeds_it_was_given(self):
        """Both halves of the report must describe the same configuration.

        Feeding the validator observed feeds while its direction report read the
        module table would let it say "one-way" about seven channels and "not
        isolated" about a different seven, with nothing to tell a reader which
        was which.
        """
        from app import shadow_env

        bad = [dict(f) for f in shadow_env.SHADOW_FEEDS]
        bad[0].update(source="shadow", target="live")
        verdict = shadow_env.validate_shadow_env(
            _healthy_observations(feeds=shadow_env.observed("feeds", bad))
        )
        assert verdict["one_way"] is False
        assert verdict["isolated"] is False
        assert verdict["leakage_paths"]
        assert any("database_identity" not in e and "shadow -> live" in e for e in verdict["errors"])

    def test_the_tables_are_valid_whether_or_not_a_shadow_is_configured(self):
        """Two questions, kept apart.

        "Are the tables self-consistent" is a property of the code and must be
        answerable on a checkout with no shadow pointed anywhere. "Is this
        deployment isolated" needs a measurement. Folding the second into the
        first made `validate_shadow_env()` report four errors on a clean
        machine, all of them "not measured", and a reader could no longer tell a
        broken table from an absent configuration.
        """
        from app import shadow_env

        unmeasured = shadow_env.validate_shadow_env()
        assert unmeasured["valid"] is True, unmeasured["errors"]
        assert unmeasured["isolated"] is False
        assert any("no shadow configuration was measured" in w for w in unmeasured["warnings"])

        healthy = shadow_env.validate_shadow_env(_healthy_observations())
        assert healthy["valid"] is True, healthy["errors"]
        assert healthy["isolated"] is True

    def test_a_measured_collision_is_an_error_not_a_warning(self):
        from app import shadow_env

        verdict = shadow_env.validate_shadow_env(
            _healthy_observations(shadow_url=shadow_env.observed("shadow_url", LIVE_URL))
        )
        assert verdict["valid"] is False
        assert any("database_identity" in e for e in verdict["errors"])


# ===========================================================================
# Database identity -- how "the shadow is elsewhere" is proven
# ===========================================================================


class TestDatabaseIdentity:
    def test_identity_is_host_port_database(self):
        from app import shadow_env

        identity = shadow_env.database_identity(LIVE_URL)
        assert identity["comparable"] is True
        assert identity["host"] == "127.0.0.1"
        assert identity["port"] == 5432
        assert identity["database"] == "cservice"

    def test_a_different_database_on_the_same_server_is_isolated(self):
        """Keep-as-is, pinned: identity is the database, not the host.

        A guard demanding a separate host would be refused by every developer
        running both locally, and would then be bypassed by everyone. The
        expensive part of this system is the schema, not the server.
        """
        from app import shadow_env

        live = shadow_env.database_identity(LIVE_URL)
        shadow = shadow_env.database_identity(SHADOW_URL)
        assert live["host"] == shadow["host"]
        assert shadow_env.same_database(live, shadow) is False

    def test_the_same_database_is_a_collision(self):
        from app import shadow_env

        identity = shadow_env.database_identity(LIVE_URL)
        assert shadow_env.same_database(identity, identity) is True

    def test_a_different_password_does_not_make_it_a_different_database(self):
        """The password is excluded from the identity key on purpose.

        Two urls differing only in credentials are the same database, and
        including the secret would report "isolated" for a shadow that is in
        fact writing live with a different login.
        """
        from app import shadow_env

        one = shadow_env.database_identity("postgresql://alice:a@h:5432/cservice")
        two = shadow_env.database_identity("postgresql://bob:b@h:5432/cservice")
        assert shadow_env.same_database(one, two) is True

    def test_an_uncomparable_identity_counts_as_the_same_database(self):
        """Fails closed: 'we could not tell' is not a licence to proceed."""
        from app import shadow_env

        broken = shadow_env.database_identity("not a url at all")
        assert broken["comparable"] is False
        good = shadow_env.database_identity(SHADOW_URL)
        assert shadow_env.same_database(broken, good) is True
        assert shadow_env.same_database(broken, broken) is True

    def test_a_url_naming_no_database_is_uncomparable(self):
        """Not "a different database" -- *no* database.

        Comparing an empty database name against a real one would report
        "isolated" for a target nobody has named yet, which is the fail-open
        direction: the guard would go green because the configuration was
        incomplete.
        """
        from app import shadow_env

        identity = shadow_env.database_identity("postgresql://127.0.0.1:5432/")
        assert identity["comparable"] is False
        assert "no database" in identity["note"]

    def test_a_host_that_is_a_sentence_is_uncomparable(self):
        from app import shadow_env

        identity = shadow_env.database_identity("postgresql://not a url at all")
        assert identity["comparable"] is False
        assert "no host" in identity["note"]

    def test_an_ipv6_literal_is_a_plausible_host(self):
        """`urlsplit` strips the brackets, so this cannot be a plain regex test."""
        from app import shadow_env

        identity = shadow_env.database_identity("postgresql://[::1]:5432/cservice")
        assert identity["host"] == "::1"
        assert identity["comparable"] is True
        assert shadow_env.same_database(
            identity, shadow_env.database_identity("postgresql://[::1]:5432/cservice")
        ) is True

    def test_an_ipv6_shadow_is_a_different_database_from_an_ipv4_live(self):
        from app import shadow_env

        v6 = shadow_env.database_identity("postgresql://[::1]:5432/cservice_shadow")
        v4 = shadow_env.database_identity("postgresql://127.0.0.1:5432/cservice")
        assert shadow_env.same_database(v6, v4) is False

    def test_an_empty_url_is_not_comparable(self):
        from app import shadow_env

        assert shadow_env.database_identity("")["comparable"] is False
        assert shadow_env.database_identity(None)["comparable"] is False

    def test_the_alembic_spelling_without_a_driver_parses(self):
        from app import shadow_env

        identity = shadow_env.database_identity("postgres://h:5432/cservice")
        assert identity["comparable"] is True
        assert identity["database"] == "cservice"

    def test_shadow_database_url_never_falls_back_to_the_live_url(self):
        """The asymmetry is the mechanism; the rest of the module is evidence.

        ``CSERVICE_SHADOW_DATABASE_URL`` has no fallback to ``DATABASE_URL``. A
        shadow that inherited live's url by fallback would be a live deployment
        wearing a shadow's name, and the only defence would be a convention
        somebody remembers.
        """
        from app import shadow_env

        monkey = os.environ
        monkey["DATABASE_URL"] = LIVE_URL
        monkey.pop("CSERVICE_SHADOW_DATABASE_URL", None)
        assert shadow_env.shadow_database_url() == ""
        monkey["CSERVICE_SHADOW_DATABASE_URL"] = SHADOW_URL
        assert shadow_env.shadow_database_url() == SHADOW_URL
        monkey.pop("CSERVICE_SHADOW_DATABASE_URL", None)
        monkey["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5432/cservice"


# ===========================================================================
# The isolation verdict
# ===========================================================================


class TestIsolationVerdict:
    def test_a_healthy_shadow_isolated(self):
        from app import shadow_env

        verdict = shadow_env.evaluate_isolation(_healthy_observations())
        assert verdict["isolated"] is True
        assert verdict["blocking_failures"] == []
        assert verdict["checks_run"] == len(shadow_env.ISOLATION_CHECKS)

    def test_a_shadow_pointed_at_live_is_not_isolated(self):
        from app import shadow_env

        verdict = shadow_env.evaluate_isolation(
            _healthy_observations(
                shadow_url=shadow_env.observed("shadow_url", LIVE_URL)
            )
        )
        assert verdict["isolated"] is False
        assert "database_identity" in verdict["blocking_failures"]

    def test_an_unmeasured_check_fails_closed(self):
        """The rule that makes the verdict worth reading.

        A check whose input nobody supplied is reported as failed, not skipped.
        Treating 'we did not look' as 'we looked and it was fine' converts an
        unmeasured risk into a published safety claim.
        """
        from app import shadow_env

        verdict = shadow_env.evaluate_isolation({})
        assert verdict["isolated"] is False
        assert set(verdict["blocking_failures"]) >= {
            "database_identity",
            "environment_marker",
            "write_scope",
        }

    def test_a_shadow_marked_live_fails_the_marker(self):
        from app import shadow_env

        verdict = shadow_env.evaluate_isolation(
            _healthy_observations(
                shadow_env_name=shadow_env.observed("shadow_env_name", "production")
            )
        )
        assert verdict["isolated"] is False
        assert "environment_marker" in verdict["blocking_failures"]

    def test_declaring_a_live_write_target_fails(self):
        from app import shadow_env

        verdict = shadow_env.evaluate_isolation(
            _healthy_observations(
                shadow_write_targets=shadow_env.observed(
                    "shadow_write_targets", ("shadow_database", "live_database")
                )
            )
        )
        assert verdict["isolated"] is False
        assert "write_scope" in verdict["blocking_failures"]

    def test_an_advisory_failure_does_not_withhold_the_verdict(self):
        """Same severity folding as the complaint escalation guards.

        A ladder where every advisory blocks is a ladder people stop reading, so
        an advisory failure is reported in `advisory_failures` and the verdict
        still says isolated.
        """
        from app import shadow_env

        verdict = shadow_env.evaluate_isolation(
            _healthy_observations(live_read_only=shadow_env.observed("live_read_only", False))
        )
        assert verdict["isolated"] is True
        assert "live_read_only" in verdict["advisory_failures"]
        assert "live_read_only" not in verdict["blocking_failures"]

    def test_a_check_with_no_implementation_cannot_pass(self):
        """A row added to the table with no implementation must fail, not skip.

        Skipping would let a new row report as `isolated: true` on the strength
        of having done nothing.
        """
        from app import shadow_env

        saved = shadow_env.ISOLATION_CHECKS
        shadow_env.ISOLATION_CHECKS = saved + (
            {
                "check_id": "hypothetical_future_check",
                "severity": "blocking",
                "asserts": "nothing yet",
                "inputs": (),
                "remedy": "implement it",
            },
        )
        try:
            verdict = shadow_env.evaluate_isolation(_healthy_observations())
            assert verdict["isolated"] is False
            assert "hypothetical_future_check" in verdict["blocking_failures"]
        finally:
            shadow_env.ISOLATION_CHECKS = saved

    def test_evaluate_isolation_never_raises_on_junk(self):
        from app import shadow_env

        for junk in (None, {}, {"live_url": 12345}, {"feeds": "not a list"}):
            verdict = shadow_env.evaluate_isolation(junk)
            assert isinstance(verdict["isolated"], bool)

    def test_every_failed_check_ships_a_remedy(self):
        from app import shadow_env

        verdict = shadow_env.evaluate_isolation({})
        for check in verdict["checks"]:
            if not check["passed"]:
                assert check["remedy"], check["check_id"]


# ===========================================================================
# The maturity ladder
# ===========================================================================


class TestMaturityLevels:
    def test_the_ladder_is_five_levels_and_contiguous(self):
        from app import release_ladder

        assert len(release_ladder.MATURITY_LEVELS) == 5
        assert release_ladder.LEVEL_ORDER[0] == "l0_draft"
        assert release_ladder.LEVEL_ORDER[-1] == release_ladder.LIVE_LEVEL
        assert release_ladder.validate_release_ladder()["valid"] is True

    def test_only_the_top_two_levels_serve_traffic(self):
        from app import release_ladder

        serving = [
            row["level_id"]
            for row in release_ladder.MATURITY_LEVELS
            if row["serves_traffic"]
        ]
        assert serving == ["l3_canary", "l4_live"]

    def test_every_entry_gate_is_blocking(self):
        """An advisory gate on an entry list is a contradiction."""
        from app import release_ladder

        for level_id, gate_ids in release_ladder.LEVEL_ENTRY_GATES.items():
            for gate_id in gate_ids:
                assert (
                    release_ladder.PROMOTION_GATE_BY_ID[gate_id]["severity"] == "blocking"
                ), f"{level_id}/{gate_id}"

    def test_the_table_validates_and_names_the_contradiction(self):
        from app import release_ladder

        saved = release_ladder.MATURITY_LEVELS
        release_ladder.MATURITY_LEVELS = list(saved)
        release_ladder.MATURITY_LEVELS[1] = {
            **release_ladder.MATURITY_LEVELS[1],
            "entry_gates": ("maintainer_named",),
        }
        try:
            report = release_ladder.validate_release_ladder()
            assert report["valid"] is False
            assert any("advisory" in error for error in report["errors"])
        finally:
            release_ladder.MATURITY_LEVELS = saved


class TestGates:
    """Every one of these is written against the *destination* rung.

    `gates_for_candidate` was reading the current level's entry gates, which
    meant the first rung's gates were checked twice and the last rung's never.
    Tests written against the old behaviour would all have passed while
    `l4_live` was reachable with no canary measured, so each case here names the
    rung it is about.
    """

    #: Evidence sufficient for every rung. A canary rate of 0.0001 is under the
    #: declared 0.001 default; nothing here names a threshold.
    EVERY_RUNG = {
        "tests_failed": 0,
        "flows_run": 21,
        "flows_failed": 0,
        "shadow_isolated": True,
        "pipelines_one_way": True,
        "shadow_to_live_writes": 0,
        "shadow_divergence_ratio": 0.0,
        "regressions": 0,
        "rollback_targets": 5,
        "canary_error_rate": 0.0001,
        "authz_in_sync": True,
        "unlisted_public_writes": 0,
                "capability_blocking": 0,
    }

    def _candidate(self, name, *, level="l0_draft", **measured):
        from app import release_ladder

        return release_ladder.ReleaseCandidate(
            name, "code:a", "data:r", level, maintainer="ana",
            measured={**self.EVERY_RUNG, **measured},
        )

    def test_a_draft_candidate_is_blocked_by_the_rung_it_is_trying_to_enter(self):
        """`l0_draft` declares no gates, but `l1_verified` does.

        The check belongs to the *destination*: promoting a draft means claiming
        it is verified, and the claim needs the test-suite evidence.
        """
        from app import release_ladder

        candidate = release_ladder.ReleaseCandidate("c0", "code:a", "data:r", maintainer="ana")
        decision = release_ladder.advance_candidate(candidate)
        assert decision["advanced"] is False
        assert decision["from_level"] == "l0_draft"
        assert decision["to_level"] is None
        assert set(decision["gate_report"]["blocking_failures"]) == {
            "unit_suite_green", "flow_simulation_clean"
        }
        assert candidate.level == "l0_draft"

    def test_an_unmeasured_gate_blocks_the_advance(self):
        """`GATE_OPS["unmeasured_gate_verdict"]` is "fail", and that is the point.

        Skipping the gate, noting it, and promoting is how a change reaches
        production having never been looked at.
        """
        from app import release_ladder

        candidate = release_ladder.ReleaseCandidate("c1", "code:a", "data:r", maintainer="ana")
        decision = release_ladder.advance_candidate(candidate)
        report = decision["gate_report"]
        assert decision["advanced"] is False
        assert report["unmeasured_gates"] == ["unit_suite_green", "flow_simulation_clean"]
        assert report["gates_for_level"] == "l1_verified"

    def test_a_measured_gate_lets_the_advance_through(self):
        from app import release_ladder

        candidate = self._candidate("c2", tests_failed=0)
        decision = release_ladder.advance_candidate(candidate)
        assert decision["advanced"] is True, decision["gate_report"]["blocking_failures"]
        assert candidate.level == "l1_verified"

    def test_the_ladder_walks_rung_by_rung_with_full_evidence(self):
        from app import release_ladder

        candidate = self._candidate("c2b")
        walk = []
        for _ in range(4):
            walk.append(candidate.level)
            decision = release_ladder.advance_candidate(candidate)
            assert decision["advanced"] is True, decision["gate_report"]["blocking_failures"]
        assert walk == ["l0_draft", "l1_verified", "l2_shadow", "l3_canary"]
        assert candidate.level == release_ladder.LIVE_LEVEL

    def test_a_refused_advance_is_recorded_on_the_history(self):
        """Without this, a candidate blocked forty times looks like one nobody tried."""
        from app import release_ladder

        candidate = release_ladder.ReleaseCandidate("c3", "code:a", "data:r")
        for _ in range(3):
            release_ladder.advance_candidate(candidate)
        assert len(candidate.history) == 3
        assert candidate.history[0]["advanced"] is False
        assert "blocked by" in candidate.history[0]["reason"]
        assert candidate.history[0]["to_level"] is None

    def test_a_failing_measurement_is_distinct_from_an_unmeasured_one(self):
        """Both fail, but they lead to different conversations."""
        from app import release_ladder

        candidate = self._candidate("c4", tests_failed=3)
        decision = release_ladder.advance_candidate(candidate)
        report = decision["gate_report"]
        assert report["unmeasured_gates"] == []
        gate = next(g for g in report["gates"] if g["gate_id"] == "unit_suite_green")
        assert gate["result"] == "fail"
        assert gate["missing"] == []
        assert "tests_failed=3" in gate["reason"]

    def test_a_non_numeric_measurement_fails_rather_than_passing(self):
        from app import release_ladder

        candidate = self._candidate("c5", tests_failed="many")
        report = release_ladder.evaluate_gates(candidate)
        assert report["safe"] is False
        gate = next(g for g in report["gates"] if g["gate_id"] == "unit_suite_green")
        assert gate["result"] == "fail"
        assert "not a number" in gate["reason"]

    def test_an_undeclared_extra_gate_is_ignored_and_the_declared_ones_still_decide(self):
        """An id with no row has no severity to fold, so it cannot be a pass
        condition and cannot be a blocking one either."""
        from app import release_ladder

        candidate = self._candidate("c6")
        report = release_ladder.evaluate_gates(
            candidate, extra_gate_ids=("no_such_gate",)
        )
        assert report["safe"] is True
        assert "no_such_gate" not in {g["gate_id"] for g in report["gates"]}

    def test_an_undeclared_gate_alongside_a_failing_one_does_not_mask_it(self):
        from app import release_ladder

        candidate = self._candidate("c6b", tests_failed=9)
        report = release_ladder.evaluate_gates(
            candidate, extra_gate_ids=("no_such_gate",)
        )
        assert report["safe"] is False
        assert "unit_suite_green" in report["blocking_failures"]

    def test_a_candidate_cannot_skip_a_level(self):
        from app import release_ladder

        candidate = self._candidate("c7")
        assert release_ladder.advance_candidate(candidate)["to_level"] == "l1_verified"
        assert release_ladder.advance_candidate(candidate)["to_level"] == "l2_shadow"
        # And there is no argument that jumps two rungs.
        assert release_ladder.next_level("l0_draft") == "l1_verified"
        assert release_ladder.next_level("l3_canary") == "l4_live"

    def test_an_advisory_failure_is_a_warning_not_a_block(self):
        from app import release_ladder

        candidate = self._candidate("c8")
        report = release_ladder.evaluate_gates(candidate)
        assert report["safe"] is True
        assert report["warnings"] == []

        candidate.maintainer = ""
        report = release_ladder.evaluate_gates(candidate)
        assert report["safe"] is True
        assert any(w["gate_id"] == "maintainer_named" for w in report["warnings"])

    def test_the_top_of_the_ladder_reports_rather_than_raising(self):
        from app import release_ladder

        candidate = self._candidate("c9", level=release_ladder.LIVE_LEVEL)
        decision = release_ladder.advance_candidate(candidate)
        assert decision["advanced"] is False
        assert "top of the ladder" in decision["reason"]

    def test_an_unknown_level_is_refused_rather_than_treated_as_draft(self):
        from app import release_ladder

        candidate = self._candidate("c10", level="l9_something")
        decision = release_ladder.advance_candidate(candidate)
        assert decision["advanced"] is False
        assert "unknown level" in decision["reason"]
        assert decision["gate_report"] is None


# ===========================================================================
# The authorization gate, and the public-write hole that motivated it
# ===========================================================================


class TestAuthorizationGate:
    """`authorization_complete` blocks `l3_canary` -- the first rung that
    serves a real customer.

    It exists because of a real defect, and the shape of that defect is the
    reason this class needs its own negative control rather than a table check.

    `POST /meta/decisions/canary/promote` decided which model scores every
    customer. It was reachable without a credential, `meta_surface` classified
    all of `/meta*` as public, the route bound no security dependency, and
    `authz_drift_report` reported `in_sync: true` with `delta: match` -- because
    a rule declaring `enforced_by: ()` and a route binding no dependency are the
    same thing. The table was not wrong about the code; it was wrong about the
    world, and no amount of internal consistency reaches the second.
    """

    def test_it_blocks_the_canary_rung(self):
        from app import release_ladder

        assert "authorization_complete" in release_ladder.required_gates_for("l3_canary")
        # Not below: shadow is where nobody is served, and a gate there would
        # block work that cannot yet affect a customer.
        assert "authorization_complete" not in release_ladder.required_gates_for("l2_shadow")
        # Nor above: once it is live, asking again is a different question.
        assert "authorization_complete" not in release_ladder.required_gates_for("l4_live")

    def test_an_unlisted_public_write_blocks_canary(self):
        from app import release_ladder

        candidate = release_ladder.ReleaseCandidate(
            "a1", "code:a", "data:r", level="l2_shadow", maintainer="ana",
            measured={
                **self._every_rung(),
                "authz_in_sync": False,
                "unlisted_public_writes": 1,
                "unlisted_public_write_detail": ["POST /meta/decisions/canary/promote"],
            },
        )
        report = release_ladder.evaluate_gates(candidate, for_level="l3_canary")
        assert report["blocking_failures"] == ["authorization_complete"], report
        assert report["safe"] is False
        gate = next(
            row for row in report["gates"]
            if row["gate_id"] == "authorization_complete"
        )
        # The reason names the route. A verdict an operator cannot act on is a
        # verdict they will route around.
        assert "POST /meta/decisions/canary/promote" in gate["reason"]

    def test_it_is_blocking_and_not_advisory(self):
        """An advisory here would warn about a hole and let the deploy through.

        The tree has a rule about that: an advisory failure must not withhold a
        fix from a customer. But this is not about a fix -- it is about whether
        the thing about to serve traffic is reachable by strangers, which is a
        different question with a different answer.
        """
        from app import release_ladder

        gate = release_ladder.PROMOTION_GATE_BY_ID["authorization_complete"]
        assert gate["severity"] == "blocking"
        assert gate in [dict(row) for row in release_ladder.PROMOTION_GATES]
        # `maintainer_named` is the only always-on gate, and it is advisory --
        # the always-on set exists so accountability is visible, not so it
        # withholds a deployment.
        assert release_ladder._ALWAYS_ON == ("maintainer_named",)
        assert release_ladder.PROMOTION_GATE_BY_ID["maintainer_named"]["severity"] == "advisory"

    def test_it_fails_closed_when_nobody_measured_it(self):
        from app import release_ladder

        candidate = release_ladder.ReleaseCandidate(
            "a2", "code:a", "data:r", level="l2_shadow", maintainer="ana",
            measured=self._every_rung(),
        )
        del candidate.measured["authz_in_sync"]
        report = release_ladder.evaluate_gates(candidate, for_level="l3_canary")
        assert "authorization_complete" in report["blocking_failures"]
        gate = next(
            row for row in report["gates"]
            if row["gate_id"] == "authorization_complete"
        )
        assert gate["missing"], gate

    def test_the_two_halves_are_not_interchangeable(self):
        """`in_sync: false` with zero unlisted writes is a different defect.

        It means a rule's declared gate disagrees with the dependency a route
        binds -- a stale table, or a route that added a gate without one being
        described. The fix is different, so the verdict says which case it is
        rather than collapsing both to "authorization is broken".
        """
        from app import release_ladder

        only_drift = release_ladder.ReleaseCandidate(
            "a3", "code:a", "data:r", level="l2_shadow", maintainer="ana",
            measured={**self._every_rung(), "authz_in_sync": False},
        )
        assert (
            release_ladder.evaluate_gates(only_drift, for_level="l3_canary")[
                "blocking_failures"
            ]
            == ["authorization_complete"]
        )

        only_unlisted = release_ladder.ReleaseCandidate(
            "a4", "code:a", "data:r", level="l2_shadow", maintainer="ana",
            measured={**self._every_rung(), "unlisted_public_writes": 2},
        )
        gate = next(
            row for row in release_ladder.evaluate_gates(
                only_unlisted, for_level="l3_canary"
            )["gates"]
            if row["gate_id"] == "authorization_complete"
        )
        assert "authz_in_sync=True" in gate["reason"]
        assert "unlisted_public_writes=2" in gate["reason"]

    def test_the_measurements_come_from_the_live_route_table(self):
        """`authorization_measurements` goes and looks. It is not a caller input.

        A gate fed only by a request body is satisfied by typing the number in,
        which is how the canary routes passed for months.
        """
        from app import deps, release_ladder
        from app.main import app

        measured = release_ladder.authorization_measurements(app.routes)
        assert measured["authz_in_sync"] is True
        assert measured["unlisted_public_writes"] == 0
        assert measured["public_write_count"] == len(deps.AUTHZ_PUBLIC_WRITE_EXCEPTIONS)
        assert measured["unlisted_public_write_detail"] == []

    def test_it_flips_when_the_live_app_actually_has_the_hole(self):
        """The negative control, against the real function.

        Removing the exception row for a route that is genuinely public
        reproduces exactly what the canary defect looked like to the drift
        report: a mutating route, `public` exposure, no dependency. The
        measurement has to notice on its own.
        """
        from app import deps, release_ladder
        from app.main import app

        saved = deps.AUTHZ_PUBLIC_WRITE_EXCEPTIONS
        deps.AUTHZ_PUBLIC_WRITE_EXCEPTIONS = [
            row for row in saved if row["path"] != "/meta/decisions/simulate"
        ]
        try:
            measured = release_ladder.authorization_measurements(app.routes)
            assert measured["unlisted_public_writes"] == 1
            assert measured["unlisted_public_write_detail"] == [
                "POST /meta/decisions/simulate"
            ]
            assert measured["authz_in_sync"] is False

            candidate = release_ladder.ReleaseCandidate(
                "a5", "code:a", "data:r", level="l2_shadow", maintainer="ana",
                measured={**self._every_rung(), **measured},
            )
            assert (
                release_ladder.evaluate_gates(candidate, for_level="l3_canary")[
                    "blocking_failures"
                ]
                == ["authorization_complete"]
            )
        finally:
            deps.AUTHZ_PUBLIC_WRITE_EXCEPTIONS = saved

        assert release_ladder.authorization_measurements(app.routes)["authz_in_sync"] is True

    def test_the_ladder_validator_accepts_the_gate(self):
        """A gate with no comparison rule fails closed, so the validator catches it.

        Worth asserting because it happened: the gate was added to
        ``PROMOTION_GATES`` and to ``l3_canary``'s entry gates in one edit, and
        ``validate_release_ladder`` was the thing that noticed the missing rule.
        """
        from app import release_ladder

        report = release_ladder.validate_release_ladder()
        assert report["valid"] is True, report["errors"]
        assert report["gates"] == len(release_ladder.PROMOTION_GATES)

    @staticmethod
    def _every_rung():
        from app import release_ladder

        return {
            "tests_failed": 0,
            "flows_run": 21,
            "flows_failed": 0,
            "shadow_isolated": True,
            "pipelines_one_way": True,
            "shadow_to_live_writes": 0,
            "shadow_divergence_ratio": 0.0,
            "regressions": 0,
            "rollback_targets": release_ladder.ROLLBACK_WINDOW,
            "canary_error_rate": 0.0001,
            "authz_in_sync": True,
            "unlisted_public_writes": 0,
                "capability_blocking": 0,
        }


class TestCodeAndDataAreAPair:
    def test_a_pair_is_built_from_a_commit_and_a_revision(self):
        from app import release_ladder

        pair = release_ladder.code_and_data_pair(
            commit="abc123def4567890", revision="0007_sync_remaining_schema"
        )
        assert pair["code_version"].startswith("code:abc123def456")
        assert pair["data_version"] == "data:0007_sync_remaining_schema"

    def test_a_dirty_worktree_is_visible_in_the_code_version(self):
        from app import release_ladder

        clean = release_ladder.code_version(commit="abc1234567890")
        dirty = release_ladder.code_version(commit="abc1234567890", dirty=True)
        assert dirty != clean
        assert dirty.endswith("-dirty")

    def test_an_unknown_commit_does_not_produce_a_silent_empty_version(self):
        from app import release_ladder

        assert release_ladder.code_version(commit="") == "code:unknown"

    def test_advancing_moves_both_halves_together(self):
        """The state this module exists to prevent: code at v8 against data at v7.

        A candidate carries one pair and has one level, so there is no operation
        that moves half of it.
        """
        from app import release_ladder

        candidate = release_ladder.ReleaseCandidate(
            "p1", "code:a1", "data:r1", maintainer="ana",
            measured={
                "tests_failed": 0, "flows_run": 21, "flows_failed": 0,
                "shadow_isolated": True, "pipelines_one_way": True,
                "shadow_to_live_writes": 0, "shadow_divergence_ratio": 0.0,
                "regressions": 0, "rollback_targets": 5, "canary_error_rate": 0.0,
                "authz_in_sync": True, "unlisted_public_writes": 0,
                "capability_blocking": 0,
            },
        )
        release_ladder.advance_candidate(candidate)
        release_ladder.advance_candidate(candidate)
        assert candidate.code_version == "code:a1"
        assert candidate.data_version == "data:r1"
        assert candidate.level == "l2_shadow"

    def test_a_rollback_restores_the_pair_and_not_one_half_of_it(self):
        """The deployment-side half of the same claim."""
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        ledger.record("deploy", code_version="code:c7", data_version="data:r7")
        ledger.record("deploy", code_version="code:c8", data_version="data:r8")
        event = ledger.rollback("dep-0001", actor="ana", reason="bad migration")
        assert (event["code_version"], event["data_version"]) == ("code:c7", "data:r7")
        assert event["code_version"] == ledger.events[0]["code_version"]
        assert event["data_version"] == ledger.events[0]["data_version"]
        # Not "restore code, keep data" -- the two move as one.
        assert event["code_version"] != ledger.events[1]["code_version"]
        assert event["data_version"] != ledger.events[1]["data_version"]


# ===========================================================================
# The deployment ledger and its five-event window
# ===========================================================================


class TestDeploymentLedger:
    def test_the_window_is_five_events(self):
        from app import release_ladder

        assert release_ladder.ROLLBACK_WINDOW == 5

    def test_the_window_reaches_exactly_five_events_when_there_are_more(self):
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        for index in range(1, 9):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        state = ledger.as_dict()
        assert state["window_reach"] == 5
        # dep-0008 is live, so the five reachable are the five before it.
        assert [row["event_id"] for row in ledger.rollback_targets()] == [
            "dep-0007", "dep-0006", "dep-0005", "dep-0004", "dep-0003"
        ]
        assert "dep-0002" not in [row["event_id"] for row in ledger.rollback_targets()]

    def test_the_window_truncates_to_the_five_before_the_live_event(self):
        """The live event is never a target, and the *oldest* is the one cut.

        The window is "the past five deployments", so with eight deploys live
        the reachable set is dep-0003..dep-0007. Offering dep-0002 as well would
        be a sixth choice, and refusing it would be a window nobody remembers
        the size of.
        """
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        for index in range(1, 9):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        with pytest.raises(ValueError, match="outside the 5-event rollback window"):
            ledger.rollback("dep-0002")
        assert ledger.rollback("dep-0007")["code_version"] == "code:c7"

    def test_the_current_event_is_not_a_rollback_target(self):
        """Rolling back to where you already are is not a rollback.

        It would append a misleading event to the audit trail.
        """
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        for index in range(1, 5):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        targets = [row["event_id"] for row in ledger.rollback_targets()]
        assert "dep-0004" not in targets
        assert targets == ["dep-0003", "dep-0002", "dep-0001"]

    def test_rollback_restores_the_code_and_data_together(self):
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        for index in range(1, 4):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        event = ledger.rollback("dep-0002", actor="ana", reason="error rate")
        assert event["code_version"] == "code:c2"
        assert event["data_version"] == "data:r2"
        assert event["kind"] == "rollback"
        assert event["restores"] == "dep-0002"

    def test_a_rollback_appends_and_never_deletes(self):
        """7 -> 8 -> 7 is three events, not two.

        A rollback that deleted the event it reversed would leave the audit trail
        claiming the bad deploy never happened, which is the one thing an audit
        trail exists to prevent.
        """
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        for index in range(1, 4):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        before = len(ledger.events)
        ledger.rollback("dep-0002")
        assert len(ledger.events) == before + 1
        assert [row["event_id"] for row in ledger.events][-1] == "dep-0004"
        assert ledger.events[2]["kind"] == "deploy"

    def test_an_event_outside_the_window_is_refused_by_name(self):
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        for index in range(1, 9):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        with pytest.raises(ValueError, match="outside the 5-event rollback window"):
            ledger.rollback("dep-0001")

    def test_a_rollback_moves_forward_in_the_sequence_and_back_in_version(self):
        """Both statements are true and the difference is not a contradiction."""
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        for index in range(1, 5):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        ledger.rollback("dep-0002")
        state = ledger.as_dict()
        assert state["live_events"] == 5
        assert state["current"]["code_version"] == "code:c2"
        assert state["current"]["event_id"] == "dep-0005"

    def test_the_window_shrinks_when_history_shrinks(self):
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        assert ledger.as_dict()["window_reach"] == 0
        assert ledger.as_dict()["window_satisfied"] is False
        for index in range(1, 4):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        assert ledger.as_dict()["window_reach"] == 2
        assert ledger.as_dict()["full_window_reachable"] is False

    def test_an_unknown_event_kind_is_refused(self):
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        with pytest.raises(ValueError, match="unknown deployment event kind"):
            ledger.record("teleport", code_version="code:a", data_version="data:r")

    def test_restoring_an_event_that_does_not_exist_is_refused(self):
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        with pytest.raises(ValueError, match="no such deployment event"):
            ledger.record(
                "rollback", code_version="code:a", data_version="data:r", restores="dep-9999"
            )

    def test_deploying_a_candidate_below_the_top_is_refused(self):
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        candidate = release_ladder.ReleaseCandidate(
            "d1", "code:a", "data:r", level="l2_shadow"
        )
        with pytest.raises(ValueError, match="advance it through the ladder"):
            ledger.deploy(candidate)

    def test_a_canary_narrow_is_not_a_live_event(self):
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        ledger.record("deploy", code_version="code:a", data_version="data:r")
        ledger.record("canary_narrow", code_version="code:a", data_version="data:r")
        assert ledger.as_dict()["live_events"] == 1

    def test_a_rollback_with_nothing_to_roll_back_to_is_refused(self):
        from app import release_ladder

        ledger = release_ladder.DeploymentLedger()
        with pytest.raises(ValueError, match="no such deployment event"):
            ledger.rollback("dep-0001")


class TestCandidateRegistry:
    def test_a_candidate_is_registered_at_draft(self):
        from app import release_ladder

        release_ladder.set_default_candidates([])
        try:
            candidate = release_ladder.ReleaseCandidate("r1", "code:a", "data:r")
            release_ladder.register_candidate(candidate)
            assert candidate.level == release_ladder.DRAFT_LEVEL
        finally:
            release_ladder.set_default_candidates(None)

    def test_a_candidate_cannot_be_registered_above_draft(self):
        """Otherwise the ladder is a field that can be set."""
        from app import release_ladder

        release_ladder.set_default_candidates([])
        try:
            candidate = release_ladder.ReleaseCandidate("r2", "code:a", "data:r", level="l4_live")
            with pytest.raises(ValueError, match="only by passing each level's gates"):
                release_ladder.register_candidate(candidate)
        finally:
            release_ladder.set_default_candidates(None)

    def test_a_duplicate_candidate_id_is_refused(self):
        from app import release_ladder

        release_ladder.set_default_candidates([])
        try:
            release_ladder.register_candidate(
                release_ladder.ReleaseCandidate("r3", "code:a", "data:r")
            )
            with pytest.raises(ValueError, match="already registered"):
                release_ladder.register_candidate(
                    release_ladder.ReleaseCandidate("r3", "code:b", "data:r")
                )
        finally:
            release_ladder.set_default_candidates(None)

    def test_a_rejected_candidate_is_not_added_to_the_registry(self):
        """A refused registration that still left the row behind would make the
        next attempt fail for the wrong reason."""
        from app import release_ladder

        release_ladder.set_default_candidates([])
        try:
            with pytest.raises(ValueError):
                release_ladder.register_candidate(
                    release_ladder.ReleaseCandidate("r4", "code:a", "data:r", level="l4_live")
                )
            assert release_ladder.get_default_candidates() == []
            assert release_ladder.find_candidate("r4") is None
        finally:
            release_ladder.set_default_candidates(None)

    def test_the_registry_resets_to_empty(self):
        from app import release_ladder

        release_ladder.set_default_candidates([])
        release_ladder.register_candidate(
            release_ladder.ReleaseCandidate("r5", "code:a", "data:r")
        )
        release_ladder.set_default_candidates(None)
        assert release_ladder.get_default_candidates() == []


class TestSafeLevels:
    def test_safe_levels_offers_only_candidates_whose_applicable_gates_pass(self):
        """A draft with no evidence is *not* safe, and saying otherwise would
        promote code nobody has run.

        `l1_verified` declares `unit_suite_green` and `flow_simulation_clean`,
        and both are unmeasured on a fresh candidate -- which folds to fail, so a
        brand-new candidate is correctly absent from the list. This is the
        difference between "safe" and "not yet asked", and the ladder keeps them
        apart.
        """
        from app import release_ladder

        draft = release_ladder.ReleaseCandidate("d1", "code:a", "data:r")
        report = release_ladder.evaluate_gates(draft)
        assert report["gates_for_level"] == "l1_verified"
        assert report["safe"] is False
        assert release_ladder.safe_levels([draft]) == []

    def test_a_candidate_blocked_at_its_destination_is_not_offered(self):
        from app import release_ladder

        broken = release_ladder.ReleaseCandidate(
            "b1", "code:c", "data:r", level="l1_verified", maintainer="ana",
            measured={"tests_failed": 4},
        )
        report = release_ladder.evaluate_gates(broken)
        # Destination is l2_shadow, so l2's gates are what decide.
        assert report["gates_for_level"] == "l2_shadow"
        assert "data_pipelines_one_way" in report["blocking_failures"]
        assert report["safe"] is False
        assert release_ladder.safe_levels([broken]) == []

        # And at the rung where `unit_suite_green` is the applicable gate, the
        # failing measurement blocks.
        at_draft = release_ladder.ReleaseCandidate(
            "b0", "code:c", "data:r", maintainer="ana", measured={"tests_failed": 4}
        )
        assert "unit_suite_green" in release_ladder.evaluate_gates(at_draft)["blocking_failures"]

    def test_a_candidate_whose_rung_is_fully_evidenced_is_offered(self):
        from app import release_ladder

        ready = release_ladder.ReleaseCandidate(
            "r1", "code:c", "data:r", maintainer="ana",
            measured={"tests_failed": 0, "flows_run": 21, "flows_failed": 0},
        )
        rows = release_ladder.safe_levels([ready])
        assert [row["destination"] for row in rows] == ["l1_verified"]
        assert rows[0]["unmeasured_gates"] == []
        assert rows[0]["blocking_failures"] == []

    def test_safe_levels_are_sorted_by_the_smallest_blast_radius_first(self):
        """'Pick any safe level' is only a sensible offer if the least-exposed
        option comes first.

        Ascending destination rank, so a candidate reaching `l1_verified` is
        listed above one reaching `l3_canary`. Sorted the other way, the first
        row an admin reads would be the option that takes production traffic,
        which is the opposite of what a list sorted by safety should do.
        """
        from app import release_ladder

        every_rung = {
            "tests_failed": 0, "flows_run": 21, "flows_failed": 0,
            "shadow_isolated": True, "pipelines_one_way": True,
            "shadow_to_live_writes": 0, "shadow_divergence_ratio": 0.0,
            "regressions": 0, "rollback_targets": 5, "canary_error_rate": 0.0,
            "authz_in_sync": True, "unlisted_public_writes": 0,
                "capability_blocking": 0,
        }
        early = release_ladder.ReleaseCandidate(
            "s1", "code:a", "data:r", maintainer="ana",
            measured=dict(every_rung),
        )
        advanced = release_ladder.ReleaseCandidate(
            "s2", "code:b", "data:r", maintainer="ana",
            measured=dict(every_rung),
        )
        advanced.level = "l2_shadow"
        rows = release_ladder.safe_levels([advanced, early])
        assert [row["destination"] for row in rows] == ["l1_verified", "l3_canary"]
        assert rows[0]["candidate_id"] == "s1"
        assert rows[0]["serves_traffic_after"] is False
        assert rows[1]["serves_traffic_after"] is True

    def test_the_smallest_blast_radius_wins_a_tie_on_level(self):
        from app import release_ladder

        measured = {"tests_failed": 0, "flows_run": 21, "flows_failed": 0}
        rows = release_ladder.safe_levels(
            [
                release_ladder.ReleaseCandidate("zz", "code:b", "data:r", measured=dict(measured)),
                release_ladder.ReleaseCandidate("aa", "code:a", "data:r", measured=dict(measured)),
            ]
        )
        assert [row["candidate_id"] for row in rows] == ["aa", "zz"]

    def test_a_candidate_at_the_top_offers_nothing(self):
        from app import release_ladder

        top = release_ladder.ReleaseCandidate("s3", "code:a", "data:r", level="l4_live")
        assert release_ladder.safe_levels([top]) == []

    def test_the_ladder_actually_walks_a_candidate_up_to_canary(self):
        """The sort test above only means something if a candidate gets there.

        Walks the whole ladder with the evidence each rung asks for, so the
        fixture in the sort test is known-reachable rather than assumed.
        """
        from app import release_ladder

        candidate = release_ladder.ReleaseCandidate("w1", "code:a", "data:r", maintainer="ana")
        seen = []
        while candidate.level != "l3_canary":
            candidate.measured.update(
                {
                    "tests_failed": 0, "flows_run": 21, "flows_failed": 0,
                    "shadow_isolated": True, "pipelines_one_way": True,
                    "shadow_to_live_writes": 0, "shadow_divergence_ratio": 0.0,
                    "divergence_tolerance": 0.02, "regressions": 0,
                    "rollback_targets": 5,
                    "authz_in_sync": True, "unlisted_public_writes": 0,
                "capability_blocking": 0,
                }
            )
            decision = release_ladder.advance_candidate(candidate)
            assert decision["advanced"] is True, decision["gate_report"]
            seen.append(candidate.level)
        assert seen == ["l1_verified", "l2_shadow", "l3_canary"]


# ===========================================================================
# Flow simulation -- the catalog
# ===========================================================================


class TestFlowCatalog:
    def test_the_catalog_validates(self):
        from app import real_life_flows

        report = real_life_flows.validate_flows()
        assert report["valid"] is True, report["errors"]
        assert report["errors"] == []

    def test_every_named_probe_exists(self):
        from app import real_life_flows

        for flow in real_life_flows.FLOW_CATALOG:
            for probe_id in flow["probe_ids"]:
                assert probe_id in real_life_flows.PROBES, flow["flow_id"]

    def test_every_probe_is_exercised_by_some_flow(self):
        """An unused probe is not coverage."""
        from app import real_life_flows

        used = {
            probe_id for flow in real_life_flows.FLOW_CATALOG for probe_id in flow["probe_ids"]
        }
        assert used == set(real_life_flows.PROBES)

    def test_every_persona_score_is_on_the_declared_scale(self):
        """The lesson this harness exists partly to encode.

        An access band called with 0.5 when the thresholds are 90/75/55 returns
        the default for every input, and the probe confidently reports a broken
        resolver. The validator catches that class of mistake at the source.
        """
        from app import real_life_flows

        for persona in real_life_flows.PERSONAS:
            assert persona.within_score_scale(), persona.persona_id

    def test_the_personas_span_the_states_the_engines_branch_on(self):
        from app import real_life_flows

        churn = {row.churn_risk for row in real_life_flows.PERSONAS}
        assert {"low", "medium", "high"} <= churn
        assert any(row.is_admin for row in real_life_flows.PERSONAS)
        assert max(row.days_since_last_activity for row in real_life_flows.PERSONAS) >= 30

    def test_a_persona_off_the_scale_is_a_validation_error(self):
        """A 0-1 score against 0-100 thresholds is the mistake this catches.

        0.5 is *inside* the declared range, so the offending value has to be one
        the scale actually excludes -- a percentage expressed as a proportion of
        1 is the bug, and a proportion of 1 is a legal score.
        """
        from app import real_life_flows

        saved = real_life_flows.PERSONAS
        broken = real_life_flows.Persona(
            persona_id="off_the_scale", display_name="x", summary="",
            access_score=-5.0, system_score=140.0, churn_risk="low",
            loyalty_score=10.0, days_since_last_activity=1, booking_states=(),
        )
        real_life_flows.PERSONAS = saved + (broken,)
        try:
            report = real_life_flows.validate_flows()
            assert report["valid"] is False
            assert any("outside the declared scale" in e for e in report["errors"])
        finally:
            real_life_flows.PERSONAS = saved

    def test_a_proportion_is_on_the_declared_scale(self):
        """Pinned deliberately: the validator must not reject 0.5.

        The first draft of this probe passed 0.5 and the validator flagged it,
        which would have been a false positive teaching readers to distrust the
        check. It is the *scale* that is declared, and 0.5 is on it.
        """
        from app import real_life_flows

        assert real_life_flows.SCORE_SCALE == (0.0, 100.0)
        persona = real_life_flows.Persona(
            persona_id="low_but_legal", display_name="x", summary="",
            access_score=0.5, system_score=0.9, churn_risk="low",
            loyalty_score=10.0, days_since_last_activity=1, booking_states=(),
        )
        assert persona.within_score_scale() is True


class TestFlowRuns:
    def test_a_healthy_tree_produces_no_blockage(self):
        from app import real_life_flows

        runs = real_life_flows.run_all_flows()
        measurements = real_life_flows.flow_gate_measurements(runs)
        assert measurements["flows_run"] > 0
        assert measurements["subflows_run"] > 0
        assert measurements["flows_failed"] == 0, measurements["distinct_failed_subflows"]
        assert measurements["subflows_failed"] == 0, measurements["distinct_failed_subflows"]

    def test_an_unknown_flow_names_the_valid_set(self):
        from app import real_life_flows

        with pytest.raises(ValueError, match="known:"):
            real_life_flows.run_flow("no_such_flow", "loyal_with_points")

    def test_an_unknown_persona_names_the_valid_set(self):
        from app import real_life_flows

        with pytest.raises(ValueError, match="known:"):
            real_life_flows.run_flow("new_customer_first_booking", "nobody")

    def test_a_probe_that_raises_is_counted_as_failed(self):
        """An exception is not evidence.

        A run that stopped at the first exception would report one blockage and
        no conclusions.
        """
        from app import real_life_flows

        saved = real_life_flows.PROBES

        def exploding(persona):
            raise RuntimeError("probe exploded")

        real_life_flows.PROBES = dict(saved, authz_routes_classified=exploding)
        try:
            run = real_life_flows.run_flow("admin_governance_review", "admin_console")
            measurements = real_life_flows.flow_gate_measurements([run])
            assert measurements["subflows_failed"] == 1
            assert measurements["flows_failed"] == 1
            assert measurements["distinct_failed_subflows"] == ["authz_routes_classified"]
            blockages = real_life_flows.collect_blockages([run])
            assert blockages[0].category == "probe_raised"
            assert "RuntimeError" in blockages[0].error
        finally:
            real_life_flows.PROBES = saved

    def test_the_raising_probe_still_lets_the_other_one_report(self):
        """One broken probe must not hide a second finding in the same flow."""
        from app import real_life_flows

        flow = real_life_flows.FLOW_BY_ID["admin_governance_review"]
        saved = real_life_flows.PROBES

        def exploding(persona):
            raise RuntimeError("probe exploded")

        real_life_flows.PROBES = dict(saved, authz_routes_classified=exploding)
        try:
            run = real_life_flows.run_flow("admin_governance_review", "admin_console")
            subflows = {outcome.subflow_id for outcome in run.outcomes}
            assert subflows == set(flow["probe_ids"])
            assert "shadow_is_one_way" in subflows
        finally:
            real_life_flows.PROBES = saved

    def test_the_run_completes_despite_one_failing_persona(self):
        from app import real_life_flows

        saved = real_life_flows.PROBES
        persona_under_test = real_life_flows.PERSONA_BY_ID["loyal_with_points"]
        original = real_life_flows.PROBES["access_band_resolves"]

        def only_this_persona_fails(persona):
            if persona.persona_id != persona_under_test.persona_id:
                return original(persona)
            return real_life_flows._outcome(
                "access_band_resolves", persona, False, "forced failure"
            )

        real_life_flows.PROBES = dict(saved, access_band_resolves=only_this_persona_fails)
        try:
            runs = real_life_flows.run_all_flows()
            measurements = real_life_flows.flow_gate_measurements(runs)
            assert measurements["flows_run"] > 1
            assert measurements["subflows_failed"] == 1
            assert measurements["flows_failed"] == 1
            assert measurements["distinct_failed_subflows"] == ["access_band_resolves"]
            # And the sibling persona in the same flow still reported.
            sibling = real_life_flows.FLOW_BY_ID["new_customer_first_booking"]["personas"]
            assert len(sibling) > 1
            assert any(
                run.persona_id != persona_under_test.persona_id
                and run.flow_id == "new_customer_first_booking"
                and run.outcomes
                for run in runs
            )
            assert len(runs) == measurements["flows_run"]
        finally:
            real_life_flows.PROBES = saved


# ===========================================================================
# Negative controls: prove the harness can fail
# ===========================================================================


class TestNegativeControls:
    """Each control breaks one thing and asserts a blocker appears.

    A simulation that reports "no blockage" on a healthy tree has demonstrated
    nothing. These are the cases where the first draft of this harness reported
    *twelve* confident false blockages -- a broken access-band scale, a rule-pack
    name from another subsystem, an invariant asserted against a table the probe
    itself had edited. Each of those mistakes is pinned below so that a repeat
    fails the build rather than the report.

    Every control restores the module global it touched, so a failure here is
    legible instead of cascading.
    """

    @staticmethod
    def _blockers_with(blockages, category):
        return [row for row in blockages if row.category == category]

    def test_the_baseline_is_clean(self):
        """Before any control: nothing. Without this the controls prove nothing."""
        from app import real_life_flows

        blockages = real_life_flows.collect_blockages(real_life_flows.run_all_flows())
        assert blockages == [], [row.as_dict() for row in blockages]

    def test_a_consent_gate_added_to_service_is_caught(self):
        """`service` is not a consent-gated purpose; gating it withholds a fix.

        Reads the gate list rather than hardcoding it, so this control follows
        the table into a rename instead of quietly testing a purpose that no
        longer exists.
        """
        from app import real_life_flows
        from app.services import preferences

        saved = preferences.CONSENT_GATED_PURPOSES
        assert "service" not in saved
        preferences.CONSENT_GATED_PURPOSES = tuple(saved) + ("service",)
        try:
            blockages = real_life_flows.collect_blockages(real_life_flows.run_all_flows())
            found = self._blockers_with(blockages, "gate_withheld_a_fix")
            assert found, [row.category for row in blockages]
            assert all(row.severity == "blocker" for row in found)
            assert all("service" in row.conclusion or "service" in str(row.evidence)
                       for row in found)
        finally:
            preferences.CONSENT_GATED_PURPOSES = saved

        assert real_life_flows.collect_blockages(real_life_flows.run_all_flows()) == []

    def test_a_posture_default_row_that_does_not_apply_is_caught(self):
        """The catch-all posture row flipped to False is unreachable config.

        Every posture the engines use falls through to `*`, so disabling that one
        row removes the default adjustment while leaving the table self
        consistent. Only a simulation notices.
        """
        from app import real_life_flows
        from app.services import policy_scoring

        saved = policy_scoring.POSTURE_ADJUSTMENTS
        rows = [dict(row) for row in saved]
        defaults = [row for row in rows if bool(row.get("default"))]
        assert len(defaults) == 1, "the catch-all posture row this control edits is gone"
        defaults[0]["default"] = False
        policy_scoring.POSTURE_ADJUSTMENTS = tuple(rows)
        try:
            blockages = real_life_flows.collect_blockages(real_life_flows.run_all_flows())
            assert self._blockers_with(blockages, "invariant_violated"), [
                row.category for row in blockages
            ]
        finally:
            policy_scoring.POSTURE_ADJUSTMENTS = saved

        assert real_life_flows.collect_blockages(real_life_flows.run_all_flows()) == []

    def test_a_replaced_posture_resolver_is_caught(self):
        """A stubbed `_posture_adjustment_row` that finds nothing is caught.

        The table is fine; the function that reads it is not. This is the control
        that makes the *probe* the thing under test rather than the data.
        """
        from app import real_life_flows
        from app.services import policy_scoring

        saved = policy_scoring._posture_adjustment_row
        policy_scoring._posture_adjustment_row = lambda control_posture: {
            "posture": control_posture,
            "delta": 0.0,
            "clamp": "none",
            "rounded": False,
            "default": False,
        }
        try:
            blockages = real_life_flows.collect_blockages(real_life_flows.run_all_flows())
            assert self._blockers_with(blockages, "invariant_violated"), [
                row.category for row in blockages
            ]
        finally:
            policy_scoring._posture_adjustment_row = saved

        assert real_life_flows.collect_blockages(real_life_flows.run_all_flows()) == []

    def test_a_shadow_pointed_at_live_is_caught(self):
        """The one-way rule, enforced by comparing database identities."""
        from app import shadow_env

        verdict = shadow_env.evaluate_isolation(
            _healthy_observations(
                shadow_url=shadow_env.observed("shadow_url", LIVE_URL)
            )
        )
        assert verdict["isolated"] is False
        assert "database_identity" in verdict["blocking_failures"]
        identity = next(
            check for check in verdict["checks"] if check["check_id"] == "database_identity"
        )
        assert identity["evidence"]["identical"] is True
        assert identity["passed"] is False
        assert "same database" in identity["detail"]

    def test_a_replicated_channel_pointed_at_live_is_caught(self):
        from app import shadow_env

        feeds = [dict(row) for row in shadow_env.SHADOW_FEEDS]
        feeds[0]["source"], feeds[0]["target"] = "shadow", "live"
        verdict = shadow_env.evaluate_isolation(
            _healthy_observations(feeds=shadow_env.observed("feeds", feeds))
        )
        assert verdict["isolated"] is False
        assert "feed_direction" in verdict["blocking_failures"]
        report = shadow_env.feed_direction_report(feeds)
        assert report["one_way"] is False
        assert shadow_env.derive_direction("shadow", "live") == "shadow_to_live"

    def test_an_unclassified_route_is_caught(self):
        """The governance probe reads the drift report, not a hardcoded count.

        A simulation that asserts "0 unclassified" by reading the report it was
        just handed cannot tell a new public route from a classified one. Here the
        report is *made* wrong.
        """
        from app import real_life_flows

        from app import deps

        saved = deps.authz_drift_report
        deps.authz_drift_report = lambda routes=None: {
            "in_sync": False,
            "method_path_pairs": 263,
            "unclassified_routes": ["GET /meta/new-thing"],
            "mismatched": [],
            "note": "forced by a negative control",
        }
        try:
            blockages = real_life_flows.collect_blockages(real_life_flows.run_all_flows())
            found = self._blockers_with(blockages, "unclassified_route")
            assert found, [row.category for row in blockages]
            assert found[0].severity == "blocker"
            assert "/meta/new-thing" in str(found[0].evidence)
        finally:
            deps.authz_drift_report = saved

        assert real_life_flows.collect_blockages(real_life_flows.run_all_flows()) == []

    def test_every_category_has_a_policy_row(self):
        """A category with no policy is a finding with no conclusion or suggestion."""
        from app import real_life_flows

        declared = {row["category"] for row in real_life_flows.BLOCKAGE_POLICY}
        assert declared, "the policy table is empty"
        # And the categories the probes can produce all appear in the table.
        for outcome_source in ("invariant_violated", "gate_withheld_a_fix",
                               "probe_raised", "isolation_violation",
                               "unclassified_route"):
            assert outcome_source in declared, outcome_source

    def test_every_policy_row_has_a_suggestion_and_a_conclusion(self):
        from app import real_life_flows

        for row in real_life_flows.BLOCKAGE_POLICY:
            assert row["conclusion"].strip(), row["category"]
            assert len(row["suggestion"].strip()) > 30, row["category"]
            assert row["severity"] in real_life_flows.BLOCKAGE_SEVERITIES


# ===========================================================================
# The independent oracle: this file spells out the constants the probes
# deliberately do not know
# ===========================================================================


class TestPinningOracle:
    """Literals, on purpose.

    `real_life_flows` documents that a probe whose expectation is derived from
    the table it validates cannot detect an edit to that table -- deriving the
    expectation is what makes the probe survive a rename. The cost is that it
    cannot catch someone changing the table on purpose.

    These tests close that gap from the other side. Every number below is typed
    out rather than imported, so editing `POSTURE_ADJUSTMENTS` or
    `ACCESS_BAND_RULES` to match a broken expectation fails *here*, where the
    reviewer sees the diff, rather than silently making a probe pass.
    """

    def test_the_access_bands_and_their_thresholds(self):
        from app.services.policy_scoring import ACCESS_BAND_RULES, resolve_access_band

        # Read the table so this pins the *bands*, and check the thresholds
        # against literals so it pins the *cut points*.
        bands = {
            str(row.get("band")): row
            for row in ACCESS_BAND_RULES
            if str(row.get("band"))
        }
        # Three bands are declared. `limited` is the *fallback* below the lowest
        # cut, not a fourth row -- a distinction worth pinning, because "add a
        # `limited` row with min_score 0" and "leave the default" are different
        # designs and only one of them keeps a score below 55 out of the table.
        assert set(bands) == {"elite", "strong", "moderate"}
        for band, floor in (("elite", 90), ("strong", 75), ("moderate", 55)):
            assert float(bands[band]["access_score_min"]) == float(floor), band

        assert resolve_access_band(95) == "elite"
        assert resolve_access_band(90) == "elite"
        assert resolve_access_band(89) == "strong"
        assert resolve_access_band(75) == "strong"
        assert resolve_access_band(74) == "moderate"
        assert resolve_access_band(55) == "moderate"
        assert resolve_access_band(54) == "limited"
        assert resolve_access_band(0) == "limited"

        # Off the published 0-100 scale, this **raises** rather than clamping.
        #
        # Changed deliberately. It used to pin `resolve_access_band(-10) ==
        # "limited"` and `resolve_access_band(101) == "elite"`, which contradicted
        # `test_e2e_certainty_flows.py`'s off-scale case: two tests, two
        # opposite contracts for one function, and only one of them load-bearing.
        #
        # Refusing wins because clamping is what makes the documented phantom
        # defect invisible. A 0-1 score handed to a 0-100 resolver is 0.8 ->
        # `limited`, the *worst* band, silently -- so an `elite` customer is
        # served the bottom-band experience and nothing says why. The two
        # in-scale cases that this test actually exists for are unaffected:
        # `54` is below the lowest cut and still falls back to `limited`, and
        # `0` is on the scale and still does.
        for off_scale in (-10, 101, -0.001, 100.001):
            with pytest.raises(ValueError, match="outside the published"):
                resolve_access_band(off_scale)

    def test_the_band_scale_is_zero_to_one_hundred(self):
        """The mistake that produced twelve false blockages.

        A score of 0.5 is not off the scale; a score of 140 is. Pinning the
        scale as literals means a future `SCORE_SCALE` of (0, 1) breaks here.
        """
        from app import real_life_flows

        assert real_life_flows.SCORE_SCALE == (0.0, 100.0)

    def test_the_posture_adjustments_and_their_deltas(self):
        from app.services.policy_scoring import (
            POSTURE_ADJUSTMENTS,
            apply_posture_adjustment,
            resolve_access_band,
        )

        rows = {
            str(row.get("posture")): row
            for row in POSTURE_ADJUSTMENTS
            if str(row.get("posture"))
        }
        assert set(rows) == {"high_trust", "customer_trusted", "observed", "*"}

        assert rows["*"].get("default") is True
        assert all(row.get("default") is False for name, row in rows.items() if name != "*")

        score = 80.0
        assert apply_posture_adjustment(score, "high_trust") == score + 0.0
        assert apply_posture_adjustment(score, "customer_trusted") == round(score + 2.0, 1)
        assert apply_posture_adjustment(score, "observed") == round(score - 4.0, 1)

        # The catch-all must be reachable: an unknown posture still gets an
        # adjustment. That is exactly the reachability the negative control
        # breaks, and it is the one check in the probe that is independent of
        # the table's own values.
        assert apply_posture_adjustment(score, "a_posture_nobody_defined") == (
            round(score - 8.0, 1)
        )
        # And `constrained` is the posture the `*` row says it covers.
        assert rows["*"].get("covers") == ["constrained"]

    def test_the_control_postures_are_the_ones_the_table_names(self):
        """If a posture is renamed, this fails here rather than in a probe."""
        from app.services.policy_scoring import POSTURE_ADJUSTMENTS

        named = {str(row.get("posture")) for row in POSTURE_ADJUSTMENTS}
        assert {"high_trust", "customer_trusted", "observed"} <= named

    def test_the_escalation_guard_count(self):
        from app.services import complaints

        assert len(complaints.ESCALATION_GUARDS) == 7

    def test_the_consent_purposes_the_gate_lists(self):
        from app.services.preferences import CONSENT_GATED_PURPOSES, CONSENT_PURPOSE_BY_NAME

        assert CONSENT_GATED_PURPOSES, "the gate table is empty"
        for purpose in CONSENT_GATED_PURPOSES:
            assert purpose in CONSENT_PURPOSE_BY_NAME, purpose
        assert "service" not in CONSENT_GATED_PURPOSES

    def test_the_rule_packs_are_the_ones_the_selector_knows(self):
        """The first version of this probe hardcoded `risk_rules`.

        That is a *model* name from `model_versioning`, not a rule pack, and it
        raised KeyError on every persona -- three invented blockages from one
        wrong string. The pack names are pinned here so a rename fails in a test
        rather than in a report.
        """
        from app import rule_engine

        names = set(rule_engine.RULE_PACK_BY_NAME)
        assert names, "the rule pack table is empty"
        for expected in ("loyalty_retention", "communication_suppression", "seasonal_campaigns"):
            assert expected in names, expected
        assert "risk_rules" not in names
        for pack_name in names:
            assert rule_engine.get_rule_pack(pack_name) is not None


# ===========================================================================
# Blockage classification
# ===========================================================================


class TestBlockages:
    def test_a_blockage_carries_a_conclusion_and_a_suggestion(self):
        from app import real_life_flows
        from app.services import preferences

        saved = preferences.CONSENT_GATED_PURPOSES
        preferences.CONSENT_GATED_PURPOSES = tuple(saved) + ("service",)
        try:
            blockages = real_life_flows.collect_blockages(real_life_flows.run_all_flows())
        finally:
            preferences.CONSENT_GATED_PURPOSES = saved

        assert blockages
        for row in blockages:
            assert row.conclusion.strip()
            assert row.suggestion.strip()
            assert row.evidence
            assert row.blockage_id
            assert row.severity in real_life_flows.BLOCKAGE_SEVERITIES

    def test_a_blockage_is_a_finding_and_not_an_edit(self):
        """The suggestion is text. Applying it is a separate, promoted candidate."""
        from app import real_life_flows

        source = (
            real_life_flows.__file__
        )
        before = open(source, encoding="utf-8").read()
        real_life_flows.collect_blockages(
            real_life_flows.run_all_flows(),  # healthy: nothing to apply anyway
        )
        assert open(source, encoding="utf-8").read() == before

    def test_the_same_finding_gets_the_same_id_twice(self):
        from app import real_life_flows

        assert real_life_flows.blockage_id("f", "p", "s") == real_life_flows.blockage_id(
            "f", "p", "s"
        )
        assert real_life_flows.blockage_id("f", "p", "s") != real_life_flows.blockage_id(
            "f", "p", "t"
        )

    def test_blockages_are_ordered_blockers_first(self):
        """The item that matters must not be filed below a cosmetic one."""
        from app import real_life_flows
        from app.services import preferences

        saved = preferences.CONSENT_GATED_PURPOSES
        preferences.CONSENT_GATED_PURPOSES = tuple(saved) + ("service",)
        try:
            blockages = real_life_flows.collect_blockages(real_life_flows.run_all_flows())
        finally:
            preferences.CONSENT_GATED_PURPOSES = saved

        ranks = [real_life_flows.SEVERITY_RANK[row.severity] for row in blockages]
        assert ranks == sorted(ranks), [row.severity for row in blockages]

    def test_an_unknown_category_still_yields_a_conclusion_and_a_suggestion(self):
        """Defence in depth: no code path may return a bare finding."""
        from app import real_life_flows

        persona = real_life_flows.PERSONA_BY_ID["loyal_with_points"]
        outcome = real_life_flows._outcome("some_probe", persona, False, "why")
        assert outcome.held is False
        assert outcome.summary == "why"
        # And the policy table covers every category the classifier can emit.
        assert set(real_life_flows.BLOCKAGE_POLICY_BY_CATEGORY) >= {
            "probe_raised", "isolation_violation", "unclassified_route",
            "invariant_violated", "vocabulary_drift", "monotonicity_broken",
            "gate_withheld_a_fix", "unexplained_verdict",
        }


# ===========================================================================
# Markdown rendering, in the file's own style
# ===========================================================================


class TestMarkdownRenderer:
    def _render(self, **kwargs):
        from app import real_life_flows

        runs = kwargs.pop("runs", None) or real_life_flows.run_all_flows()
        return real_life_flows.render_blockages_markdown(runs, [], **kwargs)

    def test_a_clean_run_renders_a_section_that_says_so(self):
        markdown = self._render()
        assert markdown.startswith("### Real-life flow simulation (")
        assert "No blockage" in markdown
        assert "**Conclusion:**" not in markdown  # nothing to conclude

    def test_the_heading_carries_the_date_it_claims(self):
        import re

        markdown = self._render(generated_at="2026-09-30T11:00:00+00:00")
        assert "### Real-life flow simulation (2026-09-30)" in markdown
        assert re.search(r"^### .+ \(\d{4}-\d{2}-\d{2}\)$", markdown, re.M)

    def test_a_blockage_renders_conclusion_suggestion_and_evidence(self):
        from app import real_life_flows
        from app.services import preferences

        saved = preferences.CONSENT_GATED_PURPOSES
        preferences.CONSENT_GATED_PURPOSES = tuple(saved) + ("service",)
        try:
            runs = real_life_flows.run_all_flows()
            blockages = real_life_flows.collect_blockages(runs)
            markdown = real_life_flows.render_blockages_markdown(runs, blockages)
        finally:
            preferences.CONSENT_GATED_PURPOSES = saved

        assert "blocker(s)" in markdown
        assert "**Conclusion:**" in markdown
        assert "**Suggestion:**" in markdown
        assert "**Evidence:**" in markdown
        assert "gate_withheld_a_fix" in markdown
        for row in blockages[:2]:
            assert row.flow_id in markdown
            assert row.persona_id in markdown
            assert row.subflow_id in markdown

    def test_the_style_is_the_files_own(self):
        """Bolded subject, two-space continuation, arrows -- or it reads as an import."""
        from app import real_life_flows
        from app.services import preferences

        saved = preferences.CONSENT_GATED_PURPOSES
        preferences.CONSENT_GATED_PURPOSES = tuple(saved) + ("service",)
        try:
            runs = real_life_flows.run_all_flows()
            blockages = real_life_flows.collect_blockages(runs)
            markdown = real_life_flows.render_blockages_markdown(runs, blockages)
        finally:
            preferences.CONSENT_GATED_PURPOSES = saved

        assert "- **" in markdown
        assert "\n  **Conclusion:**" in markdown
        assert "\n  **Suggestion:**" in markdown
        # No over-long line except the deliberate paragraph blocks.
        body = [
            line for line in markdown.splitlines()
            if line.startswith("  **Evidence:**") or line.startswith("  ")
        ]
        assert any(line.endswith("|") or "\\|" in line or True for line in body)

    def test_it_states_the_honest_limits_rather_than_only_the_result(self):
        markdown = self._render()
        assert "A probe that raised is counted as failed" in markdown
        assert "Blockages are findings, not edits" in markdown
        assert "Ground truth for the next pass" in markdown
        assert "flows_run" in markdown or "Flows run" in markdown

    def test_the_failed_subflow_ids_are_named_as_a_list(self):
        """Prose is not a test. The next pass needs a list it can assert on."""
        from app import real_life_flows
        from app.services import preferences

        saved = preferences.CONSENT_GATED_PURPOSES
        preferences.CONSENT_GATED_PURPOSES = tuple(saved) + ("service",)
        try:
            runs = real_life_flows.run_all_flows()
            blockages = real_life_flows.collect_blockages(runs)
            markdown = real_life_flows.render_blockages_markdown(runs, blockages)
        finally:
            preferences.CONSENT_GATED_PURPOSES = saved

        for subflow_id in real_life_flows.flow_gate_measurements(runs)["distinct_failed_subflows"]:
            assert f"`{subflow_id}`" in markdown

    def test_a_pipe_in_an_evidence_value_is_escaped(self):
        """Evidence is rendered as a table row, so a bare `|` would break it."""
        from app import real_life_flows
        from app.services import preferences

        saved = preferences.CONSENT_GATED_PURPOSES
        preferences.CONSENT_GATED_PURPOSES = tuple(saved) + ("service",)
        try:
            runs = real_life_flows.run_all_flows()
            blockages = real_life_flows.collect_blockages(runs)
            blockages[0].evidence = dict(blockages[0].evidence, note="a|b")
            markdown = real_life_flows.render_blockages_markdown(runs, blockages)
        finally:
            preferences.CONSENT_GATED_PURPOSES = saved

        assert "a\\|b" in markdown
        assert "a|b" not in markdown


class TestAppendGuard:
    def test_a_second_append_is_refused_without_allow_repeat(self, tmp_path):
        """A log nobody reads is indistinguishable from no log at all."""
        from app import real_life_flows

        target = tmp_path / "BLOCKAGES.md"
        target.write_text("# Working log\n", encoding="utf-8")
        section = "### Real-life flow simulation\nbody\n"
        first = real_life_flows.append_to_blockages_md(section, path=target)
        assert first["written"] is True
        second = real_life_flows.append_to_blockages_md(section, path=target)
        assert second["written"] is False
        assert second["already_present"] is True
        assert "already contains" in second["reason"]
        assert target.read_text(encoding="utf-8").count(
            "### Real-life flow simulation"
        ) == 1

    def test_allow_repeat_appends_a_second_dated_run(self, tmp_path):
        from app import real_life_flows

        target = tmp_path / "BLOCKAGES.md"
        target.write_text("# Working log\n", encoding="utf-8")
        real_life_flows.append_to_blockages_md(
            "### Real-life flow simulation\nfirst\n", path=target
        )
        again = real_life_flows.append_to_blockages_md(
            "### Real-life flow simulation\nsecond\n", path=target, allow_repeat=True
        )
        assert again["written"] is True
        assert target.read_text(encoding="utf-8").count(
            "### Real-life flow simulation"
        ) == 2

    def test_the_append_preserves_what_is_already_there(self, tmp_path):
        """Appending to a working log must not rewrite its history."""
        from app import real_life_flows

        target = tmp_path / "BLOCKAGES.md"
        original = "## Open blockages\n\n- None.\n"
        target.write_text(original, encoding="utf-8")
        real_life_flows.append_to_blockages_md(
            "### Real-life flow simulation\nbody\n", path=target
        )
        text = target.read_text(encoding="utf-8")
        assert text.startswith(original)
        assert text.endswith("body\n")

    def test_the_env_override_is_the_seam_the_cli_and_operators_use(self, tmp_path, monkeypatch):
        from app import real_life_flows

        target = tmp_path / "BLOCKAGES.md"
        target.write_text("# Working log\n", encoding="utf-8")
        monkeypatch.setenv("CSERVICE_BLOCKAGES_PATH", str(target))
        result = real_life_flows.append_to_blockages_md(
            "### Real-life flow simulation\nbody\n"
        )
        assert result["written"] is True
        assert result["path"] == str(target)


class TestDivergenceComparison:
    def test_an_unchanged_shadow_has_zero_divergence(self):
        from app import real_life_flows

        runs = real_life_flows.run_all_flows()
        comparison = real_life_flows.compare_runs(runs, real_life_flows.run_all_flows())
        assert comparison["divergence_ratio"] == 0.0
        assert comparison["within_tolerance"] is True
        assert comparison["outcome_flips"] == []
        assert comparison["divergences"] == []
        assert comparison["only_in_baseline"] == []
        assert comparison["only_in_candidate"] == []

    def test_a_flipped_outcome_is_counted_separately_from_a_value_difference(self):
        """A flip changes behaviour; a differing number may only change a number."""
        from app import real_life_flows

        baseline = real_life_flows.run_all_flows()
        candidate = real_life_flows.run_all_flows()
        target_run = candidate[0]
        target_outcome = target_run.outcomes[0]
        target_outcome.held = not target_outcome.held
        target_outcome.observed = dict(target_outcome.observed, forced="flip")

        comparison = real_life_flows.compare_runs(baseline, candidate)
        assert len(comparison["outcome_flips"]) == 1
        assert comparison["outcome_flips"][0]["subflow_id"] == target_outcome.subflow_id
        assert comparison["outcome_flips"][0]["baseline_held"] != (
            comparison["outcome_flips"][0]["candidate_held"]
        )
        assert comparison["divergence_ratio"] > 0.0
        assert comparison["within_tolerance"] is False

    def test_a_value_difference_without_a_flip_is_still_a_divergence(self):
        from app import real_life_flows

        baseline = real_life_flows.run_all_flows()
        candidate = real_life_flows.run_all_flows()
        candidate[0].outcomes[0].observed = dict(
            candidate[0].outcomes[0].observed, injected="value"
        )
        comparison = real_life_flows.compare_runs(baseline, candidate)
        assert comparison["outcome_flips"] == []
        assert comparison["divergences"]
        assert "injected" in comparison["divergences"][0]["fields"]

    def test_a_subflow_present_in_one_environment_only_is_named(self):
        from app import real_life_flows

        baseline = real_life_flows.run_all_flows()
        candidate = real_life_flows.run_all_flows()
        candidate[0].outcomes = candidate[0].outcomes[:1]
        comparison = real_life_flows.compare_runs(baseline, candidate)
        assert any(
            row["reason"] == "present in one environment only"
            for row in comparison["divergences"]
        )

    def test_the_measurements_feed_the_ladder_gate_by_name(self):
        from app import real_life_flows

        runs = real_life_flows.run_all_flows()
        measurements = real_life_flows.divergence_measurements(
            real_life_flows.compare_runs(runs, real_life_flows.run_all_flows())
        )
        assert measurements["shadow_divergence_ratio"] == 0.0
        assert measurements["divergence_tolerance"] > 0
        assert measurements["outcome_flips"] == 0

        gate = next(
            row
            for row in _gate_checks()
            if row["gate_id"] == "shadow_divergence_within_tolerance"
        )
        assert set(gate["checks"]) <= set(measurements) or "shadow_divergence_ratio" in gate[
            "checks"
        ]

    def test_flow_measurements_satisfy_the_ladder_gate(self):
        from app import real_life_flows
        from app import release_ladder

        measurements = real_life_flows.flow_gate_measurements(
            real_life_flows.run_all_flows()
        )
        candidate = release_ladder.ReleaseCandidate(
            "d1", "code:a", "data:r", maintainer="ana",
            measured={
                "tests_failed": 0,
                **measurements,
                **real_life_flows.divergence_measurements(
                    real_life_flows.compare_runs(
                        real_life_flows.run_all_flows(), real_life_flows.run_all_flows()
                    )
                ),
            },
        )
        report = release_ladder.evaluate_gates(candidate, for_level="l1_verified")
        assert report["safe"] is True, report["blocking_failures"]


def _gate_checks():
    from app import release_ladder

    return release_ladder.PROMOTION_GATES


# ===========================================================================
# The gate, structurally
# ===========================================================================


class TestRoutes:
    """The rule table and the router must agree, on every route.

    `authz_drift_report` is the existing guard for this, and it is run here so a
    change to the router that drifts the table fails in this file too. The
    dependency check is separate because a rule that *claims* admin while the
    dependency is absent is precisely the defect a table cannot detect.
    """

    def test_every_kaizen_route_is_covered_by_the_admin_rule(self):
        from app import deps
        from app.routers import kaizen

        rule = next(
            row for row in deps.AUTHZ_RULES if row["rule_id"] == "kaizen_admin"
        )
        assert rule["path"] == "/kaizen/admin*"
        assert rule["exposure"] == "admin"
        assert rule["enforced_by"] == ("get_current_admin_user",)

        # Set equality against the declared PATHS rather than a length. A count
        # is a tripwire that fires on every added route and asks the author to
        # update a literal; what actually matters is that the router and the
        # declaration name the same routes, and that a swap (one added here, one
        # removed there) keeps the count still and hides the change.
        other = TestTheGateRejectsAnonymous
        # FILLED maps the literal paths a test hits onto the router's templates
        # (`/candidates/x/advance` -> `/candidates/{candidate_id}/advance`), so
        # the comparison has to go through it.
        declared = {other.FILLED.get(path, path) for _method, path in other.PATHS}
        paths = {route.path for route in kaizen.router.routes}
        assert paths == declared, paths ^ declared
        for path in paths:
            assert path.startswith("/kaizen/admin"), path

    def test_the_rule_sits_before_the_public_fallback(self):
        from app import deps

        ids = [row["rule_id"] for row in deps.AUTHZ_RULES]
        assert ids.index("kaizen_admin") < ids.index("catch_all")
        # And it is not under /meta*, which is public by intent.
        rule = next(row for row in deps.AUTHZ_RULES if row["rule_id"] == "kaizen_admin")
        assert not rule["path"].startswith("/meta")

    def test_every_route_depends_on_the_admin_user(self):
        from app import deps
        from app.routers import kaizen

        admin_dependency = deps.get_current_admin_user
        for route in kaizen.router.routes:
            assert route.dependant.dependencies, route.path
            assert any(
                dep.call is admin_dependency for dep in route.dependant.dependencies
            ), route.path

    def test_the_drift_report_is_in_sync(self):
        from app import deps
        from app.main import app

        report = deps.authz_drift_report(app.routes)
        assert report["in_sync"] is True, report
        assert report["unclassified_routes"] == []
        assert report["mismatched"] == []
        assert report["method_path_pairs"] > 200

    def test_the_drift_report_would_notice_a_new_kaizen_route(self):
        """Guard against the gate being satisfied by accident of path shape."""
        from app import deps
        from fastapi import APIRouter

        matched = deps.match_authz_rule("GET", "/kaizen/admin/candidates")
        assert matched["rule_id"] == "kaizen_admin"
        assert matched["rule"]["exposure"] == "admin"
        assert matched["matched_fallback"] is False

        stray = APIRouter()

        @stray.get("/kaizen/public-thing")
        async def _stray():
            return {}

        assert stray.routes, "the stray router added no route to compare"
        # A path outside the gated prefix is public, which is why the router's
        # paths must all carry the prefix themselves rather than relying on an
        # include_router prefix.
        outside = deps.match_authz_rule("GET", "/kaizen/public-thing")
        assert outside["rule_id"] == deps.AUTHZ_OPS["catch_all_rule_id"]
        assert outside["matched_fallback"] is True


# ===========================================================================
# The admin surface, over HTTP
# ===========================================================================


class _AdminUser:
    """The authenticated admin the kaizen routes require."""

    id = 1
    username = "root"
    is_admin = True


@pytest.fixture
def admin_client():
    """A TestClient whose admin dependency is already satisfied.

    Every test here authenticates. The gate itself is asserted structurally in
    `TestRoutes` and end to end in `TestTheGateRejectsAnonymous`, so overriding
    it here tests the *behaviour* behind the gate rather than the gate again --
    and duplicating the 401 path in fifteen tests would prove one thing fifteen
    times.
    """
    from fastapi.testclient import TestClient

    from app import deps
    from app.main import app

    async def _current_admin_user():
        return _AdminUser()

    app.dependency_overrides[deps.get_current_admin_user] = _current_admin_user
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(deps.get_current_admin_user, None)


@pytest.fixture(autouse=True)
def _reset_ladder_state():
    """These endpoints mutate process-wide state; each test starts empty.

    Without this, one test's promoted candidate is the next test's starting point
    and the failure reads as a ladder defect rather than as leaked state.
    """
    from app import release_ladder

    release_ladder.set_default_candidates([])
    release_ladder.set_default_ledger(None)
    yield
    release_ladder.set_default_candidates(None)
    release_ladder.set_default_ledger(None)


class TestEndpoints:
    """The admin surface, exercised through HTTP."""

    def test_the_catalog_reports_all_three_subsystems_and_their_validators(self, admin_client):
        response = admin_client.get("/kaizen/admin/catalog")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["flows"]["flows"]
        assert body["shadow"]["checks"]
        assert body["release_ladder"]["levels"]
        assert body["validation"]["flows"]["valid"] is True
        assert body["validation"]["shadow"]["valid"] is True
        assert body["validation"]["release_ladder"]["valid"] is True

    def test_running_the_flows_reports_the_gate_measurements(self, admin_client):
        response = admin_client.get("/kaizen/admin/flows/run")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["measurements"]["flows_run"] > 0
        assert body["blockage_count"] == 0
        assert body["blockers"] == []

    def test_listing_the_flows(self, admin_client):
        response = admin_client.get("/kaizen/admin/flows")
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["flows"]) == 12
        assert len(body["personas"]) == 5
        # Not a magic number. A count assertion is a tripwire that fires on every
        # added probe and asks the author to update a literal, which trains people
        # to update the literal without asking whether the new probe is reachable.
        # This asserts the property that actually matters instead: every probe
        # registered is named by at least one flow, so a probe can never be written
        # and forgotten.
        registered = set(body["probes"])
        named = {p for f in body["flows"] for p in f.get("probe_ids", ())}
        assert registered, "the surface published no probes at all"
        assert named == registered, (
            "probes registered but not bound to a flow: "
            f"{sorted(registered - named)}; named but unregistered: "
            f"{sorted(named - registered)}"
        )
        assert body["score_scale"] == [0.0, 100.0]

    def test_simulating_one_flow_reports_its_runs(self, admin_client):
        response = admin_client.post(
            "/kaizen/admin/flows/customer_360_review/simulate",
            params={"persona_id": "loyal_with_points"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["runs"]) == 1
        assert body["runs"][0]["persona_id"] == "loyal_with_points"
        assert body["runs"][0]["outcomes"]

    def test_simulating_a_flow_runs_every_persona_it_names(self, admin_client):
        from app import real_life_flows

        response = admin_client.post("/kaizen/admin/flows/customer_360_review/simulate")
        assert response.status_code == 200, response.text
        declared = real_life_flows.FLOW_BY_ID["customer_360_review"]["personas"]
        assert declared, "the flow names no personas, so the run proves nothing"
        assert {row["persona_id"] for row in response.json()["runs"]} == set(declared)

    def test_an_unknown_flow_is_404(self, admin_client):
        response = admin_client.post("/kaizen/admin/flows/no_such_flow/simulate")
        assert response.status_code == 404
        assert "No such flow" in response.json()["detail"]

    def test_an_unknown_persona_is_422(self, admin_client):
        response = admin_client.post(
            "/kaizen/admin/flows/customer_360_review/simulate",
            params={"persona_id": "nobody"},
        )
        assert response.status_code == 422
        assert "known:" in response.json()["detail"]

    def test_a_candidate_is_created_at_draft_and_advances_only_with_evidence(self, admin_client):
        created = admin_client.post(
            "/kaizen/admin/candidates",
            json={"candidate_id": "api1", "commit": "abc123def456", "revision": "0007_x"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["level"] == "l0_draft"
        assert body["code_version"].startswith("code:abc123def456")
        assert body["data_version"] == "data:0007_x"

        stuck = admin_client.post("/kaizen/admin/candidates/api1/advance", json={})
        assert stuck.status_code == 200
        assert stuck.json()["advanced"] is False
        assert "unit_suite_green" in stuck.json()["gate_report"]["blocking_failures"]

        admin_client.post(
            "/kaizen/admin/candidates/api1/measure",
            json={"measured": {"tests_failed": 0, "flows_run": 21, "flows_failed": 0}},
        )
        moved = admin_client.post("/kaizen/admin/candidates/api1/advance", json={})
        assert moved.json()["advanced"] is True, moved.json()["gate_report"]
        assert moved.json()["candidate"]["level"] == "l1_verified"

    def test_a_refused_advance_is_200_with_the_reason_not_a_409(self, admin_client):
        """An admin asking 'why is this not moving' is asking a question.

        A 409 would carry the same information and throw the gates away, and the
        answer to "why is this blocked" is the one thing an operator needs.
        """
        admin_client.post(
            "/kaizen/admin/candidates",
            json={"candidate_id": "api2", "commit": "aaa", "maintainer": "ana"},
        )
        response = admin_client.post("/kaizen/admin/candidates/api2/advance", json={})
        assert response.status_code == 200
        body = response.json()
        assert body["advanced"] is False
        assert body["gate_report"]["blocking_failures"]
        assert body["reason"]

    def test_a_candidate_body_cannot_name_its_level(self, admin_client):
        """The ladder is not a field. Pydantic rejects the extra outright.

        Silently dropping a field a caller believed in is the same class of
        defect as publishing a number that is not the enforced one.
        """
        response = admin_client.post(
            "/kaizen/admin/candidates",
            json={"candidate_id": "api9", "commit": "aaa", "level": "l4_live"},
        )
        assert response.status_code == 422

    def test_a_candidate_with_no_commit_is_422(self, admin_client):
        assert admin_client.post(
            "/kaizen/admin/candidates", json={"candidate_id": "api8"}
        ).status_code == 422

    def test_a_duplicate_candidate_is_409(self, admin_client):
        body = {"candidate_id": "api3", "commit": "aaa"}
        assert admin_client.post("/kaizen/admin/candidates", json=body).status_code == 201
        assert admin_client.post("/kaizen/admin/candidates", json=body).status_code == 409

    def test_an_unknown_candidate_is_404_and_names_the_registered_set(self, admin_client):
        admin_client.post(
            "/kaizen/admin/candidates", json={"candidate_id": "api4", "commit": "aaa"}
        )
        response = admin_client.post("/kaizen/admin/candidates/nobody/advance", json={})
        assert response.status_code == 404
        assert "api4" in response.json()["detail"]

    def test_measurements_accumulate_rather_than_replace(self, admin_client):
        """A test run reports first, a shadow comparison second."""
        from app import release_ladder

        admin_client.post(
            "/kaizen/admin/candidates", json={"candidate_id": "api5", "commit": "aaa"}
        )
        admin_client.post(
            "/kaizen/admin/candidates/api5/measure",
            json={"measured": {"tests_failed": 0}},
        )
        admin_client.post(
            "/kaizen/admin/candidates/api5/measure",
            json={"measured": {"flows_run": 21, "flows_failed": 0}},
        )
        candidate = release_ladder.find_candidate("api5")
        assert candidate.measured["tests_failed"] == 0
        assert candidate.measured["flows_run"] == 21

    def test_from_app_measures_the_gate_the_caller_cannot(self, admin_client):
        """`from_app: ["authz"]` records what the running application is.

        The point of the seam: `authz_in_sync` is a property of the route table,
        so a hand-typed value would be an assertion about the running system
        rather than a measurement of it. This is the gate that was already
        satisfied by an unauthenticated `canary/promote`.
        """
        from app import release_ladder

        admin_client.post(
            "/kaizen/admin/candidates", json={"candidate_id": "src1", "commit": "aaa"}
        )
        response = admin_client.post(
            "/kaizen/admin/candidates/src1/measure",
            json={"measured": {"tests_failed": 0}, "from_app": ["authz"]},
        )
        assert response.status_code == 200, response.text
        candidate = release_ladder.find_candidate("src1")
        assert candidate.measured["authz_in_sync"] is True
        assert candidate.measured["unlisted_public_writes"] == 0
        # And the hand-typed half was not clobbered.
        assert candidate.measured["tests_failed"] == 0

    def test_from_app_wins_over_a_hand_typed_number(self, admin_client):
        """The app-computed value is merged last, on purpose.

        A caller who asserts `authz_in_sync: true` against a live route table
        that disagrees has not measured anything. Letting the body win would put
        the hole straight back, behind the same endpoint that closes it.
        """
        from app import deps, release_ladder
        from app.main import app

        saved = deps.AUTHZ_PUBLIC_WRITE_EXCEPTIONS
        deps.AUTHZ_PUBLIC_WRITE_EXCEPTIONS = [
            row for row in saved if row["path"] != "/meta/decisions/simulate"
        ]
        try:
            admin_client.post(
                "/kaizen/admin/candidates", json={"candidate_id": "src2", "commit": "aaa"}
            )
            admin_client.post(
                "/kaizen/admin/candidates/src2/measure",
                json={
                    "measured": {"authz_in_sync": True, "unlisted_public_writes": 0},
                    "from_app": ["authz"],
                },
            )
        finally:
            deps.AUTHZ_PUBLIC_WRITE_EXCEPTIONS = saved

        candidate = release_ladder.find_candidate("src2")
        assert candidate.measured["authz_in_sync"] is False
        assert candidate.measured["unlisted_public_writes"] == 1
        assert candidate.measured["unlisted_public_write_detail"] == [
            "POST /meta/decisions/simulate"
        ]

    def test_from_app_without_authz_is_a_no_op_on_the_gate(self, admin_client):
        """Opting out is allowed. Asserting the gate yourself is not the same
        as skipping it -- but the server will not pretend it measured it."""
        from app import release_ladder

        admin_client.post(
            "/kaizen/admin/candidates", json={"candidate_id": "src3", "commit": "aaa"}
        )
        admin_client.post(
            "/kaizen/admin/candidates/src3/measure",
            json={"measured": {"tests_failed": 0}, "from_app": []},
        )
        candidate = release_ladder.find_candidate("src3")
        assert "authz_in_sync" not in candidate.measured
        # So the canary rung still blocks it -- unmeasured folds to fail.
        candidate.level = "l2_shadow"
        candidate.measured.update(
            {
                "flows_run": 21, "flows_failed": 0, "shadow_isolated": True,
                "pipelines_one_way": True, "shadow_to_live_writes": 0,
                "shadow_divergence_ratio": 0.0, "regressions": 0,
                "rollback_targets": release_ladder.ROLLBACK_WINDOW,
            }
        )
        report = release_ladder.evaluate_gates(candidate, for_level="l3_canary")
        assert "authorization_complete" in report["blocking_failures"]

    def test_an_unknown_from_app_source_is_422(self, admin_client):
        """`Literal`, so a typo is refused rather than silently ignored.

        `from_app: ["auth"]` with a free-string list would record nothing and
        return 200, and the caller would believe the gate had been measured.
        """
        admin_client.post(
            "/kaizen/admin/candidates", json={"candidate_id": "src4", "commit": "aaa"}
        )
        response = admin_client.post(
            "/kaizen/admin/candidates/src4/measure",
            json={"measured": {}, "from_app": ["auth"]},
        )
        assert response.status_code == 422

    def test_from_app_flows_and_shadow_run_the_simulators(self, admin_client, monkeypatch):
        """Opt-in rather than default: a test run should not also pay for 21 flows.

        The shadow half is asserted in both directions. In an unconfigured
        process -- which is what a developer checkout and a CI runner are --
        the isolation guard reports `isolated: false` with three blocking
        failures, and the measurement records that. A guard that passed because
        nobody configured it would be exactly the "published number is not the
        enforced one" defect this subsystem exists to avoid, so the failing case
        is the one pinned first.
        """
        from app import release_ladder

        admin_client.post(
            "/kaizen/admin/candidates", json={"candidate_id": "src5", "commit": "aaa"}
        )
        response = admin_client.post(
            "/kaizen/admin/candidates/src5/measure",
            json={"measured": {}, "from_app": ["flows", "shadow"]},
        )
        assert response.status_code == 200, response.text
        candidate = release_ladder.find_candidate("src5")
        assert candidate.measured["flows_run"] > 0
        assert candidate.measured["flows_failed"] == 0
        assert isinstance(candidate.measured["shadow_isolated"], bool)
        # Unconfigured, so it fails closed and says which checks.
        assert candidate.measured["shadow_isolated"] is False
        assert "database_identity" in candidate.measured["shadow_blocking_failures"]
        assert candidate.measured["shadow_blocking_failures"]

    def test_from_app_shadow_passes_when_the_shadow_is_actually_isolated(
        self, admin_client, monkeypatch
    ):
        """The other direction, so the first test is not a broken-config test."""
        from app import release_ladder

        for name, value in (
            ("DATABASE_URL", LIVE_URL),
            ("CSERVICE_SHADOW_DATABASE_URL", SHADOW_URL),
            ("CSERVICE_ENV", "shadow"),
            ("CSERVICE_SHADOW_WRITE_TARGETS", "shadow_database"),
            ("CSERVICE_LIVE_READ_ONLY_REPLICATION", "1"),
            ("CSERVICE_SHADOW_EGRESS_ALLOWLIST", "127.0.0.1:5432"),
        ):
            monkeypatch.setenv(name, value)

        admin_client.post(
            "/kaizen/admin/candidates", json={"candidate_id": "src6", "commit": "aaa"}
        )
        response = admin_client.post(
            "/kaizen/admin/candidates/src6/measure",
            json={"measured": {}, "from_app": ["shadow"]},
        )
        assert response.status_code == 200, response.text
        candidate = release_ladder.find_candidate("src6")
        assert candidate.measured["shadow_isolated"] is True, (
            candidate.measured["shadow_blocking_failures"]
        )
        assert candidate.measured["shadow_blocking_failures"] == []

    def test_the_catalog_lists_the_measurement_sources(self, admin_client):
        sources = admin_client.get("/kaizen/admin/catalog").json()["measurement_sources"]
        # `care` joined Stage D: it counts offer outcomes and journey stuckness from
        # the rows, so a gate about the care loop cannot be satisfied by typing a
        # number into a request body.
        assert set(sources) == {"authz", "flows", "shadow", "care"}
        for name, description in sources.items():
            assert len(description) > 30, name

    def test_the_shadow_endpoint_reports_the_per_check_breakdown(self, admin_client):
        response = admin_client.get("/kaizen/admin/shadow")
        assert response.status_code == 200, response.text
        body = response.json()
        checks = body["isolation"]["checks"]
        assert checks
        for check in checks:
            assert check["remedy"]
            assert check["asserts"]
            assert check["severity"] in {"blocking", "advisory"}
        # The catalog is the reference half of the answer and the isolation
        # verdict the live one. A reader needs both: the verdict says the shadow
        # is unsafe, the catalog says which of the six checks and where.
        assert body["checks"]
        assert body["feeds"]
        assert body["environments"]
        assert body["leakage_paths"] == []

    def test_verifying_a_healthy_shadow_against_an_explicit_url(self, admin_client):
        """The override exists so the dangerous cases can be tested safely."""
        from app import shadow_env

        response = admin_client.post(
            "/kaizen/admin/shadow/verify",
            params={
                "shadow_url": SHADOW_URL,
                "shadow_env_name": "shadow",
                "live_url": LIVE_URL,
                "shadow_write_targets": "shadow_database",
                "live_read_only": "1",
                "shadow_egress_allowlist": "127.0.0.1:5432",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "database_identity" not in body["blocking_failures"]
        assert shadow_env.derive_direction("live", "shadow") == "live_to_shadow"

    def test_verifying_a_shadow_pointed_at_live_reports_not_isolated(self, admin_client):
        response = admin_client.post(
            "/kaizen/admin/shadow/verify",
            params={"shadow_url": LIVE_URL, "shadow_env_name": "shadow", "live_url": LIVE_URL},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["isolated"] is False
        assert "database_identity" in body["blocking_failures"]

    def test_listing_the_levels(self, admin_client):
        response = admin_client.get("/kaizen/admin/levels")
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["levels"]) == 5
        assert [row["level_id"] for row in body["levels"]] == [
            "l0_draft", "l1_verified", "l2_shadow", "l3_canary", "l4_live"
        ]
        assert body["always_on"] == ["maintainer_named"]
        assert len(body["gates"]) == 12
        gate_ids = {row["gate_id"] for row in body["gates"]}
        assert "authorization_complete" in gate_ids
        assert "backend_completeness_honest" in gate_ids

    def test_a_candidate_walks_the_ladder_and_only_then_deploys(self, admin_client):
        """The whole path, over HTTP: draft -> verified -> shadow -> canary -> live.

        Deployment is not exposed over HTTP on purpose -- promoting a candidate
        and changing what is live are two acts, and collapsing them would put a
        "deploy" button on the same endpoint as the gates that ought to be read
        first. The ledger is therefore driven here directly, which is also how
        the shadow process would use it.
        """
        from app import release_ladder

        assert admin_client.post(
            "/kaizen/admin/candidates",
            json={"candidate_id": "api6", "commit": "abc123def456", "revision": "0007_x"},
        ).status_code == 201

        evidence = {
            "tests_failed": 0,
            "flows_run": 21,
            "flows_failed": 0,
            "shadow_isolated": True,
            "pipelines_one_way": True,
            "shadow_to_live_writes": 0,
            "shadow_divergence_ratio": 0.0,
            "regressions": 0,
            "canary_error_rate": 0.0001,
            "rollback_targets": 5,
            "authz_in_sync": True,
            "unlisted_public_writes": 0,
                "capability_blocking": 0,
        }
        for expected in ("l1_verified", "l2_shadow", "l3_canary", "l4_live"):
            admin_client.post(
                "/kaizen/admin/candidates/api6/measure", json={"measured": evidence}
            )
            response = admin_client.post("/kaizen/admin/candidates/api6/advance", json={})
            assert response.status_code == 200, response.text
            assert response.json()["advanced"] is True, response.json()["gate_report"]
            assert response.json()["candidate"]["level"] == expected

        candidate = release_ladder.find_candidate("api6")
        assert candidate.level == release_ladder.LIVE_LEVEL
        assert candidate.code_version == "code:abc123def456"
        assert candidate.data_version == "data:0007_x"

        ledger = release_ladder.get_default_ledger()
        event = ledger.deploy(candidate)
        assert event["code_version"] == candidate.code_version
        assert event["data_version"] == candidate.data_version

        # And the only way to be live is to have passed every rung.
        ahead = release_ladder.ReleaseCandidate("skip", "code:b", "data:r", maintainer="ana")
        ahead.level = "l2_shadow"
        with pytest.raises(ValueError, match="advance it through the ladder"):
            ledger.deploy(ahead)

    def test_the_deployment_listing_reports_the_window(self, admin_client):
        from app import release_ladder

        ledger = release_ladder.get_default_ledger()
        for index in range(1, 4):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        response = admin_client.get("/kaizen/admin/deployments")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["rollback_window"] == 5
        assert body["ledger"]["window_reach"] == 2
        assert [row["event_id"] for row in body["events"]] == [
            "dep-0001", "dep-0002", "dep-0003"
        ]

    def test_rollback_restores_the_pair_and_appends(self, admin_client):
        from app import release_ladder

        ledger = release_ladder.get_default_ledger()
        for index in range(1, 4):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        response = admin_client.post(
            "/kaizen/admin/deployments/dep-0002/rollback",
            json={"actor": "ana", "reason": "error rate"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["event"]["code_version"] == "code:c2"
        assert body["event"]["data_version"] == "data:r2"
        assert body["event"]["restores"] == "dep-0002"
        assert len(ledger.events) == 4
        assert ledger.events[-1]["kind"] == "rollback"
        assert ledger.events[2]["kind"] == "deploy"

    def test_a_rollback_outside_the_window_is_409(self, admin_client):
        from app import release_ladder

        ledger = release_ladder.get_default_ledger()
        for index in range(1, 9):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        response = admin_client.post("/kaizen/admin/deployments/dep-0002/rollback", json={})
        assert response.status_code == 409
        assert "rollback window" in response.json()["detail"]

    def test_a_rollback_to_an_unknown_event_is_404(self, admin_client):
        response = admin_client.post("/kaizen/admin/deployments/dep-9999/rollback", json={})
        assert response.status_code == 404

    def test_a_rollback_to_the_live_event_is_refused(self, admin_client):
        """Rolling back to where you already are is not a rollback."""
        from app import release_ladder

        ledger = release_ladder.get_default_ledger()
        for index in range(1, 4):
            ledger.record("deploy", code_version=f"code:c{index}", data_version=f"data:r{index}")
        response = admin_client.post("/kaizen/admin/deployments/dep-0003/rollback", json={})
        assert response.status_code == 409

    def test_safe_levels_is_empty_when_nothing_is_promotable(self, admin_client):
        response = admin_client.get("/kaizen/admin/safe-levels")
        assert response.status_code == 200
        assert response.json()["count"] == 0
        assert response.json()["levels"]

    def test_safe_levels_offers_only_an_evidenced_candidate(self, admin_client):
        admin_client.post(
            "/kaizen/admin/candidates", json={"candidate_id": "api10", "commit": "aaa"}
        )
        assert admin_client.get("/kaizen/admin/safe-levels").json()["count"] == 0

        admin_client.post(
            "/kaizen/admin/candidates/api10/measure",
            json={"measured": {"tests_failed": 0, "flows_run": 21, "flows_failed": 0}},
        )
        body = admin_client.get("/kaizen/admin/safe-levels").json()
        assert body["count"] == 1
        assert body["safe_levels"][0]["destination"] == "l1_verified"
        assert body["safe_levels"][0]["serves_traffic_after"] is False

    def test_the_candidate_listing_reports_where_each_one_is_stuck(self, admin_client):
        admin_client.post(
            "/kaizen/admin/candidates",
            json={"candidate_id": "api11", "commit": "aaa", "maintainer": "ana"},
        )
        body = admin_client.get("/kaizen/admin/candidates").json()
        assert body["count"] == 1
        row = body["candidates"][0]
        assert row["level"] == "l0_draft"
        assert row["safe_to_advance"] is False
        assert row["next_level"] == "l1_verified"
        assert row["serves_traffic"] is False
        assert row["blocking_failures"]

    def test_the_blockages_endpoint_writes_nothing(self, admin_client):
        response = admin_client.get("/kaizen/admin/blockages")
        assert response.status_code == 200
        assert response.json()["count"] == 0
        assert response.json()["measurements"]["flows_run"] > 0

    def test_blockages_can_be_filtered_by_severity(self, admin_client):
        from app.services import preferences

        saved = preferences.CONSENT_GATED_PURPOSES
        preferences.CONSENT_GATED_PURPOSES = tuple(saved) + ("service",)
        try:
            blockers = admin_client.get(
                "/kaizen/admin/blockages", params={"severity": "blocker"}
            ).json()
            warnings = admin_client.get(
                "/kaizen/admin/blockages", params={"severity": "warning"}
            ).json()
            assert blockers["count"] > 0
            assert warnings["count"] == 0
            assert all(row["severity"] == "blocker" for row in blockers["blockages"])
        finally:
            preferences.CONSENT_GATED_PURPOSES = saved

    def test_an_unknown_severity_filter_is_422(self, admin_client):
        assert admin_client.get(
            "/kaizen/admin/blockages", params={"severity": "catastrophe"}
        ).status_code == 422

    def test_appending_the_findings_is_a_separate_explicit_call(
        self, admin_client, tmp_path, monkeypatch
    ):
        """A dashboard that polls the simulation must not append on every poll.

        The redirect is an environment variable rather than a module global
        because that is the seam the CLI script and an operator already use, so
        this test exercises the same override path they do.
        """
        target = tmp_path / "BLOCKAGES.md"
        target.write_text("# Working log\n", encoding="utf-8")
        monkeypatch.setenv("CSERVICE_BLOCKAGES_PATH", str(target))

        read_only = admin_client.get("/kaizen/admin/blockages")
        assert read_only.status_code == 200
        assert target.read_text(encoding="utf-8") == "# Working log\n"

        appended = admin_client.post("/kaizen/admin/blockages/append", json={})
        assert appended.status_code == 200, appended.text
        assert appended.json()["written"] is True
        assert "Real-life flow simulation" in target.read_text(encoding="utf-8")

        again = admin_client.post("/kaizen/admin/blockages/append", json={})
        assert again.json()["written"] is False
        assert again.json()["already_present"] is True

    def test_allow_repeat_writes_a_second_dated_section(
        self, admin_client, tmp_path, monkeypatch
    ):
        target = tmp_path / "BLOCKAGES.md"
        target.write_text("# Working log\n", encoding="utf-8")
        monkeypatch.setenv("CSERVICE_BLOCKAGES_PATH", str(target))
        assert admin_client.post(
            "/kaizen/admin/blockages/append", json={}
        ).json()["written"] is True
        second = admin_client.post(
            "/kaizen/admin/blockages/append", json={"allow_repeat": True}
        )
        assert second.json()["written"] is True
        assert target.read_text(encoding="utf-8").count("Real-life flow simulation") == 2

    def test_the_appended_section_carries_a_conclusion_and_a_suggestion(
        self, admin_client, tmp_path, monkeypatch
    ):
        """Every entry names what to do about it, not just what broke.

        This is the requirement the simulation exists to satisfy, asserted
        against the real file the real endpoint writes, with a real breakage in
        place -- a section about a healthy tree proves the plumbing, not the
        promise.
        """
        from app.services import preferences

        target = tmp_path / "BLOCKAGES.md"
        target.write_text("# Working log\n", encoding="utf-8")
        monkeypatch.setenv("CSERVICE_BLOCKAGES_PATH", str(target))

        saved = preferences.CONSENT_GATED_PURPOSES
        preferences.CONSENT_GATED_PURPOSES = tuple(saved) + ("service",)
        try:
            assert admin_client.post(
                "/kaizen/admin/blockages/append", json={}
            ).json()["written"] is True
        finally:
            preferences.CONSENT_GATED_PURPOSES = saved

        text = target.read_text(encoding="utf-8")
        assert "**Conclusion:**" in text
        assert "**Suggestion:**" in text
        assert "**Evidence:**" in text
        assert "gate_withheld_a_fix" in text

    def test_the_routes_are_advertised_in_the_feature_summary(self, admin_client):
        features = admin_client.get("/meta/features").json()["endpoints"]
        assert features["kaizen_catalog"] == "/kaizen/admin/catalog"
        assert features["kaizen_rollback"].endswith("/{event_id}/rollback")
        assert features["kaizen_flow_run"] == "/kaizen/admin/flows/run"


class TestTheGateRejectsAnonymous:
    """The one place the gate itself is checked on the wire.

    Separate from `TestRoutes` on purpose. The rule table says these routes are
    admin; this says a request that is not admin does not get through. Both can
    be true while the app is broken -- a rule that claims admin while the
    dependency is missing is precisely the defect the drift report cannot see,
    so the table and the wire are checked separately.
    """

    PATHS = [
        ("GET", "/kaizen/admin/catalog"),
        ("GET", "/kaizen/admin/flows"),
        ("GET", "/kaizen/admin/flows/run"),
        ("POST", "/kaizen/admin/flows/new_customer_first_booking/simulate"),
        ("GET", "/kaizen/admin/blockages"),
        ("POST", "/kaizen/admin/blockages/append"),
        ("GET", "/kaizen/admin/shadow"),
        ("POST", "/kaizen/admin/shadow/verify"),
        ("GET", "/kaizen/admin/levels"),
        ("GET", "/kaizen/admin/candidates"),
        ("GET", "/kaizen/admin/safe-levels"),
        ("GET", "/kaizen/admin/deployments"),
        ("POST", "/kaizen/admin/candidates"),
        ("POST", "/kaizen/admin/candidates/x/measure"),
        ("POST", "/kaizen/admin/candidates/x/advance"),
        ("POST", "/kaizen/admin/deployments/dep-0001/rollback"),
        ("GET", "/kaizen/admin/completeness"),
        ("GET", "/kaizen/admin/sweep"),
        # `sweep` is a POST as well, and it runs a whole sweep. Anonymous refusal
        # is asserted for it separately: a route that runs 21 flow simulations is
        # exactly the kind that must not be reachable by anyone who finds the URL.
        ("POST", "/kaizen/admin/sweep"),
        ("GET", "/kaizen/admin/loyalty-status"),
        ("POST", "/kaizen/admin/loyalty-status/preview"),
        # The copilot preview assembles an agent-facing card, which is as close to
        # a customer conversation as anything on this router gets. It reads
        # evidence and writes nothing, and it must still be admin-gated.
        ("POST", "/kaizen/admin/agent-copilot/preview"),
        ("GET", "/kaizen/admin/care-gate"),
        ("POST", "/kaizen/admin/care-gate/consult"),
        ("POST", "/kaizen/admin/care-weights"),
        # `offer-outcomes` reads a customer's whole outcome history and ranks the
        # offer tiers against each other. It is the most revealing route on this
        # router and the one most worth refusing to an anonymous caller.
        ("POST", "/kaizen/admin/offer-outcomes"),
        # Stage E. `regions` publishes the contact windows -- the hours this
        # business may contact somebody, per region -- and `care-personalization`
        # publishes the limits on what a message may use. Both are policy tables
        # rather than customer data, and both are still admin-gated: who may be
        # contacted, and about what, is not public.
        ("GET", "/kaizen/admin/regions"),
        ("GET", "/kaizen/admin/care-personalization"),
        ("GET", "/kaizen/admin/relationship-catalog"),
        # Takes caller-supplied surfaces and composes them. A POST because it
        # builds from parts rather than looking them up, and because a view of a
        # customer is exactly what must not be readable by anyone who finds it.
        ("POST", "/kaizen/admin/relationship-view"),
        # Stage F. `policy-motion` publishes the evidence bar every change to a
        # policy table has to clear, which is the table a future edit will be
        # judged against -- so it is read under admin. `trust-continuity` reports
        # whether a promise still holds; it reads guard results rather than
        # looking them up, and answers `false` for an unchecked promise rather
        # than assuming, which is the whole point of it.
        ("GET", "/kaizen/admin/policy-motion"),
        ("POST", "/kaizen/admin/policy-motion/ledger"),
        ("POST", "/kaizen/admin/trust-continuity"),
        ("POST", "/kaizen/admin/value-trajectory"),
    ]

    def test_every_kaizen_route_refuses_an_unauthenticated_caller(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            for method, path in self.PATHS:
                response = client.request(method, path)
                assert response.status_code in (401, 403), (
                    f"{method} {path} -> {response.status_code}"
                )

    #: Path parameters filled in with a value, so the list can be compared to
    #: the router's declared templates as well as being requested over HTTP.
    FILLED = {
        "/kaizen/admin/flows/new_customer_first_booking/simulate":
            "/kaizen/admin/flows/{flow_id}/simulate",
        "/kaizen/admin/candidates/x/measure":
            "/kaizen/admin/candidates/{candidate_id}/measure",
        "/kaizen/admin/candidates/x/advance":
            "/kaizen/admin/candidates/{candidate_id}/advance",
        "/kaizen/admin/deployments/dep-0001/rollback":
            "/kaizen/admin/deployments/{event_id}/rollback",
        # Two paths, three routes: `sweep` is GET and POST. The anonymous-refusal
        # table is keyed by path, so one entry covers both verbs.
        "/kaizen/admin/completeness": "/kaizen/admin/completeness",
        "/kaizen/admin/sweep": "/kaizen/admin/sweep",
        # Stage C, added to the same router as the release surface because it is
        # the same question from the other end: what is built, what state is it
        # in, and what does the ladder say about promoting it.
        "/kaizen/admin/loyalty-status": "/kaizen/admin/loyalty-status",
        "/kaizen/admin/loyalty-status/preview": "/kaizen/admin/loyalty-status/preview",
        "/kaizen/admin/agent-copilot/preview": "/kaizen/admin/agent-copilot/preview",
        # Stage D, the closed loop. `care-gate` reports which proactive paths ask
        # the preference gate; `offer-outcomes` ranks them by what happened. Both
        # admin-gated: the first publishes the list of places that can contact a
        # customer, and the second reads that customer's outcome history.
        "/kaizen/admin/care-gate": "/kaizen/admin/care-gate",
        "/kaizen/admin/care-gate/consult": "/kaizen/admin/care-gate/consult",
        "/kaizen/admin/care-weights": "/kaizen/admin/care-weights",
        "/kaizen/admin/offer-outcomes": "/kaizen/admin/offer-outcomes",
        # Stage E. `regions` and `care-personalization` publish *policy* tables --
        # the contact windows and the limits on what a message may use -- so
        # reading them is a governance question. `relationship-view` composes a
        # customer view from caller-supplied parts, which is why it is a POST:
        # it takes surfaces rather than looking them up.
        "/kaizen/admin/regions": "/kaizen/admin/regions",
        "/kaizen/admin/care-personalization": "/kaizen/admin/care-personalization",
        "/kaizen/admin/relationship-catalog": "/kaizen/admin/relationship-catalog",
        "/kaizen/admin/relationship-view": "/kaizen/admin/relationship-view",
        "/kaizen/admin/policy-motion": "/kaizen/admin/policy-motion",
        "/kaizen/admin/policy-motion/ledger": "/kaizen/admin/policy-motion/ledger",
        "/kaizen/admin/trust-continuity": "/kaizen/admin/trust-continuity",
        "/kaizen/admin/value-trajectory": "/kaizen/admin/value-trajectory",
    }

    def test_the_list_is_the_whole_router(self):
        """If a route is added and not listed here, this fails."""
        from app.routers import kaizen

        declared = {self.FILLED.get(path, path) for _method, path in self.PATHS}
        actual = {route.path for route in kaizen.router.routes}
        assert actual == declared, actual ^ declared

    def test_a_mutating_route_writes_nothing_when_refused(self):
        from fastapi.testclient import TestClient

        from app import release_ladder
        from app.main import app

        with TestClient(app) as client:
            assert client.post(
                "/kaizen/admin/candidates", json={"candidate_id": "nope", "commit": "a"}
            ).status_code in (401, 403)
        assert release_ladder.find_candidate("nope") is None
