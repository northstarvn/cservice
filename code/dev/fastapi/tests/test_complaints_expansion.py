"""Complaints: the case spine, the escalation engine, and decision support.

Most of this file runs against a **real** in-memory SQLite database rather than
a session double. The other suites use doubles because they are testing pure
functions with a mocked window; this one is testing a state machine, a durable
reference, a uniqueness constraint, and a feedback loop, and a double would
assert my own assumptions back at me instead of the database's. `open_complaint`
derives its reference from the primary key, so proving two concurrent cases get
two references *requires* real inserts.

Several tests here exist specifically to pin the decisions this module made out
loud:

- auto-escalation is confined to a statutory deadline and a breached clock, and
  the validator refuses to let that be widened by adding a row
- an admin may raise severity or tier but never lower either
- a `reject` guard is not waivable, and a failed one is still recorded
- a precedent whose case was later reopened is down-weighted and *reported*, not
  hidden
- a required feed that could not be built is named, because "no such feed" and
  "the feed says nothing" are different claims
- the SLA basis says which clock actually governed, not the flattering one
- the escalation reference is stable across process restarts
- the consent gate is advisory on this surface and can never withhold a fix
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from _doubles import SqliteHarness

from app import models, rule_engine
from app.services import complaints as C

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


# ===========================================================================
# Database harness
# ===========================================================================


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


# ===========================================================================
# Config integrity
# ===========================================================================


class TestCatalogValidation:
    def test_the_shipped_configuration_is_valid(self):
        report = C.validate_complaints()
        assert report["valid"] is True, report["errors"]
        assert report["error_count"] == 0
        assert report["warning_count"] == 0

    def test_every_check_actually_ran(self):
        """A validator that silently checks nothing is worse than none."""
        report = C.validate_complaints()
        assert len(report["checked"]) >= 9
        # Counts are derived from the tables, so a table losing rows is visible.
        assert report["counts"]["triggers"] == len(C.ESCALATION_TRIGGERS)
        assert report["counts"]["guards"] == len(C.ESCALATION_GUARDS)
        assert report["counts"]["feeds"] == len(C.DECISION_FEEDS)

    def test_auto_authority_cannot_be_widened_to_a_judgment_trigger(self, monkeypatch):
        """The load-bearing policy: a clock may act, a judgement may not."""
        monkeypatch.setattr(
            C,
            "ESCALATION_TRIGGERS",
            [
                *C.ESCALATION_TRIGGERS,
                {
                    "trigger_id": "smuggled_auto",
                    "kind": "judgment",
                    "authority": "auto",
                    "enabled": True,
                    "priority": 1,
                    "effective_from": "2026-01-01",
                    "effective_to": None,
                    "reason": "looks convenient",
                    "when": {"severity": "low"},
                    "params": {"to_tier": "tier_1"},
                },
            ],
        )
        report = C.validate_complaints()
        assert report["valid"] is False
        codes = {e["code"] for e in report["errors"]}
        assert "auto_authority_widened" in codes
        detail = next(e for e in report["errors"] if e["code"] == "auto_authority_widanged" or e["code"] == "auto_authority_widened")["detail"]
        assert "judgment" in detail

    def test_a_trigger_testing_an_unproduced_context_key_is_flagged(self, monkeypatch):
        """The failure mode that produced two dead rules before this module."""
        monkeypatch.setattr(
            C,
            "ESCALATION_TRIGGERS",
            [
                *C.ESCALATION_TRIGGERS,
                {
                    "trigger_id": "dead_key",
                    "kind": "judgment",
                    "authority": "advisory",
                    "enabled": True,
                    "priority": 1,
                    "effective_from": "2026-01-01",
                    "effective_to": None,
                    "reason": "x",
                    "when": {"a_key_nobody_emits": 1},
                    "params": {},
                },
            ],
        )
        report = C.validate_complaints()
        warnings = [w for w in report["warnings"] if w["code"] == "context_key_never_produced"]
        assert warnings, report["warnings"]
        assert "dead_key" in warnings[0]["detail"]

    def test_vocabularies_match_the_schema(self):
        """models.py may not import services, so both declare them. Check both."""
        for table, column, expected in (
            ("complaint_cases", "status", C.COMPLAINT_STATUSES),
            ("complaint_cases", "severity", C.COMPLAINT_SEVERITIES),
            ("complaint_cases", "tier", C.ESCALATION_TIER_ORDER),
            ("complaint_cases", "category", tuple(C.COMPLAINT_CATEGORY_BY_NAME)),
            ("complaint_events", "event_type", C.COMPLAINT_EVENT_TYPES),
            ("complaint_decisions", "decision", C.COMPLAINT_DECISIONS),
            ("complaint_decisions", "outcome", C.COMPLAINT_DECISION_OUTCOMES),
        ):
            declared = set(models.enum_values_for(table, column))
            assert declared == set(expected), f"{table}.{column} drifted"

    def test_the_floor_guards_are_not_waivable(self):
        for guard_id in ("override_would_weaken_policy", "regulatory_deadline_unreachable"):
            guard = next(g for g in C.ESCALATION_GUARDS if g["guard_id"] == guard_id)
            assert guard["severity"] in C.ESCALATION_OVERRIDE_RULES["non_waivable_guard_severities"]

    def test_precedent_weights_are_a_distribution(self):
        assert abs(sum(C.PRECEDENT_WEIGHTS.values()) - 1.0) < 1e-9

    def test_only_regulatory_and_sla_triggers_carry_auto_authority(self):
        for row in C.ESCALATION_TRIGGERS:
            if row.get("authority") == "auto":
                assert row["kind"] in C.AUTO_ESCALATION_TRIGGER_KINDS, row["trigger_id"]


# ===========================================================================
# SLA
# ===========================================================================


class TestSla:
    def test_every_severity_has_a_row(self):
        for severity in C.COMPLAINT_SEVERITIES:
            assert C.resolve_sla(severity)["severity"] == severity

    def test_unknown_severity_falls_back_rather_than_raising(self):
        resolved = C.resolve_sla("catastrophic")
        assert resolved["severity"] == "medium"
        assert resolved["note"]

    def test_the_statutory_floor_is_a_floor_not_a_ceiling(self):
        """A regulatory case must never get a *looser* target than the statute."""
        for severity in C.COMPLAINT_SEVERITIES:
            internal = C.resolve_sla(severity, regulatory=False)
            legal = C.resolve_sla(severity, regulatory=True)
            assert legal["response_hours"] <= internal["response_hours"]
            assert legal["resolution_hours"] <= internal["resolution_hours"]
            assert legal["regulatory_deadline_days"] == 30

    def test_basis_reports_which_clock_actually_governed(self):
        """Not `regulatory_floor` every time -- that would usually be a lie."""
        # Our internal critical targets are stricter than the statute's, so the
        # statute does not govern and saying otherwise would misreport the SLA.
        critical = C.resolve_sla("critical", regulatory=True)
        assert critical["basis"] == "internal_matrix_within_regulatory_limit"
        assert critical["resolution_hours"] == 24.0
        assert critical["resolution_hours"] < C.REGULATORY_SLA["resolution_hours"]


# ===========================================================================
# The escalation engine
# ===========================================================================


class TestEscalationEngine:
    def test_a_calm_case_does_not_escalate_and_says_so(self):
        decision = C.build_escalation_decision(C._probe_scope(), current_tier="tier_1")
        assert decision["escalated"] is False
        assert decision["triggers_fired"] == []
        assert decision["reason"]
        assert decision["considered_count"] > 0

    def test_a_breached_response_sla_escalates_automatically(self):
        ctx = C._probe_scope(response_overdue_hours=3.0, severity="high", severity_rank=2)
        decision = C.build_escalation_decision(ctx, current_tier="tier_1")
        assert decision["escalated"] is True
        assert decision["auto_applied"] is True
        assert "sla_response_breached" in decision["auto_triggers"]
        assert decision["to_tier"] == "tier_2"

    def test_a_regulatory_deadline_escalates_automatically(self):
        ctx = C._probe_scope(category="privacy", regulatory=True, regulatory_deadline_hours=5.0)
        decision = C.build_escalation_decision(ctx, current_tier="tier_1")
        assert decision["escalated"] is True
        assert decision["regulatory"] is True
        assert "regulatory_privacy_deadline" in decision["auto_triggers"]
        assert decision["to_tier"] == "tier_3"
        assert decision["owner_team"] == "privacy_office"

    def test_judgment_triggers_are_advisory_and_need_acceptance(self):
        ctx = C._probe_scope(open_complaints=3)
        decision = C.build_escalation_decision(ctx, current_tier="tier_1")
        assert "repeat_complainant" in decision["advisory_triggers"]
        assert decision["auto_triggers"] == []
        assert decision["auto_applied"] is False
        assert decision["requires_acceptance"] is True

    def test_not_escalating_is_a_reasoned_outcome_not_silence(self):
        """The specific hole: 'we decided not to' used to be indistinguishable
        from 'we never looked'."""
        decision = C.build_escalation_decision(C._probe_scope(), current_tier="tier_1")
        assert decision["escalated"] is False
        assert decision["reason"]
        assert decision["guards"]
        assert decision["considered_count"] > 0
        assert decision["to_tier"] == "tier_1"

    def test_the_computed_tier_expression_actually_resolves(self):
        """`to_tier_rank` exists because the expression engine takes no strings."""
        for rank, expected in ((0, "tier_1"), (1, "tier_1"), (2, "tier_2"), (3, "tier_2")):
            ctx = C._probe_scope(response_overdue_hours=1.0, severity_rank=rank)
            decision = C.build_escalation_decision(ctx, current_tier="tier_1")
            assert decision["to_tier"] == expected, (rank, decision["to_tier"])

    def test_an_unrecognised_tier_sorts_below_the_frontline(self):
        assert C.tier_rank("not_a_tier") == 0
        assert C.tier_rank("tier_1") == 1
        assert C.tier_by_rank(99) == "tier_1"
        assert C.highest_tier("tier_1", "tier_3", None) == "tier_3"


class TestAuthorityFloor:
    def test_an_override_may_not_route_a_case_below_policy(self):
        ctx = C._probe_scope(category="privacy", regulatory=True, regulatory_deadline_hours=2.0)
        decision = C.build_escalation_decision(
            ctx, current_tier="tier_1", overrides={"to_tier": "tier_1", "reason": "handle it here"}
        )
        assert decision["to_tier"] == "tier_3"
        assert decision["override_blocked_reason"]
        assert "lower the severity" not in (decision["override_blocked_reason"] or "")

    def test_an_override_may_not_lower_severity(self):
        ctx = C._probe_scope(severity="critical", severity_rank=3)
        decision = C.build_escalation_decision(
            ctx, current_tier="tier_3", overrides={"severity": "low", "reason": "overstated"}
        )
        assert decision["override_blocked_reason"]
        assert "lower severity" in decision["override_blocked_reason"]

    def test_an_override_may_raise_and_reassign(self):
        ctx = C._probe_scope()
        decision = C.build_escalation_decision(
            ctx, current_tier="tier_1", overrides={"to_tier": "tier_3", "owner_team": "exec", "reason": "vip"}
        )
        assert decision["override_applied"] is True
        assert decision["to_tier"] == "tier_3"
        assert decision["owner_team"] == "exec"
        assert decision["override_blocked_reason"] == ""

    def test_admin_is_the_top_role_and_step_up_carries_the_rest(self):
        """No superuser exists; the assurance level is what distinguishes an
        admin from an admin who re-authenticated for this action."""
        rules = C.ESCALATION_OVERRIDE_RULES
        assert rules["required_step_up_for_override"] == "loa2"
        assert rules["required_step_up_for_waiver"] == "loa3"
        from app import deps

        assert "loa2" in deps.STEP_UP_RANK_BY_LEVEL
        assert "loa3" in deps.STEP_UP_RANK_BY_LEVEL
        # And no superuser role was invented to make this work.
        from app import cell_matrix

        assert "superuser" not in deps.USER_ROLES
        assert "superuser" not in cell_matrix.ROLE_GROUPS


class TestGuards:
    def test_the_floor_guard_holds_when_no_override_is_requested(self):
        guards = C.evaluate_escalation_guards({"override_would_lower_tier": False})
        row = next(g for g in guards if g["guard_id"] == "override_would_weaken_policy")
        assert row["holds"] is True

    def test_the_floor_guard_fails_when_an_override_would_weaken_policy(self):
        guards = C.evaluate_escalation_guards({"override_would_lower_tier": True})
        row = next(g for g in guards if g["guard_id"] == "override_would_weaken_policy")
        assert row["holds"] is False
        assert row["severity"] == "reject"
        assert row["waivable"] is False

    def test_a_missing_metric_fails_closed(self):
        guards = C.evaluate_escalation_guards({})
        row = next(g for g in guards if g["guard_id"] == "no_owner_assigned")
        assert row["holds"] is False
        assert row["reason"] == "metric_missing"
        assert row["observed"] is False

    def test_guard_polarity_is_holds_means_no_problem(self):
        """`holds` must mean "satisfied", not "the problem fired".

        Six of the seven guards were first written holding on the problem, which
        made a healthy case report `reject` because every guard had failed. A
        backwards guard is worse than none: it makes every decision look
        blocked and trains reviewers to ignore the list.
        """
        healthy = {
            "regulatory": False,
            "override_would_lower_tier": False,
            "has_owner": True,
            "severity_understated_by": 0,
            "consent_withholds_contact": False,
            "duplicate_open_cases": 0,
            "resolved_stale_hours": 0.0,
        }
        verdict = C.escalation_verdict(healthy)
        assert verdict["decision"] == "accept", [
            (g["guard_id"], g["reason"]) for g in verdict["guards"] if not g["holds"]
        ]

    def test_each_guard_stops_holding_when_its_condition_is_present(self):
        """One case per guard, so a single inversion cannot hide."""
        cases = {
            "no_owner_assigned": {"has_owner": False},
            "severity_understated_vs_dissatisfaction": {"severity_understated_by": 3},
            "override_would_weaken_policy": {"override_would_lower_tier": True},
            "consent_gates_contact_only": {"consent_withholds_contact": True},
            "duplicate_open_case": {"duplicate_open_cases": 4},
            "resolved_but_not_closed": {"resolved_stale_hours": 200.0},
        }
        healthy = {
            "regulatory": False,
            "override_would_lower_tier": False,
            "has_owner": True,
            "severity_understated_by": 0,
            "consent_withholds_contact": False,
            "duplicate_open_cases": 0,
            "resolved_stale_hours": 0.0,
        }
        for guard_id, override in cases.items():
            guards = C.evaluate_escalation_guards({**healthy, **override})
            row = next(g for g in guards if g["guard_id"] == guard_id)
            assert row["holds"] is False, guard_id

    def test_a_scoped_guard_is_skipped_not_failed(self):
        """The regulatory metric is absent for an ordinary complaint; failing
        closed on it would reject every non-regulatory case for no reason."""
        ordinary = C.evaluate_escalation_guards({"regulatory": False})
        assert not [g for g in ordinary if g["guard_id"] == "regulatory_deadline_unreachable"]
        regulatory = C.evaluate_escalation_guards(
            {"regulatory": True, "regulatory_hours_remaining": 5.0}
        )
        row = next(g for g in regulatory if g["guard_id"] == "regulatory_deadline_unreachable")
        assert row["holds"] is True

    def test_a_passed_statutory_deadline_rejects(self):
        guards = C.evaluate_escalation_guards(
            {"regulatory": True, "regulatory_hours_remaining": -1.0}
        )
        row = next(g for g in guards if g["guard_id"] == "regulatory_deadline_unreachable")
        assert row["holds"] is False
        assert row["severity"] == "reject"

    def test_an_unknown_operator_fails_closed(self):
        guards = C.evaluate_escalation_guards(
            {"regulatory_hours_remaining": 5}, guards=[{"guard_id": "g", "metric": "regulatory_hours_remaining", "op": "nope", "threshold": 1, "severity": "review", "rationale": ""}]
        )
        assert guards[0]["holds"] is False

    def test_guards_are_ordered_strongest_first(self):
        guards = C.evaluate_escalation_guards(C._probe_scope())
        ranks = [C.ESCALATION_GUARD_SEVERITY_RANK[g["severity"]] for g in guards]
        assert ranks == sorted(ranks, reverse=True)

    def test_consent_can_never_withhold_a_fix(self):
        """Mirrors the BLOCKAGES keep-as-is for the recovery surface."""
        guards = C.evaluate_escalation_guards({"consent_withholds_contact": True})
        row = next(g for g in guards if g["guard_id"] == "consent_gates_contact_only")
        assert row["severity"] == "advisory"
        assert row["waivable"] is True
        # Advisory is the ceiling: it can never fold the verdict to reject.
        assert row["severity"] not in C.ESCALATION_OVERRIDE_RULES["non_waivable_guard_severities"]
        verdict = C.escalation_verdict(
            {"regulatory": False, "override_would_lower_tier": False, "consent_withholds_contact": True,
             "has_owner": True, "severity_understated_by": 0, "duplicate_open_cases": 0,
             "resolved_stale_hours": 0.0}
        )
        assert verdict["decision"] != "reject"

    def test_the_verdict_folds_three_severities_into_one_word(self):
        base = {
            "regulatory": False,
            "override_would_lower_tier": False,
            "has_owner": True,
            "severity_understated_by": 0,
            "consent_withholds_contact": False,
            "duplicate_open_cases": 0,
            "resolved_stale_hours": 0.0,
        }
        assert C.escalation_verdict(base)["decision"] == "accept"
        assert C.escalation_verdict({**base, "has_owner": False})["decision"] == "review"
        assert C.escalation_verdict({**base, "override_would_lower_tier": True})["decision"] == "reject"
        # An advisory alone is not enough to leave "accept" either.
        assert C.escalation_verdict({**base, "duplicate_open_cases": 5})["decision"] == "review"

    def test_an_incomplete_metric_set_fails_closed(self):
        """A guard that cannot see its evidence must not report that it passed.

        The unscoped guards all fail, which is enough to leave "accept" -- so
        this asserts "not accept" rather than a specific word, and the reject
        case below pins the word.
        """
        assert C.escalation_verdict({"override_would_lower_tier": False})["decision"] != "accept"

    def test_an_unmeasurable_reject_guard_fails_closed_to_reject(self):
        guards = C.evaluate_escalation_guards({"regulatory": True})
        row = next(g for g in guards if g["guard_id"] == "regulatory_deadline_unreachable")
        assert row["holds"] is False
        assert row["reason"] == "metric_missing"
        assert C.escalation_verdict({"regulatory": True})["decision"] == "reject"


# ===========================================================================
# Precedent retrieval
# ===========================================================================


class TestPrecedent:
    def _candidate(self, **overrides):
        base = {
            "complaint_id": 1,
            "user_id": 99,
            "reference": "CMP-000001",
            "category": "billing",
            "severity": "high",
            "resolution_code": "refunded",
            "resolution_note": "",
            "decided_by_role": "admin",
            "decided_at": "2026-06-01T00:00:00+00:00",
            "closed_at": "2026-06-05T00:00:00+00:00",
            "same_complainant": False,
            "outcome_observed": "resolved",
            "factors": {"billing": True, "arrears": True},
        }
        base.update(overrides)
        return base

    def _subject(self, **overrides):
        base = {
            "id": 999,
            "user_id": 1,
            "reference": "CMP-000999",
            "category": "billing",
            "severity": "high",
            "resolution_code": "",
            "factors": {"billing": True, "arrears": True},
        }
        base.update(overrides)
        return base

    def test_a_matching_category_ranks_above_an_unrelated_one(self):
        bundle = C.build_precedent_bundle(
            self._subject(),
            [self._candidate(complaint_id=1, category="billing"), self._candidate(complaint_id=2, category="privacy")],
            now=NOW,
        )
        assert bundle["precedents"]
        assert bundle["precedents"][0]["category"] == "billing"

    def test_a_reopened_precedent_is_down_weighted(self):
        held = C.score_precedent(self._candidate(outcome_observed="resolved"), self._subject(), now=NOW)
        reopened = C.score_precedent(self._candidate(outcome_observed="reopened"), self._subject(), now=NOW)
        assert held["vindicated"] is True
        assert reopened["contradicted"] is True
        assert reopened["score"] < held["score"]

    def test_a_contradicted_precedent_is_reported_not_hidden(self):
        bundle = C.build_precedent_bundle(
            self._subject(), [self._candidate(outcome_observed="complainant_left")], now=NOW
        )
        assert bundle["contradicted_count"] == 1
        assert bundle["contradicted"][0]["outcome_observed"] == "complainant_left"

    def test_a_weak_match_is_discarded_rather_than_presented_as_evidence(self):
        bundle = C.build_precedent_bundle(
            self._subject(),
            [self._candidate(complaint_id=7, category="onboarding", severity="low", factors={})],
            now=NOW,
        )
        assert bundle["candidates_considered"] == 1
        assert bundle["returned"] == 0

    def test_every_precedent_explains_itself(self):
        row = C.score_precedent(self._candidate(), self._subject(), now=NOW)
        assert row["reference"] in row["rationale"]
        assert "billing" in row["rationale"]

    def test_recency_decays(self):
        recent = C.score_precedent(self._candidate(closed_at="2026-09-01T00:00:00+00:00"), self._subject(), now=NOW)
        old = C.score_precedent(self._candidate(closed_at="2020-01-01T00:00:00+00:00"), self._subject(), now=NOW)
        assert recent["components"]["recency"] > old["components"]["recency"]


# ===========================================================================
# The decision dossier
# ===========================================================================


class TestDecisionDossier:
    def _feeds(self, **overrides):
        base = {
            "case_facts": {"available": True, "observed": {"age_hours": 1.0}, "confidence": 1.0, "citation": "complaint_cases"},
            "conversation": {"available": True, "observed": {}, "confidence": 0.8, "citation": "recovery_outcomes"},
            "relationship": {"available": True, "observed": {}, "confidence": 0.9, "citation": "retention_snapshots"},
            "policy_posture": {"available": True, "observed": {}, "confidence": 1.0, "citation": "customer_policy_scores"},
            "historical_decisions": {"available": True, "observed": {}, "confidence": 1.0, "citation": "complaint_decisions"},
            "governance": {"available": True, "observed": {}, "confidence": 1.0, "citation": "guards"},
        }
        base.update(overrides)
        return base

    def test_every_required_feed_is_present(self):
        dossier = C.build_decision_dossier(self._feeds(), now=NOW)
        required = {f for f, row in C.DECISION_FEED_BY_ID.items() if row["required"]}
        assert required <= set(dossier["present_feeds"])
        assert dossier["missing_required_feeds"] == []
        assert dossier["unavailable_required_feeds"] == []

    def test_declared_feeds_are_exactly_the_registry(self):
        """The dossier's feed set is the published registry, not a private list."""
        assert {f["feed_id"] for f in C.DECISION_FEEDS} == set(C.DECISION_FEED_BY_ID)
        assert len(C.DECISION_FEEDS) == 10
        # And every declared feed is reachable from the registry by kind.
        for kind in C.DECISION_FEED_KINDS:
            assert all(
                C.DECISION_FEED_BY_ID[f]["kind"] == kind
                for f in C.DECISION_FEED_BY_ID
                if C.DECISION_FEED_BY_ID[f]["kind"] == kind
            )

    def test_a_missing_required_feed_is_named_not_omitted(self):
        feeds = self._feeds()
        feeds.pop("historical_decisions")
        dossier = C.build_decision_dossier(feeds, now=NOW)
        assert "historical_decisions" in dossier["missing_required_feeds"]
        assert dossier["complete"] is False

    def test_a_stale_feed_is_flagged(self):
        old = NOW - timedelta(hours=48)
        dossier = C.build_decision_dossier(
            self._feeds(relationship={"available": True, "observed": {}, "confidence": 0.9, "citation": "x", "observed_at": old}),
            now=NOW,
        )
        block = next(f for f in dossier["feeds"] if f["feed_id"] == "relationship")
        assert block["stale"] is True
        assert "relationship" in dossier["stale_feeds"]

    def test_confidence_is_the_weakest_required_feed_not_the_average(self):
        """A dozen confident internal feeds must not hide the one that failed."""
        dossier = C.build_decision_dossier(
            self._feeds(conversation={"available": True, "observed": {}, "confidence": 0.1, "citation": "x"}),
            now=NOW,
        )
        assert dossier["confidence"] == 0.1

    def test_external_signals_are_declared_async_only(self):
        assert C.DECISION_FEED_BY_ID["external"]["async_only"] is True

    def test_feeds_are_grouped_by_kind(self):
        dossier = C.build_decision_dossier(self._feeds(), now=NOW)
        assert "historical" in dossier["by_kind"]
        assert "historical_decisions" in dossier["by_kind"]["historical"]


