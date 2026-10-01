"""Stage E tests: local time, personalization limits, device care, unified view.

The recurring theme of these tests is that **the same configuration must produce
different answers for different customers**. The Stage D gate bug survived a
complete test suite precisely because every test used a customer in the
server's timezone, so the assertions below deliberately hold the preference fixed
and vary only the region -- which is the thing a test that constructs one customer
never varies.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services import care_gate, care_personalization, region_windows, relationship_view

# 2026-10-01T23:00Z. Chosen because local hours differ sharply across the table:
# Auckland 12:00, US/East 19:00, Tokyo 08:00, London 00:00. Any test that used a
# single customer at a quiet UTC hour would have passed the old gate.
PROBE = datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc)

#: The hours in which we may contact this person. Read the sign carefully:
#: `contact_window_start_hour`/`end_hour` declares when contact is *permitted*,
#: so this is a 09:00-17:00 window and everything outside it is quiet hours.
#: An earlier draft of these tests set it to 22-08 believing it to be a quiet-hours
#: range, which asserted that 23:00 must be held -- and it passed, because 23:00
#: *is* inside a 22-08 contact window. The assertion was true by accident and the
#: test was measuring the opposite of what it claimed.
OFFICE_HOURS = {
    "contact_window_start_hour": 9,
    "contact_window_end_hour": 17,
    "quiet_hours_enabled": True,
}


# ---------------------------------------------------------------------------
# Item 1 -- regions and local hours
# ---------------------------------------------------------------------------


def test_regions_table_validates():
    report = region_windows.validate_regions()
    assert report["valid"], report["error_list"]


def test_same_utc_moment_resolves_to_different_local_hours():
    """The discrimination the UTC bug erased."""
    hours = {
        region: region_windows.local_hour(region, moment=PROBE)
        for region in region_windows.REGION_IDS
    }
    assert len(set(hours.values())) >= 4, hours
    assert hours["oceania_auckland"] == 12  # UTC+13 in NZDT
    assert hours["us_east"] == 19  # UTC-4 in EDT
    assert hours["asia_tokyo"] == 8  # no DST, UTC+9
    assert hours["europe_london"] == 0  # BST, UTC+1


def test_dst_is_a_delta_not_a_total():
    """Auckland is +12 standard and +13 in DST -- not +25."""
    assert region_windows.region_offset_hours("oceania_auckland", moment=PROBE) == 13.0
    winter = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert region_windows.region_offset_hours("oceania_auckland", moment=winter) == 12.0
    assert region_windows.region_offset_hours("us_west", moment=PROBE) == -7.0
    assert -14.0 <= region_windows.region_offset_hours("us_west", moment=PROBE) <= 14.0


def test_daylight_saving_uses_the_local_month_not_the_utc_month():
    """Computing the month at UTC is the same bug as computing the hour at UTC.

    Auckland crosses into the new year before UTC does, so at 2025-12-31T12:00Z
    the UTC month is December and the local month is January. Auckland is in DST
    in both, so this needs a region where the two answers differ in the table.
    """
    just_rolled_over = datetime(2025, 12, 31, 12, 0, tzinfo=timezone.utc)
    local = region_windows.local_hour("oceania_auckland", moment=just_rolled_over)
    # UTC+13 -> 01:00 on 1 January, locally.
    assert local == 1
    assert region_windows.local_date("oceania_auckland", moment=just_rolled_over) == "2026-01-01"


def test_unknown_region_falls_back_to_the_narrowest_window():
    resolved = region_windows.resolve_region("atlantis")
    assert resolved["region_id"] == region_windows.DEFAULT_REGION_ID
    window = region_windows.evaluate_contact_window(region_id="atlantis", moment=PROBE)
    assert window["resolved_fallback"] is True
    # The fallback is conservative about the *hour* -- it sits on UTC rather than
    # guessing an offset -- and it declares itself unattributed so the gate does
    # not invent an office closure. Narrowing it was tried and reverted: an
    # unknown region is an absence of a region, not a closed one.
    assert region_windows.region_offset_hours("atlantis", moment=PROBE) == 0.0
    assert resolved["unattributed"] is True
    assert region_windows.is_region_open("atlantis", moment=PROBE)["unattributed"] is True


def test_an_unattributed_region_is_not_a_closed_office():
    """Absence of a region has no office hours; a real region does."""
    unknown = region_windows.is_region_open("atlantis", moment=PROBE)
    assert unknown["open"] is True
    known = region_windows.is_region_open("asia_tokyo", moment=PROBE)
    assert known["open"] is False
    assert known["unattributed"] is False


def test_a_region_can_be_closed_while_the_day_is_early():
    """Region hours and the customer's quiet hours are different facts."""
    report = region_windows.is_region_open("asia_tokyo", moment=PROBE)
    assert report["local_hour"] == 8
    assert report["open"] is False
    assert "closed" in report["in_hours_reason"]


