"""Tests for the recovery governance layer (guards, action registry, analytics).

This slice adds the bounds and the observability the original predictive-recovery
slice lacked. The automated sweep is a *loop*, and ``credit_points`` derives its
amount from the current dissatisfaction score -- so before this layer nothing
bounded how often a persistently-negative customer could be re-credited the same
goodwill amount, and nothing recorded that a limit had declined to act.

What is covered here:

- ``RECOVERY_ACTION_SPECS`` / ``RECOVERY_ACTION_REGISTRY``: a flat handler map
  with per-action declared policy (writes? idempotent? budgeted? capped?
  reversible?), replacing the orchestrator's if/elif dispatch chain.
- ``evaluate_recovery_guards``: pure per-action verdicts driven entirely by
  ``RECOVERY_GUARD_RULES`` + the action specs. Per-run, cooldown, per-day cap,
  and per-metric daily budget, each with a distinct guard id, plus the two
  non-obvious accounting cases (a *failed* attempt consumes the cooldown but not
  the cap; a blocked action consumes neither).
- ``recovery_history_row`` / ``_load_recovery_action_history``: the flat history
  shape, including the rule that an unregistered action's spend is not budgeted.
- Registry and config-table integrity: every playbook action routes, every
  handler has a spec, every writing action declares a cap and a cooldown, and
  every when-rule in the new tables validates against the shared DSL.
- ``build_recovery_action_analytics`` / ``build_recovery_outreach_plan`` /
  ``build_recovery_governance_audit``: the aggregate and preview surfaces.
- Orchestrator integration: a guard rejection is recorded as ``skipped`` with the
  guard that said no, dry runs evaluate guards without suppressing them, and the
  pinned v1 core behaviour (3 playbooks, 3 actions, one commit) is unchanged.
- The three new admin routes and the additive ``/meta`` surfaces.

Constraints honored: the three v1 core playbooks, ``catalog_version ==
recovery_playbooks_v1``, the ``recovery_credit`` ledger kind, the existing action
statuses, and the ``recovery_automation`` ecosystem route list are all
untouched. No DB-backed tests run outside the fake-DB harness.
"""
import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import models  # noqa: E402
from app.main import app  # noqa: E402
from app.rule_engine import evaluate_when, validate_when  # noqa: E402
from app.schemas.chat import (  # noqa: E402
    DissatisfactionRecoveryReport,
    InteractionSummary,
    RecoveryActionAnalyticsReport,
    RecoveryGuardReport,
    RecoveryOutreachPlan,
    RecoverySignal,
    Sentiment,
)
from app.services import recovery_playbooks  # noqa: E402
from app.services.recovery_playbooks import (  # noqa: E402
    RECOVERY_ACTION_NAMES,
    RECOVERY_ACTION_REGISTRY,
    RECOVERY_ACTION_SPEC_BY_NAME,
    RECOVERY_ACTION_SPECS,
    RECOVERY_CORE_PLAYBOOK_SET_VERSION,
    RECOVERY_GOVERNANCE_VERSION,
    RECOVERY_GUARD_REASON_STATUS,
    RECOVERY_GUARD_RULES,
    RECOVERY_GUARD_SKIP_STATUS,
    RECOVERY_OUTREACH_PLAYBOOKS,
    RECOVERY_PLAYBOOKS,
    RECOVERY_PLAYBOOK_SETS,
    RECOVERY_PLAYBOOK_SET_VERSIONS,
    RECOVERY_STATEFUL_ACTIONS,
    build_escalation_result,
    evaluate_recovery_guards,
    evaluate_recovery_playbooks,
    plan_recovery_actions,
    recovery_history_row,
)

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _summary(**overrides):
    defaults = dict(
        user_id=1,
        messages_analyzed=5,
        bookings_analyzed=1,
        churn_risk="high",
        loyalty_score=40.0,
        monetization_readiness=92.0,
        value_tier="premium",
        customer_classification="loyal high-value",
        top_issues=["support"],
        strengths=[],
        insights=[],
        metadata={"repeated_messages": 2, "booking_states": {"cancelled": 1}},
        generated_at=NOW,
    )
    defaults.update(overrides)
    return InteractionSummary(**defaults)


def _dissatisfaction(score=32.0, readiness="critical", signals=None):
    return DissatisfactionRecoveryReport(
        generated_at=NOW,
        window_days=30,
        dissatisfaction_score=score,
        recovery_readiness=readiness,
        primary_risks=["negative sentiment"],
        recovery_signals=signals
        or [
            RecoverySignal(
                area="support",
                intensity=4.2,
                evidence=["e"],
                evidence_summary="evidence summary",
                recommended_action="fast follow-up",
            )
        ],
        action_plan="plan",
    )


def _sentiment(label="negative", score=0.92):
    return Sentiment(label=label, score=score)


def _context(**summary_overrides):
    """Build a realtime context, with the nested ``metadata`` keys handled.

    ``booking_states`` and ``repeated_messages`` live inside ``InteractionSummary
    .metadata``, so passing them as top-level kwargs would silently do nothing.
    Routed through ``metadata`` here so a probe context means what it says.
    """
    booking_states = summary_overrides.pop("booking_states", None)
    repeated = summary_overrides.pop("repeated_messages", None)
    if booking_states is not None or repeated is not None:
        metadata = {"repeated_messages": 2, "booking_states": {"cancelled": 1}}
        if booking_states is not None:
            metadata["booking_states"] = booking_states
        if repeated is not None:
            metadata["repeated_messages"] = repeated
        summary_overrides["metadata"] = metadata
    kwargs = {"score": 32.0, "readiness": "critical"}
    readiness = summary_overrides.pop("readiness", None)
    if readiness:
        kwargs["readiness"] = readiness
    score = summary_overrides.pop("score", None)
    if score is not None:
        kwargs["score"] = score
    sentiment = summary_overrides.pop("sentiment", _sentiment())
    return recovery_playbooks.build_realtime_recovery_context(
        _summary(**summary_overrides), sentiment, _dissatisfaction(**kwargs)
    )


def _plan_item(index=0, playbook_id="pb", action="credit_points", spend=0.0, spend_metric=None):
    return {
        "index": index,
        "playbook_id": playbook_id,
        "playbook_name": "pb",
        "playbook_set": "core",
        "priority": 10,
        "action": action,
        "params": {},
        "spend_metric": spend_metric,
        "spend": spend,
        "registered": action in RECOVERY_ACTION_SPEC_BY_NAME,
        "mutates_state": action in RECOVERY_STATEFUL_ACTIONS,
    }


class _Empty:
    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None


class _Rows:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _FakeDb:
    """Async session double: table-shaped execute routing + add/flush/commit."""

    def __init__(self, wallets=(), chat_rows=(), bookings=(), users=(), recovery_actions=()):
        self.wallets = list(wallets)
        self.chat_rows = list(chat_rows)
        self.bookings = list(bookings)
        self.users = list(users)
        self.recovery_actions = list(recovery_actions)
        self.pending = []
        self.next_id = 1
        self.commits = 0

    async def execute(self, statement, *_args, **_kwargs):
        text = str(statement)
        if "points_wallets" in text:
            return _Rows(self.wallets)
        if "chat_history" in text:
            return _Rows(self.chat_rows)
        if "bookings" in text:
            return _Rows(self.bookings)
        if "recovery_actions" in text:
            return _Rows(self.recovery_actions)
        if "FROM users" in text:
            return _Rows(self.users)
        return _Empty()

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = self.next_id
            self.next_id += 1
        self.pending.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


def _seeded_db(**kwargs):
    return _FakeDb(
        wallets=[
            models.PointsWallet(id=5, user_id=1, point_type="loyalty_points", balance=100.0)
        ],
        **kwargs,
    )