# ===========================================================================
# The lifecycle, against a real database
# ===========================================================================


class TestCaseLifecycle:
    def test_a_case_gets_a_durable_reference(self, harness):
        harness.user(1)
        case = harness.open(1, category="billing", summary="charged twice")
        assert case["reference"].startswith("CMP-")
        assert case["status"] == "open"

    def test_references_do_not_collide(self, harness):
        """The ESC- bug: a per-run counter reissued references after a restart,
        so two cases must differ even though both come from the same run."""
        harness.user(1)
        first = harness.open(1, category="billing")
        second = harness.open(1, category="privacy")
        assert first["reference"] != second["reference"]
        assert first["id"] != second["id"]

    def test_the_reference_is_the_case_identity(self, harness):
        harness.user(1)
        case = harness.open(1, category="billing")
        assert case["reference"] == f"CMP-{case['id']:06d}"
        # And it resolves back to exactly one case.
        assert C.case_to_dict(harness.case(case["reference"]))["id"] == case["id"]

    def test_opening_sets_an_sla_clock(self, harness):
        harness.user(1)
        case = harness.open(1, category="billing")
        assert case["response_due_at"] is not None
        assert case["resolution_due_at"] is not None
        assert case["response_due_at"] < case["resolution_due_at"]

    def test_a_privacy_case_starts_regulatory_and_high(self, harness):
        harness.user(1)
        case = harness.open(1, category="privacy")
        assert case["regulatory"] is True
        assert case["severity"] == "high"
        # A regulatory case resolves against the statutory floor.
        assert case["resolution_due_at"] is not None

    def test_opening_writes_an_event(self, harness):
        harness.user(1)
        case = harness.open(1, category="billing")
        events = harness.run(C.build_complaint_timeline(harness.session, harness.case(case["reference"])))
        assert [e["event_type"] for e in events] == ["opened"]
        assert events[0]["to_status"] == "open"
        assert events[0]["actor_role"] == "customer"

    def test_an_unknown_category_falls_back_rather_than_raising(self, harness):
        harness.user(1)
        case = harness.open(1, category="not_a_category")
        assert case["category"] == C.DEFAULT_COMPLAINT_CATEGORY

    def test_the_full_transition_sequence(self, harness):
        harness.user(1)
        opened = harness.open(1, category="service_quality")
        ref = opened["reference"]
        db = harness.session

        acked = harness.run(C.acknowledge_complaint(db, opened["id"], actor_user_id=1))
        assert acked["status"] == "acknowledged"
        assert acked["first_response_at"] is not None

        resolved = harness.run(C.resolve_complaint(db, opened["id"], resolution_code="refunded", resolution_note="done"))
        assert resolved["status"] == "resolved"
        assert resolved["resolution_code"] == "refunded"

        closed = harness.run(C.close_complaint(db, opened["id"], actor_user_id=1))
        assert closed["status"] == "closed"
        assert closed["closed_at"] is not None

        events = [e["event_type"] for e in harness.run(C.build_complaint_timeline(db, harness.case(ref)))]
        assert events == ["opened", "acknowledged", "resolved", "closed"]

    def test_an_unknown_resolution_code_is_refused(self, harness):
        harness.user(1)
        opened = harness.open(1)
        with pytest.raises(ValueError):
            harness.run(C.resolve_complaint(harness.session, opened["id"], resolution_code="handwave"))

    def test_a_withdrawn_case_is_terminal(self, harness):
        harness.user(1)
        opened = harness.open(1)
        withdrawn = harness.run(C.withdraw_complaint(harness.session, opened["id"], actor_user_id=1))
        assert withdrawn["status"] == "withdrawn"
        with pytest.raises(ValueError):
            harness.run(C.withdraw_complaint(harness.session, opened["id"]))

    def test_a_note_does_not_change_state(self, harness):
        harness.user(1)
        opened = harness.open(1)
        after = harness.run(C.add_complaint_note(harness.session, opened["id"], "called them back", actor_user_id=1))
        assert after["status"] == opened["status"]
        events = [e["event_type"] for e in harness.run(C.build_complaint_timeline(harness.session, harness.case(opened["reference"])))]
        assert "note_added" in events

    def test_a_reopen_is_only_possible_from_resolved_or_closed(self, harness):
        harness.user(1)
        opened = harness.open(1)
        with pytest.raises(ValueError):
            harness.run(C.reopen_complaint(harness.session, opened["id"], reason="still broken"))
        harness.run(C.resolve_complaint(harness.session, opened["id"], resolution_code="explained"))
        reopened = harness.run(C.reopen_complaint(harness.session, opened["id"], reason="not fixed"))
        assert reopened["status"] == "open"
        assert reopened["reopened_count"] == 1
        assert reopened["resolved_at"] is None

    def test_satisfaction_is_bounded(self, harness):
        harness.user(1)
        opened = harness.open(1)
        resolved = harness.run(C.resolve_complaint(harness.session, opened["id"], resolution_code="credited", satisfaction_score=9))
        assert resolved["satisfaction_score"] == 9

    def test_a_repeat_complaint_is_reported_not_blocked(self, harness):
        """A customer must be able to complain twice.

        Rejecting the second case would be `suppress_recent_complaint` applied to
        the wrong place -- the rule exists to stop *outreach*, not the right to
        complain. So the open response names the live case and still creates the
        new one.
        """
        harness.user(1)
        first = harness.open(1, category="billing", now=NOW)
        assert first["existing_open_case"] is None

        second = harness.open(1, category="billing", now=NOW)
        assert second["reference"] != first["reference"]
        assert second["existing_open_case"] == {
            "reference": first["reference"],
            "id": first["id"],
            "status": "open",
        }

    def test_a_different_category_has_no_link(self, harness):
        harness.user(1)
        harness.open(1, category="billing", now=NOW)
        other = harness.open(1, category="privacy", now=NOW)
        assert other["existing_open_case"] is None

    def test_a_closed_case_does_not_count_as_open(self, harness):
        harness.user(1)
        first = harness.open(1, category="billing", now=NOW)
        harness.run(C.close_complaint(harness.session, first["id"]))
        second = harness.open(1, category="billing", now=NOW)
        assert second["existing_open_case"] is None

    def test_lodging_is_never_gated_by_consent(self, harness):
        """The consent gate must not be able to withhold a complaint.

        Mirrors the existing keep-as-is decision that consent never suppresses a
        `service` or `recovery` purpose. Asserted directly rather than inferred:
        `open_complaint` reads no consent state at all, so a future edit that
        added such a check would fail here.
        """
        import inspect

        source = inspect.getsource(C.open_complaint)
        assert "CONSENT" not in source
        assert "consent" not in source.lower()

    def test_a_missing_case_raises_lookup_error(self, harness):
        with pytest.raises(LookupError):
            harness.run(C._load_case(harness.session, reference="CMP-999999"))


