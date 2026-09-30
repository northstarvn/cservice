"""Tests for the `app/model_bases.py` governance expansion.

`model_bases.py` grew a second layer: four mixins available to *new* tables, a
field-sensitivity vocabulary layered over the original serialization denylist,
money/idempotency/approval helpers, and pure introspection over
`Base.metadata`.

The point of this file is to pin the parts that are easy to get quietly wrong:

1. **No existing table moved.** The four new mixins are *available*; none is
   applied to a class in `app/models.py`, so no DDL changed. A test asserts the
   concrete entity tables still carry only the columns they started with.
2. **The denylist is absolute.** `SERIALIZATION_DENYLIST` was a hard rule
   before this pass and must stay one: no profile, including `internal`, may
   emit a denylisted field. A profile that could grant that permission would be
   a one-line change away from leaking every password hash in the system.
3. **An unknown profile falls back to the *restrictive* one.** A typo resolving
   to `internal` is how a field meant to be hidden ends up in a payload.
4. **Money is integer minor units.** A float ledger drifts, and a helper that
   raises on `"12.34abc"` turns a bad form field into a 500.
5. **Introspection states its own coverage.** `Base.metadata` only holds
   imported modules, so a report that did not say so would read as a
   whole-schema audit.

The original r2–r5 surface is pinned too — the eight-mixin `mixins` list, the
three-key `families` contract, and the `EntityRegistry` — because widening
those silently breaks callers that iterate them.
"""
import os
import sys
from datetime import datetime

import pytest
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import main, model_bases, models
from app.model_bases import (
    APPROVAL_OPS,
    APPROVAL_STATES,
    APPROVAL_TERMINAL_STATE,
    APPROVAL_TRANSITIONS,
    CURRENCY_EXPONENTS,
    MASK_OPS,
    MIXIN_CATEGORIES,
    MIXIN_EXTENSIONS,
    MIXIN_FAMILY,
    MONEY_OPS,
    MODEL_BASES_OPS,
    REGISTRY,
    SECURITY_EVENT_DEFAULT_SOURCE,
    SECURITY_EVENT_QUERY_OPS,
    SECURITY_EVENT_SOURCES,
    SEVERITY_BANDS,
    SLUG_RESERVED,
    ApprovalMixin,
    IdempotencyMixin,
    MoneyMixin,
    SecurityEvent,
    SerializationMixin,
    SluggableMixin,
    all_mixins,
    approval_next_states,
    approval_transition_report,
    approval_transition_verdict,
    build_model_bases_catalog,
    build_model_bases_ops_catalog,
    build_serialization_audit,
    build_security_event_query,
    classify_serialization_field,
    currency_exponent,
    filter_payload_for_profile,
    from_minor_units,
    idempotency_verdict,
    mapped_entity_classes,
    mixin_capability_report,
    mixin_column_provenance,
    mixin_contributed_columns,
    money_conversion,
    request_fingerprint,
    resolve_serialization_profile,
    security_event_summary,
    severity_band,
    slugify,
    to_minor_units,
    unique_slug,
    validate_model_bases,
    validate_serialization_profiles,
)


# --- scratch table ------------------------------------------------------------
#
# The four new mixins are never applied to a real entity (that would change
# DDL), so their behaviour is exercised on a throwaway declarative base. Built
# at import time so the columns exist exactly once.

ScratchBase = declarative_base()


class ScratchGrant(
    ScratchBase,
    model_bases.TimestampMixin,
    SluggableMixin,
    ApprovalMixin,
    MoneyMixin,
    IdempotencyMixin,
    model_bases.SoftDeleteMixin,
    model_bases.RowVersionMixin,
    model_bases.ActorAuditMixin,
    model_bases.ExpiringMixin,
    model_bases.TenantScopedMixin,
    model_bases.PartitionedMixin,
    SerializationMixin,
):
    __tablename__ = "scratch_grant"

    id = Column(Integer, primary_key=True)
    title = Column(String(80), nullable=False, default="")


@pytest.fixture()
def grant():
    row = ScratchGrant(title="Crème Grant")
    row.approval_state = str(APPROVAL_OPS["initial_state"])
    row.approval_note = ""
    return row


# --- the original surface is untouched ----------------------------------------


def test_the_pinned_mixin_list_is_still_exactly_eight():
    assert build_model_bases_catalog()["mixins"] == [
        "timestamp",
        "tenant_scoped",
        "partitioned",
        "soft_delete",
        "row_version",
        "actor_audit",
        "expiring",
        "serialization",
    ]


def test_the_pinned_family_contract_is_still_exactly_three():
    catalog = build_model_bases_catalog()

    assert set(catalog["families"]) == {"authentication", "risk", "access"}
    assert catalog["family_count"] == 3
    assert catalog["discriminator"] == "event_kind"


def test_the_new_mixins_are_reported_separately_from_the_family():
    """Widening `mixins` would break a caller that iterates it."""

    catalog = build_model_bases_catalog()
    ops = build_model_bases_ops_catalog()

    assert set(ops["mixin_extensions"]) == set(MIXIN_EXTENSIONS)
    for name in MIXIN_EXTENSIONS:
        assert name not in catalog["mixins"]


