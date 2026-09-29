"""Tests for the ``app.i18n`` governance expansion.

The module grew from 564 to ~3,000 LOC. What was added is a report layer: six
config tables, six audits, a validator, and a locale-aware number formatter.
None of it is wired into the request path, and that is the property most of
this file is checking.

Three groups:

1. **Pinned behaviour.** ``ORIG_*`` constants transcribe what the shipped
   functions did before the expansion. Every one is asserted, so "the renderer
   was not touched" is a checkable claim rather than an intention. Several of
   them are *defects* -- a leaked ``{name}``, a plural message that renders its
   zero branch, an ``AttributeError`` escaping ``translate`` -- and they are
   pinned as defects on purpose, because the expansion reports them and does
   not repair them.
2. **The tables and the audits.** Shape, internal coherence, and the fact that
   each guard actually fires against a deliberately broken catalog rather than
   only ever returning "clean" on a well-formed one.
3. **The surfaces.** ``/meta/i18n``, ``/meta/scoring-catalog``, ``/meta``,
   ``/meta/features`` and ``/meta/ecosystem``.

Mutation discipline: tables are replaced wholesale with
``monkeypatch.setattr``, never edited in place. ``list(TABLE)`` is a shallow
copy whose row dicts are the live ones, and an in-place row edit restored by
assignment would reinstate the mutation rather than undo it. The only shared
mutable state touched is ``OVERRIDES``, which is cleared in a ``finally``.
"""
import pytest

from app import i18n


# ==============================================================================
# 1. Pinned shipped behaviour
# ==============================================================================

# Transcribed from the module as it stood at 564 LOC.
ORIG_SUPPORTED_LOCALES = {
    "en": {"default": "English", "fallback": "en"},
    "es": {"default": "Español", "fallback": "en"},
    "fr": {"default": "Français", "fallback": "en"},
}
ORIG_RTL_LOCALES = ("ar", "he", "fa", "ur")
ORIG_PLURAL_CATEGORIES = {
    "en": ("one", "other"),
    "es": ("one", "other"),
    "fr": ("one", "other"),
    "ar": ("zero", "one", "two", "few", "many", "other"),
}
ORIG_DEFAULT_LOCALE = "en"
ORIG_CATALOG_KEYS = 15
ORIG_TEMPLATE_COUNT = 45


def test_locale_registry_tables_are_untouched():
    assert i18n.SUPPORTED_LOCALES == ORIG_SUPPORTED_LOCALES
    assert i18n.RTL_LOCALES == ORIG_RTL_LOCALES
    assert i18n.PLURAL_CATEGORIES == ORIG_PLURAL_CATEGORIES
    assert i18n.DEFAULT_LOCALE == ORIG_DEFAULT_LOCALE


def test_catalog_shape_is_untouched():
    assert len(i18n.MESSAGE_CATALOG) == ORIG_CATALOG_KEYS
    assert sum(len(entry) for entry in i18n.MESSAGE_CATALOG.values()) == ORIG_TEMPLATE_COUNT
    for entry in i18n.MESSAGE_CATALOG.values():
        assert set(entry) == {"en", "es", "fr"}


def test_locale_resolution_is_untouched():
    assert i18n.normalize_locale("PT_br") == "pt-br"
    assert i18n.fallback_chain("pt-BR") == ("pt-br", "pt", "en")
    assert i18n.fallback_chain("fr") == ("fr", "en")

    pt = i18n.resolve_locale("pt-BR")
    assert (pt.resolved, pt.fallback_used, pt.chain, pt.landed_on) == (
        "en",
        True,
        ("pt-br", "pt", "en"),
        "en",
    )
    fr = i18n.resolve_locale("fr")
    assert (fr.resolved, fr.fallback_used) == ("fr", False)
    empty = i18n.resolve_locale("")
    # An empty request is not counted as a fallback. negotiate_locale disagrees;
    # that disagreement is a reported finding, not a change.
    assert (empty.resolved, empty.fallback_used) == ("en", False)


def test_locale_payload_key_set_is_untouched():
    assert sorted(i18n.locale_payload("es")) == [
        "direction",
        "display_name",
        "fallback_chain",
        "fallback_locale",
        "fallback_used",
        "requested",
        "resolved",
        "supported_locales",
    ]


def test_translation_interpolation_is_untouched():
    assert i18n.translate("auth.welcome", "es", name="Ana") == "Bienvenido, Ana"
    assert i18n.translate("auth.welcome", "fr", name="Ana") == "Bienvenue, Ana"
    assert i18n.translate("auth.welcome", "en", name="Ana") == "Welcome, Ana"
    assert i18n.translate("does.not.exist", "fr") == "does.not.exist"
    assert i18n.translate("common.welcome", "pt-BR") == "Welcome to Customer Service"


def test_plural_rendering_is_untouched():
    assert i18n.translate("booking.count", "en", count=1) == "1 booking"
    assert i18n.translate("booking.count", "en", count=3) == "3 bookings"
    assert i18n.translate("error.count", "en", count=0) == "no errors"
    assert i18n.translate("error.count", "en", count=2) == "2 errors"
    assert i18n.translate("error.count", "fr", count=5) == "5 erreurs"
    assert i18n.plural_category(1, "en") == "one"
    assert i18n.plural_category(2, "en") == "other"
    # Arabic is declared with six categories and the renderer produces two.
    assert i18n.plural_category(0, "ar") == "other"
    assert i18n.plural_category(1, "ar") == "one"


def test_accept_language_parsing_is_untouched():
    assert i18n.parse_accept_language("fr;q=abc") == [("fr", 1.0)]
    assert i18n.parse_accept_language("*") == []
    assert i18n.parse_accept_language(None) == []
    # Out-of-range q is kept verbatim and reorders the sort.
    assert i18n.parse_accept_language("fr;q=5, es;q=-1") == [("fr", 5.0), ("es", -1.0)]
    # A repeated parameter is last-wins.
    assert i18n.parse_accept_language("fr;q=0.2;q=0.9, en;q=0.5") == [("fr", 0.9), ("en", 0.5)]


def test_catalog_coverage_is_untouched():
    coverage = i18n.catalog_coverage()
    assert coverage["locales"] == ["en", "es", "fr"]
    assert coverage["message_count"] == ORIG_CATALOG_KEYS
    assert coverage["missing_by_locale"] == {"en": [], "es": [], "fr": []}
    assert coverage["complete_locales"] == ["en", "es", "fr"]
    assert coverage["coverage"] == {"en": 1.0, "es": 1.0, "fr": 1.0}
    assert coverage["rtl_locales"] == []