def test_weekend_is_reported_separately_from_hours():
    # 2026-10-03T12:00Z is Monday in UTC but already Tuesday in Auckland, and
    # 2026-10-04T12:00Z is Sunday in UTC but *Monday* in Auckland -- so "a Sunday
    # moment" has to be chosen against the local calendar, not the UTC one.
    sunday = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)  # Sunday 15:00 in Auckland
    report = region_windows.is_region_open("oceania_auckland", moment=sunday)
    assert report["is_weekend"] is True
    assert report["covers_weekends"] is False
    assert "weekend" in report["in_hours_reason"]


def test_overnight_window_wraps():
    """A window ending before it starts is legitimate and must not be inverted."""
    overnight_pref = {
        "contact_window_start_hour": 22,
        "contact_window_end_hour": 8,
        "quiet_hours_enabled": True,
    }
    inside = region_windows.evaluate_contact_window(
        preferences_map=overnight_pref, region_id="us_east", moment=PROBE
    )
    assert inside["local_hour"] == 19
    assert inside["within_declared_window"] is False
    overnight = region_windows.evaluate_contact_window(
        preferences_map=overnight_pref, region_id="europe_london", moment=PROBE
    )
    assert overnight["local_hour"] == 0
    assert overnight["within_declared_window"] is True


# ---------------------------------------------------------------------------
# The gate fix -- the test the Stage D suite was missing
# ---------------------------------------------------------------------------


def test_gate_uses_local_hour_not_utc_hour():
    """A customer at 23:00 local must not be contacted, whatever UTC says.

    Under the old implementation this moment was 23:00 UTC, and the gate read
    ``float(moment.hour)`` = 23.0, so a 22-08 quiet window evaluated as "inside
    their window" and the customer was called at 23:00 local.
    """
    gate = care_gate.consult(
        "recovery_outreach",
        dict(OFFICE_HOURS),
        {},
        purpose="recovery",
        region_id="oceania_auckland",
        now=datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc),  # 23:00 local
    )
    assert gate["local_hour"] == 23
    assert gate["region_id"] == "oceania_auckland"
    assert gate["push"] is False
    assert any("contact window" in reason for reason in gate["reasons"])


def test_identical_preferences_differ_by_region():
    """The property a one-customer test suite cannot express."""
    verdicts = {}
    for region in ("oceania_auckland", "us_east", "europe_london"):
        gate = care_gate.consult(
            "recovery_outreach",
            dict(OFFICE_HOURS),
            {},
            purpose="recovery",
            region_id=region,
            now=PROBE,
        )
        verdicts[region] = (gate["local_hour"], gate["push"], tuple(gate["reasons"]))
    # Against a 09:00-17:00 window: Auckland's 12:00 is contactable, London's
    # 00:00 is quiet. Identical preferences, one UTC moment, opposite verdicts.
    assert verdicts["oceania_auckland"][0] == 12
    assert verdicts["europe_london"][0] == 0
    assert verdicts["oceania_auckland"][1] is True
    assert verdicts["europe_london"][1] is False


def test_gate_still_validates_after_the_change():
    report = care_gate.validate_care_gate()
    assert report["valid"], report.get("error_list")
    assert report["gated"] == report["paths"]


# ---------------------------------------------------------------------------
# Item 2 -- purpose-limited personalization
# ---------------------------------------------------------------------------


def test_personalization_tables_validate():
    assert care_personalization.validate_personalization_limits()["valid"]
    assert care_personalization.validate_trusted_device_care()["valid"]