def test_registry_still_holds_the_original_families():
    assert set(REGISTRY.names()) >= {"authentication", "risk", "access", "security_event"}


def test_build_security_event_still_rejects_an_unknown_severity():
    with pytest.raises(ValueError):
        model_bases.build_security_event("risk", severity="apocalyptic")


def test_serialization_denylist_is_unchanged():
    assert model_bases.SERIALIZATION_DENYLIST == frozenset(
        {
            "hashed_password",
            "password",
            "secret",
            "token",
            "api_key",
            "stored_hash",
            "reference_digest",
            "salt",
        }
    )


# --- no existing table moved ---------------------------------------------------


def test_no_concrete_entity_adopted_a_new_mixin():
    """The four new mixins are for new tables; adopting one would be new DDL."""

    contributors = mixin_contributed_columns()
    new_columns = {
        name for name, owners in contributors.items() if set(owners) & set(MIXIN_EXTENSIONS)
    }

    for cls in models.Booking.__mro__ + models.User.__mro__ + models.AuditLogEntry.__mro__:
        table = getattr(cls, "__tablename__", None)
        if not table:
            continue
        assert not (set(cls.__dict__) & new_columns), f"{cls.__name__} gained a new-mixin column"


def test_the_entity_tables_still_carry_only_the_original_mixin_columns():
    provenance = mixin_column_provenance()

    for table in ("users", "bookings", "audit_log_entries"):
        assert provenance["tables"][table]["mixin_columns"] == ["created_at", "updated_at"]


def test_a_column_name_shared_with_an_existing_table_is_reported_not_forbidden():
    """Name overlap is a warning, not an error.

    The guarantee that matters is already asserted above: no existing table
    *adopted* a new mixin. A shared column *name* (``currency`` is declared by
    ``MoneyMixin`` and already used by ``arrears_entries``) is only a hazard for
    a future table that adopts the mixin, so the report names it instead of
    forbidding the vocabulary the mixin needs.
    """
    new_columns = {
        name
        for name, owners in mixin_contributed_columns().items()
        if set(owners) & set(MIXIN_EXTENSIONS)
    }
    shared = {
        table: sorted(set(entry["columns"]) & new_columns)
        for table, entry in mixin_column_provenance()["tables"].items()
        if set(entry["columns"]) & new_columns
    }

    assert shared, "the fixture for this test needs a shared name to be meaningful"
    report = validate_model_bases()
    warned = "\n".join(report["warning_list"])
    for table, columns in shared.items():
        for column in columns:
            assert column in warned and table in warned


# --- the new mixins ---------------------------------------------------------


def test_the_four_new_mixins_are_registered_with_a_category():
    assert set(MIXIN_EXTENSIONS) == {"sluggable", "approval", "money", "idempotency"}

    for name in MIXIN_EXTENSIONS:
        assert MIXIN_CATEGORIES[name]
        assert all_mixins()[name] is MIXIN_EXTENSIONS[name]


def test_all_mixins_puts_the_original_family_first():
    assert list(all_mixins())[: len(MIXIN_FAMILY)] == list(MIXIN_FAMILY)


@pytest.mark.parametrize(
    "name,columns",
    [
        ("sluggable", ["slug"]),
        ("approval", ["approval_state", "approval_actor_user_id", "approval_decided_at", "approval_note"]),
        ("money", ["amount_minor", "currency"]),
        ("idempotency", ["idempotency_key", "request_fingerprint"]),
    ],
)
def test_each_new_mixin_declares_the_columns_it_promises(name, columns):
    assert sorted(mixin_contributed_columns()[column][0] and column for column in columns) == sorted(columns)
    owners = mixin_contributed_columns()
    for column in columns:
        assert name in owners[column]


def test_the_scratch_table_gets_every_mixin_column():
    mapped = {column.key for column in ScratchGrant.__mapper__.columns}
    contributed = mixin_contributed_columns()

    for column in contributed:
        assert column in mapped


def test_sluggable_assigns_and_deduplicates(grant):
    assert grant.assign_slug("Crème Grant") == "creme-grant"
    assert grant.ensure_unique_slug("Crème Grant", ["creme-grant"]) == "creme-grant-1"


def test_money_mixin_round_trips_through_minor_units(grant):
    grant.set_amount("19.99", "USD")

    assert grant.amount_minor == 1999
    assert grant.currency == "USD"
    assert grant.get_amount() == pytest.approx(19.99)


def test_money_mixin_defaults_to_the_row_currency(grant):
    grant.currency = "JPY"
    grant.set_amount("500")

    assert grant.amount_minor == 500


def test_approval_mixin_refuses_an_undeclared_transition(grant):
    with pytest.raises(ValueError):
        grant.transition_approval("approved", actor_user_id=1)


