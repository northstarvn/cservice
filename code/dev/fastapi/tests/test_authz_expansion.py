"""Tests for the `app/deps.py` authorization-governance expansion.

`deps.py` held the whole authorization path and none of its reasoning. Every
`require_*` factory closed over an inline status literal, there was no scope
vocabulary anywhere, the role a principal held and the capability it could be
granted were unrelated, and the only way to ask "who can reach this?" was to
read a FastAPI signature. The six factories were also bound to no route at all.

This pass added 14 config tables and 19 functions on top of that, and changed
exactly one thing in the request path: the status code each factory raises now
reads `AUTHZ_DENIALS` instead of being written inline. Every value in the table
is the value that was inline.

The point of this file is to pin the parts that are easy to get quietly wrong:

1. **The bytes a client receives did not move.** `ORIG_*` below transcribes the
   pre-expansion literals -- `status.HTTP_403_FORBIDDEN`, the exact `detail`
   dict, the exact key order, the `Retry-After` arithmetic -- and the tests
   re-derive them from the live factories. A status code that quietly becomes
   401, a payload key inserted in the middle, a `Retry-After` computed with a
   different formula: all of them are client-visible, and nothing else in the
   suite would notice.
2. **`USER_ROLES` is untouched.** It is a *user-record* role map and this pass
   had no reason to grow one; the governance table that did grow is
   `ROLE_SCOPE_GRANTS`, which is keyed by *resolved* role.
3. **`ROLE_SCOPE_GRANTS` is never applied.** `require_scopes` reads
   `Principal.scopes` and nothing else. Synthesizing scopes from roles would
   turn a role check into a capability grant and start authorizing calls that
   are denied today, so the table is reported and validated but never merged.
4. **`require_step_up`'s signature default is still the literal `"loa2"`.** A
   config lookup there would change what a caller who omits the argument gets,
   which is the one thing a signature default is.
5. **`hardening` is a proposal and stays one.** Every rule carries a separate
   `hardening` key, `evaluate_authz` only reads it when explicitly asked, and
   every check it produces is marked `enforced: False`.
6. **The rule table is a description, and it is checked against the app.**
   209 live `(method, path)` pairs, 197 paths, every one attributed to a rule,
   every rule's `enforced_by` equal to the dependency the route actually
   resolves. The claim is falsifiable, so a test falsifies it.
7. **`iter_authz_routes` reproduces the OpenAPI path set exactly.** In this
   FastAPI release `route.path` is not the effective path, so a rule table
   written against it would match nothing while looking complete.
8. **`AUTHZ_DENIALS` is proved, not described.** `authz_denial_contract`
   invokes each real factory and compares what came out.
9. **A validator that raises is not a validator.** Malformed tables produce
   error strings, never tracebacks.

The original surface is pinned throughout: the six `require_*` factories (both
the deny path and the allow path), `require_tenant`, `require_rate_limit`,
`build_deps_catalog`, `roles_for_user`, `_unauthorized`, and the five
`get_current_*` dependencies.
"""
import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import cell_matrix, main, security
from app import deps
from app import deps as D
from app.deps import (
    AUTHZ_CHECK_DENIALS,
    AUTHZ_CHECK_ORDER,
    AUTHZ_DENIALS,
    AUTHZ_DENIAL_BY_KIND,
    AUTHZ_DENIAL_KINDS,
    AUTHZ_DEPENDENCY_NAMES,
    AUTHZ_EXPOSURE_RANK,
    AUTHZ_EXPOSURES,
    AUTHZ_FACTORY_NAMES,
    AUTHZ_FACTORY_PREFIXES,
    AUTHZ_FALLBACK_RULE_ID,
    AUTHZ_OPS,
    AUTHZ_PROBES,
    AUTHZ_PROBE_NAMES,
    AUTHZ_ROLE_SERVICE_ACCOUNT,
    AUTHZ_RULES,
    AUTHZ_RULE_BY_ID,
    AUTHZ_RULE_IDS,
    AUTHZ_VERSION,
    RATE_LIMITER,
    RATE_TIERS,
    RATE_TIER_BY_NAME,
    RATE_TIER_NAMES,
    ROLE_SCOPE_GRANTS,
    SCOPE_BY_NAME,
    SCOPE_CATALOG,
    SCOPE_NAMES,
    STEP_UP_LEVELS,
    STEP_UP_RANKS,
    STEP_UP_RANK_BY_LEVEL,
    USER_ROLES,
    Principal,
    authz_coverage_report,
    authz_denial_contract,
    authz_denial_trace,
    authz_drift_report,
    authz_hardening_backlog,
    authz_probe,
    authz_route_inventory,
    authz_rule_sample_path,
    authz_simulation_report,
    build_authz_catalog,
    build_deps_catalog,
    evaluate_authz,
    implied_scopes,
    match_authz_rule,
    rate_tier_limiter,
    route_authz_dependencies,
    route_authz_facts,
    require_cell_access,
    require_rate_limit,
    require_rate_tier,
    require_public_rate_tier,
    require_roles,
    require_scopes,
    require_step_up,
    require_tenant,
    scope_satisfies,
    validate_authz,
)
from app.routers import users

# ===========================================================================
# The pre-expansion literals, transcribed.
#
# Every one of these was an inline value in the code before this pass. The
# status codes are the FastAPI constants that were used; the payloads are the
# dicts that were written. Nothing here reads the new tables.
# ===========================================================================

ORIG_STATUS_401 = 401
ORIG_STATUS_403 = 403
ORIG_STATUS_429 = 429

ORIG_UNAUTHORIZED = {
    "status_code": ORIG_STATUS_401,
    "detail": "Could not validate credentials",
    "headers": {"WWW-Authenticate": "Bearer"},
}

ORIG_USER_ROLES = {
    "customer": ("owner",),
    "agent": ("agent",),
    "auditor": ("auditor",),
    "admin": ("admin", "agent", "auditor", "owner"),
}

# The step-up default was a signature default, not a table lookup.
ORIG_REQUIRE_STEP_UP_DEFAULT = "loa2"
ORIG_REQUIRE_STEP_UP_NAME = "require_step_up_loa2"
ORIG_REQUIRE_SCOPES_NAME = "require_scopes_audit_read"
ORIG_REQUIRE_CELL_NAME = "require_cell_customer_profile_full_name"
ORIG_REQUIRE_ROLES_NAME = "require_roles_admin"
ORIG_RATE_LIMITER_NAME = "require_rate_limit"


# ===========================================================================
# 1. The surface: tables exist, indexes agree with them
# ===========================================================================


def test_the_version_is_declared_and_stamped():
    assert AUTHZ_VERSION == "authz_governance_v1"
    assert build_authz_catalog()["version"] == AUTHZ_VERSION


def test_the_fourteen_config_tables_are_present_and_non_empty():
    tables = {
        "SCOPE_CATALOG": SCOPE_CATALOG,
        "ROLE_SCOPE_GRANTS": ROLE_SCOPE_GRANTS,
        "AUTHZ_DENIALS": AUTHZ_DENIALS,
        "STEP_UP_RANKS": STEP_UP_RANKS,
        "RATE_TIERS": RATE_TIERS,
        "AUTHZ_RULES": AUTHZ_RULES,
        "AUTHZ_PROBES": AUTHZ_PROBES,
        "AUTHZ_CHECK_ORDER": AUTHZ_CHECK_ORDER,
        "AUTHZ_CHECK_DENIALS": AUTHZ_CHECK_DENIALS,
        "AUTHZ_EXPOSURE_RANK": AUTHZ_EXPOSURE_RANK,
        "AUTHZ_DEPENDENCY_NAMES": AUTHZ_DEPENDENCY_NAMES,
        "AUTHZ_FACTORY_PREFIXES": AUTHZ_FACTORY_PREFIXES,
        "AUTHZ_OPS": AUTHZ_OPS,
    }
    for name, table in tables.items():
        assert table, name


def test_the_derived_indexes_match_their_tables():
    assert SCOPE_BY_NAME == {str(row["scope"]): row for row in SCOPE_CATALOG}
    assert SCOPE_NAMES == tuple(sorted(SCOPE_BY_NAME))
    assert AUTHZ_DENIAL_BY_KIND == {str(row["kind"]): row for row in AUTHZ_DENIALS}
    assert AUTHZ_DENIAL_KINDS == tuple(sorted(AUTHZ_DENIAL_BY_KIND))
    assert RATE_TIER_BY_NAME == {str(row["tier"]): row for row in RATE_TIERS}
    assert RATE_TIER_NAMES == tuple(sorted(RATE_TIER_BY_NAME))
    assert STEP_UP_RANK_BY_LEVEL == {
        str(row["level"]): int(row["rank"]) for row in STEP_UP_RANKS
    }
    assert AUTHZ_RULE_BY_ID == {str(row["rule_id"]): row for row in AUTHZ_RULES}
    assert AUTHZ_RULE_IDS == tuple(str(row["rule_id"]) for row in AUTHZ_RULES)
    assert AUTHZ_PROBE_NAMES == tuple(str(row["probe"]) for row in AUTHZ_PROBES)


def test_the_exposure_and_check_orders_are_ordered_not_alphabetical():
    assert AUTHZ_EXPOSURES == (
        "public",
        "authenticated",
        "self_service",
        "policy",
        "admin",
    )
    # `self_service` shares a rank with `authenticated` deliberately: it is a
    # scope of effect (a customer acting on their own account), not a stricter
    # gate. Giving it a higher rank would make a plain session look like it
    # implies more than it does.
    assert AUTHZ_EXPOSURE_RANK == {
        "public": 0,
        "authenticated": 1,
        "self_service": 1,
        "policy": 2,
        "admin": 3,
    }
    # Check order is the contract: it decides which denial a caller with several
    # problems receives, so it is fixed and not sorted.
    assert AUTHZ_CHECK_ORDER == (
        "authenticated",
        "admin",
        "scopes",
        "roles",
        "step_up",
        "cell",
        "rate_limit",
        "tenant",
        "policy_tier",
    )
    assert list(AUTHZ_CHECK_ORDER) != sorted(AUTHZ_CHECK_ORDER)


def test_policy_tier_is_the_one_check_with_no_declared_denial():
    # `get_current_customer_policy_score` only attaches a score; the tier gate
    # runs in `get_current_policy_or_admin_user` or in the route body, so no
    # factory raises a denial for it. The engine must not invent one.
    assert "policy_tier" in AUTHZ_CHECK_ORDER
    assert "policy_tier" not in AUTHZ_CHECK_DENIALS
    assert set(AUTHZ_CHECK_DENIALS) == set(AUTHZ_CHECK_ORDER) - {"policy_tier"}


def test_the_fallback_rule_is_last_and_named_in_one_place():
    assert AUTHZ_RULE_IDS[-1] == AUTHZ_FALLBACK_RULE_ID
    assert AUTHZ_OPS["catch_all_rule_id"] == AUTHZ_FALLBACK_RULE_ID
    assert AUTHZ_FALLBACK_RULE_ID == "catch_all"


def test_every_derived_tuple_is_frozen():
    for name in (
        "SCOPE_NAMES",
        "AUTHZ_DENIAL_KINDS",
        "RATE_TIER_NAMES",
        "STEP_UP_LEVELS",
        "AUTHZ_RULE_IDS",
        "AUTHZ_PROBE_NAMES",
        "AUTHZ_EXPOSURES",
        "AUTHZ_CHECK_ORDER",
        "AUTHZ_DEPENDENCY_NAMES",
        "AUTHZ_FACTORY_NAMES",
    ):
        assert isinstance(getattr(D, name), tuple), name


def test_the_factory_prefix_map_covers_exactly_the_seven_factories():
    assert AUTHZ_FACTORY_NAMES == (
        "require_cell_access",
        "require_public_rate_limit",
        "require_rate_limit",
        "require_roles",
        "require_scopes",
        "require_step_up",
        "require_tenant",
    )
    # A factory generates a fresh closure per call site, so a route can only be
    # recognised by prefix. Missing one is how a real cell check gets reported
    # as "no authorization at all".
    for factory in AUTHZ_FACTORY_NAMES:
        assert factory in set(AUTHZ_FACTORY_PREFIXES.values()), factory
    assert "require_tenant" in AUTHZ_FACTORY_PREFIXES


def test_the_service_account_role_is_declared_because_nothing_else_declares_it():
    # `_api_key_principal` mints this role. It is a *resolved* role, so it is
    # absent from USER_ROLES (keyed by user-record role) and from
    # cell_matrix.ROLE_GROUPS (it grants no cell).
    assert AUTHZ_ROLE_SERVICE_ACCOUNT == "service_account"
    assert AUTHZ_ROLE_SERVICE_ACCOUNT not in {r for roles in USER_ROLES.values() for r in roles}
    assert AUTHZ_ROLE_SERVICE_ACCOUNT not in {
        r for roles in cell_matrix.ROLE_GROUPS.values() for r in roles
    }
    assert AUTHZ_ROLE_SERVICE_ACCOUNT in D._known_roles()


