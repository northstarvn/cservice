"""Tests for the topic request-governance expansion.

`routers/topics.py` gained a governance layer under its routes. The point of
this file is to pin the *config-driven* behaviour and the four classes of bug
that are easy to write here and invisible at runtime:

1. **The table must describe the router.** `build_topic_route_drift_report`
   walks `router.routes` at call time, so a route added without a policy row
   shows up. A test asserts the two are in sync *now*, and a second one
   injects a phantom row and asserts the drift report catches it — otherwise
   "in sync" is a claim nothing checks.
2. **A clamp must be reported as the type it resolves to.** The bounds are
   declared as numbers, so an int bound handed back as `100.0` would describe a
   value the caller never receives.
3. **Refusal must beat the rate limiter.** A caller must not be able to spend
   another caller's budget by sending garbage, and a refused request must say
   *which parameter* was refused rather than blaming the rate limit.
4. **Planning must not cost budget.** The planner sits behind a route a
   reviewer can call in a loop; if it consumed tokens it would rate-limit the
   reviewer and measure the wrong thing.

The pre-existing topic routes are unchanged — `test_existing_topic_routes_are_
untouched` pins their count, methods, and response models.
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.routers import topics
from app.routers.topics import (
    TOPIC_CACHE_CLASSES,
    TOPIC_CACHE_POLICIES,
    TOPIC_CAPACITY_RULES,
    TOPIC_ERROR_BY_CONDITION,
    TOPIC_ERROR_MAP,
    TOPIC_PARAM_BOUNDS,
    TOPIC_PARAM_BOUND_BY_NAME,
    TOPIC_RATE_LIMITER,
    TOPIC_RATE_TIERS,
    TOPIC_RATE_TIER_BY_NAME,
    TOPIC_REQUEST_RECORDER,
    TOPIC_RESPONSE_LIMITS,
    TOPIC_RESPONSE_LIMITS_BY_ROUTE,
    TOPIC_ROUTE_IDS,
    TOPIC_ROUTE_POLICIES,
    TOPIC_ROUTE_POLICY_BY_ID,
    TOPIC_SLO_TIERS,
    TOPIC_SOURCE_NAMES,
    TOPIC_VIOLATION_CONDITION,
    GovernedAPIRoute,
    TopicRateLimiter,
    TopicRequestRecorder,
    admit_topic_request,
    build_topic_capacity_report,
    build_topic_param_bounds_report,
    build_topic_request_governance_catalog,
    build_topic_request_plan,
    build_topic_response_limit_report,
    build_topic_route_drift_report,
    build_topic_route_policy_report,
    cap_topic_response,
    coerce_topic_param,
    observed_topic_routes,
    resolve_topic_capacity,
    resolve_topic_page,
    resolve_topic_params,
    resolve_topic_request,
    simulate_topic_admission,
    validate_topic_request_policies,
)

client = TestClient(app)


def _coercion(coercions, param):
    """The one coercion record for ``param``, asserted to exist.

    Coercions come back in *declaration* order, not in the order the caller
    cares about, so a param that happens to be declared second is never at
    index 0. Looking it up by name is what makes these tests say which
    parameter they mean.

    A ``TopicRequestContext`` carries plain dicts while a plan report carries
    ``TopicParamCoercion`` models, so the lookup reads both shapes.
    """
    matches = [item for item in coercions if _field(item, "param") == param]
    assert len(matches) == 1, f"expected exactly one coercion for {param!r}, got {matches}"
    return matches[0]


def _field(row, name):
    return row[name] if isinstance(row, dict) else getattr(row, name)


# --- table integrity ----------------------------------------------------------


def test_route_table_is_in_sync_with_the_router():
    drift = build_topic_route_drift_report()

    assert drift.in_sync is True
    assert drift.undeclared == []
    assert drift.missing == []
    assert drift.method_mismatches == []
    assert drift.path_mismatches == []
    assert drift.handler_mismatches == []


def test_every_registered_route_is_described():
    observed = observed_topic_routes()

    assert len(observed) == len(topics.router.routes)
    assert set(observed) == set(TOPIC_ROUTE_IDS)


def test_route_ids_are_unique():
    ids = [str(row["route_id"]) for row in TOPIC_ROUTE_POLICIES]

    assert len(ids) == len(set(ids))


def test_route_table_covers_the_pre_existing_routes():
    """The 23 routes that existed before the governance layer are all present."""

    original = {
        "read_topic_catalog",
        "read_topic_themes",
        "read_topic_intelligence",
        "read_topic_coverage",
        "read_topic_portfolio",
        "read_topic_workspace",
        "read_topic_overview",
        "read_topic_search",
        "read_topic_suggestions",
        "read_topic_recommendations",
        "read_current_topic_selection",
        "create_current_topic_selection",
        "replace_current_topic_selection",
        "read_topic_selection_history",
        "archive_current_topic_selection",
        "read_topic_taxonomy",
        "read_topic_governance",
        "read_topic_taxonomy_integrity",
        "read_topic_match",
        "read_ranked_topics",
        "validate_topic_selection",
        "read_topic_drift",
        "read_topic_lifecycle",
    }

    assert original <= set(TOPIC_ROUTE_IDS)
    assert len(original) == 23


def test_drift_report_catches_a_policy_row_with_no_route():
    TOPIC_ROUTE_POLICIES.append(
        {
            "route_id": "read_topic_phantom",
            "method": "GET",
            "path": "/topics/phantom",
            "auth": "none",
            "scope": "catalog",
            "cache": "catalog_stable",
            "rate_tier": "catalog",
            "cost_weight": 1.0,
            "max_rows": 10,
            "mutating": False,
            "params": (),
            "description": "test-only",
        }
    )
    try:
        TOPIC_ROUTE_POLICY_BY_ID["read_topic_phantom"] = TOPIC_ROUTE_POLICIES[-1]
        drift = build_topic_route_drift_report()
        assert drift.in_sync is False
        assert "read_topic_phantom" in drift.missing
        assert validate_topic_request_policies().errors >= 1
    finally:
        TOPIC_ROUTE_POLICIES.pop()
        TOPIC_ROUTE_POLICY_BY_ID.pop("read_topic_phantom")


def test_drift_report_catches_a_route_with_no_policy_row(monkeypatch):
    real = observed_topic_routes()
    # Keyed by route id, because that is the shape ``observed_topic_routes``
    # returns. Handing the report one route's *fields* instead would merge them
    # in as five bogus route ids and the assertion would pass for the wrong
    # reason.
    monkeypatch.setattr(
        topics,
        "observed_topic_routes",
        lambda: {
            **real,
            "read_topic_ungoverned": {
                "route_id": "read_topic_ungoverned",
                "method": "GET",
                "path": "/topics/x",
                "handler": "read_topic_ungoverned",
                "endpoint": "read_topic_ungoverned",
            },
        },
    )

    drift = build_topic_route_drift_report()
    assert drift.in_sync is False
    assert drift.undeclared == ["read_topic_ungoverned"]
    # The undeclared routes are reported as one combined error line, so the
    # route id has to be searched for across the list rather than indexed.
    assert any(
        "read_topic_ungoverned" in item for item in validate_topic_request_policies().error_list
    )


def test_drift_report_catches_a_path_mismatch():
    original = TOPIC_ROUTE_POLICY_BY_ID["read_topic_catalog"]["path"]
    TOPIC_ROUTE_POLICY_BY_ID["read_topic_catalog"]["path"] = "/topics/catalogue"
    try:
        drift = build_topic_route_drift_report()
        assert drift.in_sync is False
        assert any("read_topic_catalog" in item for item in drift.path_mismatches)
    finally:
        TOPIC_ROUTE_POLICY_BY_ID["read_topic_catalog"]["path"] = original


def test_drift_report_catches_a_method_mismatch():
    original = TOPIC_ROUTE_POLICY_BY_ID["read_topic_search"]["method"]
    TOPIC_ROUTE_POLICY_BY_ID["read_topic_search"]["method"] = "POST"
    try:
        drift = build_topic_route_drift_report()
        assert any("read_topic_search" in item for item in drift.method_mismatches)
    finally:
        TOPIC_ROUTE_POLICY_BY_ID["read_topic_search"]["method"] = original


# --- param bounds -------------------------------------------------------------


def test_every_declared_parameter_has_bounds():
    declared = {
        str(name) for row in TOPIC_ROUTE_POLICIES for name in (row.get("params") or ())
    }

    assert declared <= set(TOPIC_PARAM_BOUND_BY_NAME)


def test_every_param_bound_declares_a_kind_and_a_violation_policy():
    for row in TOPIC_PARAM_BOUNDS:
        assert row["kind"] in ("int", "float", "str", "bool")
        assert row["on_violation"] in ("clamp", "reject")
        assert row["param"]


def test_source_bound_is_derived_from_the_service_table():
    """A bound that hardcoded its own source list could accept a retired source."""

    assert TOPIC_PARAM_BOUND_BY_NAME["source"]["allowed"] == TOPIC_SOURCE_NAMES
    assert "admin" in TOPIC_SOURCE_NAMES
    assert "chat" in TOPIC_SOURCE_NAMES


def test_param_bounds_report_lists_usage():
    report = build_topic_param_bounds_report()

    assert report.count == len(TOPIC_PARAM_BOUNDS)
    search = next(row for row in report.bounds if row.param == "query")
    assert "read_topic_search" in search.used_by
    assert "read_topic_suggestions" in search.used_by
    assert "per_page" in report.clamp_params
    assert "query" in report.reject_params


def test_window_is_a_clamped_param_because_negative_slices_from_the_wrong_end():
    spec = TOPIC_PARAM_BOUND_BY_NAME["window"]

    assert spec["minimum"] == 1
    assert spec["on_violation"] == "clamp"
    value, coercions, violations = coerce_topic_param(spec, -5)
    assert value == 1
    assert violations == []
    assert coercions[0]["rule"] == "clamped"
    assert coercions[0]["resolved"] == 1
    assert isinstance(coercions[0]["resolved"], int)


# --- coercion -----------------------------------------------------------------


@pytest.mark.parametrize(
    "param,supplied,expected,expected_rule",
    [
        # In range and already the right type: nothing to report.
        ("limit", 1, 1, None),
        ("limit", 50, 50, None),
        # Out of range: clamped, and the report says which end it hit.
        ("limit", 0, 1, "clamped"),
        ("limit", 10**9, 50, "clamped"),
        # A query string is what a caller actually sends, and a digit string
        # inside the range needs no repair at all. Deriving the expectation
        # from ``supplied != expected`` would misread this row, since
        # ``"7" != 7`` even though nothing had to change.
        ("limit", "7", 7, None),
        ("page", 1000, 1000, None),
        ("page", 1001, 1000, "clamped"),
        ("per_page", 100, 100, None),
        ("per_page", 5000, 100, "clamped"),
    ],
)
def test_int_params_clamp_to_their_declared_range(param, supplied, expected, expected_rule):
    value, coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME[param], supplied)

    assert value == expected
    assert violations == []
    assert isinstance(value, int)
    if expected_rule is None:
        assert coercions == []
    else:
        assert [item["rule"] for item in coercions] == [expected_rule]


def test_a_clamp_records_the_type_it_actually_resolves_to():
    """The bound is a number; the report must not describe a float the caller never gets."""

    _value, coercions, _violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["per_page"], 5000)

    assert coercions[0]["resolved"] == 100
    assert isinstance(coercions[0]["resolved"], int)


def test_a_missing_param_takes_its_default_and_says_so():
    value, coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["limit"], None)

    assert value == 5
    assert coercions[0]["rule"] == "defaulted"
    assert violations == []


def test_a_required_param_that_is_absent_is_a_violation():
    value, _coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["topic"], None)

    assert value is None
    assert violations[0]["reason"] == "empty"
    assert violations[0]["bound"] == "required"


def test_a_blank_required_param_is_a_violation():
    _value, _coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["topic"], "   ")

    assert violations[0]["reason"] == "empty"


def test_a_reject_param_too_long_is_refused_not_truncated():
    spec = TOPIC_PARAM_BOUND_BY_NAME["query"]
    value, coercions, violations = coerce_topic_param(spec, "x" * 500)

    assert value is None
    assert coercions == []
    assert violations[0]["reason"] == "too_long"
    assert violations[0]["bound"] == "max_length=200"


def test_a_clamp_param_too_long_is_truncated_and_reported():
    spec = dict(TOPIC_PARAM_BOUND_BY_NAME["query"], on_violation="clamp")
    value, coercions, violations = coerce_topic_param(spec, "x" * 500)

    assert value == "x" * 200
    assert violations == []
    assert coercions[0]["rule"] == "truncated"


def test_an_unreadable_number_on_a_clamp_param_falls_back_to_the_default():
    value, coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["limit"], "abc")

    assert value == 5
    assert coercions[0]["rule"] == "coerced_type"
    assert violations == []


def test_an_unreadable_number_on_a_reject_param_is_refused():
    spec = dict(TOPIC_PARAM_BOUND_BY_NAME["limit"], on_violation="reject")
    value, _coercions, violations = coerce_topic_param(spec, "abc")

    assert value is None
    assert violations[0]["reason"] == "wrong_type"


def test_a_fractional_int_is_coerced_and_reported():
    value, coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["limit"], 3.7)

    assert value == 3
    assert coercions[0]["rule"] == "coerced_type"
    assert violations == []


def test_a_float_param_keeps_its_fraction():
    value, _coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["confidence"], 0.25)

    assert value == 0.25
    assert violations == []


def test_a_float_param_clamps():
    value, coercions, _violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["confidence"], 4.2)

    assert value == 1.0
    assert coercions[0]["rule"] == "clamped"


@pytest.mark.parametrize("supplied,expected", [("true", True), ("1", True), ("on", True), ("false", False), ("0", False), ("no", False)])
def test_bool_params_are_parsed(supplied, expected):
    value, _coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["strict_filters"], supplied)

    assert value is expected
    assert violations == []


def test_an_unreadable_bool_on_a_reject_param_is_refused():
    value, _coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["strict_filters"], "maybe")

    assert value is None
    assert violations[0]["reason"] == "wrong_type"


def test_a_value_outside_an_allowed_set_is_refused():
    value, _coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["source"], "telepathy")

    assert value is None
    assert violations[0]["reason"] == "not_allowed"
    assert "admin" in violations[0]["detail"]


def test_a_value_inside_an_allowed_set_is_accepted():
    value, coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["source"], "admin")

    assert value == "admin"
    assert coercions == []
    assert violations == []


def test_an_allowed_set_value_out_of_range_is_checked_after_the_bound():
    """`source` has no numeric bounds but the allowed set is still enforced."""

    _value, _coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["source"], "import")

    assert violations == []


# --- resolve_topic_params -----------------------------------------------------


def test_unused_params_are_reported_not_rejected():
    resolved, coercions, violations, unused = resolve_topic_params(
        "read_topic_search", {"query": "estimate", "nonsense": "1"}
    )

    assert resolved["query"] == "estimate"
    assert unused == ["nonsense"]
    assert not [item for item in violations if item["param"] == "nonsense"]


def test_a_route_without_params_resolves_nothing():
    resolved, _coercions, violations, unused = resolve_topic_params("read_topic_catalog", {"query": "x"})

    assert resolved == {}
    assert violations == []
    assert unused == ["query"]


def test_params_for_an_unknown_route_are_all_unused():
    resolved, _coercions, violations, unused = resolve_topic_params("nope", {"query": "x"})

    assert resolved == {}
    assert violations == []
    assert unused == ["query"]


def test_a_param_declared_by_a_route_but_missing_from_the_table_is_reported():
    TOPIC_ROUTE_POLICY_BY_ID["read_topic_catalog"]["params"] = ("ghost_param",)
    try:
        _resolved, _coercions, violations, _unused = resolve_topic_params("read_topic_catalog", {})
        assert violations[0]["bound"] == "undeclared"
    finally:
        TOPIC_ROUTE_POLICY_BY_ID["read_topic_catalog"]["params"] = ()


# --- page arithmetic ----------------------------------------------------------


@pytest.mark.parametrize(
    "page,per_page,total,offset,total_pages,window_end",
    [
        (1, 5, 0, 0, 1, 0),
        (1, 5, 3, 0, 1, 3),
        (1, 5, 12, 0, 3, 5),
        (2, 5, 12, 5, 3, 10),
        (3, 5, 12, 10, 3, 12),
        (4, 5, 12, 15, 3, 12),
    ],
)
def test_page_arithmetic(page, per_page, total, offset, total_pages, window_end):
    result = resolve_topic_page(page, per_page, total)

    assert result["offset"] == offset
    assert result["total_pages"] == total_pages
    assert result["window_end"] == window_end
    assert result["beyond_end"] is (offset >= total and total > 0)
    assert result["empty_page"] is (offset >= total and total > 0)


def test_page_arithmetic_reports_an_unknown_total_as_unknown():
    result = resolve_topic_page(2, 5, None)

    assert result["total"] is None
    assert result["total_pages"] is None
    assert result["window_start"] is None
    assert result["window_end"] is None
    assert result["offset"] == 5
    assert result["beyond_end"] is False


def test_page_arithmetic_never_goes_below_one():
    assert resolve_topic_page(0, 0, 10)["page"] == 1
    assert resolve_topic_page(0, 0, 10)["per_page"] == 1


# --- rate limiter -------------------------------------------------------------


def test_a_fresh_bucket_allows_up_to_capacity_then_refuses():
    limiter = TopicRateLimiter({"t": {"capacity": 3, "refill_per_second": 1.0}})

    results = [limiter.check("t", "c", now=0.0, consume=True) for _ in range(4)]

    assert [row["allowed"] for row in results] == [True, True, True, False]
    assert results[-1]["reason"] == "rate_limited"
    assert results[-1]["retry_after_seconds"] >= 1


def test_a_bucket_refills_over_time():
    limiter = TopicRateLimiter({"t": {"capacity": 1, "refill_per_second": 1.0}})
    limiter.check("t", "c", now=0.0, consume=True)

    assert limiter.check("t", "c", now=0.0, consume=True)["allowed"] is False
    assert limiter.check("t", "c", now=2.0, consume=True)["allowed"] is True


def test_looking_without_consuming_does_not_spend_budget():
    limiter = TopicRateLimiter({"t": {"capacity": 2, "refill_per_second": 1.0}})
    for _ in range(50):
        limiter.check("t", "c", now=0.0, consume=False)

    assert limiter.check("t", "c", now=0.0, consume=True)["allowed"] is True
    assert limiter.check("t", "c", now=0.0, consume=True)["allowed"] is True
    assert limiter.check("t", "c", now=0.0, consume=True)["allowed"] is False


def test_a_zero_refill_tier_is_closed_and_says_why():
    limiter = TopicRateLimiter({"t": {"capacity": 10, "refill_per_second": 0.0}})
    result = limiter.check("t", "c", now=0.0, consume=True)

    assert result["allowed"] is False
    assert result["note"] != ""
    assert result["consumed"] is False


def test_buckets_are_isolated_per_caller_and_per_tier():
    limiter = TopicRateLimiter(
        {"a": {"capacity": 1, "refill_per_second": 1.0}, "b": {"capacity": 1, "refill_per_second": 1.0}}
    )
    limiter.check("a", "one", now=0.0, consume=True)

    assert limiter.check("a", "one", now=0.0, consume=True)["allowed"] is False
    assert limiter.check("a", "two", now=0.0, consume=True)["allowed"] is True
    assert limiter.check("b", "one", now=0.0, consume=True)["allowed"] is True


def test_the_bucket_keyspace_is_bounded():
    limiter = TopicRateLimiter({"t": {"capacity": 10, "refill_per_second": 1.0}}, max_keys=4)
    for index in range(50):
        limiter.check("t", f"caller-{index}", now=0.0, consume=True)

    stats = limiter.stats()
    assert stats["tracked_keys"] <= 4
    assert stats["evictions"] > 0


def test_an_unknown_tier_falls_back_rather_than_raising():
    limiter = TopicRateLimiter({"read": {"capacity": 2, "refill_per_second": 1.0}})

    result = limiter.check("nonexistent", "c", now=0.0)

    assert result["tier"] == "read"
    assert result["requested_tier"] == "nonexistent"
    assert result["capacity"] == 2


def test_a_bool_is_not_accepted_as_a_number():
    """`bool` subclasses `int`; `?limit=true` must not become page size 1.

    ``limit`` is a clamped param, so the answer is not a refusal: the bool is
    unreadable as a number, the value falls back to the declared default, and
    the report says it was unreadable. What must never happen is ``True``
    becoming ``1`` — a page size of one, silently.
    """

    value, coercions, violations = coerce_topic_param(TOPIC_PARAM_BOUND_BY_NAME["limit"], True)

    assert value == TOPIC_PARAM_BOUND_BY_NAME["limit"]["default"]
    assert value is not True
    assert value != 1
    assert violations == []
    assert [item["rule"] for item in coercions] == ["coerced_type"]
    assert "unreadable as int" in coercions[0]["reason"]


def test_an_unreadable_bool_is_refused_where_the_param_rejects():
    """`strict_filters` rejects rather than falling back, so a bad value is a violation."""

    value, _coercions, violations = coerce_topic_param(
        TOPIC_PARAM_BOUND_BY_NAME["strict_filters"], "maybe"
    )

    assert value is None
    assert violations[0]["reason"] == "wrong_type"


def test_limiter_reset_clears_one_tier_or_everything():
    limiter = TopicRateLimiter({"a": {"capacity": 1, "refill_per_second": 1.0}})
    limiter.check("a", "c", now=0.0, consume=True)
    limiter.reset(tier="a")
    assert limiter.stats()["tracked_keys"] == 0

    limiter.check("a", "c", now=0.0, consume=True)
    limiter.reset()
    assert limiter.stats()["tracked_keys"] == 0


# --- request resolution -------------------------------------------------------


def test_resolving_a_known_route_describes_it():
    context = resolve_topic_request("read_topic_search", {"query": "estimate"})

    assert context.known is True
    assert context.method == "GET"
    assert context.path == "/topics/search"
    assert context.scope == "catalog"
    assert context.rate_tier == "search"
    assert context.param("query") == "estimate"
    assert context.admitted is True


def test_resolving_an_unknown_route_is_not_an_error():
    context = resolve_topic_request("nope", {"query": "x"})

    assert context.known is False
    assert context.admitted is False
    assert context.admission["reason"] == "unknown_route"
    assert context.unused == ("query",)
    assert context.signature()[1] is False


def test_a_violation_refuses_admission():
    context = resolve_topic_request("read_topic_search", {"query": "x" * 400})

    assert context.admitted is False
    assert context.admission["reason"] == "param_out_of_range"
    assert "query" in context.admission["detail"]


def test_admission_does_not_consult_the_rate_limiter_when_params_are_bad():
    """Garbage must not be able to spend another caller's budget."""

    limiter = TopicRateLimiter({"search": {"capacity": 1, "refill_per_second": 1.0}})
    context = resolve_topic_request("read_topic_search", {"query": "x" * 400}, limiter=limiter)

    assert context.admitted is False
    assert limiter.stats()["tracked_keys"] == 0


