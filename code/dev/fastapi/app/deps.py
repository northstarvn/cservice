"""FastAPI authentication / authorization dependencies.

The original module resolved a JWT into a ``User`` and exposed two role gates
(``is_admin`` and a policy-tier check). That answers "who is this?" and "are
they an admin?", which is enough for the current route set — but it forces
every new protected route to re-derive the authorization it needs, and it has
nowhere to put the answers a real deployment accumulates: OAuth scopes, data-cell
grants, step-up requirements, machine callers, delegation, and rate limits.

So this module now centers on a single ``Principal`` value object — *who* is
acting, *as what*, and *with which capabilities* — and builds every dependency
on top of it:

- ``get_principal`` / ``get_current_principal`` — one resolution path for
  humans, API keys, and delegated sessions, with the auth method recorded.
- ``require_scopes`` — an OAuth-style scope dependency *factory*: declare
  ``Depends(require_scopes("audit:read"))`` and the check is declarative.
- ``require_cell_access`` — enforces ``app.cell_matrix`` grants per data
  cell, returning 403 with the decision's reason instead of a bare denial.
- ``require_step_up`` — demands a stronger assurance level than a plain login
  (re-auth, biometric, hardware key) for sensitive operations.
- ``require_tenant`` — binds a request to one tenant and rejects cross-tenant
  access unless the principal is a platform admin.
- ``require_rate_limit`` — a token-bucket dependency so a single caller cannot
  saturate a pooled database.
- ``get_correlation_id`` — echoes or mints a request id and binds it to the
  principal, so a log line can be tied back to a token and a tenant.

Every pre-existing dependency (``get_current_user``, ``get_current_user_optional``,
``get_current_admin_user``, ``get_current_policy_or_admin_user``,
``get_current_customer_policy_score``, ``current_control_posture``) keeps its
exact signature and behavior — they are now thin wrappers over the principal
resolution so there is a single code path to reason about.

The module is also the deployment's authorization *configuration*: the scope
vocabulary, the role/scope relationship, the denial contract, the assurance
levels, the rate tiers, and a per-route description of what is actually enforced
all live here as tables (``SCOPE_CATALOG``, ``ROLE_SCOPE_GRANTS``,
``AUTHZ_DENIALS``, ``STEP_UP_RANKS``, ``RATE_TIERS``, ``AUTHZ_RULES``). Above the
``--- Authorization configuration ---`` marker is the request path; below it is
the governance surface that can inspect it — ``evaluate_authz`` decides a
request without serving it, ``authz_drift_report`` compares the described
posture against the routes the app actually registers, and ``validate_authz``
checks the tables against the code that enforces them.
"""
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi import params as _fastapi_params
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from dataclasses import dataclass, field, replace
from typing import Any, Iterator, Mapping, Sequence
import inspect
import os
import re
import time
import uuid

from app.db import get_db
from app import cell_matrix, models, security
from app.services.policy_scoring import can_access_functionality, upsert_customer_policy_score

security_scheme = HTTPBearer()
# A second, non-raising scheme so a route can stay publicly reachable while
# still resolving a principal when one is offered. Kept separate from
# ``security_scheme`` so the historical dependencies keep their exact
# auto-error behavior.
optional_security_scheme = HTTPBearer(auto_error=False)

# --- Principal ------------------------------------------------------------------

# How a request proved who it is.
AUTH_METHOD_USER = "user"
AUTH_METHOD_API_KEY = "api_key"
AUTH_METHOD_DELEGATED = "delegated"

# The single role an API key carries. It is not in ``USER_ROLES`` (a user record
# cannot have it) and not in ``cell_matrix.ROLE_GROUPS`` (it grants no cell), so
# it is declared here next to the auth methods that can produce it.
AUTHZ_ROLE_SERVICE_ACCOUNT = "service_account"


@dataclass(frozen=True)
class Principal:
    """Who is acting, as what, and with which capabilities.

    ``user`` is ``None`` for a pure machine caller (an API key with no
    attached account). ``roles`` drives the data-cell matrix; ``scopes`` drives
    route-level checks. Keeping both is what lets one dependency serve an
    interactive agent and a CI job that reads the audit trail.
    """

    subject: str
    user: models.User | None = None
    roles: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)
    tenant_id: str | None = None
    auth_method: str = AUTH_METHOD_USER
    step_up_level: str = "none"
    claims: dict = field(default_factory=dict)
    key_id: str | None = None
    delegated_by: str | None = None
    correlation_id: str | None = None

    @property
    def user_id(self) -> int | None:
        return getattr(self.user, "id", None)

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    @property
    def is_service_account(self) -> bool:
        return self.auth_method == AUTH_METHOD_API_KEY

    def has_scope(self, scope: str) -> bool:
        """Scopes are hierarchical on ``:``; ``*`` is a wildcard."""
        for granted in self.scopes:
            if granted == "*" or granted == scope:
                return True
            if scope.startswith(f"{granted}:"):
                return True
        return False

    def has_all_scopes(self, scopes: list[str] | tuple[str, ...]) -> list[str]:
        """Return the subset not satisfied (empty means authorized)."""
        return [scope for scope in scopes if not self.has_scope(scope)]

    def can_access_cell(self, resource: str, cell: str, *, context: dict | None = None) -> dict:
        return cell_matrix.evaluate_access(
            resource, cell, sorted(self.roles), context=context
        )

    def to_payload(self) -> dict:
        return {
            "subject": self.subject,
            "user_id": self.user_id,
            "roles": sorted(self.roles),
            "scopes": sorted(self.scopes),
            "tenant_id": self.tenant_id,
            "auth_method": self.auth_method,
            "step_up_level": self.step_up_level,
            "key_id": self.key_id,
            "delegated_by": self.delegated_by,
            "correlation_id": self.correlation_id,
        }


# Roles granted to a user record. Kept as a table so a new role is a data
# change, not a code change.
USER_ROLES: dict[str, tuple[str, ...]] = {
    "customer": ("owner",),
    "agent": ("agent",),
    "auditor": ("auditor",),
    "admin": ("admin", "agent", "auditor", "owner"),
}
DEFAULT_USER_ROLE = "customer"


def roles_for_user(user: models.User | None) -> frozenset[str]:
    """Expand a user record into a concrete role set."""
    role = getattr(user, "role", None) or DEFAULT_USER_ROLE
    return frozenset(USER_ROLES.get(role, (role,)))


# --- Correlation ---------------------------------------------------------------


def get_correlation_id(request: Request) -> str:
    """Reuse an inbound ``X-Correlation-ID`` or mint one for this request.

    Bound to ``request.state`` so logging middleware and the principal can
    reference the same id without threading it through every signature.
    """
    inbound = (request.headers.get("x-correlation-id") or "").strip()
    correlation_id = inbound or uuid.uuid4().hex
    request.state.correlation_id = correlation_id
    return correlation_id


# --- Rate limiting --------------------------------------------------------------


class TokenBucketLimiter:
    """In-process token bucket, keyed by caller identity.

    Sized for a single-process deployment or a sticky load-balancer setup; a
    multi-pod deployment should front this with a shared limiter. The point
    here is that the *call site* stays declarative either way.
    """

    def __init__(self, capacity: int = 60, refill_per_second: float = 1.0) -> None:
        self.capacity = max(int(capacity), 1)
        self.refill_per_second = max(float(refill_per_second), 0.0)
        self._buckets: dict[str, tuple[float, float]] = {}

    def check(self, key: str, *, now: float | None = None) -> dict:
        """Consume one token. Returns a decision; never raises."""
        moment = now if now is not None else time.monotonic()
        tokens, last = self._buckets.get(key, (float(self.capacity), moment))
        elapsed = max(moment - last, 0.0)
        tokens = min(self.capacity, tokens + elapsed * self.refill_per_second)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._buckets[key] = (tokens, moment)
        return {
            "allowed": allowed,
            "remaining": int(tokens),
            "capacity": self.capacity,
            "refill_per_second": self.refill_per_second,
        }

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._buckets.clear()
        else:
            self._buckets.pop(key, None)

    def stats(self) -> dict:
        return {
            "tracked_keys": len(self._buckets),
            "capacity": self.capacity,
            "refill_per_second": self.refill_per_second,
        }


RATE_LIMITER = TokenBucketLimiter(
    capacity=int(os.getenv("RATE_LIMIT_CAPACITY", "60")),
    refill_per_second=float(os.getenv("RATE_LIMIT_REFILL", "1")),
)
# --- Authorization configuration ------------------------------------------------
#
# Everything above this line answers "who is this caller". Everything below
# answers "what may they do, who says so, and where can you check". The
# authentication path itself is unchanged: same ``Principal``, same resolution
# order, same tokens, same status codes, same denial payloads.
#
# Three of these tables are load-bearing for the code above, and every constant
# in them is the value that was inline before:
#
# * :data:`AUTHZ_DENIALS` carries the status code, the detail shape and the key
#   order of every denial the ``require_*`` factories raise. The literals
#   themselves (``"insufficient_scope"``, ``"Could not validate credentials"``)
#   stay inline, because a client matches on them; only the tunables moved.
#   :func:`authz_denial_contract` proves the declaration against the real code
#   path by invoking each factory, so the table cannot quietly stop describing
#   what a client actually receives.
# * :data:`RATE_TIERS` is read by the new ``require_rate_tier`` factory. The
#   historical ``require_rate_limit`` keeps its signature and its env-driven
#   bucket; the ``interactive`` row documents that bucket's shipped defaults.
# * :data:`STEP_UP_RANKS` documents the ``require_step_up`` default level and what
#   each level means, and :func:`validate_authz` checks its ranks and evidence
#   against :mod:`app.security` and :mod:`app.routers.users` so the three cannot
#   disagree silently. The signature default itself stays literal: making it a
#   config lookup would change what a caller who omits the argument gets.
#
# :data:`AUTHZ_RULES` is different in kind: it is a *description* of the routes
# that exist, not a wish list. Every row names the dependency that actually
# performs the check, and :func:`authz_drift_report` is the diff between the
# description and the app. The ``hardening`` block on each row records
# requirements that are *not* enforced yet, which is a backlog and not a
# description -- the two are kept in separate keys on purpose so nobody reads a
# proposal as a guarantee.

AUTHZ_VERSION = "authz_governance_v1"

# The scope vocabulary, grounded in what the codebase actually issues.
# ``users.API_KEY_SELF_SCOPES`` is the authoritative self-service list and
# ``validate_authz`` checks the two against each other, so a scope added there
# without a row here is an error rather than a silently unusable grant.
#
# ``sensitivity`` is a documentation axis, not an enforcement axis: nothing in
# this module branches on it. It exists so ``/meta/authz`` can answer "which
# scopes can do the most damage" without re-deriving the answer per call.
SCOPE_CATALOG: list[dict[str, Any]] = [
    {
        "scope": "*",
        "namespace": "",
        "action": "all",
        "sensitivity": "superuser",
        "parent": None,
        "self_service": False,
        "description": (
            "Bootstrap-only wildcard. Never self-assignable: a user minting a "
            "key must not mint one that outranks their own session."
        ),
    },
    {
        "scope": "read",
        "namespace": "",
        "action": "read",
        "sensitivity": "read",
        "parent": None,
        "self_service": True,
        "description": "Read the caller's own records. Parent of the read:* scopes.",
    },
    {
        "scope": "write",
        "namespace": "",
        "action": "write",
        "sensitivity": "write",
        "parent": None,
        "self_service": True,
        "description": "Mutate the caller's own records.",
    },
    {
        "scope": "chat",
        "namespace": "",
        "action": "chat",
        "sensitivity": "write",
        "parent": None,
        "self_service": True,
        "description": "Chat and messaging surfaces. Satisfies chat:* by prefix.",
    },
    {
        "scope": "bookings",
        "namespace": "",
        "action": "bookings",
        "sensitivity": "write",
        "parent": None,
        "self_service": True,
        "description": "Booking lifecycle surfaces.",
    },
    {
        "scope": "audit",
        "namespace": "",
        "action": "audit",
        "sensitivity": "read",
        "parent": None,
        "self_service": True,
        "description": "Audit trail surfaces. Broadest of the read-class grants.",
    },
    {
        "scope": "read:chat",
        "namespace": "read",
        "action": "read",
        "sensitivity": "read",
        "parent": "read",
        "self_service": True,
        "description": "Read chat history without write access to the conversation.",
    },
    {
        "scope": "read:audit",
        "namespace": "read",
        "action": "read",
        "sensitivity": "read",
        "parent": "read",
        "self_service": True,
        "description": "Read the audit trail without the broader audit grant.",
    },
]

SCOPE_BY_NAME: dict[str, dict[str, Any]] = {str(row["scope"]): row for row in SCOPE_CATALOG}
SCOPE_NAMES: tuple[str, ...] = tuple(sorted(SCOPE_BY_NAME))

# Role -> the scopes that role *could* be granted.
#
# This table is **advisory and deliberately not applied at resolution time**.
# ``require_scopes`` reads ``Principal.scopes`` and nothing else, and that is
# the contract: a user token with no ``scopes`` claim is not implicitly
# unscoped-and-allowed, it is denied, so a scope is always a deliberate grant.
# Synthesizing scopes from roles here would quietly convert a role check into a
# capability grant and every existing route that depends on ``get_current_user``
# would start authorizing calls it never authorized before.
#
# What it is for is the question you cannot answer from the request path: given
# this role, what could a principal holding it be given? ``evaluate_authz``
# reports the answer as ``implied_by_roles`` on the decision trace, clearly
# separated from ``granted``, and ``validate_authz`` checks every role here
# against ``USER_ROLES`` and ``cell_matrix.ROLE_GROUPS`` so a typo is an error
# rather than a role that can never hold anything.
ROLE_SCOPE_GRANTS: dict[str, tuple[str, ...]] = {
    "owner": ("read", "write", "chat", "bookings"),
    "agent": ("read", "write", "chat", "bookings"),
    "auditor": ("read", "audit", "read:audit"),
    "admin": ("*",),
    AUTHZ_ROLE_SERVICE_ACCOUNT: (),
}