# ===========================================================================
# 2. USER_ROLES is untouched
# ===========================================================================


def test_user_roles_is_exactly_what_it_was():
    assert USER_ROLES == ORIG_USER_ROLES
    assert D.DEFAULT_USER_ROLE == "customer"


def test_roles_for_user_still_expands_the_user_record_role():
    class _User:
        def __init__(self, role):
            self.role = role

    assert deps.roles_for_user(_User("customer")) == frozenset({"owner"})
    assert deps.roles_for_user(_User("admin")) == frozenset({"admin", "agent", "auditor", "owner"})
    assert deps.roles_for_user(_User(None)) == frozenset({"owner"})
    # An unknown role is passed through rather than dropped.
    assert deps.roles_for_user(_User("nonexistent")) == frozenset({"nonexistent"})
    assert deps.roles_for_user(None) == frozenset({"owner"})


def test_known_roles_is_resolved_roles_not_user_record_roles():
    known = D._known_roles()
    assert "admin" in known and "agent" in known and "auditor" in known and "owner" in known
    # `customer` is a user-record role that expands to `owner`. Treating it as
    # grantable would invent a second spelling of the same thing.
    assert "customer" not in known


# ===========================================================================
# 3. The denial contract: the bytes a client receives
# ===========================================================================


def test_the_unauthorized_exception_is_byte_identical():
    exc = deps._unauthorized()
    assert exc.status_code == ORIG_UNAUTHORIZED["status_code"]
    assert exc.detail == ORIG_UNAUTHORIZED["detail"]
    assert dict(exc.headers or {}) == ORIG_UNAUTHORIZED["headers"]


def _principal(**kwargs) -> Principal:
    kwargs.setdefault("subject", "probe")
    return Principal(**kwargs)


async def _deny(awaitable):
    """Await a dependency call that must deny, and hand back the exception."""
    with pytest.raises(HTTPException) as caught:
        await awaitable
    return caught.value


def test_require_scopes_denial_is_byte_identical():
    async def _run():
        principal = _principal(roles=frozenset({"owner"}))
        exc = await _deny(require_scopes("authz:probe")(principal))
        assert exc.status_code == ORIG_STATUS_403
        assert exc.detail == {
            "error": "insufficient_scope",
            "required": ["authz:probe"],
            "missing": ["authz:probe"],
            "granted": [],
        }
        assert tuple(exc.detail) == ("error", "required", "missing", "granted")
        assert exc.headers is None

    asyncio.run(_run())


def test_require_scopes_allows_a_principal_that_has_the_scope():
    async def _run():
        principal = _principal(roles=frozenset({"owner"}), scopes=frozenset({"read"}))
        returned = await require_scopes("read:audit")(principal)
        assert returned is principal

    asyncio.run(_run())


def test_require_cell_access_denial_is_byte_identical():
    async def _run():
        weak = _principal(roles=frozenset({"auditor"}))
        exc = await _deny(
            require_cell_access("customer_profile", "full_name", minimum="write")(weak)
        )
        assert exc.status_code == ORIG_STATUS_403
        assert exc.detail == {
            "error": "cell_access_denied",
            "resource": "customer_profile",
            "cell": "full_name",
            "required": "write",
            "reason": "granted by the base matrix",
            "requires_justification": False,
        }
        assert tuple(exc.detail) == (
            "error",
            "resource",
            "cell",
            "required",
            "reason",
            "requires_justification",
        )

    asyncio.run(_run())


def test_require_cell_access_allows_a_principal_at_the_required_rank():
    async def _run():
        owner = _principal(roles=frozenset({"owner"}))
        returned = await require_cell_access("customer_profile", "full_name", minimum="write")(
            owner
        )
        assert returned is owner

    asyncio.run(_run())


def test_require_step_up_denial_is_byte_identical():
    async def _run():
        plain = _principal(roles=frozenset({"owner"}))
        exc = await _deny(require_step_up("loa3")(plain))
        assert exc.status_code == ORIG_STATUS_403
        assert exc.detail == {
            "error": "step_up_required",
            "required_level": "loa3",
            "presented_level": "none",
        }
        assert tuple(exc.detail) == ("error", "required_level", "presented_level")

    asyncio.run(_run())


def test_require_step_up_allows_a_matching_assurance_level():
    async def _run():
        strong = _principal(claims={"acr": "loa3", "amr": ["hwk"]})
        returned = await require_step_up("loa2")(strong)
        assert returned is strong

    asyncio.run(_run())


def test_require_roles_denial_is_byte_identical():
    async def _run():
        plain = _principal(roles=frozenset({"owner"}))
        exc = await _deny(require_roles("admin")(plain))
        assert exc.status_code == ORIG_STATUS_403
        assert exc.detail == {
            "error": "role_required",
            "required_any": ["admin"],
            "roles": ["owner"],
        }
        assert tuple(exc.detail) == ("error", "required_any", "roles")

    asyncio.run(_run())


def test_require_roles_allows_any_one_of_the_named_roles():
    async def _run():
        principal = _principal(roles=frozenset({"auditor"}))
        returned = await require_roles("admin", "auditor")(principal)
        assert returned is principal

    asyncio.run(_run())


def test_require_tenant_denial_is_byte_identical():
    async def _run():
        principal = _principal(roles=frozenset({"owner"}), tenant_id="tenant-b")
        exc = await _deny(require_tenant(deps._probe_request("tenant-a"), principal))
        assert exc.status_code == ORIG_STATUS_403
        assert exc.detail == {
            "error": "tenant_mismatch",
            "requested": "tenant-a",
            "principal_tenant": "tenant-b",
        }
        assert tuple(exc.detail) == ("error", "requested", "principal_tenant")

    asyncio.run(_run())


def test_require_tenant_returns_the_tenant_and_binds_it_to_the_request():
    from app.tenant_router import DEFAULT_TENANT

    async def _run():
        principal = _principal(roles=frozenset({"owner"}))
        request = deps._probe_request(DEFAULT_TENANT)
        returned = await require_tenant(request, principal)
        assert returned == DEFAULT_TENANT
        assert request.state.tenant_id == DEFAULT_TENANT

    asyncio.run(_run())


def test_an_admin_may_cross_tenants():

    async def _run():
        admin = _principal(roles=frozenset({"admin"}), tenant_id="tenant-a")
        returned = await require_tenant(deps._probe_request("tenant-b"), admin)
        assert returned == "tenant-b"

    asyncio.run(_run())


def test_require_rate_limit_denial_is_byte_identical():
    async def _run():
        dependency = require_rate_limit(capacity=1, refill_per_second=0.0)
        principal = _principal(subject="rate-probe")
        await dependency(principal)
        exc = await _deny(dependency(principal))
        assert exc.status_code == ORIG_STATUS_429
        assert tuple(exc.detail) == (
            "error",
            "allowed",
            "remaining",
            "capacity",
            "refill_per_second",
        )
        # The decision dict is spliced in whole, after the error key. Note the
        # refill is 1.0 and not the 0.0 that was passed: ``refill or 1.0``
        # treats a zero argument as "unspecified", which is the original
        # behaviour and is why a zero refill is not reachable from this factory.
        assert exc.detail == {
            "error": "rate_limited",
            "allowed": False,
            "remaining": 0,
            "capacity": 1,
            "refill_per_second": 1.0,
        }
        # The original arithmetic: str(int(1 / refill_per_second)).
        assert dict(exc.headers or {}) == {"Retry-After": "1"}

    asyncio.run(_run())


def test_a_zero_refill_reached_through_the_clamp_still_yields_retry_after_one():
    # ``TokenBucketLimiter`` clamps a negative refill up to 0.0, which is the
    # only way this factory can hold a genuinely empty bucket.
    async def _run():
        dependency = require_rate_limit(capacity=1, refill_per_second=-5.0)
        principal = _principal(subject="clamped-probe")
        await dependency(principal)
        exc = await _deny(dependency(principal))
        assert exc.detail["refill_per_second"] == 0.0
        assert dict(exc.headers or {}) == {"Retry-After": "1"}

    asyncio.run(_run())


def test_require_rate_limit_retry_after_follows_the_original_formula():
    async def _run():
        dependency = require_rate_limit(capacity=1, refill_per_second=0.5)
        principal = _principal(subject="retry-probe")
        await dependency(principal)
        exc = await _deny(dependency(principal))
        assert dict(exc.headers or {}) == {"Retry-After": str(int(1 / 0.5))}

    asyncio.run(_run())


def test_require_rate_limit_keeps_its_signature_and_the_env_bucket():
    signature = inspect.signature(require_rate_limit)
    assert list(signature.parameters) == ["capacity", "refill_per_second"]
    assert signature.parameters["capacity"].default is None
    assert signature.parameters["refill_per_second"].default is None
    assert require_rate_limit().__name__ == ORIG_RATE_LIMITER_NAME
    # No arguments at all -> the historical env-driven limiter, not a fresh one.
    assert deps.RATE_LIMITER is RATE_LIMITER


def test_the_generated_dependency_names_are_unchanged():
    assert require_scopes("audit:read").__name__ == ORIG_REQUIRE_SCOPES_NAME
    assert require_cell_access("customer_profile", "full_name").__name__ == (
        ORIG_REQUIRE_CELL_NAME
    )
    assert require_roles("admin").__name__ == ORIG_REQUIRE_ROLES_NAME
    assert require_rate_limit().__name__ == ORIG_RATE_LIMITER_NAME


# ===========================================================================
# 4. The signature default that deliberately did not become a config lookup
# ===========================================================================


def test_require_step_up_default_is_still_the_literal():
    signature = inspect.signature(require_step_up)
    assert signature.parameters["level"].default == ORIG_REQUIRE_STEP_UP_DEFAULT
    assert isinstance(signature.parameters["level"].default, str)
    require_step_up.__doc__ and "stays literal" in require_step_up.__doc__


def test_the_configured_default_agrees_with_the_signature_default():
    # The table *documents* the floor; it does not supply it. They must agree,
    # which is what makes the documentation falsifiable.
    assert AUTHZ_OPS["default_step_up_level"] == ORIG_REQUIRE_STEP_UP_DEFAULT
    assert STEP_UP_RANK_BY_LEVEL[ORIG_REQUIRE_STEP_UP_DEFAULT] == 2


def test_require_step_up_name_still_uses_the_closure_level():
    assert require_step_up().__name__ == ORIG_REQUIRE_STEP_UP_NAME
    assert require_step_up("loa3").__name__ == "require_step_up_loa3"
    # Both spellings resolve through the prefix map to the same factory.
    for name in (require_step_up().__name__, require_step_up("loa3").__name__):
        resolved = next(
            factory
            for prefix, factory in AUTHZ_FACTORY_PREFIXES.items()
            if name == prefix or name.startswith(prefix)
        )
        assert resolved == "require_step_up"


# ===========================================================================
# 5. The scope catalog
# ===========================================================================


def test_the_scope_catalog_is_grounded_in_what_the_app_issues():
    self_service = {str(row["scope"]) for row in SCOPE_CATALOG if row.get("self_service")}
    assert self_service == set(users.API_KEY_SELF_SCOPES)


def test_the_wildcard_is_never_self_service():
    wildcard = SCOPE_BY_NAME["*"]
    assert wildcard["self_service"] is False
    assert wildcard["parent"] is None
    assert "*" not in ROLE_SCOPE_GRANTS.get("auditor", ())


def test_every_scope_parent_resolves_and_no_scope_is_its_own_parent():
    for row in SCOPE_CATALOG:
        parent = row.get("parent")
        if parent is not None:
            assert str(parent) in SCOPE_BY_NAME
        assert row.get("parent") != row.get("scope")


def test_scope_satisfies_agrees_with_principal_has_scope_over_the_whole_catalog():
    # Two independent implementations of the same rule. If they ever diverge,
    # the pure engine would answer a different question than the dependency.
    for granted in SCOPE_NAMES:
        principal = Principal(subject="s", scopes=frozenset({granted}))
        for required in SCOPE_NAMES:
            assert scope_satisfies([granted], required) == principal.has_scope(required), (
                granted,
                required,
            )


def test_scope_satisfies_handles_the_empty_and_wildcard_cases():
    assert scope_satisfies([], "read") is False
    assert scope_satisfies(["*"], "anything:at:all") is True
    assert scope_satisfies(["read"], "read:audit") is True
    assert scope_satisfies(["audit"], "read") is False
    # The prefix rule is one-directional: a child does not grant its parent.
    assert scope_satisfies(["read:audit"], "read") is False


# ===========================================================================
# 6. ROLE_SCOPE_GRANTS is advisory and never applied
# ===========================================================================


def test_role_scope_grants_names_only_known_resolved_roles():
    for role in ROLE_SCOPE_GRANTS:
        assert role in D._known_roles(), role


def test_every_granted_scope_is_issuable():
    for role, scopes in ROLE_SCOPE_GRANTS.items():
        for scope in scopes:
            assert str(scope) in SCOPE_BY_NAME, (role, scope)