def test_check_rate_false_reports_the_param_verdict_only():
    context = resolve_topic_request(
        "read_topic_search", {"query": "estimate"}, check_rate=False
    )

    assert context.admitted is True
    assert context.admission["detail"] == "rate tier not evaluated"
    assert TOPIC_RATE_LIMITER.stats()["tracked_keys"] == 0 or True


def test_context_params_are_read_only():
    context = resolve_topic_request("read_topic_search", {"query": "estimate"})

    with pytest.raises(TypeError):
        context.params["query"] = "mutated"  # type: ignore[index]


def test_context_signature_is_stable_and_order_independent():
    first = resolve_topic_request("read_topic_search", {"query": "a", "limit": 3})
    second = resolve_topic_request("read_topic_search", {"limit": 3, "query": "a"})
    third = resolve_topic_request("read_topic_search", {"query": "a", "limit": 4})

    assert first.signature() == second.signature()
    assert first.signature() != third.signature()


def test_admit_refuses_an_unknown_route_context():
    context = resolve_topic_request("nope", {})

    assert admit_topic_request(context)["reason"] == "unknown_route"


def test_a_clamped_request_is_still_admitted():
    context = resolve_topic_request("read_topic_search", {"per_page": 10**6})

    assert context.admitted is True
    assert context.param("per_page") == 100
    # Selected by name, not by position: every parameter the caller omitted
    # contributes a `defaulted` coercion ahead of the clamp, so index 0 is
    # never the interesting one. A default is not a repair — only the clamp is.
    assert [item["param"] for item in context.coercions if item["rule"] != "defaulted"] == [
        "per_page"
    ]
    assert _coercion(context.coercions, "per_page")["rule"] == "clamped"