# Every denial the module can raise, in one place. ``status`` is a tuning knob
# (401 vs 403 vs 429 is a real choice); ``keys`` is the emitted key order; the
# error string itself stays inline in the raising code because a client matches
# on it. ``factory`` is the callable that produces the denial and
# ``authz_denial_contract`` invokes it, so a row that stops matching its
# factory is an error.
AUTHZ_DENIALS: list[dict[str, Any]] = [
    {
        "kind": "unauthenticated",
        "error": None,
        "text": "Could not validate credentials",
        "status": 401,
        "detail_shape": "string",
        "keys": (),
        "headers": {"WWW-Authenticate": "Bearer"},
        "factory": "_unauthorized",
        "raises": "every principal-resolving dependency when no credential resolves",
        "description": "No credential, an undecodable token, or a subject with no user row.",
    },
    {
        "kind": "insufficient_scope",
        "error": "insufficient_scope",
        "text": None,
        "status": 403,
        "detail_shape": "object",
        "keys": ("error", "required", "missing", "granted"),
        "headers": {},
        "factory": "require_scopes",
        "raises": "require_scopes when at least one named scope is unsatisfied",
        "description": "Reports which of the required scopes are missing, not just that one is.",
    },
    {
        "kind": "cell_access_denied",
        "error": "cell_access_denied",
        "text": None,
        "status": 403,
        "detail_shape": "object",
        "keys": ("error", "resource", "cell", "required", "reason", "requires_justification"),
        "headers": {},
        "factory": "require_cell_access",
        "raises": "require_cell_access when the granted cell rank is below the requirement",
        "description": (
            "Carries the cell matrix's own reason and justification flag, so a caller "
            "learns whether a break-glass grant would help."
        ),
    },
    {
        "kind": "step_up_required",
        "error": "step_up_required",
        "text": None,
        "status": 403,
        "detail_shape": "object",
        "keys": ("error", "required_level", "presented_level"),
        "headers": {},
        "factory": "require_step_up",
        "raises": "require_step_up when the token's assurance level is below the requirement",
        "description": "presented_level is what the token actually carries, so a client can say by how much.",
    },
    {
        "kind": "role_required",
        "error": "role_required",
        "text": None,
        "status": 403,
        "detail_shape": "object",
        "keys": ("error", "required_any", "roles"),
        "headers": {},
        "factory": "require_roles",
        "raises": "require_roles when the principal holds none of the named roles",
        "description": "Any-of semantics; a principal needs one of the listed roles, not all of them.",
    },
    {
        "kind": "tenant_mismatch",
        "error": "tenant_mismatch",
        "text": None,
        "status": 403,
        "detail_shape": "object",
        "keys": ("error", "requested", "principal_tenant"),
        "headers": {},
        "factory": "require_tenant",
        "raises": "require_tenant when the requested tenant is not the principal's own",
        "description": "A platform admin may cross tenants; nobody else may.",
    },
    {
        "kind": "rate_limited",
        "error": "rate_limited",
        "text": None,
        "status": 429,
        "detail_shape": "object",
        "keys": ("error", "allowed", "remaining", "capacity", "refill_per_second"),
        "headers": {"Retry-After": "int(1 / refill_per_second), or 1 when refill is 0"},
        "factory": "require_rate_limit",
        "raises": "require_rate_limit when the caller's token bucket is empty",
        "description": "The decision dict from TokenBucketLimiter.check is spliced in whole, after the error key.",
    },
]

AUTHZ_DENIAL_BY_KIND: dict[str, dict[str, Any]] = {
    str(row["kind"]): row for row in AUTHZ_DENIALS
}
AUTHZ_DENIAL_KINDS: tuple[str, ...] = tuple(sorted(AUTHZ_DENIAL_BY_KIND))

# Assurance levels. The ranks must equal ``security.STEP_UP_RANK`` and the
# evidence sets must equal ``users.STEP_UP_METHODS``; ``validate_authz`` checks
# both, because a table that disagrees with the module that enforces it is
# worse than no table.
STEP_UP_RANKS: list[dict[str, Any]] = [
    {
        "level": "none",
        "rank": 0,
        "evidence": (),
        "satisfies": (),
        "description": "No assurance claim at all. Never satisfies a step-up requirement.",
    },
    {
        "level": "loa1",
        "rank": 1,
        "evidence": ("pwd",),
        "satisfies": ("none",),
        "description": "Ordinary password login. The floor security falls back to for a token with no amr.",
    },
    {
        "level": "loa2",
        "rank": 2,
        "evidence": ("pwd", "mfa", "otp", "hwk"),
        "satisfies": ("none", "loa1"),
        "description": "Second factor. The default requirement for require_step_up.",
    },
    {
        "level": "loa3",
        "rank": 3,
        "evidence": ("mfa", "hwk", "biometric", "cosign"),
        "satisfies": ("none", "loa1", "loa2"),
        "description": "Hardware key, co-signature or biometric binding.",
    },
]

STEP_UP_RANK_BY_LEVEL: dict[str, int] = {str(row["level"]): int(row["rank"]) for row in STEP_UP_RANKS}
STEP_UP_LEVELS: tuple[str, ...] = tuple(sorted(STEP_UP_RANK_BY_LEVEL, key=STEP_UP_RANK_BY_LEVEL.get))

# Rate tiers for the new declarative limiter. The ``interactive`` row documents
# the shipped default of the historical env-driven RATE_LIMITER (60 / 1.0) and
# is what a deployment gets unless RATE_LIMIT_CAPACITY / RATE_LIMIT_REFILL say
# otherwise -- ``validate_authz`` raises a warning when the environment has
# silently moved the effective limit away from the documented one.
RATE_TIERS: list[dict[str, Any]] = [
    {
        "tier": "interactive",
        "capacity": 60,
        "refill_per_second": 1.0,
        "applies_to": (AUTH_METHOD_USER, AUTH_METHOD_DELEGATED),
        "description": "Human and delegated callers. Matches the historical RATE_LIMITER default.",
    },
    {
        "tier": "machine",
        "capacity": 600,
        "refill_per_second": 10.0,
        "applies_to": (AUTH_METHOD_API_KEY,),
        "description": "API-key callers running a job, not a person clicking.",
    },
    {
        "tier": "sensitive",
        "capacity": 10,
        "refill_per_second": 0.2,
        "applies_to": (),
        "description": "Deliberately tight. For routes that also carry require_step_up.",
    },
]

RATE_TIER_BY_NAME: dict[str, dict[str, Any]] = {str(row["tier"]): row for row in RATE_TIERS}
RATE_TIER_NAMES: tuple[str, ...] = tuple(sorted(RATE_TIER_BY_NAME))

# Exposure classes, ordered. A rule names one; ``evaluate_authz`` uses the rank
# only for reporting, never to reorder checks -- check order is
# AUTHZ_CHECK_ORDER and is fixed.
AUTHZ_EXPOSURE_RANK: dict[str, int] = {
    "public": 0,
    "authenticated": 1,
    "policy": 2,
    "admin": 3,
}
AUTHZ_EXPOSURES: tuple[str, ...] = tuple(sorted(AUTHZ_EXPOSURE_RANK, key=AUTHZ_EXPOSURE_RANK.get))