def test_build_i18n_catalog_key_set_is_untouched():
    assert sorted(i18n.build_i18n_catalog()) == [
        "coverage",
        "fallback_locale",
        "locale_detail",
        "locales",
        "message_count",
        "messages",
        "negotiation",
        "overrides",
    ]


def test_rtl_direction_is_never_reachable_and_that_is_unchanged():
    # 'ar' is in RTL_LOCALES but not in the registry, so it resolves to 'en'
    # and the direction is 'ltr'. Reported by locale_registry_audit, not fixed.
    assert i18n.locale_payload("ar")["direction"] == "ltr"
    assert i18n.locale_payload("ar")["resolved"] == "en"


# ==============================================================================
# 2. The reported defects, pinned as defects
# ==============================================================================
#
# Each of these is a live behaviour of the shipped renderer. They are asserted
# rather than fixed: _render_template catches (KeyError, IndexError, ValueError)
# and returns the joined string, so changing what it returns changes a string
# that is already being served to customers.

def test_missing_value_leaks_the_raw_token():
    assert i18n.translate("auth.welcome", "en") == "Welcome, {name}"


def test_plural_without_count_renders_the_zero_branch():
    assert i18n.translate("error.count", "en") == "no errors"
    # ... and is therefore indistinguishable from a real count of zero.
    assert i18n.translate("error.count", "en") == i18n.translate("error.count", "en", count=0)
    # booking.count has no '=0' branch, so it falls to 'other' with a zero.
    assert i18n.translate("booking.count", "en") == "0 bookings"


def test_a_single_stray_brace_disables_interpolation_for_the_whole_template():
    assert i18n._render_template("stray } brace and {name}", "en", {"name": "x"}) == (
        "stray } brace and {name}"
    )
    assert i18n._render_template("Welcome, {name} }", "en", {"name": "x"}) == "Welcome, {name} }"
    # Doubling it is the documented escape and it works.
    assert i18n._render_template("Welcome, {name} {{ok}}", "en", {"name": "x"}) == "Welcome, x {ok}"


def test_dotted_field_escapes_the_except_clause_and_raises():
    # Indexing is covered; a dot is not.
    assert i18n._render_template("Hi {a[9]}", "en", {"a": "v"}) == "Hi {a[9]}"
    with pytest.raises(AttributeError):
        i18n._render_template("Value is {a.b} here", "en", {"a": "v"})
    with pytest.raises(AttributeError):
        i18n._render_template("Value is {a.b.c} here", "en", {"a": "v"})


def test_hash_substitution_rewrites_a_literal_reference_number():
    template = "{n, plural, other {issue #7 resolved with # items}}"
    i18n.OVERRIDES.set("t", "hash", "en", template)
    try:
        assert i18n.translate("hash", "en", scopes=("t",), n=3) == "issue 37 resolved with 3 items"
    finally:
        i18n.OVERRIDES.clear("t")


def test_last_branch_serves_every_unmatched_count_when_other_is_absent():
    i18n.OVERRIDES.set("t", "noother", "en", "{n, plural, one {# item}}")
    try:
        assert i18n.translate("noother", "en", scopes=("t",), n=1) == "1 item"
        # A plural count of 99 rendered in the singular. No error, no diagnostic.
        assert i18n.translate("noother", "en", scopes=("t",), n=99) == "99 item"
    finally:
        i18n.OVERRIDES.clear("t")


def test_unterminated_plural_block_discards_the_tail():
    i18n.OVERRIDES.set("t", "unterm", "en", "{n, plural, one {# item} other {# items")
    try:
        assert i18n.translate("unterm", "en", scopes=("t",), n=1) == "1 item"
    finally:
        i18n.OVERRIDES.clear("t")


def test_non_numeric_count_degrades_to_zero():
    i18n.OVERRIDES.set("t", "frac", "en", "{n, plural, other {# kg}}")
    try:
        assert i18n.translate("frac", "en", scopes=("t",), n=2.5) == "2.5 kg"
        assert i18n.translate("frac", "en", scopes=("t",), n="abc") == "0 kg"
    finally:
        i18n.OVERRIDES.clear("t")


def test_negotiate_reports_the_whole_header_as_requested():
    outcome = i18n.negotiate_locale("fr-CA, es;q=0.8, en;q=0.5")
    assert outcome.requested == "fr-ca, es;q=0.8, en;q=0.5"
    assert outcome.resolved == "fr"


def test_q_zero_is_still_eligible_despite_rfc_9110():
    assert i18n.parse_accept_language("fr;q=0") == [("fr", 0.0)]
    assert i18n.negotiate_locale("fr;q=0").resolved == "fr"


def test_the_two_resolvers_disagree_about_fallback():
    # resolve_locale: "not exactly what was asked for". negotiate_locale: "not
    # the default locale". 'es' is not a fallback by one definition and is one
    # by the other, for the same resolved locale.
    for request in ("es", "fr", ""):
        assert i18n.resolve_locale(request).fallback_used is False
        assert i18n.negotiate_locale(request).fallback_used is True
    # 'pt-BR' is a fallback under both.
    assert i18n.resolve_locale("pt-BR").fallback_used is True
    assert i18n.negotiate_locale("pt-BR").fallback_used is True


def test_chain_means_different_things_per_producer():
    resolved = i18n.resolve_locale("es")
    negotiated = i18n.negotiate_locale("es")
    # resolve_locale: the rungs walked. negotiate_locale: the header's tags.
    assert resolved.chain == ("es", "en")
    assert negotiated.chain == ("es",)


# ==============================================================================
# 3. The six config tables
# ==============================================================================

def test_every_catalog_prefix_has_a_namespace_row():
    for key in i18n.MESSAGE_CATALOG:
        assert i18n._namespace_of(key) in i18n.MESSAGE_NAMESPACES, key


def test_every_namespace_policy_resolves():
    for name, row in i18n.MESSAGE_NAMESPACES.items():
        assert row["placeholder_policy"] in i18n.PLACEHOLDER_POLICIES, name


def test_near_duplicate_references_are_symmetric_and_exist():
    for name, row in i18n.MESSAGE_NAMESPACES.items():
        for other in row["near_duplicates"]:
            assert other in i18n.MESSAGE_NAMESPACES
            assert name in i18n.MESSAGE_NAMESPACES[other]["near_duplicates"]


def test_locale_expectations_point_at_real_profiles():
    for name, row in i18n.LOCALE_EXPECTATIONS.items():
        assert row["format_profile"] in i18n.NUMBER_FORMATS, name
        for alternate in row["alternates"]:
            assert alternate in i18n.NUMBER_FORMATS, (name, alternate)