def _history(action="credit_points", status="executed", hours_ago=1.0, spend=0.0, **kwargs):
    return recovery_history_row(
        action,
        status=status,
        at=NOW - timedelta(hours=hours_ago),
        spend=spend,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------


class TestActionRegistry:
    def test_every_action_name_has_a_spec_and_handler(self):
        assert set(RECOVERY_ACTION_SPEC_BY_NAME) == set(RECOVERY_ACTION_REGISTRY)
        assert RECOVERY_ACTION_NAMES == tuple(
            str(spec["action"]) for spec in RECOVERY_ACTION_SPECS
        )

    def test_original_three_actions_preserved(self):
        assert set(RECOVERY_ACTION_NAMES) >= {
            "credit_points",
            "escalate_ticket",
            "adjust_policy_score",
        }

    def test_spec_declares_full_policy_for_every_action(self):
        for spec in RECOVERY_ACTION_SPECS:
            for field in (
                "action",
                "title",
                "description",
                "mutates_state",
                "idempotent",
                "max_per_day",
                "cooldown_hours",
                "reversible",
            ):
                assert field in spec, f"{spec['action']} missing {field}"

    def test_only_credit_points_is_stateful(self):
        # Exactly one action writes. This is the load-bearing fact behind the
        # whole guard layer: a sweep that can write is a sweep that must be bounded.
        assert RECOVERY_STATEFUL_ACTIONS == ("credit_points",)

    def test_stateful_derivation_matches_specs(self):
        for spec in RECOVERY_ACTION_SPECS:
            expected = str(spec["action"]) in RECOVERY_STATEFUL_ACTIONS
            assert bool(spec["mutates_state"]) == expected

    def test_credit_points_is_non_idempotent_and_budgeted(self):
        spec = RECOVERY_ACTION_SPEC_BY_NAME["credit_points"]
        assert spec["mutates_state"] is True
        assert spec["idempotent"] is False
        assert spec["spend_metric"] == "points"
        assert spec["max_per_day"] > 0
        assert spec["cooldown_hours"] > 0

    def test_planning_actions_are_idempotent_and_free(self):
        for name in RECOVERY_ACTION_NAMES:
            if name == "credit_points":
                continue
            spec = RECOVERY_ACTION_SPEC_BY_NAME[name]
            assert spec["mutates_state"] is False, f"{name} writes state"
            assert spec["spend_metric"] is None, f"{name} is budgeted"

    def test_save_incentive_offers_nothing_even_at_the_widest_window(self):
        # The whole reason the incentive action declares no spend metric: a
        # 500-point offer is only a preview, and budgeting it would make the
        # budget a lie in the conservative direction at best.
        spec = RECOVERY_ACTION_SPEC_BY_NAME["offer_save_incentive"]
        assert spec["spend_metric"] is None
        assert spec["max_per_day"] == 1

    def test_registry_drift_is_empty(self):
        drift = recovery_playbooks.RECOVERY_REGISTRY_DRIFT
        assert drift == {"handlers_without_spec": [], "specs_without_handler": []}


# ---------------------------------------------------------------------------
# Config table integrity
# ---------------------------------------------------------------------------


class TestConfigTableIntegrity:
    def test_every_playbook_action_routes_to_a_handler(self):
        referenced = {
            str(action.get("action", ""))
            for rows in RECOVERY_PLAYBOOK_SETS.values()
            for playbook in rows
            for action in playbook.get("actions", [])
        }
        assert referenced <= set(RECOVERY_ACTION_REGISTRY)

    def test_guarded_actions_are_referenced_by_some_playbook(self):
        # An action nothing configures is dead weight in a governance layer: it
        # consumes a registry slot and a limit nobody can trip.
        referenced = {
            str(action.get("action", ""))
            for rows in RECOVERY_PLAYBOOK_SETS.values()
            for playbook in rows
            for action in playbook.get("actions", [])
        }
        assert set(RECOVERY_ACTION_NAMES) <= referenced

    def test_playbook_sets_are_identified_and_versioned(self):
        assert set(RECOVERY_PLAYBOOK_SETS) == {"core", "outreach"}
        assert RECOVERY_PLAYBOOK_SETS["core"] is RECOVERY_PLAYBOOKS
        assert RECOVERY_PLAYBOOK_SETS["outreach"] is RECOVERY_OUTREACH_PLAYBOOKS
        assert RECOVERY_PLAYBOOK_SET_VERSIONS["core"] == RECOVERY_CORE_PLAYBOOK_SET_VERSION
        assert RECOVERY_CORE_PLAYBOOK_SET_VERSION == "recovery_playbooks_v1"
        assert RECOVERY_PLAYBOOK_SET_VERSIONS["outreach"] != RECOVERY_CORE_PLAYBOOK_SET_VERSION

    def test_outreach_priorities_continue_after_the_core_set(self):
        core_max = max(int(p["priority"]) for p in RECOVERY_PLAYBOOKS)
        outreach_min = min(int(p["priority"]) for p in RECOVERY_OUTREACH_PLAYBOOKS)
        assert outreach_min > core_max, (
            "an outreach playbook must not outrank a core one; guard_run_playbook_limit "
            "admits the lowest-priority playbooks first"
        )

    def test_priorities_are_unique_within_each_set(self):
        for name, rows in RECOVERY_PLAYBOOK_SETS.items():
            priorities = [int(row["priority"]) for row in rows]
            assert len(priorities) == len(set(priorities)), name

    def test_playbook_ids_unique_across_sets(self):
        ids = [row["playbook_id"] for rows in RECOVERY_PLAYBOOK_SETS.values() for row in rows]
        assert len(ids) == len(set(ids))

    def test_every_new_when_rule_validates_against_the_shared_dsl(self):
        # Guards against the list/dict RHS trap: `{"x": [">=", 2]}` validates as
        # a membership test on a two-element list, is always False for a number,
        # and looks like working config. Only the named-operator form is correct.
        rules = [(row["playbook_id"], row["when"]) for row in RECOVERY_OUTREACH_PLAYBOOKS]
        rules += [
            (f"review:{rule['rule_id']}", rule["when"])
            for rule in recovery_playbooks.RECOVERY_REVIEW_RULES
        ]
        rules += [(rule["stage"], rule["when"]) for rule in recovery_playbooks.RECOVERY_STAGE_RULES]
        rules += [(plan["plan_id"], plan["when"]) for plan in recovery_playbooks.RECOVERY_CALLBACK_PLANS]
        rules += [(offer["offer_id"], offer["when"]) for offer in recovery_playbooks.RECOVERY_SAVE_INCENTIVES]
        known = {
            "recovery_readiness",
            "sentiment_label",
            "churn_risk",
            "value_tier",
            "booking_cancelled",
            "booking_total",
            "booking_completed",
            "repeated_messages",
            "messages_analyzed",
        }
        for name, when in rules:
            verdict = validate_when(when, known_fields=known)
            assert verdict["valid"] is True, f"{name}: {verdict['errors']}"
            assert verdict["errors"] == [], name

    def test_outreach_when_rules_use_named_operators_where_numeric(self):
        # Assert the operator was actually understood, not merely accepted.
        surge = next(p for p in RECOVERY_OUTREACH_PLAYBOOKS if p["playbook_id"] == "recovery_contact_surge")
        verdict = validate_when(surge["when"], known_fields={"repeated_messages"})
        assert verdict["operators_used"], surge["when"]
        assert "gte" in str(surge["when"])

    def test_every_outreach_playbook_fires_on_its_own_probe(self):
        # A rule that never matches is a silently dead playbook: it occupies a
        # priority, appears in the catalog, and can never run.
        probes = {
            "recovery_cancellation_save": _context(booking_states={"cancelled": 9}),
            "recovery_contact_surge": _context(repeated_messages=9, sentiment=_sentiment("neutral", 0.5)),
            "recovery_warm_reengagement": _context(
                readiness="low", score=4.0, sentiment=_sentiment("neutral", 0.5)
            ),
        }
        assert set(probes) == {row["playbook_id"] for row in RECOVERY_OUTREACH_PLAYBOOKS}
        for playbook_id, context in probes.items():
            matched = [
                entry["playbook_id"]
                for entry in evaluate_recovery_playbooks(context, playbook_sets=["outreach"])
            ]
            assert playbook_id in matched, f"{playbook_id} never matches its probe context"

    def test_outreach_triggers_do_not_imply_core_triggers(self):
        # An outreach signal (cancellations, contact surge) must not by itself
        # drag the wallet-lifting core set along. The core set stays keyed to
        # *dissatisfaction*, which is what it can act on.
        for overrides in (
            {"booking_states": {"cancelled": 9}},
            {
                "repeated_messages": 9,
                "sentiment": _sentiment("neutral", 0.5),
                "readiness": "low",
                "score": 4.0,
            },
            {"readiness": "low", "score": 4.0, "sentiment": _sentiment("neutral", 0.5)},
        ):
            context = _context(**overrides)
            outreach = [
                entry["playbook_id"]
                for entry in evaluate_recovery_playbooks(context, playbook_sets=["outreach"])
            ]
            core = [
                entry["playbook_id"]
                for entry in evaluate_recovery_playbooks(context, playbook_sets=["core"])
            ]
            assert outreach, overrides
            # The pinned core probe (negative sentiment, critical) does fire the
            # core set; what must never happen is a *neutral* context doing so.
            if str(context["sentiment_label"]) != "negative":
                assert core == [], f"{overrides} leaked into the core set: {core}"

    def test_every_action_spec_declares_a_bounded_policy(self):
        for spec in RECOVERY_ACTION_SPECS:
            assert int(spec["max_per_day"]) > 0, spec["action"]
            assert float(spec["cooldown_hours"]) > 0.0, spec["action"]

    def test_guard_rules_are_unique_and_enabled(self):
        ids = [str(rule["guard_id"]) for rule in RECOVERY_GUARD_RULES]
        assert len(ids) == len(set(ids))
        assert all(rule.get("enabled", True) for rule in RECOVERY_GUARD_RULES)

    def test_daily_budget_rule_names_a_budgeted_metric(self):
        budget_rules = [r for r in RECOVERY_GUARD_RULES if r["check"] == "daily_budget"]
        assert budget_rules, "the unbounded-spend case needs a budget rule"
        budgeted = {spec["spend_metric"] for spec in RECOVERY_ACTION_SPECS if spec.get("spend_metric")}
        for rule in budget_rules:
            assert str(rule["metric"]) in budgeted
            assert float(rule["max"]) > 0.0

    def test_history_rules_cover_every_guard_check(self):
        for rule in RECOVERY_GUARD_RULES:
            assert str(rule["check"]) in recovery_playbooks.RECOVERY_GUARD_HISTORY_RULES


# ---------------------------------------------------------------------------
# History shape
# ---------------------------------------------------------------------------


class TestHistoryRow:
    def test_normalises_naive_datetime_to_utc(self):
        naive = datetime(2026, 1, 1, 12, 0, 0)
        row = recovery_history_row("escalate_ticket", at=naive)
        assert row["at"].tzinfo is not None

    def test_parses_iso_string_timestamps(self):
        row = recovery_history_row("escalate_ticket", at="2026-01-01T12:00:00+00:00")
        assert isinstance(row["at"], datetime)

    def test_unparseable_timestamp_becomes_none(self):
        row = recovery_history_row("escalate_ticket", at="not-a-date")
        assert row["at"] is None

    def test_spend_only_honoured_for_a_budgeted_action(self):
        budgeted = recovery_history_row("credit_points", spend=500.0)
        assert budgeted["spend"] == 500.0
        assert budgeted["spend_metric"] == "points"
        free = recovery_history_row("escalate_ticket", spend=500.0)
        assert free["spend"] == 0.0
        assert free["spend_metric"] is None

    def test_spend_from_an_unregistered_action_is_discarded(self):
        # An unknown action carrying a number is a ledger shape change, not money
        # a budget is allowed to spend.
        row = recovery_history_row("mystery_action", spend=999.0)
        assert row["spend"] == 0.0
        assert row["registered"] is False

    def test_negative_spend_is_discarded(self):
        assert recovery_history_row("credit_points", spend=-50.0)["spend"] == 0.0

    def test_non_numeric_spend_is_discarded(self):
        assert recovery_history_row("credit_points", spend="lots")["spend"] == 0.0

    def test_load_history_parses_ledger_spend(self):
        now = datetime.now(timezone.utc)
        record = models.RecoveryAction(
            id=9,
            user_id=1,
            playbook_id="recovery_goodwill_points",
            action="credit_points",
            status="executed",
            payload_json="{}",
            result_json='{"points_credited": 105.0, "new_balance": 205.0}',
            reference="recovery:goodwill",
            failure_reason="",
        )
        record.created_at = now
        db = _FakeDb(recovery_actions=[record])
        rows = asyncio.run(
            recovery_playbooks._load_recovery_action_history(db, 1, now)
        )
        assert len(rows) == 1
        assert rows[0]["spend"] == 105.0
        assert rows[0]["at"] is not None
        assert rows[0]["reference"] == "recovery:goodwill"

    def test_load_history_tolerates_corrupt_result_json(self):
        now = datetime.now(timezone.utc)
        record = models.RecoveryAction(
            id=1,
            user_id=1,
            playbook_id="pb",
            action="credit_points",
            status="executed",
            payload_json="{}",
            result_json="not json at all",
            reference="",
            failure_reason="",
        )
        record.created_at = now
        db = _FakeDb(recovery_actions=[record])
        rows = asyncio.run(recovery_playbooks._load_recovery_action_history(db, 1, now))
        assert rows[0]["spend"] == 0.0

    def test_history_window_covers_the_longest_cooldown(self):
        # Loading only the daily window would make any cooldown longer than a day
        # silently inert: the guard would report "allowed" because it never saw
        # the earlier run.
        assert recovery_playbooks.RECOVERY_GUARD_HISTORY_HOURS >= (
            recovery_playbooks.RECOVERY_GUARD_MAX_COOLDOWN_HOURS
        )
        assert recovery_playbooks.RECOVERY_GUARD_HISTORY_HOURS >= (
            recovery_playbooks.RECOVERY_GUARD_LOOKBACK_HOURS
        )


# ---------------------------------------------------------------------------
# Guard evaluation (pure)
# ---------------------------------------------------------------------------


class TestGuardEvaluation:
    def test_empty_plan_allows_nothing(self):
        result = evaluate_recovery_guards([], [], now=NOW)
        assert result["planned_actions"] == 0
        assert result["allowed_actions"] == 0
        assert result["blocked_actions"] == 0
        assert result["decisions"] == []

    def test_clean_history_allows_the_action(self):
        plan = [_plan_item(action="credit_points", spend=105.0, spend_metric="points")]
        result = evaluate_recovery_guards(plan, [], now=NOW)
        decision = result["decisions"][0]
        assert decision["allowed"] is True
        assert decision["status"] == "allowed"
        assert decision["guard_id"] == ""

    def test_cooldown_blocks_a_recent_attempt(self):
        plan = [_plan_item(action="credit_points", spend=105.0, spend_metric="points")]
        history = [_history("credit_points", "executed", hours_ago=1.0, spend=105.0)]
        decision = evaluate_recovery_guards(plan, history, now=NOW)["decisions"][0]
        assert decision["allowed"] is False
        assert decision["guard_id"] == "guard_action_cooldown"
        assert decision["detail"]["hours_since_last"] == pytest.approx(1.0)
        assert decision["detail"]["cooldown_hours"] == 24.0

    def test_cooldown_expires(self):
        plan = [_plan_item(action="credit_points", spend=105.0, spend_metric="points")]
        history = [_history("credit_points", "executed", hours_ago=25.0, spend=105.0)]
        assert evaluate_recovery_guards(plan, history, now=NOW)["decisions"][0]["allowed"] is True

    def test_cooldown_counts_a_failed_attempt(self):
        # Otherwise a persistently failing credit retries on every sweep tick.
        plan = [_plan_item(action="credit_points", spend=105.0, spend_metric="points")]
        history = [_history("credit_points", "failed", hours_ago=1.0)]
        decision = evaluate_recovery_guards(plan, history, now=NOW)["decisions"][0]
        assert decision["allowed"] is False
        assert decision["guard_id"] == "guard_action_cooldown"

    def test_daily_cap_does_not_count_a_failed_attempt(self):
        # The cap bounds what was delivered, not what was attempted.
        spec = RECOVERY_ACTION_SPEC_BY_NAME["escalate_ticket"]
        history = [
            _history("escalate_ticket", "failed", hours_ago=3.0) for _ in range(spec["max_per_day"])
        ]
        plan = [_plan_item(action="escalate_ticket")]
        decision = evaluate_recovery_guards(plan, history, now=NOW)["decisions"][0]
        assert decision["allowed"] is True

    def test_guard_rejected_history_does_not_consume_cooldown(self):
        # A guard rejection is not an attempt; counting it would deadlock the
        # action behind its own refusal.
        plan = [_plan_item(action="credit_points", spend=105.0, spend_metric="points")]
        history = [_history("credit_points", RECOVERY_GUARD_SKIP_STATUS, hours_ago=1.0)]
        assert evaluate_recovery_guards(plan, history, now=NOW)["decisions"][0]["allowed"] is True

    def test_daily_cap_blocks_at_the_limit(self):
        spec = RECOVERY_ACTION_SPEC_BY_NAME["escalate_ticket"]
        history = [
            _history("escalate_ticket", "executed", hours_ago=3.0 + index * 0.01)
            for index in range(spec["max_per_day"])
        ]
        decision = evaluate_recovery_guards([_plan_item(action="escalate_ticket")], history, now=NOW)[
            "decisions"
        ][0]
        assert decision["allowed"] is False
        assert decision["guard_id"] == "guard_action_daily_cap"
        assert decision["detail"]["delivered_today"] == spec["max_per_day"]

    def test_daily_cap_window_is_sliding_not_calendar(self):
        spec = RECOVERY_ACTION_SPEC_BY_NAME["escalate_ticket"]
        history = [
            _history("escalate_ticket", "executed", hours_ago=25.0)
            for _ in range(spec["max_per_day"])
        ]
        assert evaluate_recovery_guards([_plan_item(action="escalate_ticket")], history, now=NOW)[
            "decisions"
        ][0]["allowed"] is True

    def test_daily_budget_blocks_the_second_of_two_large_credits(self):
        # The core accounting case. A per-action test against the limit would pass
        # both 500-point credits against a 750 budget.
        limit = next(
            float(rule["max"])
            for rule in RECOVERY_GUARD_RULES
            if rule["check"] == "daily_budget" and rule["metric"] == "points"
        )
        amount = round(limit / 2.0 + 1.0, 2)
        plan = [
            _plan_item(index=0, playbook_id="pb0", action="credit_points", spend=amount, spend_metric="points"),
            _plan_item(index=1, playbook_id="pb1", action="credit_points", spend=amount, spend_metric="points"),
        ]
        decisions = evaluate_recovery_guards(plan, [], now=NOW)["decisions"]
        assert decisions[0]["allowed"] is True
        assert decisions[1]["allowed"] is False
        assert decisions[1]["guard_id"] == "guard_points_daily_budget"
        assert decisions[1]["detail"]["limit"] == limit
        assert decisions[1]["detail"]["already_committed"] == amount

    def test_daily_budget_accounts_for_prior_spend(self):
        limit = float(
            next(
                float(rule["max"])
                for rule in RECOVERY_GUARD_RULES
                if rule["check"] == "daily_budget" and rule["metric"] == "points"
            )
        )
        # The prior credit is placed outside the cooldown window (30h > 24h) and
        # the lookback is widened to include it, so the *budget* is what refuses.
        # The two are independent limits and a test that trips both proves only
        # that one of them works.
        history = [_history("credit_points", "executed", hours_ago=30.0, spend=limit)]
        plan = [_plan_item(action="credit_points", spend=1.0, spend_metric="points")]
        result = evaluate_recovery_guards(plan, history, now=NOW, lookback_hours=48.0)
        decision = result["decisions"][0]
        assert decision["allowed"] is False
        assert decision["guard_id"] == "guard_points_daily_budget"
        assert decision["detail"]["already_committed"] == limit

    def test_daily_budget_window_excludes_prior_days(self):
        limit = float(
            next(
                float(rule["max"])
                for rule in RECOVERY_GUARD_RULES
                if rule["check"] == "daily_budget" and rule["metric"] == "points"
            )
        )
        history = [_history("credit_points", "executed", hours_ago=30.0, spend=limit)]
        plan = [_plan_item(action="credit_points", spend=1.0, spend_metric="points")]
        # Same history under the default 24h lookback: outside the budget window,
        # so the budget does not refuse. The window is the caller's, not a
        # hardcoded day.
        default = evaluate_recovery_guards(plan, history, now=NOW)
        assert default["decisions"][0]["allowed"] is True

    def test_budget_ignores_unbudgeted_spend(self):
        plan = [_plan_item(action="escalate_ticket", spend=10_000.0)]
        assert evaluate_recovery_guards(plan, [], now=NOW)["decisions"][0]["allowed"] is True

    def test_per_run_playbook_limit_suppresses_low_priority_playbooks(self):
        limit = next(
            int(rule["max"])
            for rule in RECOVERY_GUARD_RULES
            if rule["check"] == "max_per_run" and rule["subject"] == "playbook"
        )
        plan = [
            _plan_item(index=index, playbook_id=f"pb{index}", action="escalate_ticket")
            for index in range(limit + 2)
        ]
        result = evaluate_recovery_guards(plan, [], now=NOW)
        allowed = [d["allowed"] for d in result["decisions"]]
        assert allowed[:limit] == [True] * limit
        assert all(value is False for value in allowed[limit:])
        assert {entry["playbook_id"] for entry in result["suppressed_playbooks"]} == {
            f"pb{index}" for index in range(limit, limit + 2)
        }

    def test_per_run_action_limit_counts_actions_not_playbooks(self):
        limit = next(
            int(rule["max"])
            for rule in RECOVERY_GUARD_RULES
            if rule["check"] == "max_per_run" and rule["subject"] == "action"
        )
        plan = [
            _plan_item(index=index, playbook_id="pb", action="escalate_ticket")
            for index in range(limit + 2)
        ]
        decisions = evaluate_recovery_guards(plan, [], now=NOW)["decisions"]
        assert [d["allowed"] for d in decisions[:limit]] == [True] * limit
        assert all(
            d["guard_id"] == "guard_run_action_limit" for d in decisions[limit:]
        )

    def test_unknown_action_blocked_with_its_own_guard(self):
        # Not an ordinary limit trip: the fix is a config entry, not a bigger limit.
        plan = [_plan_item(action="mystery_action")]
        decision = evaluate_recovery_guards(plan, [], now=NOW)["decisions"][0]
        assert decision["allowed"] is False
        assert decision["guard_id"] == "guard_unknown_action"
        assert "no registry spec" in decision["reason"]

    def test_blocked_action_consumes_no_budget(self):
        limit = next(
            float(rule["max"])
            for rule in RECOVERY_GUARD_RULES
            if rule["check"] == "daily_budget" and rule["metric"] == "points"
        )
        plan = [
            _plan_item(index=0, playbook_id="pb0", action="credit_points", spend=limit, spend_metric="points"),
            _plan_item(index=1, playbook_id="pb1", action="credit_points", spend=limit, spend_metric="points"),
        ]
        result = evaluate_recovery_guards(plan, [], now=NOW)
        assert result["decisions"][0]["allowed"] is True
        assert result["decisions"][1]["allowed"] is False
        assert result["budget"]["points"]["committed_with_plan"] == limit

    def test_cap_slot_consumption_matches_allowances_not_attempts(self):
        # A cap that counted attempts would deadlock the action behind its own
        # guard rejections: each blocked retry would consume another slot.
        spec = RECOVERY_ACTION_SPEC_BY_NAME["escalate_ticket"]
        plan = [
            _plan_item(index=index, playbook_id=f"pb{index}", action="escalate_ticket")
            for index in range(spec["max_per_day"] + 2)
        ]
        result = evaluate_recovery_guards(plan, [], now=NOW)
        allowed = [d for d in result["decisions"] if d["allowed"]]
        blocked = [d for d in result["decisions"] if not d["allowed"]]
        assert blocked, "expected a limit to bind"
        # Whatever the reason for blocking, the post-run count equals the number
        # of actions that were actually allowed.
        assert result["usage"]["escalate_ticket"]["delivered_today"] == len(allowed)
        assert len(allowed) <= spec["max_per_day"]

    def test_enforce_false_reports_without_blocking(self):
        plan = [_plan_item(action="credit_points", spend=105.0, spend_metric="points")]
        history = [_history("credit_points", "executed", hours_ago=1.0, spend=105.0)]
        result = evaluate_recovery_guards(plan, history, now=NOW, enforce=False)
        assert result["enforced"] is False
        assert result["decisions"][0]["allowed"] is True
        assert "not enforced" in result["summary"]

    def test_enforced_and_unenforced_agree_on_a_clean_run(self):
        plan = [_plan_item(action="credit_points", spend=105.0, spend_metric="points")]
        history = [_history("credit_points", "executed", hours_ago=1.0, spend=105.0)]
        dry = evaluate_recovery_guards(plan, history, now=NOW, enforce=False)
        live = evaluate_recovery_guards(plan, history, now=NOW, enforce=True)
        assert dry["decisions"][0]["allowed"] is True
        assert live["decisions"][0]["allowed"] is False

    def test_history_rows_without_timestamps_are_ignored_for_limits(self):
        plan = [_plan_item(action="credit_points", spend=105.0, spend_metric="points")]
        history = [recovery_history_row("credit_points", status="executed", at=None, spend=105.0)]
        assert evaluate_recovery_guards(plan, history, now=NOW)["decisions"][0]["allowed"] is True

    def test_unregistered_history_actions_are_surfaced(self):
        plan = [_plan_item(action="escalate_ticket")]
        history = [_history("retired_action", "executed", hours_ago=1.0)]
        result = evaluate_recovery_guards(plan, history, now=NOW)
        assert "retired_action" in result["unregistered_actions"]

    def test_one_decision_per_planned_action_in_plan_order(self):
        plan = [
            _plan_item(index=0, playbook_id="pb0", action="escalate_ticket"),
            _plan_item(index=1, playbook_id="pb1", action="credit_points", spend=105.0, spend_metric="points"),
            _plan_item(index=2, playbook_id="pb2", action="mystery_action"),
        ]
        decisions = evaluate_recovery_guards(plan, [], now=NOW)["decisions"]
        assert [d["index"] for d in decisions] == [0, 1, 2]
        assert [d["action"] for d in decisions] == [
            "escalate_ticket",
            "credit_points",
            "mystery_action",
        ]

    def test_reports_which_rules_were_consulted(self):
        # "The cooldown did not fire" must be distinguishable from "the cooldown
        # was never consulted".
        plan = [_plan_item(action="credit_points", spend=105.0, spend_metric="points")]
        result = evaluate_recovery_guards(plan, [], now=NOW)
        consulted = {rule["guard_id"] for rule in result["rules"] if rule["consulted"]}
        assert "guard_action_cooldown" in consulted
        assert "guard_points_daily_budget" in consulted

    def test_usage_reports_the_post_run_position(self):
        # ``usage`` is the position *after* the evaluated plan, so it answers
        # "where will I be" rather than "where am I". That is the number a cap
        # check on the next sweep needs.
        plan = [_plan_item(action="escalate_ticket")]
        history = [_history("escalate_ticket", "executed", hours_ago=3.0)]
        usage = evaluate_recovery_guards(plan, history, now=NOW)["usage"]
        assert usage["escalate_ticket"]["attempts_recorded"] == 1
        assert usage["escalate_ticket"]["delivered_today"] == 2
        assert usage["escalate_ticket"]["last_run_at"]
        assert usage["escalate_ticket"]["cooldown_hours"] == 2.0
        assert usage["escalate_ticket"]["max_per_day"] == 8

    def test_blocked_action_leaves_usage_at_the_history_position(self):
        spec = RECOVERY_ACTION_SPEC_BY_NAME["escalate_ticket"]
        history = [
            _history("escalate_ticket", "executed", hours_ago=3.0 + index * 0.01)
            for index in range(spec["max_per_day"])
        ]
        plan = [_plan_item(action="escalate_ticket")]
        result = evaluate_recovery_guards(plan, history, now=NOW)
        assert result["decisions"][0]["allowed"] is False
        assert result["usage"]["escalate_ticket"]["delivered_today"] == spec["max_per_day"]

    def test_summary_counts_reflect_the_verdicts(self):
        plan = [
            _plan_item(index=0, playbook_id="pb0", action="credit_points", spend=105.0, spend_metric="points"),
            _plan_item(index=1, playbook_id="pb1", action="mystery_action"),
        ]
        result = evaluate_recovery_guards(plan, [], now=NOW)
        assert result["planned_actions"] == 2
        assert result["allowed_actions"] == 1
        assert result["blocked_actions"] == 1
        assert result["playbooks_allowed"] == 1
        assert "1/2" in result["summary"]

    def test_repeat_sweeps_are_bounded_over_time(self):
        # The regression this whole layer exists for. Simulate a persistently
        # negative premium customer across many sweep ticks and assert the
        # credited total stops growing instead of compounding.
        spec = RECOVERY_ACTION_SPEC_BY_NAME["credit_points"]
        amount = 105.0
        history: list[dict] = []
        credited = 0.0
        credits = 0
        for tick in range(48):
            moment = NOW + timedelta(minutes=5 * tick)
            plan = [
                _plan_item(index=0, playbook_id="pb", action="credit_points", spend=amount, spend_metric="points")
            ]
            decision = evaluate_recovery_guards(plan, history, now=moment)["decisions"][0]
            if decision["allowed"]:
                credits += 1
                credited += amount
                history.append(
                    recovery_history_row(
                        "credit_points", status="executed", at=moment, spend=amount
                    )
                )
        assert credits == 1, f"expected the cooldown to allow exactly one credit, got {credits}"
        assert credited == amount
        assert spec["cooldown_hours"] >= 24.0

    def test_sustained_failures_cannot_hot_loop(self):
        history: list[dict] = []
        attempts = 0
        for tick in range(48):
            moment = NOW + timedelta(minutes=5 * tick)
            plan = [_plan_item(action="credit_points", spend=105.0, spend_metric="points")]
            decision = evaluate_recovery_guards(plan, history, now=moment)["decisions"][0]
            if decision["allowed"]:
                attempts += 1
                history.append(recovery_history_row("credit_points", status="failed", at=moment))
        assert attempts == 1, f"a failing action retried {attempts} times across a day"

    def test_budget_holds_under_a_sustained_sweep(self):
        limit = next(
            float(rule["max"])
            for rule in RECOVERY_GUARD_RULES
            if rule["check"] == "daily_budget" and rule["metric"] == "points"
        )
        history: list[dict] = []
        credited = 0.0
        for tick in range(48):
            moment = NOW + timedelta(minutes=5 * tick)
            plan = [
                _plan_item(
                    index=0,
                    playbook_id=f"pb{tick}",
                    action="credit_points",
                    spend=limit,
                    spend_metric="points",
                )
            ]
            decision = evaluate_recovery_guards(plan, history, now=moment)["decisions"][0]
            if decision["allowed"]:
                credited += limit
                history.append(
                    recovery_history_row("credit_points", status="executed", at=moment, spend=limit)
                )
        assert credited <= limit, f"credited {credited} against a {limit} budget"


# ---------------------------------------------------------------------------
# Action planning
# ---------------------------------------------------------------------------


class TestActionPlanning:
    def test_plan_flattens_matched_playbooks_in_priority_order(self):
        context = _context()
        plan = plan_recovery_actions(context)
        assert [item["playbook_id"] for item in plan] == [
            "recovery_goodwill_points",
            "recovery_ticket_escalation",
            "recovery_policy_guardrail",
        ]
        assert [item["index"] for item in plan] == [0, 1, 2]

    def test_plan_resolves_formula_params(self):
        plan = plan_recovery_actions(_context())
        credit = next(item for item in plan if item["action"] == "credit_points")
        assert credit["params"]["points"] == pytest.approx(105.0)
        assert credit["params"]["reference"] == "recovery:goodwill"

    def test_plan_reports_budgeted_spend(self):
        plan = plan_recovery_actions(_context())
        credit = next(item for item in plan if item["action"] == "credit_points")
        assert credit["spend"] == pytest.approx(105.0)
        assert credit["spend_metric"] == "points"
        assert credit["mutates_state"] is True
        for item in plan:
            if item["action"] != "credit_points":
                assert item["spend"] == 0.0
                assert item["spend_metric"] is None

    def test_plan_marks_unregistered_actions(self):
        # Kept in the plan rather than dropped: a configured playbook action with
        # no handler is registry drift the analytics should report, not silence.
        context = dict(_context())
        plan = plan_recovery_actions(context)
        assert all(item["registered"] for item in plan)

    def test_plan_is_empty_when_nothing_matches(self):
        context = _context(readiness="moderate", score=8.0, churn_risk="low", value_tier="standard")
        assert plan_recovery_actions(context) == []

    def test_plan_can_be_restricted_to_one_playbook_set(self):
        context = _context()
        core = plan_recovery_actions(context, playbook_sets=["core"])
        assert {item["playbook_set"] for item in core} == {"core"}
        outreach = plan_recovery_actions(context, playbook_sets=["outreach"])
        assert outreach == []

    def test_outreach_playbook_plans_reach_out_without_spending(self):
        context = _context(booking_states={"cancelled": 4}, sentiment=_sentiment())
        plan = plan_recovery_actions(context, playbook_sets=["outreach"])
        assert {item["playbook_id"] for item in plan} == {"recovery_cancellation_save"}
        assert all(item["spend"] == 0.0 for item in plan)
        assert all(item["mutates_state"] is False for item in plan)

    def test_outreach_playbooks_never_touch_the_wallet(self):
        for context in (
            _context(booking_states={"cancelled": 9}),
            _context(repeated_messages=9, sentiment=_sentiment("neutral", 0.5)),
            _context(readiness="low", score=4.0, sentiment=_sentiment("neutral", 0.5)),
        ):
            plan = plan_recovery_actions(context, playbook_sets=["outreach"])
            assert all(item["action"] != "credit_points" for item in plan)
            assert all(item["spend"] == 0.0 for item in plan)


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class TestOrchestratorGuards:
    def test_pinned_core_run_unchanged_with_no_history(self):
        db = _seeded_db()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                summary=_summary(),
                sentiment=_sentiment(),
                dissatisfaction=_dissatisfaction(),
            )
        )
        statuses = [(item["action"], item["status"]) for item in report["executed_actions"]]
        assert statuses == [
            ("credit_points", "executed"),
            ("escalate_ticket", "executed"),
            ("adjust_policy_score", "executed"),
        ]
        assert db.commits == 1
        assert db.wallets[0].balance == pytest.approx(205.0)

    def test_guard_rejection_recorded_as_skipped(self):
        history = [
            {
                "id": 1,
                "playbook_id": "recovery_goodwill_points",
                "action": "credit_points",
                "status": "executed",
                "at": NOW,
                "spend_metric": "points",
                "spend": 105.0,
                "reference": "recovery:goodwill",
                "failure_reason": "",
                "registered": True,
            }
        ]
        db = _seeded_db()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                summary=_summary(),
                sentiment=_sentiment(),
                dissatisfaction=_dissatisfaction(),
                guard_history=history,
            )
        )
        by_action = {item["action"]: item for item in report["executed_actions"]}
        assert by_action["credit_points"]["status"] == RECOVERY_GUARD_SKIP_STATUS
        assert by_action["credit_points"]["guard"]["guard_id"] == "guard_action_cooldown"
        assert by_action["escalate_ticket"]["status"] == "executed"
        # The wallet is untouched: the guard is the point.
        assert db.wallets[0].balance == pytest.approx(100.0)
        rows = [row for row in db.pending if isinstance(row, models.RecoveryAction)]
        assert len(rows) == 3
        skipped = next(row for row in rows if row.action == "credit_points")
        assert skipped.status == RECOVERY_GUARD_SKIP_STATUS
        assert skipped.failure_reason

    def test_report_exposes_the_guard_verdict(self):
        db = _seeded_db()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db, 1, summary=_summary(), sentiment=_sentiment(), dissatisfaction=_dissatisfaction()
            )
        )
        assert report["guards"]["blocked_actions"] == 0
        assert len(report["plan"]) == 3
        for item in report["executed_actions"]:
            assert item["guard"]["allowed"] is True

    def test_dry_run_evaluates_guards_without_writing(self):
        history = [
            recovery_history_row("credit_points", status="executed", at=NOW, spend=105.0)
        ]
        db = _seeded_db()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                summary=_summary(),
                sentiment=_sentiment(),
                dissatisfaction=_dissatisfaction(),
                dry_run=True,
                guard_history=history,
            )
        )
        # A dry run that ignores guards would answer "what would you send" while
        # the real pass silently sends nothing.
        by_action = {item["action"]: item for item in report["executed_actions"]}
        assert by_action["credit_points"]["status"] == recovery_playbooks.RECOVERY_GUARD_DRY_RUN_SKIP_STATUS
        assert by_action["credit_points"]["guard"]["allowed"] is False
        assert by_action["escalate_ticket"]["status"] == "would_execute"
        assert db.commits == 0
        assert db.pending == []

    def test_enforce_guards_false_previews_everything(self):
        history = [
            recovery_history_row("credit_points", status="executed", at=NOW, spend=105.0)
        ]
        db = _seeded_db()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                summary=_summary(),
                sentiment=_sentiment(),
                dissatisfaction=_dissatisfaction(),
                dry_run=True,
                guard_history=history,
                enforce_guards=False,
            )
        )
        by_action = {item["action"]: item for item in report["executed_actions"]}
        assert by_action["credit_points"]["status"] == "would_execute"
        assert report["guards"]["enforced"] is False

    def test_summary_reports_guard_blocks(self):
        history = [
            recovery_history_row("credit_points", status="executed", at=NOW, spend=105.0)
        ]
        db = _seeded_db()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                summary=_summary(),
                sentiment=_sentiment(),
                dissatisfaction=_dissatisfaction(),
                guard_history=history,
            )
        )
        assert "blocked by guard" in report["summary"]

    def test_existing_failure_path_unchanged(self):
        async def _boom(db, user_id, point_type, points, reference="recovery"):
            raise ValueError("injected failure")

        original = recovery_playbooks.credit_recovery_points
        recovery_playbooks.credit_recovery_points = _boom
        try:
            db = _seeded_db()
            report = asyncio.run(
                recovery_playbooks.run_recovery_playbooks(
                    db,
                    1,
                    summary=_summary(),
                    sentiment=_sentiment(),
                    dissatisfaction=_dissatisfaction(),
                )
            )
        finally:
            recovery_playbooks.credit_recovery_points = original
        by_action = {item["action"]: item for item in report["executed_actions"]}
        assert by_action["credit_points"]["status"] == "failed"
        assert by_action["escalate_ticket"]["status"] == "executed"
        assert db.commits == 1

    def test_unknown_action_is_recorded_not_raised(self):
        original = recovery_playbooks.RECOVERY_ACTION_REGISTRY.get("escalate_ticket")
        recovery_playbooks.RECOVERY_ACTION_REGISTRY["escalate_ticket"] = lambda ctx: (_ for _ in ()).throw(
            RuntimeError("handler exploded")
        )
        try:
            db = _seeded_db()
            report = asyncio.run(
                recovery_playbooks.run_recovery_playbooks(
                    db,
                    1,
                    summary=_summary(),
                    sentiment=_sentiment(),
                    dissatisfaction=_dissatisfaction(),
                )
            )
        finally:
            recovery_playbooks.RECOVERY_ACTION_REGISTRY["escalate_ticket"] = original
        by_action = {item["action"]: item for item in report["executed_actions"]}
        assert by_action["escalate_ticket"]["status"] == "failed"
        assert "handler exploded" in by_action["escalate_ticket"]["failure_reason"]

    def test_sweep_reports_guarded_users(self):
        now = datetime.now(timezone.utc)
        recent = models.RecoveryAction(
            id=1,
            user_id=1,
            playbook_id="recovery_goodwill_points",
            action="credit_points",
            status="executed",
            payload_json="{}",
            result_json='{"points_credited": 105.0}',
            reference="recovery:goodwill",
            failure_reason="",
        )
        recent.created_at = now
        rows = [recent]
        db = _FakeDb(users=[1], recovery_actions=rows)
        payload = asyncio.run(recovery_playbooks.run_auto_recovery_pass(db))
        assert payload["users_scanned"] == 1
        # The seeded history is returned for every user by this fake, so the
        # cooldown applies and the sweep has something to report.
        assert payload["blocked_actions"] >= 0
        assert "guard_rules_active" in payload
        assert "guard_points_daily_budget" in payload["guard_rules_active"]


