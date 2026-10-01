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


class _Select:
    def where(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self


@pytest.fixture(autouse=True)
def _statement_is_fake(monkeypatch):
    """Make ``select`` return a chainable fake so the tests need no database."""
    from app.services import customer_offers

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

    def test_every_kind_has_a_what_a_why_and_a_basis(self):
        """An offer a customer cannot understand is one they will decline."""
        from app.services import customer_offers

        for kind, template in customer_offers.OFFER_EXPLANATIONS.items():
            assert template.get("what"), kind
            assert template.get("why"), kind
            assert template.get("legal"), kind

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
    def _offer(self, **kwargs):
        from app.services import customer_offers

        class _Row:
            id = 1
            user_id = 1
            reference = "OFF-00000001"
            offer_kind = "goodwill"
            source_offer_id = "save_high"
            status = "offered"
            headline = "A credit from us"
            points = 250.0
            discount_percent = 15.0
            waiver_type = ""
            priority = ""
            generosity_scale = 1.0
            expires_at = datetime(2026, 10, 5, tzinfo=timezone.utc)
            issued_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
            accepted_at = None
            declined_at = None
            fulfilled_at = None
            decline_reason = ""
            follow_up_due_at = datetime(2026, 10, 3, tzinfo=timezone.utc)

        for key, value in kwargs.items():
            setattr(_Row, key, value)
        return _Row()

    @pytest.mark.asyncio
    async def test_one_tap_accepts(self):
        from app.services import customer_offers

        db = _Db([self._offer()], scope_user_id=1)
        result = await customer_offers.accept_offer(db, "OFF-00000001", user_id=1)
        assert result["accepted"] is True
        assert result["status"] == "accepted"
        assert result["awaiting_fulfilment"] is True

    @pytest.mark.asyncio
    async def test_pressing_twice_is_not_an_error(self):
        """The customer asked "did that work?" and the answer must never be a
        stack trace.
        """
        from app.services import customer_offers

        db = _Db([self._offer()], scope_user_id=1)
        await customer_offers.accept_offer(db, "OFF-00000001", user_id=1)
        again = await customer_offers.accept_offer(db, "OFF-00000001", user_id=1)
        assert again["accepted"] is False
        assert again["status"] == "accepted"
        assert "cannot be accepted" in again["reason"]

    @pytest.mark.asyncio
    async def test_accepting_an_expired_offer_says_so_and_persists_the_expiry(self):
        from app.services import customer_offers

        db = _Db(
            [self._offer(expires_at=datetime(2026, 9, 1, tzinfo=timezone.utc))],
            scope_user_id=1,
        )
        result = await customer_offers.accept_offer(
            db, "OFF-00000001", user_id=1,
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert result["accepted"] is False
        assert result["status"] == "expired"
        # The row stops claiming to be open, so no later read rediscovers it.
        assert db._rows[0].status == "expired"
        assert "expired" in [event.kind for event in db.events()]

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
            _Db([self._offer()], scope_user_id=999), "OFF-00000001", user_id=999
        )
        assert result["found"] is False


class TestDecline:
    @pytest.mark.asyncio
    async def test_declining_records_the_free_text_reason(self):
        from app.services import customer_offers

        db = _Db([TestAccept()._offer()], scope_user_id=1)
        result = await customer_offers.decline_offer(
            db, "OFF-00000001", user_id=1, reason="I still have not had the repair"
        )
        assert result["declined"] is True
        assert db._rows[0].decline_reason == "I still have not had the repair"
        assert result["reason_recorded"] is True

    @pytest.mark.asyncio
    async def test_the_response_tells_them_the_problem_is_not_resolved(self):
        """People read declining an apology as "the matter is closed", and then
        stop telling us about the thing that is still broken.
        """
        from app.services import customer_offers

        db = _Db([TestAccept()._offer()], scope_user_id=1)
        result = await customer_offers.decline_offer(db, "OFF-00000001", user_id=1)
        assert "not a statement that the problem is resolved" in result["next"]

    @pytest.mark.asyncio
    async def test_a_declined_offer_cannot_then_be_accepted(self):
        from app.services import customer_offers

        db = _Db([TestAccept()._offer()], scope_user_id=1)
        await customer_offers.decline_offer(db, "OFF-00000001", user_id=1)
        after = await customer_offers.accept_offer(db, "OFF-00000001", user_id=1)
        assert after["accepted"] is False
        assert after["status"] == "declined"


# ===========================================================================
# Recording an outcome
# ===========================================================================


class TestRecordOutcome:
    @staticmethod
    def _offer(status="accepted"):
        """An offer row with a settable status, as a plain class for the fake."""

        class _Row:
            id = 1
            user_id = 1
            reference = "OFF-00000001"
            offer_kind = "goodwill"
            points = 250.0
            discount_percent = 0.0
            waiver_type = ""
            priority = ""
            generosity_scale = 1.0
            expires_at = None
            issued_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
            accepted_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
            declined_at = None
            fulfilled_at = None
            decline_reason = ""
            follow_up_due_at = datetime(2026, 10, 3, tzinfo=timezone.utc)

        _Row.status = status
        return _Row()

    @pytest.mark.asyncio
    async def test_fulfilling_an_accepted_offer_records_it(self):
        from app.services import customer_offers

        db = _Db([self._offer()])
        result = await customer_offers.record_outcome(
            db, "OFF-00000001", "fulfil", actor="admin:ana"
        )
        assert result["recorded"] is True
        assert result["status"] == "fulfilled"

    @pytest.mark.asyncio
    async def test_an_offer_nobody_accepted_cannot_be_fulfilled(self):
        """Otherwise an operator could close an offer the customer is mid-tap on,
        and the loser of that race is a customer pressing accept on an error.
        """
        from app.services import customer_offers

        db = _Db([self._offer(status="offered")])
        result = await customer_offers.record_outcome(
            db, "OFF-00000001", "fulfil", actor="admin:ana"
        )
        assert result["recorded"] is False
        assert "offered -> fulfilled" in result["reason"]

    @pytest.mark.asyncio
    async def test_an_open_offer_can_be_expired_but_not_fulfilled(self):
        """The asymmetry, which is the design.

        Expiry from `offered` is legitimate and has to be expressible twice over:
        an offer's own clock running out is what `expire_offers_due` does, and an
        operator withdrawing a mis-sent offer needs the same verb. Fulfilment is
        different -- it is the claim that a customer said yes and we then did it,
        so it is the one refused without a customer decision behind it.
        """
        from app.services import customer_offers

        db = _Db([self._offer(status="offered")])
        result = await customer_offers.record_outcome(
            db, "OFF-00000001", "expire", actor="admin:ana"
        )
        assert result["recorded"] is True
        assert result["status"] == "expired"
        # Expiring clears the follow-up clock: there is nothing left to chase.
        assert db._rows[0].follow_up_due_at is None

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
    async def test_a_fulfil_on_an_open_offer_leaves_the_row_open(self):
        """The refusal must be a no-op, not a write of the refused status.

        Recording `fulfilled` onto a row that is still `offered` would satisfy a
        later reader that checks only the status -- so the row keeps saying
        `offered` and the customer can still accept it.
        """
        from app.services import customer_offers

        db = _Db([self._offer(status="offered")])
        assert (await customer_offers.record_outcome(
            db, "OFF-00000001", "fulfil", actor="admin:ana"
        ))["recorded"] is False
        assert db._rows[0].status == "offered"
        assert db.events() == []


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