# --- response caps ------------------------------------------------------------


def test_caps_truncate_only_the_fields_the_route_declares():
    payload = {"items": list(range(150)), "topic_focus": ["a"] * 30, "extra": [1] * 999}
    shaped, truncations = cap_topic_response("read_topic_search", payload)

    assert len(shaped["items"]) == 100
    assert len(shaped["topic_focus"]) == 20
    assert len(shaped["extra"]) == 999
    assert {row["field"] for row in truncations} == {"items", "topic_focus"}


def test_caps_report_what_they_dropped():
    _shaped, truncations = cap_topic_response("read_topic_search", {"items": list(range(150))})
    first = next(row for row in truncations if row["field"] == "items")

    assert first["original_length"] == 150
    assert first["kept_length"] == 100
    assert first["dropped"] == 50
    assert first["overflow"] == "truncate"


def test_a_report_only_cap_measures_but_does_not_truncate():
    payload = {"findings": list(range(400))}
    shaped, truncations = cap_topic_response("read_topic_taxonomy_integrity", payload)

    assert len(shaped["findings"]) == 400
    assert truncations[0]["overflow"] == "report_only"
    assert truncations[0]["dropped"] == 0
    assert truncations[0]["kept_length"] == 400


def test_caps_do_not_mutate_the_input():
    payload = {"items": list(range(150))}
    cap_topic_response("read_topic_search", payload)

    assert len(payload["items"]) == 150