def test_implied_scopes_is_only_a_question_about_what_could_be_granted():
    assert implied_scopes(["owner"]) == ["bookings", "chat", "read", "write"]
    assert implied_scopes(["auditor"]) == ["audit", "read", "read:audit"]
    assert implied_scopes(["admin"]) == ["*"]
    assert implied_scopes([]) == []
    # Unknown roles imply nothing rather than raising.
    assert implied_scopes(["not-a-role"]) == []


def test_implied_scopes_is_never_merged_into_a_principal():
    principal = _principal(roles=frozenset({"admin"}))
    assert principal.scopes == frozenset()
    assert principal.has_scope("read") is False
    assert principal.has_scope("*") is False
    assert principal.has_all_scopes(("read", "write")) == ["read", "write"]


def test_an_admin_with_no_scopes_is_still_denied_a_scope_requirement():
    # The single most important consequence of keeping the table advisory: if
    # scopes were synthesized from roles, this call would start succeeding.
    async def _run():
        admin = _principal(roles=frozenset({"admin"}))
        exc = await _deny(require_scopes("read")(admin))
        assert exc.status_code == ORIG_STATUS_403
        assert exc.detail["granted"] == []
        assert exc.detail["missing"] == ["read"]

    asyncio.run(_run())


def test_a_wildcard_scope_still_satisfies_everything():
    principal = _principal(roles=frozenset({"admin"}), scopes=frozenset({"*"}))
    assert principal.has_scope("read:audit") is True
    assert principal.has_all_scopes(("read", "audit", "chat")) == []


def test_the_decision_trace_reports_implied_scopes_separately_and_unapplied():
    decision = evaluate_authz(_principal(roles=frozenset({"owner"})), "GET", "/users/me")
    facts = decision["facts"]
    assert facts["scopes"] == []
    assert facts["implied_by_roles"] == ["bookings", "chat", "read", "write"]
    assert facts["implied_by_roles_applied"] is False


# ===========================================================================
# 7. The denial table
# ===========================================================================


def test_the_denial_table_declares_every_denial_the_factories_raise():
    assert set(AUTHZ_DENIAL_KINDS) == {
        "unauthenticated",
        "insufficient_scope",
        "cell_access_denied",
        "step_up_required",
        "role_required",
        "tenant_mismatch",
        "rate_limited",
    }


def test_the_declared_status_codes_are_the_original_literals():
    statuses = {str(row["kind"]): int(row["status"]) for row in AUTHZ_DENIALS}
    assert statuses == {
        "unauthenticated": ORIG_STATUS_401,
        "insufficient_scope": ORIG_STATUS_403,
        "cell_access_denied": ORIG_STATUS_403,
        "step_up_required": ORIG_STATUS_403,
        "role_required": ORIG_STATUS_403,
        "tenant_mismatch": ORIG_STATUS_403,
        "rate_limited": ORIG_STATUS_429,
    }


def test_the_declared_key_order_is_the_payload_key_order():
    for kind, exc_detail in _observed_denial_details().items():
        row = AUTHZ_DENIAL_BY_KIND[kind]
        assert str(row["detail_shape"]) == "object"
        assert tuple(exc_detail) == tuple(row["keys"]), kind


def test_only_the_unauthenticated_denial_has_a_string_detail():
    for row in AUTHZ_DENIALS:
        if str(row["detail_shape"]) == "string":
            assert row["kind"] == "unauthenticated"
            assert row["keys"] == ()
            assert row["text"]
        else:
            assert "error" in tuple(row["keys"])


def test_the_error_strings_stay_the_ones_a_client_matches_on():
    for row in AUTHZ_DENIALS:
        kind = str(row["kind"])
        if kind == "unauthenticated":
            assert row["error"] is None
            continue
        assert row["error"] == kind


def test_each_denial_names_the_factory_that_raises_it():
    for row in AUTHZ_DENIALS:
        factory = str(row["factory"])
        assert callable(globals().get(factory) or getattr(D, factory, None)), factory
    assert AUTHZ_DENIAL_BY_KIND["unauthenticated"]["factory"] == "_unauthorized"
    assert AUTHZ_DENIAL_BY_KIND["rate_limited"]["factory"] == "require_rate_limit"


def _observed_denial_details() -> dict:
    """The detail dict each factory actually raises, for the key-order tests."""
    async def _run():
        weak = _principal(roles=frozenset({"auditor"}))
        unscoped = _principal(roles=frozenset({"owner"}))
        out = {}
        out["insufficient_scope"] = (
            await _deny(require_scopes("authz:probe")(unscoped))
        ).detail
        out["cell_access_denied"] = (
            await _deny(require_cell_access("customer_profile", "full_name", minimum="write")(weak))
        ).detail
        out["step_up_required"] = (await _deny(require_step_up("loa3")(unscoped))).detail
        out["role_required"] = (await _deny(require_roles("authz:no-such-role")(unscoped))).detail
        out["tenant_mismatch"] = (
            await _deny(require_tenant(deps._probe_request(), unscoped))
        ).detail
        limiter = require_rate_limit(capacity=1, refill_per_second=0.0)
        subject = _principal(subject="key-order-probe")
        await limiter(subject)
        out["rate_limited"] = (await _deny(limiter(subject))).detail
        return out

    return asyncio.run(_run())


def test_the_denial_contract_agrees_with_every_factory():
    report = asyncio.run(authz_denial_contract())
    assert report["valid"] is True
    assert report["errors"] == 0
    assert report["error_list"] == []
    assert {row["kind"] for row in report["observed"]} == set(AUTHZ_DENIAL_KINDS)
    assert len(report["observed"]) == 7
    assert all(row["agrees"] for row in report["observed"])


def test_the_denial_status_code_is_sourced_from_the_table(monkeypatch):
    # The status code is the one thing the table *is*: the factory reads it, so
    # changing the row changes the response and the declaration cannot drift.
    # That is the opposite of a payload string, which is why the payload stays
    # inline and is proved instead.
    monkeypatch.setitem(
        AUTHZ_DENIAL_BY_KIND,
        "role_required",
        {**AUTHZ_DENIAL_BY_KIND["role_required"], "status": 401},
    )

    async def _run():
        return await _deny(require_roles("admin")(_principal(roles=frozenset({"owner"}))))

    exc = asyncio.run(_run())
    assert exc.status_code == 401
    # The rest of the payload is untouched by a status change.
    assert exc.detail["error"] == "role_required"
    # And the contract is still satisfied, because both sides moved together.
    assert asyncio.run(authz_denial_contract())["valid"] is True


def test_the_denial_contract_fails_when_the_payload_drifts_from_the_declaration(monkeypatch):
    # The payload strings stayed inline because a client matches on them, so
    # the table can only be *proved* against the code -- not sourced from it.
    # This is the drift the contract exists to catch.
    monkeypatch.setitem(
        AUTHZ_DENIAL_BY_KIND,
        "tenant_mismatch",
        {**AUTHZ_DENIAL_BY_KIND["tenant_mismatch"], "error": "wrong_tenant"},
    )
    report = asyncio.run(authz_denial_contract())
    assert report["valid"] is False
    assert any("tenant_mismatch" in message and "error" in message for message in report["error_list"])


def test_the_denial_contract_fails_when_the_text_of_a_string_denial_drifts(monkeypatch):
    monkeypatch.setitem(
        AUTHZ_DENIAL_BY_KIND,
        "unauthenticated",
        {**AUTHZ_DENIAL_BY_KIND["unauthenticated"], "text": "Please log in"},
    )
    report = asyncio.run(authz_denial_contract())
    assert report["valid"] is False
    assert any("text" in message for message in report["error_list"])


def test_the_denial_contract_fails_when_a_key_is_added_to_the_payload(monkeypatch):
    declared = AUTHZ_DENIAL_BY_KIND["tenant_mismatch"]
    monkeypatch.setitem(
        AUTHZ_DENIAL_BY_KIND, "tenant_mismatch", {**declared, "keys": (*declared["keys"], "hint")}
    )
    report = asyncio.run(authz_denial_contract())
    assert report["valid"] is False
    assert any("keys" in message for message in report["error_list"])


def test_the_denial_contract_reports_a_kind_with_no_probe(monkeypatch):
    # A denial nobody wired a factory for: the contract iterates the kinds, so
    # an unwired row is an error rather than a silently uncovered denial.
    original = list(AUTHZ_DENIALS)
    try:
        AUTHZ_DENIALS.append(
            {
                "kind": "unwired_denial",
                "error": "unwired_denial",
                "text": None,
                "status": 403,
                "detail_shape": "object",
                "keys": ("error",),
                "headers": {},
                "factory": "require_scopes",
            }
        )
        monkeypatch.setattr(
            D, "AUTHZ_DENIAL_KINDS", (*AUTHZ_DENIAL_KINDS, "unwired_denial")
        )
        report = asyncio.run(authz_denial_contract())
        assert report["valid"] is False
        assert any("no probe is wired" in message for message in report["error_list"])
    finally:
        AUTHZ_DENIALS[:] = original
        AUTHZ_DENIAL_BY_KIND.pop("unwired_denial", None)


def test_the_denial_trace_builds_a_denial_without_raising_it():
    trace = authz_denial_trace(
        "insufficient_scope", required=["read"], missing=["read"], granted=[]
    )
    assert trace["known"] is True
    assert trace["status"] == ORIG_STATUS_403
    assert trace["detail_shape"] == "object"
    assert trace["error"] == "insufficient_scope"
    assert trace["keys"] == ("error", "required", "missing", "granted")
    assert trace["detail"] == {
        "error": "insufficient_scope",
        "required": ["read"],
        "missing": ["read"],
        "granted": [],
    }
    assert trace["missing_fields"] == []


def test_a_denial_trace_names_the_fields_it_could_not_fill():
    trace = authz_denial_trace("role_required", required_any=["admin"])
    assert sorted(trace["missing_fields"]) == ["roles"]
    assert trace["detail"] == {
        "error": "role_required",
        "required_any": ["admin"],
        "roles": None,
    }
    # The point: a trace cannot render a payload a client would never receive.
    assert any(value is None for value in trace["detail"].values())


def test_the_string_shaped_denial_trace_carries_its_text():
    trace = authz_denial_trace("unauthenticated")
    assert trace["detail_shape"] == "string"
    assert trace["detail"] == ORIG_UNAUTHORIZED["detail"]
    assert trace["headers"] == ORIG_UNAUTHORIZED["headers"]
    assert trace["error"] is None


def test_an_unknown_denial_kind_is_reported_rather_than_guessed():
    trace = authz_denial_trace("not_a_denial")
    assert trace["known"] is False
    assert trace["status"] == 403
    assert trace["detail"] is None


# ===========================================================================
# 8. Step-up ranks and rate tiers
# ===========================================================================


def test_the_step_up_ranks_equal_the_module_that_enforces_them():
    assert STEP_UP_RANK_BY_LEVEL == dict(security.STEP_UP_RANK)
    assert STEP_UP_LEVELS == tuple(sorted(STEP_UP_RANK_BY_LEVEL, key=STEP_UP_RANK_BY_LEVEL.get))


def test_the_step_up_evidence_equal_the_module_that_mints_it():
    declared = {str(row["level"]): tuple(row["evidence"]) for row in STEP_UP_RANKS if row["evidence"]}
    assert declared == {str(k): tuple(v) for k, v in users.STEP_UP_METHODS.items()}


def test_every_level_is_satisfied_only_by_weaker_or_equal_levels():
    for row in STEP_UP_RANKS:
        level, rank = str(row["level"]), int(row["rank"])
        for satisfied in row.get("satisfies") or ():
            assert STEP_UP_RANK_BY_LEVEL[str(satisfied)] < rank, (level, satisfied)


def test_the_satisfies_sets_match_the_real_predicate():
    for row in STEP_UP_RANKS:
        level = str(row["level"])
        for other, other_rank in STEP_UP_RANK_BY_LEVEL.items():
            claims = {"acr": level}
            expected = other_rank <= int(row["rank"])
            assert security.step_up_satisfied(claims, other) is expected, (level, other)


def test_the_interactive_rate_tier_documents_the_shipped_limiter():
    row = RATE_TIER_BY_NAME["interactive"]
    assert int(row["capacity"]) == RATE_LIMITER.capacity
    assert float(row["refill_per_second"]) == RATE_LIMITER.refill_per_second
    assert (int(row["capacity"]), float(row["refill_per_second"])) == (60, 1.0)


def test_rate_tiers_are_ordered_by_budget_and_name_themselves_explicitly():
    for row in RATE_TIERS:
        assert row["description"]
        assert int(row["capacity"]) >= 1
        assert float(row["refill_per_second"]) >= 0.0
        for method in row.get("applies_to") or ():
            assert method in (D.AUTH_METHOD_USER, D.AUTH_METHOD_API_KEY, D.AUTH_METHOD_DELEGATED)


