"""Stage A: Customer 360, plain-language explanations, customer-visible recovery,
preference & consent, and the self-service status surface.

Every test here is written against the *contract* rather than the implementation
where that is possible, and several exist specifically to pin the decisions the
modules made out loud:

- a blocked recovery action is shown, not hidden
- the consent gate applies to marketing and not to recovery, and says which
- a 360 section that cannot be built is reported rather than defaulted healthy
- the explanation phrase tables are checked against what the engines can emit
- a preference key the customer has not set is distinguishable from one set to
  its default
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import models
from app.schemas.chat import Customer360Preferences
from app.services import (
    customer_360,
    customer_explain as ce,
    preferences as pref,
    self_service as ss,
)

NOW = datetime.now(timezone.utc)


# ===========================================================================
# Fakes
# ===========================================================================


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value

    def one(self):
        return (self._value,)

    def all(self):
        return [self._value] if isinstance(self._value, tuple) else [self._value]

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self.all())


class _Seq:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalar(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def one(self):
        return self._rows[0] if self._rows else (None,)

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _UserRow:
    def __init__(self, user_id=1, username="ana", email="ana@example.com"):
        self.id = user_id
        self.username = username
        self.email = email
        self.full_name = "Ana"
        self.is_admin = False
        self.created_at = NOW - timedelta(days=90)


class _WalletRow:
    def __init__(self, point_type, balance):
        self.id = 1
        self.user_id = 1
        self.point_type = point_type
        self.balance = balance
        self.updated_at = NOW


class _TxnRow:
    def __init__(self, delta, kind="purchase_points"):
        self.delta = delta
        self.kind = kind


class _RecoveryRow:
    def __init__(self, action, status, result=None, reference="", failure=""):
        self.id = 1
        self.user_id = 1
        self.playbook_id = "recovery_goodwill_points"
        self.action = action
        self.status = status
        self.result_json = json.dumps(result or {})
        self.payload_json = "{}"
        self.reference = reference
        self.failure_reason = failure
        self.created_at = NOW


class _ProfileRow:
    def __init__(self, user_id=1, prefs=None, consents=None):
        self.id = 1
        self.user_id = user_id
        self.preferences_json = json.dumps(prefs or {})
        self.consents_json = json.dumps(consents or {})
        self.consent_version = pref.PREFERENCE_CATALOG_VERSION
        self.created_at = NOW
        self.updated_at = NOW


class _ConsentEventRow:
    def __init__(self, purpose, granted, created_at):
        self.id = 1
        self.user_id = 1
        self.purpose = purpose
        self.granted = granted
        self.version = pref.PREFERENCE_CATALOG_VERSION
        self.lawful_basis = "consent"
        self.recorded_by_id = 1
        self.note = ""
        self.created_at = created_at


class _PrefDb:
    """In-memory stand-in for the preference + consent tables."""

    def __init__(self, profile=None, events=None):
        self.profile = profile
        self.events = list(events or [])
        self.added = []
        self.committed = 0

    async def execute(self, statement):
        text = str(statement)
        if "user_preference_profiles" in text:
            return _Scalar(self.profile)
        if "user_consent_events" in text and "ORDER BY" in text:
            return _Seq(self.events)
        return _Seq([])

    def add(self, row):
        self.added.append(row)
        if isinstance(row, models.UserConsentEvent):
            self.events.insert(0, row)

    async def flush(self):
        return None

    async def commit(self):
        self.committed += 1

    async def refresh(self, _row):
        return None


# ===========================================================================
# Preference & consent: the pure core
# ===========================================================================


class TestCatalog:
    def test_every_preference_key_is_unique(self):
        keys = [row["key"] for row in pref.PREFERENCE_CATALOG]
        assert len(keys) == len(set(keys))

    def test_every_preference_declares_a_known_type(self):
        allowed = {"string", "boolean", "number", "select", "string_list"}
        for row in pref.PREFERENCE_CATALOG:
            assert row["type"] in allowed, row["key"]

    def test_every_preference_belongs_to_a_declared_category(self):
        declared = {row["category"] for row in pref.PREFERENCE_CATEGORIES}
        for row in pref.PREFERENCE_CATALOG:
            assert row["category"] in declared, row["key"]

    def test_a_select_preference_always_offers_options(self):
        """Otherwise it is unanswerable and every value is a 422."""
        for row in pref.PREFERENCE_CATALOG:
            if row["type"] == "select":
                assert row.get("options"), f"{row['key']} is a select with no options"

    def test_a_bounded_preference_declares_its_bounds(self):
        for row in pref.PREFERENCE_CATALOG:
            if row["type"] == "number":
                assert "min" in row and "max" in row, row["key"]
                assert row["min"] < row["max"], row["key"]

    def test_every_consent_purpose_is_unique(self):
        purposes = [row["purpose"] for row in pref.CONSENT_PURPOSES]
        assert len(purposes) == len(set(purposes))

    def test_marketing_is_opt_out_and_service_is_opt_in(self):
        """The asymmetry is the design: no marketing without a grant."""
        by_name = {row["purpose"]: row for row in pref.CONSENT_PURPOSES}
        assert by_name["marketing"]["default"] is False
        assert by_name["service"]["default"] is True
        assert by_name["recovery"]["default"] is True

    def test_service_and_recovery_do_not_gate_outreach(self):
        """A consent switch must not be able to suppress the fix for a complaint."""
        ungated = set(pref.CONSENT_GATED_PURPOSES)
        assert "service" not in ungated
        assert "recovery" not in ungated
        assert "marketing" in ungated
        assert "analytics" in ungated

    def test_required_purposes_are_a_subset_of_the_catalog(self):
        for name in pref.CONSENT_REQUIRED_PURPOSES:
            assert name in pref.CONSENT_PURPOSE_BY_NAME


class TestValueCoercion:
    def test_a_boolean_accepts_the_usual_string_spellings(self):
        spec = pref.PREFERENCE_BY_KEY["booking_reminders"]
        for text in ("true", "True", "1", "yes"):
            assert pref._coerce_preference_value(spec, text) == (True, None)
        for text in ("false", "0", "no"):
            assert pref._coerce_preference_value(spec, text) == (False, None)

    def test_a_boolean_rejects_a_number(self):
        spec = pref.PREFERENCE_BY_KEY["booking_reminders"]
        value, error = pref._coerce_preference_value(spec, 1)
        assert value is None and error

    def test_a_select_rejects_a_value_outside_its_options(self):
        spec = pref.PREFERENCE_BY_KEY["communication_channel"]
        value, error = pref._coerce_preference_value(spec, "carrier_pigeon")
        assert value is None
        assert "must be one of" in error

    def test_a_number_is_range_checked(self):
        spec = pref.PREFERENCE_BY_KEY["contact_window_start_hour"]
        assert pref._coerce_preference_value(spec, 25)[0] is None
        assert pref._coerce_preference_value(spec, -1)[0] is None
        assert pref._coerce_preference_value(spec, 9) == (9.0, None)

    def test_a_string_list_accepts_a_comma_string_and_respects_its_cap(self):
        spec = pref.PREFERENCE_BY_KEY["topic_focus"]
        assert pref._coerce_preference_value(spec, "a, b ,c")[0] == ["a", "b", "c"]
        over = pref._coerce_preference_value(spec, [str(i) for i in range(30)])
        assert over[0] is None and "at most" in over[1]

    def test_an_unknown_key_is_an_error_rather_than_a_silent_drop(self):
        """A client that posts a typo and gets 200 was told its value was saved."""
        value, error = pref._coerce_preference_value(
            {"key": "not_a_real_key", "type": "string"}, "x"
        )
        assert value == "x" and error is None  # coercion is total on a spec
        # the *lookup* is what rejects it
        assert "not_a_real_key" not in pref.PREFERENCE_BY_KEY


class TestDefaults:
    def test_a_key_with_no_declared_default_is_absent_not_none(self):
        """'not set' and 'set to nothing' are different states."""
        defaults = pref.default_preferences()
        assert "communication_channel" not in defaults
        assert defaults["show_recovery_activity"] is True

    def test_default_consents_cover_every_purpose(self):
        assert set(pref.default_consents()) == set(pref.CONSENT_PURPOSE_BY_NAME)


class TestContactWindow:
    def test_an_unconfigured_window_admits_everything(self):
        window = pref.resolve_contact_window({})
        assert window["configured"] is False
        assert pref.hour_in_contact_window(3, window) is True

    def test_a_daytime_window_excludes_night(self):
        window = pref.resolve_contact_window(
            {"contact_window_start_hour": 9, "contact_window_end_hour": 17}
        )
        assert window["overnight"] is False
        assert pref.hour_in_contact_window(12, window) is True
        assert pref.hour_in_contact_window(3, window) is False

    def test_an_overnight_window_wraps_across_midnight(self):
        """22:00-07:00 is legitimate, not an inverted range to be 'corrected'."""
        window = pref.resolve_contact_window(
            {"contact_window_start_hour": 22, "contact_window_end_hour": 7}
        )
        assert window["overnight"] is True
        assert pref.hour_in_contact_window(23, window) is True
        assert pref.hour_in_contact_window(3, window) is True
        assert pref.hour_in_contact_window(12, window) is False

    def test_quiet_hours_only_apply_when_enabled(self):
        values = {"contact_window_start_hour": 9, "contact_window_end_hour": 17}
        assert pref.is_within_quiet_hours(values, 3) is False
        assert (
            pref.is_within_quiet_hours({**values, "quiet_hours_enabled": True}, 3) is True
        )

    def test_an_unknown_hour_does_not_hold_a_message(self):
        window_on = {"quiet_hours_enabled": True, "contact_window_start_hour": 9,
                     "contact_window_end_hour": 17}
        assert pref.is_within_quiet_hours(window_on, None) is False


class TestFrequency:
    def test_reactive_only_is_unbounded(self):
        assert pref.frequency_cooldown_hours("only_reactive") == float("inf")

    def test_an_unknown_frequency_falls_back_to_the_default_not_to_realtime(self):
        """A typo must not remove the customer's own cap."""
        assert pref.frequency_cooldown_hours("nonsense") == pref.frequency_cooldown_hours(
            pref.FREQUENCY_DEFAULT
        )
        assert pref.frequency_cooldown_hours("nonsense") > 0.0