def test_recovery_may_refer_to_history_but_marketing_may_not_without_consent():
    """A recovery message written generically is worse at the one job it has."""
    recovery = care_personalization.resolve_personalization(
        purpose="recovery", consents={"personalization": False}
    )
    assert "history_reference" in recovery["allowed"]

    marketing = care_personalization.resolve_personalization(
        purpose="marketing", consents={"personalization": False}
    )
    assert "history_reference" not in marketing["allowed"]
    assert "history_reference" in marketing["refused"]
    assert "personalization consent" in marketing["refusal_reasons"]["history_reference"]

    granted = care_personalization.resolve_personalization(
        purpose="marketing", consents={"personalization": True}
    )
    assert "history_reference" in granted["allowed"]


def test_refusals_name_the_dimension():
    result = care_personalization.resolve_personalization(
        purpose="service", consents={"personalization": False}
    )
    for dimension in result["refused"]:
        assert dimension in result["refusal_reasons"]
        assert result["refusal_reasons"][dimension]


def test_analytics_sends_no_content_at_all():
    result = care_personalization.resolve_personalization(
        purpose="analytics", consents={"personalization": True}
    )
    assert result["allowed"] == []


def test_unknown_purpose_is_refused_not_defaulted():
    result = care_personalization.resolve_personalization(purpose="campaign_2027")
    assert result["known_purpose"] is False
    assert len(result["refused"]) == len(care_personalization.PERSONALIZATION_DIMENSIONS)


def test_peer_comparison_is_never_available():
    for purpose in care_personalization.PERSONALIZATION_PURPOSES:
        result = care_personalization.resolve_personalization(
            purpose=purpose, consents={"personalization": True}
        )
        assert "peer_comparison" not in result["allowed"]


def test_every_copilot_protected_fact_is_also_a_refused_dimension():
    """The copilot's list and the content table must agree.

    A fact kept out of an agent's opening line while a copywriter is free to reach
    for it is not protected at all.
    """
    from app.services import agent_copilot

    for purpose in care_personalization.PERSONALIZATION_PURPOSES:
        result = care_personalization.resolve_personalization(
            purpose=purpose, consents={"personalization": True}
        )
        for fact_id in agent_copilot.COPILOT_DO_NOT_LEAD_WITH_BY_ID:
            assert fact_id in result["refused"] or fact_id in result["allowed"]


# ---------------------------------------------------------------------------
# Item 5 -- trusted-device care paths
# ---------------------------------------------------------------------------


def test_trust_band_is_conservative_by_construction():
    assert care_personalization.resolve_trust_band(recognized=False)["band"] == "unknown"
    assert care_personalization.resolve_trust_band(recognized=True, recognitions=1)["band"] == "recognized"
    assert care_personalization.resolve_trust_band(recognized=True, recognitions=4)["band"] == "elevated"
    strong = care_personalization.resolve_trust_band(
        recognized=True, recognitions=20, signals_matched=3
    )
    assert strong["band"] == "trusted"


def test_one_sighting_cannot_unlock_a_money_moving_step():
    paths = care_personalization.resolve_care_paths(
        care_personalization.resolve_trust_band(recognized=True, recognitions=1)
    )
    assert "widen_credit_limit" in paths["refused_step_ids"]
    assert "apply_policy_adjustment" in paths["refused_step_ids"]


def test_trusted_device_never_bypasses_the_contact_gate():
    """Recognition shortens authentication, not permission."""
    trust = care_personalization.resolve_trust_band(
        recognized=True, recognitions=20, signals_matched=3
    )
    refused = care_personalization.resolve_care_paths(trust, gate={"push": True})
    assert "send_message_unattended" in refused["permitted_step_ids"]

    held = care_personalization.resolve_care_paths(trust, gate={"push": False})
    assert "send_message_unattended" in held["refused_step_ids"]
    assert any(
        "does not override a stated preference" in row["reason"] for row in held["refused"]
    )


def test_a_contact_refusal_does_not_stop_them_acting_on_their_own_account():
    """A gate about contact must not become a gate about their own business."""
    trust = care_personalization.resolve_trust_band(
        recognized=True, recognitions=20, signals_matched=3
    )
    held = care_personalization.resolve_care_paths(trust, gate={"push": False})
    for step in ("view_own_offers", "accept_own_offer", "decline_own_offer"):
        assert step in held["permitted_step_ids"], step


def test_overreach_invariant_is_asserted_not_assumed():
    with pytest.raises(AssertionError):
        care_personalization.assert_trust_does_not_overreach(
            {"overreach": ["widen_credit_limit"], "trust_band": "recognized", "rank": 1}
        )