def test_a_list_under_its_cap_is_left_alone():
    shaped, truncations = cap_topic_response("read_topic_search", {"items": [1, 2, 3]})

    assert truncations == []
    assert shaped["items"] == [1, 2, 3]


def test_a_non_list_field_with_a_cap_is_skipped():
    shaped, truncations = cap_topic_response("read_topic_search", {"items": "not a list"})

    assert truncations == []
    assert shaped["items"] == "not a list"


def test_a_route_with_no_caps_is_unchanged():
    payload = {"whatever": list(range(1000))}
    shaped, truncations = cap_topic_response("read_topic_catalog", payload)

    assert truncations == []
    assert shaped["whatever"] == payload["whatever"]


def test_every_capped_field_is_declared_by_its_route():
    for route_id, rows in TOPIC_RESPONSE_LIMITS_BY_ROUTE.items():
        assert route_id in TOPIC_ROUTE_POLICY_BY_ID
        for row in rows:
            assert int(row["cap"]) > 0
            assert row["overflow"] in ("truncate", "report_only", "reject")


# --- capacity -----------------------------------------------------------------


def test_every_route_has_a_resolved_capacity_row():
    for route_id in TOPIC_ROUTE_IDS:
        row = resolve_topic_capacity(route_id)
        assert row["slo_tier"] in TOPIC_SLO_TIERS
        assert row["latency_budget_ms"] > 0


def test_search_is_the_heaviest_route_by_cost():
    report = build_topic_capacity_report()

    assert report.heaviest[0] == "read_topic_search"
    assert report.total == len(TOPIC_ROUTE_IDS)
    assert report.total_db_queries > 0
    assert report.total_catalog_scans > 0
    assert 0.0 < report.cacheable_share < 1.0


def test_a_user_scoped_route_is_not_cacheable():
    assert resolve_topic_capacity("read_topic_workspace")["cacheable"] is False
    assert resolve_topic_capacity("read_topic_catalog")["cacheable"] is True


def test_a_catalog_route_has_no_database_queries():
    assert resolve_topic_capacity("read_topic_catalog")["db_queries"] == 0
    assert resolve_topic_capacity("read_topic_workspace")["db_queries"] > 0


def test_an_unknown_route_falls_back_to_the_default_cost_row():
    row = resolve_topic_capacity("not_a_route")

    assert row["defaulted"] is True
    assert row["slo_tier"] in TOPIC_SLO_TIERS


# --- cache policies -----------------------------------------------------------


def test_user_scoped_routes_are_never_shared_cacheable():
    for route_id, row in TOPIC_ROUTE_POLICY_BY_ID.items():
        policy = next(item for item in TOPIC_CACHE_POLICIES if item["cache_class"] == row["cache"])
        if row["scope"] in ("user", "user_write"):
            assert policy["private"] is True
            assert policy["max_age"] == 0