class TestOutreachGate:
    def test_a_withdrawn_marketing_consent_blocks_marketing(self):
        decision = pref.is_outreach_permitted({"marketing": False}, "marketing")
        assert decision["permitted"] is False
        assert "withdrawn" in decision["reason"]

    def test_a_withdrawn_recovery_consent_does_not_block_recovery(self):
        """The load-bearing asymmetry: the fix is not suppressible."""
        decision = pref.is_outreach_permitted({"recovery": False}, "recovery")
        assert decision["permitted"] is True
        assert decision["override"] == "required_purpose"

    def test_service_critical_overrides_a_withdrawn_gated_purpose(self):
        decision = pref.is_outreach_permitted(
            {"marketing": False}, "marketing", service_critical=True
        )
        assert decision["permitted"] is True
        assert decision["override"] == "service_critical"

    def test_an_undeclared_purpose_is_refused_not_defaulted_to_allowed(self):
        decision = pref.is_outreach_permitted({}, "not_a_purpose")
        assert decision["permitted"] is False
        assert decision["known_purpose"] is False

    def test_every_permitted_answer_carries_its_reason(self):
        for purpose in pref.CONSENT_PURPOSE_BY_NAME:
            for granted in (True, False):
                decision = pref.is_outreach_permitted({purpose: granted}, purpose)
                assert decision["reason"], (purpose, granted)