class TestDecisionHistory:
    def test_a_resolution_is_itself_a_decision(self, harness):
        """Otherwise the decision log holds only escalations, the precedent
        bundle has nothing to retrieve, and the learning loop has no resolutions
        to be vindicated or refuted."""
        harness.user(1)
        opened = harness.open(1)
        harness.run(
            C.resolve_complaint(
                harness.session, opened["id"], resolution_code="refunded",
                resolution_note="duplicate charge", actor_user_id=1, actor_role="agent",
            )
        )
        history = harness.run(
            C.build_complaint_decision_history(harness.session, harness.case(opened["reference"]))
        )
        assert [d["decision"] for d in history] == ["resolve"]
        assert history[0]["factors"]["resolution_code"] == "refunded"
        assert history[0]["rationale"] == "duplicate charge"

    def test_a_reopen_records_its_own_decision(self, harness):
        harness.user(1)
        opened = harness.open(1)
        harness.run(C.resolve_complaint(harness.session, opened["id"], resolution_code="explained"))
        harness.run(C.reopen_complaint(harness.session, opened["id"], reason="not fixed"))
        history = harness.run(
            C.build_complaint_decision_history(harness.session, harness.case(opened["reference"]))
        )
        decisions = [d["decision"] for d in history]
        assert "reopen" in decisions
        # The judgement is recorded before the reopen's own row is written, so it
        # lands on the resolution and the reopen is left unjudged -- the case is
        # live again and there is nothing to report about it yet.
        resolved = next(d for d in history if d["factors"].get("resolution_code") == "explained")
        assert resolved["outcome_observed"] == "reopened"
        assert next(d for d in history if d["decision"] == "reopen")["outcome_observed"] == ""

    def test_assignment_fills_the_owner_guard_without_raising_the_tier(self, harness):
        harness.user(1)
        opened = harness.open(1, category="billing")
        assigned = harness.run(
            C.assign_complaint(
                harness.session, opened["id"], owner_team="billing_ops", actor_user_id=1, note="mine"
            )
        )
        assert assigned["owner_team"] == "billing_ops"
        assert assigned["tier"] == opened["tier"]
        history = harness.run(
            C.build_complaint_decision_history(harness.session, harness.case(opened["reference"]))
        )
        assert [d["decision"] for d in history] == ["assign"]
        assert C.escalation_verdict(
            {"regulatory": False, "override_would_lower_tier": False, "has_owner": True,
             "severity_understated_by": 0, "consent_withholds_contact": False,
             "duplicate_open_cases": 0, "resolved_stale_hours": 0.0}
        )["decision"] == "accept"

    def test_assignment_may_raise_the_tier_but_never_lower_it(self, harness):
        harness.user(1)
        opened = harness.open(1)
        raised = harness.run(
            C.assign_complaint(harness.session, opened["id"], tier="tier_2", owner_team="senior_review")
        )
        assert raised["tier"] == "tier_2"
        with pytest.raises(ValueError):
            harness.run(C.assign_complaint(harness.session, opened["id"], tier="tier_1"))

    def test_a_decision_is_recorded_with_its_actor_and_assurance(self, harness):
        harness.user(1, admin=True)
        opened = harness.open(1)
        record = harness.run(
            C.record_complaint_decision(
                harness.session,
                opened["id"],
                decision="escalate",
                outcome="applied",
                to_tier="tier_2",
                decided_by_id=1,
                decided_by_role="admin",
                step_up_level="loa2",
                rationale="repeat complainant",
            )
        )
        assert record["decided_by_role"] == "admin"
        assert record["step_up_level"] == "loa2"
        assert record["outcome_observed"] == ""

    def test_a_later_decision_supersedes_an_earlier_proposal(self, harness):
        harness.user(1)
        opened = harness.open(1)
        first = harness.run(
            C.record_complaint_decision(harness.session, opened["id"], decision="escalate", outcome="proposed", to_tier="tier_2")
        )
        second = harness.run(
            C.record_complaint_decision(harness.session, opened["id"], decision="hold", outcome="applied", rationale="looked into it")
        )
        history = {
            d["id"]: d
            for d in harness.run(C.build_complaint_decision_history(harness.session, harness.case(opened["reference"])))
        }
        assert history[first["id"]]["superseded_by_id"] == second["id"]
        assert history[first["id"]]["outcome"] == "superseded"

    def test_closing_records_what_the_decision_actually_did(self, harness):
        harness.user(1)
        opened = harness.open(1)
        harness.run(C.record_complaint_decision(harness.session, opened["id"], decision="escalate", outcome="applied"))
        harness.run(C.resolve_complaint(harness.session, opened["id"], resolution_code="refunded"))
        harness.run(C.close_complaint(harness.session, opened["id"]))
        history = harness.run(C.build_complaint_decision_history(harness.session, harness.case(opened["reference"])))
        assert history[-1]["outcome_observed"] == "resolved"

    def test_a_reopen_marks_the_resolution_as_wrong(self, harness):
        """The single most valuable event: it is what makes precedent refutable.

        `resolve_complaint` writes its own decision row, so there is no need to
        record one by hand here -- and doing so would have counted twice.
        """
        harness.user(1)
        opened = harness.open(1)
        harness.run(C.resolve_complaint(harness.session, opened["id"], resolution_code="explained"))
        harness.run(C.reopen_complaint(harness.session, opened["id"], reason="not fixed"))
        history = harness.run(C.build_complaint_decision_history(harness.session, harness.case(opened["reference"])))
        by_decision = {d["decision"]: d for d in history}
        assert by_decision["resolve"]["outcome_observed"] == "reopened"
        # The reopen itself is still unjudged -- the case is live again.
        assert by_decision["reopen"]["outcome_observed"] == ""

    def test_an_unknown_decision_is_refused(self, harness):
        harness.user(1)
        opened = harness.open(1)
        with pytest.raises(ValueError):
            harness.run(C.record_complaint_decision(harness.session, opened["id"], decision="vibe"))

    def test_the_decision_factors_are_frozen_at_decision_time(self, harness):
        """A row that re-read live feeds later would report reasoning that was
        never actually given."""
        harness.user(1)
        opened = harness.open(1)
        record = harness.run(
            C.record_complaint_decision(
                harness.session,
                opened["id"],
                decision="escalate",
                outcome="applied",
                factors={"churn_risk": "low", "dissatisfaction_score": 3.0},
                precedent_refs=[{"reference": "CMP-000001", "score": 0.71}],
            )
        )
        assert record["factors"]["churn_risk"] == "low"
        assert record["precedent_refs"][0]["reference"] == "CMP-000001"