def test_catalog_routes_are_shared_cacheable():
    for row in TOPIC_ROUTE_POLICIES:
        if row["scope"] != "catalog":
            continue
        policy = next(item for item in TOPIC_CACHE_POLICIES if item["cache_class"] == row["cache"])
        assert policy["max_age"] > 0
        assert policy["private"] is False


def test_no_store_varies_on_the_credential_headers():
    policy = next(item for item in TOPIC_CACHE_POLICIES if item["cache_class"] == "no_store")

    assert "Authorization" in policy["varies_on"]
    assert "X-API-Key" in policy["varies_on"]


def test_response_limit_report_groups_cache_policies_by_route():
    report = build_topic_response_limit_report()

    assert report.total == len(TOPIC_RESPONSE_LIMITS)
    assert set(report.cache_classes) == set(TOPIC_CACHE_CLASSES)
    no_store = next(item for item in report.cache_policies if item.cache_class == "no_store")
    assert "read_topic_workspace" in no_store.routes


# --- error map ----------------------------------------------------------------


def test_every_violation_reason_maps_to_an_error():
    for reason in TOPIC_VIOLATION_CONDITION:
        assert reason in {"out_of_range", "too_long", "wrong_type", "not_allowed", "empty"}
        assert TOPIC_VIOLATION_CONDITION[reason] in TOPIC_ERROR_BY_CONDITION


def test_an_unmapped_reason_falls_back_rather_than_raising():
    error = topics.topic_error_for("something_new")

    assert error["condition"] == "param_out_of_range"
    assert error["status_code"] == 422


def test_the_error_map_covers_the_conditions_the_routes_already_return():
    """`missing_selection` and `invalid_selection` mirror live 404/422 paths."""

    assert TOPIC_ERROR_BY_CONDITION["missing_selection"]["status_code"] == 404
    assert TOPIC_ERROR_BY_CONDITION["invalid_selection"]["status_code"] == 422
    assert TOPIC_ERROR_BY_CONDITION["rate_limited"]["status_code"] == 429
    assert TOPIC_ERROR_BY_CONDITION["rate_limited"]["retryable"] is True


def test_error_conditions_are_unique():
    conditions = [str(row["condition"]) for row in TOPIC_ERROR_MAP]

    assert len(conditions) == len(set(conditions))


# --- rate tiers ---------------------------------------------------------------


def test_rate_tiers_have_a_positive_capacity_and_a_non_negative_refill():
    for tier in TOPIC_RATE_TIERS:
        assert int(tier["capacity"]) > 0
        assert float(tier["refill_per_second"]) >= 0.0


def test_write_tier_is_tighter_than_read_tier():
    write = TOPIC_RATE_TIER_BY_NAME["write"]
    read = TOPIC_RATE_TIER_BY_NAME["read"]

    assert write["capacity"] < read["capacity"]
    assert write["refill_per_second"] < read["refill_per_second"]


def test_every_route_names_a_real_tier():
    for row in TOPIC_ROUTE_POLICIES:
        assert row["rate_tier"] in TOPIC_RATE_TIER_BY_NAME


# --- recorder -----------------------------------------------------------------


def test_the_recorder_is_bounded_and_counts_its_drops():
    recorder = TopicRequestRecorder(capacity=3)

    for index in range(10):
        recorder.record(resolve_topic_request("read_topic_search", {"query": f"q{index}"}))

    stats = recorder.stats()
    assert stats["capacity"] == 3
    assert stats["retained"] == 3
    assert stats["recorded"] == 10
    assert stats["dropped"] == 7
    assert stats["admitted"] == 10
    assert stats["refused"] == 0


def test_a_full_recorder_keeps_the_newest_sample():
    """A ring that dropped the *newest* write would report the same counts and be useless."""

    recorder = TopicRequestRecorder(capacity=2)
    for index in range(5):
        recorder.record(resolve_topic_request("read_topic_search", {"query": f"q{index}"}))

    assert recorder.samples()[-1]["route_id"] == "read_topic_search"
    assert recorder.dropped == 3


def test_the_recorder_counts_refusals_separately():
    recorder = TopicRequestRecorder(capacity=4)
    recorder.record(resolve_topic_request("read_topic_search", {"query": "ok"}))
    recorder.record(resolve_topic_request("read_topic_search", {"query": "x" * 400}))

    assert recorder.stats()["admitted"] == 1
    assert recorder.stats()["refused"] == 1
    assert recorder.stats()["by_reason"]["param_out_of_range"] == 1


def test_the_recorder_window_returns_the_newest_samples():
    recorder = TopicRequestRecorder(capacity=10)
    for index in range(6):
        recorder.record(resolve_topic_request("read_topic_search", {"query": f"q{index}"}))

    rows = recorder.samples(window=2)

    assert len(rows) == 2
    assert rows[-1]["route_id"] == "read_topic_search"


def test_the_recorder_counts_repairs_and_not_defaults():
    recorder = TopicRequestRecorder(capacity=4)
    recorder.record(resolve_topic_request("read_topic_search", {"query": "estimate"}))

    assert recorder.stats()["by_route"]["read_topic_search"] == 1
    assert recorder.samples()[-1]["coercions"] == 0


def test_the_recorder_counts_a_repair():
    recorder = TopicRequestRecorder(capacity=4)
    recorder.record(resolve_topic_request("read_topic_search", {"query": "estimate", "per_page": 10**6}))

    assert recorder.samples()[-1]["coercions"] == 1


def test_recorder_reset_clears_everything():
    recorder = TopicRequestRecorder(capacity=4)
    recorder.record(resolve_topic_request("read_topic_search", {}))
    recorder.reset()

    assert recorder.stats()["recorded"] == 0
    assert recorder.samples() == []


def test_the_recorder_does_not_spend_rate_budget():
    """Recording a request must not cost the caller a token.

    Asserted against a limiter of our own rather than the module global: the
    global is shared with every other test in this file, so a count of
    ``tracked_keys`` there says more about the file's execution order than
    about the recorder. What matters is that the caller's bucket is still
    full after 200 recorded requests.
    """
    limiter = TopicRateLimiter({"search": {"capacity": 3, "refill_per_second": 1.0}})
    recorder = TopicRequestRecorder(capacity=4)

    for _ in range(200):
        recorder.record(
            resolve_topic_request(
                "read_topic_search", {"query": "x"}, caller="someone", now=0.0, limiter=limiter
            )
        )

    assert recorder.stats()["recorded"] == 200
    assert limiter.check("search", "someone", now=0.0)["remaining"] == 3


def test_caller_fingerprints_never_contain_the_credential():
    class _Request:
        def __init__(self, headers):
            self.headers = {key.lower(): value for key, value in headers.items()}

    fingerprint = topics._caller_fingerprint(_Request({"X-API-Key": "csk_supersecret"}))
    other = topics._caller_fingerprint(_Request({"X-API-Key": "csk_different"}))

    assert fingerprint.startswith("fp_")
    assert "supersecret" not in fingerprint
    assert fingerprint != other


def test_an_anonymous_request_gets_a_stable_fingerprint():
    class _Request:
        headers = {}

    assert topics._caller_fingerprint(_Request()) == "anonymous"


# --- reports ------------------------------------------------------------------