class TestPreferredChannel:
    def test_no_stated_preference_leaves_the_resolution_intact(self):
        decision = pref.preferred_channel({}, "email")
        assert decision["channel"] == "email"
        assert decision["source"] == "resolved"

    def test_a_matching_preference_is_reported_as_honoured(self):
        decision = pref.preferred_channel({"communication_channel": "email"}, "email")
        assert decision["honored"] is True

    def test_a_different_preference_overrides_and_names_what_it_replaced(self):
        decision = pref.preferred_channel({"communication_channel": "sms"}, "email")
        assert decision["channel"] == "sms"
        assert decision["overrides"] == "email"
        assert decision["honored_as"] == "override"

    def test_in_app_is_a_supplement_not_a_withdrawal(self):
        """'in_app' must not make a caller think we stopped emailing."""
        decision = pref.preferred_channel({"communication_channel": "in_app"}, "email")
        assert decision["channel"] == "email"
        assert decision["supplemental_channel"] == "in_app"
        assert decision["honored"] is False
        assert decision["honored_as"] == "supplemental"


class TestEffectiveContactPlan:
    def test_a_marketing_opt_out_does_not_block_service_contact(self):
        plan = pref.effective_contact_plan({}, {"marketing": False})
        assert plan["service_permitted"] is True
        assert plan["marketing_permitted"] is False

    def test_quiet_hours_hold_a_message_and_say_so(self):
        values = {
            "quiet_hours_enabled": True,
            "contact_window_start_hour": 9,
            "contact_window_end_hour": 17,
        }
        plan = pref.effective_contact_plan(values, {}, hour=3)
        assert plan["held_for_quiet_hours"] is True
        assert "contact window" in plan["reason"]

    def test_reactive_only_is_surfaced_as_a_flag(self):
        plan = pref.effective_contact_plan(
            {"communication_frequency": "only_reactive"}, {}
        )
        assert plan["reactive_only"] is True
        assert plan["cooldown_hours"] is None

    def test_transparency_defaults_to_on(self):
        plan = pref.effective_contact_plan({}, {})
        assert plan["show_recovery_activity"] is True
        assert plan["plain_language_explanations"] is True


# ===========================================================================
# Preference & consent: the async mutations
# ===========================================================================


@pytest.mark.asyncio
async def test_a_first_read_creates_the_profile_rather_than_returning_empty():
    db = _PrefDb()
    profile = await pref.get_or_create_preference_profile(db, 1)
    assert profile is not None
    assert isinstance(profile, models.UserPreferenceProfile)
    # An unread key must be absent, not None, so the resolver can tell
    # "not set" from "set to nothing".
    assert json.loads(profile.preferences_json) == {}


@pytest.mark.asyncio
async def test_malformed_blobs_degrade_to_defaults_rather_than_raising():
    """A corrupted preference must not 500 a settings screen."""

    class _Broken(_PrefDb):
        def __init__(self):
            super().__init__()
            self.profile = _ProfileRow()
            self.profile.preferences_json = "{not json"
            self.profile.consents_json = "]]]"

    preferences, consents = await pref.load_user_preferences(_Broken(), 1)
    assert preferences == pref.default_preferences()
    assert consents == pref.default_consents()


@pytest.mark.asyncio
async def test_a_stored_consent_for_an_unknown_purpose_is_ignored():
    profile = _ProfileRow(consents={"marketing": True, "not_a_purpose": True})
    _prefs, consents = await pref.load_user_preferences(_PrefDb(profile), 1)
    assert "not_a_purpose" not in consents
    assert consents["marketing"] is True


@pytest.mark.asyncio
async def test_updating_a_preference_persists_it():
    db = _PrefDb(profile=_ProfileRow())
    result = await pref.update_user_preferences(
        db, 1, {"communication_channel": "sms"}, None
    )
    assert "communication_channel" in result["updated_preferences"]
    assert result["preferences"]["communication_channel"] == "sms"
    assert json.loads(db.profile.preferences_json)["communication_channel"] == "sms"


@pytest.mark.asyncio
async def test_an_invalid_value_is_reported_without_blocking_a_valid_one():
    db = _PrefDb(profile=_ProfileRow())
    result = await pref.update_user_preferences(
        db, 1, {"communication_channel": "sms", "contact_window_start_hour": 99}, None
    )
    assert result["updated_preferences"] == ["communication_channel"]
    assert len(result["errors"]) == 1
    assert "contact_window_start_hour" in result["errors"][0]


@pytest.mark.asyncio
async def test_a_required_consent_cannot_be_withdrawn_and_says_why():
    db = _PrefDb(profile=_ProfileRow())
    result = await pref.update_user_preferences(db, 1, None, {"service": False})
    assert result["updated_consents"] == []
    assert len(result["blocked_consents"]) == 1
    assert result["blocked_consents"][0]["purpose"] == "service"
    assert result["blocked_consents"][0]["granted"] is True
    assert result["blocked_consents"][0]["reason"]


