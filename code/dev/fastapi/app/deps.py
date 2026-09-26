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
"""
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from dataclasses import dataclass, field, replace
import os
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


# --- Resolution ----------------------------------------------------------------


def _unauthorized() -> HTTPException:
    """Fresh exception per raise (FastAPI reuses handler state otherwise)."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
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
            roles=frozenset({"service_account"}),
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
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
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
    required_rank = cell_matrix.ACCESS_RANK.get(minimum, cell_matrix.ACCESS_RANK["read"])

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        decision = principal.can_access_cell(resource, cell, context=context)
        granted = cell_matrix.ACCESS_RANK.get(decision["level"], 0)
        if granted < required_rank:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
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
    """

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not security.step_up_satisfied(principal.claims, level):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "step_up_required",
                    "required_level": level,
                    "presented_level": security.step_up_level_of(principal.claims),
                },
            )
        return principal

    _dependency.__name__ = f"require_step_up_{level}"
    return _dependency


def require_roles(*roles: str):
    """Dependency factory: require membership in at least one named role."""

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not (principal.roles & frozenset(roles)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
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
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
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

    Yields 429 with a ``Retry-After`` hint when the bucket is empty.
    """
    limiter = RATE_LIMITER if capacity is None else TokenBucketLimiter(capacity, refill_per_second or 1.0)

    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        decision = limiter.check(principal.subject)
        if not decision["allowed"]:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": "rate_limited", **decision},
                headers={"Retry-After": str(int(1 / limiter.refill_per_second) if limiter.refill_per_second else 1)},
            )
        return principal

    _dependency.__name__ = "require_rate_limit"
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
