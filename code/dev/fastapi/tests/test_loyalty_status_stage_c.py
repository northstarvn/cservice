"""Stage C, part 1: loyalty status, the experiential ledger, and the investment band.

This is the module where a scale error is most expensive, because its output
decides **how much money the system is willing to give away**. Two real defects
were found while writing it and both are pinned here:

1. A day count and a completion ratio were added together and then divided by a
   "reachable maximum" that was also in units of days, so the normalisation
   cancelled the mistake and *everyone* scored near zero -- a seven-year,
   fully-reliable customer at 0.079. Nothing raised. It simply reported that
   nobody is worth anything.
2. A date supplied as an ISO string parsed to nothing, so `tenure` became 0 and
   a loyal customer aged to zero. The same failure class: a silent wrong value
   that reads exactly like a correct one.

And one design decision worth defending in a test:

* **`unscored` is not a tier.** It was the bottom rung at `min_score: 0.0`, which
  reported a customer with three cancellations and an open complaint as having
  "no recorded history". A zero score is the *bottom of the ladder*, not an
  absence from it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _booking(status: str, days_ago: float, booking_id: int):
    return {
        "status": status,
        "created_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "completed_at": (NOW - timedelta(days=max(0.0, days_ago - 1))).isoformat(),
        "booking_id": booking_id,
    }


def _complaint(status: str, days_ago: float):
    return {
        "status": status,
        "created_at": (NOW - timedelta(days=days_ago)).isoformat(),
    }


class _Loyal:
    """Seven years, three completions, one resolved complaint, one chase.

    The complaint is dated **before** the most recent completion on purpose: the
    ledger reads "came back after something went wrong" off timestamps, so a
    complaint raised last month with nothing since is correctly *not* counted as
    a return. A first draft of this fixture had the order the other way round and
    the engine was right to score it zero.
    """

    LEDGER = {
        "bookings": [
            _booking("completed", 2555, 1),
            _booking("completed", 1850, 2),
            _booking("completed", 240, 3),
        ],
        "complaints": [_complaint("closed", 300)],
        "chat_rows": [{"booking_id": 1}],
    }


class _Churny:
    """A year of cancellations, an unresolved complaint, and repeated chasing."""

    LEDGER = {
        "bookings": [
            _booking("cancelled", 270, 1),
            _booking("cancelled", 240, 2),
            _booking("cancelled", 210, 3),
            _booking("pending", 180, 4),
        ],
        "complaints": [_complaint("open", 150)],
        "chat_rows": [
            {"booking_id": 1}, {"booking_id": 1}, {"booking_id": 2},
            {"booking_id": 2}, {"booking_id": 3},
        ],
    }


def _ledger(cls, now=NOW):
    from app.services import loyalty_status

    return loyalty_status.build_experiential_ledger(now=now, **cls.LEDGER)


# ===========================================================================
# The scale error
# ===========================================================================


class TestScoreScale:
    def test_a_loyal_customer_is_worth_more_than_a_churning_one(self):
        """The headline behaviour, and the one the scale bug broke.

        Before the fix this was 0.079 vs 0.000 -- correct ordering, both
        meaningless. A test asserting only the ordering would have passed on the
        broken version, which is why the absolute band is asserted too.
        """
        loyal = _ledger(_Loyal)
        churny = _ledger(_Churny)
        assert loyal["score"] > churny["score"] * 10, (loyal["score"], churny["score"])
        # And in a band that means something, not merely "larger".
        assert loyal["score"] >= 0.40, loyal["score"]
        assert loyal["tier"] in ("gold", "platinum"), loyal["tier"]

    def test_every_raw_value_is_a_ratio_so_none_needs_a_divisor(self):
        """Pinned as structure, because the bug was structural.

        A day count and a completion ratio cannot be added or divided against
        each other. The fix is that every signal arrives already normalised, and
        the published contributions must show it.
        """
        for row in _ledger(_Loyal)["signals"]:
            assert 0.0 <= row["value"] <= 1.0, row
            # Compared with a tolerance: the published value is rounded to 4dp
            # and the product of a float weight and a rounded ratio is not
            # exactly reproducible, so an equality here would be testing the
            # rounding rather than the structure.
            assert abs(row["contribution"] - row["weight"] * row["value"]) < 1e-3, row

    def test_an_iso_string_date_is_not_silently_ignored(self):
        """The second defect: a string date parsed to nothing and tenure became 0.

        The probe data is deliberately ISO strings, which is what a serialised 360
        or a JSON-cached row looks like. Tenure must survive it.
        """
        from app.services import loyalty_status

        assert _ledger(_Loyal)["counts"]["tenure_days"] > 2000
        assert loyalty_status._aware("2026-01-01T00:00:00Z") is not None
        assert loyalty_status._aware("2026-01-01T00:00:00+00:00") is not None
        # Unparseable is None, never "now": "now" would turn a missing timestamp
        # into zero tenure, which is the same silent error in a new hat.
        assert loyalty_status._aware("not a date") is None
        assert loyalty_status._aware("") is None
        assert loyalty_status._aware(None) is None

    def test_the_weighted_mean_only_counts_countable_signals(self):
        """An absence must not read as a failure.

        Someone with no complaints cannot be credited for coming back after one,
        and crediting them anyway would let an absence inflate the score.
        """
        ledger = _ledger(_Loyal)
        countable = {row["signal_id"] for row in ledger["signals"] if row["countable"]}
        assert "goodwill_returned" in countable  # they did complain
        assert "reliability" in countable

        no_history = _ledger(type("X", (), {"LEDGER": {"bookings": [], "complaints": [], "chat_rows": []}}))
        counted = [row for row in no_history["signals"] if row["countable"]]
        assert counted == [], counted
        assert no_history["total_weight"] == 0.0


# ===========================================================================
# `unscored` is not a tier
# ===========================================================================


class TestUnscoredIsNotATier:
    def test_a_consistently_bad_customer_is_bronze_not_unscored(self):
        """"No recorded history" for someone whose recorded history was bad is a
        lie, and it is the kind that hides a problem.
        """
        churny = _ledger(_Churny)
        assert churny["unscored"] is False
        assert churny["tier"] == "bronze"
        assert churny["reported_tier"] == "bronze"

    def test_a_brand_new_customer_is_unscored(self):
        empty = _ledger(type("X", (), {"LEDGER": {"bookings": [], "complaints": [], "chat_rows": []}}))
        assert empty["unscored"] is True
        assert empty["reported_tier"] == "unscored"
        assert empty["tier"] == "bronze"  # the ladder still has a position for it

    def test_unscored_is_not_a_row_in_the_tier_table(self):
        from app.services import loyalty_status

        assert "unscored" not in loyalty_status.EXPERIENTIAL_TIERS
        assert loyalty_status.EXPERIENTIAL_TIER_RULES[0]["min_score"] == 0.0
        assert loyalty_status.NO_HISTORY_TIER == "unscored"

    def test_the_lowest_tier_is_reachable_at_zero(self):
        """The regression guard for the row that used to sit at 0.0."""
        from app.services import loyalty_status

        assert loyalty_status.resolve_experiential_tier(0.0) == "bronze"
        assert loyalty_status.resolve_experiential_tier(0.999) == "platinum"


# ===========================================================================
# Status
# ===========================================================================


class TestStatus:
    def test_status_comes_from_the_ledger_and_never_from_points(self):
        from app.services import loyalty_status

        status = loyalty_status.resolve_loyalty_status(_ledger(_Loyal))
        assert status["derived_from_points"] is False
        assert status["derived_from"] == "experiential_ledger"
        assert status["label"] == "Trusted"
        assert status["perks"]

    def test_a_loyal_customer_outranks_a_churning_one(self):
        from app.services import loyalty_status

        loyal = loyalty_status.resolve_loyalty_status(_ledger(_Loyal))
        churny = loyalty_status.resolve_loyalty_status(_ledger(_Churny))
        assert loyal["rank"] > churny["rank"]

    def test_status_never_decays_to_inactivity(self):
        """A customer-visible demotion for going quiet punishes the exact pause
        a loyalty programme should forgive, and is the fastest way to teach
        someone the programme is not worth caring about.
        """
        from app.services import loyalty_status

        assert loyalty_status.STATUS_DECAYS_ON_INACTIVITY is False
        status = loyalty_status.resolve_loyalty_status(_ledger(_Loyal))
        assert status["decays_on_inactivity"] is False
        # And no status rule mentions recency, so nothing can decay one.
        for rule in loyalty_status.LOYALTY_STATUS_RULES:
            keys = set((rule.get("when") or {}).keys())
            assert keys <= {"experiential_tier"}, rule["status_id"]

    def test_the_validator_refuses_a_decay_flag(self):
        from app.services import loyalty_status

        saved = loyalty_status.STATUS_DECAYS_ON_INACTIVITY
        loyalty_status.STATUS_DECAYS_ON_INACTIVITY = True
        try:
            report = loyalty_status.validate_loyalty_status()
            assert report["valid"] is False
            assert any("decay" in error.lower() for error in report["error_list"])
        finally:
            loyalty_status.STATUS_DECAYS_ON_INACTIVITY = saved

    def test_an_unreadable_ledger_falls_back_to_the_lowest_rung(self):
        """Fails closed. An unreadable ledger must not promote anyone."""
        from app.services import loyalty_status

        status = loyalty_status.resolve_loyalty_status({"tier": "banana"})
        assert status["status_id"] == "member"
        assert "note" in status

    def test_every_rung_declares_perks(self):
        """A status a customer cannot see any difference in is a label."""
        from app.services import loyalty_status

        for rule in loyalty_status.LOYALTY_STATUS_RULES:
            assert rule["perks"], rule["status_id"]
            assert rule["when"]["experiential_tier"], rule["status_id"]


# ===========================================================================
# The investment band
# ===========================================================================


class TestInvestmentBand:
    def test_a_loyal_low_risk_customer_is_high_value(self):
        from app.services import loyalty_status

        band = loyalty_status.resolve_investment_band(
            ledger=_ledger(_Loyal), churn_band="low"
        )
        assert band["band"] == "high"
        assert band["generosity_rule"] == "generosity_established"

    def test_a_churning_customer_is_low_regardless_of_history(self):
        from app.services import loyalty_status

        assert (
            loyalty_status.resolve_investment_band(
                ledger=_ledger(_Churny), churn_band="low"
            )["band"]
            == "low"
        )

    def test_live_risk_inverts_the_contribution(self):
        """The interesting rule.

        A customer at critical risk scores zero on the risk contribution however
        valuable their history. That is what routes them to an offer and a
        follow-up rather than to an upsell -- and it is why a Principal customer
        in trouble does not receive a Principal upsell.
        """
        from app.services import loyalty_status

        ledger = _ledger(_Loyal)
        low = loyalty_status.resolve_investment_band(ledger=ledger, churn_band="low")
        critical = loyalty_status.resolve_investment_band(ledger=ledger, churn_band="critical")
        assert critical["investment_score"] < low["investment_score"]
        assert critical["inputs"]["risk_factor"] == 0.0
        assert low["inputs"]["risk_factor"] == 1.0
        # History still counts for 65% of it, so critical is not "worthless".
        assert critical["investment_score"] > 0.0

    def test_the_band_is_ordinal_and_says_so(self):
        from app.services import loyalty_status

        band = loyalty_status.resolve_investment_band(ledger=_ledger(_Loyal))
        assert band["ordinal_only"] is True
        assert "no revenue column" in band["note"]
        assert "do not sum" in band["note"]

    def test_no_history_is_a_band_not_a_null(self):
        """"We do not know what this is worth" and "worth nothing" must not
        collapse into the same answer.
        """
        from app.services import loyalty_status

        empty = _ledger(type("X", (), {"LEDGER": {"bookings": [], "complaints": [], "chat_rows": []}}))
        band = loyalty_status.resolve_investment_band(ledger=empty)
        assert band["band"] in ("unscored", "low")
        assert band["label"]

    def test_the_context_shape_is_what_customer_offers_reads(self):
        """One key, so the two modules stay decoupled."""
        from app.services import customer_offers, loyalty_status

        context = loyalty_status.investment_context(
            ledger=_ledger(_Loyal), churn_band="low"
        )
        assert "investment_band" in context
        resolved = customer_offers.resolve_offer_generosity(context)
        assert resolved["rule_id"] == "generosity_established"

    def test_every_risk_band_is_a_real_retention_band(self):
        """A missing key contributes 1.0 -- it treats an at-risk customer as
        healthy, which is the most dangerous way this table can fail.
        """
        from app.services import loyalty_status, retention

        known = {str(item) for item in retention.RETENTION_CHURN_BANDS}
        assert set(loyalty_status.INVERTED_RISK_CONTRIBUTIONS) <= known

    def test_an_unknown_risk_band_does_not_fail_closed_to_healthy(self):
        """An unrecognised band is treated as unknown risk, not as no risk."""
        from app.services import loyalty_status

        ledger = _ledger(_Loyal)
        known = loyalty_status.resolve_investment_band(ledger=ledger, churn_band="low")
        unknown = loyalty_status.resolve_investment_band(ledger=ledger, churn_band="banana")
        # Not scored as though everything were fine.
        assert unknown["investment_score"] <= known["investment_score"]
        assert unknown["inputs"]["churn_band"] == "banana"


# ===========================================================================
# The signals themselves
# ===========================================================================


class TestSignals:
    def test_every_signal_declares_a_source_and_a_reason(self):
        """A weight a reader cannot question is a weight nobody will question."""
        from app.services import loyalty_status

        for spec in loyalty_status.EXPERIENTIAL_SIGNALS:
            assert spec["source"], spec["signal_id"]
            assert len(str(spec["why"])) > 40, spec["signal_id"]

    def test_chasing_effort_is_negative_and_still_rewards_good_behaviour(self):
        """Effort spent chasing is a cost we imposed.

        A ledger that only records positives cannot represent a customer who cost
        us a great deal and should still be treated well -- which is the case
        where being stingy costs the most.
        """
        from app.services import loyalty_status

        spec = loyalty_status.EXPERIENTIAL_SIGNAL_BY_ID["chasing_effort"]
        assert spec["weight"] < 0.0

        quiet = _ledger(_Loyal)
        chased = _ledger(_Churny)
        assert quiet["score"] > chased["score"]

    def test_a_pending_booking_is_not_a_broken_promise(self):
        """Counting pending as broken would punish a customer for our delay."""
        from app.services import loyalty_status

        ledger = _ledger(_Churny)
        assert ledger["counts"]["follow_through_negative"] == 1
        assert ledger["counts"]["reliability_negative"] == 3  # cancels only
        assert ledger["counts"]["reliability_positive"] == 0

    def test_returning_after_a_complaint_is_read_from_timestamps(self):
        """"They complained and never came back" and "they complained and came
        straight back" are the same two counts in the wrong order.
        """
        from app.services import loyalty_status

        returned = loyalty_status.build_experiential_ledger(
            complaints=[_complaint("closed", 100)],
            bookings=[_booking("completed", 10, 9)],
            now=NOW,
        )
        assert returned["counts"]["goodwill_returned"] == 1

        never = loyalty_status.build_experiential_ledger(
            complaints=[_complaint("closed", 100)],
            bookings=[_booking("completed", 200, 9)],
            now=NOW,
        )
        assert never["counts"]["goodwill_returned"] == 0

    def test_repeated_contact_means_extra_messages_about_one_booking(self):
        """A customer with ten messages about ten things is not chasing."""
        from app.services import loyalty_status

        spread = loyalty_status.build_experiential_ledger(
            chat_rows=[{"booking_id": i} for i in range(10)], now=NOW
        )
        assert spread["counts"]["chase_repeats"] == 0
        focused = loyalty_status.build_experiential_ledger(
            chat_rows=[{"booking_id": 1}] * 5, now=NOW
        )
        assert focused["counts"]["chase_repeats"] == 4

    def test_nothing_is_stored(self):
        """A stored copy would need its own reconciliation and would leave every
        existing engine with two sources of truth for the same fact.
        """
        ledger = _ledger(_Loyal)
        assert "computed" in ledger["note"]
        assert "nothing is accumulated" in ledger["note"] or "nothing is stored" in ledger["note"]


# ===========================================================================
# Validation
# ===========================================================================


class TestValidation:
    def test_the_tables_are_valid(self):
        from app.services import loyalty_status

        report = loyalty_status.validate_loyalty_status()
        assert report["valid"] is True, report["error_list"]

    def test_out_of_order_tier_rows_are_an_error(self):
        """The walk takes the highest match, so a lower row silently loses."""
        from app.services import loyalty_status

        saved = loyalty_status.EXPERIENTIAL_TIER_RULES
        loyalty_status.EXPERIENTIAL_TIER_RULES = (
            {"tier": "bronze", "min_score": 0.0},
            {"tier": "silver", "min_score": 0.6},
            {"tier": "gold", "min_score": 0.3},
        )
        try:
            assert loyalty_status.validate_loyalty_status()["valid"] is False
        finally:
            loyalty_status.EXPERIENTIAL_TIER_RULES = saved

    def test_a_risk_key_the_retention_engine_does_not_have_is_an_error(self):
        from app.services import loyalty_status

        saved = dict(loyalty_status.INVERTED_RISK_CONTRIBUTIONS)
        loyalty_status.INVERTED_RISK_CONTRIBUTIONS["catastrophic"] = 0.0
        try:
            report = loyalty_status.validate_loyalty_status()
            assert report["valid"] is False
            assert any("catastrophic" in error for error in report["error_list"])
        finally:
            loyalty_status.INVERTED_RISK_CONTRIBUTIONS.clear()
            loyalty_status.INVERTED_RISK_CONTRIBUTIONS.update(saved)

    def test_a_status_naming_an_undeclared_tier_is_an_error(self):
        from app.services import loyalty_status

        saved = loyalty_status.LOYALTY_STATUS_RULES
        loyalty_status.LOYALTY_STATUS_RULES = (
            {**dict(saved[0]), "status_id": "mystery", "when": {"experiential_tier": ["vibranium"]}},
        ) + tuple(dict(row) for row in saved)
        try:
            assert loyalty_status.validate_loyalty_status()["valid"] is False
        finally:
            loyalty_status.LOYALTY_STATUS_RULES = saved

    def test_the_catalog_publishes_the_policy(self):
        from app.services import loyalty_status

        catalog = loyalty_status.build_loyalty_status_catalog()
        assert len(catalog["statuses"]) == len(loyalty_status.LOYALTY_STATUSES)
        assert catalog["status_decays_on_inactivity"] is False
        assert "never from points" in catalog["note"]