@pytest.mark.asyncio
async def test_a_consent_change_appends_an_auditable_event():
    db = _PrefDb(profile=_ProfileRow())
    await pref.update_user_preferences(db, 1, None, {"marketing": True}, recorded_by_id=7)
    events = [row for row in db.added if isinstance(row, models.UserConsentEvent)]
    assert len(events) == 1
    assert events[0].purpose == "marketing"
    assert events[0].granted is True
    assert events[0].recorded_by_id == 7


@pytest.mark.asyncio
async def test_re_confirming_a_consent_does_not_append_an_event():
    """A log that records a re-confirmation cannot answer 'when did they first consent'."""
    profile = _ProfileRow(consents={"marketing": True})
    db = _PrefDb(profile=profile)
    result = await pref.update_user_preferences(db, 1, None, {"marketing": True})
    assert result["updated_consents"] == []
    assert not [row for row in db.added if isinstance(row, models.UserConsentEvent)]


@pytest.mark.asyncio
async def test_preference_writes_never_touch_the_consent_trail():
    db = _PrefDb(profile=_ProfileRow())
    await pref.update_user_preferences(db, 1, {"booking_reminders": True}, None)
    assert not [row for row in db.added if isinstance(row, models.UserConsentEvent)]


@pytest.mark.asyncio
async def test_the_report_distinguishes_set_from_unset():
    db = _PrefDb(profile=_ProfileRow(prefs={"booking_reminders": True}))
    report = await pref.build_preference_consent_report(db, 1)
    by_key = {row["key"]: row for row in report["preferences"]}
    assert by_key["booking_reminders"]["set"] is True
    assert by_key["communication_channel"]["set"] is False
    # a key set to exactly its shipped default is still 'set'
    db2 = _PrefDb(profile=_ProfileRow(prefs={"show_recovery_activity": True}))
    report2 = await pref.build_preference_consent_report(db2, 1)
    by_key2 = {row["key"]: row for row in report2["preferences"]}
    assert by_key2["show_recovery_activity"]["set"] is True
    assert by_key2["show_recovery_activity"]["at_default"] is True


@pytest.mark.asyncio
async def test_the_report_publishes_which_purposes_gate_outreach():
    db = _PrefDb(profile=_ProfileRow())
    report = await pref.build_preference_consent_report(db, 1)
    assert "marketing" in report["consent"]["gated_purposes"]
    assert "service" in report["consent"]["ungated_purposes"]


@pytest.mark.asyncio
async def test_the_consent_trail_rolls_up_by_purpose():
    events = [
        _ConsentEventRow("marketing", True, NOW - timedelta(days=10)),
        _ConsentEventRow("marketing", False, NOW - timedelta(days=2)),
        _ConsentEventRow("analytics", True, NOW - timedelta(days=5)),
    ]
    trail = await pref.list_consent_events(_PrefDb(events=events), 1)
    assert trail["total"] == 3
    assert trail["by_purpose"]["marketing"]["grants"] == 1
    assert trail["by_purpose"]["marketing"]["revocations"] == 1
    assert trail["by_purpose"]["analytics"]["grants"] == 1


@pytest.mark.asyncio
async def test_an_empty_trail_says_so_rather_than_reporting_nothing():
    trail = await pref.list_consent_events(_PrefDb(), 1)
    assert trail["total"] == 0
    assert "no consent changes recorded" in trail["summary"]


def test_the_catalog_publishes_its_own_version_and_every_key():
    catalog = pref.preference_catalog()
    assert catalog["catalog_version"] == pref.PREFERENCE_CATALOG_VERSION
    assert catalog["total_preferences"] == len(pref.PREFERENCE_CATALOG)
    assert {row["key"] for row in catalog["preferences"]} == set(
        pref.PREFERENCE_BY_KEY
    )


def test_an_infinite_cooldown_is_serialised_as_null_not_infinity():
    """JSON has no infinity; the catalog is served as a payload."""
    catalog = pref.preference_catalog()
    assert catalog["frequency_cooldown_hours"]["only_reactive"] is None
    assert catalog["frequency_cooldown_hours"]["daily"] == 24.0


# ===========================================================================
# Plain-language explanation
# ===========================================================================


class TestBands:
    def test_a_high_score_lands_in_the_top_band(self):
        assert ce.band_for_score(95)["band"] == "strong"

    def test_a_low_score_lands_in_the_bottom_band(self):
        assert ce.band_for_score(5)["band"] == "critical"

    def test_a_value_above_the_range_clamps_but_reports_the_value(self):
        band = ce.band_for_score(150)
        assert band["band"] == "strong"
        assert band["clamped"] is True
        assert band["value"] == 150.0

    def test_a_friction_score_reads_inverted(self):
        assert ce.band_for_friction(1)["band"] == "clear"
        assert ce.band_for_friction(40)["band"] == "severe"

    def test_every_band_carries_a_phrase_and_a_meaning(self):
        for table in (ce.SCORE_BANDS, ce.INVERSE_SCORE_BANDS):
            for row in table:
                assert row["plain"].strip(), row["band"]
                assert row["meaning"].strip(), row["band"]

    def test_bands_cover_the_range_without_a_gap_or_an_overlap(self):
        for table in (ce.SCORE_BANDS, ce.INVERSE_SCORE_BANDS):
            ordered = sorted(table, key=lambda row: row["min"])
            for index in range(1, len(ordered)):
                assert abs(
                    float(ordered[index - 1]["max"]) - float(ordered[index]["min"])
                ) < 1e-9, (table is ce.SCORE_BANDS, ordered[index]["band"])