def test_a_rate_tier_limiter_is_shared_per_tier():
    first = rate_tier_limiter("machine")
    assert rate_tier_limiter("machine") is first
    assert rate_tier_limiter("sensitive") is not first
    assert first.capacity == int(RATE_TIER_BY_NAME["machine"]["capacity"])
    assert first.refill_per_second == float(RATE_TIER_BY_NAME["machine"]["refill_per_second"])


def test_an_unknown_rate_tier_falls_back_instead_of_raising():
    # A typo in a route decorator should be a catalog finding, not a 500.
    fallback = rate_tier_limiter("no-such-tier")
    assert fallback is rate_tier_limiter("interactive")


def test_require_rate_tier_names_its_tier_and_records_whether_it_is_known():
    dependency = require_rate_tier("sensitive")
    assert dependency.__name__ == ORIG_RATE_LIMITER_NAME
    assert dependency.authz_rate_tier == "sensitive"
    assert dependency.authz_rate_tier_known is True
    # The generated name is deliberately the same as its sibling's, so the
    # prefix map still resolves both to require_rate_limit.
    assert any(
        dependency.__name__ == prefix or dependency.__name__.startswith(prefix)
        for prefix in AUTHZ_FACTORY_PREFIXES
    )


def test_require_rate_tier_reports_an_unknown_tier_rather_than_hiding_it():
    dependency = require_rate_tier("nope")
    assert dependency.authz_rate_tier_known is False
    # It still serves, on the fallback bucket, so the route is not broken.
    assert dependency.authz_rate_tier == AUTHZ_OPS["default_rate_tier"]


def test_require_rate_tier_denies_with_the_same_payload_as_its_sibling():
    async def _run():
        dependency = require_rate_tier("sensitive")
        principal = _principal(subject="tier-probe")
        for _ in range(int(RATE_TIER_BY_NAME["sensitive"]["capacity"])):
            await dependency(principal)
        exc = await _deny(dependency(principal))
        assert exc.status_code == ORIG_STATUS_429
        assert exc.detail["error"] == "rate_limited"
        assert exc.detail["capacity"] == int(RATE_TIER_BY_NAME["sensitive"]["capacity"])
        assert "Retry-After" in dict(exc.headers or {})

    asyncio.run(_run())


def test_the_tier_limiter_is_shared_so_a_budget_cannot_be_multiplied():
    # Ten endpoints on one tier must draw on one bucket, or naming a tier buys
    # nothing.
    assert rate_tier_limiter("machine") is rate_tier_limiter("machine")


# ===========================================================================
# 9. The rule table is a description of the app
# ===========================================================================


def test_the_rule_table_has_no_duplicate_ids_and_ends_with_the_fallback():
    assert len(AUTHZ_RULE_IDS) == len(set(AUTHZ_RULE_IDS))
    assert AUTHZ_RULE_IDS[-1] == AUTHZ_FALLBACK_RULE_ID
    assert AUTHZ_RULE_IDS[0] != AUTHZ_FALLBACK_RULE_ID


def test_every_rule_declares_a_real_exposure_and_a_path():
    for row in AUTHZ_RULES:
        assert str(row["exposure"]) in AUTHZ_EXPOSURE_RANK, row["rule_id"]
        assert str(row["path"])
        assert tuple(row["methods"])
        for method in row["methods"]:
            assert str(method).upper() in D._HTTP_METHODS, (row["rule_id"], method)
        assert row.get("note")


def test_an_admin_rule_is_gated_by_the_admin_dependency():
    for row in AUTHZ_RULES:
        if str(row["exposure"]) == "admin":
            assert "get_current_admin_user" in tuple(row["enforced_by"]), row["rule_id"]


def test_a_public_rule_names_no_gate():
    for row in AUTHZ_RULES:
        if str(row["exposure"]) == "public":
            assert tuple(row["enforced_by"]) == (), row["rule_id"]


def test_an_authenticated_rule_names_something_that_enforces_it():
    for row in AUTHZ_RULES:
        if str(row["exposure"]) == "authenticated":
            assert tuple(row["enforced_by"]), row["rule_id"]
            for dependency in row["enforced_by"]:
                assert str(dependency) in AUTHZ_DEPENDENCY_NAMES


def test_hardening_and_enforcement_are_different_keys_on_every_row():
    # The separation is the point: a proposal that reads like a guarantee is
    # how an authorization audit goes wrong.
    for row in AUTHZ_RULES:
        assert "hardening" in row
        hardening = row["hardening"]
        for kind in hardening:
            assert kind in D._HARDENING_KEYS, (row["rule_id"], kind)
        # A rule with hardening still says what is enforced today, and vice
        # versa: `enforced_by` never contains a proposal.
        for requirement in hardening:
            assert requirement != "enforced_by"


def test_the_hardening_keys_are_the_five_documented_ones():
    declared = set()
    for row in AUTHZ_RULES:
        declared.update(row["hardening"])
    assert declared <= D._HARDENING_KEYS
    assert declared == {"scopes", "cell", "rate_tier", "step_up"}
    # `roles` is supported and currently unused: every role-gated route today is
    # gated by a whole-role dependency rather than by a per-rule role list.
    assert "roles" in D._HARDENING_KEYS


def test_the_rule_table_covers_every_live_method_path_pair():
    inventory = authz_route_inventory(main.app.routes)
    assert inventory
    assert all(row["delta"] == "match" for row in inventory)
    assert all(row["rule_id"] != AUTHZ_FALLBACK_RULE_ID for row in inventory)
    # And the union of the rules accounts for every pair.
    assert {row["rule_id"] for row in inventory} <= set(AUTHZ_RULE_IDS)


def test_the_drift_report_is_in_sync():
    report = authz_drift_report(main.app.routes)
    assert report["in_sync"] is True
    assert report["mismatched"] == []
    assert report["unclassified_routes"] == []
    assert report["unreachable_rules"] == []
    assert report["drift"] == {"match": report["method_path_pairs"]}
    assert report["method_path_pairs"] == len(authz_route_inventory(main.app.routes))


def test_the_exposure_split_is_reported_and_sums_to_the_route_count():
    coverage = authz_coverage_report(main.app.routes)
    exposures = coverage["exposures"]
    assert set(exposures) == set(AUTHZ_EXPOSURES)
    assert sum(entry["routes"] for entry in exposures.values()) == coverage["method_path_pairs"]
    # Ordering is meaningful: the admin surface is the largest single block and
    # the public one is dominated by the /meta and catalog reads.
    assert exposures["admin"]["routes"] > exposures["public"]["routes"]
    assert exposures["public"]["routes"] > 0
    for entry in exposures.values():
        assert 0.0 <= entry["share"] <= 1.0
    # Shares are rounded for display, so they sum to 1 to within the rounding.
    assert abs(sum(entry["share"] for entry in exposures.values()) - 1.0) < 1e-5


def test_the_catch_all_rule_is_expected_to_be_dead():
    coverage = authz_coverage_report(main.app.routes)
    # It is reported separately from the real rules: a fallback that matches
    # something is not a fallback.
    assert coverage["fallback_rule"] == AUTHZ_FALLBACK_RULE_ID
    assert coverage["fallback_routes"] == 0
    assert AUTHZ_FALLBACK_RULE_ID not in coverage["unreachable_rules"]


def test_without_a_route_table_the_reports_say_so_instead_of_guessing():
    report = authz_drift_report()
    assert report["routes_provided"] is False
    assert report["method_path_pairs"] == 0
    assert report["coverage"]["routes_provided"] is False
    assert report["in_sync"] is True
    # Static sections are still there: the rules, the checks, the backlog.
    assert [row["rule_id"] for row in report["coverage"]["rules"]] == list(AUTHZ_RULE_IDS)


def test_a_route_with_no_rule_is_reported_by_name(monkeypatch):
    # A new endpoint must land on the fallback and be named, not left looking
    # governed.
    original = list(AUTHZ_RULES)
    try:
        AUTHZ_RULES[:] = [row for row in original if str(row["rule_id"]) != "user_self"]
        report = authz_drift_report(main.app.routes)
        assert report["in_sync"] is False
        assert any("GET /users/me" == item for item in report["unclassified_routes"])
    finally:
        AUTHZ_RULES[:] = original
    assert authz_drift_report(main.app.routes)["in_sync"] is True


def test_a_rule_that_disagrees_with_the_route_is_reported_as_drift():
    # Two directions, and they mean different things. A rule claiming a gate the
    # route does not resolve is documentation that overstates the posture. A
    # route resolving a gate the rule does not name is an undeclared check.
    original = list(AUTHZ_RULES)
    try:
        for index, row in enumerate(original):
            if str(row["rule_id"]) == "user_self":
                D.AUTHZ_RULES[index] = {**row, "enforced_by": ("get_principal",)}
        report = authz_drift_report(main.app.routes)
        assert report["in_sync"] is False
        assert {row["delta"] for row in report["mismatched"]} == {"rule_expects_more"}
        assert report["mismatched"][0]["rule_id"] == "user_self"
        assert report["mismatched"][0]["declared_by"] == ["get_principal"]
        assert report["mismatched"][0]["actual_by"] == ["get_current_user"]
    finally:
        D.AUTHZ_RULES[:] = original

    try:
        for index, row in enumerate(original):
            if str(row["rule_id"]) == "user_self":
                D.AUTHZ_RULES[index] = {**row, "enforced_by": ()}
        report = authz_drift_report(main.app.routes)
        assert report["in_sync"] is False
        assert {row["delta"] for row in report["mismatched"]} == {"route_has_more"}
    finally:
        D.AUTHZ_RULES[:] = original
    assert authz_drift_report(main.app.routes)["in_sync"] is True


def test_a_route_with_a_gate_the_table_does_not_describe_would_be_drift():
    # Nothing in the app today, so the live report is clean. The direction is
    # pinned here by the mutation: adding a gate the table does not name is the
    # `route_has_more` delta, and it is not a match.
    from fastapi import APIRouter, Depends, FastAPI

    probe_app = FastAPI()
    router = APIRouter()

    @router.get("/only-ruled")
    async def _only_ruled(current_user=Depends(deps.get_current_user)):
        return {}

    @router.get("/ungated")
    async def _ungated(
        current_user=Depends(deps.get_current_user),
        principal=Depends(deps.get_principal),
    ):
        return {}

    probe_app.include_router(router, prefix="/probe")
    original = list(AUTHZ_RULES)
    try:
        D.AUTHZ_RULES[:] = [
            *original[:-1],
            {
                "rule_id": "probe_only_ruled",
                "methods": ("GET",),
                "path": "/probe/only-ruled",
                "exposure": "authenticated",
                "enforced_by": ("get_current_user",),
                "hardening": {},
            },
            original[-1],
        ]
        inventory = {row["path"]: row for row in authz_route_inventory(probe_app.routes)}
        assert inventory["/probe/only-ruled"]["delta"] == "match"
        assert inventory["/probe/ungated"]["delta"] == "route_has_more"
        assert inventory["/probe/ungated"]["actual_by"] == [
            "get_current_user",
            "get_principal",
        ]
    finally:
        D.AUTHZ_RULES[:] = original


def test_all_six_factories_are_unbound_and_the_report_says_so():
    # The honest finding, not a bug to fix: the factories work, are tested, and
    # no route calls them, which is exactly why their requirements live in
    # `hardening` and nowhere else.
    #
    # `require_public_rate_limit` is the exception and is *bound* -- the four
    # credential-in-the-body sign-in routes use it. It is asserted separately
    # below rather than folded in here, because "unbound" was previously true of
    # every factory and a seventh one that is actually used is exactly the sort
    # of exception that gets quietly forgotten.
    report = authz_drift_report(main.app.routes)
    unbound = [name for name in AUTHZ_FACTORY_NAMES if name != "require_public_rate_limit"]
    assert report["unbound_factories"] == sorted(unbound)
    assert set(report["bound_factories"]) == set(AUTHZ_FACTORY_NAMES)
    for name in unbound:
        assert report["bound_factories"][name]["routes"] == 0
        assert report["bound_factories"][name]["sample"] == []
    assert "unbound" in report["note"]


def test_the_public_rate_limit_factory_is_reported_as_bound():
    # A factory name that resolves but is never counted makes `unbound_factories`
    # lie about the one factory that is in use, which is the same category of
    # quiet miscount the exposure and drift sections exist to rule out.
    report = authz_drift_report(main.app.routes)
    entry = report["bound_factories"]["require_public_rate_limit"]
    assert entry["routes"] > 0
    assert entry["sample"], "a bound factory with no sample route recorded"
    # The authenticated rate limiter is still unbound, and saying so is the
    # honest reading: adding a seventh factory does not mean the sixth is used.
    assert "require_rate_limit" in report["unbound_factories"]
    # ...and the two must not be conflated. `require_public_rate_limit` does not
    # share a prefix with `require_rate_limit`, so prefix matching keeps them
    # apart -- which is what lets the report say one is bound and one is not.
    assert report["bound_factories"]["require_rate_limit"]["routes"] == 0


# ===========================================================================
# 10. Effective paths: the FastAPI quirk this whole table depends on
# ===========================================================================