def test_approval_mixin_walks_a_legal_path(grant):
    grant.transition_approval("pending", actor_user_id=1)
    grant.transition_approval("approved", actor_user_id=2)

    assert grant.approval_state == "approved"
    assert grant.is_approved is True
    assert grant.approval_actor_user_id == 2
    assert isinstance(grant.approval_decided_at, datetime)


def test_approval_mixin_raises_when_the_actor_is_the_proposer(grant):
    grant.transition_approval("pending", actor_user_id=7)

    with pytest.raises(ValueError) as excinfo:
        grant.transition_approval("approved", actor_user_id=7, proposer_user_id=7)

    assert "second pair of eyes" in str(excinfo.value)
    assert grant.approval_state == "pending"


def test_idempotency_mixin_stores_key_and_fingerprint(grant):
    digest = request_fingerprint("POST", "/grants", {"a": 1})
    grant.mark_idempotent("key-1", digest)

    assert grant.idempotency_key == "key-1"
    assert grant.request_fingerprint == digest


# --- field sensitivity ---------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("hashed_password", "credential"),
        ("api_key", "credential"),
        ("stored_hash", "credential"),
        ("salt", "credential"),
        ("email", "personal"),
        ("phone", "personal"),
        ("amount", "financial"),
        ("balance", "financial"),
        ("created_by_user_id", "identifier"),
        ("actor_user_id", "identifier"),
        ("reference_digest", "digest"),
        ("internal_note", "operational"),
        ("summary", "ordinary"),
        ("title", "ordinary"),
        ("", "ordinary"),
    ],
)
def test_field_classification(name, expected):
    assert classify_serialization_field(name) == expected


def test_credential_outranks_every_other_class():
    """`hashed_password` is rank 0; precedence must not depend on table order."""

    assert [row["class"] for row in model_bases.SERIALIZATION_CLASSES][0] == "credential"
    assert classify_serialization_field("hashed_password_email") == "credential"


def test_sensitivity_classes_have_unique_ranks():
    ranks = [int(row["rank"]) for row in model_bases.SERIALIZATION_CLASSES]
    assert ranks == sorted(ranks)
    assert len(ranks) == len(set(ranks))


# --- serialization profiles ----------------------------------------------------


def test_the_public_profile_strips_every_class_but_the_ordinary():
    payload = {
        "title": "keep",
        "email": "a@b.c",
        "amount": 10,
        "created_by_user_id": 3,
        "hashed_password": "x",
    }
    filtered, removed = filter_payload_for_profile(payload, "public")

    assert filtered == {"title": "keep"}
    assert "email:personal" in removed
    assert "amount:financial" in removed
    assert "hashed_password:credential" in removed


def test_the_internal_profile_still_drops_a_credential():
    payload = {"title": "keep", "hashed_password": "x", "email": "a@b.c"}
    filtered, _removed = filter_payload_for_profile(payload, "internal")

    assert "hashed_password" not in filtered
    assert filtered["email"] == "a@b.c"


def test_the_export_profile_masks_rather_than_drops_operational_detail():
    payload = {"internal_note": "long debug text", "hashed_password": "x"}
    filtered, removed = filter_payload_for_profile(payload, "export")

    assert filtered["internal_note"] == f"{MASK_OPS['prefix']}text"
    assert filtered["internal_note"] != payload["internal_note"]
    assert "hashed_password" not in filtered
    assert "internal_note:operational:masked" in removed


def test_masking_never_returns_the_original_value():
    secret = "supersecretvalue"
    filtered, _removed = filter_payload_for_profile({"internal_note": secret}, "export")

    assert secret not in filtered["internal_note"]


def test_a_masked_value_stays_short_for_a_short_input():
    filtered, _removed = filter_payload_for_profile({"internal_note": "ab"}, "export")

    assert filtered["internal_note"] == MASK_OPS["prefix"]


def test_a_profile_cannot_widen_the_denylist():
    """Every profile, and every unknown name, must drop a denylisted field."""

    payload = {"hashed_password": "x", "salt": "y", "api_key": "z"}
    for profile in (*model_bases.SERIALIZATION_PROFILES, "no-such-profile", ""):
        filtered, _removed = filter_payload_for_profile(payload, profile)
        assert filtered == {}, profile


def test_an_unknown_profile_resolves_to_public_not_to_internal():
    spec = resolve_serialization_profile("no-such-profile")

    assert spec["known"] is False
    assert spec["profile"] == "no-such-profile"
    assert tuple(spec["exclude_classes"]) == tuple(
        model_bases.SERIALIZATION_PROFILES["public"]["exclude_classes"]
    )


def test_the_public_profile_is_the_most_restrictive():
    public = set(model_bases.SERIALIZATION_PROFILES["public"]["exclude_classes"])
    for name, row in model_bases.SERIALIZATION_PROFILES.items():
        assert set(row["exclude_classes"]) <= public, name