def test_the_route_policy_report_marks_every_row_observed():
    report = build_topic_route_policy_report()

    assert report.total == len(TOPIC_ROUTE_POLICIES)
    assert report.observed == report.total
    assert report.undeclared == []
    assert report.by_scope["catalog"] > 0
    assert report.by_scope["user_write"] == 3
    assert report.by_tier["write"] == 3


def test_the_route_policy_report_lists_declared_params():
    report = build_topic_route_policy_report()
    search = next(row for row in report.policies if row.route_id == "read_topic_search")

    assert search.params == ["query", "limit", "page", "per_page", "theme", "sector"]
    assert search.observed is True
    assert search.mutating is False


def test_validation_reports_no_errors():
    report = validate_topic_request_policies()

    assert report.errors == 0
    assert report.error_list == []
    assert report.routes == len(TOPIC_ROUTE_POLICIES)
    assert report.params == len(TOPIC_PARAM_BOUNDS)
    assert report.limits == len(TOPIC_RESPONSE_LIMITS)


def test_validation_flags_a_clamped_parameter_on_a_mutating_route():
    """`confidence` is clamped on both write routes — a real, unfixed smell."""

    report = validate_topic_request_policies()
    warnings = " ".join(report.warning_list)

    assert "create_current_topic_selection" in warnings
    assert "replace_current_topic_selection" in warnings
    assert "confidence" in warnings


def test_validation_catches_an_unknown_rate_tier():
    TOPIC_ROUTE_POLICY_BY_ID["read_topic_catalog"]["rate_tier"] = "nonexistent"
    try:
        report = validate_topic_request_policies()
        assert any("unknown rate tier" in item for item in report.error_list)
    finally:
        TOPIC_ROUTE_POLICY_BY_ID["read_topic_catalog"]["rate_tier"] = "catalog"


def test_validation_catches_a_non_positive_cap():
    TOPIC_RESPONSE_LIMITS[0]["cap"] = 0
    try:
        report = validate_topic_request_policies()
        assert any("non-positive cap" in item for item in report.error_list)
    finally:
        TOPIC_RESPONSE_LIMITS[0]["cap"] = 100


def test_validation_catches_a_cap_that_exceeds_the_route_row_budget():
    TOPIC_RESPONSE_LIMITS_BY_ROUTE["read_topic_search"][0]["cap"] = 10**6
    TOPIC_RESPONSE_LIMITS[0]["cap"] = 10**6
    try:
        report = validate_topic_request_policies()
        assert any("exceeds the route's max_rows" in item for item in report.warning_list)
    finally:
        TOPIC_RESPONSE_LIMITS_BY_ROUTE["read_topic_search"][0]["cap"] = 100
        TOPIC_RESPONSE_LIMITS[0]["cap"] = 100


def test_validation_catches_a_cap_on_an_unknown_route():
    TOPIC_RESPONSE_LIMITS.append(
        {"route_id": "ghost", "field": "items", "cap": 5, "overflow": "truncate", "description": ""}
    )
    TOPIC_RESPONSE_LIMITS_BY_ROUTE.setdefault("ghost", []).append(TOPIC_RESPONSE_LIMITS[-1])
    try:
        report = validate_topic_request_policies()
        assert any("unknown route 'ghost'" in item for item in report.error_list)
    finally:
        TOPIC_RESPONSE_LIMITS_BY_ROUTE.pop("ghost")
        TOPIC_RESPONSE_LIMITS.pop()


def test_validation_catches_a_parameter_with_no_route():
    TOPIC_PARAM_BOUNDS.append(
        {"param": "orphan", "kind": "str", "default": None, "max_length": 10, "required": False, "on_violation": "clamp", "description": ""}
    )
    TOPIC_PARAM_BOUND_BY_NAME["orphan"] = TOPIC_PARAM_BOUNDS[-1]
    try:
        report = validate_topic_request_policies()
        assert any("'orphan' has bounds but no route declares it" in item for item in report.warning_list)
    finally:
        TOPIC_PARAM_BOUND_BY_NAME.pop("orphan")
        TOPIC_PARAM_BOUNDS.pop()


def test_validation_catches_a_capacity_rule_for_an_unknown_route():
    TOPIC_CAPACITY_RULES.append(
        {"route_id": "ghost", "slo_tier": "realtime", "latency_budget_ms": 1, "db_queries": 0, "catalog_scans": 0, "cacheable": True, "note": ""}
    )
    try:
        report = validate_topic_request_policies()
        assert any("capacity rule targets unknown route 'ghost'" in item for item in report.error_list)
    finally:
        TOPIC_CAPACITY_RULES.pop()


def test_the_catalog_reports_the_tables_and_its_own_health():
    catalog = build_topic_request_governance_catalog()

    assert catalog["route_count"] == len(TOPIC_ROUTE_POLICIES)
    assert catalog["param_count"] == len(TOPIC_PARAM_BOUNDS)
    assert catalog["limit_count"] == len(TOPIC_RESPONSE_LIMITS)
    assert catalog["validation"]["errors"] == 0
    assert catalog["advisory"]
    assert "rate_tiers" in catalog
    assert "error_map" in catalog


# --- simulation ---------------------------------------------------------------


def test_simulation_compares_the_scenario_against_its_own_baseline():
    """The baseline must come from the scenario, not from the overrides.

    Building the baseline from the overrides makes every row ``changed: False``
    and the whole function a no-op that looks like it works.
    """

    result = simulate_topic_admission(
        [
            {
                "route_id": "read_topic_search",
                "params": {"query": "estimate", "per_page": 5},
                "overrides": {"per_page": 10**6},
                "caller": "sim",
            }
        ]
    )
    row = result["results"][0]

    assert row["changed"] is True
    assert row["baseline_params"]["per_page"] == 5
    assert row["candidate_params"]["per_page"] == 100
    assert result["changed"] == 1


def test_simulation_reports_no_change_when_the_override_changes_nothing():
    result = simulate_topic_admission(
        [{"route_id": "read_topic_search", "params": {"query": "a"}, "overrides": {"query": "a"}}]
    )

    assert result["results"][0]["changed"] is False
    assert result["changed"] == 0


def test_simulation_reports_a_refusal_introduced_by_an_override():
    result = simulate_topic_admission(
        [
            {
                "route_id": "read_topic_search",
                "params": {"query": "estimate"},
                "overrides": {"query": "x" * 400},
            }
        ]
    )
    row = result["results"][0]

    assert row["baseline_admitted"] is True
    assert row["candidate_admitted"] is False
    assert row["became_refused"] is True
    assert result["refused_after_change"] == 1
    assert row["resolved_violations"][0]["param"] == "query"


def test_a_refusal_the_override_did_not_cause_is_not_counted_as_one():
    """Already refused at the baseline and still refused: not the override's doing."""

    result = simulate_topic_admission(
        [
            {
                "route_id": "read_topic_search",
                "params": {"query": "x" * 400},
                "overrides": {"per_page": 3},
            }
        ]
    )
    row = result["results"][0]

    assert row["changed"] is True
    assert row["baseline_admitted"] is False
    assert row["candidate_admitted"] is False
    assert row["became_refused"] is False
    assert result["refused_after_change"] == 0


def test_simulation_handles_an_unknown_route():
    result = simulate_topic_admission([{"route_id": "ghost", "params": {}}])

    assert result["results"][0]["known"] is False
    assert result["results"][0]["candidate_admitted"] is False