def test_iter_authz_routes_reproduces_the_openapi_path_set_exactly():
    # `route.path` is not the effective path in this FastAPI release:
    # `include_router(prefix=...)` lives in `route.include_context.prefix`, and
    # a router's own prefix is already baked into its child paths. Adding both
    # doubles every segment; adding neither drops the router prefix. A rule
    # table written against the wrong one matches nothing while looking complete.
    effective = {path for path, _ in D.iter_authz_routes(main.app.routes)}
    published = set(main.app.openapi()["paths"])
    assert effective == published
    assert effective - published == set()
    assert published - effective == set()
    assert len(effective) > 100


def test_iter_authz_routes_yields_api_routes_only():
    from fastapi.routing import APIRoute

    for _path, route in D.iter_authz_routes(main.app.routes):
        assert isinstance(route, APIRoute)


def test_iter_authz_routes_survives_garbage_without_raising():
    # It is called from a validator and from an endpoint that must not 500 over
    # a routing detail.
    assert list(D.iter_authz_routes([object(), None, 42, "routes", []])) == []
    assert list(D.iter_authz_routes(None)) == []
    assert list(D.iter_authz_routes(object())) == []


def test_route_facts_separate_dependencies_from_generated_factories():
    from fastapi import APIRouter, Depends, FastAPI

    router = APIRouter()

    @router.get("/declared")
    async def _declared(current_user=Depends(deps.get_current_user)):
        return {}

    @router.get("/generated")
    async def _generated(cell=Depends(deps.require_cell_access("customer_profile", "full_name"))):
        return {}

    @router.get("/listed", dependencies=[Depends(deps.require_scopes("audit:read"))])
    async def _listed():
        return {}

    @router.get("/nothing")
    async def _nothing():
        return {}

    probe = FastAPI()
    probe.include_router(router, prefix="/probe")
    pairs = dict(D.iter_authz_routes(probe.routes))
    facts = {path: route_authz_facts(route) for path, route in pairs.items()}
    assert facts["/probe/declared"] == {
        "dependencies": {"get_current_user"},
        "factories": set(),
    }
    # A cell check is an authorization check. Reading its generated name as an
    # unrecognized callable would report the route as having none.
    assert facts["/probe/generated"] == {
        "dependencies": set(),
        "factories": {"require_cell_access"},
    }
    # A router-level dependency is found too, not only an endpoint parameter.
    assert facts["/probe/listed"]["factories"] == {"require_scopes"}
    assert facts["/probe/nothing"] == {"dependencies": set(), "factories": set()}
    assert route_authz_dependencies(pairs["/probe/declared"]) == {"get_current_user"}
    assert route_authz_dependencies(pairs["/probe/nothing"]) == set()


def test_route_facts_survive_a_route_with_no_introspectable_endpoint():
    class _Route:
        name = "opaque"
        methods = ["GET"]
        dependencies = ()

    facts = route_authz_facts(_Route())
    assert facts == {"dependencies": set(), "factories": set()}


def test_a_generated_factory_name_is_recognised_by_prefix():
    names = [
        require_scopes("audit:read").__name__,
        require_scopes("read", "write").__name__,
        require_roles("admin", "agent").__name__,
        require_step_up().__name__,
        require_cell_access("booking", "status").__name__,
        require_rate_limit().__name__,
        require_rate_tier("machine").__name__,
        require_public_rate_tier("sensitive").__name__,
        require_tenant.__name__,
    ]
    resolved = set()
    for name in names:
        for prefix, factory in AUTHZ_FACTORY_PREFIXES.items():
            if name == prefix or name.startswith(prefix):
                resolved.add(factory)
                break
        else:
            raise AssertionError(f"unrecognised generated dependency name: {name}")
    assert resolved == set(AUTHZ_FACTORY_NAMES)


# ===========================================================================
# 11. Rule matching
# ===========================================================================


def test_a_specific_rule_beats_a_broad_one():
    specific = match_authz_rule("GET", "/users/me/step-up")
    broad = match_authz_rule("GET", "/users/me")
    assert specific["rule_id"] == "user_step_up_read"
    assert broad["rule_id"] == "user_self"
    assert specific["rule"]["path"] == "/users/me/step-up"


def test_the_method_participates_in_the_match():
    assert match_authz_rule("POST", "/users/me/step-up")["rule_id"] == "user_step_up_mint"
    assert match_authz_rule("GET", "/users/me/step-up")["rule_id"] == "user_step_up_read"


def test_a_path_parameter_matches_exactly_one_segment():
    assert match_authz_rule("GET", "/bookings/1/history")["rule_id"] == "booking_history"
    # Two segments is not one segment, so the prefix rule above it wins.
    assert match_authz_rule("GET", "/bookings/1/audit/summary")["rule_id"] != "booking_history"


def test_a_wildcard_pattern_is_a_prefix_not_a_glob():
    assert match_authz_rule("GET", "/meta")["rule_id"] == "meta_surface"
    assert match_authz_rule("GET", "/meta/authz")["rule_id"] == "meta_surface"
    assert match_authz_rule("GET", "/metadata")["rule_id"] == AUTHZ_FALLBACK_RULE_ID


def test_an_unmatched_route_falls_back_and_says_so():
    match = match_authz_rule("GET", "/not/a/route")
    assert match["rule_id"] == AUTHZ_FALLBACK_RULE_ID
    # The catch-all *row* matched, so this is not the "table ran out" case --
    # but it is the same answer and the same flag.
    assert match["matched_fallback"] is True
    assert "unmatched" not in match
    assert match["rule"]["exposure"] == "public"
    assert match["rule"]["enforced_by"] == ()


def test_a_route_the_table_cannot_place_falls_off_the_end_and_still_says_so():
    # The catch-all rule does not list HEAD, so a HEAD request runs the table
    # out of rows. That path returns the fallback with no index, and the flag
    # must still be set -- otherwise a route nobody described looks described.
    match = match_authz_rule("HEAD", "/not/a/route")
    assert match["rule_id"] == AUTHZ_FALLBACK_RULE_ID
    assert match["rule_index"] == -1
    assert match["matched_fallback"] is True
    assert match["unmatched"] is True


def test_an_unknown_method_does_not_match_a_rule_that_lists_a_method():
    match = match_authz_rule("TRACE", "/users/me")
    assert match["rule_id"] == AUTHZ_FALLBACK_RULE_ID
    assert match["method"] == "TRACE"
    assert match["matched_fallback"] is True


def test_the_pattern_compiler_handles_a_malformed_pattern_without_raising():
    # A half-written brace must not take the validator down.
    compiled = D._authz_pattern_re("/bookings/{unclosed")
    assert compiled.match("/bookings/{unclosed")
    assert not compiled.match("/bookings/1")
    assert D._authz_pattern_re("").pattern == "^$"


def test_the_sample_path_generator_makes_a_matchable_shape():
    for row in AUTHZ_RULES:
        if str(row["rule_id"]) == AUTHZ_FALLBACK_RULE_ID:
            continue
        path = authz_rule_sample_path(row)
        assert path.startswith("/")
        assert "{" not in path and "}" not in path and "*" not in path
        verb = str((row.get("methods") or ("GET",))[0])
        # The generated shape must match its own rule, or the simulation would
        # be measuring a different route than the table describes.
        matched = match_authz_rule(verb, path)["rule_id"]
        assert matched == str(row["rule_id"]), (row["rule_id"], path, matched)


# ===========================================================================
# 12. The decision engine
# ===========================================================================


def test_anonymous_is_denied_everything_that_is_not_public():
    decision = evaluate_authz(None, "GET", "/users/me")
    assert decision["allowed"] is False
    assert decision["status"] == ORIG_STATUS_401
    assert decision["denial_kind"] == "unauthenticated"
    assert decision["enforced_decision"] == "denied"
    checks = {row["check"]: row for row in decision["checks"]}
    assert checks["authenticated"]["status"] == "failed"
    # The exposure decided which checks ran: an authenticated rule does not run
    # the admin check at all.
    assert "admin" not in checks


def test_an_anonymous_caller_never_reaches_the_admin_check():
    decision = evaluate_authz(None, "GET", "/chat/admin/stats")
    checks = {row["check"]: row for row in decision["checks"]}
    assert checks["authenticated"]["status"] == "failed"
    assert checks["admin"]["status"] == "not_evaluated"
    assert checks["admin"]["reason"] == "unauthenticated"
    # The first failure still decides, so the caller is told it is unauthenticated
    # rather than that it is not an admin.
    assert decision["denial_kind"] == "unauthenticated"


def test_anonymous_is_allowed_a_public_route():
    decision = evaluate_authz(None, "GET", "/meta/authz")
    assert decision["allowed"] is True
    assert decision["status"] == 200
    assert decision["denial"] is None
    assert decision["exposure"] == "public"
    assert decision["enforced_by"] == []


def test_an_admin_is_allowed_an_admin_route():
    decision = evaluate_authz(authz_probe("admin"), "GET", "/chat/admin/stats")
    assert decision["allowed"] is True
    assert decision["enforced_by"] == ["get_current_admin_user"]


def test_a_non_admin_is_denied_an_admin_route_with_the_role_denial():
    decision = evaluate_authz(authz_probe("auditor"), "GET", "/chat/admin/stats")
    assert decision["allowed"] is False
    assert decision["status"] == ORIG_STATUS_403
    assert decision["denial_kind"] == "role_required"
    checks = {row["check"]: row for row in decision["checks"]}
    assert checks["admin"]["status"] == "failed"
    assert checks["admin"]["required_any"] == ["admin"]


def test_the_first_failure_decides_and_the_order_is_check_order():
    # A caller with three problems gets the denial a request would have
    # produced, not three of them. `authenticated` is first, so an anonymous
    # caller never learns it also lacks a scope.
    decision = evaluate_authz(None, "POST", "/retention/maintenance/run", rate_limit=True)
    order = {name: index for index, name in enumerate(AUTHZ_CHECK_ORDER)}
    indices = [order[str(row["check"])] for row in decision["checks"]]
    assert indices == sorted(indices)
    failing = [row["check"] for row in decision["checks"] if row["status"] == "failed"]
    assert failing[0] == "authenticated"
    assert decision["denial_kind"] == "unauthenticated"


def test_every_returned_check_is_in_the_declared_order():
    for rule_id in AUTHZ_RULE_IDS:
        row = AUTHZ_RULE_BY_ID[rule_id]
        verb = str((row.get("methods") or ("GET",))[0])
        decision = evaluate_authz(authz_probe("customer"), verb, authz_rule_sample_path(row))
        seen = [str(item["check"]) for item in decision["checks"]]
        assert set(seen) <= set(AUTHZ_CHECK_ORDER)
        order = {name: index for index, name in enumerate(AUTHZ_CHECK_ORDER)}
        assert [order[name] for name in seen] == sorted(order[name] for name in seen)


def test_the_policy_tier_check_reports_unevaluated_rather_than_denying():
    # No factory raises a tier denial, so the engine must not invent one.
    decision = evaluate_authz(authz_probe("customer"), "GET", "/users/me/policy-score")
    checks = {row["check"]: row for row in decision["checks"]}
    assert checks["policy_tier"]["status"] == "not_evaluated"
    assert checks["policy_tier"]["enforced"] is True
    assert decision["allowed"] is True


def test_a_supplied_policy_tier_is_reported_on_the_check():
    decision = evaluate_authz(
        authz_probe("customer"), "GET", "/users/me/policy-score", policy_tier="gold"
    )
    checks = {row["check"]: row for row in decision["checks"]}
    assert checks["policy_tier"]["status"] == "passed"
    assert checks["policy_tier"]["policy_tier"] == "gold"


def test_the_hardening_block_is_ignored_unless_it_is_asked_for():
    # Every rule with a hardening requirement, checked: the default decision
    # evaluates nothing from it and every check it reports is enforced.
    checked = 0
    for row in AUTHZ_RULES:
        if not row["hardening"]:
            continue
        checked += 1
        verb = str((row.get("methods") or ("GET",))[0])
        path = authz_rule_sample_path(row)
        for probe in ("admin", "customer", "anonymous"):
            decision = evaluate_authz(authz_probe(probe), verb, path)
            assert decision["hardening_included"] is False
            assert all(item["enforced"] for item in decision["checks"]), (row["rule_id"], probe)
            assert not any(
                item["check"] in ("scopes", "roles", "step_up", "cell")
                for item in decision["checks"]
            ), (row["rule_id"], probe)
    assert checked >= 30


def test_asking_for_the_hardening_evaluation_marks_every_check_unenforced():
    decision = evaluate_authz(
        authz_probe("customer"), "GET", "/users/me", include_hardening=True
    )
    assert decision["hardening_included"] is True
    assert decision["hardening_considered"] == sorted(AUTHZ_RULE_BY_ID["user_self"]["hardening"])
    unforced = [row for row in decision["checks"] if not row["enforced"]]
    assert unforced
    assert all(row["status"] in ("passed", "failed") for row in unforced)
    # The proposed scopes are unmet by a deliberately unscoped probe, and the
    # current decision is unchanged by that.
    scopes = next(row for row in unforced if row["check"] == "scopes")
    assert scopes["status"] == "failed"
    assert scopes["missing"] == ["read"]
    assert decision["enforced_decision"] == "allowed"
    assert decision["allowed"] is False