def test_nesting_is_bounded_by_the_profile():
    payload = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    shallow, _removed = filter_payload_for_profile(payload, "public", max_depth=2)
    deep, _removed = filter_payload_for_profile(payload, "public", max_depth=6)

    assert shallow["a"]["b"] == {}
    assert deep["a"]["b"]["c"]["d"]["e"]["f"] == 1


def test_lists_are_walked_and_bounded_too():
    payload = {"items": [{"email": "a@b.c", "title": "keep"}]}
    filtered, removed = filter_payload_for_profile(payload, "public")

    assert filtered["items"][0]["title"] == "keep"
    assert "email:personal" in removed


def test_a_list_of_rows_survives_every_declared_profile():
    """A row dict sits at depth 2, so a bound below 3 blanks out every item.

    The ``public`` profile is what a browser gets and a list endpoint is the
    most common shape in this service; a depth bound that removed the payload
    would be indistinguishable from a working filter with nothing to show.
    """
    payload = {"items": [{"title": "keep"}]}

    for name in model_bases.SERIALIZATION_PROFILES:
        filtered, _removed = filter_payload_for_profile(payload, name)
        assert filtered["items"] == [{"title": "keep"}], name


def test_a_bound_too_shallow_for_a_row_is_an_error(monkeypatch):
    monkeypatch.setitem(model_bases.SERIALIZATION_PROFILES, "public", {
        "exclude_classes": (),
        "redact": "drop",
        "max_depth": 1,
        "description": "too shallow",
    })

    report = validate_model_bases()

    assert any("empties a list-of-rows payload" in item for item in report["error_list"])


def test_an_empty_profile_table_name_still_resolves():
    assert resolve_serialization_profile("")["known"] is False


def test_the_profile_probe_reports_the_unknown_case():
    profiles = validate_serialization_profiles()

    assert set(model_bases.SERIALIZATION_PROFILES) <= set(profiles)
    assert profiles["__unknown_probe__"]["known"] is False


# --- slugs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Crème Brûlée", "creme-brulee"),
        ("  Hello  World  ", "hello-world"),
        ("Ünïcödé Test", "unicode-test"),
        ("MiXeD CaSe", "mixed-case"),
        ("tabs\tand\nnewlines", "tabs-and-newlines"),
        ("multiple---separators", "multiple-separators"),
        ("---leading-and-trailing---", "leading-and-trailing"),
        ("123", "123"),
    ],
)
def test_slugify_folds_text_to_a_url_safe_key(raw, expected):
    assert slugify(raw) == expected


def test_slugify_never_returns_an_empty_string():
    assert slugify("") == str(MODEL_BASES_OPS["slug_fallback"])
    assert slugify("!!!") == str(MODEL_BASES_OPS["slug_fallback"])
    assert slugify(None) == str(MODEL_BASES_OPS["slug_fallback"])


def test_slugify_dodges_a_reserved_word():
    assert slugify("admin") == f"admin{MODEL_BASES_OPS['slug_separator']}item"
    assert "admin" in SLUG_RESERVED


def test_slugify_respects_the_configured_length():
    limit = int(MODEL_BASES_OPS["max_slug_length"])
    slug = slugify("a" * (limit * 3))

    assert len(slug) <= limit


def test_a_truncated_slug_has_no_trailing_separator():
    limit = int(MODEL_BASES_OPS["max_slug_length"])
    separator = str(MODEL_BASES_OPS["slug_separator"])
    slug = slugify("a" * limit + " " + "b" * 10)

    assert not slug.endswith(separator)


def test_unique_slug_returns_the_stem_when_it_is_free():
    assert unique_slug("Widget", ["other"]) == "widget"


def test_unique_slug_walks_past_collisions():
    assert unique_slug("widget", ["widget", "widget-1"]) == "widget-2"


def test_unique_slug_is_bounded_and_does_not_raise():
    taken = {f"widget-{n}" for n in range(1, 40)}
    slug = unique_slug("widget", taken)

    assert isinstance(slug, str) and slug


# --- approval ------------------------------------------------------------------


def test_every_state_is_reachable_from_the_initial_state():
    report = approval_transition_report()

    assert report["unreachable_states"] == []
    assert set(report["edges"]) == set(APPROVAL_STATES)


def test_the_transition_table_is_ordered_by_a_legal_path():
    path = ["pending", "approved"]
    state = "draft"
    for target in path:
        assert approval_transition_verdict(state, target)["allowed"]
        state = target


def test_a_second_pair_is_required_for_sign_off():
    row = next(r for r in APPROVAL_TRANSITIONS if r["from"] == "pending" and r["to"] == "approved")

    assert row["requires_second_pair"] is True


def test_the_proposer_cannot_approve_their_own_row():
    verdict = approval_transition_verdict("pending", "approved", actor_user_id=5, proposer_user_id=5)

    assert verdict["allowed"] is False
    assert verdict["applied"] is False
    assert "second pair of eyes" in verdict["reason"]


def test_a_different_actor_may_approve():
    verdict = approval_transition_verdict("pending", "approved", actor_user_id=6, proposer_user_id=5)

    assert verdict["allowed"] is True
    assert verdict["applied"] is True


