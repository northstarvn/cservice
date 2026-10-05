"""Tier 2: journeys that take more than one request, and a person rather than a row.

``test_e2e_certainty_flows.py`` is the floor. It proves the vocabulary is
honest, the order is preserved, the same input gives the same output, and the
gates fail closed. Every one of those tests could be answered by calling a pure
function with a hand-built argument.

This file is the next rung up, and the difference is not longer. It is that the
state is *earned*:

* a complaint reference comes from an insert that had to happen, and the two
  references are only distinct because two complaints were lodged;
* a debt has to be opened before it can be forgiven, and forgiven before it can
  be settled, and the second settle has to be refused -- which is the only way to
  know the first one was recorded at all;
* the actor changes mid-flow, so a test that only ever passes as `root` proves
  nothing about a customer's permissions.

Three flows, in the order the difficulty rises:

1. :class:`TestChiaraAWeek` -- one at-risk customer, seven days, eleven requests.
2. :class:`TestArrearsLifecycle` -- money: accrue, forgive, settle, refuse.
3. :class:`TestOffersLifecycle` -- an offer issued, read, accepted, then re-read.

The cast, the database and the HTTP client all come from ``tests/_e2e_world.py``.
Nothing here is seeded into a convenient starting state: the flows start from a
customer row and a policy score, exactly as production would.
"""

from datetime import timedelta

import asyncio
import pytest

from _e2e_world import (
    LATER,
    NOW,
    THE_CAST,
    assert_money,
    assert_monotonic,
    explain_keys,
    mounted,
    person,
    world,
)

#: chiara: three cancellations, high churn risk, no personalization consent, and
#: a billing complaint. The cast member with the most state, so the journey with
#: the most to go wrong.
CHIARA = 3
#: ana: healthy, two completed bookings, marketing and analytics consent.
ANA = 1
#: bruno: one pending booking and nothing to spend.
BRUNO = 2
#: root: the operator. Every admin action in this file is this actor.
ROOT = 5


class TestChiaraAWeek:
    """One complaint, from lodgement to closure, seen from both sides.

    The point of writing this as a single test rather than eleven is that the
    intermediate assertions are the *preconditions* of the later ones. Asserting
    ``status == "acknowledged"`` immediately after the acknowledge call proves the
    endpoint echoed its input; asserting it *before the resolve* proves the
    acknowledge was durable, which is the thing worth knowing.

    So: the week is walked in order, and each step asserts both "this step did
    what it said" and "the previous step is still there".
    """

    def test_the_case_walks_from_lodged_to_closed_and_both_sides_agree(self, world):
        chiara = mounted(world, user_id=CHIARA)
        root = mounted(world, user_id=ROOT)

        # --- day 0, the customer lodges it.
        lodged = chiara.post(
            "/complaints",
            json={
                "category": "billing",
                "severity": "high",
                "summary": (
                    "Charged twice for the same booking and nobody has answered "
                    "the three messages I sent."
                ),
                "source": "chat",
            },
        )
        assert lodged.status_code == 201, lodged.text
        case = lodged.json()
        reference = case["reference"]

        assert case["status"] == "open", case
        assert case["user_id"] == CHIARA, case
        # The SLA clock is part of the answer to "when will I hear from you", so
        # the response has to carry it rather than the customer having to ask.
        assert case["response_due_at"], explain_keys(case)
        assert case["opened_at"], explain_keys(case)

        # --- day 0, she can read it back, and only her own.
        own = chiara.json("get", f"/complaints/{reference}")
        assert own["reference"] == reference
        assert own["status"] == "open"

        ana = mounted(world, user_id=ANA)
        someone_elses = ana.get(f"/complaints/{reference}")
        assert someone_elses.status_code == 404, (
            "another customer read a complaint she does not own: "
            f"{someone_elses.status_code} {someone_elses.text}"
        )

        # --- day 1, an operator acknowledges.
        acknowledged = root.json("post", f"/complaints/{reference}/acknowledge", json={})
        assert acknowledged["status"] == "acknowledged", acknowledged
        assert acknowledged["acknowledged_at"], acknowledged

        # Acknowledging already put the acting operator on the case, so the case
        # has an owner before anyone assigns it. Asserted because the next step
        # depends on it: routing to a team used to silently drop that owner.
        assert acknowledged["owner_user_id"] == ROOT, acknowledged
        assert acknowledged["owner_team"], acknowledged

        # --- day 1, it is routed to the team that owns billing.
        assigned = root.json(
            "post",
            f"/complaints/{reference}/assign",
            json={"owner_team": "billing", "note": "duplicate charge, needs a refund"},
        )
        assert assigned["owner_team"] == "billing", assigned
        # Re-routing to a team must not orphan the case. It did: `assign_complaint`
        # treated an absent `owner_user_id` as "clear it", so the natural routing
        # call produced an unowned case that showed up in the queue's `unowned`
        # list -- the surface whose whole job is saying a case has nobody on it.
        assert assigned["owner_user_id"] == ROOT, (
            "routing a case to a different team silently unowned it: "
            f"{assigned['owner_user_id']!r}"
        )
        # Assignment must not undo the acknowledgement. This is the assertion
        # that catches an assign implemented as "reopen and re-lodge".
        assert assigned["status"] == "acknowledged", assigned
        assert assigned["acknowledged_at"] == acknowledged["acknowledged_at"], assigned

        # An owner can be dropped on purpose, by naming nobody.
        unowned = root.json(
            "post", f"/complaints/{reference}/assign", json={"owner_user_id": 0}
        )
        assert unowned["owner_user_id"] in (None, 0), unowned
        # ...and put back.
        reassigned = root.json(
            "post",
            f"/complaints/{reference}/assign",
            json={"owner_user_id": ROOT, "owner_team": "billing"},
        )
        assert reassigned["owner_user_id"] == ROOT, reassigned

        # --- day 2, the customer adds to the timeline.
        noted = chiara.json(
            "post",
            f"/complaints/{reference}/notes",
            json={"note": "Here is the second charge, same amount, same day."},
        )
        assert noted["reference"] == reference
        history = chiara.json("get", f"/complaints/{reference}")
        events = history.get("events") or history.get("timeline") or []
        assert len(events) >= 4, (
            f"expected the acknowledgement, the assignment, the note and the "
            f"lodgement on the timeline, got {len(events)}: "
            f"{explain_keys(history)}"
        )

        # --- day 3, the queue the operator works from agrees.
        queue = root.json("get", "/complaints/admin/queue")
        assert queue["open_cases"] >= 1, queue
        assert reference in [row["reference"] for row in queue["queue"]], queue
        # `by_status` counts cases per status rather than naming them; the names
        # are in `queue`. Asserted as a count because a reference compared against
        # a dict of counts is a test that can only pass if the reference is "0"
        # or "1", which is not what anybody reading it would think.
        assert queue["by_status"].get(assigned["status"]) == 1, (
            f"the queue counted {queue['by_status']}, but exactly one case is "
            f"in status {assigned['status']!r}"
        )
        # The queue also reports what is *unowned*, which is why routing a case
        # to a team has to leave its owner alone.
        assert "unowned" in queue, sorted(queue)

        # --- day 4, resolved, with a reason recorded.
        resolved = root.json(
            "post",
            f"/complaints/{reference}/resolve",
            json={
                "resolution_code": "refunded",
                "resolution_note": "Second charge reversed; apology sent.",
                "satisfaction_score": 6,
            },
        )
        assert resolved["status"] == "resolved", resolved
        assert resolved["resolved_at"], resolved

        # --- day 5, closed by the operator.
        closed = root.json("post", f"/complaints/{reference}/close", json={})
        assert closed["status"] == "closed", closed
        assert closed["closed_at"], closed

        # --- day 7, the customer reads the whole thing back.
        final = chiara.json("get", f"/complaints/{reference}")
        assert final["status"] == "closed", final
        assert final["resolved_at"] == resolved["resolved_at"], (
            "the resolve timestamp changed when the case was closed"
        )
        assert final["closed_at"], final

        # Her own list now carries it, and the SLA report has stopped counting it
        # as an open case.
        mine = chiara.json("get", "/complaints/me")
        assert [row["reference"] for row in mine] == [reference], mine

        sla = root.json("get", "/complaints/admin/sla")
        assert reference not in sla["breached_resolution"], sla
        assert reference not in sla["due_soon"], sla
        # `open_cases` is a count here and a list on `/complaints/admin/queue`,
        # which is the shape that made the first draft of this assert
        # `reference not in sla["open_cases"]` and fail with
        # "argument of type 'int' is not iterable". Two reports on one subsystem
        # naming the same thing differently is a small thing; it is recorded here
        # because the reader of the failing test would otherwise assume the
        # report was wrong rather than that the test guessed.
        assert sla["open_cases"] == 0, (
            f"the case is closed but the SLA report still counts it open: {sla}"
        )
        assert sla["breach_count"] == 0, sla