def test_number_formats_are_internally_coherent():
    for name, row in i18n.NUMBER_FORMATS.items():
        assert row["decimal_separator"] != row["group_separator"], name
        assert row["group_size"] > 0
        assert row["minimum_grouping_digits"] >= 0
        for column in ("percent_position", "currency_position", "negative_sign_position"):
            assert row[column] in ("prefix", "suffix"), (name, column)
        # The one thing the helper will not do, declared rather than implied.
        assert row["digit_substitution"] == "none"


def test_every_render_op_is_report_only_and_names_a_known_code():
    assert i18n.RENDER_OPS
    for name, row in i18n.RENDER_OPS.items():
        assert row["report_only"] is True, name
        assert row["finding"] in i18n.I18N_WARNINGS, name
        assert row["current_behaviour"], name
        assert row["branch"], name
        assert row["classification"] in ("defect", "wart", "info"), name


def test_every_finding_code_names_a_real_producer():
    for code, row in i18n.I18N_WARNINGS.items():
        assert row["emitted_by"], code
        for producer in row["emitted_by"]:
            assert producer in i18n._AUDIT_FUNCTIONS, (code, producer)
        assert row["severity"] in ("defect", "wart", "error", "warning", "info"), code
        assert row["description"], code
        assert row["remediation"], code


def test_unknown_finding_code_degrades_instead_of_raising():
    finding = i18n._finding("I18N_NOT_A_REAL_CODE", "detail")
    assert finding["severity"] == "unknown"
    assert finding["remediation"] == ""
    # And the audits never depend on a code being known.
    assert i18n._finding("I18N_VALUE_NOT_SUPPLIED", "x")["severity"] == "defect"


# ==============================================================================
# 4. validate_i18n
# ==============================================================================

def test_validate_is_clean_on_the_shipped_tables():
    result = i18n.validate_i18n()
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["version"] == i18n.I18N_GOVERNANCE_VERSION
    counts = result["counts"]
    assert counts["namespaces"] == len(i18n.MESSAGE_NAMESPACES)
    assert counts["policies"] == len(i18n.PLACEHOLDER_POLICIES)
    assert counts["profiles"] == len(i18n.NUMBER_FORMATS)
    assert counts["catalog_keys"] == ORIG_CATALOG_KEYS


def test_validate_reports_an_unknown_policy():
    broken = {name: dict(row) for name, row in i18n.MESSAGE_NAMESPACES.items()}
    broken["auth"] = {**broken["auth"], "placeholder_policy": "no_such_policy"}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "MESSAGE_NAMESPACES", broken)
        result = i18n.validate_i18n()
    assert result["ok"] is False
    assert any("no_such_policy" in message for message in result["errors"])


def test_validate_reports_an_unknown_format_profile():
    broken = {name: dict(row) for name, row in i18n.LOCALE_EXPECTATIONS.items()}
    broken["fr"] = {**broken["fr"], "format_profile": "fr_zz", "alternates": ("en_xx",)}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "LOCALE_EXPECTATIONS", broken)
        result = i18n.validate_i18n()
    assert result["ok"] is False
    assert any("fr_zz" in message for message in result["errors"])
    assert any("en_xx" in message for message in result["errors"])


def test_validate_reports_a_render_op_naming_an_unknown_code():
    broken = {name: dict(row) for name, row in i18n.RENDER_OPS.items()}
    broken["invented"] = {
        "op": "_render_template",
        "branch": "x",
        "trigger": "y",
        "current_behaviour": "z",
        "classification": "info",
        "finding": "I18N_MADE_UP",
        "report_only": True,
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "RENDER_OPS", broken)
        result = i18n.validate_i18n()
    assert any("I18N_MADE_UP" in message for message in result["errors"])


def test_validate_rejects_a_render_op_that_is_not_report_only():
    broken = {name: dict(row) for name, row in i18n.RENDER_OPS.items()}
    broken["dictated"] = {**broken["missing_value_leaks_token"], "report_only": False}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "RENDER_OPS", broken)
        result = i18n.validate_i18n()
    assert any("report_only" in message for message in result["errors"])


def test_validate_rejects_a_finding_naming_a_function_that_does_not_exist():
    broken = {code: dict(row) for code, row in i18n.I18N_WARNINGS.items()}
    broken["I18N_PLACEHOLDER_MISMATCH"] = {
        **broken["I18N_PLACEHOLDER_MISMATCH"],
        "emitted_by": ("not_an_audit",),
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "I18N_WARNINGS", broken)
        result = i18n.validate_i18n()
    assert any("not_an_audit" in message for message in result["errors"])


def test_validate_reports_a_malformed_number_format_row():
    broken = {name: dict(row) for name, row in i18n.NUMBER_FORMATS.items()}
    broken["en_us"] = {
        **broken["en_us"],
        "group_size": "three",
        "percent_position": "middle",
        "decimal_separator": ",",
        "group_separator": ",",
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "NUMBER_FORMATS", broken)
        result = i18n.validate_i18n()
    joined = "\n".join(result["errors"])
    assert "group_size" in joined
    assert "percent_position" in joined
    assert "same character" in joined


def test_validate_rejects_a_locale_row_that_contradicts_the_registry():
    broken = {name: dict(row) for name, row in i18n.LOCALE_EXPECTATIONS.items()}
    broken["en"] = {**broken["en"], "in_registry": False}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "LOCALE_EXPECTATIONS", broken)
        result = i18n.validate_i18n()
    assert any("in_registry is false" in message for message in result["errors"])


def test_validate_rejects_a_contradictory_plural_policy():
    broken = {name: dict(row) for name, row in i18n.PLACEHOLDER_POLICIES.items()}
    broken["plural_count"] = {
        **broken["plural_count"],
        "count_is_plural_variable": True,
        "require_values": True,
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "PLACEHOLDER_POLICIES", broken)
        result = i18n.validate_i18n()
    assert any("both be optional and required" in message for message in result["errors"])


@pytest.mark.parametrize(
    "junk",
    [None, [], "text", 42, set()],
    ids=["none", "list", "str", "int", "set"],
)
def test_validate_never_raises_on_a_non_mapping_table(junk):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "MESSAGE_NAMESPACES", junk)
        result = i18n.validate_i18n()
    assert result["ok"] is False
    assert isinstance(result["errors"], list)


def test_validate_never_raises_on_a_malformed_catalog():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "MESSAGE_CATALOG", {"broken": "not a mapping", 7: None})
        result = i18n.validate_i18n()
    assert isinstance(result["errors"], list)
    assert isinstance(result["warnings"], list)