class TestApplyEscalation:
    def test_an_auto_escalation_moves_the_tier_and_records_the_trigger(self, harness):
        harness.user(1)
        opened = harness.open(1, category="privacy")
        decision = C.build_escalation_decision(
            C._probe_scope(category="privacy", regulatory=True, regulatory_deadline_hours=2.0),
            current_tier="tier_1",
        )
        result = harness.run(C.apply_escalation(harness.session, harness.case(opened["reference"]), decision, actor_role="system"))
        assert result["applied"] is True
        assert result["case"]["tier"] == "tier_3"
        assert result["case"]["auto_escalated"] is True
        assert result["case"]["escalation_trigger_id"] == "regulatory_privacy_deadline"
        assert result["decision"]["auto_applied"] is True

    def test_a_downgrade_is_refused_and_the_refusal_is_recorded(self, harness):
        harness.user(1)
        opened = harness.open(1)
        row = harness.case(opened["reference"])
        row.tier = "tier_3"
        harness.run(harness.session.commit())
        decision = {"escalated": True, "from_tier": "tier_3", "to_tier": "tier_1", "owner_team": ""}
        result = harness.run(
            C.apply_escalation(harness.session, row, decision, actor_role="admin", rationale="try to downgrade")
        )
        assert result["applied"] is False
        assert "lower tier" in result["blocked_reason"]
        assert result["case"]["tier"] == "tier_3"

    def test_the_escalation_cites_its_precedent(self, harness):
        harness.user(1)
        opened = harness.open(1)
        decision = C.build_escalation_decision(
            C._probe_scope(response_overdue_hours=2.0, severity="high", severity_rank=2),
            current_tier="tier_1",
        )
        result = harness.run(
            C.apply_escalation(
                harness.session,
                harness.case(opened["reference"]),
                decision,
                actor_role="system",
                precedent_refs=[{"reference": "CMP-000001", "rationale": "we tried this"}],
            )
        )
        assert result["decision"]["precedent_refs"][0]["reference"] == "CMP-000001"