# ---------------------------------------------------------------------------
# Items 3 and 4 -- the unified view and the default pane
# ---------------------------------------------------------------------------


def test_relationship_view_validates():
    report = relationship_view.validate_relationship_view()
    assert report["valid"], report["error_list"]


def test_copilot_is_the_default_pane():
    view = relationship_view.build_relationship_view(
        copilot_card={"opening": "Thanks for calling."}
    )
    assert view["pane"] == "copilot"
    assert view["pane_was_defaulted"] is True


def test_pane_override_requires_an_explicit_request():
    """A default any caller can move by naming something is not a default."""
    implicit = relationship_view.build_relationship_view(
        copilot_card={"opening": "x"}, pane="history"
    )
    assert implicit["pane"] == "copilot"
    assert implicit["pane_honoured"] is False
    assert "not requested explicitly" in implicit["pane_reason"]

    explicit = relationship_view.build_relationship_view(
        copilot_card={"opening": "x"}, pane="history", explicit_pane=True
    )
    assert explicit["pane"] == "history"
    assert explicit["pane_honoured"] is True


def test_unknown_pane_falls_back_rather_than_rendering_blank():
    resolved = relationship_view.resolve_pane("dashboard", explicit=True)
    assert resolved["pane"] == "copilot"
    assert resolved["honoured"] is False


def test_health_and_status_stay_independent():
    """A customer can be Trusted and critical at the same time."""
    view = relationship_view.build_relationship_view(
        copilot_card={"opening": "x"},
        health={"band": "critical"},
        status={"status": "Trusted"},
    )
    true_map = view["what_is_true"]
    assert true_map["loyalty_status"] == "Trusted"
    assert true_map["health_band"] == "critical"
    assert true_map["can_be_trusted_and_critical"] is True


def test_accepted_not_fulfilled_surfaces_as_open_work():
    view = relationship_view.build_relationship_view(
        copilot_card={"opening": "x"},
        offers=[
            {"reference": "A1", "status": "accepted"},
            {"reference": "B1", "status": "fulfilled"},
            {"reference": "C1", "status": "offered"},
        ],
    )
    assert view["open_commitments"]["accepted_not_fulfilled"] == 1
    assert view["open_commitments"]["offered_not_accepted"] == 1
    flagged = [row for row in view["offers"] if row["accepted_not_fulfilled"]]
    assert flagged and flagged[0]["reference"] == "A1"


def test_no_offer_outcome_claims_rule_attribution():
    view = relationship_view.build_relationship_view(
        copilot_card={"opening": "x"},
        offers=[{"reference": "A1", "status": "fulfilled", "generosity_scale": 1.5}],
    )
    for row in view["offers"]:
        assert row["attributable_to_rule"] is False
        assert "generosity_scale" in row["attribution_note"]


def test_overdue_stage_is_stated_not_left_as_a_timestamp():
    view = relationship_view.build_relationship_view(
        copilot_card={"opening": "x"},
        journey_plan={"stage": "awaiting_customer", "stage_due_at": datetime(2020, 1, 1)},
        now=datetime(2026, 10, 1),
    )
    assert view["journey"]["overdue"] is True
    assert "timebox" in view["journey"]["overdue_note"]


def test_deferred_contact_is_visible_to_the_agent():
    """An offer that exists undelivered is the system working."""
    view = relationship_view.build_relationship_view(
        copilot_card={"opening": "x"},
        offers=[{"reference": "A1", "status": "offered"}],
        gate={
            "push": False,
            "reasons": ["outside their contact window (quiet hours)"],
            "local_window": {"local_hour": 23, "region_id": "oceania_auckland"},
        },
    )
    rendered = relationship_view.render_relationship_view(view)
    assert "Contact deferred" in rendered
    assert "23:00" in rendered
    assert "A human decides" in rendered


def test_rendered_view_leads_with_the_person_not_the_process():
    view = relationship_view.build_relationship_view(
        copilot_card={"opening": "Sorry the meter reading was late."},
        journey_plan={"stage": "awaiting_customer", "stage_due_at": datetime(2020, 1, 1)},
        now=datetime(2026, 10, 1),
    )
    lines = relationship_view.render_relationship_view(view).splitlines()
    assert lines[0] == "Sorry the meter reading was late."