def test_validate_records_the_dead_pattern_as_info():
    result = i18n.validate_i18n()
    assert any("_PLACEHOLDER" in message for message in result["info"])
    assert "I18N_UNUSABLE_PATTERN" in result["coded"]


# ==============================================================================
# 5. message_placeholder_audit
# ==============================================================================

def test_placeholder_audit_reports_the_shipped_leak():
    report = i18n.message_placeholder_audit()
    assert report["keys_with_leaks"] == ["auth.welcome"]
    leaks = [
        item for item in report["findings"] if item["code"] == "I18N_VALUE_NOT_SUPPLIED"
    ]
    assert len(leaks) == 3
    assert {item["locale"] for item in leaks} == {"en", "es", "fr"}
    assert all(item["token"] == "name" for item in leaks)
    assert all(item["severity"] == "defect" for item in leaks)
    assert report["keys_that_can_raise"] == []


def test_placeholder_audit_confirms_the_shipped_catalog_is_aligned():
    # Every locale of every shipped key declares the same placeholders. The
    # check exists so the first key that does not agree is caught at review.
    report = i18n.message_placeholder_audit()
    assert report["misaligned_keys"] == []
    assert report["by_key"]["auth.welcome"]["placeholders_aligned"] is True
    assert report["by_key"]["auth.welcome"]["placeholder_signature"] == {
        "en": ["name"],
        "es": ["name"],
        "fr": ["name"],
    }


def test_placeholder_audit_detects_cross_locale_divergence():
    catalog = {
        "auth.welcome": {
            "en": "Welcome, {name}",
            "es": "Bienvenido, {name}",
            "fr": "Bienvenue, {nombre}",
        }
    }
    report = i18n.message_placeholder_audit(catalog)
    assert report["misaligned_keys"] == ["auth.welcome"]
    codes = {item["code"] for item in report["findings"]}
    assert "I18N_PLACEHOLDER_MISMATCH" in codes
    # '{nombre}' is a legal \w+ name, so this is a divergence and not a
    # malformed token: it is reported as an unfilled value in all three locales.
    assert "I18N_PLACEHOLDER_NAME_INVALID" not in codes
    assert report["by_key"]["auth.welcome"]["placeholder_signature"] == {
        "en": ["name"],
        "es": ["name"],
        "fr": ["nombre"],
    }


def test_placeholder_audit_detects_a_dotted_field():
    report = i18n.message_placeholder_audit({"auth.dot": {"en": "Value is {a.b} here"}})
    assert report["keys_that_can_raise"] == ["auth.dot"]
    raising = [item for item in report["findings"] if item["code"] == "I18N_RENDER_RAISES"]
    assert len(raising) == 1
    # The demonstration has to supply the field's root, or the renderer raises
    # KeyError and degrades -- the probe would then measure the wrong path.
    assert raising[0]["field"] == "a"
    assert raising[0]["raised"] == "AttributeError"
    assert raising[0]["severity"] == "defect"
    # Indexing is caught by the renderer, so it is not a raise.
    safe = i18n.message_placeholder_audit({"auth.idx": {"en": "Value is {a[0]} here"}})
    assert safe["keys_that_can_raise"] == []


def test_placeholder_audit_detects_a_token_the_renderer_cannot_name():
    # '_PLACEHOLDER' is \w+ only, so '{na-me}' survives rendering untouched and
    # is invisible to a placeholder-set comparison. Found in the output.
    report = i18n.message_placeholder_audit({"auth.weird": {"en": "Hi {na-me}"}})
    assert report["keys_with_unfillable_tokens"] == ["auth.weird"]
    codes = {item["code"] for item in report["findings"]}
    assert "I18N_PLACEHOLDER_NAME_INVALID" in codes
    assert "I18N_VALUE_NOT_SUPPLIED" not in codes


def test_placeholder_audit_detects_an_unexpected_placeholder():
    # 'common' is a none_expected namespace.
    report = i18n.message_placeholder_audit({"common.token": {"en": "Hi {name}"}})
    codes = {item["code"] for item in report["findings"]}
    assert "I18N_PLACEHOLDER_UNEXPECTED" in codes


def test_placeholder_audit_detects_a_placeholder_limit_breach():
    catalog = {
        "booking.count": {
            "en": "{a} {b} {c} of {count, plural, other {# things}}",
        }
    }
    codes = {item["code"] for item in i18n.message_placeholder_audit(catalog)["findings"]}
    assert "I18N_PLACEHOLDER_LIMIT" in codes


def test_placeholder_audit_detects_an_unknown_namespace():
    report = i18n.message_placeholder_audit({"nodot": {"en": "no namespace row"}})
    codes = {item["code"] for item in report["findings"]}
    assert "I18N_NAMESPACE_UNKNOWN" in codes


def test_placeholder_audit_detects_an_empty_template():
    report = i18n.message_placeholder_audit({"common.blank": {"en": "   "}})
    codes = {item["code"] for item in report["findings"]}
    assert "I18N_TEMPLATE_EMPTY" in codes


@pytest.mark.parametrize("junk", [None, [], "text", 42, {1: {2: 3}}])
def test_placeholder_audit_tolerates_a_malformed_catalog(junk):
    report = i18n.message_placeholder_audit(junk)
    assert isinstance(report["findings"], list)
    assert isinstance(report["by_key"], dict)
    assert report["clean"] is (not report["findings"])


def test_template_scan_tolerates_non_string_input():
    for junk in (None, 12345, [], {}):
        scan = i18n._template_scan(junk)
        assert scan["plain"] == []
        assert scan["plural"] == []
        assert scan["characters"] == 0
        assert scan["raising_fields"] == []


# ==============================================================================
# 6. message_budget_report
# ==============================================================================

def test_budget_report_measures_the_shipped_catalog():
    report = i18n.message_budget_report()
    assert report["overall"]["keys"] == ORIG_CATALOG_KEYS
    assert report["overall"]["templates"] == ORIG_TEMPLATE_COUNT
    # The longest shipped template is error.count[fr] at 68 characters, inside
    # every namespace budget, which is why this report is clean.
    assert report["overall"]["max_characters"] == 68
    assert report["longest"][0]["key"] == "error.count"
    assert report["longest"][0]["locale"] == "fr"
    assert report["clean"] is True
    assert report["findings"] == []