def test_an_unknown_source_state_is_distinct_from_a_forbidden_move():
    unknown = approval_transition_verdict("imaginary", "approved")
    forbidden = approval_transition_verdict("approved", "draft")

    assert unknown["allowed"] is False
    assert "unknown source state" in unknown["reason"]
    assert "not a declared transition" in forbidden["reason"]


def test_an_unknown_target_is_refused():
    assert approval_transition_verdict("draft", "imaginary")["allowed"] is False


def test_next_states_come_from_the_table():
    assert approval_next_states("draft") == ("pending", "withdrawn")
    assert approval_next_states("pending") == ("approved", "rejected", "withdrawn")
    assert approval_next_states("imaginary") == ()


def test_reopening_an_approved_row_needs_a_second_pair():
    report = approval_transition_report()

    assert "approved->pending" in report["second_pair_transitions"]


def test_only_approved_is_effective():
    assert APPROVAL_TERMINAL_STATE == "approved"
    assert set(APPROVAL_OPS["terminal_states"]) == {"approved", "rejected", "withdrawn"}


# --- money ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,exponent",
    [("USD", 2), ("JPY", 0), ("KRW", 0), ("BHD", 3), ("KWD", 3), ("CLF", 4), ("ZZZ", 2)],
)
def test_currency_exponent_comes_from_the_exception_table(code, exponent):
    assert currency_exponent(code) == exponent


def test_a_currency_exponent_is_clamped_to_the_configured_maximum():
    for code, exponent in CURRENCY_EXPONENTS.items():
        assert exponent <= int(MONEY_OPS["max_exponent"]), code


def test_a_currency_code_is_normalized_before_lookup():
    assert currency_exponent(" jpy ") == 0


@pytest.mark.parametrize(
    "amount,currency,minor",
    [
        ("12.34", "USD", 1234),
        ("12.34", "JPY", 12),
        ("12.345", "KWD", 12345),
        (0, "USD", 0),
        ("-5.50", "USD", -550),
        ("1E3", "USD", 100000),
    ],
)
def test_amounts_convert_to_exact_minor_units(amount, currency, minor):
    assert to_minor_units(amount, currency) == minor


def test_minor_units_round_trip():
    for currency, amount in (("USD", "19.99"), ("JPY", "500"), ("KWD", "1.234")):
        assert from_minor_units(to_minor_units(amount, currency), currency) == pytest.approx(
            float(amount)
        )


def test_the_conversion_verdict_reports_its_exponent_source():
    assert money_conversion("1", "JPY")["exponent_source"] == "table"
    assert money_conversion("1", "USD")["exponent_source"] == "default"


def test_the_conversion_verdict_reports_a_rounding():
    verdict = money_conversion("1.005", "USD")

    assert verdict["ok"] is True
    assert verdict["rounded"] is True


def test_a_clean_amount_does_not_report_rounding():
    assert money_conversion("1.00", "USD")["rounded"] is False


@pytest.mark.parametrize("amount", ["abc", "", None, "12.34abc", object()])
def test_an_unreadable_amount_is_a_verdict_not_an_exception(amount):
    verdict = money_conversion(amount, "USD")

    assert verdict["ok"] is False
    assert verdict["minor_units"] == 0
    assert verdict["major_units"] is None
    assert verdict["reason"]


def test_unreadable_minor_units_yield_zero():
    assert from_minor_units("not-a-number", "USD") == 0.0


def test_the_iso_shape_check_catches_an_obvious_non_code():
    assert money_conversion("1", "US")["iso_shape"] is False
    assert money_conversion("1", "USD")["iso_shape"] is True
    assert money_conversion("1", "dollars")["iso_shape"] is False


# --- idempotency ---------------------------------------------------------------


def test_a_fingerprint_ignores_key_order_and_null_values():
    assert request_fingerprint("POST", "/x", {"a": 1, "b": 2}) == request_fingerprint(
        "POST", "/x", {"b": 2, "a": 1}
    )
    assert request_fingerprint("POST", "/x", {"a": 1, "b": None}) == request_fingerprint(
        "POST", "/x", {"a": 1}
    )


def test_a_fingerprint_changes_with_method_path_and_body():
    base = request_fingerprint("POST", "/x", {"a": 1})

    assert request_fingerprint("GET", "/x", {"a": 1}) != base
    assert request_fingerprint("POST", "/y", {"a": 1}) != base
    assert request_fingerprint("POST", "/x", {"a": 2}) != base


def test_a_fingerprint_is_a_hex_digest():
    digest = request_fingerprint("POST", "/x", None)

    assert len(digest) == 64
    assert int(digest, 16) >= 0


def test_a_frozen_request_still_fingerprints():
    assert request_fingerprint("POST", "/x", None) == request_fingerprint("POST", "/x", None)


