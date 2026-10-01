"""Stage F tests: policy motion, value evolution, trust under continuous change.

The theme is the difference between a **record** and an **intention**. Every test
here asks whether the module can be made to *report* something inconvenient, and
several of them assert on the reporting rather than the outcome — because a
governance module that can only report good news is not governance.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services import loyalty_status, policy_motion

STATUS_RULE = "app.services.loyalty_status.LOYALTY_STATUS_RULES"
GENEROSITY_RULES = "app.services.customer_offers.OFFER_GENEROSITY_RULES"
REGIONS = "app.services.region_windows.REGIONS"


# ---------------------------------------------------------------------------
# Item 6 -- the motion ledger
# ---------------------------------------------------------------------------


def test_motion_and_trust_tables_validate():
    assert policy_motion.validate_policy_motion()["valid"]
    assert policy_motion.validate_trust_continuity()["valid"]


def test_classification_follows_the_table_not_a_naming_convention():
    assert policy_motion.classify_motion(REGIONS)["class_id"] == "coverage_tuning"
    assert (
        policy_motion.classify_motion(STATUS_RULE)["class_id"] == "status_rule_change"
    )
    assert (
        policy_motion.classify_motion(GENEROSITY_RULES)["class_id"]
        == "entitlement_change"
    )


def test_an_unclassifiable_change_is_treated_as_the_strictest_class():
    """The assumption that fails safely is 'this may be something we owe'."""
    weak = policy_motion.build_motion(
        target="a_table_nobody_declared",
        evidence=[{"kind": "observation", "samples": 10_000}],
        reason="we felt like it",
        proposed_by="someone",
    )
    assert weak["class_resolved"] is False
    assert weak["class_id"] == "unknown"
    # Measured against the strictest class as a floor, and it does not clear it.
    assert weak["verdict"] == "insufficient"
    # And it must outrank every declared class, not sit below them.
    strictest = max(int(row["rank"]) for row in policy_motion.POLICY_MOTION_CLASSES)
    assert int(weak["rank"]) > strictest

    # Clearing that floor is still not a verdict: `unknown`, not `sufficient`,
    # because nobody has said what the table governs.
    strong = policy_motion.build_motion(
        target="a_table_nobody_declared",
        evidence=[{"kind": "measured_outcome", "samples": 10_000}],
        reason="a large comparison",
        proposed_by="someone",
    )
    assert strong["verdict"] == "unknown"
    assert "floor and not a verdict" in " ".join(strong["verdict_reasons"])


def test_opinion_never_justifies_anything():
    motion = policy_motion.build_motion(
        target=REGIONS,
        evidence=[{"kind": "opinion", "samples": 500}],
        reason="it feels wrong",
        proposed_by="someone",
    )
    assert motion["verdict"] == "insufficient"
    assert "observation" in motion["verdict_reasons"][0]


def test_the_bar_is_per_class_not_flat():
    """Counts justify a window; they do not justify what somebody is owed."""
    counts = [{"kind": "observation", "samples": 200}]
    measured = [{"kind": "measured_outcome", "samples": 200}]

    window = policy_motion.build_motion(
        target=REGIONS, evidence=counts, reason="peak moved", proposed_by="ops"
    )
    entitlement = policy_motion.build_motion(
        target=GENEROSITY_RULES, evidence=counts, reason="seemed fine", proposed_by="ops"
    )
    assert window["verdict"] == "sufficient"
    assert entitlement["verdict"] == "insufficient"
    assert "measured_outcome" in " ".join(entitlement["verdict_reasons"])

    # The same observation evidence is fine once it is a real comparison.
    better = policy_motion.build_motion(
        target=GENEROSITY_RULES,
        evidence=measured,
        reason="fulfilment improved",
        proposed_by="analytics",
    )
    assert better["verdict"] == "sufficient"


def test_a_motion_with_no_reason_or_proposer_is_refused():
    """'We changed it' is the class of motion this ledger exists to stop."""
    motion = policy_motion.build_motion(
        target=STATUS_RULE,
        evidence=[{"kind": "measured_outcome", "samples": 500}],
    )
    assert motion["verdict"] == "insufficient"
    joined = " ".join(motion["verdict_reasons"])
    assert "no reason given" in joined
    assert "no proposer recorded" in joined


def test_a_motion_is_never_applied():
    """Stage D's rule: an engine that retunes its own table is unauditable."""
    motion = policy_motion.build_motion(
        target=STATUS_RULE,
        evidence=[{"kind": "measured_outcome", "samples": 500}],
        reason="retune",
        proposed_by="analytics",
    )
    assert motion["verdict"] == "sufficient"
    assert motion["applied"] is False
    ledger = policy_motion.build_motion_ledger(
        [
            {
                "target": STATUS_RULE,
                "evidence": [{"kind": "measured_outcome", "samples": 500}],
                "reason": "retune",
                "proposed_by": "analytics",
            }
        ]
    )
    assert ledger["applied"] == []