def test_budget_report_flags_a_namespace_over_budget():
    catalog = {"auth.long": {"en": "W" * 100, "es": "B" * 95, "fr": "C" * 20}}
    report = i18n.message_budget_report(catalog)
    over = report["namespaces"]["auth"]["over_budget"]
    assert [item["over_by"] for item in over] == [20, 15]
    assert report["namespaces"]["auth"]["min_characters"] == 20
    assert report["namespaces"]["auth"]["max_characters_observed"] == 100
    assert all(item["code"] == "I18N_BUDGET_EXCEEDED" for item in report["findings"])


def test_budget_report_flags_a_key_no_namespace_governs():
    # No namespace means no budget, so the key still has to be counted in the
    # overall numbers and reported as ungoverned rather than silently skipped.
    report = i18n.message_budget_report({"orphan.key": {"en": "W" * 500}})
    assert report["unmapped_namespaces"] == {"orphan": ["orphan.key"]}
    assert report["overall"]["max_characters"] == 500
    assert {item["code"] for item in report["findings"]} == {"I18N_NAMESPACE_UNKNOWN"}


# ==============================================================================
# 7. unbalanced_template_report
# ==============================================================================

def test_unbalanced_report_is_clean_on_the_shipped_catalog():
    report = i18n.unbalanced_template_report()
    assert report["template_count"] == ORIG_TEMPLATE_COUNT
    assert report["unbalanced"] == []
    assert report["unterminated"] == []
    # The leaks are real but they are the placeholder audit's finding; this
    # report carries them too because the two detectors are independent.
    assert report["findings"], "the shipped catalog leaks {name} in three locales"
    for item in report["findings"]:
        assert {"code", "detail", "severity", "description", "remediation"} <= set(item)


def test_unbalanced_report_flags_a_stray_brace_with_its_consequence():
    report = i18n.unbalanced_template_report({"auth.t": {"en": "Welcome, {name} }"}})
    assert report["unbalanced"] == ["auth.t|en"]
    finding = next(
        item for item in report["findings"] if item["code"] == "I18N_BRACE_UNBALANCED"
    )
    assert finding["stray_close"] == 1
    # The consequence is in the evidence: the token leaks too.
    assert "{name}" in finding["evidence"]
    codes = {item["code"] for item in report["findings"]}
    assert "I18N_VALUE_NOT_SUPPLIED" in codes


def test_unbalanced_report_flags_an_unterminated_plural_block():
    report = i18n.unbalanced_template_report(
        {"booking.count": {"en": "{count, plural, one {# item} other {# items"}}
    )
    assert report["unterminated"] == ["booking.count|en"]
    assert any(item["code"] == "I18N_PLURAL_UNTERMINATED" for item in report["findings"])


def test_unbalanced_report_flags_an_unclosed_placeholder():
    report = i18n.unbalanced_template_report({"auth.t": {"en": "Welcome, {name"}})
    codes = {item["code"] for item in report["findings"]}
    assert "I18N_BRACE_UNBALANCED" in codes


# ==============================================================================
# 8. plural_audit
# ==============================================================================

def test_plural_audit_probes_the_reachable_categories():
    report = i18n.plural_audit()
    english = report["locale_rules"]["en"]
    assert english["declared"] == ["one", "other"]
    assert english["reachable"] == ["other", "one"]
    assert english["unreachable_declared"] == []
    # The reachable set is probed, not asserted, so it follows the code.
    assert set(english["probe_counts"]) == set(i18n._PROBE_COUNTS)


def test_plural_audit_reports_the_unreachable_arabic_categories():
    report = i18n.plural_audit()
    arabic = report["locale_rules"]["ar"]
    assert len(arabic["declared"]) == 6
    assert sorted(arabic["unreachable_declared"]) == ["few", "many", "two", "zero"]
    assert arabic["reachable"] == ["other", "one"]
    assert arabic["in_registry"] is False
    finding = next(
        item
        for item in report["findings"]
        if item["code"] == "I18N_PLURAL_SELECTOR_UNREACHABLE" and item["locale"] == "ar"
    )
    assert finding["severity"] == "warning"


def test_plural_audit_reports_the_shipped_zero_branch_call_site():
    report = i18n.plural_audit()
    probe = report["call_site_probe"]["error.count"]
    assert probe["no_count"] == "no errors"
    assert probe["count_0"] == "no errors"
    assert probe["no_count_equals_zero"] is True
    assert probe["count_1"] == "1 error"
    assert report["plural_keys"] == ["booking.count", "error.count"]
    absent = [item for item in report["findings"] if item["code"] == "I18N_PLURAL_COUNT_ABSENT"]
    assert sorted(item["key"] for item in absent) == ["booking.count", "error.count"]


def test_plural_audit_confirms_the_shipped_blocks_are_well_formed():
    report = i18n.plural_audit()
    for block in report["blocks"].values():
        assert block["has_other"] is True
        assert block["unreachable_selectors"] == []
        assert block["undeclared_selectors"] == []
        assert block["unterminated"] is False
    codes = {item["code"] for item in report["findings"]}
    assert "I18N_PLURAL_NO_OTHER" not in codes
    assert "I18N_PLURAL_CROSS_LOCALE_DIVERGENCE" not in codes
    assert "I18N_PLURAL_BARE_NUMBER" not in codes


def test_plural_audit_detects_a_missing_other_branch():
    report = i18n.plural_audit(
        {"booking.count": {"en": "{count, plural, one {# booking}}"}}
    )
    assert any(item["code"] == "I18N_PLURAL_NO_OTHER" for item in report["findings"])