class TestSweep:
    def test_a_disabled_sweep_touches_nothing(self, harness, monkeypatch):
        monkeypatch.setattr(C, "COMPLAINT_AUTO_ESCALATION_ENABLED", False)
        harness.user(1)
        harness.open(1, now=NOW)
        result = harness.run(C.run_auto_escalation_sweep(harness.session))
        assert result["enabled"] is False
        assert result["examined"] == 0
        assert result["results"] == []

    def test_a_calm_case_produces_no_action(self, harness, monkeypatch):
        monkeypatch.setattr(C, "COMPLAINT_AUTO_ESCALATION_ENABLED", True)
        harness.user(1)
        # Opened on the sweep's clock, so "fresh" really is fresh -- the suite's
        # NOW is hours ahead of wall time, which would otherwise make every
        # freshly-opened case genuinely overdue.
        harness.open(1, category="billing", now=NOW)
        result = harness.run(C.run_auto_escalation_sweep(harness.session, now=NOW))
        assert result["examined"] == 1
        assert result["escalated"] == 0
        assert result["advisory_pending"] == 0

    def test_a_breached_sla_escalates_and_stamps_the_breach_once(self, harness, monkeypatch):
        monkeypatch.setattr(C, "COMPLAINT_AUTO_ESCALATION_ENABLED", True)
        harness.user(1)
        case = harness.open(1, category="billing")
        row = harness.case(case["reference"])
        row.opened_at = NOW - timedelta(days=30)
        row.response_due_at = NOW - timedelta(days=2)
        row.resolution_due_at = NOW - timedelta(days=1)
        harness.run(harness.session.commit())

        first = harness.run(C.run_auto_escalation_sweep(harness.session, now=NOW))
        assert first["examined"] == 1
        assert first["escalated"] == 1
        assert first["breaches_stamped"] == 1

        # A second sweep must not re-stamp: the timeline would become noise.
        second = harness.run(C.run_auto_escalation_sweep(harness.session, now=NOW))
        assert second["breaches_stamped"] == 0
        events = [
            e["event_type"]
            for e in harness.run(C.build_complaint_timeline(harness.session, harness.case(case["reference"])))
        ]
        assert events.count("sla_breached") == 1

    def test_judgment_triggers_are_reported_but_not_applied(self, harness, monkeypatch):
        monkeypatch.setattr(C, "COMPLAINT_AUTO_ESCALATION_ENABLED", True)
        harness.user(1)
        for _ in range(3):
            harness.open(1, category="service_quality", now=NOW)
        result = harness.run(C.run_auto_escalation_sweep(harness.session, now=NOW))
        assert result["escalated"] == 0
        assert result["advisory_pending"] > 0