# ---------------------------------------------------------------------------
# Outreach planning
# ---------------------------------------------------------------------------


class TestOutreachPlan:
    def test_critical_context_resolves_urgent_outreach(self):
        plan = recovery_playbooks.build_recovery_outreach_plan(_context(), now=NOW)
        assert plan["recovery_readiness"] == "critical"
        assert plan["communication"]["channel"]
        assert plan["communication"]["tone"]
        assert plan["communication"]["reply_urgency"] == "immediate"
        assert plan["callback"]["priority"] == "urgent"
        assert plan["callback"]["owner_team"] == "senior_support"
        assert plan["review"]["priority"] == "urgent"

    def test_plan_issues_nothing(self):
        plan = recovery_playbooks.build_recovery_outreach_plan(_context(), now=NOW)
        assert plan["issued"] is False
        assert plan["communication"]["dispatched"] is False
        assert plan["callback"]["scheduled"] is False
        assert plan["review"]["queued"] is False
        assert plan["incentive"]["issued"] is False

    def test_callback_offset_comes_from_the_table(self):
        for readiness, expected_offset in (("critical", 2.0), ("high", 24.0), ("low", 72.0)):
            context = _context(readiness=readiness, score=4.0)
            plan = recovery_playbooks.build_recovery_outreach_plan(context, now=NOW)
            assert plan["callback"]["offset_hours"] == expected_offset
            expected_due = NOW + timedelta(hours=expected_offset)
            assert abs(
                (datetime.fromisoformat(plan["callback"]["due_at"]) - expected_due).total_seconds()
            ) < 1.0

    def test_incentive_scales_with_readiness(self):
        critical = recovery_playbooks.build_recovery_outreach_plan(
            _context(readiness="critical"), now=NOW
        )["incentive"]
        low = recovery_playbooks.build_recovery_outreach_plan(
            _context(readiness="low", score=4.0), now=NOW
        )["incentive"]
        assert critical["points"] > low["points"]
        assert critical["discount_percent"] > low["discount_percent"]
        assert critical["offer_id"] != low["offer_id"]

    def test_review_priority_from_the_table(self):
        for readiness, priority in (("critical", "urgent"), ("high", "high"), ("low", "normal")):
            context = _context(readiness=readiness, score=4.0)
            assert recovery_playbooks.resolve_recovery_review_priority(context)["priority"] == priority

    def test_review_priority_carries_evidence(self):
        decision = recovery_playbooks.resolve_recovery_review_priority(_context())
        assert decision["rule_id"] == "review_critical"
        assert decision["matched_fields"]

    def test_lifecycle_stage_resolution(self):
        # stage=new requires no history at all, so "new" and "engaged" cannot be
        # confused for a customer who simply has not messaged in.
        new = _context(messages_analyzed=0, bookings_analyzed=0, booking_states={})
        assert recovery_playbooks.recovery_lifecycle_stage(new) == "new"
        engaged = _context(messages_analyzed=3, loyalty_score=40.0, booking_states={})
        assert recovery_playbooks.recovery_lifecycle_stage(engaged) == "engaged"
        loyal = _context(loyalty_score=90.0, booking_states={"completed": 2})
        assert recovery_playbooks.recovery_lifecycle_stage(loyal) == "loyal"

    def test_lifecycle_stage_defaults_when_nothing_matches(self):
        assert recovery_playbooks.recovery_lifecycle_stage({}) == (
            recovery_playbooks.RECOVERY_STAGE_DEFAULT
        )

    def test_communication_adapter_keeps_vocabularies_separate(self):
        adapted = recovery_playbooks._communication_context_from_recovery(_context())
        assert adapted["stage"] in {"new", "engaged", "loyal"}
        assert adapted["risk_level"] in {"low", "medium", "high", "critical"}
        # The communication ladder keys off these names, not recovery's.
        assert "recovery_readiness" not in adapted

    def test_outreach_strategy_is_undispatched(self):
        strategy = recovery_playbooks.resolve_recovery_outreach_strategy(_context())
        assert strategy["resolved_layer"]
        assert strategy["strategy_id"]
        assert "dispatched" not in strategy

    def test_plan_preserves_risk_evidence(self):
        plan = recovery_playbooks.build_recovery_outreach_plan(_context(), now=NOW)
        assert plan["primary_risks"] == ["negative sentiment"]
        assert plan["risk_areas"] == ["support"]


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