def test_plural_audit_detects_a_bare_hash_followed_by_a_digit():
    report = i18n.plural_audit(
        {"booking.count": {"en": "issue #7: {count, plural, other {# items}}"}}
    )
    findings = [item for item in report["findings"] if item["code"] == "I18N_PLURAL_BARE_NUMBER"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "defect"


def test_plural_audit_detects_cross_locale_selector_divergence():
    report = i18n.plural_audit(
        {
            "booking.count": {
                "en": "{count, plural, one {# booking} other {# bookings}}",
                "es": "{count, plural, one {# reserva} other {# reservas} few {# poquitas}}",
            }
        }
    )
    codes = {item["code"] for item in report["findings"]}
    assert "I18N_PLURAL_CROSS_LOCALE_DIVERGENCE" in codes
    assert "I18N_PLURAL_SELECTOR_UNDECLARED" in codes
    assert "I18N_PLURAL_SELECTOR_UNREACHABLE" in codes


def test_plural_audit_does_not_propagate_a_renderer_escape():
    # translate() raises on a dotted field, and plural_audit calls translate().
    # The audit must capture that, not become it.
    report = i18n.plural_audit(
        {
            "booking.count": {
                "en": "{count, plural, other {# of {a.b}}}",
                "es": "{count, plural, other {# cosas}}",
            }
        }
    )
    assert report["call_site_probe"]["booking.count"]["probe_failed"] is True
    assert any(item["code"] == "I18N_RENDER_RAISES" for item in report["findings"])


# ==============================================================================
# 9. negotiation_audit
# ==============================================================================

def test_negotiation_audit_records_both_fallback_definitions():
    report = i18n.negotiation_audit()
    assert report["resolvers"] == ["resolve_locale", "negotiate_locale"]
    assert "exactly the normalized request" in report["fallback_definitions"]["resolve_locale"]
    assert "the default locale" in report["fallback_definitions"]["negotiate_locale"]


def test_negotiation_audit_names_the_diverging_requests():
    report = i18n.negotiation_audit()
    # '' diverges too: negotiate_locale normalizes an empty header to '' , finds
    # no supported tag, and takes the fallback branch, which always sets the
    # flag -- while resolve_locale treats an empty request as "nothing asked".
    assert report["diverging_requests"] == ["", "es", "fr"]
    # 'en' and 'pt-BR' agree, so they are not listed.
    assert "en" not in report["diverging_requests"]
    assert "pt-BR" not in report["diverging_requests"]
    for row in report["comparison"]:
        if row["request"] in ("", "es", "fr"):
            assert row["same_resolution"] is True
            assert row["same_fallback_flag"] is False
        if row["request"] == "pt-BR":
            assert row["same_fallback_flag"] is True


def test_negotiation_audit_probes_the_header_path():
    report = i18n.negotiation_audit()
    by_header = {row["header"]: row for row in report["header_probe"]}

    zero = by_header["fr;q=0"]
    assert zero["eligible_zero_q"] == ["fr"]
    assert zero["resolved"] == "fr"

    out_of_range = by_header["fr;q=5, es;q=-1"]
    assert out_of_range["out_of_range_q"] == [
        {"tag": "fr", "q": 5.0},
        {"tag": "es", "q": -1.0},
    ]

    repeated = by_header["fr;q=0.2;q=0.9, en;q=0.5"]
    assert repeated["repeated_q"] is True

    # '*' parses to nothing and is not reported as a whole-header tag.
    assert by_header["*"]["parsed"] == []

    codes = {item["code"] for item in report["findings"]}
    assert {
        "I18N_ACCEPT_LANGUAGE_Q_ZERO",
        "I18N_ACCEPT_LANGUAGE_Q_UNCLAMPED",
        "I18N_ACCEPT_LANGUAGE_DUPLICATE_Q",
        "I18N_NEGOTIATION_REQUESTED_NOT_A_TAG",
        "I18N_NEGOTIATION_CHAIN_SEMANTICS",
        "I18N_NEGOTIATION_FALLBACK_DIVERGENCE",
    } <= codes


def test_negotiation_audit_reports_the_discarded_region():
    report = i18n.negotiation_audit()
    for row in report["regional_probe"]:
        # 'resolved' is what translate() and locale_payload() key the catalog
        # on, and it is always a bare language.
        assert row["resolved_is_bare_language"] is True
        assert "-" not in row["resolved"]
    by_request = {row["request"]: row for row in report["regional_probe"]}
    # 'landed_on' is a different field and it *does* keep the region, which is
    # exactly why the two are reported separately.
    assert by_request["es-ES"]["landed_on"] == "es-es"
    assert by_request["es-ES"]["landed_on_keeps_region"] is True
    assert by_request["pt-BR"]["landed_on_keeps_region"] is False
    assert any(item["code"] == "I18N_NEGOTIATION_REGION_DISCARDED" for item in report["findings"])


# ==============================================================================
# 10. locale_registry_audit
# ==============================================================================

def test_registry_audit_reports_the_unreachable_rtl_and_plural_locales():
    report = i18n.locale_registry_audit()
    assert report["rtl_unreachable"] == ["ar", "fa", "he", "ur"]
    assert report["plural_unreachable"] == ["ar"]
    assert report["direction_by_locale"] == {"en": "ltr", "es": "ltr", "fr": "ltr"}
    findings = [item for item in report["findings"] if item["code"] == "I18N_LOCALE_NOT_IN_REGISTRY"]
    assert len(findings) == 6
    assert {item["source"] for item in findings} == {
        "RTL_LOCALES",
        "PLURAL_CATEGORIES",
        "LOCALE_EXPECTATIONS",
    }


def test_registry_audit_counts_templates_per_shipped_locale():
    # Counting these at the top level of MESSAGE_CATALOG would find nothing,
    # because MESSAGE_CATALOG is {key: {locale: template}}.
    report = i18n.locale_registry_audit()
    assert report["templates_by_locale"] == {"en": 15, "es": 15, "fr": 15}
    assert not any(
        item["code"] == "I18N_LOCALE_NO_TEMPLATES" for item in report["findings"]
    )


def test_registry_audit_reports_the_unused_fallback_key():
    report = i18n.locale_registry_audit()
    rows = {row["locale"]: row for row in report["fallback_rows"]}
    assert all(row["declared_fallback"] == "en" for row in rows.values())
    # All three agree with DEFAULT_LOCALE today, so nothing fires. Change one and
    # the finding appears, proving the check is live rather than vacuous.
    patched = {
        "en": {"default": "English", "fallback": "en"},
        "es": {"default": "Español", "fallback": "fr"},
        "fr": {"default": "Français", "fallback": "en"},
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "SUPPORTED_LOCALES", patched)
        changed = i18n.locale_registry_audit()
    unused = [item for item in changed["findings"] if item["code"] == "I18N_LOCALE_FALLBACK_UNUSED"]
    assert len(unused) == 1
    assert unused[0]["locale"] == "es"
    # ... and the behaviour does not follow the declared value.
    assert i18n.resolve_locale("es").fallback_used is False


def test_registry_audit_reports_a_fallback_naming_an_unshipped_locale():
    patched = {
        "en": {"default": "English", "fallback": "en"},
        "es": {"default": "Español", "fallback": "xx"},
        "fr": {"default": "Français", "fallback": "en"},
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "SUPPORTED_LOCALES", patched)
        report = i18n.locale_registry_audit()
    assert any(item["code"] == "I18N_LOCALE_FALLBACK_NOT_SHIPPED" for item in report["findings"])


def test_registry_audit_reports_a_shipped_locale_with_no_templates():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "MESSAGE_CATALOG", {"health.ok": {"en": "ok"}})
        report = i18n.locale_registry_audit()
    assert report["templates_by_locale"] == {"en": 1, "es": 0, "fr": 0}
    findings = [item for item in report["findings"] if item["code"] == "I18N_LOCALE_NO_TEMPLATES"]
    assert sorted(item["locale"] for item in findings) == ["es", "fr"]