def test_the_rate_limit_check_is_enforced_when_the_route_has_no_tier():
    decision = evaluate_authz(authz_probe("customer"), "GET", "/users/me", rate_limit=True)
    check = next(row for row in decision["checks"] if row["check"] == "rate_limit")
    assert check["enforced"] is True
    assert check["status"] == "passed"
    assert check["tier"] == "interactive"


def test_a_tenant_check_is_advisory_and_reports_the_mismatch():
    decision = evaluate_authz(
        authz_probe("cross_tenant"), "GET", "/users/me", tenant_id="tenant-a"
    )
    check = next(row for row in decision["checks"] if row["check"] == "tenant")
    assert check["enforced"] is False
    assert check["status"] == "failed"
    assert check["requested"] == "tenant-a"
    assert check["principal_tenant"] == "tenant-b"


def test_the_decision_carries_the_rule_it_matched():
    decision = evaluate_authz(authz_probe("admin"), "GET", "/users/me")
    assert decision["rule_id"] == "user_self"
    assert decision["exposure"] == "authenticated"
    assert decision["exposure_rank"] == 1
    assert decision["matched_fallback"] is False
    assert decision["facts"]["is_admin"] is True
    assert decision["facts"]["is_service_account"] is False


def test_an_api_key_probe_is_reported_as_a_service_account():
    decision = evaluate_authz(authz_probe("api_key_read"), "GET", "/users/me")
    assert decision["facts"]["is_service_account"] is True
    assert decision["facts"]["auth_method"] == D.AUTH_METHOD_API_KEY
    assert decision["facts"]["roles"] == [AUTHZ_ROLE_SERVICE_ACCOUNT]


def test_a_fallback_route_with_no_gate_is_a_weak_claim_not_a_decision():
    # A route on the catch-all has no described gate, so a non-admin principal
    # is not denied by the engine -- and the report says the route is
    # unclassified, which is the real signal.
    decision = evaluate_authz(authz_probe("auditor"), "GET", "/not/a/route")
    assert decision["rule_id"] == AUTHZ_FALLBACK_RULE_ID
    assert decision["allowed"] is True
    assert decision["matched_fallback"] is True


# ===========================================================================
# 13. Probes, simulation and the backlog
# ===========================================================================


def test_every_probe_builds_a_real_principal():
    absent = 0
    for row in AUTHZ_PROBES:
        principal = authz_probe(str(row["probe"]))
        if bool(row.get("absent")):
            # The engine branches on the principal being absent, so an absent
            # probe has to be genuinely absent rather than a hollow one.
            assert principal is None, row["probe"]
            absent += 1
            continue
        assert isinstance(principal, Principal), row["probe"]
        assert principal.subject, row["probe"]
        assert principal.auth_method in (
            D.AUTH_METHOD_USER,
            D.AUTH_METHOD_API_KEY,
            D.AUTH_METHOD_DELEGATED,
        ), row["probe"]
    assert absent, "no probe covers the no-credential case"


def test_the_absent_probe_is_the_one_that_says_no_credential():
    row = next(r for r in AUTHZ_PROBES if bool(r.get("absent")))
    assert str(row["probe"]) == "anonymous"
    # Nothing to attach to an absent caller, and the validator says so rather
    # than quietly ignoring a subject it cannot use.
    for column in ("roles", "scopes", "tenant_id", "auth_method", "subject", "claims"):
        assert not row.get(column), (column, row[column])


def test_a_probe_that_is_absent_but_declares_a_credential_is_an_error():
    original = list(AUTHZ_PROBES)
    try:
        D.AUTHZ_PROBES[:] = [
            {**row, "subject": "sneaky"} if bool(row.get("absent")) else dict(row)
            for row in original
        ]
        report = validate_authz()
        assert report["valid"] is False
        assert any("marked absent but declares" in m for m in report["error_list"])
    finally:
        D.AUTHZ_PROBES[:] = original


def test_probe_scopes_and_roles_are_issuable_and_resolvable():
    known_roles = D._known_roles()
    for row in AUTHZ_PROBES:
        for role in row["roles"]:
            assert str(role) in known_roles, (row["probe"], role)
        for scope in row["scopes"]:
            assert str(scope) in SCOPE_BY_NAME, (row["probe"], scope)


def test_the_step_up_probe_carries_the_claims_the_dependency_reads():
    principal = authz_probe("step_up_loa3")
    assert security.step_up_satisfied(principal.claims, "loa3") is True
    assert principal.step_up_level == "loa3"


def test_an_unknown_probe_name_still_produces_a_principal():
    # An unknown name is not a table row, so it is not absent either: the
    # fallback is a real principal, so a typo in a caller shows up as a
    # near-empty caller rather than as a silently total denial.
    principal = authz_probe("not-a-probe")
    assert isinstance(principal, Principal)
    assert principal.subject == "not-a-probe"
    assert principal.roles == frozenset()
    assert principal.scopes == frozenset()


def test_the_simulation_covers_every_probe_rule_cell():
    report = authz_simulation_report()
    assert report["probes"] == list(AUTHZ_PROBE_NAMES)
    assert report["rules"] == list(AUTHZ_RULE_IDS)
    assert report["cells"] == len(AUTHZ_PROBE_NAMES) * len(AUTHZ_RULE_IDS)
    assert set(report["matrix"]) == set(AUTHZ_PROBE_NAMES)
    for cells in report["matrix"].values():
        assert list(cells) == list(AUTHZ_RULE_IDS)


def test_the_anonymous_probe_is_denied_everything_the_engine_can_decide():
    report = authz_simulation_report()
    # A resolvable principal with no roles and no scopes: the shape an
    # unauthenticated-but-resolved caller has. `enforced_decision` is the
    # current posture; `allowed` also folds in the proposed hardening, which is
    # why a public rule with a scope proposal can read as denied here.
    gated = [
        rule_id
        for rule_id, row in AUTHZ_RULE_BY_ID.items()
        if str(row["exposure"]) in ("admin", "authenticated")
    ]
    assert gated
    for rule_id in gated:
        cell = report["matrix"]["anonymous"][rule_id]
        assert cell["enforced_decision"] == "denied", rule_id
        assert cell["allowed"] is False, rule_id
    # The denial list is the union over both readings, so it covers the gated
    # surface and the public rules that propose a scope.
    assert set(gated) <= set(report["denied_by_probe"]["anonymous"])
    assert report["denied_by_probe"]["anonymous"] == sorted(
        report["denied_by_probe"]["anonymous"]
    )


def test_a_policy_rule_is_reported_as_not_decidable_offline_rather_than_allowed():
    # `get_current_policy_or_admin_user` needs a User row, which an offline
    # Principal does not carry, so the engine cannot decide it. It says so
    # instead of inventing a denial, and it does not quietly report `allowed`
    # either: the check is present and unevaluated.
    row = AUTHZ_RULE_BY_ID["user_can_access"]
    assert str(row["exposure"]) == "policy"
    decision = evaluate_authz(
        authz_probe("anonymous"), "GET", authz_rule_sample_path(row)
    )
    check = next(c for c in decision["checks"] if c["check"] == "policy_tier")
    assert check["status"] == "not_evaluated"
    assert check["enforced"] is True
    assert "get_current_customer_policy_score" in decision["enforced_by"]


def test_a_scope_check_agrees_with_the_primitive_it_wraps():
    # The hardening scope check must answer the same question as
    # `Principal.has_scope` for every probe and every rule, because that is the
    # primitive `require_scopes` itself uses. An absent probe has no scopes to
    # check, so it is skipped rather than compared against an empty principal.
    checked = 0
    for probe in AUTHZ_PROBE_NAMES:
        principal = authz_probe(probe)
        if principal is None:
            continue
        for rule_id, row in AUTHZ_RULE_BY_ID.items():
            required = tuple((row.get("hardening") or {}).get("scopes") or ())
            if not required:
                continue
            verb = str((row.get("methods") or ("GET",))[0])
            decision = evaluate_authz(
                principal, verb, authz_rule_sample_path(row), include_hardening=True
            )
            check = next((c for c in decision["checks"] if c["check"] == "scopes"), None)
            if check is None:
                continue
            checked += 1
            expected = [scope for scope in required if not principal.has_scope(scope)]
            assert check["status"] == ("failed" if expected else "passed"), (probe, rule_id)
            assert sorted(check["missing"]) == sorted(expected)
    assert checked > 100


def test_the_simulation_can_be_narrowed():
    report = authz_simulation_report(probes=["admin"], rules=["user_self", "chat_root"])
    assert report["probes"] == ["admin"]
    assert report["rules"] == ["user_self", "chat_root"]
    assert report["cells"] == 2


def test_the_backlog_is_grouped_by_requirement_and_ordered_by_breadth():
    backlog = authz_hardening_backlog()
    requirements = [row["requirement"] for row in backlog["requirements"]]
    assert requirements == sorted(
        requirements, key=lambda name: -next(
            r["rule_count"] for r in backlog["requirements"] if r["requirement"] == name
        )
    )
    assert backlog["requirements"][0]["requirement"] == "scopes"
    for row in backlog["requirements"]:
        assert row["enforced_by"] is None
        assert row["note"]
        assert row["values"]


def test_the_backlog_respects_its_limit():
    backlog = authz_hardening_backlog(limit=1)
    assert backlog["limit"] == 1
    assert backlog["shown"] == 1
    assert len(backlog["requirements"]) >= 1


def test_the_backlog_counts_the_rules_that_carry_a_proposal():
    backlog = authz_hardening_backlog()
    carrying = [row["rule_id"] for row in AUTHZ_RULES if row["hardening"]]
    assert backlog["total_rules_with_hardening"] == len(carrying)
    assert backlog["total_rules"] == len(AUTHZ_RULES)
    # The scopes backlog is the widest gap, and it is a gap.
    scopes = next(r for r in backlog["requirements"] if r["requirement"] == "scopes")
    assert scopes["rule_count"] == sum(
        1 for row in AUTHZ_RULES if "scopes" in (row.get("hardening") or {})
    )
    assert "proposal" in backlog["note"]


# ===========================================================================
# 14. Validation
# ===========================================================================


def test_the_shipped_tables_validate_clean():
    report = validate_authz()
    assert report["valid"] is True
    assert report["errors"] == 0
    assert report["warnings"] == 0
    assert report["error_list"] == []
    assert report["warning_list"] == []
    assert report["version"] == AUTHZ_VERSION
    assert report["counts"] == {
        "scopes": len(SCOPE_CATALOG),
        "role_grants": len(ROLE_SCOPE_GRANTS),
        "denials": len(AUTHZ_DENIALS),
        "step_up_levels": len(STEP_UP_RANKS),
        "rate_tiers": len(RATE_TIERS),
        "rules": len(AUTHZ_RULES),
        "probes": len(AUTHZ_PROBES),
        "checks": len(AUTHZ_CHECK_ORDER),
    }


def test_validate_authz_never_raises_on_malformed_configuration():
    # A validator that explodes on malformed config gets wrapped in a
    # try/except by its first caller, and then it has stopped being a validator.
    # Copies, not references: the restore writes back into the same live lists.
    original = {
        name: list(getattr(D, name)) for name in ("SCOPE_CATALOG", "RATE_TIERS", "AUTHZ_PROBES")
    }
    try:
        D.SCOPE_CATALOG[:] = [{"scope": None}, {"scope": 17, "parent": object()}, {}]
        D.RATE_TIERS[:] = [{"tier": "", "capacity": "many", "refill_per_second": None}]
        D.AUTHZ_PROBES[:] = [{"probe": "x", "claims": "not-a-mapping", "roles": ["nope"]}]
        report = validate_authz()
        assert report["valid"] is False
        assert report["errors"] > 0
        assert all(isinstance(message, str) for message in report["error_list"])
    finally:
        D.SCOPE_CATALOG[:] = original["SCOPE_CATALOG"]
        D.RATE_TIERS[:] = original["RATE_TIERS"]
        D.AUTHZ_PROBES[:] = original["AUTHZ_PROBES"]
    assert validate_authz()["errors"] == 0


def test_a_duplicate_scope_is_an_error():
    original = list(SCOPE_CATALOG)
    try:
        D.SCOPE_CATALOG.append(dict(original[1]))
        report = validate_authz()
        assert any("duplicate scope" in message for message in report["error_list"])
    finally:
        D.SCOPE_CATALOG[:] = original


def test_a_scope_with_an_unknown_parent_is_an_error():
    original = list(SCOPE_CATALOG)
    try:
        D.SCOPE_CATALOG.append({"scope": "new:child", "parent": "no-such-parent", "self_service": False})
        report = validate_authz()
        assert any("parent" in message and "SCOPE_CATALOG" in message for message in report["error_list"])
    finally:
        D.SCOPE_CATALOG[:] = original