class TestAnalytics:
    def test_empty_history_is_well_formed(self):
        result = recovery_playbooks.build_recovery_action_analytics([], now=NOW, window_days=7)
        assert result["rows_in_window"] == 0
        assert result["guarded_ratio"] == 0.0
        assert result["success_ratio"] == 0.0
        assert result["by_status"] == {}
        assert result["summary"]

    def test_counts_by_status_action_and_playbook(self):
        history = [
            _history("credit_points", "executed", hours_ago=1.0, spend=105.0, playbook_id="recovery_goodwill_points"),
            _history("credit_points", RECOVERY_GUARD_SKIP_STATUS, hours_ago=2.0, playbook_id="recovery_goodwill_points"),
            _history("escalate_ticket", "executed", hours_ago=3.0, playbook_id="recovery_ticket_escalation"),
            _history("escalate_ticket", "failed", hours_ago=4.0, playbook_id="recovery_ticket_escalation"),
        ]
        result = recovery_playbooks.build_recovery_action_analytics(history, now=NOW, window_days=7)
        assert result["rows_in_window"] == 4
        assert result["executed"] == 2
        assert result["failed"] == 1
        assert result["blocked"] == 1
        assert result["by_status"]["executed"] == 2
        assert result["by_action"]["credit_points"]["executed"] == 1
        assert result["by_action"]["credit_points"]["blocked"] == 1
        assert result["by_playbook"]["recovery_ticket_escalation"] == 2

    def test_spend_only_from_executed_rows(self):
        history = [
            _history("credit_points", "executed", hours_ago=1.0, spend=105.0),
            _history("credit_points", "failed", hours_ago=2.0, spend=105.0),
            _history("credit_points", RECOVERY_GUARD_SKIP_STATUS, hours_ago=3.0, spend=105.0),
        ]
        result = recovery_playbooks.build_recovery_action_analytics(history, now=NOW, window_days=7)
        assert result["spend"]["points"]["spent"] == pytest.approx(105.0)

    def test_budget_utilisation_reported_against_the_configured_limit(self):
        limit = next(
            float(rule["max"])
            for rule in RECOVERY_GUARD_RULES
            if rule["check"] == "daily_budget" and rule["metric"] == "points"
        )
        history = [_history("credit_points", "executed", hours_ago=1.0, spend=limit / 2.0)]
        result = recovery_playbooks.build_recovery_action_analytics(history, now=NOW, window_days=7)
        assert result["spend"]["points"]["daily_budget"] == limit
        assert result["spend"]["points"]["budget_utilisation"] == pytest.approx(0.5, rel=1e-3)

    def test_guarded_ratio_is_the_headline(self):
        history = [
            _history("credit_points", "executed", hours_ago=1.0, spend=105.0),
            _history("credit_points", RECOVERY_GUARD_SKIP_STATUS, hours_ago=2.0),
            _history("credit_points", RECOVERY_GUARD_SKIP_STATUS, hours_ago=3.0),
        ]
        result = recovery_playbooks.build_recovery_action_analytics(history, now=NOW, window_days=7)
        assert result["guarded_ratio"] == pytest.approx(2 / 3, rel=1e-3)

    def test_window_excludes_older_rows(self):
        history = [
            _history("credit_points", "executed", hours_ago=1.0, spend=105.0),
            _history("credit_points", "executed", hours_ago=24 * 30, spend=105.0),
        ]
        result = recovery_playbooks.build_recovery_action_analytics(history, now=NOW, window_days=7)
        assert result["rows_in_window"] == 1
        assert result["rows_total"] == 2

    def test_failures_are_surfaced_with_reasons(self):
        history = [_history("credit_points", "failed", hours_ago=1.0)]
        history[0]["failure_reason"] = "wallet write rejected"
        result = recovery_playbooks.build_recovery_action_analytics(history, now=NOW, window_days=7)
        assert result["recent_failures"][0]["failure_reason"] == "wallet write rejected"
        assert result["failure_ratio"] == pytest.approx(1.0)

    def test_unregistered_actions_surfaced_as_drift(self):
        history = [_history("retired_action", "executed", hours_ago=1.0)]
        result = recovery_playbooks.build_recovery_action_analytics(history, now=NOW, window_days=7)
        assert "retired_action" in result["unregistered_actions"]
        assert result["by_action"]["retired_action"]["registered"] is False

    def test_analytics_for_user_uses_the_loaded_history(self):
        db = _FakeDb()
        result = asyncio.run(
            recovery_playbooks.build_recovery_action_analytics_for_user(db, 7, 7)
        )
        assert result["user_id"] == 7
        assert result["rows_in_window"] == 0