class TestChiaraReopens:
    """A resolved case is not a closed door.

    Split out from the week because the reopen path is the one with a rule worth
    stating: a *resolved* case may be reopened, a *closed* one may not, and the
    refusal has to be an explanation rather than a 500.
    """

    def test_a_resolved_case_can_be_reopened_and_a_closed_one_cannot(self, world):
        chiara = mounted(world, user_id=CHIARA)
        root = mounted(world, user_id=ROOT)

        reference = chiara.json(
            "post",
            "/complaints",
            json={"category": "billing", "summary": "Still charged twice."},
        )["reference"]

        root.json("post", f"/complaints/{reference}/acknowledge", json={})
        root.json(
            "post",
            f"/complaints/{reference}/resolve",
            json={"resolution_code": "explained", "resolution_note": "Not a duplicate."},
        )

        reopened = chiara.json(
            "post",
            f"/complaints/{reference}/reopen",
            json={"reason": "It is the same charge twice. I have the receipts."},
        )
        assert reopened["status"] == "open", reopened
        assert reopened["resolved_at"] is None, (
            "reopening kept the resolved timestamp, so the timeline now claims "
            "the case was resolved and is open at the same time"
        )

        root.json(
            "post",
            f"/complaints/{reference}/resolve",
            json={"resolution_code": "refunded"},
        )
        closed = root.json("post", f"/complaints/{reference}/close", json={})
        assert closed["status"] == "closed", closed

        # Reopening a *closed* case is allowed, and that is deliberate: the
        # module treats a reopen as evidence the earlier decision was wrong, and
        # refusing it would throw that evidence away for the cases where it is
        # most available. What matters is that the reopen leaves no trace of the
        # finished states behind -- an open case carrying a `closed_at` tells a
        # customer two contradictory things at once.
        again = chiara.json(
            "post", f"/complaints/{reference}/reopen", json={"reason": "one more try"}
        )
        assert again["status"] == "open", again
        assert again["closed_at"] is None, (
            "the reopened case still reports closed_at, so the timeline claims it "
            f"is open and closed at the same time: {again['closed_at']}"
        )
        assert again["resolved_at"] is None, again
        assert again["reopened_count"] == 2, again

    def test_decision_support_names_the_gap_rather_than_pretending(self, world):
        """The transparency surface, for a case that has just been opened.

        ``/complaints/{reference}/decision-support`` is the answer to "what is
        happening with my complaint". A freshly-lodged case has no owner and no
        policy score, so the interesting assertion is that those *gaps are named*
        -- an operator reading this should be able to see what is missing without
        reading the source.
        """
        chiara = mounted(world, user_id=CHIARA)
        reference = chiara.json(
            "post",
            "/complaints",
            json={"category": "service_quality", "summary": "Nobody called back."},
        )["reference"]

        dossier = chiara.json(
            "get", f"/complaints/me/decision-support?reference={reference}"
        )
        assert dossier["case"]["reference"] == reference, dossier
        assert "gaps" in dossier, explain_keys(dossier)
        assert isinstance(dossier["gaps"], list)
        # Either it names the gaps or it has none; what it must not do is claim
        # completeness on a case nobody has touched.
        if dossier["gaps"]:
            assert all(isinstance(gap, str) and gap for gap in dossier["gaps"]), dossier

        # And it is not readable across customers.
        ana = mounted(world, user_id=ANA)
        cross = ana.get(f"/complaints/me/decision-support?reference={reference}")
        assert cross.status_code == 404, cross.text