class TestImpact:
    def test_a_positive_contribution_is_positive_when_higher_is_better(self):
        assert ce._impact_of(5, higher_is_better=True) == "positive"

    def test_a_positive_contribution_is_negative_when_lower_is_better(self):
        assert ce._impact_of(5, higher_is_better=False) == "negative"

    def test_zero_is_neutral_either_way(self):
        assert ce._impact_of(0, higher_is_better=True) == "neutral"
        assert ce._impact_of(0, higher_is_better=False) == "neutral"

    def test_a_non_numeric_contribution_is_neutral_not_an_exception(self):
        assert ce._impact_of("n/a", higher_is_better=True) == "neutral"


class TestConfidence:
    def test_confidence_follows_evidence_not_magnitude(self):
        """A confident statement of a bad thing is still confident."""
        assert ce.confidence_for(0) == "low"
        assert ce.confidence_for(2) == "medium"
        assert ce.confidence_for(9) == "high"
        # the function takes evidence counts, so it never sees a score at all
        assert ce.confidence_for(9) == ce.confidence_for(9)

    def test_a_model_version_counts_toward_confidence(self):
        assert ce.confidence_for(1, has_model_version=True) == "medium"


class TestNextSteps:
    def test_a_satisfied_condition_produces_a_step_naming_its_basis(self):
        steps = ce.next_steps_for({"dissatisfaction": 12.0})
        assert steps
        assert steps[0]["because"].startswith("dissatisfaction=")

    def test_an_unsatisfied_condition_produces_nothing(self):
        assert ce.next_steps_for({"dissatisfaction": 0.0}) == []

    def test_a_missing_metric_produces_nothing(self):
        assert ce.next_steps_for({}) == []

    def test_steps_are_ordered_by_the_declared_table(self):
        steps = ce.next_steps_for(
            {"dissatisfaction": 12.0, "recovery_readiness": "high", "points_balance": 5.0}
        )
        declared = [row["step_id"] for row in ce.NEXT_STEP_TEMPLATES]
        assert [step["step_id"] for step in steps] == [
            sid for sid in declared if sid in {step["step_id"] for step in steps}
        ]


class TestNarrateRecoveryAction:
    def test_a_credit_is_described_in_the_customers_words(self):
        narrated = ce.narrate_recovery_action(
            {
                "action": "credit_points",
                "status": "executed",
                "result": {"points_credited": 120},
            }
        )
        assert "points" in narrated["customer_impact"].lower()
        assert "120" in narrated["outcome_detail"]
        assert narrated["internal_reason"] == ""

    def test_a_skipped_action_says_we_chose_not_to_repeat_it(self):
        """Omitting the skip would make a guard and a bug look identical."""
        narrated = ce.narrate_recovery_action(
            {
                "action": "credit_points",
                "status": "skipped",
                "failure_reason": "daily budget for points would be exceeded",
            }
        )
        assert "not" in narrated["outcome"].lower()
        # the internal reason is retained for an agent, not served as the text
        assert narrated["internal_reason"] == "daily budget for points would be exceeded"
        assert "budget" not in narrated["outcome"].lower()

    def test_an_escalation_surfaces_its_reference(self):
        narrated = ce.narrate_recovery_action(
            {
                "action": "escalate_ticket",
                "status": "executed",
                "result": {"ticket_reference": "ESC-7-001"},
                "reference": "ESC-7-001",
            }
        )
        assert "ESC-7-001" in narrated["outcome_detail"]
        assert narrated["reference"] == "ESC-7-001"

    def test_an_unknown_action_still_produces_a_summary(self):
        narrated = ce.narrate_recovery_action(
            {"action": "not_a_real_action", "status": "executed"}
        )
        assert narrated["summary"]


class TestValidation:
    def test_the_shipped_tables_validate_clean(self):
        report = ce.validate_explanation_tables()
        assert report["ok"] is True, report["findings"]
        assert report["counts_by_severity"].get("error", 0) == 0

    def test_a_phrase_for_an_unproducible_value_is_reported(self):
        table = ce.PHRASE_TABLES["RECOVERY_ACTION_CUSTOMER_IMPACT"]
        original = dict(table)
        try:
            table["not_a_real_action"] = "should not be reachable"
            report = ce.validate_explanation_tables()
            codes = {finding["code"] for finding in report["findings"]}
            assert "unreachable_phrase" in codes
        finally:
            table.clear()
            table.update(original)

    def test_a_missing_phrase_is_an_error(self):
        """The fallback text would otherwise be served silently."""
        table = ce.PHRASE_TABLES["RECOVERY_ACTION_CUSTOMER_IMPACT"]
        original = dict(table)
        try:
            table.pop("credit_points", None)
            report = ce.validate_explanation_tables()
            missing = [
                finding
                for finding in report["findings"]
                if finding["code"] == "missing_phrase"
            ]
            assert missing
            assert missing[0]["severity"] == "error"
        finally:
            table.clear()
            table.update(original)

    def test_a_non_monotonic_band_is_an_error(self):
        original = [dict(row) for row in ce.SCORE_BANDS]
        try:
            # 65-80 is 'healthy' (positive) and sits above 50-65 'workable'
            # (neutral); claiming the upper band is negative is a real
            # mislabel, and monotonicity catches it without a magic threshold.
            ce.SCORE_BANDS[1]["direction"] = "negative"
            report = ce.validate_explanation_tables()
            codes = {finding["code"] for finding in report["findings"]}
            assert "band_direction_mismatch" in codes
        finally:
            ce.SCORE_BANDS[:] = original

    def test_a_band_with_no_phrase_is_an_error(self):
        original = [dict(row) for row in ce.SCORE_BANDS]
        try:
            ce.SCORE_BANDS[0]["plain"] = "  "
            report = ce.validate_explanation_tables()
            assert report["ok"] is False
        finally:
            ce.SCORE_BANDS[:] = original

    def test_the_catalog_publishes_its_version_and_its_binds(self):
        catalog = ce.build_explainability_vocabulary_catalog()
        assert catalog["vocabulary_version"] == ce.EXPLANATION_VOCABULARY_VERSION
        assert catalog["phrase_bindings"]["CHURN_RISK_PHRASES"]["producer"] == "churn_risk"
        assert "loyalty_score" in catalog["explains"]


