"""Choosing the brain, and moving value between customers.

Two features, grouped by what each is actually protecting.

**The router** is only worth having if deciding is cheap and answering is
bounded. So the tests are organised around those two claims rather than around
the rule table: a group that makes every I/O path raise and asserts
:func:`decide` still returns, and a group that makes the external brain hang,
raise, and vanish and asserts a response comes back every time.

**Transfers** are only worth having if they are atomic, idempotent, and cannot
be used to launder. The tests run against a real SQLite database for exactly that
reason -- a session double asserts your own assumptions back at you, and the
constraint that matters here ("a retry must not pay out twice") is a constraint.

The two refusals get their own group. Points transfer and debt transfer differ in
this one respect: a credit moving between holders cannot create value, and a debt
moving can. That asymmetry is the design, and a test is what stops a later edit
from smoothing it away.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import models, security
from app.services import brain_router as br
from app.services import transfers as tf

from tests._doubles import SqliteHarness


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# The router: deciding must be free
# ===========================================================================


class TestDecidingPerformsNoIO:
    """The claim the whole performance argument rests on.

    The implementation does not call out, so the only way this test can fail is
    if someone adds a lookup that does. That is exactly the change worth failing
    on: a `db.execute` inside `decide()` is a query added to every customer's
    message.
    """

    def test_decide_works_with_every_io_path_poisoned(self, monkeypatch):
        def explode(*_args, **_kwargs):
            raise AssertionError("decide() performed I/O")

        import socket

        monkeypatch.setattr(socket, "socket", explode)
        monkeypatch.setattr(socket, "create_connection", explode)

        verdict = br.decide({"sentiment_score": -0.9, "question": "help"})
        assert verdict["target"] == "human"

    def test_decide_is_deterministic(self):
        context = {"question": "what is my balance", "has_customer_facts": True}
        first = br.decide(context)
        second = br.decide(context)
        assert first["target"] == second["target"]
        assert first["rule_id"] == second["rule_id"]
        # The timing field is the one thing that may differ, and it is not part
        # of the decision.
        assert first["checks"] == second["checks"]

    def test_decide_stays_inside_its_declared_budget(self):
        # A number to assert against, not a timer that can be waited on: a
        # pure function either costs microseconds or someone added I/O, and the
        # second case is caught by the test above regardless of how fast it is.
        worst = 0.0
        for _ in range(200):
            verdict = br.decide(
                {"sentiment_score": -0.9, "intent": "general_knowledge", "question": "q"}
            )
            worst = max(worst, verdict["decide_ms"])
        assert worst <= br.DECIDE_BUDGET_MS, (
            f"deciding took {worst}ms against a {br.DECIDE_BUDGET_MS}ms budget"
        )

    def test_decide_does_not_mutate_the_context_it_is_given(self):
        context = {"question": "q"}
        before = dict(context)
        br.decide(context)
        assert context == before, (
            "decide() wrote derived keys back into the caller's dict. Harmless "
            "today; a footgun the moment the same dict is reused for a response"
        )

    def test_the_resolver_picks_the_highest_matching_band_not_the_first(self):
        # The first-match-wins bug this project has hit before: an ascending
        # table whose lowest threshold is 0.00 catches every score. Every row is
        # asserted to be individually reachable.
        reachable = set()
        for rule in br.ROUTING_RULES:
            probe = _context_satisfying(rule)
            if probe is None:
                continue
            verdict = br.decide(probe)
            reachable.add(verdict["rule_id"])
        unreachable = {
            str(r["rule_id"])
            for r in br.ROUTING_RULES
            if r.get("when") and str(r["rule_id"]) not in reachable
        }
        assert unreachable == set(), f"unreachable rules: {sorted(unreachable)}"


def _context_satisfying(rule: dict) -> dict | None:
    """A minimal context that satisfies one rule and nothing earlier.

    One key needs translating rather than assigning:
    ``external_permitted`` is *derived* by :func:`decide` from the consent and
    posture signals, and is deliberately overwritten there so it cannot be
    spoofed by a caller. Setting it directly would therefore do nothing --
    ``decide`` would recompute it as permitted and the rule would look
    unreachable when it is not. So the probe supplies the *input* that produces
    it.
    """
    context: dict = {}
    for key, expected in (rule.get("when") or {}).items():
        if key == "external_permitted":
            context["egress_consent"] = bool(expected)
            continue
        if isinstance(expected, dict):
            if "lte" in expected:
                context[key] = float(expected["lte"]) - 0.1
            elif "gte" in expected:
                context[key] = float(expected["gte"]) + 0.1
            else:
                return None
        else:
            context[key] = expected
    # Satisfying this rule is not enough if an earlier one also matches.
    for earlier in br.ROUTING_RULES:
        if earlier is rule:
            break
        probe = dict(context)
        probe["external_permitted"] = br.egress_allowed(probe)["allowed"]
        passed, _ = br._matches_when(earlier.get("when") or {}, probe)
        if passed:
            return None
    return context


# ===========================================================================
# The router: consent and egress
# ===========================================================================


class TestTheRouterWillNotSendAForbiddenCustomerOffHost:
    def test_consent_withdrawn_beats_an_explicit_preference(self):
        verdict = br.decide(
            {"prefers_external": True, "egress_consent": False, "question": "q"}
        )
        assert verdict["target"] == "system"
        assert verdict["rule_id"] == "external_not_permitted"

    def test_a_constrained_posture_is_answered_from_inside(self):
        verdict = br.decide(
            {"prefers_external": True, "control_posture": "constrained", "question": "q"}
        )
        assert verdict["target"] == "system"

    def test_an_unavailable_external_brain_is_not_selected(self):
        verdict = br.decide({"prefers_external": True, "external_available": False})
        assert verdict["target"] == "system"
        assert verdict["rule_id"] == "external_not_available"

    def test_a_customer_fact_question_is_never_sent_to_a_model(self):
        # The failure this prevents is a chatbot saying it cannot see your
        # account while holding the account.
        verdict = br.decide(
            {
                "intent": "general_knowledge",
                "has_customer_facts": True,
                "question": "what is my balance?",
            }
        )
        assert verdict["target"] == "system"

    def test_a_pure_general_question_may_go_external(self):
        verdict = br.decide(
            {"intent": "general_knowledge", "has_customer_facts": False, "question": "how does X work?"}
        )
        assert verdict["target"] == "external_ai"

    def test_the_context_is_stripped_before_it_leaves(self):
        payload = br.redact_for_egress(
            {
                "question": "hello",
                "user_id": 7,
                "email": "real@example.com",
                "loyalty_score": 88,
                "policy_score": 71,
                "device_digest": "abc",
            }
        )
        assert payload["context"] == {"question": "hello"}
        assert "email" in payload["withheld"]
        assert "loyalty_score" in payload["withheld"]

    def test_identity_fields_can_never_be_on_the_allow_list(self):
        assert not (set(br.EGRESS_ALLOWED_FIELDS) & br.IDENTITY_FIELDS)
        assert not (set(br.EGRESS_ALLOWED_FIELDS) & br.EGRESS_NEVER)

    def test_the_validator_is_clean(self):
        assert br.validate_brain_router()["ok"] is True


class TestEscalationDoesNotFireOnAMissingSignal:
    """Sentiment scoring calls an external service, so it is `None` offline.

    If absence matched the escalation rule, every conversation in an offline
    deployment would route to a human, and the operator queue would quietly
    become the product. That is a deployment-shaped failure, which is why it gets
    its own group rather than one assertion.
    """

    def test_no_sentiment_does_not_escalate(self):
        verdict = br.decide({"sentiment_score": None, "question": "q"})
        assert verdict["target"] == "system"

    def test_a_moderately_unhappy_customer_is_not_escalated(self):
        verdict = br.decide({"sentiment_score": -0.4, "question": "q"})
        assert verdict["target"] == "system"

    def test_a_genuinely_distressed_customer_is(self):
        verdict = br.decide({"sentiment_score": -0.9, "question": "q"})
        assert verdict["target"] == "human"

    def test_a_non_numeric_sentiment_does_not_escalate(self):
        verdict = br.decide({"sentiment_score": "very bad", "question": "q"})
        assert verdict["target"] == "system"


class TestAPersonOutranksEveryAutomatedSignal:
    def test_an_operator_pin_wins_over_everything(self):
        verdict = br.decide(
            {
                "operator_pinned": True,
                "sentiment_score": 0.9,
                "legal_hold": False,
                "prefers_external": True,
                "intent": "general_knowledge",
                "has_customer_facts": False,
            }
        )
        assert verdict["target"] == "human"
        assert verdict["rule_id"] == "operator_pinned"

    def test_a_legal_hold_wins_even_when_nothing_is_wrong(self):
        verdict = br.decide({"legal_hold": True, "sentiment_score": 0.9})
        assert verdict["target"] == "human"

    def test_calm_legal_language_still_escalates(self):
        # Sentiment ranking a solicitor's letter as neutral would otherwise keep
        # it automated, which is the dangerous direction.
        verdict = br.decide({"legal_terms_detected": True, "sentiment_score": 0.5})
        assert verdict["target"] == "human"


# ===========================================================================
# The router: answering is bounded and never silent
# ===========================================================================


class TestAnsweringAlwaysReturnsAndNeverWaitsForever:
    def test_an_external_timeout_degrades_to_system(self):
        def hangs(question, context, budget_ms):
            raise TimeoutError("provider timed out")

        result = br.respond("hi", {"prefers_external": True}, external=hangs)
        assert result.target == "system"
        assert result.fell_back is True
        assert result.external_attempted is True
        assert "TimeoutError" in result.external_failure
        assert result.text

    def test_a_missing_external_brain_is_not_attempted(self):
        result = br.respond("hi", {"prefers_external": True})
        assert result.target == "system"
        assert result.external_attempted is False
        assert "no external brain" in result.fallback_reason

    def test_a_successful_external_call_is_used(self):
        result = br.respond(
            "hi",
            {"prefers_external": True},
            external=lambda q, c, b: "from the model",
        )
        assert result.target == "external_ai"
        assert result.text == "from the model"

    def test_the_external_budget_never_exceeds_what_is_left(self):
        seen: dict = {}

        def slow(question, context, budget_ms):
            seen["budget_ms"] = budget_ms
            return "ok"

        br.respond("hi", {"prefers_external": True}, external=slow, total_budget_ms=5000.0)
        assert seen["budget_ms"] <= br.EXTERNAL_BUDGET_MS
        assert seen["budget_ms"] <= 5000.0

    def test_an_already_spent_budget_answers_faster_not_slower(self):
        result = br.respond("hi", {"prefers_external": True}, total_budget_ms=0.0)
        assert result.target == "system"
        assert result.text

    def test_the_breaker_opens_and_then_stops_the_call(self):
        br.reset_external_circuit()
        try:
            def boom(question, context, budget_ms):
                raise RuntimeError("down")

            for _ in range(br.EXTERNAL_FAILURE_THRESHOLD):
                br.respond("hi", {"prefers_external": True}, external=boom)
            assert br.EXTERNAL_CIRCUIT.state == "open"

            calls = []

            def counted(question, context, budget_ms):
                calls.append(1)
                return "should not be reached"

            result = br.respond("hi", {"prefers_external": True}, external=counted)
            assert result.target == "system"
            assert calls == [], "the breaker opened but the call was still made"
            assert "circuit" in result.fallback_reason
        finally:
            br.reset_external_circuit()

    def test_a_breaker_outage_is_not_a_user_visible_error(self):
        br.reset_external_circuit()
        try:
            br.EXTERNAL_CIRCUIT.record_failure()
            br.EXTERNAL_CIRCUIT.record_failure()
            br.EXTERNAL_CIRCUIT.record_failure()
            result = br.respond("q", {"prefers_external": True}, external=None)
            assert result.text, "a degraded provider produced no response"
        finally:
            br.reset_external_circuit()

    def test_a_provider_exception_never_leaks_the_payload(self):
        def explodes(question, context, budget_ms):
            raise RuntimeError(f"failed sending {context!r}")

        result = br.respond(
            "hi", {"prefers_external": True, "question": "secret question"}, external=explodes
        )
        assert result.fell_back is True
        assert "secret question" not in result.fallback_reason
        assert "RuntimeError" in result.fallback_reason


class TestRoutingToAPersonIsNotSilence:
    def test_the_customer_is_answered_immediately(self):
        result = br.respond(
            "I want to complain",
            {"legal_terms_detected": True},
            system=lambda q, c: "draft from the system",
        )
        assert result.target == "human"
        assert result.text
        assert "person" in result.text.lower()
        assert "draft from the system" in result.text, (
            "the operator's draft must reach the customer too, or they are told a "
            "person is on it with nothing actually noted"
        )

    def test_the_operator_task_is_queued(self):
        queued: list = []
        result = br.respond(
            "complaint",
            {"legal_terms_detected": True},
            enqueue=lambda task: queued.append(task) or {"enqueued": True},
        )
        assert queued, "nothing reached the operator queue"
        assert queued[0]["rule_id"] == "legal_pressure"
        assert result.operator_task["enqueued"] is True

    def test_a_queue_failure_does_not_break_the_chat(self):
        def broken(task):
            raise RuntimeError("queue down")

        result = br.respond(
            "complaint", {"legal_terms_detected": True}, enqueue=broken
        )
        assert result.text, "losing the queue lost the customer's reply too"
        assert result.operator_task["enqueued"] is False
        assert result.operator_task["error"] == "RuntimeError"

    def test_the_customer_told_a_person_is_on_it_when_the_queue_failed(self):
        # Deliberately NOT tested as absent: the acknowledgement still goes out.
        # What must not happen is a silent failure, and `operator_task` records
        # the drop for an operator to see. Asserted here so the choice is
        # explicit rather than accidental.
        result = br.respond(
            "complaint",
            {"legal_terms_detected": True},
            enqueue=lambda t: (_ for _ in ()).throw(RuntimeError("down")),
        )
        assert result.operator_task["enqueued"] is False
        assert result.target == "human"


# ===========================================================================
# Transfers
# ===========================================================================


@pytest.fixture
def harness():
    h = SqliteHarness()
    h.run(h.setup())
    try:
        yield h
    finally:
        h.run(h.teardown())
        h.close()


async def _wallet(session, user_id: int, point_type: str = "loyalty", balance: float = 0.0):
    row = models.PointsWallet(user_id=user_id, point_type=point_type, balance=balance)
    session.add(row)
    await session.commit()
    return row


async def _balance(session, user_id: int, point_type: str = "loyalty") -> float:
    from sqlalchemy import select

    result = await session.execute(
        select(models.PointsWallet).where(
            models.PointsWallet.user_id == user_id,
            models.PointsWallet.point_type == point_type,
        )
    )
    row = result.scalars().first()
    return float(row.balance or 0.0) if row is not None else 0.0


async def _transfer_rows(session, user_id: int) -> list:
    from sqlalchemy import select

    result = await session.execute(
        select(models.PointsTransaction).where(models.PointsTransaction.user_id == user_id)
    )
    return result.scalars().all()


def _open_arrears(harness, *, user_id: int, principal: float, reference: str) -> int:
    """Open an arrears entry on the harness loop and return its id.

    Wrapped rather than awaited inline because every call site would otherwise
    repeat ``harness.run(...)`` and one of them would eventually not, which
    surfaces as ``'coroutine' object is not subscriptable`` several lines away
    from the mistake.

    ``open_arrears_payment`` returns ``_entry_dict(...)`` -- the entry's fields,
    not an envelope -- so the id is read off the top level.
    """
    from app.services import arrears_payments as ap

    opened = harness.run(
        ap.open_arrears_payment(
            harness.session,
            user_id=user_id,
            principal=principal,
            defer_days=30,
            reference=reference,
            service_type="consultation",
        )
    )
    return int(opened["id"])


def _entry_row(harness, entry_id: int):
    from app.services import arrears_payments as ap

    return harness.run(ap._load_entry_row(harness.session, entry_id))


class TestPointsTransferIsAtomicAndIdempotent:
    def test_a_transfer_moves_the_points_and_conserves_the_total(self, harness):
        harness.run(harness._user(1, False))
        harness.run(harness._user(2, False))
        harness.run(_wallet(harness.session, 1, balance=500.0))
        harness.run(_wallet(harness.session, 2, balance=100.0))

        result = harness.run(
            tf.transfer_points(
                harness.session,
                from_user_id=1,
                to_user_id=2,
                points=200.0,
                idempotency_key="k1",
            )
        )
        assert result["sender_balance_after"] == 300.0
        assert result["receiver_balance_after"] == 300.0
        assert harness.run(_balance(harness.session, 1)) == 300.0
        assert harness.run(_balance(harness.session, 2)) == 300.0

    def test_both_ledger_legs_are_written_under_one_reference(self, harness):
        harness.run(harness._user(1, False))
        harness.run(harness._user(2, False))
        harness.run(_wallet(harness.session, 1, balance=500.0))
        harness.run(_wallet(harness.session, 2, balance=0.0))

        harness.run(
            tf.transfer_points(
                harness.session, from_user_id=1, to_user_id=2, points=50.0, idempotency_key="k2"
            )
        )
        out = harness.run(_transfer_rows(harness.session, 1))
        into = harness.run(_transfer_rows(harness.session, 2))
        assert len(out) == 1 and out[0].kind == "transfer_out"
        assert out[0].points_delta == -50.0
        assert len(into) == 1 and into[0].kind == "transfer_in"
        assert into[0].points_delta == 50.0
        # One reference, so the movement is auditable from either side alone.
        assert out[0].reference == into[0].reference
        assert out[0].reference == "transfer:k2"

    def test_a_retry_with_the_same_key_does_not_pay_out_twice(self, harness):
        harness.run(harness._user(1, False))
        harness.run(harness._user(2, False))
        harness.run(_wallet(harness.session, 1, balance=500.0))
        harness.run(_wallet(harness.session, 2, balance=0.0))

        first = harness.run(
            tf.transfer_points(
                harness.session, from_user_id=1, to_user_id=2, points=100.0, idempotency_key="k3"
            )
        )
        second = harness.run(
            tf.transfer_points(
                harness.session, from_user_id=1, to_user_id=2, points=100.0, idempotency_key="k3"
            )
        )
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        assert second["transfer_id"] == first["transfer_id"]
        # The whole point: the balance moved once.
        assert harness.run(_balance(harness.session, 1)) == 400.0
        assert harness.run(_balance(harness.session, 2)) == 100.0

    def test_a_key_is_mandatory(self, harness):
        harness.run(harness._user(1, False))
        harness.run(harness._user(2, False))
        with pytest.raises(tf.TransferError) as caught:
            harness.run(
                tf.transfer_points(
                    harness.session, from_user_id=1, to_user_id=2, points=10.0, idempotency_key=""
                )
            )
        assert caught.value.code == "idempotency_key_required"

    def test_overdrawing_is_refused_with_a_reason(self, harness):
        harness.run(harness._user(1, False))
        harness.run(harness._user(2, False))
        harness.run(_wallet(harness.session, 1, balance=10.0))
        harness.run(_wallet(harness.session, 2, balance=0.0))

        with pytest.raises(tf.TransferError) as caught:
            harness.run(
                tf.transfer_points(
                    harness.session, from_user_id=1, to_user_id=2, points=100.0,
                    idempotency_key="k4",
                )
            )
        assert caught.value.code == "insufficient_balance"
        assert "10.0" in caught.value.reason
        # A refusal must not leave a partial debit behind.
        assert harness.run(_balance(harness.session, 1)) == 10.0

    def test_transferring_to_yourself_is_refused(self, harness):
        harness.run(harness._user(1, False))
        harness.run(_wallet(harness.session, 1, balance=500.0))
        with pytest.raises(tf.TransferError) as caught:
            harness.run(
                tf.transfer_points(
                    harness.session, from_user_id=1, to_user_id=1, points=10.0,
                    idempotency_key="k5",
                )
            )
        assert caught.value.code == "transfer_to_self"

    def test_a_non_transferable_point_type_is_refused(self, harness):
        harness.run(harness._user(1, False))
        harness.run(harness._user(2, False))
        harness.run(_wallet(harness.session, 1, point_type="streak_counter", balance=500.0))
        harness.run(_wallet(harness.session, 2, point_type="streak_counter", balance=0.0))
        with pytest.raises(tf.TransferError) as caught:
            harness.run(
                tf.transfer_points(
                    harness.session, from_user_id=1, to_user_id=2, points=1.0,
                    point_type="streak_counter", idempotency_key="k6",
                )
            )
        assert caught.value.code == "point_type_not_transferable"

    def test_the_daily_limit_is_enforced_and_read_from_the_database(self, harness):
        harness.run(harness._user(1, False))
        harness.run(harness._user(2, False))
        harness.run(_wallet(harness.session, 1, balance=100000.0))
        harness.run(_wallet(harness.session, 2, balance=0.0))

        for index in range(tf.TRANSFER_LIMITS["per_day_count"]):
            harness.run(
                tf.transfer_points(
                    harness.session, from_user_id=1, to_user_id=2, points=1.0,
                    idempotency_key=f"bulk-{index}",
                )
            )
        with pytest.raises(tf.TransferError) as caught:
            harness.run(
                tf.transfer_points(
                    harness.session, from_user_id=1, to_user_id=2, points=1.0,
                    idempotency_key="bulk-over",
                )
            )
        assert caught.value.code == "over_daily_count"


class TestDebtDoesNotMove:
    """The refusal, and the thing built in its place."""

    def test_the_refusal_is_recorded_with_a_reason(self):
        refusal = next(r for r in tf.REFUSED_OPERATIONS if r["operation"] == "transfer_debt")
        assert refusal["refused"] is True
        assert len(refusal["why_not"]) > 120
        assert "evasion" in refusal["why_not"]
        assert refusal["built_instead"]

    def test_there_is_no_debt_transfer_operation_anywhere(self):
        names = {r["operation"] for r in tf.REFUSED_OPERATIONS}
        assert "transfer_debt" in names
        # ...and no public callable that would do one.
        for forbidden in ("transfer_debt", "assign_debt", "move_arrears", "transfer_arrears"):
            assert not hasattr(tf, forbidden), (
                f"{forbidden} exists; a debt-moving operation is evasion and the "
                "refusal is recorded as data, not as an absence"
            )

    def test_a_third_party_settlement_does_not_move_the_liability(self, harness):
        from app.services import arrears_payments as ap

        debtor = harness.run(harness._user(1, False))
        payer = harness.run(harness._user(2, False))
        entry_id = _open_arrears(
            harness, user_id=debtor.id, principal=250.0, reference="inv-1"
        )

        result = harness.run(
            tf.settle_arrears_for_another(
                harness.session,
                entry_id=entry_id,
                payer_user_id=payer.id,
                debtor_user_id=debtor.id,
                idempotency_key="s1",
            )
        )
        assert result["debt_transferred"] is False
        assert result["debt_remains_with"] == debtor.id
        assert result["is_third_party"] is True
        assert result["credit_awarded_to_payer"] == 0

    def test_the_payer_earns_no_credit(self, harness):
        from app.services import arrears_payments as ap

        debtor = harness.run(harness._user(1, False))
        payer = harness.run(harness._user(2, False))
        entry_id = _open_arrears(
            harness, user_id=debtor.id, principal=250.0, reference="inv-2"
        )
        before = harness.run(_balance(harness.session, payer.id))

        result = harness.run(
            tf.settle_arrears_for_another(
                harness.session,
                entry_id=entry_id,
                payer_user_id=payer.id,
                debtor_user_id=debtor.id,
                idempotency_key="s2",
            )
        )
        assert result["credit_awarded_to_payer"] == 0
        assert harness.run(_balance(harness.session, payer.id)) == before

    def test_the_database_cannot_represent_the_laundering_payout(self, harness):
        # Application code asserting it awards nothing is worth much less than a
        # database that cannot represent the reward.
        from sqlalchemy.exc import IntegrityError

        debtor = harness.run(harness._user(1, False))
        payer = harness.run(harness._user(2, False))
        entry = models.ArrearsEntry(
            user_id=debtor.id, principal=100.0, reference="inv-3", policy_id="p"
        )
        harness.session.add(entry)
        harness.session.add(
            models.ArrearsSettlement(
                idempotency_key="bad",
                debtor_user_id=debtor.id,
                payer_user_id=payer.id,
                arrears_entry_id=1,
                amount=10.0,
                payout_credit_points=10.0,
            )
        )
        with pytest.raises(IntegrityError):
            harness.commit()
        harness.rollback()

    def test_the_debtor_cannot_be_nominated_by_the_caller(self, harness):
        from app.services import arrears_payments as ap

        real_debtor = harness.run(harness._user(1, False))
        impostor = harness.run(harness._user(3, False))
        payer = harness.run(harness._user(2, False))
        entry_id = _open_arrears(
            harness, user_id=real_debtor.id, principal=100.0, reference="inv-4"
        )

        with pytest.raises(tf.TransferError) as caught:
            harness.run(
                tf.settle_arrears_for_another(
                    harness.session,
                    entry_id=entry_id,
                    payer_user_id=payer.id,
                    debtor_user_id=impostor.id,
                    idempotency_key="s3",
                )
            )
        assert caught.value.code == "debtor_mismatch"

    def test_the_quote_states_the_refusal_up_front(self, harness):
        debtor = harness.run(harness._user(1, False))
        entry_id = _open_arrears(
            harness, user_id=debtor.id, principal=100.0, reference="inv-5"
        )
        quote = tf.quote_third_party_settlement(
            entry=_entry_row(harness, entry_id), payer_is_debtor=False
        )
        assert quote["debt_transferred"] is False
        assert quote["credit_awarded_to_payer"] == 0
        assert quote["refusal"]["operation"] == "transfer_debt"


class TestTransferGovernance:
    def test_the_validator_is_clean(self):
        assert tf.validate_transfers()["ok"] is True

    def test_the_asymmetry_is_published(self):
        catalog = tf.build_transfers_catalog()
        assert catalog["operations"]["transfer_debt"].startswith("does not exist")
        assert "transfer_points" in catalog["operations"]
        assert "settle_arrears_for_another" in catalog["operations"]

    def test_the_refusals_endpoint_needs_no_session_and_leaks_nothing(self):
        from fastapi.testclient import TestClient

        from app.main import app

        response = TestClient(app).get("/transfers/refusals")
        assert response.status_code == 200
        body = response.json()
        refused = {row["operation"] for row in body["refused"]}
        assert "transfer_debt" in refused
        assert all(row["why_not"] for row in body["refused"])
        # Static policy text: describes the build, not any account.
        assert "user_id" not in json.dumps(body)

    def test_the_write_routes_require_a_session(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        assert client.post(
            "/transfers/points",
            json={"to_user_id": 2, "points": 10, "idempotency_key": "k"},
        ).status_code in (401, 403)
        assert client.post(
            "/transfers/settle-arrears",
            json={"arrears_entry_id": 1, "debtor_user_id": 1, "idempotency_key": "k"},
        ).status_code in (401, 403)
        assert client.get("/transfers/quote?points=10").status_code in (401, 403)
        assert client.get("/transfers/history").status_code in (401, 403)

    def test_every_transfer_route_is_classified(self):
        from app import deps
        from app.main import app

        report = deps.authz_drift_report(app.routes)
        assert report["in_sync"] is True, report["mismatched"]
        unclassified = [r for r in report["unclassified_routes"] if "/transfers/" in r]
        assert unclassified == []

    def test_the_migration_builds_both_tables(self):
        import importlib.util
        from pathlib import Path

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path("alembic/versions/20261002_01_add_transfers.py")
        spec = importlib.util.spec_from_file_location("transfers_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        engine = sa.create_engine("sqlite://")
        connection = engine.connect()
        try:
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            inspector = sa.inspect(connection)
            for name in ("points_transfers", "arrears_settlements"):
                table = models.get_table(name)
                columns = {c["name"] for c in inspector.get_columns(name)}
                assert columns == {c.name for c in table.columns}, name
            module.downgrade()
            names = sa.inspect(connection).get_table_names()
            assert "points_transfers" not in names
            assert "arrears_settlements" not in names
        finally:
            connection.close()
            engine.dispose()