def test_the_ledger_lists_failures_first():
    """A ledger showing only what passed is a list of achievements."""
    ledger = policy_motion.build_motion_ledger(
        [
            {
                "target": STATUS_RULE,
                "evidence": [{"kind": "measured_outcome", "samples": 500}],
                "reason": "retune",
                "proposed_by": "analytics",
            },
            {
                "target": REGIONS,
                "evidence": [{"kind": "opinion", "samples": 5}],
                "reason": "x",
                "proposed_by": "y",
            },
        ]
    )
    assert ledger["counts"]["sufficient"] == 1
    assert ledger["counts"]["insufficient"] == 1
    assert ledger["motions"][0]["verdict"] == "insufficient"
    # The order promised in the docstring is the order performed.
    verdicts = [row["verdict"] for row in ledger["motions"]]
    assert verdicts == sorted(
        verdicts, key=lambda v: ["insufficient", "unknown", "sufficient"].index(v)
    )


def test_a_customer_affecting_change_that_passes_asks_for_notification():
    ledger = policy_motion.build_motion_ledger(
        [
            {
                "target": STATUS_RULE,
                "evidence": [{"kind": "measured_outcome", "samples": 500}],
                "reason": "retune",
                "proposed_by": "analytics",
            }
        ]
    )
    assert ledger["needs_notification"] == [
        {"target": STATUS_RULE, "class_id": "status_rule_change"}
    ]


# ---------------------------------------------------------------------------
# Item 8 -- trust under continuous change
# ---------------------------------------------------------------------------


def _all_guards(overrides=None):
    guards = {
        str(row["regression_guard"]): True for row in policy_motion.PROMISES
    }
    guards.update(overrides or {})
    return guards


def test_an_unchecked_promise_is_not_a_held_one():
    """The reporting failure this function exists to prevent."""
    declared = [{"promise_id": pid} for pid in policy_motion.PROMISE_IDS]
    unchecked = policy_motion.check_promise_continuity(declared, guards={})
    assert unchecked["continuity"] is False
    assert unchecked["fully_verified"] is False
    assert len(unchecked["unverified"]) == len(policy_motion.PROMISES)


def test_nothing_checked_is_not_the_same_as_nothing_broken():
    empty = policy_motion.check_promise_continuity()
    assert empty["continuity"] is False
    assert empty["promises"] == []


def test_a_broken_promise_names_the_commitment_it_broke():
    report = policy_motion.check_promise_continuity(
        [{"promise_id": pid} for pid in policy_motion.PROMISE_IDS],
        guards=_all_guards({"contact_hour_is_local": False}),
    )
    assert report["continuity"] is False
    assert len(report["broken"]) == 1
    broken = report["broken"][0]
    assert broken["commitment"].strip()
    assert "contact_hour_is_local" in broken["reason"]