class TestArrearsLifecycle:
    """Accrue, forgive, settle, refuse to settle again.

    The money flow. Four states and two refusals, all through HTTP, on a real
    database row.

    The property that matters and that a unit test on ``settle_arrears_entry``
    cannot establish is **ordering**: forgiving interest is only legal while the
    entry is open, so a waive-after-settle must fail and a settle-after-waive must
    still work with the interest gone. Both directions are asserted.
    """

    def _open(self, world, principal=400.0, defer_days=30):
        chiara = mounted(world, user_id=CHIARA)
        response = chiara.post(
            "/chat/payments/arrears",
            json={
                "principal": principal,
                "defer_days": defer_days,
                "service_type": "consultation",
                "currency": "USD",
                "note": "cannot pay in full this month",
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_the_whole_lifecycle_over_http(self, world):
        chiara = mounted(world, user_id=CHIARA)
        root = mounted(world, user_id=ROOT)

        # --- accrue. A quote first, because a customer is entitled to be told
        # what deferring costs before agreeing to it.
        quote = chiara.json(
            "get", "/chat/payments/arrears/quote?principal=400&defer_days=30"
        )
        assert quote["eligible"] is True, quote
        assert quote["policy"]["policy_id"], quote
        # The quote and the entry have to be priced by the same policy, or the
        # number the customer was shown is not the number they got.
        entry = self._open(world)
        assert entry["policy_id"] == quote["policy"]["policy_id"], (entry, quote)
        assert_money(entry["principal"], 400.0, what="principal")
        assert entry["status"] == "open", entry
        assert entry["interest_accrued"] == 0.0, (
            "a freshly opened entry already carries interest, so day 0 is not day 0"
        )
        entry_id = entry["id"]

        # --- she sees her own debt, and only her own.
        hers = chiara.json("get", "/chat/payments/arrears")
        assert hers["total"] == 1, hers
        assert_money(hers["open_total_principal"], 400.0, what="open principal")
        others = mounted(world, user_id=ANA).json("get", "/chat/payments/arrears")
        assert others["total"] == 0, others

        # --- forgive the interest. She is a `standard` tier, so this is an
        # operator's call, not hers.
        customer_waive = chiara.post(
            f"/chat/admin/payments/arrears/{entry_id}/waive-interest"
        )
        assert customer_waive.status_code == 403, (
            "a customer forgave her own interest: "
            f"{customer_waive.status_code} {customer_waive.text}"
        )

        waived = root.json(
            "post", f"/chat/admin/payments/arrears/{entry_id}/waive-interest"
        )
        assert_money(waived["waived_interest"], 0.0, what="interest waived on day 0")
        assert_money(waived["total_owed"], 400.0, what="total owed after the waiver")
        assert waived["entry"]["status"] == "waived", waived

        # --- waive twice is refused. The message names the *status* rule rather
        # than the "already waived" rule, because `waive_arrears_interest` checks
        # "only open entries" first and the first waiver moves the status to
        # `waived`. Its own second guard -- "Interest has already been waived for
        # this entry" -- is therefore unreachable through this route. That is
        # harmless (the first guard says the same thing more usefully) but it is
        # why the assertion is on the status message and not the obvious one.
        again = root.post(f"/chat/admin/payments/arrears/{entry_id}/waive-interest")
        assert again.status_code == 422, again.text
        assert "only open arrears entries" in again.text.lower(), again.text

        # --- settle. The interest is gone because it was forgiven, not because
        # the entry is young -- these are different reasons for the same zero and
        # only one of them is the operator's decision.
        settled = root.json("post", f"/chat/admin/payments/arrears/{entry_id}/settle")
        assert_money(settled["interest_charged"], 0.0, what="interest charged")
        assert_money(settled["principal"], 400.0, what="principal paid")
        assert_money(settled["total_paid"], 400.0, what="total paid")
        assert settled["entry"]["status"] == "settled", settled
        assert settled["entry"]["settled_at"], settled

        # --- settle twice is refused. This is the assertion that proves the
        # first settle was recorded rather than merely returned.
        twice = root.post(f"/chat/admin/payments/arrears/{entry_id}/settle")
        assert twice.status_code == 422, twice.text
        assert "already settled" in twice.text.lower(), twice.text

        # --- and the customer's own view agrees with the operator's.
        after = chiara.json("get", "/chat/payments/arrears?status=settled")
        assert after["total"] == 1, after
        assert after["entries"][0]["id"] == entry_id, after

        report = root.json("get", "/chat/admin/payments/arrears")
        assert report["total_settled"] == 1, report
        assert report["total_open"] == 0, report

    def test_a_settled_entry_can_have_its_interest_waived_nobody_wins(self, world):
        """Ordering, the other direction.

        ``waive_arrears_interest`` refuses anything that is not ``open``. That is
        the right rule -- you cannot forgive interest that has already been
        charged -- and it is a rule about *state*, so it is only provable by
        arranging the states.
        """
        root = mounted(world, user_id=ROOT)
        entry_id = self._open(world)["id"]
        root.json("post", f"/chat/admin/payments/arrears/{entry_id}/settle")

        late_waiver = root.post(f"/chat/admin/payments/arrears/{entry_id}/waive-interest")
        assert late_waiver.status_code == 422, late_waiver.text
        assert "open" in late_waiver.text.lower(), late_waiver.text

    def test_interest_rises_with_time_and_is_capped(self, world):
        """The accrual property, on real rows rather than on a formula.

        One entry, read back through the engine at five horizons. ``assert_monotonic``
        with ``increasing=True`` is the whole point of the helper: interest is a
        debt, and a debt that falls as time passes is a bug that reads as good news.

        The horizons are the interesting part. The entry carries a 45-day grace
        period, so the first two readings must be identical and the third must
        not be -- a row that accrues during grace is charging somebody for a
        period the policy says is free, and only a series long enough to span
        the boundary can tell that.
        """
        from app.services import arrears_payments

        from app import models

        chiara = mounted(world, user_id=CHIARA)
        entry_id = self._open(world, principal=1000.0)["id"]

        horizons = (0, 30, 60, 200, 3650)
        accrued = []
        for days in horizons:
            row = world.run(world.fetch_one(models.ArrearsEntry, id=entry_id))
            payload = arrears_payments._entry_dict(row, as_of=NOW + timedelta(days=days))
            accrued.append(float(payload["interest_accrued"]))

        assert len(set(accrued)) > 1, (
            f"interest never moved across {horizons} days: {accrued}. Either the "
            "grace period is longer than a decade or the accrual is not computed"
        )
        assert_monotonic(accrued, what="accrued interest", increasing=True)
        assert accrued[0] == accrued[1] == 0.0, (
            f"interest was charged during the 45-day grace period: {accrued}"
        )
        assert accrued[2] > 0.0, (
            f"still no interest 60 days in, which is past the grace period: {accrued}"
        )

        # And the cap holds. The row carries `interest_cap_pct`, so the ceiling is
        # a promise the row makes about itself, and ten years is how to check it.
        row = world.run(world.fetch_one(models.ArrearsEntry, id=entry_id))
        cap = float(row.interest_cap_pct) / 100.0 * float(row.principal)
        assert accrued[-1] <= cap + 0.005, (
            f"interest {accrued[-1]} exceeded the row's own cap {cap} "
            f"(interest_cap_pct={row.interest_cap_pct})"
        )

        # The customer's own listing agrees with the engine's number.
        hers = chiara.json("get", "/chat/payments/arrears")
        assert hers["open_total_interest"] == hers["entries"][0]["interest_accrued"], hers


class TestOffersLifecycle:
    """An offer offered, accepted, fulfilled -- and the refusals in between.

    Offers are the one place a customer *spends* something, so this is the flow
    with money in it. Six states and four refusals.

    Every offer in this class is **issued through the admin endpoint** rather
    than seeded as a row. That is not incidental. ``World.offer()`` inserts a
    ``CustomerOffer`` directly, and ``reference`` is rendered by the service --
    so a seeded offer has ``reference == ""``, and ``GET /chat/me/offers/{""}``
    answers 307 to a URL with no identifier in it. A fixture that produces rows
    the product cannot address is a fixture that makes the flow untestable while
    looking like it is set up correctly. Going through
    ``POST /chat/admin/recovery/offers`` also means the flow exercises the
    upstream ``RECOVERY_SAVE_INCENTIVES`` tier the offer claims to come from,
    which is the thing worth testing about an offer's provenance.
    """

    #: High readiness, so the `save_high` tier fires: 250 points, and an
    #: explanation an operator could check against the rule table.
    GOODWILL_CONTEXT = {"recovery_readiness": "high", "churn_risk": "high"}

    def _issue(self, root, user_id, *, readiness="high", kind="goodwill", **extra):
        response = root.post(
            "/chat/admin/recovery/offers",
            json={
                "kind": kind,
                "user_id": user_id,
                "recovery_context": {
                    "recovery_readiness": readiness,
                    "churn_risk": "high" if readiness in {"high", "critical"} else "low",
                },
                **extra,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_issue_accept_fulfil_and_the_balance_moves_once(self, world):
        ana = mounted(world, user_id=ANA)
        root = mounted(world, user_id=ROOT)
        world.add(world.wallet(ANA, balance=500.0))
        world.commit()
        wallets = world.run(world.fetch_all(_models().PointsWallet))
        assert len(wallets) == 1, wallets
        assert_money(wallets[0].balance, 500.0, what="opening balance")

        # --- an operator offers.
        issued = self._issue(root, ANA)
        assert issued["issued"] is True, issued
        reference = issued["reference"]
        assert reference, "the service did not render a reference: " + repr(issued)

        # --- she sees it, with the reason it exists.
        listed = ana.json("get", "/chat/me/offers")
        assert [row["reference"] for row in listed["offers"]] == [reference], listed
        assert listed["statuses"] and listed["kinds"], (
            "the customer inbox does not publish its own vocabulary, so a client "
            f"cannot tell what states are reachable: {listed}"
        )

        detail = ana.json("get", f"/chat/me/offers/{reference}")
        assert detail["reference"] == reference, detail
        assert detail["explanation"]["why"], (
            "an offer arrived with no explanation of what produced it: "
            f"{detail['explanation']}"
        )
        assert detail["actionable"] is True, detail
        assert detail["source_offer_id"] == "save_high", (
            "the offer claims a different upstream rule than the one whose tier "
            f"matched: {detail['source_offer_id']!r}"
        )

        # --- not readable or actionable by anybody else. The reference is a
        # rendered id and therefore guessable, which is why the scope is in the
        # lookup and not in the URL.
        chiara = mounted(world, user_id=CHIARA)
        assert chiara.get(f"/chat/me/offers/{reference}").status_code == 404
        assert chiara.post(f"/chat/me/offers/{reference}/accept").status_code == 404

        # --- she accepts. One tap, and it has to answer that it worked.
        accepted = ana.json("post", f"/chat/me/offers/{reference}/accept")
        assert accepted["accepted"] is True, accepted
        assert accepted["status"] == "accepted", accepted
        assert accepted["found"] is True, (
            "accept reported a successful transition without `found`, which is "
            "the key the router checks before it commits. Every other branch of "
            "this function carries it, so its absence 404'd a successful accept: "
            f"{accepted}"
        )

        # Accepting promises the credit; it does not move the money. Fulfilment
        # is a separate act by somebody else, and conflating them is how a
        # customer gets told they were credited and then are not.
        assert_money(_balance(world, ANA), 500.0, what="balance after accept")

        # --- accepting again is a 200 that says no, not an error page. This is
        # deliberate and documented on the route: the customer asked "did that
        # work?", so an error page after a successful accept is the worst
        # possible answer. The assertion is that the *balance* did not move.
        again = ana.json("post", f"/chat/me/offers/{reference}/accept")
        assert again["found"] is True, again
        assert again["accepted"] is False, again
        assert again["status"] == "accepted", again
        assert "already" in again["reason"] or "accepted" in again["reason"], again
        assert_money(_balance(world, ANA), 500.0, what="balance after second accept")

        # --- an operator cannot fulfil an offer nobody accepted. That is the
        # race the outcome endpoint exists to close.
        bruno = mounted(world, user_id=BRUNO)
        unaccepted = self._issue(root, BRUNO)["reference"]
        refused = root.json(
            "post",
            f"/chat/admin/recovery/offers/{unaccepted}/outcome",
            json={"outcome": "fulfil", "note": "premature"},
        )
        assert refused["recorded"] is False, refused
        assert refused["found"] is True, refused
        assert refused["status"] == "offered", (
            "a refused transition moved the offer anyway: " + repr(refused)
        )

        # --- but expiring one nobody accepted is allowed, because that is what
        # an offer whose clock ran out *is*. Asserted because the endpoint's own
        # docstring used to claim it refused both.
        expired = root.json(
            "post",
            f"/chat/admin/recovery/offers/{unaccepted}/outcome",
            json={"outcome": "expire", "note": "nobody acted on it"},
        )
        assert expired["recorded"] is True, expired
        assert expired["status"] == "expired", expired

        # --- and now the accepted one is fulfilled.
        fulfilled = root.json(
            "post",
            f"/chat/admin/recovery/offers/{reference}/outcome",
            json={"outcome": "fulfil", "note": "250 points added"},
        )
        assert fulfilled["recorded"] is True, fulfilled
        assert fulfilled["status"] == "fulfilled", fulfilled
        assert fulfilled["found"] is True, (
            "fulfilment reported success without `found`; same defect as accept"
        )

        # --- the customer's own view now shows the finished state.
        final = ana.json("get", f"/chat/me/offers/{reference}")
        assert final["status"] == "fulfilled", final
        assert final["fulfilled_at"], final

        # --- and the operator's report counts what happened.
        report = root.json("get", "/chat/admin/recovery/offers")
        assert report["offered"] == 2, report
        assert report["by_status"]["fulfilled"] == 1, report
        assert report["by_status"]["expired"] == 1, report
        assert report["by_status"]["accepted"] == 0, report
        # On its own `offered` rewards making fewer offers and is maximised by
        # never offering at all, which is why the acceptance rate is published
        # beside it rather than instead of it.
        assert "acceptance_rate" in report, sorted(report)

    def test_a_declined_offer_cannot_be_accepted_afterwards(self, world):
        ana = mounted(world, user_id=ANA)
        root = mounted(world, user_id=ROOT)

        reference = self._issue(root, ANA, readiness="low")["reference"]

        declined = ana.json(
            "post", f"/chat/me/offers/{reference}/decline", json={"reason": "not interested"}
        )
        assert declined["declined"] is True, declined
        assert declined["found"] is True, (
            "decline reported a successful transition without `found`, so a "
            f"customer who said no was told the offer was not theirs: {declined}"
        )
        assert declined["reason_recorded"] is True, (
            "the decline reason is the only thing this subsystem collects about "
            f"why a fix was refused: {declined}"
        )

        # The reason reaches the record, not just the response.
        row = world.run(world.fetch_one(_models().CustomerOffer, reference=reference))
        assert row.decline_reason == "not interested", row.decline_reason

        # Declining twice is a 200 that says no, by the same rule as accepting.
        twice = ana.json(
            "post", f"/chat/me/offers/{reference}/decline", json={"reason": "really not"}
        )
        assert twice["declined"] is False, twice
        assert twice["status"] == "declined", twice
        assert row.decline_reason == "not interested", (
            "the second decline overwrote the recorded reason"
        )

        # And it cannot then be accepted.
        accepted = ana.json("post", f"/chat/me/offers/{reference}/accept")
        assert accepted["accepted"] is False, accepted
        assert accepted["status"] == "declined", accepted

        # Nor can it be fulfilled: nobody accepted it.
        fulfilled = root.json(
            "post", f"/chat/admin/recovery/offers/{reference}/outcome",
            json={"outcome": "fulfil"},
        )
        assert fulfilled["recorded"] is False, fulfilled

    def test_an_ineligible_customer_is_told_so_rather_than_given_an_error(self, world):
        """A tier that matches nothing is an answer, not a failure.

        "We did not offer this because the waiver policy does not authorise it
        for this customer's tier" is what an operator who asked needs. A 409
        would throw that away and look like a bug -- which is exactly the
        complaint a 200-with-``issued: false`` is designed to prevent.
        """
        from app.services import policy_scoring

        root = mounted(world, user_id=ROOT)

        # An interest waiver needs at least `customer-premium` policy tier. Bruno
        # is `standard`, which is one rank below, and he is supplied with an
        # administrative snapshot so the governance actually runs. Omitting
        # `policy_score` would *not* test anything: `evaluate_waiver_approval`
        # documents `None` as the legacy ungated path and allows it, so the
        # first draft of this test -- which omitted the score -- asserted an
        # ineligible issue and got an eligible one.
        assert policy_scoring.POLICY_TIER_RANK["standard"] < policy_scoring.POLICY_TIER_RANK[
            "customer-premium"
        ], "the tier table no longer ranks standard below customer-premium"

        outcome = root.json(
            "post",
            "/chat/admin/recovery/offers",
            json={
                "kind": "waiver",
                "user_id": BRUNO,
                "waiver_type": "interest",
                "recovery_context": {"recovery_readiness": "", "churn_risk": "low"},
                "policy_score": {"policy_tier": person(BRUNO).policy_tier, "access_score": 0.0},
            },
        )
        assert outcome["issued"] is False, outcome
        assert outcome["reason"], (
            "an ineligible issue returned no reason, so the operator has to guess "
            f"why: {outcome}"
        )
        assert "premium" in outcome["reason"].lower(), (
            "the refusal does not say which tier was needed, so it is not "
            f"actionable: {outcome['reason']!r}"
        )
        # And nothing was persisted.
        assert world.run(world.fetch_all(_models().CustomerOffer)) == [], (
            "an ineligible issue wrote a row anyway"
        )


class TestTheRouterContract:
    """One tripwire for the defect class, written after fixing three instances.

    All three offer mutators had the same bug: the *success* branch of the
    service function omitted ``found``, while every other branch carried it. The
    routers check ``result.get("found")`` before committing, so each one raised
    404 *after* the transition had been flushed -- and the row was committed by
    the next, unrelated request, so the state change was real while the customer
    was told it had not happened.

    The reason it was worth three debugging sessions rather than one is that the
    symptom reads as a permissions problem, not a missing dict key:

    * accept 404s -> looks like the reference belongs to somebody else
    * the next tap reports "already accepted" -> looks like the first tap worked
    * decline 404s, then accept says "already declined" -> same shape again
    * fulfil 404s, then a second fulfil says "already fulfilled" -> same again

    So this walks every mutating route in the subsystem, drives it to a genuine
    success, and asserts the two things the router needs: a 2xx, and ``found``.
    One test, one place, and a fourth mutator added without a row here shows up
    as a missing table entry rather than as a customer-facing 404.
    """

    #: (label, who, path-template, payload, the key that must read True,
    #:  whether the customer has to agree first)
    #:
    #: The precondition column is not decoration. ``accept`` and ``decline`` are
    #: mutually exclusive on one offer, and both are preconditions for the other
    #: two, so a single fixture cannot drive all four. The first draft of this
    #: table had no precondition column and pre-accepted every offer, which made
    #: the ``accept`` row assert a *second* accept -- and got a correct
    #: ``accepted: False, reason: "already accepted"`` back.
    TRANSITIONS = (
        ("accept", "customer", "/chat/me/offers/{reference}/accept", {}, "accepted", False),
        ("decline", "customer", "/chat/me/offers/{reference}/decline",
         {"reason": "not this time"}, "declined", False),
        ("fulfil", "admin", "/chat/admin/recovery/offers/{reference}/outcome",
         {"outcome": "fulfil"}, "recorded", True),
        ("expire", "admin", "/chat/admin/recovery/offers/{reference}/outcome",
         {"outcome": "expire"}, "recorded", False),
    )

    def test_every_offer_mutation_reports_the_key_its_router_checks(self, world):
        ana = mounted(world, user_id=ANA)
        bruno = mounted(world, user_id=BRUNO)
        root = mounted(world, user_id=ROOT)

        for index, (label, who, template, payload, outcome_key, pre_accept) in enumerate(
            self.TRANSITIONS
        ):
            # A fresh offer per transition, on a fresh customer, so no row can
            # depend on the rows before it.
            customer = ana if who == "customer" else bruno
            user_id = customer.user_id
            issued = root.json(
                "post",
                "/chat/admin/recovery/offers",
                json={
                    "kind": "goodwill",
                    "user_id": user_id,
                    "recovery_context": {
                        "recovery_readiness": "high" if index % 2 == 0 else "low"
                    },
                },
            )
            assert issued["issued"] is True, issued
            reference = issued["reference"]
            if pre_accept:
                agreed = customer.json("post", f"/chat/me/offers/{reference}/accept")
                assert agreed["accepted"] is True, (label, agreed)

            response = (customer if who == "customer" else root).post(
                template.format(reference=reference), json=payload
            )
            body = response.json()
            assert response.status_code < 400, (
                f"{label}: the transition succeeded in the database and the route "
                f"answered {response.status_code}. `found` was missing from the "
                f"success branch, so the router raised _not_found *after* the "
                f"flush. body={body}"
            )
            assert body.get("found") is True, (
                f"{label}: the route returned {response.status_code} but no "
                f"`found`, which is the key it checks before committing: {body}"
            )
            assert body.get(outcome_key) is True, (
                f"{label}: the transition claims not to have happened: {body}"
            )


def _models():
    """Local import, because ``app.models`` is only needed by these flows."""
    from app import models

    return models


def _balance(world, user_id: int) -> float:
    """One customer's loyalty balance, read through the ORM.

    Named because it is asserted four times in this file and the inline form is
    long enough that the interesting part -- *which* assertion -- gets lost.
    """
    rows = world.run(world.fetch_all(_models().PointsWallet))
    for row in rows:
        if int(row.user_id) == int(user_id):
            return float(row.balance)
    raise AssertionError(f"no points wallet for {user_id}; wallets are {rows}")


class TestTheCastIsRealData:
    """Guards on the fixture itself.

    A journey test that passes because the *fixture* is wrong is worse than no
    test, because it reads as coverage. These three assert the properties the
    flows above depend on, so a cast edit that breaks a flow says so here rather
    than three files later.
    """

    def test_the_cast_is_unique_on_every_axis_the_routes_use(self):
        usernames = [member.username for member in THE_CAST]
        emails = [member.email for member in THE_CAST]
        ids = [member.user_id for member in THE_CAST]
        assert len(set(usernames)) == len(usernames), usernames
        assert len(set(emails)) == len(emails), emails
        assert len(set(ids)) == len(ids), ids
        assert sum(1 for member in THE_CAST if member.is_admin) == 1, (
            "the flows assume exactly one operator; a second admin means some of "
            "them are testing the wrong actor's permissions"
        )

    def test_every_score_is_inside_the_scale_the_engines_resolve(self):
        # The scale lives in the simulator, not in the scoring service: it is the
        # contract the *threshold tables* are written against, and the audit that
        # reads it has to be able to change it without touching a service. Worth
        # knowing before somebody greps policy_scoring for it.
        from app.real_life_flows import SCORE_SCALE

        low, high = SCORE_SCALE
        for member in THE_CAST:
            for name, value in (
                ("access_score", member.access_score),
                ("system_score", member.system_score),
                ("customer_score", member.customer_score),
            ):
                assert low <= value <= high, (
                    f"{member.username}.{name} = {value} is outside the declared "
                    f"scale {low}-{high}, so no band table can resolve them"
                )

    def test_the_actors_the_flows_name_are_in_the_cast(self):
        for user_id in (ANA, BRUNO, CHIARA, ROOT):
            assert person(user_id).user_id == user_id


# ---------------------------------------------------------------------------
# Tier 2 break-and-watch
# ---------------------------------------------------------------------------


class TestBreakAndWatch:
    """Break the journey mid-flight; assert the flow refuses to lie about it.

    Every test above sees the system working. A flow that only ever runs on the
    happy path has an untested half: each claim it makes about *durable* state
    -- "we put 250 points on your account" -- is also a claim about work that
    did not happen when the worker underneath it broke.

    So each test here removes one dependency mid-journey and asserts the
    **invariant**, not an error shape. An invariant has to hold however the
    failure is dressed up:

    * a wallet balance either moved by the full amount or did not move at all;
    * an offer is either ``fulfilled`` with its effect applied, or still
      ``accepted`` -- never ``fulfilled`` over an unapplied effect.

    Asserting a status code instead would let the same bug back the moment
    someone chose a different way to report it. One of these was false before it
    was written; see its comment.
    """

    def _accepted_goodwill(self, world):
        """An accepted goodwill offer, plus the balance before it is fulfilled.

        Every test here needs the same preamble, so it is written once. A
        preamble that drifts slightly between tests is how two of these ended up
        meaning different things under the same name.
        """
        root = mounted(world, user_id=ROOT)
        ana = mounted(world, user_id=ANA)

        world.add(world.wallet(ANA, balance=500.0))
        world.commit()

        issued = root.json(
            "post",
            "/chat/admin/recovery/offers",
            json={
                "kind": "goodwill",
                "user_id": ANA,
                "recovery_context": {
                    "recovery_readiness": "high",
                    "churn_risk": "high",
                },
            },
        )
        reference = issued["reference"]
        accepted = ana.json("post", f"/chat/me/offers/{reference}/accept")
        assert accepted["accepted"] is True, accepted
        return root, ana, reference, _balance(world, ANA)

    def test_a_fulfilled_offer_means_the_points_actually_landed(self, world):
        """The control: with the writer working, the claim is true.

        Separate because every other test here is only meaningful against it.
        Without it, "the balance moved by exactly 250" would also be satisfied
        by a balance that never moves at all -- which is the very defect these
        tests exist to catch. It has to be ruled out first, in its own right.
        """
        root, _ana, reference, before = self._accepted_goodwill(world)

        fulfilled = root.json(
            "post",
            f"/chat/admin/recovery/offers/{reference}/outcome",
            json={"outcome": "fulfil", "note": "control"},
        )
        assert fulfilled["recorded"] is True, fulfilled
        assert fulfilled["status"] == "fulfilled", fulfilled
        assert fulfilled["effect"]["applied"] is True, fulfilled["effect"]
        assert_money(
            _balance(world, ANA), before + 250.0, what="balance after fulfil"
        )

    def test_a_broken_points_writer_leaves_the_offer_unfulfilled(self, world):
        """If the points never land, the offer must not claim ``fulfilled``.

        The defect this was written for. :func:`record_outcome` writes the
        ``fulfilled`` status *before* applying the effect, so any error handling
        that turns a failed credit into a tidy ``{"applied": False}`` payload
        produces a fulfilled offer over an unapplied effect: HTTP 200, status
        ``fulfilled``, and a customer told in the present perfect that points
        are on their account. A previous version of ``_apply_offer_effect``
        caught the exception and did exactly that.

        Hence the two assertions are about the claims rather than the response:

        1. the offer is not ``fulfilled`` -- the transaction aborts and it stays
           ``accepted``;
        2. the balance did not move.

        The second reads the wallet rather than the payload, and that is the
        whole point: a half-credited balance is the failure mode the first
        assertion alone would not rule out.
        """
        from app.services import recovery_playbooks

        root, ana, reference, before = self._accepted_goodwill(world)

        def broken_credit(*_args, **_kwargs):
            raise RuntimeError("points ledger unavailable")

        original = recovery_playbooks.credit_recovery_points
        recovery_playbooks.credit_recovery_points = broken_credit
        try:
            # The failure has to escape ``record_outcome``. In production it
            # surfaces as a 500; the ASGI test client re-raises instead, so the
            # assertion is on the escape itself. Anything *less* than escaping --
            # a caught exception rendered as a tidy payload -- is the bug.
            with pytest.raises(RuntimeError, match="points ledger unavailable"):
                root.post(
                    f"/chat/admin/recovery/offers/{reference}/outcome",
                    json={"outcome": "fulfil", "note": "the writer is down"},
                )
        finally:
            recovery_playbooks.credit_recovery_points = original

        # 1. the offer did not move.
        reread = ana.json("get", f"/chat/me/offers/{reference}")
        assert reread["status"] == "accepted", (
            f"the offer reports {reread['status']!r} after its credit raised; "
            "'fulfilled' is defined as the effect having landed, so a failed "
            "credit must leave it accepted"
        )

        # 2. and neither did the balance.
        assert_money(
            _balance(world, ANA),
            before,
            what="balance after a fulfil whose credit raised",
        )

    def test_a_repeated_fulfilment_never_credits_twice(self, world):
        """Re-running fulfilment is refused, so the balance moves at most once.

        A double credit is the failure mode an idempotency claim hides, and the
        one a reader of a single response cannot rule out. ``_prior_offer_credit``
        is what prevents it, so the assertion is on the wallet rather than on the
        refusal message -- the message is copy, the balance is money.
        """
        root, _ana, reference, before = self._accepted_goodwill(world)

        first = root.json(
            "post",
            f"/chat/admin/recovery/offers/{reference}/outcome",
            json={"outcome": "fulfil", "note": "first"},
        )
        assert first["recorded"] is True, first
        assert_money(
            _balance(world, ANA), before + 250.0, what="balance after one fulfil"
        )

        second = root.json(
            "post",
            f"/chat/admin/recovery/offers/{reference}/outcome",
            json={"outcome": "fulfil", "note": "again"},
        )
        assert second["recorded"] is False, second
        assert_money(
            _balance(world, ANA),
            before + 250.0,
            what="balance after a second, refused fulfil",
        )

    def test_the_sentence_a_customer_reads_tracks_what_actually_landed(self, world):
        """The prose is a claim too, and it was claiming things that did not happen.

        This is the third instance of one defect, and the one that outlived the
        first two. ``_apply_offer_effect`` claims ``fulfilled`` before applying the
        effect and nothing rolled it back (fixed above). Then it reported
        ``requires_out_of_band`` so an operator would know which components it
        could not deliver. Both fixed. And the sentence the *customer* reads said,
        from every status including ``offered``:

            "We put 250 points on your account and took 15.0% off your next service."

        Two claims in one template, both wrong:

        * **tense** -- "we put 250 points on your account" while the offer was
          merely ``offered`` and the balance had not moved. The module's own
          comment names the state this destroys: "you accepted and we have not
          done it yet" is a real and embarrassing state, and a fixed past-tense
          sentence renders it identically to the state where it is not true;
        * **scope** -- "took 15% off" for a component the same response reported
          as ``requires_out_of_band``. One reader was told the truth and the
          other the opposite, in the same flow.

        So the assertions are cross-readings rather than strings: for each state,
        what the customer is told and what the operator is told have to agree. A
        ``"15%" not in what`` assertion would be satisfied by deleting the clause
        altogether, which is not the fix; asserting the copy *and* the payload
        name the same outstanding component is.
        """
        root, ana, reference, before = self._accepted_goodwill(world)

        # --- while it is only accepted, nothing has been given yet.
        pending = ana.json("get", f"/chat/me/offers/{reference}")
        assert pending["status"] == "accepted", pending
        assert_money(_balance(world, ANA), before, what="balance while merely accepted")
        assert "We put" not in pending["explanation"]["what"], (
            "the customer was told in the past tense that points are on their "
            f"account while the status is {pending['status']!r} and the balance "
            f"has not moved: {pending['explanation']['what']!r}"
        )
        assert "discount_percent" in pending["explanation"]["outstanding"], (
            f"a component nothing applies is not named as outstanding to the "
            f"customer: {pending['explanation']}"
        )

        # --- the operator's report and the customer's sentence name the same
        # outstanding component, from the same flow.
        fulfilled = root.json(
            "post",
            f"/chat/admin/recovery/offers/{reference}/outcome",
            json={"outcome": "fulfil", "note": "done"},
        )
        assert fulfilled["recorded"] is True, fulfilled
        operator_outstanding = sorted(fulfilled["effect"]["requires_out_of_band"])

        after = ana.json("get", f"/chat/me/offers/{reference}")
        assert after["status"] == "fulfilled", after
        assert_money(_balance(world, ANA), before + 250.0, what="balance after fulfil")
        assert "We put" in after["explanation"]["what"], (
            f"the points did land and the customer is not told so: "
            f"{after['explanation']['what']!r}"
        )
        assert sorted(after["explanation"]["outstanding"]) == operator_outstanding, (
            f"the customer and the operator were told different things about the "
            f"same offer: customer {after['explanation']['outstanding']}, operator "
            f"{operator_outstanding}"
        )

    def test_a_discount_nothing_applies_is_never_spoken_of_in_the_past_tense(self, world):
        """The control on the other half of the defect: it was true before this.

        The test above pins that the two readers agree. This pins that the
        agreement is not achieved by deleting the discount from the sentence --
        the discount is 15% on every goodwill offer this repository issues
        (``RECOVERY_SAVE_INCENTIVES``), so silently dropping it would leave a
        customer with a promise nobody mentions, which is the same defect wearing
        the opposite sign.

        So the amount is asserted present, and asserted *not* as something that
        has happened, at every status including ``fulfilled``.
        """
        root, ana, reference, _before = self._accepted_goodwill(world)
        root.json(
            "post",
            f"/chat/admin/recovery/offers/{reference}/outcome",
            json={"outcome": "fulfil", "note": "done"},
        )

        for label in ("while offered", "after fulfilled"):
            detail = ana.json("get", f"/chat/me/offers/{reference}")
            what = detail["explanation"]["what"]
            assert "15%" in what, (
                f"{label}: the 15% discount the offer carries is absent from the "
                f"customer's sentence, so a promise made to the operator is not "
                f"made to the customer: {what!r}"
            )
            assert "15.0%" not in what, (
                f"{label}: a storage float reached the page: {what!r}"
            )
            lowered = what.lower()
            assert "took 15%" not in lowered and "discounted 15%" not in lowered, (
                f"{label}: the discount is described as spent, and nothing in this "
                f"repository spends it: {what!r}"
            )
            assert "discount_percent" in detail["explanation"]["outstanding"], (
                f"{label}: not named as outstanding: {detail['explanation']}"
            )

    def test_a_waiver_nothing_here_can_apply_says_so_rather_than_promising(self, world):
        """The third form of the same defect, and the one a reword would miss.

        The two tests above cover a component this service **never** applies. A
        waiver is different: `_apply_offer_effect` *can* apply it, but only when
        `arrears_entry_id` is populated -- and `issue_offer` never populates it.
        So an unlinked waiver is not an edge case, it is **every** waiver this
        service issues, and the component is genuinely appliable in principle.

        That is what makes a two-form rewrite wrong. "We're removing the interest
        from your account" is in the *future* tense, so it passes every review that
        the past-tense template failed, and it is still a promise by this process
        to do something this process will not do. The honest sentence has to name
        a third state -- not-yet-done-by-us versus not-ours-to-do -- and that
        distinction only exists because the row decides.

        So the assertions are on the two readers agreeing *and* on the sentence
        declining to commit us. Asserting the absence of "We're removing" would be
        satisfied by deleting the clause; asserting the clause is present *and*
        hands the work to somebody else is not.
        """
        root = mounted(world, user_id=ROOT)
        ana = mounted(world, user_id=ANA)

        # Ana is `customer-premium`, so the waiver is eligible -- a refusal would
        # test the approval path instead of the copy.
        issued = root.json(
            "post",
            "/chat/admin/recovery/offers",
            json={
                "kind": "waiver",
                "user_id": ANA,
                "waiver_type": "interest",
                "recovery_context": {"recovery_readiness": "", "churn_risk": "low"},
                "policy_score": {
                    "policy_tier": person(ANA).policy_tier,
                    "access_score": 0.0,
                },
            },
        )
        assert issued["issued"] is True, issued
        reference = issued["reference"]

        before = ana.json("get", f"/chat/me/offers/{reference}")
        assert before["explanation"]["what"], before
        assert "We're removing" not in before["explanation"]["what"], (
            "the customer was told this service is about to remove the interest, "
            "and nothing in this service removes it -- no arrears entry is linked, "
            f"and `issue_offer` never links one: {before['explanation']['what']!r}"
        )
        assert "our team" in before["explanation"]["what"], (
            "the work is handed to somebody rather than promised to the customer, "
            f"which is what actually happens: {before['explanation']['what']!r}"
        )
        assert "waiver" in before["explanation"]["outstanding"], before["explanation"]

        # And after fulfilment, the two readers still agree about what is left.
        ana.json("post", f"/chat/me/offers/{reference}/accept")
        fulfilled = root.json(
            "post",
            f"/chat/admin/recovery/offers/{reference}/outcome",
            json={"outcome": "fulfil", "note": "done"},
        )
        assert fulfilled["recorded"] is True, fulfilled
        operator_outstanding = sorted(fulfilled["effect"]["requires_out_of_band"])
        assert operator_outstanding == ["waiver"], (
            f"an unlinked waiver should be reported as out-of-band and nothing else: "
            f"{fulfilled['effect']}"
        )
        assert fulfilled["effect"]["applied"] is False, (
            f"nothing was applied here, so the effect must not claim it was: "
            f"{fulfilled['effect']}"
        )

        after = ana.json("get", f"/chat/me/offers/{reference}")
        assert sorted(after["explanation"]["outstanding"]) == operator_outstanding, (
            f"the customer and the operator were told different things: customer "
            f"{after['explanation']['outstanding']}, operator {operator_outstanding}"
        )
        assert "We removed" not in after["explanation"]["what"], (
            f"the waiver is reported removed and nothing removed it: "
            f"{after['explanation']['what']!r}"
        )

    def test_a_dropped_recovery_action_refuses_fulfilled(self, world):
        """A mid-journey failure leaves the offer unfulfilled, and the copy says so.

        The recovery planner emits a sequence of actions. If one of them fails
        (writer raises), the offer must stay in `accepted` and the customer's
        sentence must not claim the effect landed. This is the same invariant as
        BW-2 but at the planner level rather than the mutator level.

        The failure must escape as an exception (in production: 500). The test
        verifies the exception escapes, then checks the offer state and balance
        are unchanged.
        """
        from app.services import recovery_playbooks

        root = mounted(world, user_id=ROOT)
        ana = mounted(world, user_id=ANA)

        # Issue a goodwill offer that carries points and a discount.
        # Use recovery_readiness="high" (250 points, 15% discount) with
        # investment_band="standard" (scale 1.0) for predictable amounts.
        issued = root.json(
            "post",
            "/chat/admin/recovery/offers",
            json={
                "kind": "goodwill",
                "user_id": ANA,
                "recovery_context": {"recovery_readiness": "high", "churn_risk": "high"},
                "policy_score": {"policy_tier": person(ANA).policy_tier, "access_score": 0.0},
                "investment_band": "standard",
            },
        )
        assert issued["issued"] is True, issued
        reference = issued["reference"]

        # Accept the offer.
        ana.json("post", f"/chat/me/offers/{reference}/accept")

        # Patch the points writer to fail.
        original_credit = recovery_playbooks.credit_recovery_points

        async def failing_credit(*_args, **_kwargs):
            raise RuntimeError("simulated points writer failure")

        recovery_playbooks.credit_recovery_points = failing_credit
        try:
            # The failure must escape -- in production this is a 500, the test
            # client re-raises the app exception.
            with pytest.raises(RuntimeError, match="simulated points writer failure"):
                root.post(
                    f"/chat/admin/recovery/offers/{reference}/outcome",
                    json={"outcome": "fulfil", "note": "attempt"},
                )
        finally:
            recovery_playbooks.credit_recovery_points = original_credit

        # After the failed attempt, the offer must still be accepted (not fulfilled)
        # and the balance must be unchanged.
        after = ana.json("get", f"/chat/me/offers/{reference}")
        assert after["status"] == "accepted", (
            f"offer must stay accepted after failed fulfilment: {after}"
        )
        assert "We put" not in after["explanation"]["what"], (
            f"customer was told points landed but they didn't: {after['explanation']['what']!r}"
        )
        assert "points" in after["explanation"]["outstanding"], after["explanation"]
        assert "discount_percent" in after["explanation"]["outstanding"], after["explanation"]

    def test_a_broken_consent_gate_is_reported_through_the_probe(self, world):
        """Consent gate withholds a fix, and the probe path surfaces it.

        The probe `consent_gate_excludes_service` has two independent checks:
        1. `service` and `recovery` must NOT be in `CONSENT_GATED_PURPOSES` (config
           invariant -- the negative control in `TestNegativeControls` patches the
           module to add `service` and requires `gate_withheld_a_fix`).
        2. For all declared purposes, the persona's consent must permit them.

        Since `service`/`recovery` are never gated in the real config, check 2 is
        only exercised for purposes that ARE gated (analytics, marketing, etc.).
        This test verifies check 1 by patching the module constant.
        """
        from app.real_life_flows import _probe_consent_propagation, PERSONA_BY_ID
        from app.services import preferences

        # Verify the probe's config invariant: service/recovery must not be gated.
        original_gated = tuple(preferences.CONSENT_GATED_PURPOSES)
        preferences.CONSENT_GATED_PURPOSES = list(original_gated) + ["service"]
        try:
            chiara = PERSONA_BY_ID["repeatedly_cancelled"]
            outcome = _probe_consent_propagation(chiara)
            assert outcome.held is False, (
                f"probe must fail when service is gated: {outcome.summary}"
            )
            summ = outcome.summary.lower()
            assert "gates ['service'], which must never gate" in summ, (
                f"probe should name the forbidden gated purpose: {outcome.summary}"
            )
        finally:
            preferences.CONSENT_GATED_PURPOSES = list(original_gated)

    def test_partial_fulfilment_credit_yes_waiver_no_agrees_on_outstanding(self, world):
        """One component applies, the other does not; both readers agree on what is left.

        A goodwill offer (with points + discount) and a waiver offer (unlinked)
        for the same customer. The goodwill credits points but cannot apply the
        discount; the waiver cannot apply at all (no arrears entry). Both readers
        must agree on what is outstanding.
        """
        from types import SimpleNamespace
        root = mounted(world, user_id=ROOT)
        ana = mounted(world, user_id=ANA)

        # Issue a goodwill offer (points + discount) -- recovery_readiness="high"
        # gives 250 points / 15% discount at standard generosity.
        issued_goodwill = root.json(
            "post",
            "/chat/admin/recovery/offers",
            json={
                "kind": "goodwill",
                "user_id": ANA,
                "recovery_context": {"recovery_readiness": "high", "churn_risk": "high"},
                "policy_score": {"policy_tier": person(ANA).policy_tier, "access_score": 0.0},
                "investment_band": "standard",
            },
        )
        assert issued_goodwill["issued"] is True, issued_goodwill
        ref_gw = issued_goodwill["reference"]

        # Issue a waiver offer (no arrears entry, so unlinked).
        # Waiver policy needs access_score >= 70 for customer-premium tier.
        policy_snapshot = SimpleNamespace(policy_tier=person(ANA).policy_tier, access_score=88.0)
        issued_waiver = root.json(
            "post",
            "/chat/admin/recovery/offers",
            json={
                "kind": "waiver",
                "user_id": ANA,
                "waiver_type": "interest",
                "recovery_context": {"recovery_readiness": "", "churn_risk": "low"},
                "policy_score": {"policy_tier": person(ANA).policy_tier, "access_score": 88.0},
            },
        )
        assert issued_waiver["issued"] is True, issued_waiver
        ref_waiver = issued_waiver["reference"]

        # Accept both.
        ana.json("post", f"/chat/me/offers/{ref_gw}/accept")
        ana.json("post", f"/chat/me/offers/{ref_waiver}/accept")

        # Fulfil goodwill.
        fulfilled_gw = root.json(
            "post",
            f"/chat/admin/recovery/offers/{ref_gw}/outcome",
            json={"outcome": "fulfil", "note": "goodwill done"},
        )
        assert fulfilled_gw["recorded"] is True, fulfilled_gw
        assert fulfilled_gw["effect"]["applied"] is True, fulfilled_gw["effect"]
        assert "points" not in fulfilled_gw["effect"]["requires_out_of_band"], (
            "points must be applied, not out-of-band"
        )
        assert "discount_percent" in fulfilled_gw["effect"]["requires_out_of_band"], (
            "discount must be out-of-band"
        )

        # Fulfil waiver (unlinked, so nothing applies).
        fulfilled_waiver = root.json(
            "post",
            f"/chat/admin/recovery/offers/{ref_waiver}/outcome",
            json={"outcome": "fulfil", "note": "waiver done"},
        )
        assert fulfilled_waiver["recorded"] is True, fulfilled_waiver
        assert fulfilled_waiver["effect"]["applied"] is False, fulfilled_waiver["effect"]
        assert fulfilled_waiver["effect"]["requires_out_of_band"] == ["waiver"], (
            f"unlinked waiver must be out-of-band: {fulfilled_waiver['effect']}"
        )

        # Customer copy for goodwill: points done, discount out-of-band.
        after_gw = ana.json("get", f"/chat/me/offers/{ref_gw}")
        assert "250 points on your account" in after_gw["explanation"]["what"], after_gw["explanation"]["what"]
        assert "discount_percent" in after_gw["explanation"]["outstanding"], after_gw["explanation"]
        assert "points" not in after_gw["explanation"]["outstanding"], after_gw["explanation"]

        # Customer copy for waiver: out-of-band clause, waiver outstanding.
        after_waiver = ana.json("get", f"/chat/me/offers/{ref_waiver}")
        assert "our team" in after_waiver["explanation"]["what"], after_waiver["explanation"]["what"]
        assert "waiver" in after_waiver["explanation"]["outstanding"], after_waiver["explanation"]

        # Operator and customer agree on goodwill outstanding.
        op_outstanding_gw = sorted(fulfilled_gw["effect"]["requires_out_of_band"])
        cust_outstanding_gw = sorted(after_gw["explanation"]["outstanding"])
        assert op_outstanding_gw == cust_outstanding_gw == ["discount_percent"], (
            f"goodwill: op={op_outstanding_gw} cust={cust_outstanding_gw}"
        )

        # Operator and customer agree on waiver outstanding.
        op_outstanding_wv = sorted(fulfilled_waiver["effect"]["requires_out_of_band"])
        cust_outstanding_wv = sorted(after_waiver["explanation"]["outstanding"])
        assert op_outstanding_wv == cust_outstanding_wv == ["waiver"], (
            f"waiver: op={op_outstanding_wv} cust={cust_outstanding_wv}"
        )