def test_a_self_service_wildcard_is_an_error():
    original = list(SCOPE_CATALOG)
    try:
        # Replace the row rather than editing it. `list(SCOPE_CATALOG)` is a
        # shallow copy, so the row dicts it holds are the live ones; setting a
        # key on one of them would mutate the table the `finally` restores.
        D.SCOPE_CATALOG[:] = [
            {**row, "self_service": True} if str(row["scope"]) == "*" else row
            for row in original
        ]
        report = validate_authz()
        assert any("wildcard" in message for message in report["error_list"])
    finally:
        D.SCOPE_CATALOG[:] = original


def test_a_role_grant_for_an_unknown_role_is_an_error(monkeypatch):
    monkeypatch.setitem(ROLE_SCOPE_GRANTS, "not-a-role", ("read",))
    report = validate_authz()
    assert any("not produced by USER_ROLES" in message for message in report["error_list"])


def test_a_role_grant_for_an_unknown_scope_is_an_error(monkeypatch):
    monkeypatch.setitem(ROLE_SCOPE_GRANTS, "owner", ("read", "no:such:scope"))
    report = validate_authz()
    assert any("unknown scope" in message for message in report["error_list"])


def test_a_role_with_no_grant_row_is_a_warning_not_an_error(monkeypatch):
    # A missing row means "no scope beyond an explicit grant", which is a fact
    # about the table, not a misconfiguration.
    monkeypatch.setitem(ROLE_SCOPE_GRANTS, "auditor", ())
    monkeypatch.delitem(ROLE_SCOPE_GRANTS, "auditor")
    report = validate_authz()
    assert report["errors"] == 0
    assert any("no ROLE_SCOPE_GRANTS row" in message for message in report["warning_list"])


def test_a_step_up_rank_that_disagrees_with_security_is_an_error():
    # Mutate STEP_UP_RANKS, not STEP_UP_RANK_BY_LEVEL. The derived index is a
    # convenience for the request path; the table is what the validator reads,
    # so editing the index is editing a copy of a table nobody validates.
    original = list(STEP_UP_RANKS)
    try:
        D.STEP_UP_RANKS[:] = [
            {**row, "rank": 7} if str(row["level"]) == "loa2" else dict(row)
            for row in original
        ]
        report = validate_authz()
        assert any(
            "disagrees with security.STEP_UP_RANK" in message for message in report["error_list"]
        )
    finally:
        D.STEP_UP_RANKS[:] = original


def test_a_step_up_level_security_knows_and_the_table_does_not_is_an_error():
    original = list(STEP_UP_RANKS)
    try:
        D.STEP_UP_RANKS[:] = [*original, {**original[0], "level": "loa9", "rank": 9}]
        report = validate_authz()
        assert any("security.STEP_UP_RANK does not" in message for message in report["error_list"])
    finally:
        D.STEP_UP_RANKS[:] = original


def test_the_validator_reads_the_table_and_not_the_index_derived_from_it():
    # The reason the two tests above mutate rows. If the validator consulted
    # STEP_UP_RANK_BY_LEVEL it would still pass these, and the whole table could
    # be edited into disagreement without a single word of complaint.
    original = list(STEP_UP_RANKS)
    try:
        D.STEP_UP_RANKS[:] = [
            {**row, "rank": 7} if str(row["level"]) == "loa2" else dict(row)
            for row in original
        ]
        assert STEP_UP_RANK_BY_LEVEL["loa2"] == 2, "the index was rebuilt, not left stale"
        report = validate_authz()
        assert report["valid"] is False
    finally:
        D.STEP_UP_RANKS[:] = original


def test_step_up_evidence_that_disagrees_with_users_is_an_error():
    original = list(STEP_UP_RANKS)
    try:
        D.STEP_UP_RANKS[:] = [
            {**row, "evidence": ("pwd",)} if str(row["level"]) == "loa2" else dict(row)
            for row in original
        ]
        report = validate_authz()
        assert any("STEP_UP_METHODS" in message for message in report["error_list"])
    finally:
        D.STEP_UP_RANKS[:] = original


def test_a_rate_tier_with_a_bad_budget_is_an_error():
    for mutation, expected in (
        ({"capacity": 0}, "capacity must be at least 1"),
        ({"capacity": "many"}, "is not an integer"),
        ({"refill_per_second": -1.0}, "must not be negative"),
        ({"refill_per_second": None}, "is not a number"),
    ):
        original = list(RATE_TIERS)
        try:
            D.RATE_TIERS[0] = {**original[0], **mutation}
            report = validate_authz()
            assert any(expected in message for message in report["error_list"]), mutation
        finally:
            D.RATE_TIERS[:] = original


def test_a_rate_tier_naming_an_unknown_auth_method_is_an_error():
    original = list(RATE_TIERS)
    try:
        D.RATE_TIERS[0] = {**original[0], "applies_to": ("magic",)}
        report = validate_authz()
        assert any("unknown auth method" in message for message in report["error_list"])
    finally:
        D.RATE_TIERS[:] = original


def test_removing_the_fallback_tier_is_an_error():
    original = list(RATE_TIERS)
    try:
        D.RATE_TIERS[:] = [row for row in original if str(row["tier"]) != "interactive"]
        report = validate_authz()
        assert any("no 'interactive' row" in message for message in report["error_list"])
    finally:
        D.RATE_TIERS[:] = original