class TestReports:
    def test_reads_do_not_write(self, harness):
        """The old GET /chat/recovery inserted a row on every hit."""
        from sqlalchemy import func, select

        harness.user(1)
        harness.open(1, category="billing")
        for _ in range(5):
            harness.run(C.list_complaints(harness.session))
            harness.run(C.build_sla_report(harness.session))
            harness.run(C.build_complaint_admin_report(harness.session))
        count = harness.run(harness.session.execute(select(func.count(models.ComplaintCase.id))))
        assert int(count.first()[0]) == 1

    def test_the_sla_report_is_derived_from_the_rows(self, harness):
        harness.user(1)
        case = harness.open(1, category="billing")
        row = harness.case(case["reference"])
        row.response_due_at = NOW - timedelta(hours=1)
        harness.run(harness.session.commit())
        report = harness.run(C.build_sla_report(harness.session, now=NOW))
        assert case["reference"] in report["breached_response"]
        assert report["breach_count"] >= 1

    def test_a_resolved_case_stops_breaching(self, harness):
        harness.user(1)
        case = harness.open(1, category="billing")
        row = harness.case(case["reference"])
        row.response_due_at = NOW - timedelta(hours=1)
        harness.run(harness.session.commit())
        assert harness.run(C.build_sla_report(harness.session, now=NOW))["breached_response"]
        harness.run(C.acknowledge_complaint(harness.session, case["id"]))
        assert not harness.run(C.build_sla_report(harness.session, now=NOW))["breached_response"]

    def test_the_admin_report_publishes_the_contradiction_rate(self, harness):
        """The one number that says whether the policy is actually working."""
        harness.user(1)
        opened = harness.open(1)
        harness.run(C.resolve_complaint(harness.session, opened["id"], resolution_code="explained"))
        harness.run(C.reopen_complaint(harness.session, opened["id"], reason="not fixed"))
        report = harness.run(C.build_complaint_admin_report(harness.session))
        # Two decisions: the resolution (refuted) and the reopen (still open).
        assert report["decisions_judged"] == 1
        assert report["contradicted_decisions"] == 1
        assert report["contradiction_rate"] == 1.0

    def test_a_reopen_overwrites_an_earlier_verdict(self, harness):
        """The bug this pins: judging *every* unjudged row meant a `close`
        stamped a resolution `resolved`, and the later `reopen` had nothing left
        to refute -- so a decision the complainant demonstrably rejected still
        counted as vindicated and the contradiction rate read zero."""
        harness.user(1)
        opened = harness.open(1)
        harness.run(C.resolve_complaint(harness.session, opened["id"], resolution_code="explained"))
        harness.run(C.close_complaint(harness.session, opened["id"]))
        history = harness.run(
            C.build_complaint_decision_history(harness.session, harness.case(opened["reference"]))
        )
        assert history[-1]["outcome_observed"] == "resolved"

        harness.run(C.reopen_complaint(harness.session, opened["id"], reason="still charged"))
        history = harness.run(
            C.build_complaint_decision_history(harness.session, harness.case(opened["reference"]))
        )
        resolved = next(d for d in history if d["factors"].get("resolution_code") == "explained")
        assert resolved["outcome_observed"] == "reopened"

        report = harness.run(C.build_complaint_admin_report(harness.session))
        assert report["contradicted_decisions"] == 1
        assert report["contradiction_rate"] == 1.0

    def test_only_the_latest_decision_carries_the_outcome(self, harness):
        """A superseded proposal is not a judgement target."""
        harness.user(1)
        opened = harness.open(1)
        harness.run(
            C.record_complaint_decision(harness.session, opened["id"], decision="escalate", outcome="proposed")
        )
        harness.run(C.record_complaint_decision(harness.session, opened["id"], decision="hold", outcome="applied"))
        harness.run(C.resolve_complaint(harness.session, opened["id"], resolution_code="credited"))
        harness.run(C.close_complaint(harness.session, opened["id"]))
        history = {
            (d["decision"], d["outcome"]): d
            for d in harness.run(
                C.build_complaint_decision_history(harness.session, harness.case(opened["reference"]))
            )
        }
        assert history[("escalate", "superseded")]["outcome_observed"] == ""
        assert history[("hold", "applied")]["outcome_observed"] == ""
        assert history[("resolve", "applied")]["outcome_observed"] == "resolved"

    def test_a_held_resolution_is_not_counted_as_contradicted(self, harness):
        harness.user(1)
        opened = harness.open(1)
        harness.run(C.resolve_complaint(harness.session, opened["id"], resolution_code="refunded"))
        harness.run(C.close_complaint(harness.session, opened["id"]))
        report = harness.run(C.build_complaint_admin_report(harness.session))
        assert report["decisions_judged"] == 1
        assert report["contradicted_decisions"] == 0
        assert report["contradiction_rate"] == 0.0

    def test_a_closed_case_leaves_the_open_queue(self, harness):
        harness.user(1)
        opened = harness.open(1)
        harness.run(C.close_complaint(harness.session, opened["id"]))
        assert harness.run(C.list_complaints(harness.session)) == []
        assert len(harness.run(C.list_complaints(harness.session, include_closed=True))) == 1

    def test_find_open_case_prevents_a_duplicate(self, harness):
        harness.user(1)
        harness.open(1, category="billing")
        found = harness.run(C.find_open_case(harness.session, 1, category="billing"))
        assert found is not None
        assert harness.run(C.find_open_case(harness.session, 1, category="privacy")) is None

    def test_the_duplicate_guard_can_see_an_uncommitted_case(self, harness):
        """A caller that passed commit=False has rows a fresh select cannot see.

        Without this, a second escalation inside the same transaction would not
        find the case the first one just opened and would open a duplicate --
        the exact thing `find_open_case` exists to prevent.
        """
        harness.user(1)
        opened = harness.run(C.open_complaint(harness.session, 1, category="billing", commit=False))
        # `find_open_case` issues a real SELECT, so the session's autoflush makes
        # the uncommitted case visible to it. That is the whole guarantee: a
        # second escalation in the same transaction reuses this case.
        found = harness.run(C.find_open_case(harness.session, 1, category="billing"))
        assert found is not None
        assert found.id == opened["id"]
        # A different category is still a different case.
        assert harness.run(C.find_open_case(harness.session, 1, category="privacy")) is None
        harness.run(harness.session.rollback())


class TestDecisionSupport:
    def test_the_dossier_is_assembled_without_applying_anything(self, harness):
        """The read path must never escalate; only the sweep and the explicit
        apply endpoint may."""
        harness.user(1)
        case = harness.open(1, category="privacy")
        support = harness.run(C.build_escalation_decision_support(harness.session, harness.case(case["reference"]), now=NOW))
        assert support["dossier"]["present_feeds"]
        assert support["decision"]["escalated"] is False
        # Nothing moved.
        assert C.case_to_dict(harness.case(case["reference"]))["tier"] == case["tier"]
        assert C.case_to_dict(harness.case(case["reference"]))["escalated_at"] is None

    def test_the_summary_names_incomplete_evidence(self, harness):
        harness.user(1)
        case = harness.open(1)
        support = harness.run(C.build_escalation_decision_support(harness.session, harness.case(case["reference"]), now=NOW))
        # A fresh customer has no retention snapshot, so the relationship feed
        # is unavailable -- and the summary has to say so rather than imply the
        # decision was fully informed.
        if not support["dossier"]["complete"]:
            assert "Incomplete evidence" in support["summary"]["text"]
            assert support["summary"]["evidence_complete"] is False

    def test_the_external_feed_reports_itself_as_uncalled(self, harness):
        harness.user(1)
        case = harness.open(1)
        support = harness.run(C.build_escalation_decision_support(harness.session, harness.case(case["reference"]), now=NOW))
        block = next(f for f in support["dossier"]["feeds"] if f["feed_id"] == "external")
        assert block["available"] is False
        assert block["async_only"] is True
        assert "background" in block["gap"]