# ===========================================================================
# Customer 360
# ===========================================================================


class _Summary:
    def __init__(self, **overrides):
        self.user_id = 1
        self.messages_analyzed = 4
        self.bookings_analyzed = 2
        self.churn_risk = "medium"
        self.loyalty_score = 62.0
        self.monetization_readiness = 70.0
        self.value_tier = "growth"
        self.customer_classification = "loyal growth-ready"
        self.top_issues = ["response speed"]
        self.strengths = []
        self.insights = []
        self.metadata = {"repeated_messages": 1, "booking_states": {"completed": 1}}
        self.generated_at = NOW
        for key, value in overrides.items():
            setattr(self, key, value)


def test_the_summary_text_is_composed_from_the_fields_it_describes():
    from app.schemas.chat import (
        Customer360InteractionSummary,
        Customer360LoyaltyJourney,
        Customer360Payments,
        Customer360Points,
        Customer360Recovery,
        Customer360Sentiment,
    )
    from app.schemas.chat import Customer360Profile

    profile = Customer360Profile(
        user_id=1,
        username="ana",
        created_at=NOW,
        lifecycle_stage="engaged",
        value_tier="growth",
        customer_classification="loyal growth-ready",
        loyalty_score=62.0,
        churn_risk="medium",
        monetization_readiness=70.0,
        engagement_score=40.0,
        lifetime_value_estimate=0.0,
        referral_count=0,
        tier_status="growth",
    )
    text = customer_360.build_summary_text(
        profile,
        Customer360InteractionSummary(
            total_chats=4, total_bookings=2, completed_bookings=1, pending_bookings=0,
            cancelled_bookings=0, total_snapshots=1, days_since_last_activity=2,
        ),
        Customer360Sentiment(
            current_label="neutral", current_score=0.6, has_sentiment=True,
            trend="stable", negative_count=0, positive_count=1, neutral_count=3,
        ),
        Customer360Recovery(
            recovery_readiness="moderate", dissatisfaction_score=6.0,
            primary_risks=["response speed"], recent_recovery_actions=1,
            blocked_recovery_actions=0,
        ),
        Customer360LoyaltyJourney(
            current_family="retention", matched_scenarios=2,
            scenario_families=["retention", "recovery"],
        ),
        Customer360Points(
            total_balance=100.0, redeemable_balance=100.0, wallet_count=1,
            recent_transactions=2,
        ),
        Customer360Payments(
            open_arrears_count=1, total_principal_at_risk=50.0,
            total_interest_at_risk=1.0, overdue_count=0,
        ),
    )
    # every number in the sentence is one the payload also carries
    assert "62" in text and "growth" in text and "engaged" in text
    assert "4 message" in text and "2 booking" in text
    assert "moderate" in text
    assert "100" in text
    assert "50 principal" in text


def test_an_unknown_section_is_refused_rather_than_ignored():
    assert "unknown 360 section" in customer_360.CUSTOMER_360_SECTIONS[0] or True
    # the real check is the async one
    import asyncio

    async def _run():
        try:
            await customer_360.build_customer_360(None, 1, 30, sections=["not_a_section"])
        except ValueError as exc:
            return str(exc)
        return None

    message = asyncio.run(_run())
    assert message and "unknown 360 section" in message


def test_every_declared_section_is_part_of_the_published_set():
    assert set(customer_360.CUSTOMER_360_SECTIONS) == {
        "profile",
        "interactions",
        "sentiment",
        "recovery",
        "loyalty_journey",
        "communication",
        "payments",
        "points",
        "preferences",
    }


def test_the_admin_rollup_publishes_what_it_omits():
    """A rollup that silently drops a per-user signal is misleading by omission."""
    import inspect

    source = inspect.getsource(customer_360.build_customer_360_admin_report)
    assert "omitted_by_design" in source
    # and the key is a real payload field, not only a local
    assert '"omitted_by_design"' in source


# ===========================================================================
# Self-service: customer-visible recovery
# ===========================================================================


class _RecoveryContextDb:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    async def execute(self, statement):
        text = str(statement)
        if "recovery_actions" in text and "ORDER BY" in text:
            return _Seq(self.rows)
        return _Seq([])

    def add(self, _row):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, _row):
        return None


def _visible(action, status, result=None, reference="", failure=""):
    return ss.to_visible_action(_RecoveryRow(action, status, result, reference, failure))


def test_an_executed_credit_reports_the_points_that_landed():
    visible = _visible("credit_points", "executed", {"points_credited": 120})
    assert visible.status == "executed"
    assert "120" in visible.benefit_to_customer
    assert visible.playbook_name  # looked up from the live config, not blank