def test_an_environment_override_of_the_documented_limit_is_a_warning(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_CAPACITY", "7")
    monkeypatch.setenv("RATE_LIMIT_REFILL", "0.25")
    report = validate_authz()
    assert report["errors"] == 0
    warnings = " ".join(report["warning_list"])
    assert "RATE_LIMIT_CAPACITY=7" in warnings
    assert "RATE_LIMIT_REFILL=0.25" in warnings


def test_a_denial_with_a_malformed_shape_is_an_error():
    cases = (
        ({"detail_shape": "weird"}, "is not string or object"),
        ({"status": "many"}, "is not an integer"),
        ({"keys": ("error", "error")}, "duplicate key"),
        ({"detail_shape": "string", "keys": ("error",)}, "cannot declare keys"),
        ({"detail_shape": "object", "keys": ()}, "must declare an 'error' key"),
        ({"detail_shape": "string", "text": None}, "must declare its text"),
        ({"factory": "no_such_factory"}, "is not callable"),
    )
    for mutation, expected in cases:
        original = list(AUTHZ_DENIALS)
        try:
            D.AUTHZ_DENIALS[1] = {**original[1], **mutation}
            report = validate_authz()
            assert any(expected in message for message in report["error_list"]), mutation
        finally:
            D.AUTHZ_DENIALS[:] = original


def test_a_denial_status_outside_the_usual_set_is_a_warning():
    original = list(AUTHZ_DENIALS)
    try:
        D.AUTHZ_DENIALS[1] = {**original[1], "status": 418}
        report = validate_authz()
        assert any("outside the usual denial set" in message for message in report["warning_list"])
    finally:
        D.AUTHZ_DENIALS[:] = original


def test_a_check_without_a_declared_denial_is_an_error(monkeypatch):
    for check in AUTHZ_CHECK_ORDER:
        if check == "policy_tier":
            continue
        monkeypatch.setitem(AUTHZ_CHECK_DENIALS, check, "no_such_denial")
        report = validate_authz()
        assert any("which AUTHZ_DENIALS does not declare" in message for message in report["error_list"])


def test_a_check_denial_naming_an_unknown_check_is_an_error(monkeypatch):
    monkeypatch.setitem(AUTHZ_CHECK_DENIALS, "telepathy", "unauthenticated")
    report = validate_authz()
    assert any("not in AUTHZ_CHECK_ORDER" in message for message in report["error_list"])


def test_every_declared_denial_is_claimed_by_a_check():
    # A denial no check claims is raised by a dependency only, which is a fact
    # about the table worth knowing -- a warning, never an error, because
    # `_unauthorized` is raised by principal resolution rather than by a check.
    report = validate_authz()
    assert report["warnings"] == 0
    assert all(
        str(row["kind"]) in set(AUTHZ_CHECK_DENIALS.values()) for row in AUTHZ_DENIALS
    )


def test_a_denial_no_check_claims_is_a_warning(monkeypatch):
    monkeypatch.setitem(AUTHZ_CHECK_DENIALS, "authenticated", "insufficient_scope")
    report = validate_authz()
    assert report["errors"] == 0
    warnings = " ".join(report["warning_list"])
    assert "unauthenticated" in warnings
    assert "never from evaluate_authz" in warnings


def test_a_malformed_rule_is_an_error():
    cases = (
        ({"exposure": "secret"}, "unknown exposure"),
        ({"exposure": "public", "enforced_by": ("get_current_user",)}, "cannot be gated"),
        ({"exposure": "admin", "enforced_by": ()}, "does not include get_current_admin_user"),
        ({"exposure": "authenticated", "enforced_by": ()}, "nothing enforces it"),
        ({"enforced_by": ("get_current_wizard",)}, "unknown dependency"),
        ({"methods": ()}, "can never match"),
        ({"methods": ("GET", "FLY")}, "unknown HTTP method"),
        ({"path": ""}, "no path pattern"),
    )
    for mutation, expected in cases:
        original = list(AUTHZ_RULES)
        try:
            index = next(
                i
                for i, row in enumerate(original)
                if str(row["rule_id"]) == "user_self"
            )
            D.AUTHZ_RULES[index] = {**original[index], **mutation}
            report = validate_authz()
            assert any(expected in message for message in report["error_list"]), mutation
        finally:
            D.AUTHZ_RULES[:] = original


def test_a_duplicate_rule_id_is_an_error():
    original = list(AUTHZ_RULES)
    try:
        D.AUTHZ_RULES.append(dict(original[0]))
        report = validate_authz()
        assert any("duplicate rule_id" in message for message in report["error_list"])
    finally:
        D.AUTHZ_RULES[:] = original


def test_a_hardening_requirement_naming_something_unknown_is_an_error():
    cases = (
        ({"scopes": ("no:such:scope",)}, "not in SCOPE_CATALOG"),
        ({"roles": ("not-a-role",)}, "not a known role"),
        ({"step_up": "loa9"}, "not a known level"),
        ({"rate_tier": "no-such-tier"}, "not in RATE_TIERS"),
        ({"cell": ["customer_profile"]}, "must be (resource, cell"),
        ({"cell": ["customer_profile", "full_name", "sideways"]}, "not a cell access level"),
        ({"vibes": ("good",)}, "unknown hardening key"),
    )
    for mutation, expected in cases:
        original = list(AUTHZ_RULES)
        try:
            index = next(
                i for i, row in enumerate(original) if str(row["rule_id"]) == "user_self"
            )
            D.AUTHZ_RULES[index] = {**original[index], "hardening": mutation}
            report = validate_authz()
            assert any(expected in message for message in report["error_list"]), mutation
        finally:
            D.AUTHZ_RULES[:] = original


def test_a_hardening_cell_the_matrix_does_not_have_is_a_warning():
    # require_cell_access would deny every caller, so the row would be a rule
    # that cannot run -- notable, not a table integrity error.
    original = list(AUTHZ_RULES)
    try:
        index = next(i for i, row in enumerate(original) if str(row["rule_id"]) == "user_self")
        D.AUTHZ_RULES[index] = {
            **original[index],
            "hardening": {"cell": ["no_such_resource", "field"]},
        }
        report = validate_authz()
        assert report["errors"] == 0
        assert any("which the cell matrix does not have" in message for message in report["warning_list"])
    finally:
        D.AUTHZ_RULES[:] = original


def test_the_fallback_rule_first_would_shadow_everything():
    original = list(AUTHZ_RULES)
    try:
        D.AUTHZ_RULES[:] = [original[-1], *original[:-1]]
        report = validate_authz()
        # Both halves of the failure are reported: it is no longer last, and it
        # now shadows every rule that follows it.
        assert any("shadows every other rule" in message for message in report["error_list"])
        assert any("must be last" in message for message in report["error_list"])
    finally:
        D.AUTHZ_RULES[:] = original


def test_a_malformed_probe_is_an_error():
    # Name the row rather than its position: the probe table has one row that
    # resolves to no principal, and index 0 is it, so a positional mutation
    # would be testing the absent branch no matter what it set.
    cases = (
        ({"roles": ("not-a-role",)}, "unknown role"),
        ({"scopes": ("no:such:scope",)}, "unknown scope"),
        ({"auth_method": "telepathy"}, "unknown auth_method"),
        ({"claims": "not-a-mapping"}, "claims must be a mapping"),
    )
    for mutation, expected in cases:
        original = list(AUTHZ_PROBES)
        try:
            D.AUTHZ_PROBES[:] = [
                {**row, **mutation} if str(row["probe"]) == "customer" else dict(row)
                for row in original
            ]
            report = validate_authz()
            assert any(expected in message for message in report["error_list"]), mutation
        finally:
            D.AUTHZ_PROBES[:] = original


def test_a_delegated_probe_without_an_act_claim_is_a_warning():
    original = list(AUTHZ_PROBES)
    try:
        D.AUTHZ_PROBES[:] = [
            {
                **row,
                "auth_method": D.AUTH_METHOD_DELEGATED,
                "claims": {},
            }
            if str(row["probe"]) == "customer"
            else dict(row)
            for row in original
        ]
        report = validate_authz()
        assert any("no act claim" in message for message in report["warning_list"])
    finally:
        D.AUTHZ_PROBES[:] = original


def test_the_default_probe_must_exist(monkeypatch):
    monkeypatch.setitem(AUTHZ_OPS, "default_probe", "no-such-probe")
    report = validate_authz()
    assert any("which has no probe row" in message for message in report["error_list"])


# ===========================================================================
# 15. The catalog
# ===========================================================================


def test_the_catalog_exposes_every_section():
    catalog = build_authz_catalog(main.app.routes)
    assert catalog["version"] == AUTHZ_VERSION
    for key in (
        "validation",
        "scopes",
        "role_scope_grants",
        "role_scope_grants_note",
        "denials",
        "step_up_ranks",
        "rate_tiers",
        "rate_limiters",
        "exposures",
        "checks",
        "check_denials",
        "dependency_names",
        "factory_names",
        "factory_prefixes",
        "rules",
        "probes",
        "ops",
        "drift",
        "coverage",
        "backlog",
        "deps_catalog",
        "note",
    ):
        assert key in catalog, key


def test_the_catalog_reproduces_the_tables_exactly():
    catalog = build_authz_catalog()
    assert catalog["scopes"] == [dict(row) for row in SCOPE_CATALOG]
    assert catalog["denials"] == [dict(row) for row in AUTHZ_DENIALS]
    assert catalog["step_up_ranks"] == [dict(row) for row in STEP_UP_RANKS]
    assert catalog["rate_tiers"] == [dict(row) for row in RATE_TIERS]
    assert catalog["rules"] == [dict(row) for row in AUTHZ_RULES]
    assert catalog["probes"] == [dict(row) for row in AUTHZ_PROBES]
    assert catalog["ops"] == dict(AUTHZ_OPS)
    assert catalog["checks"] == list(AUTHZ_CHECK_ORDER)
    assert catalog["check_denials"] == dict(AUTHZ_CHECK_DENIALS)
    assert catalog["role_scope_grants"] == {
        role: list(scopes) for role, scopes in ROLE_SCOPE_GRANTS.items()
    }
    assert catalog["validation"]["errors"] == 0


def test_the_pinned_deps_catalog_is_reproduced_unchanged():
    # `build_deps_catalog` existed and no route surfaced it. It is reproduced
    # under `deps_catalog` with the same key set, not replaced.
    catalog = build_authz_catalog()
    assert catalog["deps_catalog"] == build_deps_catalog()
    assert set(catalog["deps_catalog"]) == {
        "auth_methods",
        "user_roles",
        "default_role",
        "dependencies",
        "factories",
        "cell_matrix_integration",
        "rate_limiter",
        "correlation_id",
    }
    assert catalog["deps_catalog"]["user_roles"] == {
        role: list(roles) for role, roles in ORIG_USER_ROLES.items()
    }


def test_the_pinned_deps_catalog_still_describes_the_unchanged_surface():
    catalog = build_deps_catalog()
    assert catalog["auth_methods"] == [
        D.AUTH_METHOD_USER,
        D.AUTH_METHOD_API_KEY,
        D.AUTH_METHOD_DELEGATED,
    ]
    assert catalog["default_role"] == "customer"
    assert catalog["dependencies"]["get_principal"] == (
        "any authenticated principal (user, API key, delegated)"
    )
    assert catalog["cell_matrix_integration"]["masks_rejected_at_authz"] is True
    assert catalog["rate_limiter"] == RATE_LIMITER.stats()
    assert catalog["correlation_id"] == {"header": "X-Correlation-ID", "generated": "uuid4 hex"}


def test_the_catalog_says_the_role_table_is_advisory():
    note = build_authz_catalog()["role_scope_grants_note"]
    assert "advisory" in note
    assert "never" in note
    assert "denied rather than treated as unscoped-and-allowed" in note


def test_the_catalog_is_json_serializable():
    json.dumps(build_authz_catalog(main.app.routes))


def test_the_catalog_carries_the_drift_and_the_backlog():
    catalog = build_authz_catalog(main.app.routes)
    assert catalog["drift"]["in_sync"] is True
    # Same exception as the drift test: the public rate limiter is bound (four
    # credential routes), the authenticated one is not.
    assert catalog["drift"]["unbound_factories"] == sorted(
        name for name in AUTHZ_FACTORY_NAMES if name != "require_public_rate_limit"
    )
    assert catalog["coverage"]["method_path_pairs"] == catalog["drift"]["method_path_pairs"]
    assert catalog["backlog"]["requirements"][0]["requirement"] == "scopes"
    # The backlog is a separate key from the rules, never folded into them.
    assert all("hardening" in row for row in catalog["rules"])


def test_the_catalog_works_without_a_route_table():
    catalog = build_authz_catalog()
    assert catalog["drift"]["routes_provided"] is False
    assert catalog["drift"]["method_path_pairs"] == 0
    assert catalog["coverage"]["routes_provided"] is False


# ===========================================================================
# 16. HTTP wiring
# ===========================================================================


def test_the_meta_root_lists_the_feature():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    assert "authz_governance" in client.get("/meta").json()["features"]


def test_the_authz_catalog_endpoint_is_reachable_over_http():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    response = client.get("/meta/authz")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["version"] == AUTHZ_VERSION
    assert payload["name"] == main.APP_NAME
    assert payload["validation"]["errors"] == 0
    assert payload["drift"]["in_sync"] is True
    assert payload["scopes"]
    assert payload["rules"]


def test_the_authz_endpoint_is_public_and_appears_as_such_in_its_own_table():
    # A governance endpoint that required the credential it describes would be
    # useless to the operator holding no credential.
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    payload = client.get("/meta/authz").json()
    row = next(
        r for r in payload["rules"] if r["rule_id"] == match_authz_rule("GET", "/meta/authz")["rule_id"]
    )
    assert row["exposure"] == "public"
    assert row["enforced_by"] == []


def test_the_scoring_catalog_endpoint_carries_the_new_section():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    payload = client.get("/meta/scoring-catalog").json()
    assert "authz_governance" in payload
    assert payload["authz_governance"]["version"] == AUTHZ_VERSION


def test_the_feature_summary_points_at_the_new_endpoint():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    payload = client.get("/meta/features").json()
    assert payload["endpoints"]["authz_catalog"] == "/meta/authz"


def test_the_ecosystem_describes_the_subservice_with_its_config_tables():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    subservice = client.get("/meta/ecosystem").json()["subservices"]["authz_governance"]
    assert "/meta/authz" in subservice["routes"]
    for table in (
        "SCOPE_CATALOG",
        "ROLE_SCOPE_GRANTS",
        "AUTHZ_DENIALS",
        "STEP_UP_RANKS",
        "RATE_TIERS",
        "AUTHZ_RULES",
        "AUTHZ_PROBES",
        "AUTHZ_CHECK_ORDER",
        "AUTHZ_CHECK_DENIALS",
        "AUTHZ_EXPOSURE_RANK",
        "AUTHZ_DEPENDENCY_NAMES",
        "AUTHZ_FACTORY_PREFIXES",
        "AUTHZ_OPS",
        "AUTHZ_VERSION",
    ):
        assert table in subservice["config_tables"], table
    assert subservice["status"] == "ready"
    assert subservice["notes"]


# ===========================================================================
# 17. The unchanged authentication path
# ===========================================================================


def test_the_principal_payload_is_unchanged():
    principal = _principal(
        roles=frozenset({"owner"}),
        scopes=frozenset({"read"}),
        tenant_id="tenant-a",
        key_id="key-1",
        delegated_by="manager@example.com",
    )
    assert principal.to_payload() == {
        "subject": "probe",
        "user_id": None,
        "roles": ["owner"],
        "scopes": ["read"],
        "tenant_id": "tenant-a",
        "auth_method": D.AUTH_METHOD_USER,
        "step_up_level": "none",
        "key_id": "key-1",
        "delegated_by": "manager@example.com",
        "correlation_id": None,
    }


def test_the_principal_predicates_are_unchanged():
    assert _principal(roles=frozenset({"admin"})).is_admin is True
    assert _principal(roles=frozenset({"owner"})).is_admin is False
    assert _principal(auth_method=D.AUTH_METHOD_API_KEY).is_service_account is True
    assert _principal().is_service_account is False
    assert _principal().user_id is None


def test_the_claim_helpers_are_unchanged():
    assert deps._roles_from_claims({"roles": "admin"}) == frozenset({"admin", "agent", "auditor", "owner"})
    assert deps._roles_from_claims({"roles": ["agent"]}) == frozenset({"agent"})
    assert deps._roles_from_claims({}) == frozenset()
    assert deps._roles_from_claims({"roles": []}) == frozenset()
    assert deps._scopes_from_claims({"scopes": "read write"}) == frozenset({"read", "write"})
    assert deps._scopes_from_claims({"scope": ["audit"]}) == frozenset({"audit"})
    assert deps._scopes_from_claims({}) == frozenset()


def test_the_correlation_id_helper_reuses_an_inbound_id():
    from starlette.requests import Request as _Request

    def _request(*, correlation_id: str | None = None):
        headers = []
        if correlation_id is not None:
            headers.append((b"x-correlation-id", correlation_id.encode("utf-8")))
        return _Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": headers,
                "query_string": b"",
            }
        )

    minted = deps.get_correlation_id(_request())
    assert len(minted) == 32
    assert deps.get_correlation_id(_request(correlation_id="given-id")) == "given-id"


def test_the_five_historical_dependencies_keep_their_signatures():
    # Same parameters, same order. A dependency re-ordered here is a
    # ``Depends``-by-position bug waiting to happen somewhere else.
    assert list(inspect.signature(deps.get_principal).parameters) == ["principal"]
    assert list(inspect.signature(deps.get_optional_principal).parameters) == [
        "request",
        "credentials",
        "db",
    ]
    assert list(inspect.signature(deps.get_current_user).parameters) == [
        "credentials",
        "db",
    ]
    assert list(inspect.signature(deps.get_current_user_optional).parameters) == [
        "credentials",
        "db",
    ]
    assert list(inspect.signature(deps.get_current_admin_user).parameters) == ["current_user"]
    assert list(inspect.signature(deps.get_current_policy_or_admin_user).parameters) == [
        "current_user",
        "db",
    ]
    assert list(inspect.signature(deps.get_current_customer_policy_score).parameters) == [
        "current_user",
        "db",
    ]
    assert list(inspect.signature(deps.current_control_posture).parameters) == ["current_user"]


def test_the_factory_signatures_are_unchanged():
    assert list(inspect.signature(require_scopes).parameters) == ["scopes"]
    assert list(inspect.signature(require_roles).parameters) == ["roles"]
    assert list(inspect.signature(require_step_up).parameters) == ["level"]
    assert list(inspect.signature(require_cell_access).parameters) == [
        "resource",
        "cell",
        "minimum",
        "context",
    ]
    assert list(inspect.signature(require_tenant).parameters) == ["request", "principal"]
    assert list(inspect.signature(require_rate_limit).parameters) == [
        "capacity",
        "refill_per_second",
    ]
    assert list(inspect.signature(require_rate_tier).parameters) == ["tier"]
    cell = inspect.signature(require_cell_access).parameters
    assert cell["minimum"].default == "read"
    assert cell["minimum"].kind is inspect.Parameter.KEYWORD_ONLY
    assert cell["context"].default is None
    assert inspect.signature(require_rate_tier).parameters["tier"].default == "interactive"


def test_the_token_bucket_arithmetic_is_unchanged():
    limiter = D.TokenBucketLimiter(capacity=3, refill_per_second=1.0)
    decisions = [limiter.check("k", now=0.0) for _ in range(4)]
    assert [row["allowed"] for row in decisions] == [True, True, True, False]
    assert decisions[0]["capacity"] == 3
    assert decisions[0]["refill_per_second"] == 1.0
    # A half second later half a token is back, which is not enough.
    assert limiter.check("k", now=0.5)["allowed"] is False
    # A full second is exactly one token.
    assert limiter.check("k", now=1.0)["allowed"] is True
    assert limiter.check("k", now=1.0)["allowed"] is False
    # Refill is capped at the bucket's capacity.
    assert limiter.check("k", now=100.0)["remaining"] == 2
    # A capacity of 0 is clamped up, not a divide-by-zero at construction.
    assert D.TokenBucketLimiter(capacity=0).capacity == 1
    assert D.TokenBucketLimiter(capacity=5, refill_per_second=-3).refill_per_second == 0.0
    limiter.reset("k")
    assert limiter.stats()["tracked_keys"] == 0