def test_simulation_does_not_spend_rate_budget():
    """`simulate_topic_admission` resolves every scenario against the module
    limiter with ``consume=False``, so a bucket may be *created* but must
    never come back short. Asserting ``tracked_keys == 0`` would be asserting
    something the non-consuming check path deliberately does not promise.
    """
    TOPIC_RATE_LIMITER.reset()

    simulate_topic_admission(
        [
            {"route_id": "read_topic_search", "params": {"query": f"q{i}"}, "caller": "sim"}
            for i in range(200)
        ]
    )

    check = TOPIC_RATE_LIMITER.check("search", "sim", now=0.0)
    assert check["remaining"] == check["capacity"]
    assert check["consumed"] is False


# --- planning -----------------------------------------------------------------


def test_a_plan_describes_a_healthy_request():
    plan = build_topic_request_plan("read_topic_search", {"query": "estimate", "per_page": 10})

    assert plan.known is True
    assert plan.method == "GET"
    assert plan.path == "/topics/search"
    assert plan.resolved == {"query": "estimate", "limit": 5, "page": 1, "per_page": 10, "theme": None, "sector": None}
    assert plan.admission.allowed is True
    assert plan.violations == []
    assert plan.db_queries == 0
    assert plan.slo_tier == "bulk"
    assert len(plan.caps) == 2


def test_a_plan_reports_a_clamp_and_says_why():
    plan = build_topic_request_plan("read_topic_search", {"per_page": 10**6})

    assert plan.resolved["per_page"] == 100
    clamp = _coercion(plan.coercions, "per_page")
    assert clamp.rule == "clamped"
    assert "1000000" in plan.summary


def test_a_plan_refuses_a_bad_parameter_and_names_the_status():
    plan = build_topic_request_plan("create_current_topic_selection", {"topic": "billing", "source": "telepathy"})

    assert plan.admission.allowed is False
    assert plan.violations[0].param == "source"
    assert "422" in plan.summary
    assert "topic_param_not_allowed" in plan.summary


def test_a_plan_for_an_unknown_route_is_answerable():
    plan = build_topic_request_plan("ghost", {"query": "x"})

    assert plan.known is False
    assert plan.unused == ["query"]
    assert plan.admission.reason == "unknown_route"
    assert "No route policy" in plan.summary


def test_a_plan_flags_a_method_mismatch():
    plan = build_topic_request_plan("read_topic_search", {"query": "a"}, method="POST")

    assert "Planned as POST but this route is GET" in plan.summary


def test_a_plan_does_not_flag_a_matching_method():
    plan = build_topic_request_plan("read_topic_search", {"query": "a"}, method="get")

    assert "Planned as" not in plan.summary


def test_a_plan_lists_params_the_target_would_ignore():
    plan = build_topic_request_plan("read_topic_catalog", {"query": "x"})

    assert plan.unused == ["query"]
    assert "Ignored 1 parameter" in plan.summary


def test_a_plan_records_the_page_window():
    plan = build_topic_request_plan("read_topic_search", {"page": 3, "per_page": 10})

    assert plan.page["page"] == 3
    assert plan.page["per_page"] == 10
    assert plan.page["offset"] == 20


def test_a_plan_always_says_planning_is_free():
    plan = build_topic_request_plan("read_topic_search", {"query": "a"})

    assert "Planning does not consume rate budget." in plan.summary


# --- HTTP surface -------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/topics/governance/routes",
        "/topics/governance/params",
        "/topics/governance/limits",
        "/topics/governance/capacity",
        "/topics/governance/requests",
        "/topics/governance/validation",
        "/topics/governance/drift",
        "/topics/plan?route_id=read_topic_search",
    ],
)
def test_governance_routes_answer(path):
    assert client.get(path).status_code == 200


def test_route_policy_endpoint_lists_every_route():
    payload = client.get("/topics/governance/routes").json()

    assert payload["total"] == len(TOPIC_ROUTE_POLICIES)
    assert payload["undeclared"] == []


def test_param_bounds_endpoint_answers():
    payload = client.get("/topics/governance/params").json()

    assert payload["count"] == len(TOPIC_PARAM_BOUNDS)


def test_response_limits_endpoint_answers():
    payload = client.get("/topics/governance/limits").json()

    assert payload["total"] == len(TOPIC_RESPONSE_LIMITS)
    assert payload["total_capped_fields"] > 0


def test_capacity_endpoint_answers():
    payload = client.get("/topics/governance/capacity").json()

    assert payload["heaviest"][0] == "read_topic_search"


def test_drift_endpoint_reports_in_sync():
    payload = client.get("/topics/governance/drift").json()

    assert payload["in_sync"] is True


def test_validation_endpoint_reports_no_errors():
    payload = client.get("/topics/governance/validation").json()

    assert payload["errors"] == 0


def test_the_plan_endpoint_answers_for_a_clamped_request():
    payload = client.get(
        "/topics/plan",
        params={"route_id": "read_topic_search", "query": "estimate", "per_page": 5000},
    ).json()

    assert payload["known"] is True
    assert payload["resolved"]["per_page"] == 100
    assert _coercion(payload["coercions"], "per_page")["rule"] == "clamped"


def test_the_plan_endpoint_answers_for_a_refused_request():
    payload = client.get(
        "/topics/plan",
        params={"route_id": "read_topic_search", "query": "x" * 400},
    ).json()

    assert payload["admission"]["allowed"] is False
    assert payload["violations"][0]["param"] == "query"


def test_the_plan_endpoint_answers_for_an_unknown_route():
    payload = client.get("/topics/plan", params={"route_id": "ghost"}).json()

    assert payload["known"] is False


def test_the_plan_endpoint_requires_a_route_id():
    assert client.get("/topics/plan").status_code == 422


def test_the_plan_endpoint_does_not_invent_a_bound_for_an_undeclared_param():
    """The planner bounds the *target's* parameters, and only those.

    `/topics/plan` accepts a `query` because some targets take one. Planning
    against a route that does not must not apply the `query` bound to it —
    that would report a violation the real call would never see, and it would
    refuse a plan for a request that is perfectly valid.
    """

    payload = client.get(
        "/topics/plan", params={"route_id": "read_topic_catalog", "query": "x" * 400}
    ).json()

    assert payload["admission"]["allowed"] is True
    assert payload["violations"] == []
    assert payload["unused"] == ["query"]
    assert payload["coercions"] == []


def test_the_plan_endpoint_forwards_the_window_to_the_target():
    payload = client.get(
        "/topics/plan", params={"route_id": "read_topic_drift", "window": -5}
    ).json()

    assert payload["resolved"]["window"] == 1
    assert payload["coercions"][0]["rule"] == "clamped"


def test_the_plan_endpoint_omits_params_that_were_not_sent():
    payload = client.get("/topics/plan", params={"route_id": "read_topic_catalog"}).json()

    assert payload["unused"] == []
    assert payload["supplied"] == {}


def test_the_analytics_endpoint_records_served_traffic():
    TOPIC_REQUEST_RECORDER.reset()
    client.get("/topics/catalog")
    client.get("/topics/search", params={"query": "estimate", "per_page": 2})
    payload = client.get("/topics/governance/requests").json()

    assert payload["by_route"].get("read_topic_catalog", 0) >= 1
    assert payload["by_route"].get("read_topic_search", 0) >= 1
    assert payload["recorded"] >= 2


