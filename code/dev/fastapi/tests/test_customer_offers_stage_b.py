"""Stage B: customer-facing recovery offers.

The behaviours under test are the ones where the obvious implementation is
wrong in a way that is expensive to discover later:

* **A preference about contact is not a preference about existence.** The gate
  governs the *notification*, so a reactive-only customer still receives the
  offer and can accept it. Getting this backwards hides a fix from exactly the
  people who asked not to be interrupted, which is the opposite of what they
  asked for.
* **Consent must never withhold a recovery or service offer.** A customer who
  reported a problem does not get told "we had a fix but you declined marketing".
* **Absence of investment signal is not low value.** Generosity defaults to
  1.0, never to zero.
* **An internal name is not customer-facing copy.** "Critical save offer" tells
  a customer our churn model graded them critical.
* **A one-tap path must never return an error page after a successful tap.**
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pathlib import Path
from tests._doubles import SqliteHarness


# ===========================================================================
# Fakes
# ===========================================================================


class _Result:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Db:
    """Records what was added; never executes SQL.

    ``select(...)`` returns a chainable fake so the service's query *building*
    runs for real and only the fetch is faked. A service function that grows a
    ``.where`` the fake cannot interpret fails here rather than passing silently.

    ``scope_user_id`` stands in for the ``user_id`` predicate the service adds
    when a customer-scoped load is requested. It exists because the scoping tests
    are the ones worth having -- "one customer cannot accept another's offer" is
    a claim about the *query*, and a fake that ignored the predicate would make
    it untestable and therefore unasserted.

    An honest stand-in, not a SQL engine: it filters one column and ignores
    every other clause. The real chain is exercised against a real database by
    ``tests/test_migration_chain.py``, which builds these tables from the
    migration and asserts them column-for-column.
    """

    def __init__(self, rows=None, *, scope_user_id=None):
        self.added = []
        self._rows = list(rows or [])
        self._scope = scope_user_id
        self.flushes = 0
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = len(self.added)
        return obj

    @pytest.mark.asyncio
    async def flush(self):
        self.flushes += 1

    @pytest.mark.asyncio
    async def commit(self):
        self.commits += 1

    @pytest.mark.asyncio
    async def rollback(self):
        pass

    @pytest.mark.asyncio
    async def execute(self, statement):
        rows = self._rows
        if self._scope is not None:
            rows = [row for row in rows if getattr(row, "user_id", None) == self._scope]
        return _Result(rows)

    def by_kind(self, kind: str):
        return [row for row in self.added if getattr(row, "offer_kind", None) == kind]

    def events(self):
        return [row for row in self.added if type(row).__name__ == "CustomerOfferEvent"]


# ===========================================================================
# Real-database helpers, for the tests that mutate
# ===========================================================================
#
# Anything that *changes* an offer needs a real database, because the change is
# now made by a conditional ``UPDATE`` and the correctness of the whole
# subsystem is the row count that comes back. ``_Db`` cannot answer that -- it
# returns its canned rows for every statement -- so a mutator test on ``_Db``
# would assert the service's own assumption back at itself and call it a pass.
# The queries are still built for real either way, so the ``_Db`` tests below
# keep doing what they are good at: pinning which predicates the service emits.


@pytest.fixture()
def harness():
    """A real in-memory database per test. See ``tests/_doubles.SqliteHarness``."""
    h = SqliteHarness()
    h.run(h.setup())
    try:
        yield h
    finally:
        h.run(h.teardown())
        h.close()


async def _offer_row(session, reference: str):
    from sqlalchemy import select

    from app import models

    result = await session.execute(
        select(models.CustomerOffer).where(models.CustomerOffer.reference == reference)
    )
    return result.scalars().first()


async def _event_kinds(session, reference: str) -> list[str]:
    from sqlalchemy import select

    from app import models

    row = await _offer_row(session, reference)
    result = await session.execute(
        select(models.CustomerOfferEvent)
        .where(models.CustomerOfferEvent.offer_id == int(row.id))
        .order_by(models.CustomerOfferEvent.id)
    )
    return [event.kind for event in result.scalars().all()]


async def _wallet_balance(session, user_id: int = 1) -> float:
    from sqlalchemy import select

    from app import models

    result = await session.execute(
        select(models.PointsWallet).where(models.PointsWallet.user_id == int(user_id))
    )
    wallet = result.scalars().first()
    return float(getattr(wallet, "balance", 0.0) or 0.0)


def _row_double(**overrides):
    """A plain-object offer row for the ``_Db`` query-building tests.

    Deliberately not an ORM instance. ``_Db`` never executes SQL, so the claim
    these tests carry is which *predicates the service builds* -- "one customer
    cannot accept another's offer" is a statement about the lookup, and a fake
    that ignored the ``user_id`` clause would make it unassertable.
    """
    values = {
        "id": 1,
        "user_id": 1,
        "reference": "OFF-00000001",
        "offer_kind": "goodwill",
        "source_offer_id": "save_high",
        "status": "offered",
        "headline": "A credit from us",
        "points": 250.0,
        "discount_percent": 0.0,
        "waiver_type": "",
        "priority": "",
        "generosity_scale": 1.0,
        "expires_at": None,
        "issued_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "accepted_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "declined_at": None,
        "fulfilled_at": None,
        "decline_reason": "",
        "follow_up_due_at": datetime(2026, 10, 3, tzinfo=timezone.utc),
    }
    values.update(overrides)
    row = type("_Row", (), values)()
    return row


class _Select:
    def where(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self


@pytest.fixture(autouse=True)
def _statement_is_fake(monkeypatch, request):
    """Make ``select`` return a chainable fake so the tests need no database.

    **Backs off for any test that asks for the ``harness`` fixture.** That opt-out
    is not a convenience -- it was added because this fixture was quietly applying
    to tests that needed a real database, and a file-wide fake is invisible from
    inside any one test. A test that swaps out the ORM's statement builder cannot
    assert anything about how a statement is evaluated, so every such test was
    passing on the service's own assumptions: the ``WHERE status = 'offered'`` on
    the conditional write that makes concurrent accept and decline safe was not
    being checked by anything, in this file or any other.

    The fixture stays for the tests it suits. What those tests are actually for
    is pinned in the comment above the real-database helpers.
    """
    from app.services import customer_offers

    if "harness" in request.fixturenames:
        return
    monkeypatch.setattr(customer_offers, "select", lambda *a, **k: _Select())


# ===========================================================================
# The gate: contact versus existence
# ===========================================================================


class TestContactGate:
    def test_reactive_only_still_receives_the_offer(self):
        """The requirement, stated as a test.

        `communication_frequency: only_reactive` means "do not interrupt me". It
        does not mean "do not tell me things" -- and an implementation that
        withholds the offer has turned a contact preference into a suppression.
        """
        from app.services import customer_offers

        gate = customer_offers.resolve_offer_contact(
            {"communication_frequency": "only_reactive"}, {}, purpose="recovery"
        )
        assert gate["issue"] is True
        assert gate["proactive"] is False
        assert any("reactive" in reason for reason in gate["reasons"])

    def test_quiet_hours_defer_the_push_with_a_time(self):
        """Deferred with a ``deferred_until``, never dropped.

        A scheduler needs to know *when* it may try again. "We did not tell them"
        is not actionable.
        """
        from app.services import customer_offers

        moment = datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc)
        gate = customer_offers.resolve_offer_contact(
            {
                "quiet_hours_enabled": True,
                "contact_window_start_hour": 9,
                "contact_window_end_hour": 18,
            },
            {},
            purpose="recovery",
            hour=23.0,
            now=moment,
        )
        assert gate["issue"] is True
        assert gate["proactive"] is False
        assert gate["deferred_until"] is not None
        assert "quiet hours" in " ".join(gate["reasons"])

    def test_the_cooldown_is_respected_and_names_its_numbers(self):
        from app.services import customer_offers

        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        gate = customer_offers.resolve_offer_contact(
            {"communication_frequency": "daily"},
            {},
            purpose="recovery",
            last_contact_at=now - timedelta(hours=3),
            now=now,
        )
        assert gate["proactive"] is False
        reason = " ".join(gate["reasons"])
        assert "3.0h" in reason and "24h" in reason
        assert gate["deferred_until"] is not None

    def test_no_preference_means_contact_now(self):
        from app.services import customer_offers

        gate = customer_offers.resolve_offer_contact({}, {}, purpose="recovery")
        assert gate["issue"] is True
        assert gate["proactive"] is True


class TestConsentNeverSuppressesAFix:
    """The invariant this subsystem is built around."""

    @pytest.mark.parametrize("purpose", ["service", "recovery"])
    def test_every_kind_issues_with_all_consent_withdrawn(self, purpose):
        from app.services import customer_offers

        gate = customer_offers.resolve_offer_contact(
            {}, {name: False for name in ("marketing", "analytics", "personalization")},
            purpose=purpose,
        )
        assert gate["issue"] is True, gate
        assert gate["service_critical"] is True
        assert gate["consent_gate_applied"] is False
        assert "not applied" in str(gate["consent"]["reason"])

    def test_a_marketing_purpose_is_still_gated(self):
        """So the exemption is narrow, and provably so.

        Reading the contact plan's top-level `permitted` for a marketing purpose
        is a real bug this test was written against: the plan reports the
        *service* gate at the top, so marketing offers were being created with
        the consent explicitly withdrawn.
        """
        from app.services import customer_offers

        gate = customer_offers.resolve_offer_contact({}, {"marketing": False}, purpose="marketing")
        assert gate["issue"] is False
        assert gate["service_critical"] is False

    def test_the_three_kinds_are_all_service_or_recovery(self):
        """A new kind that is *not* one of these is a decision, not an accident.

        `validate_offers` warns when a kind's purpose is consent-gated, so
        adding a campaign-flavoured fourth kind produces a warning rather than a
        silently suppressible fix.
        """
        from app.services import customer_offers
        from app.services import preferences

        for kind in customer_offers.OFFER_KINDS:
            purpose = customer_offers.offer_purpose(kind)
            assert purpose in {"service", "recovery"}, kind
            assert purpose not in preferences.CONSENT_GATED_PURPOSES, kind


# ===========================================================================
# Generosity
# ===========================================================================


class TestGenerosity:
    @pytest.mark.parametrize("band", ["unscored", "low", "standard", "", None])
    def test_an_absent_signal_never_scales_to_zero(self, band):
        """Failing closed on money owed to a customer is how this becomes a way
        of short-changing people.
        """
        from app.services import customer_offers

        result = customer_offers.resolve_offer_generosity({"investment_band": band})
        assert result["scale"] == 1.0

    def test_a_strategic_signal_scales_up_with_a_reason(self):
        from app.services import customer_offers

        result = customer_offers.resolve_offer_generosity({"investment_band": "strategic"})
        assert result["scale"] > 1.0
        assert result["rule_id"] == "generosity_strategic"
        assert result["reason"]

    def test_an_unrecognised_band_falls_back_rather_than_raising(self):
        from app.services import customer_offers

        assert customer_offers.resolve_offer_generosity({"investment_band": "martian"})["scale"] == 1.0

    def test_the_cap_is_applied_and_reported(self):
        """A tiny base amount rounded up by 1.5x is not generosity, it is noise."""
        from app.services import customer_offers

        base = 5.0
        scaled, note = customer_offers._apply_generosity(
            base, {"scale": 2.0, "rule_id": "generosity_strategic"}
        )
        assert note["cap_applied"] is True
        assert scaled <= base * 1.5

    @pytest.mark.asyncio
    async def test_the_scale_is_recorded_on_the_row(self):
        """So an amount cannot move under an offer already accepted.

        The scale is stored rather than recomputed: a customer who accepted 750
        points must not find 500 waiting for them because the investment signal
        was re-read at fulfilment time.
        """
        from app.services import customer_offers

        db = _Db()
        await customer_offers.issue_offer(
            db, 1, "goodwill",
            recovery_context={"recovery_readiness": "critical"},
            generosity=customer_offers.resolve_offer_generosity(
                {"investment_band": "strategic"}
            ),
        )
        assert db.by_kind("goodwill"), [type(r).__name__ for r in db.added]
        offer = db.by_kind("goodwill")[0]
        assert offer.generosity_scale > 1.0
        assert offer.points > 500.0
        # And the issued event carries it, so the trail explains the amount.
        issued = next(e for e in db.events() if e.kind == "issued")
        assert "generosity_scale" in issued.payload_json


# ===========================================================================
# Previews delegate to the engines that own the policy
# ===========================================================================


class TestPreviews:
    def test_goodwill_comes_from_the_recovery_incentive_table(self):
        from app.services import customer_offers

        preview = customer_offers.preview_goodwill_offer(
            {"recovery_readiness": "critical", "investment_band": "standard"}
        )
        assert preview["eligible"] is True
        assert preview["source_offer_id"] == "save_critical"
        assert preview["points"] == 500.0  # the table's value, unscaled

    def test_no_matching_tier_is_not_eligible_and_offers_nothing(self):
        """Handing a default-sized credit to everyone the sweep touches is how
        a goodwill programme becomes a cost centre.
        """
        from app.services import customer_offers

        preview = customer_offers.preview_goodwill_offer({"recovery_readiness": "none"})
        assert preview["eligible"] is False
        assert preview["points"] == 0.0
        assert "no RECOVERY_SAVE_INCENTIVES tier matched" in preview["reason"]

    def test_a_waiver_the_policy_refuses_is_not_offered(self):
        """An offer a customer can accept that the system would then refuse to
        apply makes the refusal the customer's problem.
        """
        from app.services import customer_offers

        refused = customer_offers.preview_waiver_offer(
            "interest", {"policy_tier": "standard", "access_score": 10.0}
        )
        assert refused["eligible"] is False
        assert refused["headline"] == ""

    def test_a_waiver_the_policy_authorises_is_offered(self):
        """Read the policy's own requirement rather than guessing it.

        `ARREARS_WAIVER_POLICY["interest"]` declares its minimum tier and score;
        hardcoding a pair here would make this test pass while the policy moved
        and this module went on offering waivers the policy now refuses.
        """
        from app.services import arrears_payments, customer_offers

        from app.routers.offers import PolicyScoreIn

        rule = arrears_payments.ARREARS_WAIVER_POLICY["interest"]
        # A plain dict does not work here and the failure is silent:
        # `evaluate_waiver_approval` reads `getattr(policy_score, "policy_tier")`,
        # a dict has no such attribute, and every waiver comes back denied for a
        # *missing tier*. That is the bug PolicyScoreIn exists to make
        # unrepresentable, so the test uses the same shape the router sends.
        snapshot = PolicyScoreIn(
            policy_tier=str(rule["required_policy_tier"]),
            access_score=float(rule["min_access_score"]) + 1.0,
        ).as_snapshot()
        allowed = customer_offers.preview_waiver_offer("interest", snapshot)
        assert allowed["eligible"] is True, allowed["approval"]
        assert allowed["source_offer_id"] == "interest"

        # And the denied case goes the other way, from the same shape.
        denied = customer_offers.preview_waiver_offer(
            "interest", PolicyScoreIn(policy_tier="standard", access_score=99.0).as_snapshot()
        )
        assert denied["eligible"] is False
        assert denied["headline"] == ""

    def test_priority_reads_the_review_rules_not_a_new_table(self):
        from app.services import customer_offers

        preview = customer_offers.preview_priority_offer({"recovery_readiness": "high"})
        assert preview["eligible"] is True
        assert preview["source_offer_id"] == "review_high"
        assert preview["validity_hours"] == 0.0  # a queue position has no expiry

    def test_the_priority_vocabulary_is_not_a_fourth_copy_of_priority_rank(self):
        """Two modules already declare `PRIORITY_RANK` (high/medium/low) and
        neither means queue urgency. Adding a third here would be the easy way
        to make three things called priority mean different things.
        """
        from app.services import customer_offers, recovery_playbooks

        review_priorities = {
            str(row["priority"]) for row in recovery_playbooks.RECOVERY_REVIEW_RULES
        }
        preview = customer_offers.preview_priority_offer({"recovery_readiness": "critical"})
        assert preview["priority"] in review_priorities
        assert preview["priority"] not in {"high", "medium", "low"} or (
            "urgent" in review_priorities
        )

    def test_an_unknown_kind_is_refused_naming_the_valid_set(self):
        from app.services import customer_offers

        with pytest.raises(ValueError, match="unknown offer kind"):
            customer_offers.preview_offer("voucher")


# ===========================================================================
# Customer-facing copy
# ===========================================================================


class TestCustomerFacingCopy:
    def test_the_internal_tier_name_never_reaches_the_headline(self):
        """"Critical save offer" tells a customer our churn model graded them
        critical -- unsettling, and a small disclosure of the scoring.
        """
        from app.services import customer_offers

        preview = customer_offers.preview_goodwill_offer({"recovery_readiness": "critical"})
        assert "critical" not in preview["headline"].lower()
        assert preview["internal_name"] == "Critical save offer"
        # The operator can still find out which tier fired.
        assert preview["source_offer_id"] == "save_critical"

    def test_every_kind_has_copy_and_every_promise_has_a_clause(self):
        """An offer a customer cannot understand is one they will decline.

        **This used to assert a ``what`` template per kind and no longer does,
        because there is no longer one.** The customer-facing sentence is composed
        per component by ``_component_sentence``, from ``_COMPONENT_CLAUSES``,
        because a kind's promise is not one claim -- ``goodwill`` promises points
        this service delivers *and* a discount it does not, and a template
        bundling the two was true in neither half.

        So the property this pins is now the one that actually matters: every kind
        carries the *context* copy (``why`` and ``legal``, which do not vary by
        component), and every promise a kind can make is described by a clause.
        The second half is the check that would have caught the defect -- a kind
        whose components are not in the table renders the bare fallback sentence,
        which is comprehensible and says nothing.
        """
        from app.services import customer_offers

        for kind, template in customer_offers.OFFER_EXPLANATIONS.items():
            assert template.get("why"), kind
            assert template.get("legal"), kind

        probe = customer_offers._ProbeRow()
        for kind in customer_offers.OFFER_KINDS:
            probe.offer_kind = kind
            probe.points = 1.0
            probe.discount_percent = 1.0
            probe.waiver_type = "interest"
            probe.arrears_entry_id = 1
            components = customer_offers._component_states(probe, "fulfilled")
            assert components, (
                f"{kind} promises nothing the clause tables can describe, so the "
                f"customer is shown the bare fallback sentence"
            )
            for component, _delivered in components:
                assert component in customer_offers._COMPONENT_CLAUSES, (
                    f"{kind} promises {component!r} but no clause describes it, so "
                    f"the promise reaches the operator and not the customer"
                )
                assert customer_offers._COMPONENT_CLAUSES[component], component

    def test_consent_changes_the_framing_and_never_the_substance(self):
        from app.services import customer_offers

        class _Row:
            offer_kind = "goodwill"
            reference = "OFF-00000001"
            points = 500.0
            discount_percent = 25.0
            waiver_type = ""
            generosity_scale = 1.0
            expires_at = None
            status = "offered"

        with_consent = customer_offers.explain_offer(_Row(), consents={"marketing": True})
        without = customer_offers.explain_offer(_Row(), consents={"marketing": False})

        assert without["framing"] == "service_only"
        assert with_consent["framing"] == "campaign"
        # The reason is given either way: this is a legitimate-interest
        # communication about something that happened to them.
        assert without["why"] == with_consent["why"]
        assert without["substance_withheld"] is False
        assert "nothing wrong" in without["why"] or without["why"]

    def test_the_plain_language_preference_still_means_terse(self):
        """Same meaning it has everywhere else in this codebase."""
        from app.services import customer_offers

        class _Row:
            offer_kind = "goodwill"
            reference = "OFF-00000002"
            points = 500.0
            discount_percent = 25.0
            waiver_type = ""
            generosity_scale = 1.0
            expires_at = None
            status = "offered"

        full = customer_offers.explain_offer(_Row(), preferences_map={"plain_language_explanations": True})
        terse = customer_offers.explain_offer(_Row(), preferences_map={"plain_language_explanations": False})
        assert full["terse"] is False and terse["terse"] is True
        assert len(terse["what"]) < len(full["what"])
        # And the reason survives the terse form.
        assert terse["why"]


# ===========================================================================
# The state machine
# ===========================================================================


class TestStateMachine:
    @pytest.mark.parametrize(
        "current,target,allowed",
        [
            ("offered", "accepted", True),
            ("offered", "declined", True),
            ("offered", "expired", True),
            ("accepted", "fulfilled", True),
            ("offered", "fulfilled", False),
            ("declined", "accepted", False),
            ("expired", "accepted", False),
            ("fulfilled", "accepted", False),
            ("fulfilled", "expired", False),
        ],
    )
    def test_the_transitions(self, current, target, allowed):
        from app.services import customer_offers

        assert customer_offers.resolve_offer_transition(current, target)["allowed"] is allowed

    def test_an_unknown_status_is_refused_not_guessed(self):
        """A machine that defaults to allowed will eventually revive a fulfilled
        offer, and the ledger will then say it was never fulfilled.
        """
        from app.services import customer_offers

        result = customer_offers.resolve_offer_transition("fulfilled", "banana")
        assert result["allowed"] is False
        assert result["unknown"] is True
        assert "banana" in result["reason"]

    def test_declining_is_terminal_and_re_offering_is_a_new_row(self):
        from app.services import customer_offers

        assert customer_offers.OFFER_TRANSITIONS["declined"] == ()
        # Which is what makes "we offered twice" answerable from the trail.
        event = customer_offers.OFFER_EVENT_KINDS
        assert "declined" in event and "issued" in event


# ===========================================================================
# Expiry
# ===========================================================================


class TestExpiry:
    def test_an_expired_offer_reads_as_expired_even_before_it_is_swept(self):
        """A customer returning after the window must see "expired", not a card
        that looks open and fails when they press accept.
        """
        from app.services import customer_offers

        class _Row:
            status = "offered"
            expires_at = datetime(2026, 9, 30, tzinfo=timezone.utc)

        assert customer_offers.effective_status(
            _Row(), now=datetime(2026, 10, 1, tzinfo=timezone.utc)
        ) == "expired"

    def test_a_naive_expiry_still_compares(self):
        """SQLite hands back naive datetimes; comparing one to an aware `now`
        raises, and this is the comparison every read performs.
        """
        from app.services import customer_offers

        class _Row:
            status = "offered"
            expires_at = datetime(2026, 9, 30)  # naive

        assert (
            customer_offers.effective_status(
                _Row(), now=datetime(2026, 10, 1, tzinfo=timezone.utc)
            )
            == "expired"
        )

    @pytest.mark.asyncio
    async def test_a_priority_offer_has_no_expiry_at_all(self):
        """A queue position with a 72-hour deadline is a deadline on the
        customer's problem rather than on our queue.
        """
        from app.services import customer_offers

        db = _Db()
        await customer_offers.issue_offer(
            db, 1, "priority", recovery_context={"recovery_readiness": "high"}
        )
        offer = db.by_kind("priority")[0]
        assert offer.expires_at is None
        assert offer.follow_up_due_at is None
        assert customer_offers.OFFER_KIND_BY_NAME["priority"]["has_expiry"] is False


# ===========================================================================
# The reference
# ===========================================================================


class TestReference:
    def test_it_is_the_primary_key_rendered(self):
        """Rendered, not counted.

        The complaints spine had this exact bug: an id built from a per-process
        counter reissued its own values after every redeploy, so two customers
        ended up holding the same reference.
        """
        from app.services import customer_offers

        assert customer_offers.offer_reference(42) == "OFF-00000042"
        refs = {customer_offers.offer_reference(i) for i in range(500)}
        assert len(refs) == 500

    @pytest.mark.asyncio
    async def test_issuing_sets_it_after_the_flush_that_assigned_the_id(self):
        from app.services import customer_offers

        db = _Db()
        result = await customer_offers.issue_offer(
            db, 7, "goodwill", recovery_context={"recovery_readiness": "high"}
        )
        assert result["reference"] == customer_offers.offer_reference(result["offer_id"])


# ===========================================================================
# Issuing
# ===========================================================================


class TestIssuing:
    @pytest.mark.asyncio
    async def test_an_ineligible_customer_gets_no_row_and_a_reason(self):
        from app.services import customer_offers

        db = _Db()
        result = await customer_offers.issue_offer(db, 1, "goodwill", recovery_context={})
        assert result["issued"] is False
        assert db.by_kind("goodwill") == []
        assert "RECOVERY_SAVE_INCENTIVES" in result["reason"]

    @pytest.mark.asyncio
    async def test_a_consent_gated_purpose_creates_nothing(self):
        from app.services import customer_offers

        db = _Db()
        result = await customer_offers.issue_offer(
            db, 1, "goodwill",
            recovery_context={"recovery_readiness": "high"},
            consents={"marketing": False},
            # A future campaign-flavoured kind would land here.
        )
        # A recovery offer still issues, because recovery is not consent-gated.
        assert result["issued"] is True

    @pytest.mark.asyncio
    async def test_issuing_records_the_issued_event_with_the_preview(self):
        from app.services import customer_offers

        db = _Db()
        await customer_offers.issue_offer(db, 1, "goodwill", recovery_context={"recovery_readiness": "high"})
        events = db.events()
        assert events[0].kind == "issued"
        assert events[0].to_status == "offered"
        assert "save_high" in events[0].payload_json

    @pytest.mark.asyncio
    async def test_a_deferred_notification_is_recorded_as_such(self):
        """The operator can then see we issued it and chose not to push, which
        is different from never having issued it.
        """
        from app.services import customer_offers

        db = _Db()
        await customer_offers.issue_offer(
            db, 1, "goodwill",
            recovery_context={"recovery_readiness": "high"},
            preferences_map={"communication_frequency": "only_reactive"},
        )
        kinds = [event.kind for event in db.events()]
        assert "issued" in kinds
        assert "notification_deferred" in kinds
        assert "notified" not in kinds

    @pytest.mark.asyncio
    async def test_a_normal_offer_records_a_notification(self):
        from app.services import customer_offers

        db = _Db()
        await customer_offers.issue_offer(db, 1, "goodwill", recovery_context={"recovery_readiness": "high"})
        assert "notified" in [event.kind for event in db.events()]

    @pytest.mark.asyncio
    async def test_the_automated_actor_is_not_a_user_id(self):
        from app.services import customer_offers

        assert customer_offers.AUTOMATED_ACTOR.startswith("system:")
        db = _Db()
        await customer_offers.issue_offer(db, 1, "goodwill", recovery_context={"recovery_readiness": "high"})
        assert db.events()[0].actor == customer_offers.AUTOMATED_ACTOR


# ===========================================================================
# Accept and decline
# ===========================================================================


class TestAccept:
    """Accept, against a real database.

    These were on the ``_Db`` double until the conditional write landed, and the
    double could not survive it -- ``_Db.execute`` returns its canned rows for
    *any* statement, so ``UPDATE ... WHERE status = 'offered'`` came back as a
    result with no row count and the invariant under test simply evaporated.
    That is the ``_Db`` docstring's own point ("a session double asserts your own
    assumptions back at you") arriving as a concrete failure rather than as a
    philosophy, so these tests now use ``SqliteHarness`` and the ``WHERE`` clause
    is genuinely evaluated.

    The doubles are still the right tool for the query-*building* tests below,
    where the claim is about which predicates the service emits.
    """

    @staticmethod
    async def _seed(harness, **overrides):
        """A real ``goodwill`` offer row, and the reference to act on."""
        from app import models

        # Use far-future dates so the test is independent of the clock.
        # The frozen clock in the test suite is NOW = 2026-10-01.
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        values = {
            "user_id": 1,
            "reference": "OFF-00000001",
            "offer_kind": "goodwill",
            "source_offer_id": "save_high",
            "status": "offered",
            "headline": "A credit from us",
            "points": 250.0,
            "discount_percent": 15.0,
            "waiver_type": "",
            "priority": "",
            "generosity_scale": 1.0,
            "expires_at": far_future,
            "issued_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "accepted_at": None,
            "declined_at": None,
            "fulfilled_at": None,
            "decline_reason": "",
            "follow_up_due_at": far_future,
        }
        values.update(overrides)
        session = harness.session
        session.add(models.User(id=1, username="u1", email="u1@e.com", full_name="U", hashed_password="x"))
        await session.flush()
        session.add(models.CustomerOffer(**values))
        await session.commit()
        return session, values["reference"]

    @pytest.mark.asyncio
    async def test_one_tap_accepts(self, harness):
        from app.services import customer_offers

        db, reference = await self._seed(harness)
        result = await customer_offers.accept_offer(db, reference, user_id=1)
        assert result["accepted"] is True
        assert result["found"] is True
        assert result["status"] == "accepted"
        assert result["awaiting_fulfilment"] is True
        # And the row really moved, which the double could not have told us.
        await db.refresh(await _offer_row(db, reference))
        assert (await _offer_row(db, reference)).status == "accepted"

    @pytest.mark.asyncio
    async def test_pressing_twice_is_not_an_error(self, harness):
        """The customer asked "did that work?" and the answer must never be a
        stack trace.
        """
        from app.services import customer_offers

        db, reference = await self._seed(harness)
        await customer_offers.accept_offer(db, reference, user_id=1)
        again = await customer_offers.accept_offer(db, reference, user_id=1)
        assert again["accepted"] is False
        assert again["found"] is True
        assert again["status"] == "accepted"
        assert "cannot be accepted" in again["reason"]

    @pytest.mark.asyncio
    async def test_accepting_an_expired_offer_says_so_and_persists_the_expiry(self, harness):
        from app.services import customer_offers

        db, reference = await self._seed(
            harness, expires_at=datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        result = await customer_offers.accept_offer(
            db, reference, user_id=1,
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert result["accepted"] is False
        assert result["status"] == "expired"
        # The row stops claiming to be open, so no later read rediscovers it.
        row = await _offer_row(db, reference)
        assert row.status == "expired"
        assert "expired" in await _event_kinds(db, reference)

    @pytest.mark.asyncio
    async def test_an_unknown_reference_is_reported_not_raised(self):
        from app.services import customer_offers

        result = await customer_offers.accept_offer(_Db([]), "OFF-99999999", user_id=1)
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_one_customer_cannot_accept_another_s_offers(self):
        """The reference is a rendered id and therefore guessable, so the scope
        is in the lookup and not in the handler.
        """
        from app.services import customer_offers

        result = await customer_offers.accept_offer(
            _Db([_row_double()], scope_user_id=999), "OFF-00000001", user_id=999
        )
        assert result["found"] is False


class TestDecline:
    @pytest.mark.asyncio
    async def test_declining_records_the_free_text_reason(self, harness):
        from app.services import customer_offers

        db, reference = await TestAccept._seed(harness)
        result = await customer_offers.decline_offer(
            db, reference, user_id=1, reason="I still have not had the repair"
        )
        assert result["declined"] is True
        assert result["found"] is True
        assert (await _offer_row(db, reference)).decline_reason == (
            "I still have not had the repair"
        )
        assert result["reason_recorded"] is True

    @pytest.mark.asyncio
    async def test_the_response_tells_them_the_problem_is_not_resolved(self, harness):
        """People read declining an apology as "the matter is closed", and then
        stop telling us about the thing that is still broken.
        """
        from app.services import customer_offers

        db, reference = await TestAccept._seed(harness)
        result = await customer_offers.decline_offer(db, reference, user_id=1)
        assert "not a statement that the problem is resolved" in result["next"]

    @pytest.mark.asyncio
    async def test_a_declined_offer_cannot_then_be_accepted(self, harness):
        from app.services import customer_offers

        db, reference = await TestAccept._seed(harness)
        await customer_offers.decline_offer(db, reference, user_id=1)
        after = await customer_offers.accept_offer(db, reference, user_id=1)
        assert after["accepted"] is False
        assert after["status"] == "declined"


# ===========================================================================
# Recording an outcome
# ===========================================================================


class TestRecordOutcome:
    """Fulfil and expire, against a real database.

    Real here for two reasons. The conditional write needs a real row count, as
    in :class:`TestAccept`. And the fulfilment path now *moves a wallet balance*,
    which a session double cannot represent at all -- so this class is where the
    second defect's fix is actually pinned.
    """

    @staticmethod
    async def _seed(harness, status="accepted", **overrides):
        return await TestAccept._seed(
            harness, status=status, discount_percent=0.0, **overrides
        )

    @pytest.mark.asyncio
    async def test_fulfilling_an_accepted_offer_records_it(self, harness):
        from app.services import customer_offers

        db, reference = await self._seed(harness)
        result = await customer_offers.record_outcome(
            db, reference, "fulfil", actor="admin:ana"
        )
        assert result["recorded"] is True
        assert result["status"] == "fulfilled"
        assert (await _offer_row(db, reference)).status == "fulfilled"
        assert await _event_kinds(db, reference) == ["fulfilled"]

    @pytest.mark.asyncio
    async def test_an_offer_nobody_accepted_cannot_be_fulfilled(self, harness):
        """Otherwise an operator could close an offer the customer is mid-tap on,
        and the loser of that race is a customer pressing accept on an error.
        """
        from app.services import customer_offers

        db, reference = await self._seed(harness, status="offered")
        result = await customer_offers.record_outcome(
            db, reference, "fulfil", actor="admin:ana"
        )
        assert result["recorded"] is False
        assert "offered -> fulfilled" in result["reason"]

    @pytest.mark.asyncio
    async def test_an_open_offer_can_be_expired_but_not_fulfilled(self, harness):
        """The asymmetry, which is the design.

        Expiry from `offered` is legitimate and has to be expressible twice over:
        an offer's own clock running out is what `expire_offers_due` does, and an
        operator withdrawing a mis-sent offer needs the same verb. Fulfilment is
        different -- it is the claim that a customer said yes and we then did it,
        so it is the one refused without a customer decision behind it.
        """
        from app.services import customer_offers

        db, reference = await self._seed(harness, status="offered")
        result = await customer_offers.record_outcome(
            db, reference, "expire", actor="admin:ana"
        )
        assert result["recorded"] is True
        assert result["status"] == "expired"
        # Expiring clears the follow-up clock: there is nothing left to chase.
        assert (await _offer_row(db, reference)).follow_up_due_at is None
        # ...and expiry applies no effect, so there is nothing to report having
        # done. Asserted because `effect` is deliberately absent here: a customer
        # whose offer expired did not receive anything, and a response that said
        # otherwise would be the original bug wearing a different hat.
        assert "effect" not in result

    def test_the_transitions_allow_expiry_but_refuse_fulfilment_from_offered(self):
        """Pinned as data, because the asymmetry is easy to "tidy" away."""
        from app.services import customer_offers

        assert "expired" in customer_offers.OFFER_TRANSITIONS["offered"]
        assert "fulfilled" not in customer_offers.OFFER_TRANSITIONS["offered"]

    @pytest.mark.asyncio
    async def test_accept_and_decline_are_not_operator_outcomes(self):
        """Offering them in this API would let a sweep make the customer's
        decision for them -- and a sweep that marks an offer *accepted* has
        recorded a consent the customer never gave, in a table whose whole
        purpose is to record consents and outcomes honestly.
        """
        from app.services import customer_offers

        for verb in ("accept", "decline", "accepted", ""):
            with pytest.raises(ValueError, match="belong to the customer"):
                await customer_offers.record_outcome(
                    _Db(), "OFF-00000001", verb, actor="x"
                )

    @pytest.mark.asyncio
    async def test_a_fulfil_on_an_open_offer_leaves_the_row_open(self, harness):
        """The refusal must be a no-op, not a write of the refused status.

        Recording `fulfilled` onto a row that is still `offered` would satisfy a
        later reader that checks only the status -- so the row keeps saying
        `offered` and the customer can still accept it.
        """
        from app.services import customer_offers

        db, reference = await self._seed(harness, status="offered")
        assert (await customer_offers.record_outcome(
            db, reference, "fulfil", actor="admin:ana"
        ))["recorded"] is False
        assert (await _offer_row(db, reference)).status == "offered"
        assert await _event_kinds(db, reference) == []

    @pytest.mark.asyncio
    async def test_a_refused_fulfilment_does_not_credit_anything(self, harness):
        """The refusal must be a no-op in *both* directions.

        The row not moving is the half that was always checked. The wallet not
        moving is the half that matters now that fulfilment has an effect to
        apply: a refusal that credited the points and then declined to record the
        status would be a customer given money for an offer still sitting open in
        their inbox.
        """
        from app.services import customer_offers

        db, reference = await self._seed(harness, status="offered")
        await customer_offers.record_outcome(db, reference, "fulfil", actor="admin:ana")
        assert await _wallet_balance(db) == 0.0
        assert "effect" not in (
            await customer_offers.record_outcome(db, reference, "fulfil", actor="admin:ana")
        )


# ===========================================================================
# Validation and catalog
# ===========================================================================


class TestValidation:
    def test_the_tables_are_valid(self):
        from app.services import customer_offers

        report = customer_offers.validate_offers()
        assert report["valid"] is True, report["error_list"]

    def test_a_renamed_resolver_is_an_error_not_a_warning(self):
        """A resolver that has been renamed is an offer that raises at the
        moment it is issued, to a customer, from the recovery sweep.
        """
        from app.services import customer_offers

        saved = customer_offers.CUSTOMER_OFFER_KINDS
        customer_offers.CUSTOMER_OFFER_KINDS = tuple(
            {**dict(row), "resolver_path": "app.services.recovery_playbooks:nope"}
            if str(row["offer_kind"]) == "goodwill"
            else dict(row)
            for row in saved
        )
        try:
            report = customer_offers.validate_offers()
            assert report["valid"] is False
            assert any("goodwill" in error for error in report["error_list"])
        finally:
            customer_offers.CUSTOMER_OFFER_KINDS = saved

    def test_a_missing_upstream_table_is_an_error(self):
        from app.services import customer_offers

        saved = customer_offers.CUSTOMER_OFFER_KINDS
        customer_offers.CUSTOMER_OFFER_KINDS = tuple(
            {**dict(row), "upstream": "NO_SUCH_TABLE"}
            if str(row["offer_kind"]) == "waiver"
            else dict(row)
            for row in saved
        )
        try:
            assert customer_offers.validate_offers()["valid"] is False
        finally:
            customer_offers.CUSTOMER_OFFER_KINDS = saved

    def test_a_transition_to_an_unknown_status_is_an_error(self):
        from app.services import customer_offers

        saved = customer_offers.OFFER_TRANSITIONS
        customer_offers.OFFER_TRANSITIONS = {"offered": ("banana",)}
        try:
            assert customer_offers.validate_offers()["valid"] is False
        finally:
            customer_offers.OFFER_TRANSITIONS = saved

    def test_the_catalog_publishes_the_policy(self):
        from app.services import customer_offers

        catalog = customer_offers.build_offer_catalog()
        assert {row["offer_kind"] for row in catalog["offer_kinds"]} == set(
            customer_offers.OFFER_KINDS
        )
        assert catalog["offer_statuses"] == list(customer_offers.OFFER_STATUSES)
        assert "no offer catalogue is declared here" in catalog["note"]


# ===========================================================================
# Routes
# ===========================================================================


class TestRoutes:
    def test_the_offer_routes_inherit_their_prefix_rule(self):
        """Authorization comes from the existing /chat wildcards *by prefix*.

        No new AUTHZ_RULES rows were added, so this test is the thing that stops
        a later reorder from quietly reclassifying an operator route as
        customer-readable: `chat_admin` is matched first-match-wins, and moving
        it below `chat_reads` would change these answers without changing a line
        of either router.
        """
        from app import deps
        from app.main import app

        expected = {
            ("GET", "/chat/me/offers"): ("chat_reads", "authenticated"),
            ("GET", "/chat/me/offers/{reference}"): ("chat_reads", "authenticated"),
            ("POST", "/chat/me/offers/{reference}/accept"): ("chat_writes", "authenticated"),
            ("POST", "/chat/me/offers/{reference}/decline"): ("chat_writes", "authenticated"),
            ("GET", "/chat/admin/recovery/offers"): ("chat_admin", "admin"),
            ("POST", "/chat/admin/recovery/offers"): ("chat_admin", "admin"),
            ("POST", "/chat/admin/recovery/offers/{reference}/outcome"): ("chat_admin", "admin"),
        }
        for (method, path), (rule_id, exposure) in expected.items():
            match = deps.match_authz_rule(method, path)
            assert match["rule_id"] == rule_id, (method, path)
            assert match["rule"]["exposure"] == exposure, (method, path)

    def test_every_offer_route_is_registered_under_the_expected_paths(self):
        from app.routers import offers

        paths = {route.path for route in offers.router.routes}
        assert paths == {
            "/chat/me/offers",
            "/chat/me/offers/{reference}",
            "/chat/me/offers/{reference}/accept",
            "/chat/me/offers/{reference}/decline",
            "/chat/admin/recovery/offers",
            "/chat/admin/recovery/offers",
            "/chat/admin/recovery/offers/{reference}/outcome",
        }

    def test_the_paths_are_not_doubled(self):
        """A prefix on the router produced /chat/chat/me/offers once already."""
        from app import deps
        from app.main import app

        served = {path for path, _ in deps.iter_authz_routes(app.routes)}
        assert "/chat/me/offers" in served
        assert "/chat/chat/me/offers" not in served

    def test_an_unknown_kind_is_422_and_never_reaches_the_resolver(self):
        """An exception in the middle of a recovery sweep is worse than a 422."""
        from fastapi.testclient import TestClient

        from app import deps
        from app.main import app

        class _Admin:
            id = 1
            username = "tester"
            is_admin = True

        @pytest.mark.asyncio
        async def _admin():
            return _Admin()

        app.dependency_overrides[deps.get_current_admin_user] = _admin
        try:
            response = TestClient(app).post(
                "/chat/admin/recovery/offers", json={"kind": "voucher", "user_id": 1}
            )
            assert response.status_code == 422
        finally:
            app.dependency_overrides.pop(deps.get_current_admin_user, None)

    def test_the_decline_body_forbids_extra_fields(self):
        """A body naming a status this endpoint does not set would otherwise be
        accepted and silently dropped, and a decline whose reason was ignored is
        the one thing this subsystem exists to collect.
        """
        from app.routers.offers import DeclineOfferIn

        with pytest.raises(Exception):
            DeclineOfferIn(reason="too busy", status="fulfilled")

    def test_the_outcome_body_refuses_accept_and_decline(self):
        """Not a style choice: `accept` reaching an operator endpoint would let a
        sweep make the customer's decision for them, and a sweep that marks an
        offer accepted has recorded a consent the customer never gave.
        """
        from typing import get_args

        from app.routers.offers import RecordOutcomeIn

        assert set(get_args(RecordOutcomeIn.model_fields["outcome"].annotation)) == {
            "fulfil",
            "expire",
        }

    def test_the_issue_body_names_only_the_three_kinds(self):
        from typing import get_args

        from app.routers.offers import IssueOfferIn

        assert set(get_args(IssueOfferIn.model_fields["kind"].annotation)) == {
            "goodwill",
            "waiver",
            "priority",
        }

# ===========================================================================
# Negative controls for the validator's structural concurrency checks
# ===========================================================================
#
# `customer_offers.validate_offers()` asserts that every mutator calls
# `_claim_transition` and that the two customer actions require an unexpired
# offer. Those checks are structural, and a structural check is worthless unless
# it is shown to fail.
#
# Each control loads a *reverted copy* of the module from disk and asks the
# validator. Copied-and-patched rather than monkeypatched, because the checks
# read the function's own source through `inspect.getsource`: patching a live
# attribute leaves the source intact, so the check would pass on a function that
# no longer does the thing. That is precisely the failure these controls exist to
# rule out, and it means a monkeypatch cannot test this at all.


#: Renaming this call in a copy is how the "mutator stops claiming" control is
#: written. Same signature as the real helper, so the reverted module still runs --
#: the point is only that the AST no longer contains the call.
_REVERTED_HELPER = (
    "async def _claim_transition(",
    "async def _reverted_noop(\n"
    "    db, row, target, *, values=None, now=None, require_unexpired=False\n"
    ") -> bool:\n"
    "    return False\n\n\n"
    "async def _claim_transition(",
)


def _load_reverted(tmp_path, replacements, *, only_in=None, hide_source_of=None):
    """A patched copy of ``customer_offers``, importable and independent.

    ``replacements`` is a list of ``(old, new)`` applied in order. ``only_in``
    scopes every replacement to the body of one top-level function, which matters
    here because several calls are textually identical in more than one place --
    a bare ``replace(..., 1)`` against the first occurrence patches whichever
    function happens to come first in the file, and the control then quietly
    tests something other than what it claims.
    """
    import importlib.util
    import sys

    from app.services import customer_offers as live

    source = Path(live.__file__).read_text(encoding="utf-8")
    if only_in is not None:
        head, sep, body = source.partition(f"async def {only_in}(")
        assert sep, f"{only_in} is gone; this control needs updating"
        for old, new in replacements:
            assert old in body, (
                f"{old[:60]!r} is not in {only_in} any more; the control is stale"
            )
            body = body.replace(old, new, 1)
        source = head + sep + body
    else:
        for old, new in replacements:
            assert old in source, f"negative control no longer applies: {old[:60]!r}"
            source = source.replace(old, new, 1)

    source = source.replace(*_REVERTED_HELPER, 1)
    target = tmp_path / "reverted_offers.py"
    target.write_text(source)
    spec = importlib.util.spec_from_file_location("reverted_offers", target)
    module = importlib.util.module_from_spec(spec)
    sys.modules["reverted_offers"] = module
    spec.loader.exec_module(module)
    if hide_source_of is not None:
        # Make `inspect.getsource(hide_source_of)` raise `OSError` for *that
        # function only*. `getsource` reads through `linecache` using the code
        # object's `co_filename`, so pointing that at a name with no file behind
        # it is the honest plant -- and it has to be per-function, because the
        # first version deleted the whole file and thereby hid every function in
        # the module. That version "passed" for the wrong reason: the other three
        # mutators became unmeasurable too, so it was testing the validator's
        # skip path four times and its reachability zero times.
        #
        # A realistic source of per-function unreadability is a decorator or
        # wrapper that returns a function object defined elsewhere.
        original = getattr(module, hide_source_of)
        original.__code__ = original.__code__.replace(co_filename="<unavailable>")
    return module


class TestTheStructuralChecksCanFail:
    """A structural check nobody has seen fail is a comment with an assert."""

    def test_the_live_module_validates(self):
        from app.services import customer_offers

        report = customer_offers.validate_offers()
        assert report["valid"] is True, report["errors"]
        assert report["errors"] == []

    def test_a_mutator_that_stops_claiming_its_transition_is_caught(self, tmp_path):
        """The defect, planted: ``record_outcome`` reads then writes again.

        This is the control that matters most, because it is the only way to know
        the check is doing anything rather than passing vacuously. It was written
        twice and failed twice before it worked:

        * The first version wrapped the call in ``if False and ...``, which leaves
          the call node in the AST -- so the check correctly reported the module as
          still guarded. The control was wrong, not the check.
        * A substring version of the check (looking for ``_claim_transition`` in
          the source text) *was* satisfied by the reverted function's own comment,
          and reported ``valid: True``.
        """
        module = _load_reverted(
            tmp_path,
            [
                (
                    "if not await _claim_transition(\n        db,\n        row,\n        target,",
                    "if not await _reverted_noop(\n        db,\n        row,\n        target,",
                )
            ],
            only_in="record_outcome",
        )
        report = module.validate_offers()
        assert report["valid"] is False, "the validator accepted an unguarded mutator"
        assert any(
            "record_outcome does not call _claim_transition" in e
            for e in report["errors"]
        ), report["errors"]
        # And it is the *only* complaint, so the control is not passing for a
        # reason unrelated to what it claims to test.
        assert len(report["errors"]) == 1, report["errors"]

    def test_a_customer_action_that_drops_the_clock_check_is_caught(self, tmp_path):
        """``accept_offer`` without ``require_unexpired``.

        The silent half of the guard: without the clock predicate, an offer whose
        validity elapsed between the read and the write can still be accepted, and
        nothing anywhere reports it.
        """
        module = _load_reverted(
            tmp_path,
            [
                (
                    'db, row, "accepted", values={"accepted_at": moment}, '
                    "now=moment, require_unexpired=True",
                    'db, row, "accepted", values={"accepted_at": moment}, now=moment',
                )
            ],
            only_in="accept_offer",
        )
        report = module.validate_offers()
        assert report["valid"] is False
        assert any(
            "accept_offer is a customer action but does not require an unexpired offer"
            in e
            for e in report["errors"]
        ), report["errors"]

    def test_the_sweep_demanding_an_unexpired_offer_is_caught(self, tmp_path):
        """``expire_offers_due`` with the flag it must never have.

        The worst failure mode of the three, because it is completely silent:
        the sweep selects *on* expiry, so requiring "unexpired" would refuse every
        row it selected, and offers would never close with no error anywhere. This
        is the check that has to catch it, and the control exists because a check
        whose failure mode is silence needs to be seen failing.
        """
        # Scoped to the sweep's own body, not the bare call: the same claim appears
        # inside `accept_offer`'s expiry-persistence branch, and a bare
        # `replace(..., 1)` patches whichever comes first in the file -- which is a
        # legitimate claim, just not the one this control is about.
        module = _load_reverted(
            tmp_path,
            [
                (
                    'if await _claim_transition(db, row, "expired", now=moment):',
                    'if await _claim_transition(db, row, "expired", '
                    "now=moment, require_unexpired=True):",
                )
            ],
            only_in="expire_offers_due",
        )
        report = module.validate_offers()
        assert report["valid"] is False
        assert any(
            "expire_offers_due requires an unexpired offer" in e
            for e in report["errors"]
        ), report["errors"]

    def test_a_past_tense_clause_for_something_nothing_applies_is_caught(self, tmp_path):
        """The original defect, reintroduced into the tables rather than the code.

        ``discount_percent`` had a sentence reading "We took {discount}% off your
        next service" while the same response reported it in
        ``requires_out_of_band``. The clause table now encodes the absence of that
        sentence, and this control proves the absence is checked rather than merely
        true today.

        It patches the *table*, not the renderer, because the renderer cannot be
        wrong here: ``_component_states`` decides tense from
        ``_components_this_service_applies`` and the table has to supply the
        sentence. Restoring the sentence is the only way to produce the original
        bug, so that is what is reverted.
        """
        module = _load_reverted(
            tmp_path,
            [
                (
                    '    "discount_percent": {\n'
                    '        "out_of_band": (\n'
                    '            "A {discount}% discount on your next service is noted '
                    'on your account "\n'
                    '            "for our team to apply."\n'
                    "        ),\n"
                    "    },",
                    '    "discount_percent": {\n'
                    '        "done": "We took {discount}% off your next service.",\n'
                    '        "out_of_band": (\n'
                    '            "A {discount}% discount on your next service is noted '
                    'on your account "\n'
                    '            "for our team to apply."\n'
                    "        ),\n"
                    "    },",
                )
            ],
        )
        report = module.validate_offers()
        assert report["valid"] is False, (
            "the validator accepted a past-tense clause for a component nothing in "
            "this service delivers"
        )
        assert any(
            "has a past-tense `done` clause" in e for e in report["errors"]
        ), report["errors"]
        # Named rather than counted: a count passes if the checks are renamed, and
        # this one is paired with a second diagnosis that fires alongside it.
        assert any(
            "_COMPONENT_TERSE" in e for e in report["errors"]
        ), (
            "adding a `done` clause to one table and not the other is the same "
            f"drift as the original defect, one level down: {report['errors']}"
        )

    def test_a_clause_missing_from_the_terse_form_is_caught(self, tmp_path):
        """The terse form is a second table, so it can drift from the first.

        ``plain_language_explanations`` off yields `_COMPONENT_TERSE`, and a
        component with a full sentence but no terse one would simply vanish for
        that customer -- a promise that appears to depend on a preference setting.
        The clause tables are separate because a truncation is not a shorter
        sentence ("250 points on the way" differs in tense from the full form, and
        the tense is the defect), and separate tables need a parity check.
        """
        module = _load_reverted(
            tmp_path,
            [
                (
                    '    "points": {\n'
                    '        "done": "{points} points credited",\n'
                    '        "pending": "{points} points on the way",\n'
                    "    },",
                    '    "points": {\n'
                    '        "pending": "{points} points on the way",\n'
                    "    },",
                )
            ],
        )
        report = module.validate_offers()
        assert report["valid"] is False, (
            "the validator accepted a component whose terse form is missing"
        )
        assert any(
            "in _COMPONENT_TERSE" in e for e in report["errors"]
        ), report["errors"]
        # Only the parity complaint. `points` *is* appliable here, so the
        # undeliverable-clause check must not fire -- a control that also trips an
        # unrelated check is not pinning what it claims to.
        assert not any(
            "past-tense `done` clause" in e for e in report["errors"]
        ), (
            f"the parity check fired alone as intended, but the undeliverable "
            f"check also fired on an appliable component: {report['errors']}"
        )

    def test_a_first_person_clause_for_something_we_never_apply_is_caught(self, tmp_path):
        """The subtle one: future tense, and still a false promise.

        Rewriting the past-tense template into the future tense passes every review
        the original failed -- it stops claiming a completed act -- and leaves the
        claim false in a new way: "We're taking 15% off your next service" is a
        commitment by *this* process to do something it does not do. A control
        that only looked for past tense would call that fixed.

        So this one plants exactly that sentence and requires the validator to
        object on the ground that no shape of row makes it appliable, rather than
        on tone or on the tense.
        """
        module = _load_reverted(
            tmp_path,
            [
                (
                    '    "discount_percent": {\n'
                    '        "out_of_band": (\n'
                    '            "A {discount}% discount on your next service is noted '
                    'on your account "\n'
                    '            "for our team to apply."\n'
                    "        ),\n"
                    "    },",
                    '    "discount_percent": {\n'
                    '        "pending": "We\'re taking {discount}% off your next '
                    'service.",\n'
                    '        "out_of_band": (\n'
                    '            "A {discount}% discount on your next service is noted '
                    'on your account "\n'
                    '            "for our team to apply."\n'
                    "        ),\n"
                    "    },",
                ),
                (
                    '    "discount_percent": {\n'
                    '        "out_of_band": "{discount}% discount noted for our '
                    'team",\n'
                    "    },",
                    '    "discount_percent": {\n'
                    '        "pending": "{discount}% off your next service",\n'
                    '        "out_of_band": "{discount}% discount noted for our '
                    'team",\n'
                    "    },",
                ),
            ],
        )
        report = module.validate_offers()
        assert report["valid"] is False, (
            "the validator accepted a first-person promise for a component this "
            "service never applies"
        )
        assert any(
            "first-person `pending` clause" in e for e in report["errors"]
        ), report["errors"]
        # Isolation, in both directions. The clause is still legitimately
        # out-of-band, so the missing-clause check must stay quiet; and a future
        # tense is not a past one, so the undeliverable-`done` check must not be
        # what caught it. A control satisfied by the wrong diagnosis would keep
        # passing after the check it names was deleted.
        assert not any(
            "needs an `out_of_band` clause" in e for e in report["errors"]
        ), report["errors"]
        assert not any(
            "past-tense `done` clause" in e for e in report["errors"]
        ), report["errors"]

    def test_a_form_nothing_selects_is_caught(self, tmp_path):
        """A fourth form is configuration that can never be read.

        The three forms the renderer selects between are named in `_CLAUSE_FORMS`,
        and a name outside that set is a sentence no code path can produce. It is
        the most plausible mistake of the next three, because adding a fourth way for
        a component to be spoken about *looks* like the extensible thing to do: the
        table is a dict of dicts and adding a key to it type-checks fine.

        So this control adds a form nobody selects and requires the complaint. Without
        the check, the clause would sit in the table looking deliberate, and the
        per-component render would keep reaching for `pending` — which is how the
        *first* defect came to describe an effect that never happened.
        """
        module = _load_reverted(
            tmp_path,
            [
                (
                    '    "priority": {\n'
                    '        "out_of_band": (\n'
                    '            "Your next request is flagged on your account for '
                    'our team to prioritise."\n'
                    "        ),\n"
                    "    },",
                    '    "priority": {\n'
                    '        "eventually": (\n'
                    '            "Your next request will be prioritised soon."\n'
                    "        ),\n"
                    '        "out_of_band": (\n'
                    '            "Your next request is flagged on your account for '
                    'our team to prioritise."\n'
                    "        ),\n"
                    "    },",
                ),
                (
                    '    "priority": {\n'
                    '        "out_of_band": "next request flagged for priority",\n'
                    "    },",
                    '    "priority": {\n'
                    '        "eventually": "next request prioritised soon",\n'
                    '        "out_of_band": "next request flagged for priority",\n'
                    "    },",
                ),
            ],
        )
        report = module.validate_offers()
        assert report["valid"] is False, (
            "the validator accepted a clause in a form no renderer selects"
        )
        assert any(
            "has an unrecognised tense" in e for e in report["errors"]
        ), report["errors"]
        # Isolation. The new form is unreadable but so is every other claim about
        # `priority`, and the check must be about the form rather than about
        # `priority` having lost its only honest clause -- otherwise deleting this
        # check would leave the missing-`out_of_band` check holding the door up and
        # this control would still pass.
        assert not any(
            "needs an `out_of_band` clause" in e for e in report["errors"]
        ), report["errors"]

    def test_a_component_this_service_cannot_fully_apply_needs_an_out_of_band_clause(
        self, tmp_path
    ):
        """`waiver` is appliable in principle and not in practice, so it needs three.

        `points` is appliable for every offer that carries it. `waiver` is appliable
        only when `arrears_entry_id` is populated -- and `issue_offer` never
        populates it, so the unlinked case is every waiver this service issues. That
        is the row deciding, and it is invisible to a check that probes one shape.

        Which is the trap this control sets: the natural implementation is "forbid
        `out_of_band` on anything this service can apply", which fires immediately
        on `waiver` and gets "fixed" by deleting the clause the unlinked case needs.
        The check has to distinguish never-appliable from not-always-appliable, and
        the only way to know that is to probe both shapes *and* ask whether the row
        carries the component at all -- a goodwill offer for zero points carries no
        points component, so an unfiltered second probe invents a third state for
        `points` too.

        So this control removes the clause and requires the specific complaint, and
        then requires that `points` -- which has no such case -- is *not* reported.
        """
        module = _load_reverted(
            tmp_path,
            [
                (
                    '        "out_of_band": (\n'
                    '            "The {waiver_type} removal is noted on your account '
                    'for our team to "\n'
                    '            "complete."\n'
                    "        ),\n",
                    "",
                ),
                (
                    '        "out_of_band": "{waiver_type} removal noted for our '
                    'team",\n',
                    "",
                ),
            ],
        )
        report = module.validate_offers()
        assert report["valid"] is False, (
            "the validator accepted an offer carrying a waiver that no clause "
            "describes for the case this service cannot handle"
        )
        assert any(
            "is carried by an offer this service cannot apply it to" in e
            for e in report["errors"]
        ), report["errors"]
        assert not any(
            "'points' is carried by an offer" in e for e in report["errors"]
        ), (
            f"points is appliable for every offer that carries it, so it must not "
            f"be reported as having an unappliable case: {report['errors']}"
        )

    def test_the_check_skips_rather_than_raises_when_it_cannot_read_the_source(
        self, tmp_path
    ):
        """A validator that cannot read itself must not take the sweep down.

        A function with no retrievable source -- a wrapper defined elsewhere, an
        extension function, a build without sources -- is unmeasurable. The choice
        is between saying nothing and raising, and saying nothing is right: a
        sweep that dies because it could not introspect one function is worse than
        one that checks less.

        Two earlier attempts at this control were wrong in the direction of looking
        fine, which is the only direction worth recording:

        * Replacing the function with a one-line stub. The stub is valid Python,
          so `getsource` returns it, the AST parses it, no claim is found and the
          mutator is reported unguarded. Correct diagnosis, wrong premise — and it
          read as a passing control for "unreadable source is skipped".
        * Deleting the module file, so `getsource` raises. But `getsource` reads
          through the file, so that hid *all four* mutators, and the control
          exercised the skip path four times and the reachability of the check
          zero times.
        """
        module = _load_reverted(
            tmp_path,
            [
                (
                    "if not await _claim_transition(\n        db,\n        row,\n        target,",
                    "if not await _reverted_noop(\n        db,\n        row,\n        target,",
                )
            ],
            only_in="record_outcome",
            hide_source_of="record_outcome",
        )
        import inspect

        with pytest.raises(OSError):
            inspect.getsource(module.record_outcome)  # the precondition
        # ... and the other three are still readable, which is what makes the
        # next test's assertion about them meaningful.
        for readable in (module.accept_offer, module.decline_offer, module.expire_offers_due):
            assert "async def" in inspect.getsource(readable)

        report = module.validate_offers()  # must not raise
        assert report["valid"] is True, (
            "an unreadable function should be skipped, not reported as unguarded "
            f"-- got {report['errors']}"
        )
        assert report["errors"] == [], report["errors"]

    def test_an_unreadable_function_still_leaves_the_others_checked(self, tmp_path):
        """Skipping one must not skip the rest.

        The failure mode of any "can't check this one" path is that it becomes
        "can't check any of this one". Here one function is unreadable and three
        are readable, one of them reverted -- and the control above proves an
        unreadable function is silently passed, so this proves the two coexist.
        """
        module = _load_reverted(
            tmp_path,
            [
                (
                    'db, row, "accepted", values={"accepted_at": moment}, '
                    "now=moment, require_unexpired=True",
                    'db, row, "accepted", values={"accepted_at": moment}, now=moment',
                )
            ],
            only_in="accept_offer",
            hide_source_of="record_outcome",
        )
        report = module.validate_offers()
        assert report["valid"] is False, "the readable revert should still be caught"
        assert [e for e in report["errors"] if "accept_offer" in e], report["errors"]
        assert not [e for e in report["errors"] if "record_outcome" in e], (
            "the unreadable function was reported despite being unmeasurable"
        )