def test_registry_audit_reports_a_shipped_locale_with_no_expectations_row():
    reduced = {"en": dict(i18n.LOCALE_EXPECTATIONS["en"])}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "LOCALE_EXPECTATIONS", reduced)
        report = i18n.locale_registry_audit()
    findings = [item for item in report["findings"] if item["code"] == "I18N_LOCALE_NO_EXPECTATIONS"]
    assert sorted(item["locale"] for item in findings) == ["es", "fr"]


def test_registry_audit_reports_the_region_ambiguous_locales():
    report = i18n.locale_registry_audit()
    assert report["region_ambiguous"] == ["ar", "en", "es"]
    findings = [item for item in report["findings"] if item["code"] == "I18N_LOCALE_REGION_AMBIGUOUS"]
    # Only the shipped ones are actionable; 'ar' is already not in the registry.
    assert sorted(item["locale"] for item in findings) == ["en", "es"]


def test_registry_audit_reports_override_keys_coverage_cannot_see():
    i18n.OVERRIDES.set_catalog("tenant", {"tenant.only": {"en": "hi", "es": "hola"}})
    try:
        report = i18n.locale_registry_audit()
        assert report["override_keys_outside_catalog"] == {"tenant.only": ["tenant"]}
        finding = next(
            item
            for item in report["findings"]
            if item["code"] == "I18N_OVERRIDE_OUTSIDE_CATALOG"
        )
        assert finding["key"] == "tenant.only"
        # The override really is served ...
        assert i18n.translate("tenant.only", "es", scopes=("tenant",)) == "hola"
        # ... and coverage really cannot see it.
        coverage = i18n.catalog_coverage()
        assert coverage["message_count"] == ORIG_CATALOG_KEYS
        assert "tenant.only" not in coverage["missing_by_locale"]["es"]
    finally:
        i18n.OVERRIDES.clear("tenant")


def test_registry_audit_is_quiet_when_no_overrides_exist():
    i18n.OVERRIDES.clear()
    report = i18n.locale_registry_audit()
    assert report["override_keys_outside_catalog"] == {}
    assert not any(
        item["code"] == "I18N_OVERRIDE_OUTSIDE_CATALOG" for item in report["findings"]
    )


def test_keys_by_scope_is_a_read_only_view():
    i18n.OVERRIDES.set("b", "z.one", "en", "1")
    i18n.OVERRIDES.set("a", "y.one", "en", "1")
    try:
        assert i18n.OVERRIDES.keys_by_scope() == {"a": ["y.one"], "b": ["z.one"]}
    finally:
        i18n.OVERRIDES.clear()
    assert i18n.OVERRIDES.keys_by_scope() == {}


# ==============================================================================
# 11. format_number
# ==============================================================================

def test_format_number_uses_the_locale_profile():
    assert i18n.format_number(1234567.5, "en", profile="en_us") == "1,234,567.5"
    assert i18n.format_number(1234567.5, "es", profile="es_es") == "1.234.567,5"
    assert i18n.format_number(1234567.5, "es", profile="es_419") == "1,234,567.5"
    assert i18n.format_number(1234567.5, "fr", profile="fr_fr") == "1\u202f234\u202f567,5"
    assert i18n.format_number(1234567.5, "de", profile="de_de") == "1.234.567,5"


def test_format_number_resolves_the_profile_from_the_language():
    # es -> es_ES by default, which is the region-ambiguity finding made visible.
    assert i18n.format_number(1234.5, "es") == "1.234,5"
    assert i18n.format_number(1234.5, "es-419") == "1.234,5"
    assert i18n.format_number(1234.5, "en") == "1,234.5"
    assert i18n.format_number(1234.5, "ar") == "1,234.5"


def test_format_number_falls_back_to_the_default_profile():
    assert i18n.format_number(1234.5, "zz") == "1,234.5"
    assert i18n.format_number(1234.5, None) == "1,234.5"
    assert i18n.format_number(1234.5) == "1,234.5"
    assert i18n.DEFAULT_FORMAT_PROFILE == "en_us"


def test_format_number_respects_minimum_grouping_digits():
    spec = {**i18n.NUMBER_FORMATS["en_us"], "minimum_grouping_digits": 2}
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(i18n.NUMBER_FORMATS, "en_us", spec)
        # 4 digits: needs group_size 3 + minimum 2, so no grouping.
        assert i18n.format_number(1234, "en", profile="en_us") == "1234"
        assert i18n.format_number(1234567, "en", profile="en_us") == "1,234,567"


def test_format_number_does_not_group_below_the_threshold():
    assert i18n.format_number(999, "en", profile="en_us") == "999"
    assert i18n.format_number(1000, "en", profile="en_us") == "1,000"
    assert i18n.format_number(0, "en", profile="en_us") == "0"
    assert i18n.format_number(0.5, "en", profile="en_us") == "0.5"


def test_format_number_kinds():
    assert i18n.format_number(0.256, "de", kind="percent", profile="de_de") == "0,256%"
    # es_ES places the symbol after a non-breaking space, per the table's
    # currency_separator column.
    assert i18n.format_number(1234.5, "es", kind="currency", profile="es_es") == "1.234,50\u00a0\u20ac"
    assert i18n.format_number(1234.5, "es", kind="currency", profile="es_419") == "$1,234.50"
    assert i18n.format_number(1234.5, "en", kind="currency", profile="en_us") == "$1,234.50"
    # An unknown kind behaves as a plain number rather than raising.
    assert i18n.format_number(1234.5, "en", kind="bananas") == "1,234.5"


def test_format_number_negatives():
    assert i18n.format_number(-1234.5, "fr", profile="fr_fr") == "-1\u202f234,5"
    assert i18n.format_number(-1234.5, "en", profile="en_us") == "-1,234.5"


def test_format_number_decimals():
    assert i18n.format_number(1.5, "en", decimals=0) == "2"
    assert i18n.format_number(1.5, "en", decimals=3) == "1.500"
    assert i18n.format_number(1.23456, "en") == "1.23456"
    # Currency defaults to two places; a plain number does not.
    assert i18n.format_number(1.5, "en", kind="currency") == "$1.50"
    assert i18n.format_number(1.5, "en") == "1.5"


@pytest.mark.parametrize("junk", ["abc", None, object(), [1, 2]])
def test_format_number_degrades_rather_than_raising(junk):
    assert i18n.format_number(junk, "en") in (str(junk), "[]")