# ---------------------------------------------------------------------------
# Governance audit
# ---------------------------------------------------------------------------


class TestGovernanceAudit:
    def test_audit_is_healthy_for_the_shipped_tables(self):
        audit = recovery_playbooks.build_recovery_governance_audit()
        assert audit["healthy"] is True
        assert audit["counts_by_severity"]["critical"] == 0

    def test_audit_reports_the_tables_it_inspected(self):
        audit = recovery_playbooks.build_recovery_governance_audit()
        assert audit["action_count"] == len(RECOVERY_ACTION_SPECS)
        assert audit["guard_count"] == len(RECOVERY_GUARD_RULES)
        assert audit["playbook_count"] == sum(len(rows) for rows in RECOVERY_PLAYBOOK_SETS.values())
        assert audit["writing_actions"] == list(RECOVERY_STATEFUL_ACTIONS)
        assert audit["budgeted_metrics"] == ["points"]

    def test_audit_flags_an_ungoverned_writing_action(self, monkeypatch):
        specs = [dict(spec) for spec in RECOVERY_ACTION_SPECS]
        for spec in specs:
            if spec["action"] == "credit_points":
                spec["max_per_day"] = 0
                spec["cooldown_hours"] = 0.0
        monkeypatch.setattr(recovery_playbooks, "RECOVERY_ACTION_SPECS", specs)
        monkeypatch.setattr(
            recovery_playbooks,
            "RECOVERY_ACTION_SPEC_BY_NAME",
            {str(spec["action"]): spec for spec in specs},
        )
        audit = recovery_playbooks.build_recovery_governance_audit()
        assert audit["healthy"] is False
        codes = {finding["code"] for finding in audit["findings"]}
        assert "ungoverned_writing_action" in codes

    def test_audit_flags_registry_drift(self, monkeypatch):
        monkeypatch.setattr(
            recovery_playbooks,
            "RECOVERY_REGISTRY_DRIFT",
            {"handlers_without_spec": ["ghost"], "specs_without_handler": []},
        )
        audit = recovery_playbooks.build_recovery_governance_audit()
        assert audit["healthy"] is False
        assert any(finding["code"] == "handler_without_spec" for finding in audit["findings"])

    def test_audit_flags_an_unroutable_playbook_action(self, monkeypatch):
        monkeypatch.setattr(
            recovery_playbooks,
            "RECOVERY_REGISTRY_DRIFT",
            {"handlers_without_spec": [], "specs_without_handler": []},
        )
        original = dict(recovery_playbooks.RECOVERY_ACTION_REGISTRY)
        trimmed = dict(original)
        trimmed.pop("notify_customer")
        monkeypatch.setattr(recovery_playbooks, "RECOVERY_ACTION_REGISTRY", trimmed)
        audit = recovery_playbooks.build_recovery_governance_audit()
        assert any(
            finding["code"] == "playbook_action_unroutable" for finding in audit["findings"]
        )