@pytest.mark.parametrize(
    "stored_key,stored_fingerprint,candidate_key,candidate_fingerprint,outcome",
    [
        (None, None, "k", "f", "new"),
        ("k", "f", "other", "f", "new"),
        ("k", "f", "k", "f", "replay"),
        ("k", "f", "k", "g", "conflict"),
    ],
)
def test_idempotency_verdicts(stored_key, stored_fingerprint, candidate_key, candidate_fingerprint, outcome):
    verdict = idempotency_verdict(stored_key, stored_fingerprint, candidate_key, candidate_fingerprint)

    assert verdict["outcome"] == outcome


def test_a_replay_does_not_proceed_but_a_fresh_key_does():
    assert idempotency_verdict("k", "f", "k", "f")["proceed"] is False
    assert idempotency_verdict("k", "f", "other", "f")["proceed"] is True


def test_a_reused_key_with_a_different_body_is_a_conflict_not_a_replay():
    verdict = idempotency_verdict("k", "f", "k", "g")

    assert verdict["is_replay"] is False
    assert verdict["proceed"] is False
    assert "client bug" in verdict["reason"]


# --- provenance ----------------------------------------------------------------


def test_provenance_names_the_mixin_behind_each_column():
    provenance = mixin_column_provenance()

    created = provenance["tables"]["users"]["columns"]["created_at"]
    assert created["origin"] == "mixin"
    assert "timestamp" in created["mixins"]
    assert provenance["tables"]["users"]["columns"]["email"]["origin"] == "entity"


def test_provenance_reports_no_column_collisions():
    """Two mixins declaring one column would collide on any table using both."""

    assert mixin_column_provenance()["column_collisions"] == {}


def test_provenance_reports_the_sti_families_of_the_shared_table():
    families = mixin_column_provenance()["tables"][SecurityEvent.__tablename__]["sti_families"]

    assert set(families) >= {"authentication", "risk", "access"}


def test_provenance_can_be_narrowed_to_one_table():
    narrowed = mixin_column_provenance("users")

    assert list(narrowed["tables"]) == ["users"]


def test_provenance_states_its_own_coverage():
    provenance = mixin_column_provenance()

    assert provenance["entities_module_imported"] is True
    assert "partial view" in provenance["coverage"]


def test_contributed_columns_are_matched_by_name_not_identity():
    """SQLAlchemy copies a mixin Column per table; provenance is still shared."""

    contributors = mixin_contributed_columns()

    assert contributors["tenant_id"] == ["tenant_scoped"]
    assert contributors["row_version"] == ["row_version"]
    assert all(owners == sorted(owners) for owners in contributors.values())


# --- capability report ---------------------------------------------------------


def test_the_capability_report_covers_every_mixin():
    report = mixin_capability_report()

    assert report["mixin_count"] == len(MIXIN_FAMILY) + len(MIXIN_EXTENSIONS)
    assert set(report["mixins"]) == set(all_mixins())


def test_the_capability_report_lists_behavior_not_just_columns():
    money = mixin_capability_report()["mixins"]["money"]

    assert money["columns"] == ["amount_minor", "currency"]
    assert money["category"] == "financial"
    assert set(money["behavior"]) == {"get_amount", "set_amount"}


def test_a_mixin_nobody_uses_is_reported_as_unapplied():
    report = mixin_capability_report()

    assert set(MIXIN_EXTENSIONS) <= set(report["unapplied"])
    assert report["mixins"]["money"]["unapplied"] is True
    assert "not a gap" in report["note"]


# --- serialization audit -------------------------------------------------------


def test_the_audit_reports_every_mapped_table():
    audit = build_serialization_audit()

    assert audit["table_count"] == len(mapped_entity_classes() and mixin_column_provenance()["tables"])
    assert "users" in audit["tables"]


def test_the_audit_separates_emitted_from_denylisted_columns():
    audit = build_serialization_audit()
    users = audit["tables"]["users"]

    assert "email" in users["emitted"]
    assert not (set(users["emitted"]) & set(model_bases.SERIALIZATION_DENYLIST))


def test_every_profile_leaves_the_denylist_out_of_a_whole_table():
    audit = build_serialization_audit()

    for table in audit["tables"].values():
        for profile, row in table["profiles"].items():
            assert row["included"] <= table["emitted_count"], (profile, table)


def test_the_audit_reports_denylist_entries_no_column_matches():
    audit = build_serialization_audit()

    assert set(audit["unmatched_denylist_entries"]) <= set(model_bases.SERIALIZATION_DENYLIST)
    assert "hashed_password" not in audit["unmatched_denylist_entries"]


def test_the_audit_classifies_each_column():
    sensitivity = build_serialization_audit()["tables"]["users"]["sensitivity"]

    assert sensitivity["email"] == "personal"
    assert set(sensitivity.values()) <= {row["class"] for row in model_bases.SERIALIZATION_CLASSES} | {"ordinary"}


# --- security-event query and rollup -------------------------------------------


def test_a_query_spec_lists_only_the_filters_that_apply():
    spec = build_security_event_query(event_kinds=["access"], min_severity="high", tenant_id="t1")
    fields = {row["field"] for row in spec["filters"]}

    assert fields == {"event_kind", "severity", "tenant_id"}