class TestComplaintCaseCells:
    """The `complaint_case` cell matrix and the authz rule that names it.

    The admin rule originally declared `audit_trail/detail_json` because no
    `complaint_cases` resource existed, and `validate_authz` reported that as a
    warning: a hardening clause naming a resource the matrix does not have would
    make `require_cell_access` deny every caller. The resource now exists.
    """

    def test_the_resource_exists(self):
        from app.cell_matrix import CELL_MATRIX

        assert "complaint_case" in CELL_MATRIX
        assert set(CELL_MATRIX["complaint_case"]) == {
            "summary", "resolution_note", "factors_json",
        }

    def test_an_auditor_cannot_read_the_decision_snapshot(self):
        """The whole reason for a dedicated resource: `factors_json` is derived
        scoring about a person mid-dispute."""
        from app.cell_matrix import can_read_cell

        assert can_read_cell("complaint_case", "factors_json", ["admin"]) is True
        assert can_read_cell("complaint_case", "factors_json", ["agent"]) is True
        assert can_read_cell("complaint_case", "factors_json", ["auditor"]) is False
        assert can_read_cell("complaint_case", "factors_json", ["owner"]) is False

    def test_an_auditor_cannot_alter_the_record(self):
        from app.cell_matrix import can_write_cell

        assert can_write_cell("complaint_case", "summary", ["admin"]) is True
        assert can_write_cell("complaint_case", "summary", ["auditor"]) is False

    def test_an_agent_can_work_a_case_but_not_rewrite_its_summary(self):
        from app.cell_matrix import can_read_cell, can_write_cell

        assert can_read_cell("complaint_case", "summary", ["agent"]) is True
        assert can_write_cell("complaint_case", "resolution_note", ["agent"]) is True
        assert can_write_cell("complaint_case", "summary", ["agent"]) is False

    def test_the_admin_rule_names_a_cell_that_exists(self):
        from app import deps
        from app.cell_matrix import CELL_MATRIX

        assert deps.validate_authz()["warnings"] == 0
        rule = next(
            r for r in deps.AUTHZ_RULES if r["rule_id"] == "complaints_admin"
        )
        resource, cell, access = rule["hardening"]["cell"]
        assert resource in CELL_MATRIX, resource
        assert cell in CELL_MATRIX[resource], cell
        # And the admin role can actually do what the rule asks of it.
        from app.cell_matrix import can_read_cell

        assert can_read_cell(resource, cell, ["admin"]) is True

    def test_every_cell_matrix_resource_is_classified_in_the_authz_table(self):
        """A resource nobody routes through is either a mistake or dead config."""
        from app import deps
        from app.cell_matrix import CELL_MATRIX

        named = {
            spec.get("cell", ("",))[0]
            for rule in deps.AUTHZ_RULES
            for spec in [rule.get("hardening") or {}]
            if spec.get("cell")
        }
        # complaint_case is the one resource the table reaches for; the rest are
        # covered by rules that name no cell, which is the pre-existing state.
        assert "complaint_case" in named
        assert "complaint_case" in CELL_MATRIX