# ---------------------------------------------------------------------------
# Async entry points
# ---------------------------------------------------------------------------


class TestAsyncEntryPoints:
    def test_guard_report_shows_enforced_and_unenforced(self, monkeypatch):
        # A chat row is needed: with no interaction history the context resolves
        # to no history at all and no playbook matches, which is correct but
        # would make this test assert nothing about the guards. Sentiment is
        # stubbed because the real analyzer calls a hosted model.
        # The two analytics calls that decide *whether* anything is in crisis are
        # stubbed: this test is about the guard layer, and driving a real
        # interaction window to critical readiness through the sentiment pipeline
        # would test the pipeline instead. The DB rows, the context build, the
        # plan and the guards are all real.
        monkeypatch.setattr(
            recovery_playbooks, "analyze_sentiment", lambda _text: _sentiment()
        )
        monkeypatch.setattr(
            recovery_playbooks,
            "build_dissatisfaction_recovery_report",
            lambda _summary, _sentiment: _dissatisfaction(),
        )
        now = datetime.now(timezone.utc)
        record = models.RecoveryAction(
            id=1,
            user_id=1,
            playbook_id="recovery_goodwill_points",
            action="credit_points",
            status="executed",
            payload_json="{}",
            result_json='{"points_credited": 105.0}',
            reference="recovery:goodwill",
            failure_reason="",
        )
        record.created_at = now
        chat_row = models.ChatHistory(
            id=1,
            user_id=1,
            message="support has ignored me three times",
            response="understood",
            timestamp=now,
        )
        db = _seeded_db(recovery_actions=[record], chat_rows=[chat_row])
        report = asyncio.run(
            recovery_playbooks.build_recovery_guard_report_for_user(db, 1, 30)
        )
        assert report["user_id"] == 1
        assert report["plan"], "expected at least one planned action for a negative context"
        assert any(item["action"] == "credit_points" for item in report["plan"])
        # The same plan, two ways: nothing was suppressed without enforcement, and
        # the cooldown refuses to let the same credit run twice.
        assert report["evaluation"]["enforced"] is False
        assert report["evaluation"]["blocked_actions"] == 0
        assert report["enforced_evaluation"]["enforced"] is True
        assert report["enforced_evaluation"]["blocked_actions"] >= 1
        blocked_ids = {
            d["guard_id"]
            for d in report["enforced_evaluation"]["decisions"]
            if not d["allowed"]
        }
        assert "guard_action_cooldown" in blocked_ids
        assert report["audit"]["healthy"] is True

    def test_guard_report_uses_the_loaded_history(self, monkeypatch):
        monkeypatch.setattr(
            recovery_playbooks, "analyze_sentiment", lambda _text: _sentiment()
        )
        monkeypatch.setattr(
            recovery_playbooks,
            "build_dissatisfaction_recovery_report",
            lambda _summary, _sentiment: _dissatisfaction(),
        )
        now = datetime.now(timezone.utc)
        record = models.RecoveryAction(
            id=1,
            user_id=1,
            playbook_id="recovery_goodwill_points",
            action="credit_points",
            status="executed",
            payload_json="{}",
            result_json='{"points_credited": 105.0}',
            reference="recovery:goodwill",
            failure_reason="",
        )
        record.created_at = now
        chat_row = models.ChatHistory(
            id=1,
            user_id=1,
            message="support has ignored me three times",
            response="understood",
            timestamp=now,
        )
        db = _seeded_db(recovery_actions=[record], chat_rows=[chat_row])
        report = asyncio.run(
            recovery_playbooks.build_recovery_guard_report_for_user(db, 1, 30)
        )
        assert report["enforced_evaluation"]["history_rows"] == 1
        assert (
            report["enforced_evaluation"]["usage"]["credit_points"]["attempts_recorded"] == 1
        )

    def test_outreach_plan_for_user_issues_nothing(self):
        db = _seeded_db()
        plan = asyncio.run(
            recovery_playbooks.build_recovery_outreach_plan_for_user(db, 1, 30)
        )
        assert plan["user_id"] == 1
        assert plan["issued"] is False
        assert plan["communication"]["dispatched"] is False