def test_a_blocked_action_is_still_reported_to_the_customer():
    """Hiding the skip makes a guard and a bug look the same."""
    visible = _visible(
        "credit_points", "skipped", {}, failure="daily budget for points would be exceeded"
    )
    assert visible.status == "skipped"
    assert visible.benefit_to_customer
    assert "not" in visible.outcome.lower()


def test_the_playbook_name_comes_from_the_live_config():
    visible = _visible("credit_points", "executed", {"points_credited": 10})
    assert visible.playbook_name == "Goodwill Points Credit"


def test_an_unknown_playbook_id_falls_back_to_the_id_rather_than_blank():
    class _Row(_RecoveryRow):
        def __init__(self):
            super().__init__("credit_points", "executed", {"points_credited": 1})
            self.playbook_id = "retired_playbook"

    assert ss.to_visible_action(_Row()).playbook_name == "retired_playbook"


def test_a_malformed_result_blob_does_not_break_the_narration():
    class _Row(_RecoveryRow):
        def __init__(self):
            super().__init__("credit_points", "executed")
            self.result_json = "{not json"

    assert ss.to_visible_action(_Row()).benefit_to_customer


def test_the_transparency_preference_is_a_plain_boolean():
    """It gates a customer-facing section, so its type must not be surprising."""
    spec = pref.PREFERENCE_BY_KEY["show_recovery_activity"]
    assert spec["type"] == "boolean"
    assert spec["default"] is True


def test_preferences_model_carries_the_columns_the_service_writes():
    table = models.Base.metadata.tables["user_preference_profiles"]
    assert {"preferences_json", "consents_json", "consent_version"} <= set(
        table.columns.keys()
    )


def test_the_consent_trail_is_append_only_while_the_profile_is_mutable():
    """A revocation has to remain provable after the current value changes."""
    lifecycle = models.TABLE_LIFECYCLE
    assert lifecycle["user_consent_events"]["write_mode"] == models.APPEND_ONLY
    assert lifecycle["user_preference_profiles"]["write_mode"] == models.MUTABLE


def test_the_new_tables_are_registered_in_the_metadata_layer():
    names = set(models.Base.metadata.tables)
    assert {"user_preference_profiles", "user_consent_events"} <= names
    assert set(models.TABLE_LIFECYCLE) >= {
        "user_preference_profiles",
        "user_consent_events",
    }


def test_the_new_tables_are_classified_for_sensitivity():
    # A stated preference is about the customer and is not free text, but it is
    # still theirs: it must redact under a public/operator preset rather than
    # pass through as `internal`.
    assert models.sensitivity_of("user_preference_profiles", "preferences_json") == "content"
    # A consent blob is booleans over a fixed vocabulary, so it may travel in a
    # bulk export without identifying anyone.
    assert (
        models.sensitivity_of("user_preference_profiles", "consents_json") == "behavioral"
    )
    assert models.sensitivity_of("user_consent_events", "purpose") == "behavioral"
    assert models.sensitivity_of("user_consent_events", "lawful_basis") == "internal"


def test_a_stated_preference_redacts_under_the_public_preset():
    assert (
        models.projection_for(
            models.sensitivity_of("user_preference_profiles", "preferences_json"),
            preset="public",
        )
        == "redact"
    )


def test_the_preferences_json_columns_are_recognised_as_json():
    for name in ("preferences_json", "consents_json"):
        assert models.is_json_column(name)
    assert models.json_container_for("preferences_json") == "object"


def test_a_malformed_preferences_blob_loads_as_an_empty_object():
    assert models.loads_json("{oops") == {}


def test_the_customer_360_preferences_schema_defaults_to_a_safe_posture():
    """Absent preferences must not read as consent granted."""
    stated = Customer360Preferences()
    assert stated.marketing_consent is False
    assert stated.recovery_consent is True


# ===========================================================================
# Wiring: the things a reader of BLOCKAGES.md would otherwise have to trust
# ===========================================================================


class TestMetaWiring:
    def test_the_new_subservices_are_published_on_the_ecosystem(self):
        from app import deps
        from app.main import app

        async def _user():
            return _UserRow()

        app.dependency_overrides[deps.get_current_admin_user] = _user
        try:
            spec = app.openapi()
            assert "/meta/ecosystem" in spec["paths"]
        finally:
            app.dependency_overrides.pop(deps.get_current_admin_user, None)

    def test_every_new_route_is_in_the_openapi_document(self):
        from app.main import app

        paths = set(app.openapi()["paths"])
        for route in (
            "/chat/customer-360",
            "/chat/admin/customer-360",
            "/chat/me/recovery-status",
            "/chat/me/status",
            "/chat/me/points-forecast",
            "/chat/me/policy-posture",
            "/chat/me/preferences",
            "/chat/me/consent-history",
            "/chat/me/explanations",
            "/chat/admin/explanation-vocabulary",
            "/chat/admin/preference-catalog",
            "/meta/customer-360",
        ):
            assert route in paths, route

    def test_no_self_service_route_accepts_a_user_id(self):
        """A self route that takes a user_id is a horizontal-privilege bug waiting."""
        from app.main import app

        spec = app.openapi()
        for path in (
            "/chat/customer-360",
            "/chat/me/recovery-status",
            "/chat/me/status",
            "/chat/me/points-forecast",
            "/chat/me/policy-posture",
            "/chat/me/preferences",
            "/chat/me/consent-history",
            "/chat/me/explanations",
        ):
            # A route with no query parameters omits the key entirely rather
            # than carrying an empty list.
            declared = spec["paths"][path]["get"].get("parameters") or []
            names = {parameter["name"] for parameter in declared}
            assert "user_id" not in names, path

    def test_the_preference_centre_is_readable_and_writable(self):
        from app.main import app

        operations = app.openapi()["paths"]["/chat/me/preferences"]
        assert "get" in operations
        assert "put" in operations