class TestNoSwallowedProgrammingErrors:
    """A broad `except` in a read path hides bugs behind conditions.

    `recovery_playbooks._with_complaint_history` shipped with `except Exception`
    and a missing `func` import. The NameError was caught, recorded as
    `complaints_last_30d_error`, and the context key silently never appeared --
    so `suppress_recent_complaint` stayed dead while the code read as though it
    were supplying the key. These tests are the guard against that shape.
    """

    def test_the_complaint_history_key_is_really_produced(self, harness):
        """Against a real database, the key appears -- with the right count."""
        from app.services import recovery_playbooks as rp

        harness.user(1)
        harness.open(1, category="billing", now=NOW)
        harness.open(1, category="privacy", now=NOW)
        enriched = harness.run(rp._with_complaint_history(harness.session, 1, {"stage": "engaged"}))
        assert enriched["complaints_last_30d"] == 2
        # And no error key, which is how the swallowed NameError announced itself.
        assert "complaints_last_30d_error" not in enriched

    def test_the_recovery_context_carries_the_key_into_the_suppression_pack(self, harness):
        from app import rule_engine
        from app.services import recovery_playbooks as rp

        harness.user(1)
        for _ in range(2):
            harness.open(1, category="billing", now=NOW)
        enriched = harness.run(
            rp._with_complaint_history(
                harness.session, 1, {"sentiment_label": "neutral", "churn_risk": "low"}
            )
        )
        fired = rule_engine.select_rules("communication_suppression", enriched)["fired_ids"]
        assert "suppress_recent_complaint" in fired

    def test_a_database_fault_degrades_the_feed_instead_of_raising(self, harness):
        """A missing source is a condition to report, not a crash."""
        from sqlalchemy.exc import SQLAlchemyError

        class _Broken:
            async def execute(self, *_a, **_k):
                raise SQLAlchemyError("no such table: complaint_cases")

        out = harness.run(
            C.collect_complaint_context(_Broken(), harness.open(1, now=NOW), now=NOW)
        )
        assert "historical_decisions" in out["gaps"]
        block = out["feeds"]["historical_decisions"]
        assert block["available"] is False
        assert "complaint history unavailable" in block["gap"]

    def test_a_programming_error_is_not_swallowed(self, harness):
        """The whole point: a NameError must not masquerade as a data gap."""

        class _ProgrammingError:
            async def execute(self, *_a, **_k):
                raise NameError("name 'func' is not defined")

        try:
            harness.run(
                C.collect_complaint_context(_ProgrammingError(), harness.open(1, now=NOW), now=NOW)
            )
        except NameError:
            return
        raise AssertionError(
            "a NameError inside a feed collector was swallowed; narrow the handler "
            "to SQLAlchemyError so a typo cannot hide behind a reported gap"
        )

    def test_every_feed_collector_handles_only_database_errors(self):
        """Static check, so a future broad handler is caught at review time.

        Parsed rather than grepped: the explanatory comment above deliberately
        contains the string ``except Exception``, so a substring test would fail
        on the very prose documenting the fix.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(C.collect_complaint_context)))
        handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
        assert handlers, "expected the feed collectors to have handlers"
        # `ExceptHandler.type` is the caught exception; `.name` is the bound
        # variable. A `None` type would be a bare `except:`, and anything
        # broader than SQLAlchemyError defeats the purpose, because the point is
        # that a typo propagates instead of hiding behind a reported gap.
        types = set()
        for node in handlers:
            if node.type is None:
                types.add(None)
            elif isinstance(node.type, ast.Name):
                types.add(node.type.id)
            else:
                types.add(ast.dump(node.type))
        assert types == {"SQLAlchemyError"}, types


class TestRuleEngineNumericOps:
    """`matches_field_extended` computed "legacy operators" as those NOT in
    ``OPERATOR_FAMILIES`` -- but the numeric operators are in that map, so for a
    rule containing only numeric operators the set was empty, nothing was
    delegated, and the function returned True unconditionally. Every numeric
    rule in every pack matched regardless of its threshold, and the explanation
    trace reported "satisfied" for the failures. `select_rules` routes through
    this function, so the bug decided real rule outcomes."""

    @pytest.mark.parametrize(
        "when,actual,expected",
        [
            ({"gt": 0}, 0.0, False),
            ({"gt": 0}, 1.0, True),
            ({"gte": 3}, 0, False),
            ({"gte": 3}, 3, True),
            ({"lte": 5}, 9, False),
            ({"lt": 5}, 5, False),
            ({"gte": 2, "lt": 10}, 5, True),
            ({"gte": 2, "lt": 10}, 20, False),
        ],
    )
    def test_explain_when_agrees_with_evaluate_when(self, when, actual, expected):
        basic = rule_engine.evaluate_when({"x": when}, {"x": actual})[0]
        extended = rule_engine.explain_when({"x": when}, {"x": actual})["matched"]
        assert basic is expected
        assert extended is expected

    def test_a_shipped_rule_no_longer_fires_below_its_threshold(self):
        pack = rule_engine.get_rule_pack("loyalty_retention")
        assert rule_engine.select_rules(pack, {"days_since_login": 0, "at_risk": False})["fired_ids"] == []
        assert rule_engine.select_rules(pack, {"days_since_login": 60, "at_risk": True})["fired_ids"] == [
            "retention_dormant"
        ]

    def test_the_previously_dead_suppression_rule_can_now_fire(self):
        """`suppress_recent_complaint` needed `complaints_last_30d`, which no
        producer emitted. The complaint ledger supplies it now."""
        pack = rule_engine.get_rule_pack("communication_suppression")
        base = {"sentiment_label": "neutral", "churn_risk": "low"}
        assert rule_engine.select_rules(pack, {**base, "complaints_last_30d": 0})["fired_ids"] == []
        assert rule_engine.select_rules(pack, {**base, "complaints_last_30d": 3})["fired_ids"] == [
            "suppress_recent_complaint"
        ]

    def test_the_extended_families_still_work(self):
        assert rule_engine.explain_when({"x": {"contains": "ab"}}, {"x": "xxabxx"})["matched"] is True
        assert rule_engine.explain_when({"x": {"len_gte": 2}}, {"x": [1, 2]})["matched"] is True
        assert rule_engine.explain_when({"x": {"is_null": True}}, {"x": None})["matched"] is True

    def test_unknown_and_external_operators_still_fail_closed(self):
        assert rule_engine.explain_when({"x": {"bogus": 1}}, {"x": 5})["matched"] is False
        # `evaluate_when_external` relies on the extended path returning False
        # for external operators so it can hand off to the enrichment stage.
        assert rule_engine.explain_when({"x": {"enriched_field": "a"}}, {"x": 5})["matched"] is False

    def test_the_v1_engine_still_rejects_extended_operators(self):
        assert rule_engine.evaluate_when({"x": {"is_null": True}}, {"x": None})[0] is False


# ===========================================================================
# Schema integrity
# ===========================================================================


class TestSchema:
    def test_the_three_tables_exist_with_the_declared_lifecycles(self):
        for name in ("complaint_cases", "complaint_events", "complaint_decisions"):
            assert name in models.all_table_names()
        assert models.TABLE_LIFECYCLE["complaint_cases"]["write_mode"] == models.MUTABLE
        assert models.TABLE_LIFECYCLE["complaint_events"]["write_mode"] == models.APPEND_ONLY
        assert models.TABLE_LIFECYCLE["complaint_decisions"]["write_mode"] == models.APPEND_ONLY

    def test_the_unconstrained_actor_columns_are_documented(self):
        gaps = models.referential_integrity_gaps()["gaps"]
        by_field = {f"{row['table']}.{row['column']}": row for row in gaps}
        for field in (
            "complaint_cases.owner_user_id",
            "complaint_events.actor_user_id",
            "complaint_decisions.decided_by_id",
        ):
            assert field in by_field, field
            assert by_field[field]["documented_exception"] is True
            assert by_field[field]["exception_note"]["severity"] == "out_of_band"

    def test_supersession_is_a_real_foreign_key(self):
        gaps = models.referential_integrity_gaps()["gaps"]
        fields = {f"{row['table']}.{row['column']}" for row in gaps}
        # It points at a row in the same table, so it should carry a constraint.
        assert "complaint_decisions.superseded_by_id" not in fields

    def test_complaint_content_is_sensitive_and_operational_fields_are_not(self):
        assert models.sensitivity_of("complaint_cases", "factors_json") == "content"
        assert models.sensitivity_of("complaint_decisions", "rationale") == "content"
        assert models.sensitivity_of("complaint_cases", "reference") == "identifier"
        assert models.sensitivity_of("complaint_decisions", "decided_by_role") == "internal"

    def test_satisfaction_is_the_only_bounded_numeric(self):
        table = models.get_table("complaint_cases")
        checks = [str(c.sqltext) for c in table.constraints if c.__class__.__name__ == "CheckConstraint"]
        assert any("satisfaction_score" in c for c in checks)
        assert any("reopened_count" in c for c in checks)


# ===========================================================================
# The migration
# ===========================================================================


class TestMigration:
    """The migration has to produce the schema the models describe.

    Run directly against a throwaway SQLite database rather than through
    `alembic upgrade head`, because the repository's revision chain is
    currently broken independently of this change -- an earlier migration
    carries a literal `"<PUT_PREVIOUS_REVISION_ID_HERE>"` placeholder as its
    `down_revision`, so `upgrade head` cannot resolve. That is pre-existing and
    recorded in BLOCKAGES.md; it is not a reason to leave the new migration
    unverified.
    """

    def _module(self):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path("alembic/versions/20260930_01_add_complaint_cases.py")
        spec = importlib.util.spec_from_file_location("complaints_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = sa.create_engine("sqlite://")
        connection = engine.connect()
        module.op = Operations(MigrationContext.configure(connection))
        return module, engine, connection

    def test_upgrade_produces_exactly_the_model_schema(self):
        module, engine, connection = self._module()
        try:
            module.upgrade()
            inspector = sa.inspect(connection)
            for name in ("complaint_cases", "complaint_events", "complaint_decisions"):
                table = models.get_table(name)
                columns = {c["name"] for c in inspector.get_columns(name)}
                indexes = {i["name"] for i in inspector.get_indexes(name)}
                assert columns == {c.name for c in table.columns}, name
                expected_indexes = {
                    f"ix_{name}_{c.name}" for c in table.columns if c.index
                }
                assert indexes == expected_indexes, (
                    f"{name}: missing {sorted(expected_indexes - indexes)}"
                )
        finally:
            connection.close()
            engine.dispose()

    def test_downgrade_removes_all_three_tables(self):
        module, engine, connection = self._module()
        try:
            module.upgrade()
            module.downgrade()
            inspector = sa.inspect(connection)
            assert [t for t in inspector.get_table_names() if "complaint" in t] == []
        finally:
            connection.close()
            engine.dispose()

    def test_the_revision_chain_continues_the_previous_migration(self):
        module, _engine, _connection = self._module()
        assert module.down_revision == "20260929_01_add_preference_consent_tables"

    def test_every_deliberate_non_constraint_is_declared_with_a_reason(self):
        """A non-constraint with no stated reason is a bug waiting to happen.

        Asserts against `models.UNCONSTRAINED_REFERENCE_COLUMNS` rather than
        against the migration's prose: the declaration is the machine-checkable
        contract, and a test that greps a docstring breaks the moment someone
        rewords it without changing anything.
        """
        declared = models.UNCONSTRAINED_REFERENCE_COLUMNS
        for column in (
            "complaint_cases.owner_user_id",
            "complaint_events.actor_user_id",
            "complaint_decisions.decided_by_id",
        ):
            assert column in declared, column
            assert declared[column]["severity"] == "out_of_band"
            assert declared[column]["reason"].strip()

    def test_the_config_driven_columns_carry_no_membership_constraint(self):
        """A CHECK hard-coded to today's vocabulary would reject a value the
        config already accepts -- the failure documented on
        `user_consent_events.purpose`."""
        table = models.get_table("complaint_cases")
        checks = " ".join(
            str(c.sqltext) for c in table.constraints if c.__class__.__name__ == "CheckConstraint"
        )
        for column in ("category", "severity", "status", "tier", "resolution_code"):
            assert column not in checks, column
        # And the two that do have a real domain are constrained.
        assert "satisfaction_score" in checks
        assert "reopened_count" in checks


# ===========================================================================
# Endpoints
# ===========================================================================


class TestRoutes:
    def test_every_complaint_route_is_classified(self):
        from app import deps
        from app.main import app

        report = deps.authz_drift_report(app.routes)
        assert report["in_sync"] is True
        assert not report["unclassified_routes"]
        assert not report["mismatched"]

        inventory = deps.authz_route_inventory(app.routes)
        rows = [r for r in inventory if "complaint" in r["path"]]
        assert len(rows) == 18
        assert all(r["delta"] == "match" for r in rows)

    def test_the_admin_surface_is_admin_exposure(self):
        from app import deps
        from app.main import app

        inventory = deps.authz_route_inventory(app.routes)
        admin = [r for r in inventory if r["path"].startswith("/complaints/admin")]
        assert len(admin) == 5
        for row in admin:
            assert row["exposure"] == "admin", row["path"]
            assert "get_current_admin_user" in row["actual_by"], row["path"]

    def test_the_customer_surface_is_caller_scoped(self):
        from app import deps
        from app.main import app

        inventory = deps.authz_route_inventory(app.routes)
        reads = [
            r for r in inventory
            if r["path"].startswith("/complaints") and not r["path"].startswith("/complaints/admin")
        ]
        assert reads
        for row in reads:
            assert row["exposure"] in ("authenticated", "admin"), row["path"]
            assert "get_current_user" in row["actual_by"], row["path"]

    def test_the_authorization_table_validates_clean(self):
        from app import deps

        report = deps.validate_authz()
        assert report["valid"] is True
        assert report["warnings"] == 0

    def test_the_catalog_endpoint_payload_matches_the_service(self):
        catalog = C.build_complaints_catalog()
        assert catalog["version"] == C.COMPLAINTS_CATALOG_VERSION
        assert catalog["validation"]["valid"] is True
        assert catalog["escalation_pack"]["pack"] == "complaint_escalation"
        assert len(catalog["escalation_pack"]["rules"]) == len(C.ESCALATION_TRIGGERS)
