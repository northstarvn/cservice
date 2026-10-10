"""Whether a rule pack can actually do anything, asked of the pack's own config.

A rule pack is data: a name, some ``when`` clauses, some ``params``. It is
therefore possible to ship one that is fully configured, passes every validation
in ``rule_engine``, and can never fire -- because nothing selects it, or because
the context its caller builds does not contain the fields its rules read.

That failure mode is invisible to every test that checks the engine, because the
engine is correct. It is also the most expensive kind of dead code: a rule like
``suppress_recent_complaint`` *reads* as a safeguard against pestering a customer
who has already complained twice, and a reviewer seeing it in the catalog
believes the safeguard exists.

Three things were already wrong in this tree before this file existed, all found
by asking the pack instead of the engine:

* ``suppress_recent_complaint`` needs ``complaints_last_30d``.
  ``_with_complaint_history`` loads exactly that key from the complaint ledger --
  and had, in a docstring, documented fixing the rule once already. Two frames
  later ``_communication_context_from_recovery`` rebuilt the context from a fixed
  dict and dropped it. The query ran, the number was right, the rule stayed dead.
* ``loyalty_retention`` is a complete pack with two enabled rules and **no
  production caller at all**.
* ``seasonal_campaigns`` likewise. Its single rule is date-gated to Jun-Aug, so
  it is doubly unreachable: nothing calls it, and today is October.

So the tests here are deliberately written against *declared data* rather than
against behaviour, because that is what catches this class. A behavioural test
("does the pack select anything for this persona?") is satisfied by any context
the test itself built, which is how the original gap survived: the simulator's
context had ``days_since_last_activity`` while the rule reads
``days_since_login``, and every probe reported success.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any, Optional

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"


# ---------------------------------------------------------------------------
# Reading what a pack declares it needs
# ---------------------------------------------------------------------------


def _required_fields(pack: dict[str, Any]) -> set[str]:
    """Every context field this pack's rules read, at any nesting depth.

    Derived from the ``when`` clauses rather than written down, which is the
    whole point: a hardcoded list is a list somebody has to remember to extend,
    and the failure this file exists to catch is precisely a mismatch between
    the list and the clauses.

    **It descends into ``all``/``any``/``not``, and that is not optional.**
    The first version read only the top level, which worked only while every
    clause happened to be flat. ``retention_high_risk`` tests its stage with
    ``{"any": [{"stage": "engaged"}, {"stage": "at_risk"}]}`` -- there is no
    membership operator in this DSL -- so the shallow read returned ``{"any",
    "churn_risk"}`` and never mentioned ``stage``.

    That is the exact failure this helper is supposed to detect, inverted: it
    made ``stage`` *invisible*, so a caller that does not supply ``stage`` would
    pass. A helper that misses fields makes the adapter look correct precisely
    when the field it needs is absent.
    """
    fields: set[str] = set()
    combinators = {"all", "any", "not"}
    for rule in pack.get("rules", ()):
        _collect_fields(rule.get("when") or {}, fields, combinators)
    return fields


def _collect_fields(node: Any, into: set[str], combinators: set[str]) -> None:
    if not isinstance(node, dict):
        return
    for name, value in node.items():
        if str(name) in combinators:
            branches = value if isinstance(value, list) else [value]
            for branch in branches:
                _collect_fields(branch, into, combinators)
            continue
        into.add(str(name))


def _enabled_rules(pack: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in pack.get("rules", ()) if r.get("enabled", True)]


# ---------------------------------------------------------------------------
# Who calls select_rules, and on what
# ---------------------------------------------------------------------------


def _select_rules_call_sites() -> list[tuple[str, Optional[str]]]:
    """``(file, pack name)`` for every ``select_rules`` call in ``app/``.

    The pack name is the literal argument where there is one, and ``None`` where
    the caller passes a pack object -- which is a *different* situation, not an
    absent one, because an inline pack is evaluated by definition.
    """
    sites: list[tuple[str, Optional[str]]] = []
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable file
            continue
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "select_rules"
            ):
                continue
            if not node.args:
                sites.append((path.name, None))
                continue
            first = node.args[0]
            sites.append(
                (path.name, str(first.value) if isinstance(first, ast.Constant) else None)
            )
    return sites


def _pack_names_built_inline() -> set[str]:
    """Names of packs constructed as dicts and handed straight to the engine.

    ``complaints.complaint_escalation_pack()`` is one: it is evaluated by
    ``select_rules(pack_dict, ...)`` and is deliberately *not* in
    ``RULE_PACK_BY_NAME``, so a reachability check that only looked at the
    registry would call the complaint engine unreferenced.
    """
    return {
        "complaint_escalation",
    }


class TestAPackIsNotJustWellFormed:
    """Registry hygiene -- the cheap half, and the half that was already green."""

    def test_every_registered_pack_has_at_least_one_enabled_rule(self):
        from app import rule_engine

        for name in rule_engine.RULE_PACK_BY_NAME:
            pack = rule_engine.get_rule_pack(name)
            enabled = _enabled_rules(pack)
            assert enabled, (
                f"{name!r} is registered with no enabled rule, so it cannot fire "
                "and cannot be reported as not firing -- it is simply inert"
            )

    def test_every_rule_names_the_fields_it_reads(self):
        """A rule with an empty ``when`` fires for everyone, silently.

        Not flagged as a defect -- a rule with no conditions may be deliberate.
        Flagged so the count is visible, because "fires always" and "can never
        fire" are the same green in every other check here.
        """
        from app import rule_engine

        unconditional: dict[str, list[str]] = {}
        for name in rule_engine.RULE_PACK_BY_NAME:
            for rule in _enabled_rules(rule_engine.get_rule_pack(name)):
                if not (rule.get("when") or {}):
                    unconditional.setdefault(name, []).append(str(rule.get("id")))
        assert unconditional == {}, (
            "these enabled rules have no conditions and therefore fire for "
            f"everybody: {unconditional}"
        )


class TestAConfiguredPackIsNotTheSameThingAsAReachableOne:
    """The finding: two of the three registered packs are selected by nothing."""

    def test_the_registered_packs_and_which_of_them_production_selects(self):
        from app import rule_engine

        called = {name for _, name in _select_rules_call_sites() if name}
        inline = _pack_names_built_inline()
        registered = set(rule_engine.RULE_PACK_BY_NAME)

        unreachable = sorted(registered - called)

        # The claim, stated so a reader can check it rather than trust it.
        assert called == {"communication_suppression"}, (
            "the set of packs production code selects has changed. If a pack "
            "moved into `called`, this test's assertion below is now stale and "
            f"the unreachable list should shrink. Called: {sorted(called)}"
        )
        assert unreachable == ["loyalty_retention", "seasonal_campaigns"], (
            "these packs are registered, well-formed and evaluated by nothing: "
            f"{unreachable}. Each is a rule somebody reviewed and believed in."
        )
        # `complaint_escalation` is evaluated but not registered; that is fine,
        # and this is the assertion that keeps it from being reported as dead.
        assert "complaint_escalation" in inline

    def test_an_unreachable_pack_is_reported_rather_than_silently_passed(self):
        """`rule_engine`'s own audit should surface this, and today does not.

        This test is the *reporting* half. It fails today -- which is the point:
        a finding nobody is told about is the same as no finding.
        """
        from app import rule_engine

        report = rule_engine.pack_reachability_report()

        assert report["unreachable_packs"] == [
            "loyalty_retention",
            "seasonal_campaigns",
        ], report
        assert report["reachable_packs"] == ["communication_suppression"], report
        # Every unreachable pack must say *why*, so the report is actionable.
        for name in report["unreachable_packs"]:
            reason = report["reasons"].get(name, "")
            assert reason, f"{name} is unreachable with no stated reason"


class TestTheContextAnAdapterBuildsMustContainWhatThePackReads:
    """The precise defect, pinned at the frame that broke it."""

    def test_the_communication_adapter_supplies_every_field_the_pack_reads(self):
        """The general shape: adapter output ⊇ the pack's required fields.

        Written against ``_communication_context_from_recovery``'s *output*
        rather than against a hand-listed set of keys, so adding a rule that
        reads a new field makes this fail until the adapter carries it.
        """
        from app import rule_engine
        from app.services.recovery_playbooks import _communication_context_from_recovery

        pack = rule_engine.get_rule_pack("communication_suppression")
        needed = _required_fields(pack)

        # A realistic context: the one `_with_complaint_history` produces.
        produced = _communication_context_from_recovery(
            {
                "recovery_readiness": "high",
                "churn_risk": "high",
                "sentiment_label": "negative",
                "complaints_last_30d": 3,
            }
        )

        missing = sorted(needed - set(produced))
        assert missing == [], (
            "the suppression pack reads fields the adapter does not carry, so "
            f"those rules can never fire: {missing}. Every field the pack reads "
            f"is {sorted(needed)}."
        )

    def test_the_recent_complaint_suppression_actually_fires_now(self):
        """The behavioural half, for the one rule that was dead.

        Two complaints in thirty days, a non-positive sentiment and a churn risk
        that is not low: ``suppress_recent_complaint`` must fire. Before the
        adapter carried ``complaints_last_30d`` this selected nothing, and the
        probe that covered it asserted only that selection did not raise.
        """
        from app import rule_engine
        from app.services.recovery_playbooks import (
            _communication_context_from_recovery,
            resolve_recovery_outreach_strategy,
        )

        context = {
            "recovery_readiness": "high",
            "churn_risk": "high",
            "sentiment_label": "negative",
            "complaints_last_30d": 3,
        }

        selection = rule_engine.select_rules(
            "communication_suppression", _communication_context_from_recovery(context)
        )
        fired = [entry["id"] for entry in selection["fired"]]
        assert "suppress_recent_complaint" in fired, (
            f"the rule still does not fire; selection was {selection}"
        )

        # And it must reach the answer the operator reads, not just the engine's
        # intermediate. A fix that stops one layer short of the payload is the
        # same defect one frame later.
        strategy = resolve_recovery_outreach_strategy(context, locale="global")
        assert "suppress_recent_complaint" in strategy.get("suppression_rules", []), (
            f"the rule fired but the strategy does not say so: {strategy}"
        )

    def test_a_failure_to_count_leaves_the_key_absent_rather_than_zero(self):
        """The safeguard must not be switchable by a database problem.

        ``_with_complaint_history`` deliberately omits the key when the count
        cannot be read, because the evaluator fails closed on an absent field.
        This pins that the adapter does not invent a zero on the way through --
        which would turn "we could not check" into "they have not complained".
        """
        from app.services.recovery_playbooks import _communication_context_from_recovery

        produced = _communication_context_from_recovery(
            {"recovery_readiness": "high", "complaints_last_30d_error": "boom"}
        )
        assert "complaints_last_30d" not in produced, (
            "the adapter invented a complaint count. A database failure must "
            f"leave the key absent, not zero: {produced}"
        )

    def test_the_absent_key_still_suppresses_nothing_rather_than_everything(self):
        """Absent means "cannot fire", and the direction of that is checked.

        The rule is a *suppression*, so an absent field that matched would be the
        dangerous direction. Asserted explicitly because "fails closed" is
        ambiguous about which way is closed when the predicate is a threshold.
        """
        from app import rule_engine
        from app.services.recovery_playbooks import _communication_context_from_recovery

        selection = rule_engine.select_rules(
            "communication_suppression",
            _communication_context_from_recovery(
                {"recovery_readiness": "high", "churn_risk": "high"}
            ),
        )
        fired = [entry["id"] for entry in selection["fired"]]
        assert "suppress_recent_complaint" not in fired, (
            f"an unreadable complaint count suppressed the outreach anyway: {fired}"
        )


class TestTheRegisteredPackThatNothingSelectsIsNotSilentlyWellFormed:
    """`loyalty_retention` deserves its own case because it *would* fire.

    If its fields were supplied, its rules match the persona catalog. That is
    what makes it a real loss rather than dead configuration: the behaviour was
    written, reviewed, given a priority and a discount expression, and then
    nothing asked for it.
    """

    def test_its_rules_would_fire_if_a_context_carried_its_fields(self):
        """Now that both layers of the rule are fixed, it fires for real.

        Two separate defects sat in this one rule, and each alone was enough to
        keep it inert -- which is why finding them needed the reachability check
        rather than a test of the engine:

        * its ``when`` used ``{"stage": {"in": [...]}}``; there is no ``in``
          operator in this DSL, so the clause failed closed for everyone.
        * its ``discount_pct`` used ``stage == 'at_risk'``; the expression
          language rejected every string constant, so the rule would have raised
          out of ``select_rules`` on its first match.

        Fixing only the first still leaves a rule that crashes its caller the
        moment it does its job, which is why the discount is asserted here too.
        """
        from app import rule_engine

        pack = rule_engine.get_rule_pack("loyalty_retention")
        needed = _required_fields(pack)
        assert {"churn_risk", "stage"} <= needed, needed

        # A context with exactly the fields the pack reads, for a persona the
        # catalog already contains.
        selection = rule_engine.select_rules(
            pack,
            {"churn_risk": "high", "stage": "engaged", "days_since_login": 3, "at_risk": False},
        )
        fired = [entry["id"] for entry in selection["fired"]]
        assert fired == ["retention_high_risk"], (
            "the claim in this test's name is wrong -- the pack's own rules do "
            f"not fire even when fed their own fields, so it is not 'a working "
            f"pack nothing calls'. Selection: {selection}"
        )

    def test_and_the_discount_it_computes_is_the_authored_one(self):
        """The param expression resolves, and the two stages differ.

        ``"=15 + 5 if stage == 'at_risk' else 0"`` is the whole reason this rule
        existed -- the at-risk customer gets more than the engaged one. That only
        reads as a promise if the expression can name an enum value, which is
        what the string-constant change bought.
        """
        from app import rule_engine

        def _discount(stage: str) -> Any:
            selection = rule_engine.select_rules(
                "loyalty_retention",
                {"churn_risk": "high", "stage": stage, "days_since_login": 3, "at_risk": False},
            )
            assert selection["fired_ids"] == ["retention_high_risk"], selection
            return selection["fired"][0]["params"]["discount_pct"]

        assert _discount("at_risk") == 20
        assert _discount("engaged") == 0

    def test_a_stage_outside_the_authored_pair_does_not_fire(self):
        """The fix must not have widened the rule to match any stage."""
        from app import rule_engine

        for stage in ("dormant", "churned", ""):
            selection = rule_engine.select_rules(
                "loyalty_retention",
                {"churn_risk": "high", "stage": stage, "days_since_login": 3, "at_risk": False},
            )
            assert selection["fired_ids"] == [], (stage, selection["fired_ids"])

    def test_and_no_production_path_supplies_those_fields_to_it(self):
        from app import rule_engine

        pack = rule_engine.get_rule_pack("loyalty_retention")
        needed = _required_fields(pack)
        producers = {
            name
            for _, name in _select_rules_call_sites()
            if name == "loyalty_retention"
        }
        assert not producers, (
            "something now selects loyalty_retention; this test and the "
            "reachability assertion both need revisiting"
        )
        # `days_since_login` is the field with no producer anywhere in app/, which
        # is the second half of why the pack is inert even if it were called.
        everywhere = set()
        for path in sorted(APP.rglob("*.py")):
            if path.name in {"rule_engine.py", "real_life_flows.py"}:
                # rule_engine.py: the rule's own declaration, not a producer
                # real_life_flows.py: simulator infrastructure, not a production producer
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Dict)
                    and any(
                        isinstance(k, ast.Constant) and k.value == "days_since_login"
                        for k in node.keys
                    )
                ):
                    everywhere.add(path.name)
        assert not everywhere, (
            f"{sorted(everywhere)} now produce days_since_login; the pack's "
            "dormancy rule is one step closer to being reachable"
        )
        assert "days_since_login" in needed, needed


class TestTheShippedCatalogMetTheValidatorOnlyAfterThisFileExisted:
    """The built-in rules were never run through the checks written for them.

    ``validate_when`` had been rejecting ``unknown_operator`` since it existed,
    and every one of its callers pointed it at a *tenant-authored* rule arriving
    from the admin surface. ``RULE_PACKS`` -- the rules the product itself ships
    -- was the one collection that had never met the validator that exists to
    keep rules honest. And it contained a rule the validator would have rejected.
    """

    def test_every_shipped_rule_passes_its_own_validator(self):
        from app import rule_engine

        report = rule_engine.catalog_validation_report()

        assert report["valid"] is True, (
            "the product's own rule catalog contains rules that fail the "
            "validator written for rules: "
            f"{report['invalid_rules']}"
        )
        assert report["validated"] >= 6, (
            f"only {report['validated']} rules were validated; the catalog may "
            "have grown without the report following it"
        )

    def test_the_high_risk_rule_no_longer_names_an_operator_that_does_not_exist(self):
        """Pinned by operator, not by text.

        A substring check on the source would be satisfied by a comment
        explaining the defect, and ``in`` is a substring of ``int``, ``contains``
        and ``in_phrase``. The check that cannot be fooled is the validator's own
        verdict, because it is the thing that was wrong.
        """
        from app import rule_engine

        when = rule_engine.get_rule_pack("loyalty_retention")["rules"][0]["when"]
        operators = rule_engine.validate_when(when)["operators_used"]
        assert "in" not in operators, (
            f"'in' is not implemented by any operator family, and it was in this "
            f"clause: {when}"
        )
        # And the rule still means what it meant: stage is one of two values.
        for stage, expected in (("engaged", True), ("at_risk", True), ("dormant", False)):
            matched = rule_engine.evaluate_when_extended(when, {"churn_risk": "high", "stage": stage})[0]
            assert matched is expected, (stage, when, matched)

    def test_a_param_expression_the_catalog_could_not_evaluate_is_caught(self):
        """``params`` are resolved eagerly on a match, so they must validate too.

        Every pre-existing ``validate_when`` call validates a ``when``. The
        ``=expr`` in ``params`` was resolved at *selection* time by
        ``resolve_params``, unguarded -- so the catalog shipped an expression the
        language could not evaluate, and it raised from inside ``select_rules``
        the first time that rule did its job.

        The expression is one the evaluator refuses for a reason that is a *name*
        check rather than an invented scope value, because the check must hold
        without knowing what any context field will be worth. ``__import__`` is
        not a safe call, so it is unbound at selection time under any scope.
        """
        from app import rule_engine

        names, complaints = rule_engine.expression_free_names("__import__('os').system('true')")
        assert names == {"__import__"}, names
        assert complaints, "the expression references nothing that will be bound"

        broken = {
            "pack": "scratch",
            "version": 1,
            "rules": [
                {
                    "id": "unevaluable_param",
                    "enabled": True,
                    "when": {"churn_risk": "high"},
                    "params": {"discount_pct": "=__import__('os').system('true')"},
                }
            ],
        }
        saved = rule_engine.RULE_PACKS
        try:
            rule_engine.RULE_PACKS = saved + [broken]
            report = rule_engine.catalog_validation_report()
        finally:
            rule_engine.RULE_PACKS = saved

        assert report["valid"] is False
        assert [row["rule_id"] for row in report["invalid_rules"]] == ["unevaluable_param"], report
        assert report["invalid_rules"][0]["errors"][0]["code"] == "bad_expression", report

    def test_and_an_expression_that_reads_the_rules_own_fields_is_not_flagged(self):
        """The false positive this check had to be built to avoid.

        Evaluating the shipped rule's expression with an empty scope reports
        ``unknown symbol 'stage'`` -- and ``stage`` is exactly what the caller
        supplies. A check that called that a defect would have made the catalog
        permanently red and trained everyone to ignore it, which is the failure
        mode ``_validate_threshold`` already had.
        """
        from app import rule_engine

        report = rule_engine.catalog_validation_report()
        offending = [
            row for row in report["invalid_rules"] if "unknown symbol" in str(row["errors"])
        ]
        assert offending == [], offending

    def test_a_matched_rule_whose_params_will_not_resolve_is_skipped_not_raised(self):
        """The blast radius: one bad param must not take the selection down.

        This is the behaviour change. A rule whose ``when`` matches and whose
        ``params`` will not evaluate used to raise out of ``select_rules``, which
        every caller in this tree sits inside -- so one bad expression took out a
        complaint escalation and a recovery outreach together. It now lands in
        ``skipped``, which a caller already reads.
        """
        from app import rule_engine

        pack = {
            "pack": "scratch",
            "version": 1,
            "rules": [
                {
                    "id": "fine",
                    "enabled": True,
                    "priority": 10,
                    "when": {"churn_risk": "high"},
                    "params": {"channel": "email"},
                },
                {
                    "id": "broken_params",
                    "enabled": True,
                    "priority": 90,
                    "when": {"churn_risk": "high"},
                    "params": {"channel": "=__import__('os')"},
                },
            ],
        }
        selection = rule_engine.select_rules(pack, {"churn_risk": "high"})

        assert selection["fired_ids"] == ["fine"], selection
        reasons = {row["id"]: row["reason"] for row in selection["skipped"]}
        assert "broken_params" in reasons, selection
        assert "params did not resolve" in reasons["broken_params"], reasons

    def test_an_unprefixed_expression_is_stored_as_the_literal_it_is(self):
        """Why the control above needs the ``=``, and it is worth pinning.

        ``resolve_params`` only evaluates a value that starts with ``=``.
        Everything else is passed through verbatim -- so
        ``"params": {"channel": "__import__('os')"}`` does not execute anything;
        it stores the ten characters as a channel name. That is the right
        behaviour, and it means a param expression must opt in by prefixing. An
        earlier draft of the control above omitted the ``=`` and passed for
        entirely the wrong reason: the rule was skipped because it never tried to
        evaluate anything.
        """
        from app import rule_engine

        selection = rule_engine.select_rules(
            {
                "pack": "scratch",
                "version": 1,
                "rules": [
                    {
                        "id": "literal",
                        "enabled": True,
                        "when": {"churn_risk": "high"},
                        "params": {"channel": "__import__('os')"},
                    }
                ],
            },
            {"churn_risk": "high"},
        )
        assert selection["fired_ids"] == ["literal"], selection
        assert selection["fired"][0]["params"]["channel"] == "__import__('os')", selection

    def test_a_rule_that_cannot_compute_its_own_params_does_not_fire_empty(self):
        """Which way to fail, stated explicitly.

        "Report the problem" is ambiguous for a rule: it could mean fire with
        empty params, which would apply *nothing* while reporting that the rule
        matched. For a rule whose whole purpose is a discount, that is the
        dangerous direction, so the skip is asserted rather than assumed.
        """
        from app import rule_engine

        selection = rule_engine.select_rules(
            {
                "pack": "scratch",
                "version": 1,
                "rules": [
                    {
                        "id": "broken_params",
                        "enabled": True,
                        "when": {"churn_risk": "high"},
                        "params": {"discount_pct": "=1 / 0"},
                    }
                ],
            },
            {"churn_risk": "high"},
        )
        assert selection["fired"] == [], selection
        assert selection["skipped"][0]["matched"] is True, selection


class TestTheValidatorRejectsNothingThatActuallyWorks:
    """The direction a validator being wrong matters most.

    ``_validate_threshold`` coerced the threshold of every operator in
    ``_NUMERIC_OPS`` through ``float``, which is correct for ``lt``/``gte`` and
    wrong for ``eq``/``ne``: ``matches_field`` evaluates those with
    ``operator.eq``/``operator.ne``, where ``{'churn_risk': {'ne': 'low'}}``
    compares exactly as written. The validator called it an *error*, so
    ``valid_when`` returned False about a working rule.

    ``communication_suppression.suppress_negative_sentiment`` carried that
    clause. So the pack with the only production caller held a rule its own
    validator disowned -- and a tenant copying that pattern into the admin
    surface would be refused.
    """

    def test_an_equality_comparison_over_a_string_threshold_is_valid(self):
        from app import rule_engine

        check = rule_engine.validate_when({"churn_risk": {"ne": "low"}})
        assert check["valid"] is True, check
        assert check["errors"] == [], check

    def test_and_it_evaluates_the_way_the_validator_now_allows(self):
        """The point of accepting it: the runtime semantics are the sensible ones."""
        from app import rule_engine

        when = {"churn_risk": {"ne": "low"}}
        for churn_risk, expected in (("low", False), ("medium", True), ("high", True)):
            matched = rule_engine.evaluate_when_extended(when, {"churn_risk": churn_risk})[0]
            assert matched is expected, (churn_risk, matched)

    def test_the_shipped_suppression_rule_that_carried_that_clause_is_valid(self):
        from app import rule_engine

        rule = rule_engine.get_rule_pack("communication_suppression")["rules"][0]
        check = rule_engine.validate_when(rule["when"])
        assert check["valid"] is True, (rule["id"], check)
        assert rule["when"] == {"sentiment_label": "negative", "churn_risk": {"ne": "low"}}, rule["when"]

    def test_the_negative_sentiment_suppression_actually_fires(self):
        """Behavioural, because that is the half a validator cannot establish."""
        from app import rule_engine

        selection = rule_engine.select_rules(
            "communication_suppression",
            {"churn_risk": "high", "sentiment_label": "negative", "complaints_last_30d": 0},
        )
        assert "suppress_negative_sentiment" in selection["fired_ids"], selection

    def test_and_a_low_churn_risk_is_still_not_suppressed_by_it(self):
        from app import rule_engine

        selection = rule_engine.select_rules(
            "communication_suppression",
            {"churn_risk": "low", "sentiment_label": "negative", "complaints_last_30d": 0},
        )
        assert "suppress_negative_sentiment" not in selection["fired_ids"], selection

    def test_an_ordering_operator_still_rejects_a_non_numeric_threshold(self):
        """The typo check the numeric rule existed for, and it still works.

        Removing the blanket ``float()`` coercion would have removed this too, so
        it is asserted: ``{"days_since_login": {"gte": "45 days"}}`` must still be
        an error rather than a rule that silently never matches.
        """
        from app import rule_engine

        check = rule_engine.validate_when({"days_since_login": {"gte": "45 days"}})
        assert check["valid"] is False, check
        assert check["errors"][0]["code"] == "bad_threshold", check

    def test_and_the_error_names_the_alternative_rather_than_just_the_complaint(self):
        from app import rule_engine

        message = rule_engine.validate_when({"x": {"lt": "y"}})["errors"][0]["message"]
        assert "eq/ne" in message, message

    def test_a_numeric_threshold_is_still_accepted_by_every_ordering_operator(self):
        from app import rule_engine

        for op in rule_engine._ORDERING_OPS:
            check = rule_engine.validate_when({"x": {op: 45}})
            assert check["valid"] is True, (op, check)

    def test_the_split_is_the_one_the_test_thinks_it_is(self):
        """Pin the split itself, so adding an operator to the wrong bucket fails.

        ``_ORDERING_OPS`` is what ``_validate_threshold`` consults. If somebody
        adds ``ne`` to it, every ``{"field": {"ne": "word"}}`` rule becomes
        invalid again and nothing else in this file would notice.
        """
        from app import rule_engine

        assert set(rule_engine._ORDERING_OPS) <= set(rule_engine._NUMERIC_OPS), (
            "the ordering ops must be a subset of the numeric ops the evaluator "
            f"uses: {rule_engine._ORDERING_OPS} vs {set(rule_engine._NUMERIC_OPS)}"
        )
        assert not {"eq", "ne"} & set(rule_engine._ORDERING_OPS), (
            "equality comparisons succeed against any value at runtime; putting "
            "them back in the ordering bucket reintroduces the false positive"
        )


class TestTheExpressionLanguageCanNameAValueButStillCannotReachOne:
    """String literals became expressible, and the sandbox has to stay intact.

    ``_eval_node`` rejected every ``ast.Constant`` that was not a number, so the
    documented ``==``/``!=`` comparisons only ever worked over numbers and
    ``stage == 'at_risk'`` was inexpressible. Allowing the literal is what makes
    the shipped discount work. The risk that comes with it is a new *kind* of
    value in a language that had none, so the boundary is asserted rather than
    assumed.
    """

    def test_a_string_comparison_evaluates(self):
        from app import rule_engine

        assert rule_engine.evaluate_expression("stage == 'at_risk'", {"stage": "at_risk"}) is True
        assert rule_engine.evaluate_expression("stage == 'at_risk'", {"stage": "engaged"}) is False

    def test_a_string_may_still_not_be_used_as_an_index_or_attribute(self):
        from app import rule_engine

        for expr in ("'abc'.upper()", "'abc'[0]", "'abc'.split(',')"):
            with pytest.raises(ValueError):
                rule_engine.evaluate_expression(expr)

    def test_attribute_access_is_still_refused_even_on_a_string(self):
        """The specific new hole a naive fix would open."""
        from app import rule_engine

        with pytest.raises(ValueError):
            rule_engine.evaluate_expression("stage.upper()", {"stage": "at_risk"})

    def test_the_unsafe_expression_suite_still_raises_value_error(self):
        """``ValueError`` and not ``TypeError``, because callers catch that.

        Allowing strings means ``1 + 'a'`` and ``round('x')`` now parse and fail
        at evaluation. If those escaped as ``TypeError`` the module's own
        ``except ValueError`` promise would be broken and every caller relying on
        it would see the exception pass through.
        """
        from app import rule_engine

        for expr, scope in (
            ("1 + 'a'", {}),
            ("'a' - 1", {}),
            ("round('x')", {}),
            ("1 / 0", {}),
            ("stage + 1", {"stage": "engaged"}),
        ):
            with pytest.raises(ValueError):
                rule_engine.evaluate_expression(expr, scope)

    def test_and_the_message_says_which_expression_failed(self):
        from app import rule_engine

        with pytest.raises(ValueError, match="1 / 0"):
            rule_engine.evaluate_expression("1 / 0")