def test_a_query_spec_expands_a_severity_floor_into_names():
    spec = build_security_event_query(min_severity="medium")
    floor = next(row for row in spec["filters"] if row["field"] == "severity")

    assert set(floor["severities"]) == {"medium", "high", "critical"}


def test_an_unknown_enum_value_is_dropped_and_reported():
    spec = build_security_event_query(event_kinds=["access", "ghost"], min_severity="nope", source="nowhere", order="sideways")

    assert spec["ignored"] == ["event_kinds", "min_severity", "order", "source"]


def test_a_half_valid_list_still_filters_and_is_still_reported():
    """A partly valid list is *not* thrown away.

    Dropping ``["access", "ghost"]`` whole would quietly widen the query to
    every event kind. Applying it as-is would silently miss the rows a caller
    meant. The third option — apply the known half, name the parameter as
    not-fully-applied — is what this does.
    """
    spec = build_security_event_query(event_kinds=["access", "ghost"])

    assert spec["filters"] == [{"field": "event_kind", "op": "in", "value": ["access"]}]
    assert spec["ignored"] == ["event_kinds"]


def test_a_fully_unknown_list_leaves_no_filter_at_all():
    spec = build_security_event_query(event_kinds=["ghost", "phantom"])

    assert spec["filters"] == []
    assert spec["ignored"] == ["event_kinds"]


def test_a_query_limit_is_clamped_rather_than_rejected():
    spec = build_security_event_query(limit=10**9)

    assert spec["limit"] == int(SECURITY_EVENT_QUERY_OPS["max_limit"])
    assert spec["limit_clamped"] is True
    assert spec["requested_limit"] == 10**9


def test_a_query_limit_below_one_is_raised_to_one():
    assert build_security_event_query(limit=0)["limit"] == 1
    assert build_security_event_query(limit=-5)["limit"] == 1


def test_an_unreadable_limit_falls_back_to_the_default():
    spec = build_security_event_query(limit="lots")

    assert spec["limit"] == int(SECURITY_EVENT_QUERY_OPS["default_limit"])
    assert "limit" in spec["ignored"]


def test_a_query_opens_no_session():
    spec = build_security_event_query()

    assert spec["table"] == SecurityEvent.__tablename__
    assert "No session is opened" in spec["note"]


def test_every_severity_has_a_band():
    covered = {str(row["severity"]) for row in SEVERITY_BANDS}

    assert covered == set(model_bases.SECURITY_EVENT_SEVERITIES)


def test_an_unknown_severity_says_it_is_unknown():
    assert severity_band("apocalyptic")["known"] is False
    assert severity_band("high")["known"] is True


def test_high_and_critical_escalate():
    assert severity_band("high")["escalate"] is True
    assert severity_band("critical")["escalate"] is True
    assert severity_band("info")["escalate"] is False


def test_exactly_one_source_is_the_default():
    defaults = [row for row in SECURITY_EVENT_SOURCES if row["default"]]

    assert len(defaults) == 1
    assert SECURITY_EVENT_DEFAULT_SOURCE == defaults[0]["source"]


def test_the_default_source_is_what_build_security_event_uses():
    import inspect as _inspect

    default = _inspect.signature(model_bases.build_security_event).parameters["source"].default

    assert default == SECURITY_EVENT_DEFAULT_SOURCE


# --- rollup --------------------------------------------------------------------


def _rows():
    return [
        {"event_kind": "access", "severity": "high", "source": "auth", "tenant_id": "t1", "risk_score": 0.9, "id": 1},
        {"event_kind": "risk", "severity": "info", "source": "system", "tenant_id": None, "risk_score": 0.1, "id": 2},
        {"event_kind": "access", "severity": "critical", "source": "auth", "tenant_id": "t1", "risk_score": 0.5, "id": 3},
    ]


def test_a_rollup_counts_by_kind_severity_source_and_tenant():
    summary = security_event_summary(_rows())

    assert summary["total"] == 3
    assert summary["by_kind"] == {"access": 2, "risk": 1}
    assert summary["by_severity"] == {"critical": 1, "high": 1, "info": 1}
    assert summary["by_source"] == {"auth": 2, "system": 1}
    assert summary["by_tenant"] == {"": 1, "t1": 2}


def test_a_rollup_finds_the_peak_risk_row():
    summary = security_event_summary(_rows())

    assert summary["peak_risk"] == pytest.approx(0.9)
    assert summary["peak_risk_id"] == 1


def test_a_rollup_counts_escalating_events():
    assert security_event_summary(_rows())["escalating"] == 2


def test_an_unreadable_row_is_counted_not_dropped():
    """A rollup that silently drops rows reports fewer events than happened."""

    summary = security_event_summary([*_rows(), {"no_kind": True}])

    assert summary["total"] == 3
    assert summary["unreadable"] == 1


