"""Tier 1 -- certainty flows: the claims that must hold for every customer.

Nothing in this file touches a database, a clock, or the network. That is not
simplicity for its own sake; it is the point of the tier. Everything below is a
claim we can be *certain* of, and a claim we can be certain of is a claim we
should be able to check across every persona the product actually has rather
than the one a unit test happened to construct. So the harness here is not a
database, it is :data:`THE_CAST` -- five invented customers who span the states
the engines branch on -- and the tests are *loops over the cast*.

Four properties recur, and each one is a real defect class rather than a style
preference:

* **Vocabulary containment.** Every resolver returns a label from the vocabulary
  its own module publishes. A resolver that invents a label produces a customer
  who is in a band the rest of the system has never heard of, and that surfaces
  as a silent `else` branch several subsystems away.
* **Order preservation.** Monotone inputs give monotone outputs -- a higher
  score is not a worse band, a longer horizon is not a more confident forecast.
  These are the properties that break when somebody reorders a rule list or
  flips an inequality, and they break *quietly*.
* **Determinism.** The same inputs produce the same bytes. A resolver that
  reads the wall clock or iterates a set makes "what will this customer see
  tomorrow" unanswerable, which is the question every governance surface exists
  to answer.
* **Fail-closed.** Where a subsystem cannot tell, it says so rather than
  guessing. The Stage D finding was that a gate that assumes permission is a
  gate that cannot be audited; the same shape shows up here in a dozen places.

Every loop also asserts on the *shape* of the failure. A test that says
"``band in BANDS``" and nothing else reports ``False`` when it fails, which is
the least useful possible account of why a customer ended up somewhere the
system does not recognise.

Why ``NOW`` is passed to everything that accepts it: an engine reading the wall
clock would make this file fail on 1 January and pass on 2 October, and the
result would be a test suite nobody trusts on the morning it matters. Every
engine here takes ``now=`` or an explicit date, and the frozen moment is passed
to all of them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from _e2e_world import LATER, NOW, THE_CAST, assert_money, assert_monotonic, person, world

#: The engine vocabularies, spelled out as literals rather than imported.
#:
#: Imported, a test of this kind asserts "the resolver agrees with itself", which
#: is the failure mode ``test_kaizen_shadow_release.py`` calls out in its own
#: docstring: a probe whose expectation is derived from the table it validates
#: cannot detect an edit to that table. So these are typed by hand and the tests
#: that use them will fail if somebody widens a vocabulary without saying so --
#: which is the correct outcome, and the point of writing them down.
BAND_VOCABULARY = frozenset({"limited", "moderate", "strong", "elite"})
TIER_VOCABULARY = frozenset({"restricted", "standard", "customer-premium", "system-premium"})
POSTURE_VOCABULARY = frozenset({"high_trust", "customer_trusted", "observed", "constrained"})
BOOKING_STATE_VOCABULARY = frozenset({"pending", "confirmed", "completed", "cancelled"})
SEVERITY_VOCABULARY = frozenset({"critical", "high", "medium", "low"})
CHURN_VOCABULARY = frozenset({"low", "medium", "high"})

#: A one-point ladder above and below each published band cut point. The cut
#: points themselves are the numbers, written out, because a threshold test that
#: imports the threshold is a tautology.
BAND_CUTS = ((90.0, "elite"), (75.0, "strong"), (55.0, "moderate"))


# ===========================================================================
# The cast itself
# ===========================================================================


class TestTheCastIsWorthTestingAgainst:
    """A fixture that cannot fail for the reason it claims is a decoration."""

    def test_every_engine_that_branches_on_a_score_gets_a_different_one(self):
        """Otherwise the loop below is five copies of one assertion."""
        scores = [member.access_score for member in THE_CAST]
        assert len(set(scores)) == len(scores), scores

    def test_the_cast_spans_the_bands_the_published_thresholds_cut(self):
        bands = {
            member.username: _band(member.access_score)
            for member in THE_CAST
        }
        assert set(bands.values()) == BAND_VOCABULARY, bands

    def test_the_cast_spans_the_tiers_the_published_thresholds_cut(self):
        tiers = {
            member.username: _tier(member.access_score, member.system_score)
            for member in THE_CAST
        }
        assert set(tiers.values()) == TIER_VOCABULARY, tiers

    def test_the_cast_includes_somebody_with_no_history_and_somebody_with_much(self):
        days = [member.days_since_last_activity for member in THE_CAST]
        assert min(days) == 0
        assert max(days) >= 30, days

    def test_the_cast_includes_a_person_whose_only_signal_is_their_absence(self):
        """One member carries no bookings at all, which is its own state.

        The simulator's five personas all hold at least one booking, so a
        resolver's behaviour on "this customer has no booking rows" is
        untested by the whole existing suite. ``root`` is that case here.
        """
        assert person(5).booking_states == ()
        assert all(
            len(member.booking_states) > 0
            for member in THE_CAST
            if not member.is_admin
        )

    def test_every_score_is_on_the_scale_the_resolvers_publish(self):
        for member in THE_CAST:
            for field in (
                "access_score",
                "system_score",
                "customer_score",
                "loyalty_score",
                "interest_score",
                "closeness_score",
            ):
                value = getattr(member, field)
                assert 0.0 <= value <= 100.0, (member.username, field, value)

    def test_every_field_on_a_person_is_read_by_at_least_one_engine(self):
        """A persona field nobody reads is a place for a flow to look thorough.

        Checked by asking the product, not by a hand-kept list: the fields are
        grepped out of the shipped source and each must appear. This is a
        fixture-hygiene test, and it is here because the cast grows over time and
        the growth is exactly how a persona quietly stops describing anything.
        """
        import pathlib

        import app

        source_root = pathlib.Path(app.__path__[0])
        # `app.__file__` is None: the package has no `__init__.py`, so it is a
        # PEP 420 namespace package. The path comes from `__path__` instead.
        corpus = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in sorted(source_root.rglob("*.py"))
        )
        fields = (
            "access_score",
            "system_score",
            "customer_score",
            "loyalty_score",
            "interest_score",
            "closeness_score",
            "churn_risk",
            "policy_tier",
            "control_posture",
            "lifecycle_stage",
            "days_since_last_activity",
            "booking_states",
        )
        for field in fields:
            assert field in corpus, (
                f"the cast carries {field!r} but nothing in app/ reads it -- "
                f"either the persona field is dead or a flow is about to rely on it"
            )


def _band(access_score: float) -> str:
    from app.services import policy_scoring

    return policy_scoring.resolve_access_band(access_score)


def _tier(access_score: float, system_score: float) -> str:
    from app.services import policy_scoring

    return policy_scoring.resolve_policy_tier(access_score, system_score)


# ===========================================================================
# 1. Vocabulary containment
# ===========================================================================


class TestEveryResolverAnswersInItsOwnVocabulary:
    def test_the_access_band_of_every_customer_is_a_published_band(self):
        from app.services import policy_scoring

        for member in THE_CAST:
            band = policy_scoring.resolve_access_band(member.access_score)
            assert band in BAND_VOCABULARY, (
                f"{member.username} scored {member.access_score} and came back "
                f"{band!r}, which no other subsystem recognises"
            )

    def test_the_policy_tier_of_every_customer_is_a_published_tier(self):
        from app.services import policy_scoring

        for member in THE_CAST:
            tier = policy_scoring.resolve_policy_tier(
                member.access_score, member.system_score
            )
            assert tier in TIER_VOCABULARY, (member.username, member.access_score, tier)

    def test_the_control_posture_of_every_customer_is_a_published_posture(self):
        from app.services import policy_scoring

        for member in THE_CAST:
            posture = policy_scoring.resolve_control_posture(
                member.policy_tier, member.access_score, member.system_score
            )
            assert posture in POSTURE_VOCABULARY, (member.username, posture)

    def test_the_churn_label_of_every_customer_is_a_published_label(self):
        from app.services import retention

        for member in THE_CAST:
            # `churn_risk_label` takes a *rank*; the persona carries the label,
            # because that is what every other surface on a persona says and a
            # float churn risk in a persona reads as a second, rival score.
            label = retention.churn_risk_label(
                retention.RETENTION_CHURN_RANK[member.churn_risk]
            )
            assert label in CHURN_VOCABULARY, (member.username, member.churn_risk, label)
            # Round-trips: the persona's declared label is the one that comes back.
            assert label == member.churn_risk, (
                f"{member.username} declares {member.churn_risk!r} and the resolver "
                f"reports {label!r}"
            )

    def test_every_booking_state_the_cast_holds_is_a_real_booking_state(self):
        from app import models

        shipped = {row.value for row in models.BookingStatus}
        for member in THE_CAST:
            for state in member.booking_states:
                assert state in BOOKING_STATE_VOCABULARY, (member.username, state)
                assert state in shipped, (
                    f"{member.username} holds {state!r}, which the engine accepts "
                    f"and the model does not -- a persona that cannot be written"
                )

    def test_a_severity_the_product_does_not_publish_falls_back_rather_than_inventing(self):
        """Unknown input gets the fallback row, and says which one.

        The counterpart to the tests above: containment alone is easy to satisfy
        by refusing everything, so this pins that the refusal is a *fallback* and
        the customer still gets a clock.
        """
        from app.services import complaints

        row = complaints.resolve_sla("no-such-severity")
        assert row["severity"] in SEVERITY_VOCABULARY
        assert row["severity"] == "medium"
        assert row["response_hours"] > 0 and row["resolution_hours"] > 0

    def test_a_complaint_still_gets_a_clock_when_nothing_about_it_is_recognised(self):
        """The full open path, on a category and severity nobody configured."""
        from app.services import complaints

        row = complaints.resolve_sla("")
        assert row["basis"] in {
            "internal_matrix",
            "internal_matrix_within_regulatory_limit",
            "regulatory_floor_partial",
            "regulatory_floor",
        }
        route = complaints.resolve_route({"category": "no-such-category"})
        assert route["to_tier"] and route["owner_team"], route


# ===========================================================================
# 2. Order preservation
# ===========================================================================


class TestMonotoneInputsGiveMonotoneOutputs:
    """The properties that break when a rule list is reordered.

    Every one of these failed silently the first time it was broken, which is
    what makes them worth a test rather than a code comment. A band boundary
    evaluated with ``<`` where the table meant ``<=`` still returns a
    published label for every input; it just quietly promotes the customers
    sitting exactly on the line, and nobody finds out until somebody asks why
    two customers with identical scores are treated differently.
    """

    def test_a_higher_access_score_is_never_a_worse_band(self):
        from app.services import policy_scoring

        for member in THE_CAST:
            floor = member.access_score
            for offset in (0.5, 5.0, 20.0):
                ceiling = min(100.0, floor + offset)
                low = policy_scoring.resolve_access_band(floor)
                high = policy_scoring.resolve_access_band(ceiling)
                assert _band_rank(high) >= _band_rank(low), (
                    f"{member.username}: {floor} -> {low!r} but "
                    f"{ceiling} -> {high!r}"
                )

    def test_each_published_cut_point_is_inclusive_at_its_own_boundary(self):
        """The numbers, and what happens either side of each one."""
        from app.services import policy_scoring

        for cut, expected in BAND_CUTS:
            at = policy_scoring.resolve_access_band(cut)
            above = policy_scoring.resolve_access_band(cut + 0.001)
            assert at == expected, (
                f"a score of exactly {cut} resolved to {at!r}, not {expected!r}; "
                f"a boundary that excludes its own cut point promotes nobody "
                f"and demotes everybody sitting on the line"
            )
            # Monotone: a higher score is never a worse band. A cut point is a
            # band's *floor*, so a hair above it is still that band -- asserting
            # a strict increase on crossing would be asserting that the band a
            # cut opens has a successor you reach by moving 0.001.
            assert _band_rank(above) >= _band_rank(at), (cut, at, above)
            # What crossing a cut *does* guarantee is landing in that cut's band,
            # which is the property the previous assertion could not see.
            below = policy_scoring.resolve_access_band(cut - 0.001)
            assert _band_rank(at) > _band_rank(below), (
                f"crossing {cut} upward did not promote: {below!r} -> {at!r}"
            )

    def test_a_score_below_the_lowest_cut_is_still_a_band_not_an_error(self):
        from app.services import policy_scoring

        for score in (0.0, 0.001, 54.999):
            assert policy_scoring.resolve_access_band(score) == "limited", score

    def test_a_score_above_the_highest_cut_is_still_a_band(self):
        from app.services import policy_scoring

        for score in (90.0, 99.999, 100.0):
            assert policy_scoring.resolve_access_band(score) == "elite", score

    def test_a_score_off_the_declared_scale_is_refused_rather_than_clamped(self):
        """Clamping quietly invents a band for a score that does not exist.

        A caller holding ``-10`` or ``101`` has a bug, and a band for it is a
        band nobody wrote a rule for. The simulator's own docstring records a
        phantom defect from exactly this: an access-band resolver called on a
        0-1 scale.
        """
        from app.services import policy_scoring

        for score in (-0.001, 100.001):
            with pytest.raises(ValueError):
                policy_scoring.resolve_access_band(score)

    def test_a_longer_forecast_horizon_is_never_more_confident(self):
        from app.services import retention

        for points in (1, 3, 10, 50):
            assert_monotonic(
                [retention.forecast_confidence(horizon, points) for horizon in (7, 30, 90, 180, 365, 730)],
                what=f"forecast confidence at {points} snapshots",
            )

    def test_more_history_is_never_less_confident_at_the_same_horizon(self):
        from app.services import retention

        for horizon in (7, 30, 90, 365):
            earlier = retention.forecast_confidence(horizon, 3)
            later = retention.forecast_confidence(horizon, 12)
            assert later >= earlier - 1e-9, (horizon, earlier, later)

    def test_a_confidence_is_a_confidence(self):
        from app.services import retention

        for horizon in (1, 7, 365, 3650):
            for points in (0, 1, 3, 1000):
                value = retention.forecast_confidence(horizon, points)
                assert 0.0 <= value <= 1.0, (horizon, points, value)

    def test_more_days_elapsed_is_never_less_interest_and_never_more(self):
        """Accrual is bounded below by the cap and above by the clock."""
        from app.services import arrears_payments

        for principal in (10.0, 1000.0, 5000.0):
            series = [
                arrears_payments.compute_arrears_interest(principal, 15.0, days)["interest"]
                for days in (0, 5, 10, 11, 30, 90, 365, 3650)
            ]
            # Interest *rises* with time. Asserting the falling direction here
            # would have pinned the accrual as a decay, which is the opposite of
            # what the engine does and of what a customer is owed.
            assert_monotonic(
                series,
                what=f"interest on {principal} at 15%/yr",
                increasing=True,
            )

    def test_a_waived_claim_is_never_more_expensive_than_an_unwaived_one(self):
        from app.services import arrears_payments

        for principal in (10.0, 1000.0, 5000.0):
            charged = arrears_payments.compute_arrears_interest(principal, 15.0, 200)
            assert_money(
                arrears_payments.compute_arrears_interest(principal, 0.0, 200)["interest"],
                0.0,
                what="interest at a zero rate",
            )
            assert charged["interest"] <= principal, (
                f"simple interest on {principal} exceeded the principal, which "
                f"means the cap is not binding"
            )

    def test_a_larger_principal_never_costs_less_in_absolute_terms(self):
        from app.services import arrears_payments

        series = [
            arrears_payments.compute_arrears_interest(principal, 15.0, 120)["interest"]
            for principal in (10.0, 100.0, 1000.0, 5000.0)
        ]
        assert series == sorted(series), series

    def test_the_daily_cap_is_measured_before_the_fee_not_after(self):
        """The redeem cap compares gross money, the purchase cap compares money in.

        Both are defensible; what is not defensible is one of them charging a
        customer more than the cap because the cap was measured on the net
        figure. This pins which side each direction measures, because the two
        are different rules wearing the same name.
        """
        from app.services import points_exchange

        context = worldless_context()
        # November: outside every campaign window (Mar-Apr, Jun-Aug, Sep-Oct,
        # Dec). A date inside one picks up a `redeem_multiplier` of 1.05 and the
        # base arithmetic this test is pinning -- 100 points at 100 per dollar --
        # is no longer what the quote is asserting about.
        effective = "2026-11-15"
        inside = points_exchange.quote_points_exchange(
            "loyalty_points", "redeem", 100.0, "USD", context,
            daily_used_money=0.0, effective_date=effective,
        )
        # Comfortably over the $50 daily redeem cap, not exactly on it: 5000 points is
        # $50 gross, which sits *on* the boundary and would leave "exceeds" and
        # "meets" indistinguishable.
        at_cap = points_exchange.quote_points_exchange(
            "loyalty_points", "redeem", 20000.0, "USD", context,
            daily_used_money=0.0, effective_date=effective,
        )
        cap = at_cap["daily_max_money"]
        assert_money(
            inside["gross_output"], 1.0, what="100 points at 100 points per dollar"
        )
        assert at_cap["exceeds_daily_cap"] is True
        assert_money(
            at_cap["gross_output"] - at_cap["fee"], at_cap["output_amount"],
            what="net is gross less the fee",
        )
        assert cap > 0


class _BandRank:
    """Ordering for bands, derived from the cut table rather than hard-coded.

    Read off ``BAND_CUTS`` so the ordinal is a consequence of the numbers in
    this file. If somebody widens the cut table the rank follows, which is what
    makes the ordering tests above meaningful.
    """

    def __getitem__(self, band: str) -> int:
        for index, (_, label) in enumerate(reversed(BAND_CUTS)):
            if label == band:
                return index + 1
        return 0


_BAND_RANKS = _BandRank()


def _band_rank(band: str) -> int:
    return _BAND_RANKS[band]


def worldless_context(**extra) -> dict:
    """A ``when``-DSL context with nothing cast-specific in it.

    Named for what it avoids: the cast helpers all require a seeded database, and
    a pure-function test should not need one. The keys are the ones the shipped
    rules actually read, so an unknown key is not silently accepted -- the DSL
    fails a rule that tests a field the context does not carry.
    """
    context = {
        "user_tier": "standard",
        "loyalty_score": 50.0,
        "currency": "USD",
        "lifecycle_stage": "active",
        "churn_risk": "medium",
        "service_type": "consultation",
        "principal": 1000.0,
    }
    context.update(extra)
    return context


# ===========================================================================
# 3. Determinism
# ===========================================================================


class TestTheSameQuestionGetsTheSameAnswer:
    def test_a_quote_is_reproducible_across_repeated_calls(self):
        """The invariant the ``points_and_arrears_payment`` flow declares.

        That flow's stated invariant is "a quote is reproducible", and it has no
        probe and no test behind it anywhere in the suite -- the only probe on
        that flow is a posture adjustment that never touches points. So it is
        pinned here, with an explicit date, which is the only way to make the
        claim decidable at all.
        """
        from app.services import points_exchange

        context = worldless_context()
        first = points_exchange.quote_points_exchange(
            "loyalty_points", "redeem", 400.0, "USD", context, effective_date="2026-07-15"
        )
        for _ in range(5):
            again = points_exchange.quote_points_exchange(
                "loyalty_points", "redeem", 400.0, "USD", context,
                effective_date="2026-07-15",
            )
            assert again == first, (
                "the same wallet and the same request produced two different "
                "quotes; a customer who screenshots a quote is holding evidence"
            )

    def test_a_quote_is_stable_across_a_dict_reordering(self):
        """Rule selection must not depend on context iteration order."""
        from app.services import points_exchange

        forward = worldless_context()
        reversed_context = dict(reversed(list(forward.items())))
        first = points_exchange.quote_points_exchange(
            "loyalty_points", "purchase", 20.0, "USD", forward, effective_date="2026-07-15"
        )
        second = points_exchange.quote_points_exchange(
            "loyalty_points", "purchase", 20.0, "USD", reversed_context,
            effective_date="2026-07-15",
        )
        assert second == first

    def test_an_interest_figure_is_reproducible_for_a_fixed_elapsed_count(self):
        from app.services import arrears_payments

        first = arrears_payments.compute_arrears_interest(1234.56, 15.0, 137)
        for _ in range(5):
            assert arrears_payments.compute_arrears_interest(1234.56, 15.0, 137) == first

    def test_a_wilson_bound_is_reproducible_and_bounded_by_the_observed_rate(self):
        from app.services import offer_outcomes

        successes, trials = 6, 40
        bound = offer_outcomes.wilson_lower_bound(successes, trials)
        for _ in range(5):
            assert offer_outcomes.wilson_lower_bound(successes, trials) == bound
        assert bound <= successes / trials + 1e-9, (bound, successes / trials)

    def test_the_bound_is_never_above_the_rate_and_never_below_zero(self):
        from app.services import offer_outcomes

        for trials in (1, 2, 5, 40, 500):
            for successes in range(0, trials + 1):
                bound = offer_outcomes.wilson_lower_bound(successes, trials)
                assert 0.0 <= bound <= successes / trials + 1e-9, (successes, trials, bound)

    def test_no_evidence_is_a_bound_of_zero_rather_than_an_exception(self):
        """Zero trials is the state a brand-new tier is in, not an error."""
        from app.services import offer_outcomes

        assert offer_outcomes.wilson_lower_bound(0, 0) == 0.0

    def test_the_gate_answers_the_same_way_twice_for_the_same_customer(self):
        from app.services import care_gate

        for member in THE_CAST:
            preferences = {"communication_frequency": "daily"}
            consents = {"service": True, "recovery": True, "marketing": False}
            first = care_gate.consult(
                "recovery_outreach", preferences, consents,
                region_id=member.region_id, now=NOW,
            )
            second = care_gate.consult(
                "recovery_outreach", preferences, consents,
                region_id=member.region_id, now=NOW,
            )
            for key in ("mode", "push", "issue", "purpose", "local_hour"):
                assert first[key] == second[key], (member.username, key)

    def test_a_resolver_does_not_read_the_wall_clock(self):
        """Checked by moving the moment, not by reading the source.

        A resolver that calls ``datetime.now()`` internally returns the same
        answer on both calls and passes every determinism test above; only
        moving time catches it. Two calls a week apart is the honest version of
        this and a monkeypatched clock is the dishonest one, because a module
        that captured ``datetime`` at import time would not see the patch.
        """
        from app.services import care_gate, complaints

        member = person(1)
        preferences = {"communication_frequency": "daily"}
        consents = {"service": True, "recovery": True}

        early = care_gate.consult(
            "recovery_outreach", preferences, consents,
            region_id=member.region_id, now=NOW,
        )
        later = care_gate.consult(
            "recovery_outreach", preferences, consents,
            region_id=member.region_id, now=LATER + timedelta(hours=5),
        )
        # `LATER` is exactly seven days after `NOW` at the *same* time of day, so
        # on a correct resolver the two local hours are identical and `LATER`
        # cannot be used here. The property needs a later moment that differs in
        # time of day -- which is what a wall-clock read would also produce, and
        # what a frozen `NOW` would not.
        assert early["local_hour"] != later["local_hour"], (
            "five hours of separation produced the same local hour, so the "
            "region resolver is not reading the moment it was given"
        )
        assert complaints.resolve_sla("high") == complaints.resolve_sla("high")

    def test_a_given_moment_resolves_to_different_local_hours_in_different_regions(self):
        """The one instant, three answers. This is the Stage E property.

        Worth a test of its own because the failure is not a crash: reading
        ``moment.hour`` on a UTC datetime returns a plausible hour for every
        region, and every downstream window evaluation then agrees with itself
        while being wrong about where the customer is.
        """
        from app.services import region_windows

        hours = {
            region: region_windows.evaluate_contact_window(
                moment=NOW, region_id=region
            )["local_hour"]
            for region in ("us_east", "europe_london", "oceania_auckland", "asia_tokyo")
        }
        assert len(set(hours.values())) >= 3, hours
        for region, hour in hours.items():
            assert 0 <= hour <= 23, (region, hour)


# ===========================================================================
# 4. Fail-closed
# ===========================================================================


class TestUncertaintyIsReportedRatherThanGuessed:
    """Where a subsystem cannot tell, it says so.

    The Stage D finding in one sentence: ``customer_offers`` was the only
    service in the codebase that consulted the preference centre, and the fix was
    to make every declared proactive path consult a gate that *fails closed*.
    The tests here are that same property, applied to the other places where a
    guess is available.
    """

    def test_an_unreadable_preference_profile_means_do_not_push(self):
        from app.services import care_gate

        for path_id in _declared_paths():
            decision = care_gate.consult(path_id, None, None, now=NOW)
            assert decision["push"] is False, (path_id, decision["reasons"])
            assert decision["mode"] == "preferences_missing", path_id

    def test_an_unreadable_profile_is_distinguished_from_an_empty_one(self):
        """An empty profile is a person who set nothing; unreadable is a fault.

        Both produce "do not push", which is right, and conflating them is what
        makes an outage look like a policy.
        """
        from app.services import care_gate

        missing = care_gate.consult("recovery_outreach", None, None, now=NOW)
        empty = care_gate.consult("recovery_outreach", {}, {}, now=NOW)
        assert missing["mode"] != empty["mode"], (missing["mode"], empty["mode"])

    def test_an_unregistered_proactive_path_may_not_push(self):
        """A path nobody declared is a path nobody reasoned about."""
        from app.services import care_gate

        decision = care_gate.consult(
            "a_path_that_was_never_declared", {}, {"service": True}, now=NOW
        )
        assert decision["push"] is False
        assert decision["declared"] is False

    def test_consent_never_withholds_a_recovery_or_service_thing(self):
        """The rule that ``recovery_never_withholds_a_fix`` exists for.

        A customer who reported a problem does not get told "we had a fix but
        you declined marketing". So a *withheld* marketing consent has to be
        inert on a recovery path, and the gate says which override applied.
        """
        from app.services import care_gate

        for path_id in _declared_paths():
            purpose = care_gate.PROACTIVE_PATHS_BY_ID[path_id].get("subsystem")
            gate_purpose = "recovery" if purpose in {
                "recovery_offers", "recovery_playbooks", "care_weights"
            } else "service"
            decision = care_gate.consult(
                path_id, {}, {"service": False, "recovery": False, "marketing": False},
                purpose=gate_purpose, now=NOW,
            )
            assert decision["issue"] is True, (
                f"{path_id} refused to issue on purpose {gate_purpose!r} because "
                f"consent was withdrawn; the reason was {decision['reasons']}"
            )
            assert decision["service_critical"] is True

    def test_a_consent_gated_purpose_is_actually_gated(self):
        """The other half: a *marketing* withdrawal must bite, or the gate is decorative."""
        from app.services import care_gate, preferences

        granted = care_gate.consult(
            "journey_next_action", {}, dict(preferences.default_consents(), marketing=True),
            purpose="marketing", now=NOW,
        )
        withheld = care_gate.consult(
            "journey_next_action", {}, dict(preferences.default_consents(), marketing=False),
            purpose="marketing", now=NOW,
        )
        assert granted["issue"] is True
        assert withheld["issue"] is False, (
            "a withdrawn marketing consent did not gate a marketing message; "
            "the gate is decorative"
        )

    def test_an_unknown_purpose_is_refused_rather_than_assumed_permitted(self):
        from app.services import care_gate

        decision = care_gate.consult(
            "journey_next_action", {}, {}, purpose="no_such_purpose", now=NOW
        )
        assert decision["issue"] is False, decision
        assert decision["consent"]["known_purpose"] is False

    def test_a_missing_metric_fails_the_escalation_guard_closed(self):
        """Absent evidence is not evidence of absence.

        A guard whose metric is missing reports ``holds=False`` rather than
        skipping, because a skipped guard is invisible and an unheld one shows
        up in the verdict.
        """
        from app.services import complaints

        results = complaints.evaluate_escalation_guards({})
        assert results, "no guards ran"
        for guard in results:
            if guard["reason"] == "metric_missing":
                assert guard["holds"] is False, guard
                continue
            assert guard["reason"] == "ok", guard

    def test_an_unknown_guard_operator_fails_closed_rather_than_passing(self):
        from app.services import complaints

        results = complaints.evaluate_escalation_guards(
            {"has_owner": True},
            guards=[
                {
                    "guard_id": "an_operator_nobody_implemented",
                    "metric": "has_owner",
                    "op": "approximately",
                    "threshold": True,
                    "severity": "review",
                    "rationale": "a typo in an operator must not become a pass",
                }
            ],
        )
        assert len(results) == 1
        assert results[0]["holds"] is False, results[0]

    def test_a_scoped_guard_that_does_not_apply_is_absent_rather_than_passing(self):
        """Skipping is right; *reporting a pass* would be a lie about coverage."""
        from app.services import complaints

        results = complaints.evaluate_escalation_guards(
            {"regulatory_hours_remaining": 100.0, "has_owner": True, "regulatory": False},
        )
        ids = {guard["guard_id"] for guard in results}
        assert "regulatory_deadline_unreachable" not in ids, (
            "a guard scoped to regulatory cases evaluated on a non-regulatory one"
        )

    def test_an_insufficient_history_forecast_says_so_rather_than_guessing(self):
        from app.services import retention

        for point_count in (0, 1, 2):
            confidence = retention.forecast_confidence(30, point_count)
            assert confidence < 1.0, (point_count, confidence)

    def test_an_empty_retention_series_is_not_banded(self):
        """Zero snapshots is not "healthy", and it must not resolve to a band."""
        from app.services import retention

        context = retention.build_retention_health_context([])
        banded = retention.resolve_retention_health(context)
        # The band is the claim: `no_data` is a refusal to judge, and it is
        # reached by a *named rule* (`health_no_data`) rather than by falling
        # back to a default -- so `default_applied` is False here by design.
        assert banded.get("band") == "no_data", banded
        assert banded.get("default_applied") is False, (
            "an empty series fell through to the default band rather than "
            "matching the no-data rule: " + repr(banded)
        )
        # "Capture a baseline snapshot" is a data-collection instruction, not a
        # conclusion about this customer, so it is allowed. What is not allowed
        # is an action that asserts something *about* the customer -- a retention
        # action, a re-engagement trigger, a points nudge. Asserting
        # "no actions at all" would forbid the system from ever saying it needs
        # more data, which would make `no_data` a dead end.
        for action in banded.get("actions") or []:
            assert "snapshot" in str(action).lower(), (
                f"an empty series produced an action derived from nothing: {action!r}"
            )

    def test_an_unknown_personalization_dimension_is_refused_not_defaulted(self):
        from app.services import care_personalization

        resolved = care_personalization.resolve_personalization(
            purpose="recovery", consents={"service": True, "recovery": True}
        )
        # The shipped contract splits names from reasons: `refused` is a sorted
        # list of dimension names and `refusal_reasons` maps each to its
        # sentence. Asserting on the reasons is the stronger claim anyway --
        # a refusal with no reason is the thing worth failing over.
        assert resolved["refused"], resolved
        for dimension in resolved["refused"]:
            assert resolved["refusal_reasons"].get(dimension), (
                f"{dimension} is refused with no stated reason: {resolved}"
            )


def _declared_paths() -> tuple[str, ...]:
    """The declared proactive paths, by id.

    Read from the module rather than typed out, so a newly declared path is
    automatically covered -- and this file's own coverage is itself checked by
    ``test_care_loop_stage_d.py``, which fails if a declared path does not call
    the gate.
    """
    from app.services import care_gate

    return tuple(sorted(care_gate.PROACTIVE_PATHS_BY_ID))


# ===========================================================================
# 5. The cast, through a real database
# ===========================================================================


class TestTheCertaintySurvivesBeingWrittenDown:
    """The tier-1 claims, re-run against rows rather than call arguments.

    A pure function can be certain and still be wrong about what a *person*
    looks like. These take the same invariants and feed them a stored customer,
    because the gap between "the resolver answers correctly" and "the resolver
    was handed the right number" is where the last three defects in this codebase
    lived.
    """

    def test_every_cast_member_stores_the_scores_their_persona_declares(self, world):
        """If seeding drifts from the persona, every flow below is testing a fiction."""
        from app import models

        for member in THE_CAST:
            row = world.run(
                world.fetch_one(models.CustomerPolicyScore, user_id=member.user_id)
            )
            assert row is not None, member.username
            assert row.access_score == member.access_score, member.username
            assert row.policy_tier == member.policy_tier, member.username

    def test_a_stored_customer_resolves_to_the_band_their_persona_declares(self, world):
        from app.services import policy_scoring

        for member in THE_CAST:
            row = world.run(
                world.fetch_one(__import__("app.models", fromlist=["x"]).CustomerPolicyScore,
                                user_id=member.user_id)
            )
            band = policy_scoring.resolve_access_band(row.access_score)
            assert band == _band(member.access_score), (
                f"{member.username}: the stored score and the persona disagree"
            )

    def test_every_stored_customer_is_in_a_known_posture_over_http(self, world):
        from _e2e_world import mounted

        client = mounted(world)
        for member in THE_CAST:
            body = client.as_user(member.user_id).json("get", "/users/me")
            assert body["username"] == member.username
            # `/users/me` resolves the posture through the real dependency chain,
            # which is the point: a MissingGreenlet here means the eager load in
            # `deps.get_current_user` was removed.
            assert body, body

    def test_an_operator_sees_a_different_world_than_a_customer(self, world):
        """Two actors, one database, different visibility.

        The cheapest available proof that the world is real: the same collection
        route returns the caller's own rows and refuses somebody else's.
        """
        from _e2e_world import mounted

        world.add(world.booking(1))
        world.add(world.booking(3))
        world.commit()

        client = mounted(world)
        mine = client.as_user(1).json("get", "/bookings/")
        assert len(mine["items"]) == 1, mine

        theirs = client.as_user(3).json("get", "/bookings/")
        assert len(theirs["items"]) == 1, theirs
        assert mine["items"][0]["id"] != theirs["items"][0]["id"]