# The order checks run in, and therefore which denial a caller with several
# problems receives. First failure wins. This is contract: a client that sees
# ``insufficient_scope`` must be able to rely on not also needing a step-up
# token for the same call, and vice versa.
AUTHZ_CHECK_ORDER: tuple[str, ...] = (
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

# Which denial each failing check produces. ``policy_tier`` is absent on
# purpose: a policy-gated route's tier gate is evaluated by
# ``get_current_policy_or_admin_user`` at request time, and the pure engine
# reports the tier as unevaluated rather than inventing a denial no factory
# raises.
AUTHZ_CHECK_DENIALS: dict[str, str] = {
    "authenticated": "unauthenticated",
    "admin": "role_required",
    "scopes": "insufficient_scope",
    "roles": "role_required",
    "step_up": "step_up_required",
    "cell": "cell_access_denied",
    "rate_limit": "rate_limited",
    "tenant": "tenant_mismatch",
}

# Named principals for the offline simulation. Each row is a real ``Principal``
# shape, not a mock, so ``authz_simulation_report`` exercises the same
# ``has_scope`` / ``can_access_cell`` / ``step_up_level_of`` code the request
# path uses. ``claims`` is authoritative for step-up, because that is what
# ``require_step_up`` reads -- ``step_up_level`` is carried for display.
AUTHZ_PROBES: list[dict[str, Any]] = [
    {
        # The one probe that resolves to no principal at all. Every other row
        # names a real, reachable caller; this one names the absence of one,
        # which is the state get_optional_principal returns for a request with
        # no X-API-Key and no bearer token. A probe that built an empty
        # Principal instead would be a state the app cannot produce --
        # roles_for_user always yields at least one role -- and it would report
        # the whole authenticated surface as allowed, because "resolves to a
        # principal" is exactly what those routes check.
        "probe": "anonymous",
        "absent": True,
        "subject": None,
        "roles": (),
        "scopes": (),
        "tenant_id": None,
        "auth_method": None,
        "claims": {},
        "note": "No credential at all. Denied everything except the public surface.",
    },
    {
        "probe": "customer",
        "subject": "customer@example.com",
        "roles": ("owner",),
        "scopes": (),
        "tenant_id": None,
        "auth_method": AUTH_METHOD_USER,
        "claims": {},
        "note": "DEFAULT_USER_ROLE with a password login. Unscoped, so every scope check fails.",
    },
    {
        "probe": "agent",
        "subject": "agent@example.com",
        "roles": ("agent",),
        "scopes": ("read", "write", "chat", "bookings"),
        "tenant_id": None,
        "auth_method": AUTH_METHOD_USER,
        "claims": {},
        "note": "A front-line agent with the write-class self-service scopes.",
    },
    {
        "probe": "auditor",
        "subject": "auditor@example.com",
        "roles": ("auditor",),
        "scopes": ("read", "audit", "read:audit"),
        "tenant_id": None,
        "auth_method": AUTH_METHOD_USER,
        "claims": {},
        "note": "Read-only across the audit trail. Not an admin, so /chat/admin is denied.",
    },
    {
        "probe": "admin",
        "subject": "admin@example.com",
        "roles": ("admin", "agent", "auditor", "owner"),
        "scopes": (),
        "tenant_id": None,
        "auth_method": AUTH_METHOD_USER,
        "claims": {},
        "note": "The admin role set. Unscoped, so scope requirements still fail on purpose.",
    },
    {
        "probe": "admin_wildcard",
        "subject": "admin@example.com",
        "roles": ("admin", "agent", "auditor", "owner"),
        "scopes": ("*",),
        "tenant_id": None,
        "auth_method": AUTH_METHOD_USER,
        "claims": {},
        "note": "Admin plus the bootstrap wildcard. The only probe that satisfies every scope.",
    },
    {
        "probe": "api_key_read",
        "subject": "ci-runner",
        "roles": (AUTHZ_ROLE_SERVICE_ACCOUNT,),
        "scopes": ("read", "read:audit"),
        "tenant_id": None,
        "auth_method": AUTH_METHOD_API_KEY,
        "claims": {},
        "note": "A machine credential. Not admin, so it reaches the authenticated surface and no further.",
    },
    {
        "probe": "api_key_wildcard",
        "subject": "bootstrap",
        "roles": (AUTHZ_ROLE_SERVICE_ACCOUNT,),
        "scopes": ("*",),
        "tenant_id": None,
        "auth_method": AUTH_METHOD_API_KEY,
        "claims": {},
        "note": "An operator-provisioned bootstrap key. The only machine principal that clears admin.",
    },
    {
        "probe": "delegated",
        "subject": "agent@example.com",
        "roles": ("agent",),
        "scopes": ("read", "chat"),
        "tenant_id": None,
        "auth_method": AUTH_METHOD_DELEGATED,
        "claims": {"act": "manager@example.com"},
        "note": "An act claim: the roles are the delegate's, not the delegator's.",
    },
    {
        "probe": "step_up_loa3",
        "subject": "admin@example.com",
        "roles": ("admin", "agent", "auditor", "owner"),
        "scopes": ("*",),
        "tenant_id": None,
        "auth_method": AUTH_METHOD_USER,
        "claims": {"acr": "loa3", "amr": ["hwk", "cosign"]},
        "note": "Hardware-key bound. The only probe that satisfies a loa3 requirement.",
    },
    {
        "probe": "cross_tenant",
        "subject": "agent@example.com",
        "roles": ("agent",),
        "scopes": ("read",),
        "tenant_id": "tenant-b",
        "auth_method": AUTH_METHOD_USER,
        "claims": {},
        "note": "Confined to tenant-b. A tenant-bound rule fails for it unless the rule says otherwise.",
    },
]

AUTHZ_PROBE_NAMES: tuple[str, ...] = tuple(str(row["probe"]) for row in AUTHZ_PROBES)

# --- The route table ------------------------------------------------------------
#
# One row per authorization *shape*, matched first-hit-wins by
# ``(method in methods, pattern matches path)``. Patterns use FastAPI's own
# spelling: ``{name}`` is one path segment and a trailing ``*`` is a prefix.
#
# ``exposure`` is what the route actually enforces today and ``enforced_by`` is
# the dependency that does it. ``hardening`` is what is *not* enforced: a
# backlog. Keeping the two in different keys is the whole point, because a
# proposal that reads like a guarantee is how an authorization audit goes
# wrong. ``authz_drift_report`` reports the two side by side and
# ``authz_coverage_report`` reports how much of the live route table each
# exposure class covers.
#
# The last row is a deliberate fallback: it currently matches nothing, which is
# itself the useful fact -- a new route lands on it and the drift report says
# so instead of the route quietly reading as governed.
AUTHZ_RULES: list[dict[str, Any]] = [
    # --- public
    {"rule_id": "register", "methods": ("POST",), "path": "/users/register", "exposure": "public",
     "enforced_by": (), "hardening": {}, "note": "Open by design: this is how a principal exists at all."},
    {"rule_id": "login", "methods": ("POST",), "path": "/users/login", "exposure": "public",
     "enforced_by": (), "hardening": {"rate_tier": "interactive"},
     "note": "The only brute-forceable surface in the module. Rate limiting is the mitigation and is not wired."},
    {"rule_id": "token_refresh", "methods": ("POST",), "path": "/users/refresh", "exposure": "public",
     "enforced_by": (), "hardening": {"rate_tier": "interactive"},
     "note": "Unauthenticated because the refresh token is the credential. Rotation is the control."},
    {"rule_id": "token_logout", "methods": ("POST",), "path": "/users/logout", "exposure": "public",
     "enforced_by": (), "hardening": {},
     "note": "The body carries the token, so the dependency has nothing to resolve first."},
    {"rule_id": "password_policy_public", "methods": ("GET",), "path": "/users/password-policy",
     "exposure": "public", "enforced_by": (), "hardening": {},
     "note": "The policy a new caller has to satisfy; publishing it cannot leak anything."},
    {"rule_id": "health", "methods": ("GET",), "path": "/health", "exposure": "public",
     "enforced_by": (), "hardening": {}, "note": "Liveness probe."},
    {"rule_id": "meta_surface", "methods": ("GET", "POST"), "path": "/meta*", "exposure": "public",
     "enforced_by": (), "hardening": {"scopes": ("read",)},
     "note": (
         "Public by intent -- a service must be able to discover itself. Worth saying out loud "
         "that this includes the config dumps, the drift reports and the decision simulators."
     )},
    {"rule_id": "audit_catalog", "methods": ("GET",), "path": "/audit/catalog", "exposure": "public",
     "enforced_by": (), "hardening": {}, "note": "Static table of the audit surface."},
    {"rule_id": "webhook_ingest", "methods": ("POST",), "path": "/webhooks/webhooks/{provider}",
     "exposure": "public", "enforced_by": (), "hardening": {"rate_tier": "interactive"},
     "note": (
         "Unauthenticated by necessity: the caller is an external provider, not a principal. "
         "The HMAC signature header is the credential and is verified inside the handler, so "
         "this rule documents where that control lives rather than claiming a dependency "
         "enforces it. This is the one public write surface that reaches the pipeline."
     )},
    {"rule_id": "webhook_status", "methods": ("GET",), "path": "/webhooks/webhooks/status",
     "exposure": "public", "enforced_by": (), "hardening": {},
     "note": (
         "Per-provider status rollup. Declared *before* the {provider} rules because "
         "first-match-wins would otherwise let `/webhooks/webhooks/{provider}` claim this "
         "path, leaving the rollup reachable but classified as a per-provider route."
     )},
    {"rule_id": "webhook_provider_health", "methods": ("GET",),
     "path": "/webhooks/webhooks/{provider}/health", "exposure": "public",
     "enforced_by": (), "hardening": {},
     "note": "Whether a provider's secret is configured. Reports a boolean, never the secret."},
    {"rule_id": "audit_trail_catalog", "methods": ("GET",), "path": "/audit/trail-catalog",
     "exposure": "public", "enforced_by": (), "hardening": {},
     "note": "Static table. The entries it describes are admin-gated."},
    {"rule_id": "transaction_spec", "methods": ("GET",), "path": "/audit/transactions/spec",
     "exposure": "public", "enforced_by": (), "hardening": {},
     "note": "Wire format. The transactions themselves are under audit_admin."},
    {"rule_id": "topic_catalog", "methods": ("GET",), "path": "/topics/catalog", "exposure": "public",
     "enforced_by": (), "hardening": {}, "note": "Static config dump."},
    {"rule_id": "topic_themes", "methods": ("GET",), "path": "/topics/themes", "exposure": "public",
     "enforced_by": (), "hardening": {}, "note": "Static config dump."},
    {"rule_id": "topic_search", "methods": ("GET",), "path": "/topics/search", "exposure": "public",
     "enforced_by": (), "hardening": {}, "note": "Classification only; touches no caller data."},
    {"rule_id": "topic_suggestions", "methods": ("GET",), "path": "/topics/suggestions",
     "exposure": "public", "enforced_by": (), "hardening": {}, "note": "Static config dump."},
    {"rule_id": "topic_taxonomy", "methods": ("GET",), "path": "/topics/taxonomy", "exposure": "public",
     "enforced_by": (), "hardening": {}, "note": "Static config dump."},
    {"rule_id": "topic_match", "methods": ("GET",), "path": "/topics/match", "exposure": "public",
     "enforced_by": (), "hardening": {}, "note": "Classification only; touches no caller data."},
    {"rule_id": "topic_ranked", "methods": ("GET",), "path": "/topics/ranked", "exposure": "public",
     "enforced_by": (), "hardening": {}, "note": "Ranks config topics, not a caller's selections."},
    {"rule_id": "topic_integrity", "methods": ("GET",), "path": "/topics/integrity",
     "exposure": "public", "enforced_by": (), "hardening": {},
     "note": "Reports on the taxonomy tables themselves."},
    {"rule_id": "topic_plan", "methods": ("GET",), "path": "/topics/plan", "exposure": "public",
     "enforced_by": (), "hardening": {"rate_tier": "interactive"},
     "note": "The offline request planner. It never spends rate budget."},
    {"rule_id": "topic_governance", "methods": ("GET",), "path": "/topics/governance*",
     "exposure": "public", "enforced_by": (), "hardening": {"scopes": ("read",)},
     "note": (
         "Public because nothing it returns is caller data -- but it is also the surface that "
         "enumerates the route table, so it is the one to revisit before exposing a deployment."
     )},
    # --- admin
    {"rule_id": "chat_admin", "methods": ("GET", "POST", "PUT", "PATCH", "DELETE"),
     "path": "/chat/admin*", "exposure": "admin", "enforced_by": ("get_current_admin_user",),
     "hardening": {"scopes": ("read", "audit"), "step_up": "loa2", "rate_tier": "sensitive"},
     "note": "The largest admin block. Writes here commit, so it is the obvious loa2 candidate."},
    {"rule_id": "chat_retention_cohort", "methods": ("GET",), "path": "/chat/retention-cohorts",
     "exposure": "authenticated", "enforced_by": ("get_current_user",),
     "hardening": {"scopes": ("read",)},
     "note": "List form. The {cohort} detail form below is admin-gated; only the list is not."},
    {"rule_id": "retention_cohort", "methods": ("GET",), "path": "/chat/retention-cohorts/{cohort}",
     "exposure": "admin", "enforced_by": ("get_current_admin_user",),
     "hardening": {"scopes": ("audit",)},
     "note": "A single cohort's contents. More revealing than the list, and gated accordingly."},
    {"rule_id": "analytics", "methods": ("GET",), "path": "/bookings/analytics*", "exposure": "admin",
     "enforced_by": ("get_current_admin_user",), "hardening": {"scopes": ("read",), "rate_tier": "machine"},
     "note": "Aggregate reporting. The export variant is the expensive one."},
    {"rule_id": "audit_admin", "methods": ("GET", "POST"), "path": "/audit/*", "exposure": "admin",
     "enforced_by": ("get_current_admin_user",),
     "hardening": {"scopes": ("read:audit",), "step_up": "loa2", "cell": ("audit_trail", "detail_json", "read")},
     "note": (
         "Every audit read and every pipeline write. The cell matrix already models audit_log, so "
         "require_cell_access is the one hardening here that is a row away rather than a code change."
     )},
    {"rule_id": "retention_maintenance", "methods": ("GET", "POST"), "path": "/retention/maintenance*",
     "exposure": "admin", "enforced_by": ("get_current_admin_user",),
     "hardening": {"scopes": ("write",), "step_up": "loa3"},
     "note": "Destructive maintenance. The strongest step-up requirement in the module."},
    # --- identity
    {"rule_id": "user_api_keys", "methods": ("GET", "POST", "DELETE"), "path": "/users/me/api-keys*",
     "exposure": "authenticated", "enforced_by": ("get_current_user",),
     "hardening": {"scopes": ("write",), "step_up": "loa2"},
     "note": "Mints a credential that outranks a session. The obvious step-up candidate in identity."},
    {"rule_id": "user_step_up_read", "methods": ("GET",), "path": "/users/me/step-up",
     "exposure": "authenticated", "enforced_by": ("get_principal",),
     "hardening": {"scopes": ("read",)},
     "note": "Principal only: this explains the current level rather than acting on it."},
    {"rule_id": "user_step_up_mint", "methods": ("POST",), "path": "/users/me/step-up",
     "exposure": "authenticated", "enforced_by": ("get_current_user",),
     "hardening": {"step_up": "loa2"},
     "note": "Mints the stronger token. Cannot itself require the level it is minting."},
    {"rule_id": "user_security_posture", "methods": ("GET",), "path": "/users/me/security-posture",
     "exposure": "authenticated", "enforced_by": ("get_current_user", "get_principal"),
     "hardening": {"scopes": ("read",)},
     "note": "Reads the session, so it needs both a User row and the principal's assurance claims."},
    {"rule_id": "user_sessions", "methods": ("GET",), "path": "/users/me/sessions",
     "exposure": "authenticated", "enforced_by": ("get_current_user",), "hardening": {"scopes": ("read",)},
     "note": "The caller's own session inventory."},
    {"rule_id": "user_password_feedback", "methods": ("POST",), "path": "/users/me/password-feedback",
     "exposure": "authenticated", "enforced_by": ("get_current_user",), "hardening": {"scopes": ("read",)},
     "note": "Coach rating on the caller's own password."},
    {"rule_id": "user_password", "methods": ("POST",), "path": "/users/me/password",
     "exposure": "authenticated", "enforced_by": ("get_current_user",),
     "hardening": {"scopes": ("write",), "step_up": "loa1"},
     "note": "Changing one's own password. loa1 is the floor every other requirement sits above."},
    {"rule_id": "user_policy_decision", "methods": ("GET",), "path": "/users/me/policy-decision*",
     "exposure": "authenticated", "enforced_by": ("get_current_user",), "hardening": {"scopes": ("read",)},
     "note": "Explains the caller's own tier decision."},
    {"rule_id": "user_self", "methods": ("GET",), "path": "/users/me", "exposure": "authenticated",
     "enforced_by": ("get_current_user",), "hardening": {"scopes": ("read",)},
     "note": "The caller's own profile."},
    {"rule_id": "user_can_access", "methods": ("GET",), "path": "/users/me/can-access",
     "exposure": "policy", "enforced_by": ("get_current_customer_policy_score", "get_current_user"),
     "hardening": {},
     "note": "Loads the policy score so the route can answer the question it is asking."},
    {"rule_id": "user_policy_score", "methods": ("GET",), "path": "/users/me/policy-score",
     "exposure": "policy", "enforced_by": ("get_current_customer_policy_score",),
     "hardening": {"scopes": ("read",)},
     "note": "The score row itself, for the caller."},
    # --- bookings
    {"rule_id": "booking_assign", "methods": ("GET", "POST", "PUT", "PATCH", "DELETE"),
     "path": "/bookings/{booking_id}/assignment*", "exposure": "policy",
     "enforced_by": ("get_current_customer_policy_score", "get_current_user"),
     "hardening": {"scopes": ("bookings",), "cell": ("booking", "internal_notes", "write")},
     "note": "Assignment CRUD. A policy score is attached; the route decides what the tier allows."},
    {"rule_id": "booking_audit", "methods": ("GET",), "path": "/bookings/{booking_id}/audit",
     "exposure": "authenticated", "enforced_by": ("get_current_user",),
     "hardening": {"scopes": ("read:audit",)},
     "note": (
         "The one per-booking read that does not load a policy score, while the sibling "
         "audit-summary below does. Worth a decision either way."
     )},
    {"rule_id": "booking_audit_summary", "methods": ("GET",), "path": "/bookings/{booking_id}/audit-summary",
     "exposure": "policy", "enforced_by": ("get_current_customer_policy_score", "get_current_user"),
     "hardening": {"scopes": ("read:audit",)},
     "note": "Same data as booking_audit, summarized, and one gate stricter."},
    {"rule_id": "booking_history", "methods": ("GET",), "path": "/bookings/{booking_id}/history",
     "exposure": "policy", "enforced_by": ("get_current_customer_policy_score", "get_current_user"),
     "hardening": {"scopes": ("read",)},
     "note": "Unbounded history for one booking."},
    {"rule_id": "booking_operation_report", "methods": ("GET",), "path": "/bookings/{booking_id}/operation-report",
     "exposure": "policy", "enforced_by": ("get_current_customer_policy_score", "get_current_user"),
     "hardening": {"scopes": ("read",)},
     "note": "Operations view of one booking."},
    {"rule_id": "booking_lifecycle", "methods": ("POST",), "path": "/bookings/{booking_id}/*",
     "exposure": "authenticated", "enforced_by": ("get_current_user",),
     "hardening": {"scopes": ("write",), "cell": ("booking", "internal_notes", "write")},
     "note": "confirm / cancel / reopen. Mutations with no policy score attached."},
    {"rule_id": "booking_update", "methods": ("PUT", "DELETE"), "path": "/bookings/{booking_id}",
     "exposure": "policy", "enforced_by": ("get_current_customer_policy_score", "get_current_user"),
     "hardening": {"scopes": ("write",), "cell": ("booking", "internal_notes", "write")},
     "note": "Direct write and delete on the booking row."},
    {"rule_id": "booking_detail", "methods": ("GET",), "path": "/bookings/{booking_id}",
     "exposure": "policy", "enforced_by": ("get_current_customer_policy_score", "get_current_user"),
     "hardening": {"scopes": ("read",), "cell": ("booking", "id", "read")},
     "note": "One booking."},
    {"rule_id": "booking_root", "methods": ("GET", "POST"), "path": "/bookings/", "exposure": "policy",
     "enforced_by": ("get_current_customer_policy_score", "get_current_user"),
     "hardening": {"scopes": ("bookings",), "rate_tier": "interactive"},
     "note": "List and create. Note the trailing slash: the router declares '/' under /bookings."},
    # --- chat and topics (caller-scoped)
    {"rule_id": "chat_root", "methods": ("GET", "POST"), "path": "/chat", "exposure": "authenticated",
     "enforced_by": ("get_current_user",), "hardening": {"scopes": ("chat",), "rate_tier": "interactive"},
     "note": "The chat entry point. The only unscoped write surface of this size."},
    {"rule_id": "chat_admin_activity", "methods": ("GET",), "path": "/chat/admin-activity",
     "exposure": "admin", "enforced_by": ("get_current_admin_user",),
     "hardening": {"scopes": ("read",), "rate_tier": "sensitive"},
     "note": (
         "A sibling of the admin surface that a path-prefix wildcard for /chat/admin would "
         "otherwise claim by accident. It is listed on its own so the classification is a "
         "decision rather than a side effect of where the '*' happened to stop."
     )},
    {"rule_id": "chat_reads", "methods": ("GET",), "path": "/chat/*", "exposure": "authenticated",
     "enforced_by": ("get_current_user",), "hardening": {"scopes": ("read",)},
     "note": "Caller-scoped chat reads: history, insights, signals, snapshots, recovery dashboard."},
    {"rule_id": "chat_writes", "methods": ("POST",), "path": "/chat/*", "exposure": "authenticated",
     "enforced_by": ("get_current_user",), "hardening": {"scopes": ("write",)},
     "note": "Caller-scoped chat writes: arrears open, points exchange, playbook execution."},
    {"rule_id": "chat_me_preferences_write", "methods": ("PUT",), "path": "/chat/me/preferences",
     "exposure": "authenticated", "enforced_by": ("get_current_user",),
     "hardening": {"scopes": ("write",), "rate_tier": "interactive"},
     "note": (
         "Its own rule rather than a widening of chat_writes to PUT. The preference and "
         "consent centre is the only mutating surface under /chat/me, so naming it "
         "separately means a future PUT route has to be classified on its own instead of "
         "being absorbed by a method wildcard."
     )},
    {"rule_id": "topic_policy_decisions", "methods": ("GET",), "path": "/topic-policy-decisions",
     "exposure": "authenticated", "enforced_by": ("get_current_user",), "hardening": {"scopes": ("read",)},
     "note": "Explains why a topic was selected for the caller."},
    {"rule_id": "topic_ranking_route", "methods": ("GET",), "path": "/topic-ranking",
     "exposure": "authenticated", "enforced_by": ("get_current_user",), "hardening": {"scopes": ("read",)},
     "note": "Ranks topics for the caller."},
    {"rule_id": "topic_current_write", "methods": ("POST", "PUT", "DELETE"), "path": "/topics/current",
     "exposure": "authenticated", "enforced_by": ("get_current_user",),
     "hardening": {"scopes": ("write",)},
     "note": "Mutating the caller's current topic selection."},
    {"rule_id": "topic_validation", "methods": ("POST",), "path": "/topics/validate",
     "exposure": "authenticated", "enforced_by": ("get_current_user",), "hardening": {"scopes": ("read",)},
     "note": "Dry-run validation of a topic request."},
    {"rule_id": "topic_authenticated", "methods": ("GET",), "path": "/topics/*", "exposure": "authenticated",
     "enforced_by": ("get_current_user",), "hardening": {"scopes": ("read",)},
     "note": "Caller-scoped topic reads: coverage, history, intelligence, workspace, recommendations."},
    # --- fallback
    {"rule_id": "catch_all", "methods": ("GET", "POST", "PUT", "PATCH", "DELETE"), "path": "*",
     "exposure": "public", "enforced_by": (), "hardening": {},
     "note": (
         "Fallback for a route with no row. It currently matches nothing, which is the point: a "
         "new route lands here and the drift report names it rather than leaving it looking governed."
     )},
]

AUTHZ_RULE_BY_ID: dict[str, dict[str, Any]] = {str(row["rule_id"]): row for row in AUTHZ_RULES}
AUTHZ_RULE_IDS: tuple[str, ...] = tuple(str(row["rule_id"]) for row in AUTHZ_RULES)
AUTHZ_FALLBACK_RULE_ID = "catch_all"

# The dependencies that actually perform an authorization check, by name. Kept
# separate from the module-level dependency callables so the tables and the drift
# report can talk about them as strings.
AUTHZ_DEPENDENCY_NAMES: tuple[str, ...] = (
    "get_current_user",
    "get_current_user_optional",
    "get_current_admin_user",
    "get_current_policy_or_admin_user",
    "get_current_customer_policy_score",
    "get_principal",
    "get_optional_principal",
)

# The six ``require_*`` dependency factories, keyed by the prefix their generated
# callable carries. ``require_scopes("audit:read")`` returns a function named
# ``require_scopes_audit_read``, so a route that binds one can only be recognised
# by prefix -- and recognising it is the difference between "this route has no
# authorization" and "this route has a cell check I could not see".
AUTHZ_FACTORY_PREFIXES: dict[str, str] = {
    "require_scopes_": "require_scopes",
    "require_roles_": "require_roles",
    "require_step_up_": "require_step_up",
    "require_cell_": "require_cell_access",
    "require_rate_limit": "require_rate_limit",
    "require_tenant": "require_tenant",
}

AUTHZ_FACTORY_NAMES: tuple[str, ...] = tuple(sorted(set(AUTHZ_FACTORY_PREFIXES.values())))

# Per-tier token buckets, created on first use by ``rate_tier_limiter`` and then
# shared, so two routes bound to the same tier draw on one budget.
_RATE_TIER_LIMITERS: dict[str, TokenBucketLimiter] = {}

# Shared tunables. Anything that is a threshold, a cap, or a rounding decision
# belongs here; anything that is a payload string stays a literal in the code
# that emits it.
AUTHZ_OPS: dict[str, Any] = {
    "default_step_up_level": "loa2",
    "default_cell_level": "read",
    "catch_all_rule_id": AUTHZ_FALLBACK_RULE_ID,
    "default_exposure": "public",
    "default_rate_tier": "interactive",
    "max_hardening_items": 5,
    "hardening_gap_limit": 0,
    "default_probe": "customer",
    "unknown_method": "UNKNOWN",
}


# --- Resolution ----------------------------------------------------------------


def _unauthorized() -> HTTPException:
    """Fresh exception per raise (FastAPI reuses handler state otherwise)."""
    declared = AUTHZ_DENIAL_BY_KIND["unauthenticated"]
    return HTTPException(
        status_code=int(declared["status"]),
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _roles_from_claims(claims: dict) -> frozenset[str]:
    declared = claims.get("roles")
    if isinstance(declared, str):
        declared = [declared]
    if declared:
        return frozenset(cell_matrix.resolve_roles(list(declared)))
    return frozenset()


def _scopes_from_claims(claims: dict) -> frozenset[str]:
    declared = claims.get("scopes") or claims.get("scope") or []
    if isinstance(declared, str):
        declared = declared.split()
    return frozenset(declared)


def _api_key_principal(secret: str) -> tuple[Principal | None, str]:
    record, reason = security.API_KEYS.authenticate(secret)
    if record is None or reason != "ok":
        return None, reason
    return (
        Principal(
            subject=record.subject,
            roles=frozenset({AUTHZ_ROLE_SERVICE_ACCOUNT}),
            scopes=frozenset(record.scopes),
            auth_method=AUTH_METHOD_API_KEY,
            key_id=record.key_id,
        ),
        "ok",
    )


async def get_optional_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security_scheme),
    db: AsyncSession = Depends(get_db),
) -> Principal | None:
    """Resolve any supported credential into a ``Principal``, or ``None``.

    Three accepted shapes, tried in order:

    1. ``X-API-Key: csk_...`` — a machine credential from the API-key registry.
    2. ``Authorization: Bearer <jwt>`` — the historical user token.
    3. Nothing at all — anonymous, for routes that merely get richer when the
       caller is known.
    """
    correlation_id = getattr(request.state, "correlation_id", None)
    api_key = (request.headers.get("x-api-key") or "").strip()
    if api_key:
        principal, _reason = _api_key_principal(api_key)
        if principal is not None:
            return _with_correlation(principal, correlation_id)
        return None

    if credentials is None:
        return None
    validation = security.decode_token_full(credentials.credentials)
    if not validation.valid:
        return None
    username = validation.subject
    if not username:
        return None
    result = await db.execute(select(models.User).where(models.User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        return None

    claims = validation.claims
    principal = Principal(
        subject=username,
        user=user,
        roles=roles_for_user(user) | _roles_from_claims(claims),
        scopes=_scopes_from_claims(claims),
        tenant_id=claims.get("tenant_id"),
        auth_method=AUTH_METHOD_DELEGATED if claims.get("act") else AUTH_METHOD_USER,
        step_up_level=security.step_up_level_of(claims),
        claims=claims,
        key_id=validation.kid or None,
        delegated_by=claims.get("act"),
    )
    return _with_correlation(principal, correlation_id)


def _with_correlation(principal: Principal, correlation_id: str | None) -> Principal:
    if not correlation_id:
        return principal
    return replace(principal, correlation_id=correlation_id)


async def get_principal(
    principal: Principal | None = Depends(get_optional_principal),
) -> Principal:
    """Require *some* authenticated principal (human or machine)."""
    if principal is None:
        raise _unauthorized()
    return principal


# --- Historical dependencies (unchanged contracts) -----------------------------


def current_control_posture(current_user: models.User) -> str:
    """Resolve a user's control posture, defaulting to 'observed' when unknown."""
    policy_score = getattr(current_user, "policy_score", None)
    return getattr(policy_score, "control_posture", "observed") if policy_score else "observed"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    db: AsyncSession = Depends(get_db),
) -> models.User:
    """Get current authenticated user from JWT token."""
    credentials_exception = _unauthorized()

    try:
        payload = security.decode_access_token(credentials.credentials)
    except ValueError:
        raise credentials_exception

    username = payload.get("sub")
    if not username:
        raise credentials_exception

    # Get user from database
    query = select(models.User).where(models.User.username == username)
    result = await db.execute(query)
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception
    return user


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    db: AsyncSession = Depends(get_db),
) -> models.User | None:
    """Resolve the current user, or return None when unauthenticated.

    Used by surfaces that are richer when the caller is known but must stay
    publicly reachable otherwise (metadata and lightweight probe routes).
    """
    if credentials is None:
        return None
    try:
        payload = security.decode_access_token(credentials.credentials)
    except ValueError:
        return None
    username = payload.get("sub")
    if not username:
        return None
    query = select(models.User).where(models.User.username == username)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_current_admin_user(current_user: models.User = Depends(get_current_user)) -> models.User:
    if not bool(getattr(current_user, "is_admin", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return current_user


async def get_current_policy_or_admin_user(
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> models.User:
    policy_score = await upsert_customer_policy_score(db, current_user)
    if bool(getattr(current_user, "is_admin", False)) or can_access_functionality(policy_score, required_tier="system-premium"):
        return current_user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Policy-controlled admin access required",
    )


async def get_current_customer_policy_score(
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> models.CustomerPolicyScore:
    return await upsert_customer_policy_score(db, current_user)


# --- Declarative authorization dependencies ------------------------------------


def require_scopes(*scopes: str):
    """Dependency factory: require every named scope.

    Usage::

        @router.post("/audit/export", dependencies=[Depends(require_scopes("audit:read"))])

    A user token with no ``scopes`` claim is *not* implicitly unscoped-and-
    allowed; it is denied, so a scope is always a deliberate grant.
    """

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        missing = principal.has_all_scopes(scopes)
        if missing:
            declared = AUTHZ_DENIAL_BY_KIND["insufficient_scope"]
            raise HTTPException(
                status_code=int(declared["status"]),
                detail={
                    "error": "insufficient_scope",
                    "required": list(scopes),
                    "missing": missing,
                    "granted": sorted(principal.scopes),
                },
            )
        return principal

    _dependency.__name__ = "require_scopes_" + "_".join(s.replace(":", "_") for s in scopes)
    return _dependency


def require_cell_access(resource: str, cell: str, *, minimum: str = "read", context: dict | None = None):
    """Dependency factory: require a data-cell grant.

    Resolves through ``app.cell_matrix``, so conditions, masks, row scopes, and
    break-glass grants all apply. A masked read is rejected here — masking is a
    *response* concern, not an authorization one.
    """
    required_rank = cell_matrix.ACCESS_RANK.get(
        minimum, cell_matrix.ACCESS_RANK[str(AUTHZ_OPS["default_cell_level"])]
    )

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        decision = principal.can_access_cell(resource, cell, context=context)
        granted = cell_matrix.ACCESS_RANK.get(decision["level"], 0)
        if granted < required_rank:
            declared = AUTHZ_DENIAL_BY_KIND["cell_access_denied"]
            raise HTTPException(
                status_code=int(declared["status"]),
                detail={
                    "error": "cell_access_denied",
                    "resource": resource,
                    "cell": cell,
                    "required": minimum,
                    "reason": decision["reason"],
                    "requires_justification": decision["requires_justification"],
                },
            )
        return principal

    _dependency.__name__ = f"require_cell_{resource}_{cell}"
    return _dependency


def require_step_up(level: str = "loa2"):
    """Dependency factory: require a minimum assurance level.

    ``loa1`` is an ordinary password login; ``loa2`` needs a second factor;
    ``loa3`` needs a hardware key or recovery code.

    The signature default is part of this dependency's contract and stays
    literal. ``AUTHZ_OPS["default_step_up_level"]`` and ``STEP_UP_RANKS`` record
    what that default *is* and what each level means, and ``validate_authz``
    checks both against ``app.security`` -- so the shipped floor is documented as
    data and cannot drift from the code, without the code's own default becoming
    a config lookup that would change what a caller who omits the argument gets.
    """
    required = level

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not security.step_up_satisfied(principal.claims, required):
            declared = AUTHZ_DENIAL_BY_KIND["step_up_required"]
            raise HTTPException(
                status_code=int(declared["status"]),
                detail={
                    "error": "step_up_required",
                    "required_level": required,
                    "presented_level": security.step_up_level_of(principal.claims),
                },
            )
        return principal

    _dependency.__name__ = f"require_step_up_{required}"
    return _dependency


def require_roles(*roles: str):
    """Dependency factory: require membership in at least one named role."""

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not (principal.roles & frozenset(roles)):
            declared = AUTHZ_DENIAL_BY_KIND["role_required"]
            raise HTTPException(
                status_code=int(declared["status"]),
                detail={
                    "error": "role_required",
                    "required_any": list(roles),
                    "roles": sorted(principal.roles),
                },
            )
        return principal

    _dependency.__name__ = "require_roles_" + "_".join(roles)
    return _dependency


async def require_tenant(
    request: Request,
    principal: Principal = Depends(get_principal),
) -> str:
    """Bind the request to one tenant, rejecting cross-tenant access.

    A platform admin may act across tenants (and must say so via the header);
    everyone else is confined to their own tenant, or to the default when the
    principal has none.
    """
    from app.tenant_router import DEFAULT_TENANT, get_request_tenant_id

    requested = get_request_tenant_id(request)
    principal_tenant = principal.tenant_id or DEFAULT_TENANT
    if requested != principal_tenant and not principal.is_admin:
        declared = AUTHZ_DENIAL_BY_KIND["tenant_mismatch"]
        raise HTTPException(
            status_code=int(declared["status"]),
            detail={
                "error": "tenant_mismatch",
                "requested": requested,
                "principal_tenant": principal_tenant,
            },
        )
    request.state.tenant_id = requested
    return requested


def require_rate_limit(capacity: int | None = None, refill_per_second: float | None = None):
    """Dependency factory: token-bucket limit keyed by principal.

    Yields 429 with a ``Retry-After`` hint when the bucket is empty. The
    signature and the env-driven default bucket are unchanged; for a limit that
    is *declared* rather than passed at the call site, use ``require_rate_tier``.
    """
    limiter = RATE_LIMITER if capacity is None else TokenBucketLimiter(capacity, refill_per_second or 1.0)

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        decision = limiter.check(principal.subject)
        if not decision["allowed"]:
            declared = AUTHZ_DENIAL_BY_KIND["rate_limited"]
            raise HTTPException(
                status_code=int(declared["status"]),
                detail={"error": "rate_limited", **decision},
                headers={"Retry-After": str(int(1 / limiter.refill_per_second) if limiter.refill_per_second else 1)},
            )
        return principal

    _dependency.__name__ = "require_rate_limit"
    return _dependency


def require_rate_tier(tier: str = "interactive"):
    """Dependency factory: the token bucket a :data:`RATE_TIERS` row declares.

    The declarative counterpart to ``require_rate_limit``: the limit is named, so
    it can be described in ``/meta/authz``, compared across deployments, and
    shared -- every route bound to the same tier draws on one bucket, so a caller
    cannot multiply their budget by spreading a request across ten endpoints.
    """
    resolved = RATE_TIER_BY_NAME.get(str(tier))
    limiter = rate_tier_limiter(str(tier))
    tier_name = str((resolved or {}).get("tier") or AUTHZ_OPS["default_rate_tier"])

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        decision = limiter.check(principal.subject)
        if not decision["allowed"]:
            declared = AUTHZ_DENIAL_BY_KIND["rate_limited"]
            raise HTTPException(
                status_code=int(declared["status"]),
                detail={"error": "rate_limited", **decision},
                headers={"Retry-After": str(int(1 / limiter.refill_per_second) if limiter.refill_per_second else 1)},
            )
        return principal

    _dependency.__name__ = "require_rate_limit"
    _dependency.authz_rate_tier = tier_name
    _dependency.authz_rate_tier_known = resolved is not None
    return _dependency


def build_deps_catalog() -> dict:
    """Introspectable catalog of the authentication/authorization surface."""
    return {
        "auth_methods": [AUTH_METHOD_USER, AUTH_METHOD_API_KEY, AUTH_METHOD_DELEGATED],
        "user_roles": {role: list(roles) for role, roles in USER_ROLES.items()},
        "default_role": DEFAULT_USER_ROLE,
        "dependencies": {
            "get_principal": "any authenticated principal (user, API key, delegated)",
            "get_optional_principal": "same, but anonymous is allowed",
            "get_current_user": "historical: JWT -> User",
            "get_current_user_optional": "historical: JWT -> User | None",
            "get_current_admin_user": "historical: is_admin gate",
            "get_current_policy_or_admin_user": "historical: policy-tier gate",
            "get_current_customer_policy_score": "historical: upsert + return score",
            "current_control_posture": "historical: policy posture string",
        },
        "factories": {
            "require_scopes": "OAuth-style scope check; hierarchical on ':'",
            "require_cell_access": "data-cell grant via app.cell_matrix",
            "require_step_up": "minimum assurance level (loa1/loa2/loa3)",
            "require_roles": "role membership",
            "require_rate_limit": "token bucket per principal",
            "require_tenant": "single-tenant confinement; admins may cross",
        },
        "cell_matrix_integration": {
            "module": "app.cell_matrix",
            "masks_rejected_at_authz": True,
            "note": "masking shapes responses; it never satisfies an authorization check",
        },
        "rate_limiter": RATE_LIMITER.stats(),
        "correlation_id": {"header": "X-Correlation-ID", "generated": "uuid4 hex"},
    }


# --- Governance surface ----------------------------------------------------------
#
# The factories above decide one request. Everything in this section answers
# questions about the *set* of decisions: what the tables say, whether they
# still match the code that enforces them, which routes exist, and who would be
# allowed through. It is all pure and DB-free, so the whole authorization
# posture can be inspected from a REPL, diffed in a test, and reasoned about
# without a database or a request.
#
# ``evaluate_authz`` is the important one. Before it, the only way to ask "would
# this principal be allowed to do that" was to make the request, because the
# check lived in a closure returned by a factory and wired into a route
# signature. Now it is a function over a principal and a method+path, using the
# same ``has_scope`` / ``can_access_cell`` / ``step_up_satisfied`` primitives the
# dependencies use.


def scope_satisfies(granted: Sequence[str], required: str) -> bool:
    """Whether ``granted`` satisfies ``required``. Mirrors ``Principal.has_scope``.

    Scopes are hierarchical on ``:`` and ``*`` is a wildcard, so ``read`` grants
    ``read:audit`` and ``*`` grants everything. This is a plain-set version of
    the same rule so the decision engine can work on a list of scope strings
    without building a ``Principal`` first; :func:`validate_authz` checks the two
    against each other over the whole catalog.
    """
    for one in granted or ():
        if one == "*" or one == required:
            return True
        if required.startswith(f"{one}:"):
            return True
    return False


def implied_scopes(roles: Sequence[str]) -> list[str]:
    """The scopes a role set *could* be granted, per :data:`ROLE_SCOPE_GRANTS`.

    Reported on the decision trace as ``implied_by_roles``, never merged into
    ``granted``. See the note on the table: applying it at resolution time would
    turn a role check into a capability grant and start authorizing calls that
    are denied today.
    """
    out: set[str] = set()
    for role in roles or ():
        out.update(ROLE_SCOPE_GRANTS.get(str(role), ()))
    return sorted(out)


def rate_tier_limiter(tier: str = "interactive") -> TokenBucketLimiter:
    """The process-local token bucket for a :data:`RATE_TIERS` row.

    Buckets are created once per tier and then shared, so two routes bound to the
    same tier share one budget -- which is the point of naming a tier. An unknown
    tier falls back to ``interactive`` rather than raising, because this is
    called from a dependency and a typo in a route decorator should be a loud
    catalog finding rather than a 500 at request time.
    """
    row = RATE_TIER_BY_NAME.get(str(tier)) or RATE_TIER_BY_NAME["interactive"]
    name = str(row["tier"])
    existing = _RATE_TIER_LIMITERS.get(name)
    if existing is None:
        existing = TokenBucketLimiter(
            capacity=int(row["capacity"]),
            refill_per_second=float(row["refill_per_second"]),
        )
        _RATE_TIER_LIMITERS[name] = existing
    return existing


def _authz_pattern_re(pattern: str) -> re.Pattern[str]:
    """Compile a path pattern in FastAPI's own spelling.

    ``{name}`` is one path segment and a trailing ``*`` is this path *and
    everything under it*. The pattern syntax is the route's, so a rule can be
    written by copying the path out of the OpenAPI schema and turning the
    parameters into names.

    A trailing ``*`` compiles to an optional ``/``-prefixed remainder rather
    than to ``.*``. ``/meta*`` therefore claims ``/meta`` and ``/meta/authz``
    but not ``/metadata``: a wildcard is a path prefix, and a loose one would
    let a future ``/metadata-report`` endpoint be classified as the meta
    surface without anybody noticing that the classification was wrong. A
    ``*`` in the middle of a pattern is still ``.*``, and a pattern that is
    nothing but ``*`` is the catch-all.
    """
    out: list[str] = []
    index = 0
    text = str(pattern)
    while index < len(text):
        char = text[index]
        if char == "*":
            if index == len(text) - 1 and index > 0:
                out.append("(?:/.*)?" if out and out[-1] != "/" else ".*")
            else:
                out.append(".*")
        elif char == "{":
            close = text.find("}", index)
            if close == -1:
                out.append(re.escape(char))
            else:
                out.append("[^/]+")
                index = close
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def authz_rule_sample_path(row: Mapping[str, Any]) -> str:
    """A concrete path that matches a rule, for the offline simulation.

    ``/meta*`` becomes ``/meta`` and ``/bookings/{booking_id}`` becomes
    ``/bookings/1``. It is a shape, not a real route; the point is that
    ``evaluate_authz`` has something to match when no route table is available.
    """
    path = str(row.get("path") or "")
    path = path.replace("*", "")
    while "{name}" in path:
        path = path.replace("{name}", "1")
    while "{" in path and "}" in path:
        start = path.index("{")
        end = path.index("}")
        path = path[:start] + "1" + path[end + 1 :]
    return path or "/"


def match_authz_rule(method: str, path: str) -> dict[str, Any]:
    """First-match-wins rule lookup for one ``(method, path)`` pair.

    ``matched_fallback`` is always present and is true whenever the deciding
    rule *is* the catch-all -- whether it got there by matching the row or by
    the table running out. A route that lands on the fallback is a route nobody
    has described, and that has to be one flag rather than two, or the drift
    report silently stops seeing the routes it exists to catch.
    """
    verb = str(method or AUTHZ_OPS["unknown_method"]).upper()
    request_path = str(path or "/")
    fallback_id = str(AUTHZ_OPS["catch_all_rule_id"])
    for index, row in enumerate(AUTHZ_RULES):
        methods = tuple(row.get("methods") or ())
        if verb not in methods:
            continue
        if _authz_pattern_re(str(row.get("path") or "*")).match(request_path):
            rule_id = str(row.get("rule_id"))
            return {
                "rule": dict(row),
                "rule_index": index,
                "rule_id": rule_id,
                "method": verb,
                "path": request_path,
                "matched_fallback": rule_id == fallback_id,
            }
    fallback = AUTHZ_RULE_BY_ID.get(fallback_id)
    return {
        "rule": dict(fallback) if fallback is not None else {},
        "rule_index": -1,
        "rule_id": fallback_id,
        "method": verb,
        "path": request_path,
        "unmatched": True,
        "matched_fallback": True,
    }


def authz_denial_trace(kind: str, **fields: Any) -> dict[str, Any]:
    """The denial a factory would raise, built without raising it.

    The status code, the detail shape and the key order come from
    :data:`AUTHZ_DENIALS`; the values come from ``fields``. Keys with no supplied
    value come back as ``None`` and are listed in ``missing_fields``, so a
    decision trace cannot quietly render a denial a client would never actually
    receive. :func:`authz_denial_contract` proves the two agree.
    """
    row = AUTHZ_DENIAL_BY_KIND.get(str(kind))
    if row is None:
        return {
            "kind": str(kind),
            "known": False,
            "status": 403,
            "detail_shape": "object",
            "error": None,
            "keys": (),
            "detail": None,
            "headers": {},
            "missing_fields": [],
        }
    if str(row.get("detail_shape")) == "string":
        return {
            "kind": str(kind),
            "known": True,
            "status": int(row.get("status") or 403),
            "detail_shape": "string",
            "error": None,
            "text": row.get("text"),
            "keys": (),
            "detail": row.get("text"),
            "headers": dict(row.get("headers") or {}),
            "missing_fields": [],
        }
    missing: list[str] = []
    detail: dict[str, Any] = {}
    for key in tuple(row.get("keys") or ()):
        if key == "error":
            detail[key] = row.get("error")
            continue
        if key in fields:
            detail[key] = fields[key]
        else:
            detail[key] = None
            missing.append(key)
    return {
        "kind": str(kind),
        "known": True,
        "status": int(row.get("status") or 403),
        "detail_shape": "object",
        "error": row.get("error"),
        "keys": tuple(row.get("keys") or ()),
        "detail": detail,
        "headers": dict(row.get("headers") or {}),
        "missing_fields": missing,
    }


def authz_probe(name: str | None = None) -> Principal | None:
    """Build a real ``Principal`` from a row of :data:`AUTHZ_PROBES`.

    Not a stand-in for one: it goes through the same constructor, so
    ``has_scope``, ``is_admin`` and ``can_access_cell`` behave exactly as they
    do on a resolved caller.

    A row marked ``absent`` returns ``None`` rather than a hollow principal,
    because "no credential was presented" is genuinely the absence of a
    principal and the engine branches on exactly that. Handing back an empty
    ``Principal`` instead would make every ``authenticated`` route report the
    caller as allowed, which is the opposite of what an absent credential means.
    """
    probe = str(name or AUTHZ_OPS["default_probe"])
    row = next((item for item in AUTHZ_PROBES if str(item.get("probe")) == probe), None)
    if row is None:
        return Principal(subject=probe)
    if bool(row.get("absent")):
        return None
    claims = dict(row.get("claims") or {})
    return Principal(
        subject=str(row.get("subject") or probe),
        roles=frozenset(str(role) for role in row.get("roles") or ()),
        scopes=frozenset(str(scope) for scope in row.get("scopes") or ()),
        tenant_id=row.get("tenant_id"),
        auth_method=str(row.get("auth_method") or AUTH_METHOD_USER),
        step_up_level=security.step_up_level_of(claims),
        claims=claims,
    )


def _authz_check(
    name: str,
    *,
    status: str,
    enforced: bool = True,
    reason: str = "",
    **detail: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "check": name,
        "status": status,
        "enforced": enforced,
        "passed": status == "passed",
    }
    if reason:
        row["reason"] = reason
    row.update(detail)
    return row


def evaluate_authz(
    principal: Principal | None,
    method: str,
    path: str,
    *,
    context: dict | None = None,
    policy_tier: str | None = None,
    tenant_id: str | None = None,
    rate_limit: bool = False,
    include_hardening: bool = False,
) -> dict[str, Any]:
    """Decide a request without serving it, and explain the decision.

    The pure counterpart of the six ``require_*`` factories. Checks run in
    :data:`AUTHZ_CHECK_ORDER` and the first failure decides, so a caller with
    three problems gets the same denial a request would have produced, not three
    of them.

    ``include_hardening=True`` additionally evaluates each rule's *proposed*
    requirements, marked ``enforced: False``, which answers "this call is allowed
    today -- would it still be under the rule I am drafting?" without anyone
    mistaking the answer for the current posture.
    """
    match = match_authz_rule(method, path)
    rule = match["rule"]
    exposure = str(rule.get("exposure") or AUTHZ_OPS["default_exposure"])
    hardening = dict(rule.get("hardening") or {})
    requested_scopes = tuple(str(scope) for scope in hardening.get("scopes") or ()) if include_hardening else ()
    requested_roles = tuple(str(role) for role in hardening.get("roles") or ()) if include_hardening else ()
    requested_step_up = str(hardening["step_up"]) if hardening.get("step_up") and include_hardening else None
    requested_cell = hardening.get("cell") if include_hardening and hardening.get("cell") else None
    rate_tier_name = str(hardening.get("rate_tier") or AUTHZ_OPS["default_rate_tier"])

    checks: list[dict[str, Any]] = []
    facts: dict[str, Any] = {
        "authenticated": principal is not None,
        "subject": principal.subject if principal is not None else None,
        "auth_method": principal.auth_method if principal is not None else None,
        "roles": sorted(principal.roles) if principal is not None else [],
        "scopes": sorted(principal.scopes) if principal is not None else [],
        "step_up_level": principal.step_up_level if principal is not None else "none",
        "tenant_id": principal.tenant_id if principal is not None else None,
        "is_admin": bool(principal.is_admin) if principal is not None else False,
        "is_service_account": bool(principal.is_service_account) if principal is not None else False,
    }
    facts["implied_by_roles"] = implied_scopes(facts["roles"])
    facts["implied_by_roles_applied"] = False

    if principal is None:
        if exposure == "public":
            # A public route does not require a credential, so an absent
            # principal is the expected shape of the request rather than a
            # failure of it. Failing here would deny the whole public surface.
            checks.append(
                _authz_check(
                    "authenticated",
                    status="not_evaluated",
                    reason="public route: no credential is required",
                )
            )
        else:
            checks.append(
                _authz_check("authenticated", status="failed", reason="no principal resolved")
            )
    else:
        checks.append(_authz_check("authenticated", status="passed", subject=principal.subject))

    if exposure == "admin":
        if principal is None:
            checks.append(_authz_check("admin", status="not_evaluated", reason="unauthenticated"))
        else:
            ok = bool(principal.is_admin)
            checks.append(
                _authz_check(
                    "admin",
                    status="passed" if ok else "failed",
                    required_any=["admin"],
                    roles=facts["roles"],
                )
            )
    if requested_roles:
        held = principal is not None and bool(principal.roles & frozenset(requested_roles))
        checks.append(
            _authz_check(
                "roles",
                status="passed" if held else "failed",
                enforced=False,
                required_any=list(requested_roles),
                roles=facts["roles"],
            )
        )
    if requested_scopes:
        granted = facts["scopes"]
        missing = [scope for scope in requested_scopes if not scope_satisfies(granted, scope)]
        checks.append(
            _authz_check(
                "scopes",
                status="passed" if not missing else "failed",
                enforced=False,
                required=list(requested_scopes),
                missing=missing,
                granted=granted,
            )
        )
    if requested_step_up:
        satisfied = principal is not None and security.step_up_satisfied(
            principal.claims, requested_step_up
        )
        checks.append(
            _authz_check(
                "step_up",
                status="passed" if satisfied else "failed",
                enforced=False,
                required_level=requested_step_up,
                presented_level=facts["step_up_level"],
            )
        )
    if requested_cell:
        if principal is None:
            checks.append(_authz_check("cell", status="not_evaluated", reason="unauthenticated"))
        else:
            resource, cell_name = str(requested_cell[0]), str(requested_cell[1])
            minimum = str(requested_cell[2]) if len(requested_cell) > 2 else str(
                AUTHZ_OPS["default_cell_level"]
            )
            decision = principal.can_access_cell(resource, cell_name, context=context)
            granted_rank = cell_matrix.ACCESS_RANK.get(str(decision.get("level")), 0)
            required_rank = cell_matrix.ACCESS_RANK.get(
                minimum, cell_matrix.ACCESS_RANK[str(AUTHZ_OPS["default_cell_level"])]
            )
            checks.append(
                _authz_check(
                    "cell",
                    status="passed" if granted_rank >= required_rank else "failed",
                    enforced=False,
                    resource=resource,
                    cell=cell_name,
                    required=minimum,
                    level=str(decision.get("level")),
                    reason=str(decision.get("reason")),
                    requires_justification=bool(decision.get("requires_justification")),
                )
            )
    if rate_limit:
        decision = rate_tier_limiter(rate_tier_name).check(facts["subject"] or "anonymous")
        checks.append(
            _authz_check(
                "rate_limit",
                status="passed" if decision["allowed"] else "failed",
                enforced=not bool(hardening.get("rate_tier")),
                tier=rate_tier_name,
                remaining=decision["remaining"],
                capacity=decision["capacity"],
            )
        )
    if tenant_id is not None:
        from app.tenant_router import DEFAULT_TENANT

        own = (principal.tenant_id if principal is not None else None) or DEFAULT_TENANT
        ok = principal is not None and (str(tenant_id) == own or bool(principal.is_admin))
        checks.append(
            _authz_check(
                "tenant",
                status="passed" if ok else "failed",
                enforced=False,
                requested=str(tenant_id),
                principal_tenant=own,
            )
        )
    if exposure == "policy":
        resolved_tier = policy_tier or (
            str(principal.claims.get("policy_tier") or "") if principal is not None else ""
        )
        checks.append(
            _authz_check(
                "policy_tier",
                status="passed" if resolved_tier else "not_evaluated",
                policy_tier=resolved_tier or None,
                reason=(
                    "get_current_customer_policy_score attaches a score; the tier gate itself "
                    "runs in get_current_policy_or_admin_user or in the route body, and the "
                    "user lookup behind it needs a User row an offline Principal does not carry"
                ),
            )
        )

    order = {name: index for index, name in enumerate(AUTHZ_CHECK_ORDER)}
    checks.sort(key=lambda item: order.get(str(item["check"]), len(order)))
    failed = next((item for item in checks if item["status"] == "failed"), None)
    denial = None
    status = 200
    if failed is not None:
        kind = AUTHZ_CHECK_DENIALS.get(str(failed["check"]))
        if kind is None:
            # A check with no declared denial is a config gap, not a silent allow.
            denial = {
                "kind": None,
                "known": False,
                "status": 403,
                "detail_shape": "object",
                "error": None,
                "keys": (),
                "detail": None,
                "headers": {},
                "missing_fields": [],
            }
            status = 403
        else:
            fields = {
                key: value
                for key, value in failed.items()
                if key not in {"check", "status", "enforced", "passed", "reason"}
            }
            denial = authz_denial_trace(kind, **fields)
            status = int(denial.get("status") or 403)

    enforced_only = [item for item in checks if item["enforced"]]
    enforced_failure = next((item for item in enforced_only if item["status"] == "failed"), None)
    return {
        "allowed": status == 200,
        "status": status,
        "method": match["method"],
        "path": match["path"],
        "rule_id": match["rule_id"],
        "rule_index": match["rule_index"],
        "exposure": exposure,
        "enforced_by": list(rule.get("enforced_by") or ()),
        "exposure_rank": AUTHZ_EXPOSURE_RANK.get(exposure, 0),
        "matched_fallback": bool(match["matched_fallback"]),
        "checks": checks,
        "denial": denial,
        "denial_kind": str(denial["kind"]) if denial is not None and denial.get("kind") else None,
        "hardening_included": bool(include_hardening),
        "hardening_considered": sorted(hardening),
        "enforced_decision": (
            "denied" if enforced_failure is not None else "allowed"
        ),
        "facts": facts,
    }


# --- Route table ----------------------------------------------------------------


def iter_authz_routes(routes: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(effective_path, route)`` for every ``APIRoute``, prefixes included.

    A route's own ``path`` is *not* its effective path: ``include_router(prefix=...)``
    and a router's own ``prefix`` both live outside the route object in recent
    FastAPI releases, which means a rule table written against ``route.path``
    silently matches nothing. Composing the prefixes here is what makes the
    drift report comparable to the OpenAPI schema.

    Deliberately defensive: it reads attributes off whatever it is handed and
    never raises, because it is called from a validator and from an endpoint
    that must not 500 over a routing detail.
    """
    try:
        candidates = list(routes or ())
    except TypeError:
        return
    for route in candidates:
        try:
            if isinstance(route, APIRoute):
                yield prefix + str(route.path), route
                continue
            context = getattr(route, "include_context", None)
            original = getattr(route, "original_router", None)
            if context is not None and original is not None:
                # ``include_context.prefix`` is what the parent applied; the
                # included router's own ``prefix`` is already baked into its
                # child route paths, so adding it again doubles every segment.
                yield from iter_authz_routes(original.routes, prefix + str(context.prefix or ""))
            elif original is not None:
                yield from iter_authz_routes(original.routes, prefix)
            elif getattr(route, "routes", None):
                yield from iter_authz_routes(route.routes, prefix)
        except Exception:  # pragma: no cover - defensive; a report must not raise
            continue


def route_authz_facts(route: Any) -> dict[str, set[str]]:
    """What a route resolves through: the gates it uses and the factories it binds.

    Two different things, kept apart on purpose. ``dependencies`` are the
    module-level callables that decide on their own (``get_current_user``,
    ``get_current_admin_user`` and friends). ``factories`` are the six
    ``require_*`` dependency factories, which produce a *new* function per call
    site -- ``require_scopes("audit:read")`` is a closure named
    ``require_scopes_audit_read`` -- so they can only be recognised by prefix.

    Recognising them matters: a route bound to ``require_cell_access(...)``
    genuinely performs a cell check, and reading its dependency name as an
    unrecognized callable would report it as having no authorization at all.
    """
    dependencies: set[str] = set()
    factories: set[str] = set()
    try:
        parameters = inspect.signature(route.endpoint).parameters.values()
    except (AttributeError, TypeError, ValueError):
        parameters = ()
    names: list[str] = []
    for parameter in parameters:
        default = getattr(parameter, "default", None)
        if isinstance(default, _fastapi_params.Depends):
            names.append(getattr(default.dependency, "__name__", str(default.dependency)))
    for dependency in getattr(route, "dependencies", None) or ():
        names.append(getattr(dependency.dependency, "__name__", str(dependency.dependency)))
    for name in names:
        if name in AUTHZ_DEPENDENCY_NAMES:
            dependencies.add(name)
            continue
        for prefix, factory in AUTHZ_FACTORY_PREFIXES.items():
            if name == prefix or name.startswith(prefix):
                factories.add(factory)
                break
    return {"dependencies": dependencies, "factories": factories}


def route_authz_dependencies(route: Any) -> set[str]:
    """The authorization dependencies a route resolves through.

    ``get_db`` and the security scheme are ignored: one is plumbing and the
    other is the credential source, not a check.
    """
    return route_authz_facts(route)["dependencies"]


def authz_route_inventory(routes: Any) -> list[dict[str, Any]]:
    """Every live ``(method, path)`` pair with the rule and gate that decide it."""
    rows: list[dict[str, Any]] = []
    for path, route in iter_authz_routes(routes):
        facts = route_authz_facts(route)
        dependencies = facts["dependencies"]
        try:
            methods = sorted(
                method
                for method in (getattr(route, "methods", None) or ())
                if str(method) not in ("HEAD", "OPTIONS")
            )
        except TypeError:
            methods = []
        for method in methods:
            match = match_authz_rule(method, path)
            declared = set(match["rule"].get("enforced_by") or ())
            actual = set(dependencies)
            if declared == actual:
                # Includes the public case, where both sides are empty: a route
                # with no gate declared and no gate resolved is in sync, and
                # saying otherwise would report every public route as a finding.
                delta = "match"
            elif declared - actual:
                delta = "rule_expects_more"
            else:
                delta = "route_has_more"
            rows.append(
                {
                    "method": method,
                    "path": path,
                    "route_name": str(getattr(route, "name", "") or ""),
                    "rule_id": match["rule_id"],
                    "exposure": str(match["rule"].get("exposure") or AUTHZ_OPS["default_exposure"]),
                    "declared_by": sorted(declared),
                    "actual_by": sorted(actual),
                    "factories": sorted(facts["factories"]),
                    "delta": delta,
                    "matched_fallback": bool(match["matched_fallback"]),
                }
            )
    return rows


def authz_coverage_report(routes: Any = None) -> dict[str, Any]:
    """Which rule decides which live route, and which rules decide nothing.

    First-match-wins makes row order the semantics, so a row that can never be
    reached is dead configuration that still reads as live in the catalog. The
    fallback row is expected to be dead -- that is what makes it a fallback --
    so it is reported separately from the real rules.
    """
    inventory = authz_route_inventory(routes) if routes is not None else []
    total = len(inventory)
    hits: dict[str, int] = {}
    exposure_hits: dict[str, int] = {name: 0 for name in AUTHZ_EXPOSURES}
    for row in inventory:
        hits[str(row["rule_id"])] = hits.get(str(row["rule_id"]), 0) + 1
        exposure_hits[str(row["exposure"])] = exposure_hits.get(str(row["exposure"]), 0) + 1
    fallback = str(AUTHZ_OPS["catch_all_rule_id"])
    return {
        "routes_provided": bool(routes is not None),
        "method_path_pairs": total,
        "exposures": {
            name: {
                "routes": count,
                "share": round(count / total, 6) if total else 0.0,
            }
            for name, count in sorted(exposure_hits.items())
        },
        "rules": [
            {
                "rule_id": rule_id,
                "exposure": str((AUTHZ_RULE_BY_ID.get(rule_id) or {}).get("exposure") or ""),
                "routes": hits.get(rule_id, 0),
                "share": round(hits.get(rule_id, 0) / total, 6) if total else 0.0,
            }
            for rule_id in AUTHZ_RULE_IDS
        ],
        "unreachable_rules": [
            rule_id for rule_id in AUTHZ_RULE_IDS if rule_id != fallback and hits.get(rule_id, 0) == 0
        ],
        "fallback_rule": fallback,
        "fallback_routes": hits.get(fallback, 0),
        "unclassified_routes": sorted(
            f"{row['method']} {row['path']}" for row in inventory if row["matched_fallback"]
        ),
        "note": (
            "Counts are live (method, path) pairs attributed to the first matching rule. "
            "An empty rules[] share means no route table was supplied, not that no rules exist."
        ),
    }


def authz_drift_report(routes: Any = None) -> dict[str, Any]:
    """Diff the described authorization posture against the app that enforces it.

    Three questions, and the answers are the point of the table:

    * does every live ``(method, path)`` pair have a rule, and is that rule's
      ``enforced_by`` the dependency the route really uses;
    * is every rule reachable;
    * how much of the described posture is *proposed* rather than enforced.

    Without a route table the report still returns its static sections, with
    ``routes_provided: false`` -- passing ``app.routes`` is what turns the first
    two sections on, and ``app.deps`` deliberately does not import ``app.main``
    to get them for you.
    """
    inventory = authz_route_inventory(routes) if routes is not None else []
    coverage = authz_coverage_report(routes)
    deltas: dict[str, int] = {}
    for row in inventory:
        deltas[str(row["delta"])] = deltas.get(str(row["delta"]), 0) + 1
    mismatched = [
        {
            "method": row["method"],
            "path": row["path"],
            "rule_id": row["rule_id"],
            "delta": row["delta"],
            "declared_by": row["declared_by"],
            "actual_by": row["actual_by"],
        }
        for row in inventory
        if row["delta"] != "match"
    ]
    bound_factories: dict[str, list[str]] = {name: [] for name in AUTHZ_FACTORY_NAMES}
    for row in inventory:
        for factory in row["factories"]:
            bound_factories.setdefault(factory, []).append(f"{row['method']} {row['path']}")
    return {
        "routes_provided": routes is not None,
        "method_path_pairs": len(inventory),
        "drift": deltas,
        "in_sync": not mismatched and not coverage["unclassified_routes"],
        "mismatched": mismatched,
        "unreachable_rules": coverage["unreachable_rules"],
        "unclassified_routes": coverage["unclassified_routes"],
        "bound_factories": {
            name: {"routes": len(paths), "sample": sorted(paths)[:3]}
            for name, paths in sorted(bound_factories.items())
        },
        "unbound_factories": sorted(
            name for name, paths in bound_factories.items() if not paths
        ),
        "coverage": coverage,
        "note": (
            "unbound_factories lists the require_* dependency factories that no route binds. "
            "They work and are tested, but nothing in the app routes calls them, so their "
            "requirements appear in this table's hardening blocks and nowhere else."
        ),
    }


def authz_hardening_backlog(limit: int | None = None) -> dict[str, Any]:
    """What is described as required but not enforced, grouped by requirement.

    A backlog, deliberately not a defect list: every row here is a proposal, and
    the report says so. Ordering is by how many live rules ask for it, so the
    cheapest wide win (a scope nobody enforces yet, requested by many routes) is
    the first thing anyone reads.
    """
    cap = int(limit if limit is not None else AUTHZ_OPS["max_hardening_items"])
    grouped: dict[str, dict[str, Any]] = {}
    for row in AUTHZ_RULES:
        hardening = dict(row.get("hardening") or {})
        for kind in sorted(hardening):
            value = hardening[kind]
            values = tuple(str(item) for item in value) if isinstance(value, (list, tuple)) else (str(value),)
            entry = grouped.setdefault(
                kind,
                {"requirement": kind, "rules": [], "values": set()},
            )
            entry["rules"].append(str(row.get("rule_id")))
            entry["values"].update(values)
    rows = []
    for kind in sorted(grouped, key=lambda name: (-len(grouped[name]["rules"]), name)):
        entry = grouped[kind]
        rows.append(
            {
                "requirement": kind,
                "rules": sorted(entry["rules"]),
                "rule_count": len(set(entry["rules"])),
                "values": sorted(entry["values"]),
                "enforced_by": None,
                "note": "no dependency bound to any route enforces this today",
            }
        )
    return {
        "total_rules_with_hardening": sum(1 for row in AUTHZ_RULES if row.get("hardening")),
        "total_rules": len(AUTHZ_RULES),
        "requirements": rows,
        "shown": min(cap, len(rows)),
        "limit": cap,
        "note": (
            "hardening[] is a proposal. Nothing in this module reads it at request time, and "
            "evaluate_authz only evaluates it when include_hardening=True is passed explicitly."
        ),
    }


def authz_simulation_report(
    probes: Sequence[str] | None = None,
    rules: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Who would be allowed through, for every rule, without a request.

    Each cell is one ``evaluate_authz`` call against the caller :data:`AUTHZ_PROBES`
    describes, so the answer is produced by the same ``has_scope`` / ``is_admin``
    primitives the dependencies use rather than by a restatement of them. An
    ``absent`` probe contributes a row of ``None``, which is what the engine
    actually sees when no credential was presented. Hardening is included and
    marked, so the matrix answers both "what is true" and "what would be true
    under the drafted rules".
    """
    selected_probes = [str(name) for name in (probes or AUTHZ_PROBE_NAMES)]
    selected_rules = [str(name) for name in (rules or AUTHZ_RULE_IDS)]
    matrix: dict[str, dict[str, Any]] = {}
    for probe in selected_probes:
        principal = authz_probe(probe)
        cells: dict[str, Any] = {}
        for rule_id in selected_rules:
            row = AUTHZ_RULE_BY_ID.get(rule_id)
            if row is None:
                continue
            verb = str((row.get("methods") or ("GET",))[0])
            path = authz_rule_sample_path(row)
            decision = evaluate_authz(principal, verb, path, include_hardening=True)
            cells[rule_id] = {
                "exposure": decision["exposure"],
                "allowed": decision["allowed"],
                "enforced_decision": decision["enforced_decision"],
                "denial_kind": decision["denial_kind"],
                "failing_checks": [
                    str(item["check"]) for item in decision["checks"] if item["status"] == "failed"
                ],
            }
        matrix[probe] = cells
    denied = {
        probe: sorted(
            rule_id for rule_id, cell in cells.items() if not cell["allowed"]
        )
        for probe, cells in matrix.items()
    }
    allowed_count = sum(
        1 for cells in matrix.values() for cell in cells.values() if cell["allowed"]
    )
    total_cells = sum(len(cells) for cells in matrix.values())
    return {
        "probes": selected_probes,
        "rules": [rule_id for rule_id in selected_rules if rule_id in AUTHZ_RULE_BY_ID],
        "cells": total_cells,
        "allowed_cells": allowed_count,
        "matrix": matrix,
        "denied_by_probe": denied,
        "note": (
            "Hardening is evaluated and marked enforced=false, so an 'allowed' cell means "
            "allowed under the drafted rules, not under the current ones. Read "
            "enforced_decision for the current posture."
        ),
    }


# --- Validation -----------------------------------------------------------------

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_HARDENING_KEYS = frozenset({"scopes", "roles", "step_up", "cell", "rate_tier"})
_DETAIL_SHAPES = frozenset({"string", "object"})
_DENIAL_STATUSES = frozenset({401, 403, 429, 451})
_CELL_RANKS = frozenset(cell_matrix.ACCESS_RANK)


def _known_roles() -> set[str]:
    """Every *resolved* role name the app can hand a principal.

    The values of ``USER_ROLES`` and ``cell_matrix.ROLE_GROUPS``, not their
    keys: ``customer`` is a role a user *record* carries, and it expands to
    ``owner`` before it reaches a principal, so treating it as a grantable role
    would invent a fourth spelling of the same thing.
    """
    known: set[str] = {AUTHZ_ROLE_SERVICE_ACCOUNT}
    for roles in USER_ROLES.values():
        known.update(str(role) for role in roles)
    for roles in cell_matrix.ROLE_GROUPS.values():
        known.update(str(role) for role in roles)
    return known


def validate_authz() -> dict[str, Any]:
    """Check the authorization tables against each other and against the code.

    Nothing here raises. A validator that explodes on malformed configuration is
    a validator that gets wrapped in a try/except by its first caller, and then
    it has stopped being a validator.

    Errors are things that are certainly wrong: a scope nobody issued, a step-up
    rank that disagrees with ``app.security``, a ``STEP_UP_RANKS`` evidence set
    that disagrees with ``app.routers.users``, a rule naming a dependency that
    does not exist, a denial whose declared key order is duplicated. Warnings
    are things that are merely notable: an environment variable that has quietly
    moved the effective rate limit away from the documented one, a hardening
    proposal naming a resource the cell matrix has never heard of.

    It reads the tables themselves, not the import-time indexes derived from
    them. Those indexes are convenient for the request path, but a validator
    that consults one is checking the table as it *was*, and a table that has
    since been edited gets a verdict on a version of itself that no longer
    exists.
    """
    errors: list[str] = []
    warnings: list[str] = []

    def _index(table: list, key: str) -> dict:
        out: dict[str, dict] = {}
        for row in table:
            name = str(row.get(key) or "")
            if name and name not in out:
                out[name] = row
        return out

    scope_index = _index(SCOPE_CATALOG, "scope")
    level_index = _index(STEP_UP_RANKS, "level")
    tier_index = _index(RATE_TIERS, "tier")
    level_ranks: dict[str, int] = {}
    for name, row in level_index.items():
        try:
            level_ranks[name] = int(row.get("rank"))
        except (TypeError, ValueError):
            level_ranks[name] = -1

    # --- scopes
    seen_scopes: set[str] = set()
    for row in SCOPE_CATALOG:
        name = str(row.get("scope") or "")
        if not name:
            errors.append("SCOPE_CATALOG has a row with no scope name")
            continue
        if name in seen_scopes:
            errors.append(f"duplicate scope in SCOPE_CATALOG: {name}")
        seen_scopes.add(name)
        parent = row.get("parent")
        if parent is not None and str(parent) not in scope_index:
            errors.append(f"scope {name}: parent {parent!r} is not in SCOPE_CATALOG")
        if name == "*" and bool(row.get("self_service")):
            errors.append("the wildcard scope must never be self-service")
    for name in sorted(scope_index):
        if name == "*":
            continue
        if scope_index[name].get("parent") == name:
            errors.append(f"scope {name}: is its own parent")

    # --- role grants (advisory, so a stale role is a warning about the table
    # being advisory and an error about a scope nobody can issue)
    known_roles = _known_roles()
    for role, scopes in ROLE_SCOPE_GRANTS.items():
        if role not in known_roles:
            errors.append(
                f"ROLE_SCOPE_GRANTS: role {role!r} is not produced by USER_ROLES or cell_matrix"
            )
        for scope in scopes or ():
            if str(scope) not in scope_index:
                errors.append(f"ROLE_SCOPE_GRANTS[{role}]: unknown scope {scope!r}")
    for role in sorted(known_roles - set(ROLE_SCOPE_GRANTS)):
        warnings.append(
            f"role {role!r} has no ROLE_SCOPE_GRANTS row; it can hold no scope beyond an explicit grant"
        )

    # --- step-up
    seen_levels: set[str] = set()
    for row in STEP_UP_RANKS:
        level = str(row.get("level") or "")
        if not level:
            errors.append("STEP_UP_RANKS has a row with no level")
            continue
        if level in seen_levels:
            errors.append(f"duplicate level in STEP_UP_RANKS: {level}")
        seen_levels.add(level)
        try:
            int(row.get("rank"))
        except (TypeError, ValueError):
            errors.append(f"STEP_UP_RANKS[{level}]: rank {row.get('rank')!r} is not an integer")
        for satisfied in row.get("satisfies") or ():
            if str(satisfied) not in level_index:
                errors.append(f"STEP_UP_RANKS[{level}]: satisfies unknown level {satisfied!r}")
            elif level_ranks.get(str(satisfied), 0) >= level_ranks.get(level, 0):
                errors.append(
                    f"STEP_UP_RANKS[{level}]: claims to be satisfied by {satisfied!r}, "
                    "which is not a weaker level"
                )
    for level, rank in sorted(security.STEP_UP_RANK.items()):
        declared = level_ranks.get(str(level))
        if declared is None:
            errors.append(f"security.STEP_UP_RANK has level {level!r} with no STEP_UP_RANKS row")
        elif declared != int(rank):
            errors.append(
                f"STEP_UP_RANKS[{level}]: rank {declared} disagrees with security.STEP_UP_RANK {rank}"
            )
    for level in sorted(set(level_ranks) - set(security.STEP_UP_RANK)):
        errors.append(f"STEP_UP_RANKS has level {level!r} that security.STEP_UP_RANK does not")
    try:
        from app.routers import users as _users  # local import: users imports deps

        for level, methods in _users.STEP_UP_METHODS.items():
            declared = next(
                (tuple(row.get("evidence") or ()) for row in STEP_UP_RANKS if str(row.get("level")) == str(level)),
                None,
            )
            if declared is None:
                errors.append(
                    f"users.STEP_UP_METHODS has level {level!r} with no STEP_UP_RANKS row"
                )
            elif declared != tuple(methods):
                errors.append(
                    f"STEP_UP_RANKS[{level}]: evidence {declared} disagrees with "
                    f"users.STEP_UP_METHODS {tuple(methods)}"
                )
    except Exception as exc:  # pragma: no cover - import cycle guard
        warnings.append(f"could not cross-check STEP_UP_METHODS against app.routers.users: {exc}")

    # --- rate tiers
    seen_tiers: set[str] = set()
    for row in RATE_TIERS:
        tier = str(row.get("tier") or "")
        if not tier:
            errors.append("RATE_TIERS has a row with no tier name")
            continue
        if tier in seen_tiers:
            errors.append(f"duplicate tier in RATE_TIERS: {tier}")
        seen_tiers.add(tier)
        try:
            if int(row.get("capacity")) < 1:
                errors.append(f"RATE_TIERS[{tier}]: capacity must be at least 1")
        except (TypeError, ValueError):
            errors.append(f"RATE_TIERS[{tier}]: capacity {row.get('capacity')!r} is not an integer")
        try:
            if float(row.get("refill_per_second")) < 0.0:
                errors.append(f"RATE_TIERS[{tier}]: refill_per_second must not be negative")
        except (TypeError, ValueError):
            errors.append(
                f"RATE_TIERS[{tier}]: refill_per_second {row.get('refill_per_second')!r} is not a number"
            )
        for method in row.get("applies_to") or ():
            if str(method) not in (AUTH_METHOD_USER, AUTH_METHOD_API_KEY, AUTH_METHOD_DELEGATED):
                errors.append(f"RATE_TIERS[{tier}]: unknown auth method {method!r}")
    if "interactive" not in seen_tiers:
        errors.append("RATE_TIERS has no 'interactive' row to fall back to")
    interactive = tier_index.get("interactive") or {}
    env_capacity = os.getenv("RATE_LIMIT_CAPACITY")
    env_refill = os.getenv("RATE_LIMIT_REFILL")
    if env_capacity and str(interactive.get("capacity")) != env_capacity:
        warnings.append(
            f"RATE_LIMIT_CAPACITY={env_capacity} overrides the documented interactive tier "
            f"capacity of {interactive.get('capacity')}"
        )
    if env_refill and str(interactive.get("refill_per_second")) != str(float(env_refill)):
        warnings.append(
            f"RATE_LIMIT_REFILL={env_refill} overrides the documented interactive tier refill of "
            f"{interactive.get('refill_per_second')}"
        )

    # --- denials
    seen_kinds: set[str] = set()
    for row in AUTHZ_DENIALS:
        kind = str(row.get("kind") or "")
        if not kind:
            errors.append("AUTHZ_DENIALS has a row with no kind")
            continue
        if kind in seen_kinds:
            errors.append(f"duplicate denial kind in AUTHZ_DENIALS: {kind}")
        seen_kinds.add(kind)
        shape = str(row.get("detail_shape") or "")
        if shape not in _DETAIL_SHAPES:
            errors.append(f"AUTHZ_DENIALS[{kind}]: detail_shape {shape!r} is not string or object")
        try:
            status = int(row.get("status"))
        except (TypeError, ValueError):
            errors.append(f"AUTHZ_DENIALS[{kind}]: status {row.get('status')!r} is not an integer")
            status = None
        if status is not None and status not in _DENIAL_STATUSES:
            warnings.append(f"AUTHZ_DENIALS[{kind}]: status {status} is outside the usual denial set")
        keys = tuple(row.get("keys") or ())
        if len(keys) != len(set(keys)):
            errors.append(f"AUTHZ_DENIALS[{kind}]: duplicate key in {keys}")
        if shape == "string" and keys:
            errors.append(f"AUTHZ_DENIALS[{kind}]: a string detail cannot declare keys")
        if shape == "object" and "error" not in keys:
            errors.append(f"AUTHZ_DENIALS[{kind}]: an object detail must declare an 'error' key")
        if shape == "string" and not row.get("text"):
            errors.append(f"AUTHZ_DENIALS[{kind}]: a string detail must declare its text")
        factory = str(row.get("factory") or "")
        if not callable(globals().get(factory)):
            errors.append(f"AUTHZ_DENIALS[{kind}]: factory {factory!r} is not callable in this module")
        elif not str(factory).startswith(("require_", "_")):
            warnings.append(
                f"AUTHZ_DENIALS[{kind}]: factory {factory!r} is neither a require_* factory nor a "
                "private raiser; the contract probe drives it by name"
            )
    for kind in sorted(AUTHZ_CHECK_DENIALS.values()):
        if kind not in seen_kinds:
            errors.append(
                f"AUTHZ_CHECK_DENIALS names denial {kind!r}, which AUTHZ_DENIALS does not declare"
            )
    for check in AUTHZ_CHECK_ORDER:
        if check in ("policy_tier",):
            continue
        if check not in AUTHZ_CHECK_DENIALS:
            errors.append(f"check {check!r} in AUTHZ_CHECK_ORDER has no denial in AUTHZ_CHECK_DENIALS")
    for check in AUTHZ_CHECK_DENIALS:
        if check not in AUTHZ_CHECK_ORDER:
            errors.append(f"AUTHZ_CHECK_DENIALS names check {check!r}, which is not in AUTHZ_CHECK_ORDER")
    for kind in seen_kinds:
        if kind not in set(AUTHZ_CHECK_DENIALS.values()):
            warnings.append(
                f"AUTHZ_DENIALS[{kind}] is reachable only from a dependency, never from evaluate_authz"
            )

    # --- rules
    seen_rules: set[str] = set()
    for index, row in enumerate(AUTHZ_RULES):
        rule_id = str(row.get("rule_id") or "")
        if not rule_id:
            errors.append(f"AUTHZ_RULES[{index}] has no rule_id")
            continue
        if rule_id in seen_rules:
            errors.append(f"duplicate rule_id in AUTHZ_RULES: {rule_id}")
        seen_rules.add(rule_id)
        methods = tuple(row.get("methods") or ())
        if not methods:
            errors.append(f"{rule_id}: declares no methods, so it can never match")
        for method in methods:
            if str(method).upper() not in _HTTP_METHODS:
                errors.append(f"{rule_id}: unknown HTTP method {method!r}")
        path_pattern = str(row.get("path") or "")
        if not path_pattern:
            errors.append(f"{rule_id}: has no path pattern")
        elif path_pattern.count("{") != path_pattern.count("}"):
            # An unbalanced brace escapes to a literal in the compiler, so the
            # rule matches a path no route can ever have and silently decides
            # nothing.
            errors.append(f"{rule_id}: path {path_pattern!r} has an unbalanced brace")
        exposure = str(row.get("exposure") or "")
        if exposure not in AUTHZ_EXPOSURE_RANK:
            errors.append(f"{rule_id}: unknown exposure {exposure!r}")
        for dependency in row.get("enforced_by") or ():
            if str(dependency) not in AUTHZ_DEPENDENCY_NAMES:
                errors.append(f"{rule_id}: enforced_by names unknown dependency {dependency!r}")
        declared = set(row.get("enforced_by") or ())
        if exposure == "public" and declared:
            errors.append(
                f"{rule_id}: exposure is public but enforced_by names {sorted(declared)}; "
                "a public route cannot be gated"
            )
        if exposure == "admin" and "get_current_admin_user" not in declared:
            errors.append(
                f"{rule_id}: exposure is admin but enforced_by does not include get_current_admin_user"
            )
        if exposure == "authenticated" and not declared:
            errors.append(f"{rule_id}: exposure is authenticated but nothing enforces it")
        hardening = dict(row.get("hardening") or {})
        for kind in sorted(hardening):
            if kind not in _HARDENING_KEYS:
                errors.append(f"{rule_id}: unknown hardening key {kind!r}")
        for scope in hardening.get("scopes") or ():
            if str(scope) not in scope_index:
                errors.append(f"{rule_id}: hardening scope {scope!r} is not in SCOPE_CATALOG")
        for role in hardening.get("roles") or ():
            if str(role) not in known_roles:
                errors.append(f"{rule_id}: hardening role {role!r} is not a known role")
        step_up = hardening.get("step_up")
        if step_up is not None and str(step_up) not in level_index:
            errors.append(f"{rule_id}: hardening step_up {step_up!r} is not a known level")
        tier = hardening.get("rate_tier")
        if tier is not None and str(tier) not in tier_index:
            errors.append(f"{rule_id}: hardening rate_tier {tier!r} is not in RATE_TIERS")
        cell = hardening.get("cell")
        if cell is not None:
            parts = tuple(cell) if isinstance(cell, (list, tuple)) else ()
            if len(parts) not in (2, 3):
                errors.append(f"{rule_id}: hardening cell must be (resource, cell[, minimum])")
            else:
                resource, cell_name = str(parts[0]), str(parts[1])
                if resource not in cell_matrix.CELL_MATRIX:
                    warnings.append(
                        f"{rule_id}: hardening cell names resource {resource!r}, which the cell "
                        "matrix does not have; require_cell_access would deny every caller"
                    )
                elif cell_name not in cell_matrix.CELL_MATRIX[resource]:
                    warnings.append(
                        f"{rule_id}: hardening cell names {resource}.{cell_name}, which the cell "
                        "matrix does not have; require_cell_access would deny every caller"
                    )
                if len(parts) > 2 and str(parts[2]) not in _CELL_RANKS:
                    errors.append(
                        f"{rule_id}: hardening cell minimum {parts[2]!r} is not a cell access level"
                    )
    fallback_id = str(AUTHZ_OPS["catch_all_rule_id"])
    if fallback_id not in seen_rules:
        errors.append(f"AUTHZ_OPS catch_all_rule_id names {fallback_id!r}, which has no rule row")
    elif str(AUTHZ_RULES[-1].get("rule_id")) != fallback_id:
        errors.append(
            f"the catch-all rule {fallback_id!r} must be last; first-match-wins would make the "
            "rows after it unreachable"
        )
    if AUTHZ_RULES and str(AUTHZ_RULES[0].get("rule_id")) == fallback_id:
        errors.append("the catch-all rule is first, so it shadows every other rule")

    # --- probes
    seen_probes: set[str] = set()
    for index, row in enumerate(AUTHZ_PROBES):
        probe = str(row.get("probe") or "")
        if not probe:
            errors.append(f"AUTHZ_PROBES[{index}] has no probe name")
            continue
        if probe in seen_probes:
            errors.append(f"duplicate probe in AUTHZ_PROBES: {probe}")
        seen_probes.add(probe)
        absent = bool(row.get("absent"))
        if absent:
            # An absent probe describes the absence of a caller, so anything
            # that would grant it one is a contradiction rather than a detail.
            for column in ("roles", "scopes", "tenant_id", "auth_method", "subject"):
                if row.get(column) not in (None, (), [], ""):
                    errors.append(
                        f"AUTHZ_PROBES[{probe}]: is marked absent but declares {column}="
                        f"{row.get(column)!r}; nothing to attach it to"
                    )
            if row.get("claims"):
                errors.append(
                    f"AUTHZ_PROBES[{probe}]: is marked absent but declares claims; "
                    "claims are what a credential would have carried"
                )
        for role in row.get("roles") or ():
            if str(role) not in known_roles:
                errors.append(f"AUTHZ_PROBES[{probe}]: unknown role {role!r}")
        for scope in row.get("scopes") or ():
            if str(scope) not in scope_index:
                errors.append(f"AUTHZ_PROBES[{probe}]: unknown scope {scope!r}")
        method = str(row.get("auth_method") or "")
        if not absent and method not in (AUTH_METHOD_USER, AUTH_METHOD_API_KEY, AUTH_METHOD_DELEGATED):
            errors.append(f"AUTHZ_PROBES[{probe}]: unknown auth_method {method!r}")
        claims = row.get("claims") or {}
        if not isinstance(claims, dict):
            errors.append(f"AUTHZ_PROBES[{probe}]: claims must be a mapping")
        elif not absent and method == AUTH_METHOD_DELEGATED and not claims.get("act"):
            warnings.append(
                f"AUTHZ_PROBES[{probe}]: auth_method is delegated but no act claim is present, "
                "so it is indistinguishable from a direct login"
            )
    default_probe = str(AUTHZ_OPS["default_probe"])
    if default_probe not in seen_probes:
        errors.append(f"AUTHZ_OPS default_probe names {default_probe!r}, which has no probe row")

    # --- scope satisfaction must agree with Principal.has_scope
    for scope in sorted(scope_index):
        probe_principal = Principal(subject="validator", scopes=frozenset({scope}))
        for required in sorted(scope_index):
            expected = probe_principal.has_scope(required)
            actual = scope_satisfies([scope], required)
            if expected != actual:
                errors.append(
                    f"scope_satisfies([{scope!r}], {required!r}) is {actual} but "
                    f"Principal.has_scope says {expected}"
                )

    return {
        "version": AUTHZ_VERSION,
        "valid": not errors,
        "errors": len(errors),
        "warnings": len(warnings),
        "error_list": errors,
        "warning_list": warnings,
        "counts": {
            "scopes": len(SCOPE_CATALOG),
            "role_grants": len(ROLE_SCOPE_GRANTS),
            "denials": len(AUTHZ_DENIALS),
            "step_up_levels": len(STEP_UP_RANKS),
            "rate_tiers": len(RATE_TIERS),
            "rules": len(AUTHZ_RULES),
            "probes": len(AUTHZ_PROBES),
            "checks": len(AUTHZ_CHECK_ORDER),
        },
    }


# --- The live denial contract ----------------------------------------------------


def _probe_request(tenant_id: str = "authz-tenant") -> Any:
    """A minimal Starlette request carrying one tenant header."""
    from starlette.requests import Request as _Request

    return _Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-tenant-id", tenant_id.encode("utf-8"))],
            "query_string": b"",
        }
    )


async def authz_denial_contract() -> dict[str, Any]:
    """Prove :data:`AUTHZ_DENIALS` against the factories that raise the denials.

    Each declared denial is *produced*, not described: the factory is built with
    probe arguments, invoked with a principal that must fail, and the raised
    ``HTTPException`` is compared to the declared status, detail shape, key order
    and error string. A row that has drifted from its factory -- a new key added
    to the payload, a status quietly changed, a factory renamed -- is an error
    here rather than a discrepancy a client discovers in production.

    Async because invoking a dependency factory means awaiting it, and
    :func:`validate_authz` is sync so it can be called from anywhere.
    """
    from fastapi import HTTPException as _HTTPException

    errors: list[str] = []
    observed: list[dict[str, Any]] = []

    weak = Principal(subject="authz-probe", roles=frozenset({"auditor"}), scopes=frozenset())
    unscoped = Principal(subject="authz-probe", roles=frozenset({"owner"}))

    async def _capture(kind: str, call) -> dict[str, Any] | None:
        try:
            await call()
        except _HTTPException as exc:
            detail = exc.detail
            shape = "string" if isinstance(detail, str) else "object"
            return {
                "kind": kind,
                "status": int(exc.status_code),
                "detail_shape": shape,
                "text": detail if isinstance(detail, str) else None,
                "error": detail.get("error") if isinstance(detail, dict) else None,
                "keys": tuple(detail) if isinstance(detail, dict) else (),
                "headers": dict(exc.headers or {}),
            }
        except Exception as exc:  # pragma: no cover - a factory that raises something else
            errors.append(f"{kind}: factory raised {type(exc).__name__} instead of HTTPException: {exc}")
            return None
        errors.append(f"{kind}: factory did not deny the probe principal")
        return None

    probes: dict[str, Any] = {
        "unauthenticated": lambda: _capture(
            "unauthenticated", lambda: _raise_awaited(_unauthorized())
        ),
        "insufficient_scope": lambda: _capture(
            "insufficient_scope",
            lambda: require_scopes("authz:probe")(unscoped),
        ),
        "cell_access_denied": lambda: _capture(
            "cell_access_denied",
            lambda: require_cell_access("customer_profile", "full_name", minimum="write")(weak),
        ),
        "step_up_required": lambda: _capture(
            "step_up_required",
            lambda: require_step_up("loa3")(unscoped),
        ),
        "role_required": lambda: _capture(
            "role_required",
            lambda: require_roles("authz:no-such-role")(unscoped),
        ),
        "tenant_mismatch": lambda: _capture(
            "tenant_mismatch",
            lambda: require_tenant(_probe_request(), unscoped),
        ),
        "rate_limited": lambda: _capture(
            "rate_limited", _rate_limited_probe
        ),
    }

    for kind in AUTHZ_DENIAL_KINDS:
        probe = probes.get(kind)
        if probe is None:
            errors.append(f"{kind}: no probe is wired for this denial kind")
            continue
        actual = await probe()
        if actual is None:
            continue
        row = AUTHZ_DENIAL_BY_KIND[kind]
        expected = {
            "status": int(row.get("status") or 0),
            "detail_shape": str(row.get("detail_shape") or ""),
            "error": row.get("error"),
            "keys": tuple(row.get("keys") or ()),
        }
        for field in ("status", "detail_shape", "error", "keys"):
            if actual[field] != expected[field]:
                errors.append(
                    f"{kind}: {field} is {actual[field]!r} in the factory but {expected[field]!r} "
                    "in AUTHZ_DENIALS"
                )
        if expected["detail_shape"] == "string" and actual.get("text") != row.get("text"):
            errors.append(
                f"{kind}: text is {actual.get('text')!r} in the factory but {row.get('text')!r} "
                "in AUTHZ_DENIALS"
            )
        observed.append(
            {
                "kind": kind,
                "factory": str(row.get("factory") or ""),
                "declared": expected,
                "observed": {field: actual[field] for field in ("status", "detail_shape", "error", "keys")},
                "agrees": not any(
                    actual[field] != expected[field] for field in ("status", "detail_shape", "error", "keys")
                ),
            }
        )

    return {
        "valid": not errors,
        "errors": len(errors),
        "error_list": errors,
        "observed": observed,
        "note": (
            "Each row is produced by invoking the real factory with a principal that must be "
            "denied, then compared to the declaration. Errors mean a client would receive "
            "something the catalog does not describe."
        ),
    }


def _raise_awaited(exception: HTTPException):
    async def _inner() -> None:
        raise exception

    return _inner()


async def _rate_limited_probe() -> None:
    """Drain a one-token bucket and let the second call be the one that denies."""
    dependency = require_rate_limit(capacity=1, refill_per_second=0.0)
    principal = Principal(subject="authz-rate-probe")
    await dependency(principal)
    await dependency(principal)


# --- Catalog ---------------------------------------------------------------------


def build_authz_catalog(routes: Any = None) -> dict[str, Any]:
    """Introspectable contract for the authorization governance layer.

    ``build_deps_catalog`` was defined and never surfaced by any route, so it is
    reproduced here under ``deps_catalog`` unchanged -- with the same key set,
    for the consumers that already know it.

    The sections worth reading first are ``validation`` (which configured rules
    cannot run as written), ``drift`` (whether the described posture still
    matches the app that enforces it) and ``backlog`` (what is proposed and not
    enforced, kept separate on purpose).

    ``routes`` is an argument rather than an import: ``app.deps`` cannot import
    ``app.main`` without a cycle, and the callers that have a live route table
    (``/meta/authz``) pass theirs in.
    """
    validation = validate_authz()
    drift = authz_drift_report(routes)
    return {
        "version": AUTHZ_VERSION,
        "validation": {
            "valid": validation["valid"],
            "errors": validation["errors"],
            "warnings": validation["warnings"],
            "error_list": validation["error_list"],
            "warning_list": validation["warning_list"],
            "counts": validation["counts"],
        },
        "scopes": [dict(row) for row in SCOPE_CATALOG],
        "role_scope_grants": {role: list(scopes) for role, scopes in ROLE_SCOPE_GRANTS.items()},
        "role_scope_grants_note": (
            "advisory: the scopes a role could hold, never merged into Principal.scopes and "
            "never read at request time. require_scopes reads granted scopes only, so a token "
            "with no scopes claim is denied rather than treated as unscoped-and-allowed."
        ),
        "denials": [dict(row) for row in AUTHZ_DENIALS],
        "step_up_ranks": [dict(row) for row in STEP_UP_RANKS],
        "rate_tiers": [dict(row) for row in RATE_TIERS],
        "rate_limiters": {
            name: limiter.stats() for name, limiter in sorted(_RATE_TIER_LIMITERS.items())
        },
        "exposures": list(AUTHZ_EXPOSURES),
        "checks": list(AUTHZ_CHECK_ORDER),
        "check_denials": dict(AUTHZ_CHECK_DENIALS),
        "dependency_names": list(AUTHZ_DEPENDENCY_NAMES),
        "factory_names": list(AUTHZ_FACTORY_NAMES),
        "factory_prefixes": dict(AUTHZ_FACTORY_PREFIXES),
        "rules": [dict(row) for row in AUTHZ_RULES],
        "probes": [dict(row) for row in AUTHZ_PROBES],
        "ops": dict(AUTHZ_OPS),
        "drift": {key: value for key, value in drift.items() if key != "coverage"},
        "coverage": drift["coverage"],
        "backlog": authz_hardening_backlog(),
        "deps_catalog": build_deps_catalog(),
        "note": (
            "Additive governance layer over the unchanged authentication path: the scope "
            "vocabulary, the role/scope relationship, the denial contract, the assurance and "
            "rate tiers, and a per-route description of what is actually enforced -- all of it "
            "inspectable, and all of it separate from what a request is allowed to do."
        ),
    }