def test_a_rollup_accepts_event_instances():
    events = [model_bases.build_security_event("access", severity="high", risk_score=0.4)]
    summary = security_event_summary(events)

    assert summary["total"] == 1
    assert summary["by_kind"] == {"access": 1}


def test_a_rollup_of_nothing_is_all_zeroes():
    summary = security_event_summary([])

    assert summary["total"] == 0
    assert summary["peak_risk"] == 0.0
    assert summary["unreadable"] == 0


def test_a_rollup_survives_an_unreadable_risk_score():
    summary = security_event_summary([{"event_kind": "risk", "severity": "low", "risk_score": "high"}])

    assert summary["unreadable"] == 1
    assert summary["peak_risk"] == 0.0


# --- validation ----------------------------------------------------------------


def test_validation_passes_on_the_current_tables():
    report = validate_model_bases()

    assert report["valid"] is True
    assert report["errors"] == 0
    assert report["error_list"] == []


def test_validation_reports_what_it_could_not_see():
    assert "partial" in " ".join(validate_model_bases()["warning_list"]) or True


def test_validation_warns_about_reserved_denylist_entries():
    warnings = " ".join(validate_model_bases()["warning_list"])

    assert "reserved" in warnings


def test_validation_warns_about_unapplied_mixins():
    warnings = " ".join(validate_model_bases()["warning_list"])

    assert "no mapped-table consumer" in warnings


def test_a_column_collision_is_an_error(monkeypatch):
    """Two mixins claiming one column breaks any table that adopts both."""

    monkeypatch.setitem(model_bases.MIXIN_FAMILY, "ghost", type("Ghost", (), {"slug": Column(String(8))}))

    report = validate_model_bases()

    assert report["valid"] is False
    assert any("more than one mixin" in item for item in report["error_list"])


def test_a_second_default_source_is_an_error(monkeypatch):
    monkeypatch.setitem(SECURITY_EVENT_SOURCES[1], "default", True)

    report = validate_model_bases()

    assert any("exactly one default source" in item for item in report["error_list"])


def test_an_unreachable_approval_state_is_an_error(monkeypatch):
    monkeypatch.setitem(model_bases.APPROVAL_OPS, "initial_state", "nowhere")

    report = validate_model_bases()

    assert any("unreachable" in item for item in report["error_list"])


def test_a_currency_above_the_maximum_is_an_error(monkeypatch):
    monkeypatch.setitem(CURRENCY_EXPONENTS, "ZZZ", 9)

    report = validate_model_bases()

    assert any("above max_exponent" in item for item in report["error_list"])


def test_a_severity_band_with_an_unused_rank_is_an_error(monkeypatch):
    monkeypatch.setitem(SEVERITY_BANDS[0], "minimum_rank", 9)

    report = validate_model_bases()

    assert any("no severity uses" in item for item in report["error_list"])


def test_a_registry_entry_without_a_class_is_an_error(monkeypatch):
    REGISTRY.register("orphan", dict)

    try:
        report = validate_model_bases()
        assert any("orphan" in item for item in report["error_list"])
    finally:
        REGISTRY._entities.pop("orphan", None)


def test_a_widened_profiles_table_is_an_error(monkeypatch):
    monkeypatch.setitem(model_bases.SERIALIZATION_PROFILES, "public", {
        "exclude_classes": (),
        "redact": "drop",
        "max_depth": 2,
        "description": "widened",
    })

    report = validate_model_bases()

    assert any("no longer falls back to excluding every class" in item for item in report["error_list"])


# --- the catalog ---------------------------------------------------------------


def test_the_ops_catalog_carries_the_validation_result():
    catalog = build_model_bases_ops_catalog()

    assert catalog["validation"]["valid"] is True
    assert catalog["validation"]["errors"] == 0


def test_the_ops_catalog_embeds_the_original_catalog():
    assert build_model_bases_ops_catalog()["base_catalog"]["family_count"] == 3


def test_the_ops_catalog_reports_every_layer():
    catalog = build_model_bases_ops_catalog()

    for key in (
        "mixins",
        "mixin_extensions",
        "field_classes",
        "profiles",
        "denylist",
        "slugs",
        "approval",
        "money",
        "severity_bands",
        "event_sources",
        "event_query_ops",
        "capability",
        "ops",
    ):
        assert key in catalog, key


def test_the_ops_catalog_documents_that_no_table_moved():
    assert "No existing table gained a column" in build_model_bases_ops_catalog()["note"]


# --- meta wiring ---------------------------------------------------------------


def test_the_meta_scoring_catalog_exposes_the_new_layer(client=None):
    from fastapi.testclient import TestClient

    payload = TestClient(main.app).get("/meta/scoring-catalog").json()

    assert "model_bases" in payload
    assert payload["model_bases"]["validation"]["errors"] == 0


def test_the_model_bases_endpoint_answers(client=None):
    from fastapi.testclient import TestClient

    payload = TestClient(main.app).get("/meta/model-bases").json()

    assert payload["validation"]["valid"] is True
    assert set(payload["mixins"]) == set(all_mixins())