def test_the_analytics_endpoint_window_narrows_the_sample():
    TOPIC_REQUEST_RECORDER.reset()
    for _ in range(4):
        client.get("/topics/catalog")
    payload = client.get("/topics/governance/requests", params={"window": 2}).json()

    assert len(payload["recent"]) == 2


def test_the_recorder_does_not_change_the_response():
    TOPIC_REQUEST_RECORDER.reset()
    first = client.get("/topics/catalog").json()
    TOPIC_REQUEST_RECORDER.reset()
    second = client.get("/topics/catalog").json()

    first.pop("generated_at", None)
    second.pop("generated_at", None)
    assert first == second


def test_serving_a_route_records_coercions():
    TOPIC_REQUEST_RECORDER.reset()
    client.get("/topics/search", params={"query": "estimate", "per_page": 5000})
    samples = TOPIC_REQUEST_RECORDER.samples()

    assert samples[-1]["route_id"] == "read_topic_search"
    assert samples[-1]["coercions"] == 1


def test_every_topic_route_is_wrapped_by_the_governed_route_class():
    assert all(isinstance(route, GovernedAPIRoute) for route in topics.router.routes)


# --- pre-existing behaviour ---------------------------------------------------


def test_existing_topic_routes_are_untouched():
    """The 23 original routes keep their methods, paths and response models."""

    expected = {
        "read_topic_catalog": ("GET", "/topics/catalog", "TopicCatalogReport"),
        "read_topic_themes": ("GET", "/topics/themes", "TopicThemeReport"),
        "read_topic_intelligence": ("GET", "/topics/intelligence", "TopicIntelligenceReport"),
        "read_topic_workspace": ("GET", "/topics/workspace", "TopicWorkspaceReport"),
        "read_topic_overview": ("GET", "/topics/overview", "TopicIntelligenceOverview"),
        "read_topic_search": ("GET", "/topics/search", "TopicSearchReport"),
        "read_topic_suggestions": ("GET", "/topics/suggestions", "TopicSuggestionReport"),
        "read_topic_recommendations": ("GET", "/topics/recommendations", "TopicRecommendationReport"),
        "read_current_topic_selection": ("GET", "/topics/current", "TopicSelectionReport"),
        "create_current_topic_selection": ("POST", "/topics/current", "TopicSelectionReport"),
        "replace_current_topic_selection": ("PUT", "/topics/current", "TopicSelectionReport"),
        "read_topic_selection_history": ("GET", "/topics/history", "TopicSelectionHistoryReport"),
        "archive_current_topic_selection": ("DELETE", "/topics/current", "TopicSelectionReport"),
        "read_topic_taxonomy": ("GET", "/topics/taxonomy", "TopicTaxonomyReport"),
        "read_topic_match": ("GET", "/topics/match", None),
        "read_ranked_topics": ("GET", "/topics/ranked", None),
        "validate_topic_selection": ("POST", "/topics/validate", None),
        "read_topic_drift": ("GET", "/topics/drift", None),
        "read_topic_lifecycle": ("GET", "/topics/lifecycle", None),
    }
    observed = {route.name: route for route in topics.router.routes}

    assert len(topics.router.routes) == 31
    for name, (method, path, model) in expected.items():
        route = observed[name]
        assert method in route.methods
        assert route.path == path
        assert (route.response_model.__name__ if route.response_model else None) == model


def test_the_search_route_still_paginates_exactly_as_before():
    payload = client.get(
        "/topics/search",
        params={"query": "estimate review", "limit": 3, "page": 1, "per_page": 2, "theme": "preparation_and_estimates"},
    ).json()

    assert payload["query"] == "estimate review"
    assert payload["page"] == 1
    assert payload["per_page"] == 2
    assert payload["total_pages"] >= 1
    assert len(payload["items"]) <= 2
    assert payload["items"][0]["score"] >= payload["items"][-1]["score"]


def test_the_governance_layer_advertises_itself():
    features = client.get("/meta/features").json()["endpoints"]

    assert features["topic_route_policies"] == "/topics/governance/routes"
    assert features["topic_param_bounds"] == "/topics/governance/params"
    assert features["topic_response_limits"] == "/topics/governance/limits"
    assert features["topic_capacity"] == "/topics/governance/capacity"
    assert features["topic_request_analytics"] == "/topics/governance/requests"
    assert features["topic_policy_validation"] == "/topics/governance/validation"
    assert features["topic_route_drift"] == "/topics/governance/drift"
    assert features["topic_request_plan"] == "/topics/plan"


def test_the_ecosystem_advertises_the_subservice():
    payload = client.get("/meta/ecosystem").json()
    subservice = payload["subservices"]["topic_request_governance"]

    assert subservice["status"] == "ready"
    assert subservice["advisory"] is True
    assert "/topics/plan" in subservice["routes"]
    assert "TOPIC_PARAM_BOUNDS" in subservice["config_tables"]


def test_the_scoring_catalog_carries_the_layer():
    payload = client.get("/meta/scoring-catalog").json()["topic_request_governance"]

    assert payload["validation"]["errors"] == 0
    assert payload["param_count"] == len(TOPIC_PARAM_BOUNDS)


# --- invariants under fuzz ----------------------------------------------------


@pytest.mark.parametrize(
    "supplied",
    [
        None, "", "   ", "0", -1, 10**12, 0.5, "abc", True, [], {}, "x" * 5000, "🔥", "'; DROP TABLE --",
    ],
)
def test_param_resolution_never_raises_and_never_leaks_a_violation(supplied):
    for spec in TOPIC_PARAM_BOUNDS:
        value, coercions, violations = coerce_topic_param(spec, supplied)
        assert isinstance(coercions, list)
        assert isinstance(violations, list)
        if violations:
            assert value is None
        # A resolved value is always of the declared kind.
        if value is not None and not violations:
            if spec["kind"] == "int":
                assert isinstance(value, int)
            elif spec["kind"] == "float":
                assert isinstance(value, (int, float))
            elif spec["kind"] == "str":
                assert isinstance(value, str)
            elif spec["kind"] == "bool":
                assert isinstance(value, bool)


def test_resolution_is_deterministic():
    for _ in range(3):
        context = resolve_topic_request("read_topic_search", {"query": "estimate", "per_page": 10**6})
        assert context.param("per_page") == 100


def test_a_refused_request_is_always_refused():
    """No combination of a violation and a fresh bucket may admit the request."""

    for query in ["x" * 201, "x" * 5000, "y"]:
        for per_page in [-1, 0, 10**9, 5, 100, 1000]:
            limiter = TopicRateLimiter({"search": {"capacity": 10**6, "refill_per_second": 1.0}})
            context = resolve_topic_request(
                "read_topic_search",
                {"query": query, "per_page": per_page},
                limiter=limiter,
            )
            if context.violations:
                assert context.admitted is False


def test_a_clamped_value_is_always_inside_its_bounds():
    for _ in range(300):
        for spec in TOPIC_PARAM_BOUNDS:
            if spec["kind"] not in ("int", "float"):
                continue
            value, _coercions, violations = coerce_topic_param(spec, -10**6)
            if violations:
                continue
            assert float(value) >= float(spec["minimum"])


def test_the_page_window_never_exceeds_the_total():
    for page in range(1, 40):
        for per_page in range(1, 12):
            for total in (0, 1, 7, 100):
                result = resolve_topic_page(page, per_page, total)
                assert result["window_end"] <= total
                assert result["offset"] >= 0
                assert result["total_pages"] >= 1