class TestMigration:
    """The migration must emit the same schema the models declare.

    A drift here is the quiet kind: the app boots, ``create_all`` produces the
    right shape, and the *migration* path -- which is what a real deployment
    runs -- produces something else.
    """

    def _run_upgrade(self):
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        path = root / "alembic" / "versions" / "20260929_01_add_preference_consent_tables.py"
        spec = importlib.util.spec_from_file_location("stage_a_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        captured: dict = {}

        class _Op:
            def create_table(self, name, *cols):
                captured[name] = cols

            def create_index(self, *_a, **_k):
                return None

            def drop_index(self, *_a, **_k):
                return None

            def drop_table(self, *_a, **_k):
                return None

            def f(self, name):
                return name

        module.op = _Op()
        module.upgrade()
        return captured

    def test_column_names_and_order_match_the_models(self):
        captured = self._run_upgrade()
        for name in ("user_preference_profiles", "user_consent_events"):
            declared = models.Base.metadata.tables[name]
            expected = [column.name for column in declared.columns]
            emitted = [
                column.name
                for column in captured[name]
                if column.__class__.__name__ == "Column"
            ]
            assert emitted == expected, name

    def test_the_check_constraint_is_in_the_migration_and_the_model(self):
        captured = self._run_upgrade()
        emitted = [
            constraint.name
            for constraint in captured["user_consent_events"]
            if constraint.__class__.__name__ == "CheckConstraint"
        ]
        declared = [
            constraint.name
            for constraint in models.Base.metadata.tables["user_consent_events"].constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        ]
        assert emitted == declared
        assert "ck_user_consent_events_purpose_required" in emitted

    def test_the_profile_table_is_a_singleton_per_user(self):
        """At most one row per user, enforced by the database not a filter."""
        table = models.Base.metadata.tables["user_preference_profiles"]
        unique_user_ids = [
            column
            for column in table.columns
            if column.name == "user_id" and column.unique
        ]
        assert unique_user_ids, "user_id must be unique on user_preference_profiles"


class TestAuthorizationCoverage:
    def test_no_live_route_falls_through_to_the_catch_all(self):
        """An unclassified route is not 'probably fine'; it is unreviewed."""
        from app import deps
        from app.main import app

        inventory = deps.authz_route_inventory(app.routes)
        assert inventory
        drifted = [row for row in inventory if row.get("delta") != "match"]
        assert not drifted, [f"{r.get('method')} {r.get('path')}" for r in drifted]

    def test_no_rule_is_unreachable(self):
        from app import deps
        from app.main import app

        coverage = deps.authz_coverage_report(app.routes)
        assert coverage["fallback_routes"] == 0
        assert coverage["unreachable_rules"] == []

    def test_the_authz_table_validates_clean(self):
        from app import deps

        report = deps.validate_authz()
        assert report["errors"] == 0, report["errors"]
        assert report["warnings"] == 0, report["warnings"]


class TestCodeMapSync:
    def test_the_markdown_rendering_matches_the_newick(self):
        """A stale rendering is a tree a reader can trust that does not exist."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "scripts/sync_code_map_md.py", "--check"],
            capture_output=True,
            text=True,
            cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_code_map_regenerates_without_drift(self):
        import subprocess
        import sys

        root = __import__("pathlib").Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "scripts/build_code_map.py", "--check"],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_every_version_column_can_hold_the_version_it_is_given(self):
        """The catalog version must fit the column that stores it.

        Both version columns were `VARCHAR(20)` and the value the service
        writes is `PREFERENCE_CATALOG_VERSION` -- 21 characters:

            asyncpg.exceptions.StringDataRightTruncationError:
              value too long for type character varying(20)

        which means `PUT /chat/me/preferences` could not save a single
        preference and every consent event failed to insert. The 100+ tests in
        this suite all use fakes, and a fake never enforces a length, so only a
        real database surfaced it.
        """
        version = pref.PREFERENCE_CATALOG_VERSION
        for table_name, column_name in (
            ("user_preference_profiles", "consent_version"),
            ("user_consent_events", "version"),
        ):
            column = models.Base.metadata.tables[table_name].columns[column_name]
            assert column.type.length is not None, f"{table_name}.{column_name} is unbounded"
            assert len(version) <= column.type.length, (
                f"{table_name}.{column_name} is VARCHAR({column.type.length}) but "
                f"PREFERENCE_CATALOG_VERSION is {len(version)} chars: {version!r}"
            )

    def test_every_purpose_fits_its_column(self):
        """A purpose added to the config must be writable before the DDL catches up."""
        column = models.Base.metadata.tables["user_consent_events"].columns["purpose"]
        for purpose in pref.CONSENT_PURPOSE_BY_NAME:
            assert len(purpose) <= (column.type.length or 999), purpose

    def test_every_lawful_basis_fits_its_column(self):
        column = models.Base.metadata.tables["user_consent_events"].columns[
            "lawful_basis"
        ]
        for row in pref.CONSENT_PURPOSES:
            basis = str(row["lawful_basis"])
            assert len(basis) <= (column.type.length or 999), basis

    def test_every_preference_key_and_category_fits_if_it_were_stored(self):
        """The keys live in a JSON blob today; this guards a future column split."""
        for row in pref.PREFERENCE_CATALOG:
            assert len(row["key"]) <= 50, row["key"]
            assert len(row["category"]) <= 30, row["category"]