def test_every_named_promise_is_checked_by_a_registered_probe():
    """A promise checked by nothing is an intention."""
    from app import real_life_flows

    for row in policy_motion.PROMISES:
        assert row["regression_guard"] in real_life_flows.PROBES, row["promise_id"]


def test_every_promise_says_what_breaking_it_looks_like():
    for row in policy_motion.PROMISES:
        assert row["breaks_if"].strip(), row["promise_id"]
        assert row["why"].strip(), row["promise_id"]


def test_the_silence_is_a_movement_not_an_absence():
    """A programme doing nothing must say it is doing nothing."""
    assert "paused" in loyalty_status.VALUE_MOVEMENT_IDS
    movements = loyalty_status.build_value_trajectory(history=[])
    assert movements["entries"] == []


# ---------------------------------------------------------------------------
# Item 7 -- membership value evolution
# ---------------------------------------------------------------------------


def test_value_evolution_validates():
    assert loyalty_status.validate_value_evolution()["valid"]


def test_a_trajectory_refuses_an_unjustified_demotion():
    trajectory = loyalty_status.build_value_trajectory(
        history=[
            {"movement": "earned", "rank": 2, "at": "2025-01-01"},
            {"movement": "earned", "rank": 0, "at": "2026-01-01"},
        ]
    )
    assert trajectory["refused"]
    refused = trajectory["refused"][0]
    assert refused["movement"] == "earned"
    assert "no-decay" in refused["reason"] or "not a movement allowed to lower" in refused["reason"]
    # The refused demotion must not become the current rank.
    assert trajectory["current_rank"] == 2
    assert trajectory["ever_lowered_without_evidence"] is False


def test_a_refused_move_is_not_reported_as_having_happened():
    """A field that reports refused events as if they occurred is worse than none."""
    trajectory = loyalty_status.build_value_trajectory(
        history=[
            {"movement": "earned", "rank": 2, "at": "2025-01-01"},
            {"movement": "earned", "rank": 0, "at": "2026-01-01"},
        ]
    )
    assert trajectory["refused"]
    assert trajectory["ever_lowered_without_evidence"] is False


def test_a_confirmed_move_needs_its_evidence():
    without = loyalty_status.build_value_trajectory(
        history=[
            {"movement": "earned", "rank": 1, "at": "2025-01-01"},
            {"movement": "confirmed", "rank": 2, "at": "2026-01-01"},
        ]
    )
    assert without["refused"]

    with_evidence = loyalty_status.build_value_trajectory(
        history=[
            {"movement": "earned", "rank": 1, "at": "2025-01-01"},
            {
                "movement": "confirmed",
                "rank": 2,
                "at": "2026-01-01",
                "motion_id": "m-1",
                "evidence": [{"kind": "measured_outcome", "samples": 200}],
            },
        ]
    )
    assert with_evidence["refused"] == []
    assert with_evidence["current_rank"] == 2


def test_an_invented_movement_is_refused_rather_than_defaulted():
    trajectory = loyalty_status.build_value_trajectory(
        history=[{"movement": "promoted_out_of_kindness", "rank": 3}]
    )
    assert trajectory["refused"]
    assert trajectory["refused"][0]["known_movement"] is False
    assert "not a declared movement" in trajectory["refused"][0]["reason"]


def test_only_the_risk_movement_may_lower_a_status_and_it_needs_evidence():
    lowerers = [r for r in loyalty_status.VALUE_MOVEMENTS if r.get("may_lower")]
    assert [r["movement"] for r in lowerers] == ["lowered_for_risk"]
    for row in lowerers:
        assert row["requires_evidence"] is True


def test_the_band_is_still_ordinal_and_never_a_total():
    trajectory = loyalty_status.build_value_trajectory(
        history=[{"movement": "earned", "rank": 2, "at": "2025-01-01"}]
    )
    assert trajectory["membership_is_ordinal"] is True
    assert trajectory["status_decays_on_inactivity"] is False