def test_format_number_is_not_wired_into_translate():
    # The plural '#' still renders as str(int(n)) and is not grouped.
    assert i18n.translate("booking.count", "en", count=12345) == "12345 bookings"
    i18n.OVERRIDES.set("t", "big", "en", "{n, plural, other {# items}}")
    try:
        assert i18n.translate("big", "en", scopes=("t",), n=12345) == "12345 items"
    finally:
        i18n.OVERRIDES.clear("t")


# ==============================================================================
# 12. build_i18n_governance_catalog
# ==============================================================================

def test_governance_catalog_shape():
    catalog = i18n.build_i18n_governance_catalog()
    assert catalog["version"] == i18n.I18N_GOVERNANCE_VERSION
    assert sorted(catalog) == [
        "audits",
        "blocking_codes",
        "clean_catalogs",
        "finding_count",
        "findings",
        "findings_taxonomy",
        "locale_expectations",
        "namespaces",
        "number_formats",
        "number_formatting",
        "placeholder_policies",
        "policy",
        "render_ops",
        "severity_counts",
        "unreached_codes",
        "validation",
        "version",
    ]
    # The six behaviour audits, plus the validator which returns a verdict rather
    # than a findings list and so is not in the audits map.
    assert set(catalog["audits"]) == set(i18n._AUDIT_FUNCTIONS) - {"validate_i18n"}


def test_governance_catalog_policy_block_states_the_posture():
    policy = i18n.build_i18n_governance_catalog()["policy"]
    assert policy["posture"] == "report, never repair"
    for table in ("SUPPORTED_LOCALES", "MESSAGE_CATALOG", "_render_template"):
        assert table in policy["shipped_unchanged"]


def test_governance_catalog_surfaces_the_shipped_defects():
    catalog = i18n.build_i18n_governance_catalog()
    codes = {item["code"] for item in catalog["findings"]}
    assert {
        "I18N_VALUE_NOT_SUPPLIED",
        "I18N_PLURAL_COUNT_ABSENT",
        "I18N_LOCALE_NOT_IN_REGISTRY",
        "I18N_NEGOTIATION_FALLBACK_DIVERGENCE",
        "I18N_NEGOTIATION_REQUESTED_NOT_A_TAG",
        "I18N_ACCEPT_LANGUAGE_Q_ZERO",
    } <= codes
    assert catalog["severity_counts"]["defect"] >= 10
    assert "I18N_VALUE_NOT_SUPPLIED" in catalog["blocking_codes"]
    assert catalog["validation"]["ok"] is True


def test_every_finding_carries_its_taxonomy_row():
    catalog = i18n.build_i18n_governance_catalog()
    assert catalog["findings"]
    for item in catalog["findings"]:
        assert item["code"] in i18n.I18N_WARNINGS
        assert item["description"]
        assert item["remediation"]
        assert item["detail"]
        assert item["severity"] in ("defect", "wart", "error", "warning", "info")


def test_unreached_codes_are_guards_not_claims_of_health():
    catalog = i18n.build_i18n_governance_catalog()
    unreached = set(catalog["unreached_codes"])
    # These fire only on a malformed configuration; the shipped tables are clean.
    assert "I18N_PLACEHOLDER_MISMATCH" in unreached
    assert "I18N_BUDGET_EXCEEDED" in unreached
    assert "I18N_PLACEHOLDER_MISMATCH" in {
        item["code"] for item in i18n.message_placeholder_audit(
            {"auth.welcome": {"en": "{name}", "es": "{nombre}"}}
        )["findings"]
    }
    assert set(catalog["unreached_codes"]) <= set(i18n.I18N_WARNINGS)


def test_catalog_is_deterministic():
    first = i18n.build_i18n_governance_catalog()
    second = i18n.build_i18n_governance_catalog()
    assert first == second


# ==============================================================================
# 13. The /meta surfaces
# ==============================================================================

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_meta_i18n_exposes_the_governance_block(client):
    response = client.get("/meta/i18n")
    assert response.status_code == 200
    payload = response.json()
    assert sorted(payload) == [
        "catalog",
        "environment",
        "governance",
        "name",
        "resolution",
        "version",
    ]
    governance = payload["governance"]
    assert governance["version"] == i18n.I18N_GOVERNANCE_VERSION
    assert governance["validation"]["ok"] is True
    assert governance["finding_count"] > 0
    assert len(governance["namespaces"]) == len(i18n.MESSAGE_NAMESPACES)
    # The pre-existing keys are untouched.
    assert payload["catalog"]["locales"] == ["en", "es", "fr"]
    assert payload["resolution"]["resolved"] == "en"


def test_meta_i18n_governance_is_locale_independent(client):
    english = client.get("/meta/i18n").json()
    spanish = client.get("/meta/i18n?locale=es").json()
    assert spanish["resolution"]["resolved"] == "es"
    assert spanish["governance"] == english["governance"]


def test_meta_scoring_catalog_exposes_i18n_governance(client):
    payload = client.get("/meta/scoring-catalog").json()
    assert payload["i18n_governance"]["version"] == i18n.I18N_GOVERNANCE_VERSION
    # The pre-existing key is still there and still its own thing.
    assert payload["i18n"]["locales"] == ["en", "es", "fr"]
    assert sorted(payload["i18n"]) == sorted(i18n.build_i18n_catalog())


def test_meta_features_maps_the_governance_surface(client):
    endpoints = client.get("/meta/features").json()["endpoints"]
    assert endpoints["i18n_governance"] == "/meta/i18n"
    assert endpoints["i18n_catalog"] == "/meta/i18n"


def test_meta_lists_the_governance_feature(client):
    assert "i18n_governance" in client.get("/meta").json()["features"]


def test_ecosystem_localization_declares_its_tables(client):
    localization = client.get("/meta/ecosystem").json()["subservices"]["localization"]
    assert localization["config_tables"] == [
        "MESSAGE_NAMESPACES",
        "PLACEHOLDER_POLICIES",
        "LOCALE_EXPECTATIONS",
        "NUMBER_FORMATS",
        "RENDER_OPS",
        "I18N_WARNINGS",
    ]
    assert localization["routes"] == ["/meta/i18n", "/meta/scoring-catalog"]
    assert "report_only" in localization["notes"]
    # The note must not drift from the code: every op really is report_only.
    assert all(row["report_only"] is True for row in i18n.RENDER_OPS.values())


def test_the_other_meta_surfaces_still_answer(client):
    for path in ("/meta", "/meta/features", "/meta/scoring-catalog", "/meta/i18n", "/meta/ecosystem"):
        assert client.get(path).status_code == 200, path