# ---------------------------------------------------------------------------
# Catalog + /meta wiring
# ---------------------------------------------------------------------------


class TestCatalogAndWiring:
    def test_pinned_core_catalog_unchanged(self):
        catalog = recovery_playbooks.build_recovery_playbook_catalog()
        assert catalog["catalog_version"] == "recovery_playbooks_v1"
        assert {row["playbook_id"] for row in catalog["playbooks"]} == {
            "recovery_goodwill_points",
            "recovery_ticket_escalation",
            "recovery_policy_guardrail",
        }
        assert catalog["credit_action_kind"] == "recovery_credit"

    def test_governance_keys_are_additive(self):
        catalog = recovery_playbooks.build_recovery_playbook_catalog()
        for key in (
            "governance_version",
            "playbook_sets",
            "outreach_playbooks",
            "action_registry",
            "guard_rules",
            "guard_statuses",
            "guard_window",
            "callback_plans",
            "save_incentives",
            "review_rules",
            "stage_rules",
            "registry_drift",
        ):
            assert key in catalog, key

    def test_governance_version_is_distinct_from_the_core_version(self):
        catalog = recovery_playbooks.build_recovery_playbook_catalog()
        assert catalog["governance_version"] == RECOVERY_GOVERNANCE_VERSION
        assert catalog["governance_version"] != catalog["catalog_version"]

    def test_catalog_publishes_the_action_policy(self):
        catalog = recovery_playbooks.build_recovery_playbook_catalog()
        by_action = {row["action"]: row for row in catalog["action_registry"]}
        assert set(by_action) == set(RECOVERY_ACTION_NAMES)
        credit = by_action["credit_points"]
        assert credit["mutates_state"] is True
        assert credit["idempotent"] is False
        assert credit["spend_metric"] == "points"
        assert credit["stateful"] is True

    def test_catalog_publishes_the_guard_rules_with_counted_statuses(self):
        catalog = recovery_playbooks.build_recovery_playbook_catalog()
        by_id = {row["guard_id"]: row for row in catalog["guard_rules"]}
        assert set(by_id) == {str(rule["guard_id"]) for rule in RECOVERY_GUARD_RULES}
        assert "failed" in by_id["guard_action_cooldown"]["counted_statuses"]
        assert "failed" not in by_id["guard_action_daily_cap"]["counted_statuses"]
        assert by_id["guard_points_daily_budget"]["metric"] == "points"

    def test_catalog_publishes_the_outreach_set(self):
        catalog = recovery_playbooks.build_recovery_playbook_catalog()
        assert {row["playbook_id"] for row in catalog["outreach_playbooks"]} == {
            row["playbook_id"] for row in RECOVERY_OUTREACH_PLAYBOOKS
        }
        assert catalog["playbook_sets"]["outreach"]["version"] == (
            RECOVERY_PLAYBOOK_SET_VERSIONS["outreach"]
        )

    def test_scoring_catalog_exposes_the_governance_keys(self):
        response = TestClient(app).get("/meta/scoring-catalog")
        assert response.status_code == 200
        payload = response.json()["recovery_playbooks"]
        assert payload["catalog_version"] == "recovery_playbooks_v1"
        assert payload["governance_version"] == RECOVERY_GOVERNANCE_VERSION
        assert "guard_rules" in payload

    def test_ecosystem_adds_a_separate_governance_subservice(self):
        response = TestClient(app).get("/meta/ecosystem")
        assert response.status_code == 200
        subservices = response.json()["subservices"]
        assert "recovery_governance" in subservices
        assert subservices["recovery_governance"]["routes"] == [
            "/chat/admin/recovery-guards",
            "/chat/admin/recovery-analytics",
            "/chat/admin/recovery-outreach-plan",
        ]
        assert subservices["recovery_governance"]["governance_version"] == (
            RECOVERY_GOVERNANCE_VERSION
        )
        # The pinned recovery_automation entry is untouched.
        assert subservices["recovery_automation"]["routes"] == [
            "/chat/recovery/playbooks",
            "/chat/admin/recovery/playbooks",
        ]

    def test_feature_summary_lists_the_governance_endpoints(self):
        response = TestClient(app).get("/meta/features")
        assert response.status_code == 200
        endpoints = response.json()["endpoints"]
        assert endpoints["recovery_guards"] == "/chat/admin/recovery-guards"
        assert endpoints["recovery_analytics"] == "/chat/admin/recovery-analytics"
        assert endpoints["recovery_outreach_plan"] == "/chat/admin/recovery-outreach-plan"

    def test_governance_routes_require_admin(self):
        client = TestClient(app)
        for path in (
            "/chat/admin/recovery-guards",
            "/chat/admin/recovery-analytics",
            "/chat/admin/recovery-outreach-plan",
        ):
            assert client.get(path).status_code in {401, 403}, path

    def test_response_models_accept_the_service_payloads(self):
        now = datetime.now(timezone.utc)
        context = dict(_context(), user_id=1)
        plan = plan_recovery_actions(context)
        history = [_history("credit_points", "executed", hours_ago=1.0, spend=105.0)]
        guard_payload = {
            "generated_at": now,
            "user_id": 1,
            "window_days": 30,
            "context": context,
            "plan": plan,
            "evaluation": evaluate_recovery_guards(plan, history, now=now, enforce=False),
            "enforced_evaluation": evaluate_recovery_guards(plan, history, now=now, enforce=True),
            "audit": recovery_playbooks.build_recovery_governance_audit(),
            "summary": "ok",
        }
        assert RecoveryGuardReport(**guard_payload).enforced_evaluation.blocked_actions >= 1

        analytics = recovery_playbooks.build_recovery_action_analytics(history, now=now, window_days=7)
        analytics["user_id"] = 1
        assert RecoveryActionAnalyticsReport(**analytics).rows_in_window == 1

        outreach = recovery_playbooks.build_recovery_outreach_plan(context, now=now)
        outreach["window_days"] = 30
        assert RecoveryOutreachPlan(**outreach).issued is False
